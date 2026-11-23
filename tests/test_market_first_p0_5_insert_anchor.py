# -*- coding: utf-8 -*-
"""P0-5 新事件 INSERT 时间锚测试（2026-11-23 V3 审计补正 P0-3）。

缺口（V3 独立审计）：discovery_events.created_at 此前只有
UPDATE 不可变保护，直接 INSERT 可写任意时刻，时间锚的
「新事件」一侧无 DB 层校验。

本批实施（与既有夹具无冲突的部分）：
- trg_rt_discovery_events_created_at_insert_future：
  直接 INSERT 的 created_at 晚于 DB 服务器时间 120 秒
  以上 -> RAISE(ABORT)（未来时间拒绝）；
- 正常服务器时间 INSERT 放行（正对照，含 120 秒容差内）；
- 既有 UPDATE 不可变保护保留（p0_5_created_at_guard 组
  回归实证，本文件亦含一条复核）。

后续落地（2026-11-23 P0-3 完整落地批）：NULL 拒绝与
历史回填拦截已按用户定案的方案 A（受控通道标记）实施
——无有效通道标记的 NULL/回填 INSERT 由
trg_rt_discovery_events_insert_channel 拒绝，经 ingest
通道（含时钟夹具）落字的历史时刻与 NULL 前提带标记合
法通过，见 test_market_first_p0_5_channel_marks.py。本
文件覆盖的未来时间拒绝口径不变。方案 A 落地前的冲突
分析见 evidence/v3-audit-correction-20261123/design/
DESIGN-V3-CORRECTION.md。
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool


def _pool_with_candidate(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol="603893", name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    return pool, cid


def _direct_insert_event(pool, cid, created_at):
    pool.conn.execute(
        "INSERT INTO discovery_events (candidate_id, source_id,"
        " market, symbol, discovered_at, trigger_condition,"
        " price_at_discovery, price_data_time, evidence_json,"
        " created_at) VALUES (?, 'inst_lhb_stored', 'SH',"
        " '603893', '2026-11-16T09:00:00+08:00', 'inst_net>0',"
        " 10.0, '2026-11-16', '{}', ?)", (cid, created_at))


def _event_count(pool, cid):
    return pool.conn.execute(
        "SELECT COUNT(*) c FROM discovery_events"
        " WHERE candidate_id=?", (cid,)).fetchone()["c"]


def _iso(dt):
    return dt.astimezone().isoformat(timespec="seconds")


# --- 未来时间拒绝 ---------------------------------------------------------

def test_direct_insert_future_created_at_blocked(tmp_path):
    """直写 INSERT created_at 为明天 -> 拒绝，行不落库。"""
    pool, cid = _pool_with_candidate(tmp_path)
    future = _iso(datetime.now().astimezone() + timedelta(days=1))
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_event(pool, cid, future)
    pool.conn.rollback()
    assert _event_count(pool, cid) == 1


def test_direct_insert_far_future_created_at_blocked(tmp_path):
    """直写 INSERT created_at 为 2030 年 -> 拒绝。"""
    pool, cid = _pool_with_candidate(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_event(pool, cid, "2030-01-01T00:00:00+08:00")
    pool.conn.rollback()
    assert _event_count(pool, cid) == 1


# --- 正常服务器时间正对照 ---------------------------------------------------

def test_direct_insert_current_server_time_passes(tmp_path):
    """直写 INSERT created_at 为当前服务器时间 -> 通过。"""
    pool, cid = _pool_with_candidate(tmp_path)
    _direct_insert_event(pool, cid, _iso(datetime.now().astimezone()))
    pool.conn.commit()
    assert _event_count(pool, cid) == 2


def test_direct_insert_within_tolerance_passes(tmp_path):
    """created_at 晚于服务器时间但在 120 秒容差内 -> 通过
    （与 registry approved_at 窗口同口径）。"""
    pool, cid = _pool_with_candidate(tmp_path)
    near = _iso(datetime.now().astimezone() + timedelta(seconds=60))
    _direct_insert_event(pool, cid, near)
    pool.conn.commit()
    assert _event_count(pool, cid) == 2


# --- 既有 UPDATE 保护保留复核 -------------------------------------------------

def test_update_guard_still_active(tmp_path):
    """INSERT 侧新闸不影响既有 UPDATE 不可变保护：落字后
    改写 created_at（回填或前移）仍被拒。"""
    pool, cid = _pool_with_candidate(tmp_path)
    before = pool.events_for(cid)[0]["created_at"]
    for forged in ("2026-09-01T09:00:00+08:00",
                   "2030-01-01T00:00:00+08:00"):
        with pytest.raises(sqlite3.IntegrityError):
            pool.conn.execute(
                "UPDATE discovery_events SET created_at=?"
                " WHERE candidate_id=?", (forged, cid))
        pool.conn.rollback()
    assert pool.events_for(cid)[0]["created_at"] == before
