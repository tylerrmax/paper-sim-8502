# -*- coding: utf-8 -*-
"""Market-First fix2-1：Forward benchmark 收盘完整性确认。

不得仅凭 bar 存在认定收盘形成：bar 日期严格早于取数日才
天然确认；当日 bar 须过收盘确认时刻（A股 15:30）；benchmark
一经记录不可变，行情源修订只记录 benchmark_revision_detected，
不静默覆盖；对抗测试：修订评估 + 重复创建 + judge 后
benchmark_price 与建样时全等。
"""
import json

import pytest

from market_first import validation
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.validation import (ForwardRejectedError,
                                     bar_close_confirmed, create_sample,
                                     evaluate_sample, get_sample)

# 2026-11-13 周五、11-16 周一、11-17 周二
BARS = [
    ("2026-11-12", 25.0, 25.5, 24.5, 25.2, 1000.0),
    ("2026-11-13", 26.0, 26.5, 25.8, 26.25, 1000.0),
    ("2026-11-16", 26.8, 27.2, 26.6, 27.00, 1000.0),
    ("2026-11-17", 27.1, 27.4, 26.9, 27.30, 1000.0),
]


def _pool_with_event(tmp_path, discovered_at,
                     source_id="inst_lhb_live"):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id=source_id, discovered_at=discovered_at,
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    # fix3 信任路径（MF-FWD-002）：时间锚以库内服务器侧
    # ingested_at 为准；本 helper 模拟生产不变量「发现即
    # 入库」，把事件 created_at 对齐到模拟发现时刻；
    # 本文件全部断言未改动。
    pool.conn.execute(
        "UPDATE discovery_events SET created_at=?"
        " WHERE candidate_id=?", (ev.discovered_at, cid))
    pool.conn.commit()
    return pool, cid, pool.events_for(cid)[0]["id"]


# ---------------------------------------------------------------------------
# bar_close_confirmed 判定单元
# ---------------------------------------------------------------------------

def test_bar_close_confirmed_time_rules():
    # 历史日 bar：严格早于取数日 -> 确认（与取数时刻无关）
    assert bar_close_confirmed("2026-11-13", "2026-11-17T09:00:00+08:00")
    # 未来日 bar -> 数据异常，不确认
    assert not bar_close_confirmed("2026-11-18",
                                   "2026-11-17T16:00:00+08:00")
    # 当日 bar、盘中取数（10:30 < 15:30）-> 不确认
    assert not bar_close_confirmed("2026-11-17",
                                   "2026-11-17T10:30:00+08:00")
    # 当日 bar、收盘确认时刻 15:30 整 -> 确认
    assert bar_close_confirmed("2026-11-17", "2026-11-17T15:30:00+08:00")
    # 当日 bar、收盘后 16:00 取数 -> 确认
    assert bar_close_confirmed("2026-11-17", "2026-11-17T16:00:00+08:00")
    # provenance 带 intraday 标记 -> 当日 bar 一律不确认
    assert not bar_close_confirmed(
        "2026-11-17", "2026-11-17T16:00:00+08:00",
        provenance="unit-test intraday feed")
    # 港股收盘确认时刻 16:30：16:00 不确认、16:30 确认
    assert not bar_close_confirmed("2026-11-17",
                                   "2026-11-17T16:00:00+08:00",
                                   market="HK")
    assert bar_close_confirmed("2026-11-17", "2026-11-17T16:30:00+08:00",
                               market="HK")


# ---------------------------------------------------------------------------
# create_sample 完整性门禁
# ---------------------------------------------------------------------------

def test_forward_intraday_incomplete_bar_rejected(tmp_path):
    # 11-16（周一）盘后发现，下一交易日为 11-17；11-17 盘中
    # 10:30 取数时当日 bar 尚未收盘确认 -> 拒绝 + 留痕
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-16T15:58:00+08:00")
    with pytest.raises(ForwardRejectedError):
        create_sample(pool, cid, "inst_lhb_live", kind="forward",
                      discovery_event_id=ev_id, bars=BARS,
                      fetched_at="2026-11-17T10:30:00+08:00")
    hist = [h for h in pool.history_for(cid)
            if "benchmark_close_unconfirmed" in (h["reason"] or "")]
    assert len(hist) == 1
    assert hist[0]["actor"] == "validation"
    n = pool.conn.execute(
        "SELECT COUNT(*) c FROM validation_samples").fetchone()["c"]
    assert n == 0


def test_forward_same_day_bar_after_close_confirmed(tmp_path):
    # 同一场景在 11-17 收盘后（16:00）取数 -> 当日 bar 已确认，
    # forward 建成，entry=2026-11-17、benchmark=当日收盘 27.30
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-16T15:58:00+08:00")
    sid = create_sample(pool, cid, "inst_lhb_live", kind="forward",
                        discovery_event_id=ev_id, bars=BARS,
                        fetched_at="2026-11-17T16:00:00+08:00")
    s = get_sample(pool, sid)
    assert s["kind"] == "forward"
    assert s["entry_date"] == "2026-11-17"
    assert s["benchmark_price"] == pytest.approx(27.30)
    assert s["benchmark_date"] == "2026-11-17"


