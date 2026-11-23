# -*- coding: utf-8 -*-
"""Market-First：晋升状态机与交易链模拟交接测试。

核心不变量：验证通过≠自动批准；未批准（或已拒绝）不得进入交接；
非法转移抛错且在 candidate_history 留痕；交接只写新库 handoffs 且
status 恒为 simulated。
"""
from datetime import date, timedelta

import pytest

from market_first import validation
from market_first.handoff import HandoffNotApprovedError, simulate_handoff
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.promotion import (IllegalTransitionError, decide,
                                    transition)


class _TestRule:
    """测试专用规则实现（fix2-3 起经注册表路径使用：在测试库内
    登记 approved 行 + 代码侧登记实现；不代表用户已确认门槛，
    绝不登记到规范库）。"""

    version = "test-rule-0"

    def evaluate(self, scope, metrics):
        return "passed", "test rule: mature forward samples present"


def _register_test_rule(pool):
    validation.register_rule_implementation("test_rule", _TestRule())
    validation.register_rule(
        pool, "test_rule", _TestRule.version, status="approved",
        approved_by="unit-test",
        # P0-3 集成（2026-11-23）：补齐批准信息；断言未改动。
        actor="unit-test", reason="promotion handoff test approval",
        approval_basis="test-only approval (tmp db)")


def _gen_bars():
    """2026-11-13 起工作日 bars，收盘单调上行。

    事件 discovered 于 2026-11-16 -> forward entry 推导为
    2026-11-17，其后 >=20 根 -> 样本 mature。
    """
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


def _judge_pass(pool, cid):
    """经验证引擎判定合法到达 validation_passed（P0-3/fix2-3
    注册表路径）。样本时间线为模拟时钟：事件 2026-11-16 发现、
    forward entry 推导为 2026-11-17，故取数时刻显式给到该日
    收盘确认后（16:00），与 fix2-1 完整性规则一致。"""
    _register_test_rule(pool)
    ev = pool.events_for(cid)[0]
    bars = _gen_bars()
    sid = validation.create_sample(
        pool, cid, ev["source_id"], kind="forward",
        discovery_event_id=ev["id"], bars=bars,
        fetched_at="2026-11-17T16:00:00+08:00")
    validation.evaluate_sample(pool, sid, bars)
    out = validation.judge_validation(
        pool, cid, rule_id="test_rule",
        rule_version=_TestRule.version)
    assert out["decision"] == "passed"


def _candidate(tmp_path, symbol="603893"):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol=symbol, name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    pool.clock = lambda: ev.discovered_at  # P0-5 可信时钟夹具迁移（2026-11-23）：ingest 落字时刻即模拟入库时刻，替代旧直写 UPDATE created_at 手法；断言未改动。
    cid, _ = pool.ingest(ev)
    return pool, cid


def _walk_to_pending_approval(pool, cid):
    # validation_passed 自 P0-3 起只能由 judge_validation 产生，
    # 不再经 transition() 直达；其余流转照旧。
    for state in ("pending_research", "researching", "validating"):
        transition(pool, cid, state, actor="tester", reason="walk")
    _judge_pass(pool, cid)
    transition(pool, cid, "pending_approval", actor="tester",
               reason="walk")


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
    for state in ("pending_research", "researching", "validating"):
        transition(pool, cid, state, actor="tester")
    _judge_pass(pool, cid)  # 到 validation_passed 为止（经 judge）
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
