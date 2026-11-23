# -*- coding: utf-8 -*-
"""RT-1 对抗测试（2026-11-23 自红队补正）：promotion.decide 批准强制。

自红队口径（冻结包 07_SELF_REDTEAM RT-1）：decide 是候选进入
approved 终态的唯一合法入口，此前 approver 与 basis 均可为空
即落 approvals 行——与 rule 侧 P0-3 的批准强制口径不一致。
修复后：decision='approved' 必须显式给出非空 approver 与
非空 basis（空白/纯空格视为无），缺一在写库前抛 ValueError，
approvals 不落行、状态不变（整事务回滚）。

对抗形态：
- 空 approver / 空白 approver -> 拒绝且回滚；
- 缺 basis（缺省空）/ 空白 basis -> 拒绝且回滚；
- 正对照：信息齐备 -> approved 终态 + approvals 行字段齐全；
- 范围对照：rejected 不要求 basis（与 P0-3 非 approved 流转
  的兼容口径一致），仍可正常落字。
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


def _register_test_rule(pool):
    validation.register_rule_implementation("test_rule", _TestRule())
    validation.register_rule(
        pool, "test_rule", _TestRule.version, status="approved",
        approved_by="unit-test", actor="unit-test",
        reason="rt1 decide approval test",
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


def _judge_pass(pool, cid):
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
    _judge_pass(pool, cid)
    transition(pool, cid, "pending_approval", actor="tester",
               reason="walk")
    assert pool.get_by_id(cid)["promotion_status"] == "pending_approval"
    return pool, cid


def _approvals_count(pool):
    return pool.conn.execute(
        "SELECT COUNT(*) c FROM approvals").fetchone()["c"]


def test_decide_approved_requires_approver(tmp_path):
    pool, cid = _pending_candidate(tmp_path)
    with pytest.raises(ValueError):
        decide(pool, cid, approver="", decision="approved",
               basis="依据充分")
    with pytest.raises(ValueError):
        decide(pool, cid, approver="   ", decision="approved",
               basis="依据充分")
    # 整事务回滚：状态不变、approvals 无行
    assert pool.get_by_id(cid)["promotion_status"] == "pending_approval"
    assert _approvals_count(pool) == 0


def test_decide_approved_requires_basis(tmp_path):
    pool, cid = _pending_candidate(tmp_path)
    with pytest.raises(ValueError):
        decide(pool, cid, approver="alice", decision="approved")
    with pytest.raises(ValueError):
        decide(pool, cid, approver="alice", decision="approved",
               basis="   ")
    assert pool.get_by_id(cid)["promotion_status"] == "pending_approval"
    assert _approvals_count(pool) == 0


def test_decide_approved_with_full_info_positive_control(tmp_path):
    pool, cid = _pending_candidate(tmp_path)
    aid = decide(pool, cid, approver="alice", decision="approved",
                 basis="研究与验证完整")
    assert pool.get_by_id(cid)["promotion_status"] == "approved"
    row = pool.conn.execute(
        "SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    assert row["approver"] == "alice"
    assert row["basis"] == "研究与验证完整"
    assert row["decision"] == "approved" and row["decided_at"]


def test_decide_rejected_without_basis_still_allowed(tmp_path):
    # 范围对照：rejected 非批准终态，不强制 basis（兼容口径）。
    pool, cid = _pending_candidate(tmp_path)
    aid = decide(pool, cid, approver="alice", decision="rejected")
    assert pool.get_by_id(cid)["promotion_status"] == "rejected"
    row = pool.conn.execute(
        "SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    assert row["decision"] == "rejected"
