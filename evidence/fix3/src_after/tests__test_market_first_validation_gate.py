# -*- coding: utf-8 -*-
"""Market-First P0-3：验证通过门禁。

validation_passed 只能由 judge_validation 按版本化规则产生；
transition() 直达被拒并留痕；阈值未定版（无规则）时 judge
一律 not_evaluable；未成熟/指标 NULL/仅 backfill 三条硬门槛
先行拦截；被 forward_stats 排除的样本不计入判定。
"""
from datetime import date, timedelta

import pytest

from market_first import validation
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.promotion import IllegalTransitionError, transition


class _Rule:
    """测试专用规则实现（fix2-3 经注册表路径使用：测试库内
    登记 approved 行 + 代码侧登记实现）。"""

    version = "test-rule-0"

    def evaluate(self, scope, metrics):
        return "passed", "test rule"


def _register_test_rule(pool):
    validation.register_rule_implementation("test_rule", _Rule())
    validation.register_rule(
        pool, "test_rule", _Rule.version, status="approved",
        approved_by="unit-test")


def _judge_with_test_rule(pool, cid):
    _register_test_rule(pool)
    return validation.judge_validation(
        pool, cid, rule_id="test_rule", rule_version=_Rule.version)


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


def test_transition_validation_passed_direct_rejected(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    with pytest.raises(IllegalTransitionError):
        transition(pool, cid, "validation_passed", actor="tester")
    assert pool.get_by_id(cid)["promotion_status"] == "validating"
    hist = [h for h in pool.history_for(cid)
            if h["field"] == "promotion_transition_attempt"]
    assert len(hist) == 1
    assert hist[0]["new_value"] == "validation_passed"


def test_judge_no_rule_not_evaluable(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)  # 即使样本成熟，无规则也不得通过
    out = validation.judge_validation(pool, cid)
    assert out["decision"] == "not_evaluable"
    assert out["rule_version"] == validation.NO_RULE
    assert "no_approved_rule" in out["basis"]
    assert pool.get_by_id(cid)["promotion_status"] == "validating"
    n = pool.conn.execute(
        "SELECT COUNT(*) c FROM validation_decisions").fetchone()["c"]
    assert n == 1  # 判定留痕


def test_judge_backfill_only_not_passed(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    validation.create_sample(
        pool, cid, "inst_lhb_stored", entry_date="2026-10-21",
        benchmark_price=8.63, kind="backfill")
    out = _judge_with_test_rule(pool, cid)
    assert out["decision"] == "not_passed"
    assert "backfill" in out["basis"]
    assert pool.get_by_id(cid)["promotion_status"] == "validating"


def test_judge_immature_forward_not_passed(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    ev = pool.events_for(cid)[0]
    bars = _bars()
    sid = validation.create_sample(
        pool, cid, ev["source_id"], kind="forward",
        discovery_event_id=ev["id"], bars=bars)
    validation.evaluate_sample(pool, sid, bars[:8])  # 只给少量 bars
    sample = validation.get_sample(pool, sid)
    assert sample["maturity_status"] == "immature"
    out = _judge_with_test_rule(pool, cid)
    assert out["decision"] == "not_passed"
    assert pool.get_by_id(cid)["promotion_status"] == "validating"


def test_judge_mature_forward_passes_and_flows(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    _mature_forward(pool, cid)
    out = _judge_with_test_rule(pool, cid)
    assert out["decision"] == "passed"
    assert out["rule_version"] == "test-rule-0"
    assert pool.get_by_id(cid)["promotion_status"] == "validation_passed"
    # 通过后可继续合法流转到待审批
    transition(pool, cid, "pending_approval", actor="tester")
    assert pool.get_by_id(cid)["promotion_status"] == "pending_approval"


def test_judge_excluded_forward_sample_not_counted(tmp_path):
    pool, cid = _candidate_validating(tmp_path)
    sid = _mature_forward(pool, cid)
    validation.exclude_sample(
        pool, sid, "forward_stats", "审计裁定排除",
        decided_by="independent_audit")
    out = _judge_with_test_rule(pool, cid)
    assert out["decision"] == "not_passed"  # 排除后无 forward 样本
    assert pool.get_by_id(cid)["promotion_status"] == "validating"
