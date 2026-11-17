# -*- coding: utf-8 -*-
"""Market-First fix3 P0-B（MF-REG-002）：Registry 精确绑定。

judge 按 (rule_id, version) 同时核对 rule_registry 落库条目
与代码侧实现注册表：实现缺失 -> NO_IMPL、impl_key/impl_version
错配 -> IMPL_MISMATCH，均 not_evaluable 并留痕；
validation_decisions 同时保存 rule_id 与 rule_version；
reproduce_decision 按记录版本复现历史判定（注册表变更后仍
可复现、原记录不被改写）。approved 规则只出现在 tmp 测试库。
"""
from datetime import date, timedelta

import pytest

from market_first import validation
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.promotion import transition

RID = "bind_rule"


class _Rule:
    def __init__(self, version):
        self.version = version

    def evaluate(self, scope, metrics):
        return "passed", "binding test rule %s" % self.version


def _bars(start=date(2026, 11, 13), end=date(2026, 12, 20)):
    out = []
    d = start
    i = 0
    while d <= end:
        if d.weekday() < 5:
            c = 10.0 + 0.1 * i
            out.append((d.isoformat(), c, c, c, c, 1000.0))
            i += 1
        d += timedelta(days=1)
    return out


def _candidate_validating(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id="inst_lhb_live",
        discovered_at="2026-11-13T16:30:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    pool.conn.execute(
        "UPDATE discovery_events SET created_at=?"
        " WHERE candidate_id=?", (ev.discovered_at, cid))
    pool.conn.commit()
    for state in ("pending_research", "researching", "validating"):
        transition(pool, cid, state, actor="tester")
    return pool, cid


def _mature_forward(pool, cid):
    ev = pool.events_for(cid)[0]
    bars = _bars()
    sid = validation.create_sample(
        pool, cid, ev["source_id"], kind="forward",
        discovery_event_id=ev["id"], bars=bars)
    validation.evaluate_sample(pool, sid, bars)
    return sid


@pytest.fixture(autouse=True)
def _clean_impls():
    yield
    for key in [k for k in validation.RULE_IMPLEMENTATIONS
                if k[0] == RID]:
        validation.RULE_IMPLEMENTATIONS.pop(key, None)


def _register(pool, version, status="approved", **kw):
    return validation.register_rule(
        pool, RID, version, status=status,
        approved_by="unit-test" if status == "approved" else None,
        **kw)


# ---------------------------------------------------------------------------
# 注册表 impl 绑定落字
# ---------------------------------------------------------------------------

def test_registry_row_stores_impl_binding(tmp_path):
    pool, _cid = _candidate_validating(tmp_path)
    _register(pool, "v1")
    row = validation.get_registered_rule(pool, RID, "v1")
    assert row["impl_key"] == RID
    assert row["impl_version"] == "v1"
    _register(pool, "v2", status="draft",
              impl_key="custom_impl", impl_version="v2.1")
    row2 = validation.get_registered_rule(pool, RID, "v2")
    assert row2["impl_key"] == "custom_impl"
    assert row2["impl_version"] == "v2.1"


# ---------------------------------------------------------------------------
# NO_IMPL：登记 v2、实现只有 v1
# ---------------------------------------------------------------------------

def test_no_impl_for_registered_version(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    validation.register_rule_implementation(RID, _Rule("v1"))
    _register(pool, "v2")  # 注册表有 v2 approved，实现只有 v1
    out = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v2")
    assert out["decision"] == "not_evaluable"
    assert "NO_IMPL" in out["basis"]
    assert out["rule_id"] == RID
    assert out["rule_version"] == validation.NO_RULE
    assert pool.get_by_id(cid)["promotion_status"] == "validating"


# ---------------------------------------------------------------------------
# IMPL_MISMATCH：实现 v2 冒充 v1 / 注册表绑定与实现不符
# ---------------------------------------------------------------------------

def test_impl_mismatch_impl_impersonates_version(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    # 以 v1 之名登记自报 v2 的实现（冒充）
    validation.register_rule_implementation(RID, _Rule("v2"),
                                            version="v1")
    _register(pool, "v1")  # 注册表绑定 impl_version=v1
    out = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v1")
    assert out["decision"] == "not_evaluable"
    assert "IMPL_MISMATCH" in out["basis"]
    assert pool.get_by_id(cid)["promotion_status"] == "validating"


def test_impl_mismatch_registry_binding_differs(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    validation.register_rule_implementation(RID, _Rule("v1"))
    _register(pool, "v1", impl_key="other_impl")
    out = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v1")
    assert out["decision"] == "not_evaluable"
    assert "IMPL_MISMATCH" in out["basis"]


def test_exact_binding_passes_and_decision_carries_rule_id(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    validation.register_rule_implementation(RID, _Rule("v1"))
    _register(pool, "v1")
    out = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v1")
    assert out["decision"] == "passed"
    assert out["rule_id"] == RID
    assert out["rule_version"] == "v1"
    assert pool.get_by_id(cid)["promotion_status"] \
        == "validation_passed"


# ---------------------------------------------------------------------------
# 历史判定复现：登记新版本/停用旧版本后仍按原版本复现
# ---------------------------------------------------------------------------

def test_reproduce_decision_after_version_change_and_disable(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    validation.register_rule_implementation(RID, _Rule("v1"))
    _register(pool, "v1")
    out = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v1")
    assert out["decision"] == "passed"
    before = dict(out)

    # 注册表变更：v1 停用、v2 登记并批准
    validation.set_rule_status(pool, RID, "v1", "disabled")
    validation.register_rule_implementation(RID, _Rule("v2"))
    _register(pool, "v2")

    # 正式 judge 对已停用 v1 仍拒绝
    out_now = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v1")
    assert out_now["decision"] == "not_evaluable"

    # 但历史判定可按记录版本复现，且原记录一字不改
    rep = validation.reproduce_decision(pool, before["id"])
    assert rep["match"] is True
    assert rep["reproduced_decision"] == "passed"
    assert rep["rule_id"] == RID
    assert rep["rule_version"] == "v1"
    row = pool.conn.execute(
        "SELECT * FROM validation_decisions WHERE id = ?",
        (before["id"],)).fetchone()
    assert dict(row) == before


def test_reproduce_not_evaluable_row_is_not_applicable(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    out = validation.judge_validation(pool, cid)  # 无规则
    assert out["decision"] == "not_evaluable"
    rep = validation.reproduce_decision(pool, out["id"])
    assert rep["match"] is None
    assert rep["reproduced_decision"] is None


def test_reproduce_missing_impl_reports_no_impl(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    validation.register_rule_implementation(RID, _Rule("v1"))
    _register(pool, "v1")
    out = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v1")
    assert out["decision"] == "passed"
    validation.RULE_IMPLEMENTATIONS.pop((RID, "v1"), None)
    rep = validation.reproduce_decision(pool, out["id"])
    assert rep["match"] is False
    assert "NO_IMPL" in rep["note"]
