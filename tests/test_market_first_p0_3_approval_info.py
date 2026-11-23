# -*- coding: utf-8 -*-
"""P0-3 集成对抗测试（2026-11-23，judge-p0-integration）：批准信息强制。

审计口径：rule 状态转为 approved 时必须具备明确 actor、
reason、批准依据；同事务写入 approved_by、approved_at、
approval_basis 与 registry_history 留痕；无批准信息时整个
事务回滚（状态不变、无留痕、批准字段不落字）。

对抗形态：
- 无批准人（approved_by/actor 皆缺）-> 拒绝且回滚；
- 无 reason -> 拒绝且回滚；
- 无批准依据（approval_basis 缺失/空白）-> 拒绝且回滚；
- 伪造批准信息：调用方回填 approved_at 为历史时刻 ->
  落库值恒为服务器时间，伪造值不生效；
- set_rule_status 转 approved 缺任一项 -> 拒绝且回滚；
- 正对照：信息齐备 -> 状态、批准字段、留痕同事务落字；
- registry_history append-only：UPDATE/DELETE 被触发器拒绝。
"""
import sqlite3
from datetime import datetime

import pytest

from market_first import validation
from market_first.pool import CandidatePool

RID = "p03_rule"


def _pool(tmp_path):
    return CandidatePool(db_path=str(tmp_path / "mf.db"))


def _hist(pool):
    return validation.registry_history(pool, entity_type="rule",
                                       entity_id=RID)


def test_register_approved_requires_reason_and_basis(tmp_path):
    pool = _pool(tmp_path)
    with pytest.raises(ValueError):
        validation.register_rule(
            pool, RID, "v1", status="approved",
            approved_by="unit-test")  # 无 reason / 无依据
    with pytest.raises(ValueError):
        validation.register_rule(
            pool, RID, "v1", status="approved",
            approved_by="unit-test", reason="r")  # 无依据
    with pytest.raises(ValueError):
        validation.register_rule(
            pool, RID, "v1", status="approved",
            approved_by="unit-test", reason="r",
            approval_basis="   ")  # 依据为空白 = 无依据
    # 全部回滚：注册表无行、留痕无行
    assert validation.get_registered_rule(pool, RID, "v1") is None
    assert _hist(pool) == []


def test_register_approved_requires_approver(tmp_path):
    pool = _pool(tmp_path)
    with pytest.raises(ValueError):
        validation.register_rule(
            pool, RID, "v1", status="approved",
            reason="r", approval_basis="b")  # 无批准人
    assert validation.get_registered_rule(pool, RID, "v1") is None
    assert _hist(pool) == []


def test_register_approved_forged_approved_at_ignored(tmp_path):
    """伪造批准信息：approved_at 回填为 2020 年 -> 落库恒为
    服务器当前时间，伪造值不生效；actor/reason/依据与留痕
    同事务落字。"""
    pool = _pool(tmp_path)
    before = datetime.now().astimezone()
    validation.register_rule(
        pool, RID, "v1", status="approved",
        approved_by="unit-test", approved_at="2020-01-01T00:00:00+08:00",
        actor="unit-test", reason="integration approval",
        approval_basis="user ruling 2026-11-23 (test)")
    row = validation.get_registered_rule(pool, RID, "v1")
    assert row["status"] == "approved"
    assert row["approved_by"] == "unit-test"
    assert not row["approved_at"].startswith("2020-")
    stored = datetime.fromisoformat(row["approved_at"])
    assert stored >= before.replace(microsecond=0)
    assert row["approval_basis"] == "user ruling 2026-11-23 (test)"
    hist = _hist(pool)
    assert len(hist) == 1
    assert hist[0]["field"] == "status"
    assert hist[0]["old_value"] is None
    assert hist[0]["new_value"] == "approved"
    assert hist[0]["actor"] == "unit-test"
    assert hist[0]["reason"] == "integration approval"
    assert hist[0]["basis"] == "user ruling 2026-11-23 (test)"


def test_set_status_to_approved_requires_full_info_and_rolls_back(tmp_path):
    pool = _pool(tmp_path)
    validation.register_rule(pool, RID, "v1", status="draft")
    base_hist = len(_hist(pool))
    for kw in ({}, {"actor": "unit-test"},
               {"actor": "unit-test", "reason": "r"},
               {"actor": "unit-test", "reason": "r",
                "approval_basis": "  "}):
        with pytest.raises(ValueError):
            validation.set_rule_status(pool, RID, "v1", "approved",
                                       **kw)
        row = validation.get_registered_rule(pool, RID, "v1")
        assert row["status"] == "draft"          # 状态未变
        assert row["approved_by"] is None        # 批准字段未落字
        assert row["approved_at"] is None
        assert len(_hist(pool)) == base_hist     # 无留痕


def test_set_status_to_approved_writes_all_fields_atomically(tmp_path):
    pool = _pool(tmp_path)
    validation.register_rule(pool, RID, "v1", status="draft")
    validation.set_rule_status(
        pool, RID, "v1", "approved", actor="unit-test",
        reason="promotion review passed",
        approval_basis="audit verdict P0-3 (test)")
    row = validation.get_registered_rule(pool, RID, "v1")
    assert row["status"] == "approved"
    assert row["approved_by"] == "unit-test"
    assert row["approved_at"]
    assert row["approval_basis"] == "audit verdict P0-3 (test)"
    hist = _hist(pool)
    last = hist[-1]
    assert last["old_value"] == "draft"
    assert last["new_value"] == "approved"
    assert last["actor"] == "unit-test"
    assert last["reason"] == "promotion review passed"
    assert last["basis"] == "audit verdict P0-3 (test)"


def test_non_approved_transitions_still_work_without_approval_info(tmp_path):
    """非 approved 流转（如停用）不要求批准信息，保持既有
    调用兼容；留痕仍写入（actor/reason 可缺省）。"""
    pool = _pool(tmp_path)
    validation.register_rule(
        pool, RID, "v1", status="approved", approved_by="unit-test",
        actor="unit-test", reason="r", approval_basis="b")
    validation.set_rule_status(pool, RID, "v1", "disabled")
    row = validation.get_registered_rule(pool, RID, "v1")
    assert row["status"] == "disabled"
    hist = _hist(pool)
    assert hist[-1]["old_value"] == "approved"
    assert hist[-1]["new_value"] == "disabled"


def test_registry_history_is_append_only(tmp_path):
    pool = _pool(tmp_path)
    validation.register_rule(pool, RID, "v1", status="draft")
    hist = _hist(pool)
    assert len(hist) == 1
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE registry_history SET reason='tampered'"
            " WHERE id=?", (hist[0]["id"],))
    pool.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "DELETE FROM registry_history WHERE id=?",
            (hist[0]["id"],))
    pool.conn.rollback()
    assert len(_hist(pool)) == 1
