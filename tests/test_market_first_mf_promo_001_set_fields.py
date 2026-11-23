# -*- coding: utf-8 -*-
"""MF-PROMO-001 对抗测试（2026-11-23 红队集中修复第 8 项）。

问题：pool.set_fields 是通用字段写原语，白名单含
promotion_status——任何调用方都能
set_fields(cid, actor='红队探针', promotion_status='approved')
把候选直改终态 approved（自红队 C3 探针实证），绕开
promotion.decide 的正式审批（approver/basis/approvals 记录）。

修复：set_fields 对 promotion_status='approved' 无条件拒绝
（ValueError，字段与 history 均不落字）；promotion 状态机
流转改走专用写通道 pool._transition_promotion_status；
approved 只能经 promotion.decide + 有效 approvals 记录进入，
handoff 的独立核验行为不变。
"""
from datetime import date, timedelta

import pytest

from market_first import validation
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.promotion import decide, transition


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


def _pending_candidate(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol="603893", name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    pool.clock = lambda: ev.discovered_at  # P0-5 可信时钟夹具迁移（2026-11-23）：ingest 落字时刻即模拟入库时刻，替代旧直写 UPDATE created_at 手法；断言未改动。
    cid, _ = pool.ingest(ev)
    for state in ("pending_research", "researching", "validating"):
        transition(pool, cid, state, actor="tester", reason="walk")
    validation.register_rule_implementation("test_rule", _TestRule())
    validation.register_rule(
        pool, "test_rule", _TestRule.version, status="approved",
        approved_by="unit-test", actor="unit-test",
        reason="mf-promo-001 test",
        approval_basis="test-only approval (tmp db)")
    ev_row = pool.events_for(cid)[0]
    bars = _gen_bars()
    sid = validation.create_sample(
        pool, cid, ev_row["source_id"], kind="forward",
        discovery_event_id=ev_row["id"], bars=bars,
        fetched_at="2026-11-17T16:00:00+08:00")
    validation.evaluate_sample(pool, sid, bars)
    out = validation.judge_validation(
        pool, cid, rule_id="test_rule",
        rule_version=_TestRule.version)
    assert out["decision"] == "passed"
    transition(pool, cid, "pending_approval", actor="tester",
               reason="walk")
    assert pool.get_by_id(cid)["promotion_status"] == "pending_approval"
    return pool, cid


def test_set_fields_direct_approved_rejected(tmp_path):
    """C3 探针复刻：通用写原语直改 approved 必须被拒且不落字。"""
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol="603894", name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    hist_before = pool.conn.execute(
        "SELECT COUNT(*) c FROM candidate_history"
        " WHERE candidate_id=? AND field='promotion_status'",
        (cid,)).fetchone()["c"]
    with pytest.raises(ValueError):
        pool.set_fields(cid, actor="红队探针",
                        promotion_status="approved")
    assert pool.get_by_id(cid)["promotion_status"] != "approved"
    hist_after = pool.conn.execute(
        "SELECT COUNT(*) c FROM candidate_history"
        " WHERE candidate_id=? AND field='promotion_status'",
        (cid,)).fetchone()["c"]
    assert hist_after == hist_before


def test_set_fields_other_values_unaffected(tmp_path):
    """范围对照：非 approved 的普通字段更新不受影响。"""
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol="603895", name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    pool.set_fields(cid, actor="tester", research_status="done",
                    name="更名标的")
    cand = pool.get_by_id(cid)
    assert cand["research_status"] == "done"
    assert cand["name"] == "更名标的"


def test_decide_path_still_reaches_approved(tmp_path):
    """正常路径回归：状态机流转 + decide 审批仍能进入 approved，
    且 candidate_history 留有完整流转痕。"""
    pool, cid = _pending_candidate(tmp_path)
    aid = decide(pool, cid, approver="alice", decision="approved",
                 basis="研究与验证完整")
    assert pool.get_by_id(cid)["promotion_status"] == "approved"
    rows = pool.conn.execute(
        "SELECT new_value FROM candidate_history WHERE candidate_id=?"
        " AND field='promotion_status' ORDER BY id", (cid,)).fetchall()
    assert rows[-1]["new_value"] == "approved"
    row = pool.conn.execute(
        "SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    assert row["decision"] == "approved"
