# -*- coding: utf-8 -*-
"""Market-First：晋升状态机与交易链模拟交接测试。

核心不变量：验证通过≠自动批准；未批准（或已拒绝）不得进入交接；
非法转移抛错且在 candidate_history 留痕；交接只写新库 handoffs 且
status 恒为 simulated。
"""
import pytest

from market_first.handoff import HandoffNotApprovedError, simulate_handoff
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.promotion import (IllegalTransitionError, decide,
                                    transition)

WALK = ["pending_research", "researching", "validating",
        "validation_passed", "pending_approval"]


def _candidate(tmp_path, symbol="603893"):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol=symbol, name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    return pool, cid


def _walk_to_pending_approval(pool, cid):
    for state in WALK:
        transition(pool, cid, state, actor="tester", reason="walk")


def test_legal_walk_and_approval_then_simulated_handoff(tmp_path):
    pool, cid = _candidate(tmp_path)
    _walk_to_pending_approval(pool, cid)
    assert pool.get_by_id(cid)["promotion_status"] == "pending_approval"
    aid = decide(pool, cid, approver="alice", decision="approved",
                 basis="研究与验证完整")
    assert pool.get_by_id(cid)["promotion_status"] == "approved"
    row = pool.conn.execute(
        "SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    assert row["approver"] == "alice" and row["decision"] == "approved"
    assert row["basis"] == "研究与验证完整" and row["decided_at"]
    payload = simulate_handoff(pool, cid)
    assert payload["status"] == "simulated"
    assert payload["handoff_id"] >= 1
    assert payload["symbol"] == "603893"
    assert payload["intent_placeholder"] is True
    trace = payload["source_trace"]
    assert trace["candidate_id"] == cid
    assert trace["approval_id"] == aid
    assert len(trace["discovery_event_ids"]) == 1
    hrow = pool.conn.execute(
        "SELECT * FROM handoffs WHERE id=?",
        (payload["handoff_id"],)).fetchone()
    assert hrow["status"] == "simulated"


def test_validation_passed_is_not_auto_approval(tmp_path):
    pool, cid = _candidate(tmp_path)
    for state in WALK[:4]:  # 到 validation_passed 为止
        transition(pool, cid, state, actor="tester")
    assert pool.get_by_id(cid)["promotion_status"] == "validation_passed"
    with pytest.raises(HandoffNotApprovedError):
        simulate_handoff(pool, cid)


def test_unapproved_handoff_rejected(tmp_path):
    pool, cid = _candidate(tmp_path)
    with pytest.raises(HandoffNotApprovedError):
        simulate_handoff(pool, cid)
    _walk_to_pending_approval(pool, cid)
    with pytest.raises(HandoffNotApprovedError):
        simulate_handoff(pool, cid)  # 待审批仍未批准，一样被拒


def test_illegal_transition_raises_and_leaves_trace(tmp_path):
    pool, cid = _candidate(tmp_path)
    with pytest.raises(IllegalTransitionError):
        transition(pool, cid, "approved", actor="tester")
    hist = [h for h in pool.history_for(cid)
            if h["field"] == "promotion_transition_attempt"]
    assert len(hist) == 1
    assert hist[0]["old_value"] == "discovered"
    assert hist[0]["new_value"] == "approved"
    assert "ILLEGAL" in (hist[0]["reason"] or "")
    assert pool.get_by_id(cid)["promotion_status"] == "discovered"


def test_transition_cannot_bypass_decide(tmp_path):
    pool, cid = _candidate(tmp_path)
    _walk_to_pending_approval(pool, cid)
    with pytest.raises(IllegalTransitionError):
        transition(pool, cid, "approved", actor="tester")
    # decide 仍可正常完成审批（唯一入口）
    decide(pool, cid, approver="bob", decision="approved", basis="ok")
    assert pool.get_by_id(cid)["promotion_status"] == "approved"


def test_decide_requires_pending_approval(tmp_path):
    pool, cid = _candidate(tmp_path)
    with pytest.raises(IllegalTransitionError):
        decide(pool, cid, approver="alice", decision="approved", basis="x")
    assert pool.conn.execute(
        "SELECT COUNT(*) c FROM approvals").fetchone()["c"] == 0


def test_rejected_candidate_cannot_handoff(tmp_path):
    pool, cid = _candidate(tmp_path)
    _walk_to_pending_approval(pool, cid)
    decide(pool, cid, approver="alice", decision="rejected",
           basis="证据不足")
    assert pool.get_by_id(cid)["promotion_status"] == "rejected"
    with pytest.raises(HandoffNotApprovedError):
        simulate_handoff(pool, cid)


def test_terminal_states_have_no_outgoing_transition(tmp_path):
    pool, cid = _candidate(tmp_path)
    _walk_to_pending_approval(pool, cid)
    decide(pool, cid, approver="alice", decision="approved", basis="ok")
    with pytest.raises(IllegalTransitionError):
        transition(pool, cid, "pending_research", actor="tester")
