# -*- coding: utf-8 -*-
"""Market-First fix2-3：RULE_REGISTRY 注册保护与判定入口枚举。

正式 judge 只接受 rule_id+version 并从 rule_registry 注册表
加载：未登记/伪造版本/停用/test/draft 规则一律 not_evaluable
且留痕；规则对象直传参数已删除（TypeError）；promotion 受控
流转无真实 passed 判定记录时直接调用被拒并留痕；历史判定
不随注册表变更改写；测试注册域与正式注册域隔离（注册只
发生在测试库，规范库不得出现 approved 行）。
"""
from datetime import date, timedelta

import pytest

from market_first import promotion, validation
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.promotion import IllegalTransitionError, transition


class _Rule:
    version = "v1"

    def evaluate(self, scope, metrics):
        return "passed", "registry test rule"


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
    # fix3 信任路径（MF-FWD-002）：时间锚以库内服务器侧
    # ingested_at 为准；本 helper 模拟生产不变量「发现即
    # 入库」，把事件 created_at 对齐到模拟发现时刻；
    # 本文件全部断言未改动。
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


def _register(pool, version="v1", status="approved", rule_id="reg_rule"):
    # fix3 P0-B 精确绑定：登记某版本时必须同时登记自报该版本
    # 的实现（实例属性覆盖类属性 version），否则 judge 应以
    # NO_IMPL/IMPL_MISMATCH 拒绝——本 helper 按新口径提供与
    # 登记版本一致的实现；断言未改动。
    rule = _Rule()
    rule.version = version
    validation.register_rule_implementation(rule_id, rule)
    # P0-3 集成（2026-11-23）：approved 登记须带 actor/reason/
    # approval_basis，本 helper 补齐批准信息；断言未改动。
    extra = {}
    if status == "approved":
        extra = dict(actor="unit-test",
                     reason="registry test approval",
                     approval_basis="test-only approval (tmp db)")
    return validation.register_rule(
        pool, rule_id, version, status=status,
        approved_by="unit-test" if status == "approved" else None,
        **extra)


def _decisions(pool):
    rows = pool.conn.execute(
        "SELECT * FROM validation_decisions ORDER BY id").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 绕过面：未登记 / 伪造版本 / 直传对象
# ---------------------------------------------------------------------------

