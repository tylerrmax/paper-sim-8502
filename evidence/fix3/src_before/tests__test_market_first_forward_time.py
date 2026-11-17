# -*- coding: utf-8 -*-
"""Market-First P0-1：Forward 时间规则（代码强制）。

规则：entry 必须是发现事件实际创建时间（discovered_at 的上海
日历日期）之后第一个交易日，benchmark 取该日收盘；
entry <= discovered_date 一律强制 backfill；无法确认下一
交易日/价格时拒绝创建并在 candidate_history 留痕。
"""
import pytest

from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.validation import (ForwardRejectedError, create_sample,
                                     exclude_sample, forward_stats,
                                     get_sample)

# 2026-11-13 周五、11-16 周一、11-17 周二
BARS = [
    ("2026-11-12", 25.0, 25.5, 24.5, 25.2, 1000.0),
    ("2026-11-13", 26.0, 26.5, 25.8, 26.25, 1000.0),
    ("2026-11-16", 26.8, 27.2, 26.6, 27.00, 1000.0),
    ("2026-11-17", 27.1, 27.4, 26.9, 27.30, 1000.0),
]


def _pool_with_event(tmp_path, discovered_at, source_id="inst_lhb_live"):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id=source_id, discovered_at=discovered_at,
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    ev_row = pool.events_for(cid)[0]
    return pool, cid, ev_row["id"]


def test_forward_entry_is_next_trading_day_after_close_discovery(tmp_path):
    # 2026-11-13 周五盘后发现 -> entry 为下周一 2026-11-16
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-13T16:30:00+08:00")
    sid = create_sample(pool, cid, "inst_lhb_live", kind="forward",
                        discovery_event_id=ev_id, bars=BARS)
    s = get_sample(pool, sid)
    assert s["kind"] == "forward"
    assert s["entry_date"] == "2026-11-16"
    assert s["benchmark_price"] == pytest.approx(27.00)  # 11-16 收盘
    assert s["benchmark_date"] == "2026-11-16"
    assert s["benchmark_price_source"]
    assert s["discovery_event_id"] == ev_id


def test_forward_intraday_discovery_still_next_trading_day(tmp_path):
    # 盘中 10:00 发现：当日收盘尚不可执行，entry 仍为下一交易日
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-13T10:00:00+08:00")
    sid = create_sample(pool, cid, "inst_lhb_live", kind="forward",
                        discovery_event_id=ev_id, bars=BARS)
    s = get_sample(pool, sid)
    assert s["entry_date"] == "2026-11-16"
    assert s["benchmark_price"] == pytest.approx(27.00)


def test_forward_weekend_discovery_entry_monday(tmp_path):
    # 非交易日（周六）发现 -> entry 为下周一
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-14T12:00:00+08:00")
    sid = create_sample(pool, cid, "inst_lhb_live", kind="forward",
                        discovery_event_id=ev_id, bars=BARS)
    assert get_sample(pool, sid)["entry_date"] == "2026-11-16"


def test_entry_before_discovery_forced_backfill(tmp_path):
    # 发现日晚于所请 entry（原样本 1 的错误形态）：请求 forward
    # 也必须被代码强制改类 backfill，不信任调用方 kind
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-16T15:58:00+08:00")
    sid = create_sample(pool, cid, "inst_lhb_live",
                        entry_date="2026-11-13", benchmark_price=26.25,
                        kind="forward", discovery_event_id=ev_id,
                        bars=BARS)
    s = get_sample(pool, sid)
    assert s["kind"] == "backfill"
    assert s["entry_date"] == "2026-11-13"
    assert s["benchmark_price"] == pytest.approx(26.25)


def test_historical_backfill_long_gap(tmp_path):
    # 历史数据补取：entry 早于发现多日 -> backfill
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-16T09:00:00+08:00",
        source_id="inst_lhb_stored")
    sid = create_sample(pool, cid, "inst_lhb_stored",
                        entry_date="2026-10-21", benchmark_price=8.63,
                        kind="backfill", discovery_event_id=ev_id)
    s = get_sample(pool, sid)
    assert s["kind"] == "backfill"
    assert s["benchmark_date"] == "2026-10-21"


def test_forward_price_missing_rejected_and_traced(tmp_path):
    # 下一交易日无 bar（价格缺失）-> 拒绝创建 + 留痕，不猜测
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-13T16:30:00+08:00")
    bars_up_to_discovery = [b for b in BARS if b[0] <= "2026-11-13"]
    with pytest.raises(ForwardRejectedError):
        create_sample(pool, cid, "inst_lhb_live", kind="forward",
                      discovery_event_id=ev_id,
                      bars=bars_up_to_discovery)
    hist = [h for h in pool.history_for(cid)
            if "forward_rejected:" in (h["reason"] or "")]
    assert len(hist) == 1
    assert hist[0]["actor"] == "validation"
    n = pool.conn.execute(
        "SELECT COUNT(*) c FROM validation_samples").fetchone()["c"]
    assert n == 0  # 拒绝后不落任何样本行


def test_forward_without_discovery_event_rejected(tmp_path):
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-13T16:30:00+08:00")
    pool.conn.execute("DELETE FROM discovery_events WHERE id=?",
                      (ev_id,))
    pool.conn.commit()
    with pytest.raises(ForwardRejectedError):
        create_sample(pool, cid, "inst_lhb_live", kind="forward",
                      bars=BARS)


def test_excluded_sample_not_in_forward_stats_and_row_unchanged(tmp_path):
    # 模拟存量遗留 forward 行（修复前口径写入）：排除后不进统计，
    # 且原始行逐字段不变
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-16T15:58:00+08:00")
    cur = pool.conn.execute(
        "INSERT INTO validation_samples (candidate_id, source_id,"
        " kind, entry_date, benchmark_price, maturity_status,"
        " created_at) VALUES (?, 'inst_lhb_live', 'forward',"
        " '2026-11-13', 26.25, 'immature', '2026-11-16T07:58:48+00:00')",
        (cid,))
    legacy_id = cur.lastrowid
    pool.conn.commit()
    before = pool.conn.execute(
        "SELECT * FROM validation_samples WHERE id=?",
        (legacy_id,)).fetchone()
    before = dict(before)
    assert forward_stats(pool)["total"] == 1  # 排除前在统计内

    exclude_sample(pool, legacy_id, "forward_stats",
                   "独立审计裁定：entry 早于发现创建，改类演示样本",
                   decided_by="independent_audit_2026-11-16")
    stats = forward_stats(pool)
    assert stats["total"] == 0
    assert stats["excluded_sample_ids"] == [legacy_id]
    after = dict(pool.conn.execute(
        "SELECT * FROM validation_samples WHERE id=?",
        (legacy_id,)).fetchone())
    assert after == before  # 原始行逐字段全等，未被改动
