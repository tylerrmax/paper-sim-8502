# -*- coding: utf-8 -*-
"""P0-1 集成对抗测试（2026-11-23，judge-p0-integration）：规则版本绑定。

审计口径：实现对象声明版本必须与注册版本完全一致；禁止显式
version 参数覆盖实际实现版本；不匹配时拒绝判定
（not_evaluable）。本文件在 fix3 基线（已含 P0-B 精确绑定）
上以审计给定的对抗形态逐项验证：

- v1 实现经显式 version='v2' 冒充登记 -> judge v2 拒绝
  （IMPL_MISMATCH：登记条目的 impl_version 恒取实现自报
  版本，显式参数改不了 impl_version）；
- 注册表为 v2 伪造 impl_version='v1' 绑定 -> judge v2 拒绝
  （NO_IMPL：实现只按 (rule_id, version) 精确取）；
- 只有 v2 实现时 judge v1 -> NO_IMPL 拒绝；
- 正对照：版本精确一致时判定可通过。
"""
from datetime import date, timedelta

import pytest

from market_first import validation
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.promotion import transition

RID = "p01_bind_rule"


class _Rule:
    def __init__(self, version):
        self.version = version

    def evaluate(self, scope, metrics):
        return "passed", "p0-1 test rule %s" % self.version


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


def _register_approved(pool, version, **kw):
    kw.setdefault("approved_by", "unit-test")
    kw.setdefault("actor", "unit-test")
    kw.setdefault("reason", "p0-1 integration test registration")
    kw.setdefault("approval_basis", "test-only approval (tmp db)")
    return validation.register_rule(
        pool, RID, version, status="approved", **kw)


def test_v1_impl_cannot_impersonate_v2_via_explicit_version(tmp_path):
    """v1 实现以显式 version='v2' 登记（冒充）：登记条目的
    impl_version 恒为实现自报的 'v1'，judge v2 必须拒绝。"""
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    validation.register_rule_implementation(RID, _Rule("v1"),
                                            version="v2")
    entry = validation.RULE_IMPLEMENTATIONS[(RID, "v2")]
    # 显式 version 只改变登记键，覆盖不了实现自报版本
    assert entry["impl_version"] == "v1"
    _register_approved(pool, "v2")  # 注册表绑定 impl_version='v2'
    out = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v2")
    assert out["decision"] == "not_evaluable"
    assert "IMPL_MISMATCH" in out["basis"]
    assert pool.get_by_id(cid)["promotion_status"] == "validating"


def test_registry_forged_impl_version_binding_rejected(tmp_path):
    """注册表为 v2 伪造 impl_version='v1' 绑定，代码侧只有
    v1 实现：judge v2 按 (rule_id, 'v2') 精确取实现 -> 无
    实现，拒绝判定，不得借 v1 实现冒充 v2。"""
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    validation.register_rule_implementation(RID, _Rule("v1"))
    _register_approved(pool, "v2", impl_version="v1")
    out = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v2")
    assert out["decision"] == "not_evaluable"
    assert "NO_IMPL" in out["basis"]


def test_v2_impl_does_not_satisfy_v1_judgement(tmp_path):
    """只有 v2 实现时，judge v1 无精确版本实现 -> 拒绝。"""
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    validation.register_rule_implementation(RID, _Rule("v2"))
    _register_approved(pool, "v1")
    out = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v1")
    assert out["decision"] == "not_evaluable"
    assert "NO_IMPL" in out["basis"]


def test_exact_version_match_positive_control(tmp_path):
    """正对照：实现自报版本与注册版本精确一致 -> 可判定。"""
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    validation.register_rule_implementation(RID, _Rule("v2"))
    _register_approved(pool, "v2")
    out = validation.judge_validation(
        pool, cid, rule_id=RID, rule_version="v2")
    assert out["decision"] == "passed"
    assert out["rule_id"] == RID
    assert out["rule_version"] == "v2"
