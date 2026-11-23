# -*- coding: utf-8 -*-
"""RT-1 底层闸对抗测试（2026-11-23 补强批）：晋升 approved 的 DB 层拦截。

背景（用户最终验收原则）：关键约束必须在正式入口和底层边界
同时成立。RT-1 已在 promotion.decide 入口强制 approver/basis
非空、handoff 下游独立核验 approvals 记录，但 candidates.
promotion_status 此前无 DB 层防护——直写 SQL
``UPDATE candidates SET promotion_status='approved'`` 可绕过
入口直接伪造晋升终态。

修复（db.ensure_redteam_schema，设计补记 DESIGN-BOTTOM-LAYER.md）：
- trg_rt_candidates_promotion_approved_insert /
  trg_rt_candidates_promotion_approved_update：进入 approved
  （INSERT 或 status 流转）必须有 decision='approved' 且
  approver/basis 均非空的 approvals 行对应，否则
  RAISE(ABORT)；
- decide 恒先写 approvals 后流转、同事务，天然通过（本文件
  正常路径用例实证）。

诚实残余：先伪造一条字段齐全的 approvals 行再改状态，与
应用写入不可区分，属 SQLite 单文件固有界限，登记待审计
裁定；handoff 下游核验仍是第二道防线。
"""
import sqlite3
from datetime import date, timedelta

import pytest

from market_first import bars as bars_mod
from market_first import validation
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.promotion import decide, transition


class _TestRule:
    version = "test-rule-0"

    def evaluate(self, scope, metrics):
        return "passed", "test rule: mature forward samples present"


def _register_test_rule(pool):
    validation.register_rule_implementation("test_rule", _TestRule())
    validation.register_rule(
        pool, "test_rule", _TestRule.version, status="approved",
        approved_by="unit-test", actor="unit-test",
        reason="rt1 db guard test",
        approval_basis="test-only approval (tmp db)")


def _gen_bars():
    out = []
    d = date(2026, 11, 13)
    i = 0
    while d <= date(2026, 12, 20):
        if d.weekday() < 5:
            c = 10.0 + 0.1 * i
            out.append((d.isoformat(), c, c, c, c, 1000.0))
            i += 1
        d += timedelta(days=1)
    return out


def _pool_with_candidate(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol="603893", name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    pool.clock = lambda: ev.discovered_at
    cid, _ = pool.ingest(ev)
    return pool, cid


def _pending_candidate(tmp_path):
    pool, cid = _pool_with_candidate(tmp_path)
    for state in ("pending_research", "researching", "validating"):
        transition(pool, cid, state, actor="tester", reason="walk")
    _register_test_rule(pool)
    ev = pool.events_for(cid)[0]
    bars = _gen_bars()
    sid = validation.create_sample(
        pool, cid, ev["source_id"], kind="forward",
        discovery_event_id=ev["id"], bars=bars,
        fetched_at="2026-11-17T16:00:00+08:00")
    bars_mod.store_bars(pool.get_by_id(cid)["symbol"], bars,
                        "test-fixture", pool.db_path)
    validation.evaluate_sample(pool, sid, bars)
    out = validation.judge_validation(
        pool, cid, rule_id="test_rule",
        rule_version=_TestRule.version)
    assert out["decision"] == "passed"
    transition(pool, cid, "pending_approval", actor="tester",
               reason="walk")
    assert pool.get_by_id(cid)["promotion_status"] == "pending_approval"
    return pool, cid


def _status(pool, cid):
    return pool.get_by_id(cid)["promotion_status"]


def _insert_approvals(pool, cid, decision="approved",
                      approver="alice", basis="依据充分"):
    pool.conn.execute(
        "INSERT INTO approvals (candidate_id, decision, approver,"
        " decided_at, basis, versions_json) VALUES (?, ?, ?,"
        " '2026-11-23T10:00:00+08:00', ?, '{}')",
        (cid, decision, approver, basis))
    pool.conn.commit()


# --- 直写 SQL 污染对抗 -------------------------------------------------

def test_direct_update_to_approved_without_approvals_blocked(tmp_path):
    """无任何 approvals 记录时直写 UPDATE 置 approved ->
    触发器拒绝，状态不变。"""
    pool, cid = _pool_with_candidate(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE candidates SET promotion_status='approved'"
            " WHERE id=?", (cid,))
    pool.conn.rollback()
    assert _status(pool, cid) == "discovered"


def test_direct_insert_candidate_approved_blocked(tmp_path):
    """直写 INSERT 一条出生即 approved 的候选 -> 拒绝，行不落库。"""
    pool, _ = _pool_with_candidate(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "INSERT INTO candidates (id, market, symbol,"
            " promotion_status) VALUES (999, 'SZ', '000999',"
            " 'approved')")
    pool.conn.rollback()
    assert pool.get_by_id(999) is None


def test_update_blocked_when_approvals_fields_empty(tmp_path):
    """approvals 行存在但 approver/basis 为空（或 decision 非
    approved）-> 仍拒绝：底层闸核的是记录有效性，不是行存在。"""
    pool, cid = _pool_with_candidate(tmp_path)
    _insert_approvals(pool, cid, approver="", basis="")
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE candidates SET promotion_status='approved'"
            " WHERE id=?", (cid,))
    pool.conn.rollback()
    assert _status(pool, cid) == "discovered"

    pool.conn.execute("DELETE FROM approvals WHERE candidate_id=?",
                      (cid,))
    _insert_approvals(pool, cid, decision="rejected")
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE candidates SET promotion_status='approved'"
            " WHERE id=?", (cid,))
    pool.conn.rollback()
    assert _status(pool, cid) == "discovered"


# --- 底层正对照 ---------------------------------------------------------

def test_update_allowed_with_valid_approvals_record(tmp_path):
    """DB 层正对照：有效 approvals 行存在时 UPDATE 通过——
    闸的放行依据是记录本身，不是调用方身份。"""
    pool, cid = _pool_with_candidate(tmp_path)
    _insert_approvals(pool, cid)
    pool.conn.execute(
        "UPDATE candidates SET promotion_status='approved'"
        " WHERE id=?", (cid,))
    pool.conn.commit()
    assert _status(pool, cid) == "approved"
    # 已 approved 行的其他字段更新不被本闸拦截
    pool.conn.execute(
        "UPDATE candidates SET name='改名' WHERE id=?", (cid,))
    pool.conn.commit()
    assert pool.get_by_id(cid)["name"] == "改名"


# --- 正常路径（正式入口天然通过） ----------------------------------------

def test_decide_normal_path_passes_db_guard(tmp_path):
    """decide 全链（先 approvals 后流转、同事务）在底层闸
    安装后仍天然通过，终态与 approvals 行齐全。"""
    pool, cid = _pending_candidate(tmp_path)
    aid = decide(pool, cid, approver="alice", decision="approved",
                 basis="研究与验证完整")
    assert _status(pool, cid) == "approved"
    row = pool.conn.execute(
        "SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    assert row["approver"] == "alice"
    assert row["basis"] == "研究与验证完整"
    assert row["decision"] == "approved"
