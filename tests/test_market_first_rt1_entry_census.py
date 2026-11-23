# -*- coding: utf-8 -*-
"""RT-1 强化：晋升批准入口全枚举普查（2026-11-23 红队集中修复
第 1 项强化，用户《红队缺陷集中修复指令 V1.0》）。

枚举全仓一切能把候选带入 approved 终态的候选路径，逐一验证
只有 promotion.decide（带非空 approver + 非空 basis）在合法
状态下可以，其余路径全部拒绝且留痕/不落字：

1. promotion.decide —— 唯一合法入口（正对照，RT-1 本体已测
   拒绝面，本文件测其为唯一可达路径的完整性）；
2. promotion.transition 直达 approved/rejected —— 状态机在
   DECISION_STATES 处拒绝（IllegalTransitionError + 留痕）；
3. promotion.transition 直达 validation_passed ——
   VALIDATION_GATE_STATES 拒绝（验证态只能由 judge 产生）；
4. promotion._apply_validation_passed 伪造 decision_id 直调
   —— 无对应 validation_decisions passed 记录，核验拒绝；
5. pool.set_fields 直改 promotion_status='approved' ——
   MF-PROMO-001 无条件拒绝（第 8 项，本文件交叉引用复证）；
6. ResearchEngine 等其他模块只写 research_status，不存在
   promotion_status 写入口（由全仓 grep 证据支撑，见交付
   说明的入口枚举清单）。

批准人/依据/时间/完整历史的保留：decide 正路落 approvals
行（approver/basis/decided_at 服务器时间）与
candidate_history 流转痕，本文件正对照逐项断言。
"""
from datetime import date, timedelta

import pytest

from market_first import promotion, validation
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.promotion import (
    IllegalTransitionError, decide, transition)


class _TestRule:
    version = "test-rule-0"

    def evaluate(self, scope, metrics):
        return "passed", "test rule: mature forward samples present"


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


def _walk_to_validating(tmp_path, symbol="603893"):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol=symbol, name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    pool.conn.execute(
        "UPDATE discovery_events SET created_at=?"
        " WHERE candidate_id=?", (ev.discovered_at, cid))
    pool.conn.commit()
    for state in ("pending_research", "researching", "validating"):
        transition(pool, cid, state, actor="tester", reason="walk")
    return pool, cid


def _judge_pass(pool, cid):
    validation.register_rule_implementation("test_rule", _TestRule())
    validation.register_rule(
        pool, "test_rule", _TestRule.version, status="approved",
        approved_by="unit-test", actor="unit-test",
        reason="rt1 census test",
        approval_basis="test-only approval (tmp db)")
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


def test_transition_direct_approved_rejected_with_trace(tmp_path):
    """入口 2：transition 直达 approved 被拒 + candidate_history
    留痕 + 状态不变。"""
    pool, cid = _walk_to_validating(tmp_path)
    with pytest.raises(IllegalTransitionError):
        transition(pool, cid, "approved", actor="attacker")
    assert pool.get_by_id(cid)["promotion_status"] == "validating"
    rows = pool.conn.execute(
        "SELECT reason FROM candidate_history WHERE candidate_id=?"
        " AND field='promotion_transition_attempt'"
        " AND new_value='approved'", (cid,)).fetchall()
    assert rows and all(
        "ILLEGAL TRANSITION REJECTED" in (r["reason"] or "")
        and "decide()" in (r["reason"] or "") for r in rows)


def test_transition_direct_validation_passed_rejected(tmp_path):
    """入口 3：transition 直达 validation_passed 被拒（验证态
    只能由 judge_validation 产生）。"""
    pool, cid = _walk_to_validating(tmp_path)
    with pytest.raises(IllegalTransitionError):
        transition(pool, cid, "validation_passed", actor="attacker")
    assert pool.get_by_id(cid)["promotion_status"] == "validating"


def test_apply_validation_passed_forged_decision_id_rejected(tmp_path):
    """入口 4：伪造 decision_id 直调受控入口，无 passed 记录
    对应 -> 核验拒绝、留痕、状态不变。"""
    pool, cid = _walk_to_validating(tmp_path)
    with pytest.raises(IllegalTransitionError):
        promotion._apply_validation_passed(
            pool, cid, actor="attacker", decision_id=999999)
    assert pool.get_by_id(cid)["promotion_status"] == "validating"
    with pytest.raises(IllegalTransitionError):
        promotion._apply_validation_passed(
            pool, cid, actor="attacker", decision_id=None)
    assert pool.get_by_id(cid)["promotion_status"] == "validating"


def test_set_fields_crosscheck_still_rejected(tmp_path):
    """入口 5（第 8 项交叉复证）：set_fields 直改 approved 拒绝。"""
    pool, cid = _walk_to_validating(tmp_path)
    with pytest.raises(ValueError):
        pool.set_fields(cid, actor="attacker",
                        promotion_status="approved")
    assert pool.get_by_id(cid)["promotion_status"] == "validating"


def test_decide_is_the_only_path_and_keeps_full_record(tmp_path):
    """入口 1 正对照 + 记录完整性：走完 judge 后只有 decide 能
    进入 approved；approvals 行 approver/basis/decided_at
    齐全，candidate_history 保留完整流转链。"""
    pool, cid = _walk_to_validating(tmp_path)
    _judge_pass(pool, cid)
    transition(pool, cid, "pending_approval", actor="tester",
               reason="walk")
    aid = decide(pool, cid, approver="alice", decision="approved",
                 basis="四线达标（mf-judge-1.0 方案A）")
    assert pool.get_by_id(cid)["promotion_status"] == "approved"
    row = pool.conn.execute(
        "SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    assert row["approver"] == "alice"
    assert row["basis"] == "四线达标（mf-judge-1.0 方案A）"
    assert row["decided_at"]  # 服务器时间落字
    chain = [r["new_value"] for r in pool.conn.execute(
        "SELECT new_value FROM candidate_history"
        " WHERE candidate_id=? AND field='promotion_status'"
        " AND new_value IS NOT NULL ORDER BY id", (cid,)).fetchall()]
    assert chain == ["pending_research", "researching", "validating",
                     "validation_passed", "pending_approval",
                     "approved"]