def test_unregistered_rule_rejected_and_traced(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    out = validation.judge_validation(
        pool, cid, rule_id="ghost_rule", rule_version="v1")
    assert out["decision"] == "not_evaluable"
    assert "rule_not_registered" in out["basis"]
    assert pool.get_by_id(cid)["promotion_status"] == "validating"
    assert len(_decisions(pool)) == 1  # 拒绝也留痕


def test_forged_version_rejected(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    _register(pool, version="v1")
    out = validation.judge_validation(
        pool, cid, rule_id="reg_rule", rule_version="v999")
    assert out["decision"] == "not_evaluable"
    assert "rule_not_registered" in out["basis"]
    assert pool.get_by_id(cid)["promotion_status"] == "validating"


def test_rule_object_injection_closed(tmp_path):
    # fix1 的 rule= 直传参数已删除：传入即 TypeError，正式入口
    # 不存在对象注入路径
    pool, cid = _candidate_validating(tmp_path)
    with pytest.raises(TypeError):
        validation.judge_validation(pool, cid, rule=_Rule())
    assert _decisions(pool) == []


def test_missing_version_rejected(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _register(pool, version="v1")
    out = validation.judge_validation(pool, cid, rule_id="reg_rule")
    assert out["decision"] == "not_evaluable"
    assert "rule_version_required" in out["basis"]


# ---------------------------------------------------------------------------
# 状态纪律：draft / test / disabled
# ---------------------------------------------------------------------------

def test_draft_and_test_status_rules_rejected(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    _register(pool, version="v1", status="draft")
    out = validation.judge_validation(
        pool, cid, rule_id="reg_rule", rule_version="v1")
    assert out["decision"] == "not_evaluable"
    assert "rule_not_approved" in out["basis"]
    assert "draft" in out["basis"]

    _register(pool, version="v2", status="test")
    out = validation.judge_validation(
        pool, cid, rule_id="reg_rule", rule_version="v2")
    assert out["decision"] == "not_evaluable"
    assert "rule_not_approved" in out["basis"]
    assert "test" in out["basis"]
    assert pool.get_by_id(cid)["promotion_status"] == "validating"


def test_approved_rule_in_test_db_can_pass(tmp_path):
    # 路径验证：在测试库内登记 approved 规则可正常判定通过
    # （规范库不得出现 approved 行，见交付查询证据）
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    _register(pool, version="v1", status="approved")
    out = validation.judge_validation(
        pool, cid, rule_id="reg_rule", rule_version="v1")
    assert out["decision"] == "passed"
    assert out["rule_version"] == "v1"
    assert pool.get_by_id(cid)["promotion_status"] == \
        "validation_passed"


def test_disabled_rule_stops_passing_history_kept(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    _register(pool, version="v1", status="approved")
    out1 = validation.judge_validation(
        pool, cid, rule_id="reg_rule", rule_version="v1")
    assert out1["decision"] == "passed"

    validation.set_rule_status(pool, "reg_rule", "v1", "disabled")
    # 停用后判定回到 not_evaluable，且历史 passed 记录的
    # rule_version/metrics 不被改写
    before = _decisions(pool)
    out2 = validation.judge_validation(
        pool, cid, rule_id="reg_rule", rule_version="v1")
    assert out2["decision"] == "not_evaluable"
    assert "disabled" in out2["basis"]
    after = _decisions(pool)
    assert after[0] == before[0]
    assert after[0]["decision"] == "passed"
    assert after[0]["rule_version"] == "v1"


def test_rule_version_change_new_decisions_use_new_version(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    _register(pool, version="v1", status="approved")
    out1 = validation.judge_validation(
        pool, cid, rule_id="reg_rule", rule_version="v1")
    assert out1["rule_version"] == "v1"

    # v2 取代 v1（v1 停用）：新判定用 v2，旧记录仍标 v1
    validation.set_rule_status(pool, "reg_rule", "v1", "disabled")
    _register(pool, version="v2", status="approved")
    # 同库再建一个候选验证 v2 判定记录
    ev = DiscoveryEvent(
        market="SZ", symbol="000530", name="冰山冷热",
        source_id="inst_lhb_live",
        discovered_at="2026-11-13T16:30:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 50.0})
    cid2, _ = pool.ingest(ev)
    # 同 _candidate_validating 的 fix3 对齐：模拟发现即入库。
    pool.conn.execute(
        "UPDATE discovery_events SET created_at=?"
        " WHERE candidate_id=?", (ev.discovered_at, cid2))
    pool.conn.commit()
    for state in ("pending_research", "researching", "validating"):
        transition(pool, cid2, state, actor="tester")
    _mature_forward(pool, cid2)
    out2 = validation.judge_validation(
        pool, cid2, rule_id="reg_rule", rule_version="v2")
    assert out2["decision"] == "passed"
    assert out2["rule_version"] == "v2"
    rows = _decisions(pool)
    assert rows[0]["rule_version"] == "v1"  # 旧记录未被改写
    assert rows[-1]["rule_version"] == "v2"


# ---------------------------------------------------------------------------
# promotion 受控流转：绕过 judge 直接调用被拒
# ---------------------------------------------------------------------------

def test_apply_validation_passed_direct_call_rejected(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    # 无任何判定记录时直接调用受控流转 -> 拒绝 + 留痕
    with pytest.raises(IllegalTransitionError):
        promotion._apply_validation_passed(
            pool, cid, actor="attacker", decision_id=None)
    # 伪造 decision_id -> 同样拒绝
    with pytest.raises(IllegalTransitionError):
        promotion._apply_validation_passed(
            pool, cid, actor="attacker", decision_id=99999)
    assert pool.get_by_id(cid)["promotion_status"] == "validating"
    hist = [h for h in pool.history_for(cid)
            if h["field"] == "promotion_transition_attempt"]
    assert len(hist) == 2
    assert all(h["new_value"] == "validation_passed" for h in hist)


def test_fresh_db_registry_empty(tmp_path):
    # 新库注册表为空：任何 rule_id 判定都不得通过
    pool = CandidatePool(db_path=str(tmp_path / "fresh.db"))
    rows = pool.conn.execute(
        "SELECT * FROM rule_registry").fetchall()
    assert rows == []