def test_forward_historical_bar_confirmed(tmp_path):
    # 历史补取方向：entry 日（11-16）严格早于取数日（11-17），
    # 即使取数时刻为盘中 09:00 也天然确认
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-13T16:30:00+08:00")
    sid = create_sample(pool, cid, "inst_lhb_live", kind="forward",
                        discovery_event_id=ev_id, bars=BARS,
                        fetched_at="2026-11-17T09:00:00+08:00")
    s = get_sample(pool, sid)
    assert s["entry_date"] == "2026-11-16"
    assert s["benchmark_price"] == pytest.approx(27.00)


def test_backfill_today_entry_intraday_rejected(tmp_path):
    # backfill 同受完整性约束：entry=取数当日且盘中 -> 拒绝
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-16T15:58:00+08:00")
    with pytest.raises(ForwardRejectedError):
        create_sample(pool, cid, "inst_lhb_stored",
                      entry_date="2026-11-17", benchmark_price=27.30,
                      kind="backfill", discovery_event_id=ev_id,
                      fetched_at="2026-11-17T10:30:00+08:00")
    hist = [h for h in pool.history_for(cid)
            if "benchmark_close_unconfirmed" in (h["reason"] or "")]
    assert len(hist) == 1
    # 收盘后同日 entry 则可建成
    sid = create_sample(pool, cid, "inst_lhb_stored",
                        entry_date="2026-11-17", benchmark_price=27.30,
                        kind="backfill", discovery_event_id=ev_id,
                        fetched_at="2026-11-17T16:00:00+08:00")
    assert get_sample(pool, sid)["benchmark_price"] == \
        pytest.approx(27.30)


# ---------------------------------------------------------------------------
# 行情源修订：记录不覆盖 + 对抗测试
# ---------------------------------------------------------------------------

def _revised_bars(entry_close):
    out = []
    for b in BARS:
        if b[0] == "2026-11-13":
            out.append(("2026-11-13", entry_close, entry_close,
                        entry_close, entry_close, 1000.0))
        else:
            out.append(b)
    # 补足 entry 后 20 根（工作日、收盘 30.0）使样本可成熟
    from datetime import date, timedelta
    d = date(2026, 11, 16)
    n = 0
    while n < 20:
        if d.weekday() < 5 and d.isoformat() not in {b[0] for b in out}:
            out.append((d.isoformat(), 30.0, 30.0, 30.0, 30.0, 1000.0))
            n += 1
        d += timedelta(days=1)
    return sorted(out)


def test_source_revision_recorded_benchmark_unchanged(tmp_path):
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-16T15:58:00+08:00")
    sid = create_sample(pool, cid, "inst_lhb_stored",
                        entry_date="2026-11-13", benchmark_price=26.25,
                        kind="backfill", discovery_event_id=ev_id)
    # 行情源修订：同一 entry 日收盘由 26.25 改为 26.80 后评估
    out = evaluate_sample(pool, sid, _revised_bars(26.80))
    assert out["benchmark_price"] == pytest.approx(26.25)  # 不覆盖
    rev = json.loads(out["benchmark_revision_json"])
    assert rev["detected"] is True
    assert rev["recorded_benchmark"] == pytest.approx(26.25)
    assert rev["observed_close"] == pytest.approx(26.80)
    assert rev["entry_date"] == "2026-11-13"
    # 收益仍以原 benchmark 计算：after 收盘序列为
    # [27.00, 27.30, 30.0, 30.0, 30.0, ...]，ret_t5 取第 5 根
    # 30.0 以原 benchmark 26.25 为分母（若被修订值 26.80 覆盖
    # 则结果不同，此断言即不可变的数值证据）
    assert out["ret_t5"] == pytest.approx(30.0 / 26.25 - 1.0)
    hist = [h for h in pool.history_for(cid)
            if h["field"] == "benchmark_revision"]
    assert len(hist) == 1
    assert "benchmark_revision_detected" in hist[0]["reason"]


def test_benchmark_adversarial_immutable(tmp_path):
    """对抗测试：样本建成后，修订 bar 评估 + 重复创建 + judge
    三路并发施压，benchmark_price 必须与建样时全等。"""
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-16T15:58:00+08:00")
    sid = create_sample(pool, cid, "inst_lhb_stored",
                        entry_date="2026-11-13", benchmark_price=26.25,
                        kind="backfill", discovery_event_id=ev_id)
    before = get_sample(pool, sid)["benchmark_price"]
    assert before == pytest.approx(26.25)

    # 施压 1：以修订后的 entry 日收盘评估
    evaluate_sample(pool, sid, _revised_bars(26.80))
    assert get_sample(pool, sid)["benchmark_price"] == before

    # 施压 2：同 (candidate, source, entry) 重复创建 -> 拒绝
    with pytest.raises(validation.DuplicateSampleError):
        create_sample(pool, cid, "inst_lhb_stored",
                      entry_date="2026-11-13", benchmark_price=99.99,
                      kind="backfill", discovery_event_id=ev_id)
    assert get_sample(pool, sid)["benchmark_price"] == before

    # 施压 3：judge（无规则）不得触碰 benchmark
    out = validation.judge_validation(pool, cid)
    assert out["decision"] == "not_evaluable"
    assert get_sample(pool, sid)["benchmark_price"] == before
