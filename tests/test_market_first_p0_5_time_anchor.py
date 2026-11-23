# -*- coding: utf-8 -*-
"""P0-5 集成对抗测试（2026-11-23，judge-p0-integration）：Forward 时间锚。

审计口径：discovered_at 是调用方声称值、不再作为可信入场
时间锚；以服务器实际入库时间（discovery_events.created_at，
由 pool.ingest 以服务器时间落字）为唯一可信锚；Forward 入场
必须严格晚于锚对应的交易日；延迟入库的历史事件强制
backfill 或拒绝；缺少可信入库时间时拒绝进入 Forward；
历史行情不得进入有效 Forward 统计。
"""
from datetime import date, timedelta

import pytest

from market_first import validation
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool


def _bars(start=date(2026, 9, 1), end=date(2026, 12, 20)):
    out = []
    d = start
    i = 0
    while d <= end:
        if d.weekday() < 5:
            c = 10.0 + 0.01 * i
            out.append((d.isoformat(), c, c, c, c, 1000.0))
            i += 1
        d += timedelta(days=1)
    return out


BARS = _bars()


def _pool_with_event(tmp_path, discovered_at, ingested_at,
                     source_id="inst_lhb_live"):
    """ingest 后把 discovery_events.created_at 改写为给定的
    服务器入库时刻（模拟延迟入库：事件声称的发现时间与实际
    入库时间分离）。ingested_at=None 表示入库时间缺失。"""
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id=source_id, discovered_at=discovered_at,
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    pool.conn.execute(
        "UPDATE discovery_events SET created_at=?"
        " WHERE candidate_id=?", (ingested_at, cid))
    pool.conn.commit()
    ev_row = pool.events_for(cid)[0]
    return pool, cid, ev_row["id"]


def test_sept_event_ingested_in_nov_explicit_entry_forced_backfill(tmp_path):
    """真实对抗：事件声称 2026-09-15 发现、实际 2026-11-20
    才入库；以 9 月 entry 请求 forward -> 强制 backfill，
    且不进入 forward_stats。"""
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-09-15T16:30:00+08:00",
        "2026-11-20T09:00:00+08:00")
    sid = validation.create_sample(
        pool, cid, "inst_lhb_live", entry_date="2026-09-16",
        benchmark_price=10.10, kind="forward",
        discovery_event_id=ev_id, bars=BARS)
    s = validation.get_sample(pool, sid)
    assert s["kind"] == "backfill"
    assert s["entry_date"] == "2026-09-16"
    assert validation.forward_stats(pool)["total"] == 0


def test_sept_event_ingested_in_nov_history_bars_only_rejected(tmp_path):
    """同一延迟入库事件：只给到 10 月底的历史行情请求
    forward -> 锚（11-20）之后无 bar，拒绝创建、不落样本，
    历史行情不得拼出 forward。"""
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-09-15T16:30:00+08:00",
        "2026-11-20T09:00:00+08:00")
    hist_bars = [b for b in BARS if b[0] <= "2026-10-30"]
    with pytest.raises(validation.ForwardRejectedError):
        validation.create_sample(
            pool, cid, "inst_lhb_live", kind="forward",
            discovery_event_id=ev_id, bars=hist_bars)
    n = pool.conn.execute(
        "SELECT COUNT(*) c FROM validation_samples").fetchone()["c"]
    assert n == 0


def test_sept_event_ingested_in_nov_entry_anchored_at_ingest(tmp_path):
    """延迟入库事件的 forward 入场只能锚定入库日之后：
    entry 必须是 2026-11-18（周三，实际入库日）之后第一
    个交易日 2026-11-19，绝不允许以 9 月行情作 forward
    入场。（入库日取 11-18 而非更晚：entry 须为已收盘确认
    的历史交易日，避免与运行当日未收盘 bar 相撞。）"""
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-09-15T16:30:00+08:00",
        "2026-11-18T09:00:00+08:00")
    sid = validation.create_sample(
        pool, cid, "inst_lhb_live", kind="forward",
        discovery_event_id=ev_id, bars=BARS)
    s = validation.get_sample(pool, sid)
    assert s["kind"] == "forward"
    assert s["entry_date"] == "2026-11-19"
    assert s["benchmark_date"] == "2026-11-19"


def test_missing_ingest_time_rejects_forward(tmp_path):
    """事件行缺服务器入库时间（created_at 为 NULL）：无可信
    锚 -> 拒绝进入 Forward 并留痕；同事件显式 backfill
    不受影响。"""
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-13T16:30:00+08:00", None)
    with pytest.raises(validation.ForwardRejectedError):
        validation.create_sample(
            pool, cid, "inst_lhb_live", kind="forward",
            discovery_event_id=ev_id, bars=BARS)
    hist = [h for h in pool.history_for(cid)
            if "trusted_ingest_time_missing" in (h["reason"] or "")]
    assert len(hist) == 1
    sid = validation.create_sample(
        pool, cid, "inst_lhb_live", entry_date="2026-11-13",
        benchmark_price=10.05, kind="backfill",
        discovery_event_id=ev_id, bars=BARS)
    assert validation.get_sample(pool, sid)["kind"] == "backfill"


def test_claimed_discovery_after_ingest_does_not_extend_anchor(tmp_path):
    """声称发现时间晚于入库时间时，锚仍以入库时间为准
    （discovered_at 完全不参与锚）：11-13 入库、声称
    11-16 发现 -> forward 入场为锚后第一个交易日 11-16
    （周一），而非声称日之后。"""
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-16T15:58:00+08:00",
        "2026-11-13T16:30:00+08:00")
    sid = validation.create_sample(
        pool, cid, "inst_lhb_live", kind="forward",
        discovery_event_id=ev_id, bars=BARS)
    s = validation.get_sample(pool, sid)
    assert s["kind"] == "forward"
    assert s["entry_date"] == "2026-11-16"


def test_same_day_ingest_positive_control(tmp_path):
    """正对照：发现当日入库（created_at 与 discovered_at
    同日）-> forward 入场为次一交易日，与既有口径一致。"""
    pool, cid, ev_id = _pool_with_event(
        tmp_path, "2026-11-13T16:30:00+08:00",
        "2026-11-13T16:35:00+08:00")
    sid = validation.create_sample(
        pool, cid, "inst_lhb_live", kind="forward",
        discovery_event_id=ev_id, bars=BARS)
    s = validation.get_sample(pool, sid)
    assert s["kind"] == "forward"
    assert s["entry_date"] == "2026-11-16"
