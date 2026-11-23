# -*- coding: utf-8 -*-
"""P0-5 created_at 可信性测试（2026-11-23 红队集中修复第 6 项）。

问题（自红队 B1/B2 探针）：discovery_events.created_at 是
forward 时间锚的唯一可信依据，但表无写入保护——直写 UPDATE
可回填/前移 created_at，应用层锚校验被绕过。

修复：
- 生成：pool.ingest 以服务器时间落字；测试经 CandidatePool
  可信时钟夹具（clock 参数 / pool.clock 赋值）在应用层路径
  注入模拟入库时刻；
- 受控写入：DB 触发器 trg_rt_discovery_events_created_at_
  no_update 禁止 created_at 落字后被 UPDATE 篡改；
- 旧测试 15 处直写 UPDATE created_at 已全量迁移到时钟夹具
  （逐项登记见交付说明，断言零改动）。

残余（如实登记）：直写 INSERT 一条带任意 created_at 的新
事件行在 DB 层与合法 ingest 不可区分，本批不拦，待审计裁定。
"""
import sqlite3
from datetime import datetime

import pytest

from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool


def _event(discovered_at="2026-11-16T09:00:00+08:00"):
    return DiscoveryEvent(
        market="SH", symbol="603893", name="测试标的",
        source_id="inst_lhb_stored", discovered_at=discovered_at,
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})


def test_created_at_update_backfill_blocked(tmp_path):
    """直绕对抗：ingest 后回填 created_at 到过去 -> 触发器
    RAISE(ABORT)，原值不变。"""
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    cid, _ = pool.ingest(_event())
    before = pool.events_for(cid)[0]["created_at"]
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE discovery_events SET created_at=?"
            " WHERE candidate_id=?",
            ("2026-09-01T09:00:00+08:00", cid))
    pool.conn.rollback()
    assert pool.events_for(cid)[0]["created_at"] == before


def test_created_at_update_forward_shift_blocked(tmp_path):
    """直绕对抗：前移 created_at 到未来同样被拒。"""
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    cid, _ = pool.ingest(_event())
    before = pool.events_for(cid)[0]["created_at"]
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE discovery_events SET created_at=?"
            " WHERE candidate_id=?",
            ("2030-01-01T00:00:00+08:00", cid))
    pool.conn.rollback()
    assert pool.events_for(cid)[0]["created_at"] == before


def test_clock_fixture_controls_ingest_time_via_app_path(tmp_path):
    """受控测试通道：时钟夹具经应用层 ingest 路径产生模拟
    入库时刻（延迟入库场景），无需任何直写 SQL。"""
    pool = CandidatePool(
        db_path=str(tmp_path / "mf.db"),
        clock=lambda: "2026-11-20T18:00:00+08:00")
    cid, _ = pool.ingest(_event("2026-09-15T09:00:00+08:00"))
    row = pool.events_for(cid)[0]
    assert row["created_at"] == "2026-11-20T18:00:00+08:00"
    assert row["discovered_at"] == "2026-09-15T09:00:00+08:00"


def test_clock_assignable_between_ingests(tmp_path):
    """夹具可在两次 ingest 间切换（多事件不同入库时刻）。"""
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    pool.clock = lambda: "2026-11-16T09:05:00+08:00"
    cid1, _ = pool.ingest(_event())
    ev2 = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id="inst_lhb_live",
        discovered_at="2026-11-17T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 50.0})
    pool.clock = lambda: "2026-11-17T09:05:00+08:00"
    cid2, _ = pool.ingest(ev2)
    assert pool.events_for(cid1)[0]["created_at"] == \
        "2026-11-16T09:05:00+08:00"
    assert pool.events_for(cid2)[0]["created_at"] == \
        "2026-11-17T09:05:00+08:00"


def test_production_default_uses_server_time(tmp_path):
    """正常路径回归：不注入时钟时 created_at 为真实服务器
    时间（与当前时刻差 < 60 秒）。"""
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    before = datetime.now().astimezone()
    cid, _ = pool.ingest(_event())
    after = datetime.now().astimezone()
    stamped = datetime.fromisoformat(
        pool.events_for(cid)[0]["created_at"])
    assert before.timestamp() - 60 <= stamped.timestamp()
    assert stamped.timestamp() <= after.timestamp() + 60
