# -*- coding: utf-8 -*-
"""P0-3 数据库审批保护测试（2026-11-23 红队集中修复第 5 项）。

问题（自红队探针 A1/A2）：应用层（register_rule /
set_rule_status）已强制 approved 须带批准人/依据/服务器时间，
但 DB 层无防护：
- A1：直写 UPDATE rule_registry SET status='approved',
  approved_at='2020-01-01' 等伪造字段成功；
- A2：直写 INSERT registry_history 伪造 approved 流转记录
  成功（既有触发器只禁 history 的 UPDATE/DELETE）。

修复（db.ensure_redteam_schema，设计见 evidence 设计文档）：
- trg_rt_rule_registry_approved_insert / _update：进入
  approved 必须 approved_by/approval_basis 非空且
  approved_at 与 DB 服务器时间差 ≤120 秒；
- trg_rt_registry_history_approved_insert：approved 流转
  记录必须有状态一致（已 approved 且批准字段非空）的
  rule_registry 行对应；
- history UPDATE/DELETE 防护保持不变。

诚实残余：同主体一条 SQL 全套一致伪造（当前时间 + 齐全
字段）与应用写入不可区分，属 SQLite 单文件固有界限，
登记待审计裁定（设计文档第一节）。
"""
import sqlite3

import pytest

from market_first import validation
from market_first.pool import CandidatePool

RID = "mf-judge-test"


def _pool(tmp_path):
    return CandidatePool(db_path=str(tmp_path / "mf.db"))


def _registry_row(pool, version="v1"):
    return pool.conn.execute(
        "SELECT * FROM rule_registry WHERE rule_id=? AND version=?",
        (RID, version)).fetchone()


def test_direct_insert_approved_backdated_blocked(tmp_path):
    """A1 复刻（INSERT 形态）：直写 approved + 回填 2020 年
    approved_at -> 触发器拒绝，行不落库。"""
    pool = _pool(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "INSERT INTO rule_registry (rule_id, version, status,"
            " approved_by, approved_at, approval_basis, created_at)"
            " VALUES (?, ?, 'approved', '伪造人',"
            " '2020-01-01T00:00:00+08:00', '伪造依据',"
            " '2020-01-01T00:00:00+08:00')", (RID, "v1"))
    pool.conn.rollback()
    assert _registry_row(pool) is None


def test_direct_insert_approved_missing_basis_blocked(tmp_path):
    """直写 approved 但 approval_basis 为空 -> 拒绝。"""
    pool = _pool(tmp_path)
    now = pool.conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S+08:00', 'now',"
        " '+8 hours')").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "INSERT INTO rule_registry (rule_id, version, status,"
            " approved_by, approved_at, approval_basis, created_at)"
            " VALUES (?, ?, 'approved', '某人', ?, '', ?)",
            (RID, "v1", now, now))
    pool.conn.rollback()
    assert _registry_row(pool) is None


def test_direct_update_to_approved_forged_blocked(tmp_path):
    """A1 复刻（UPDATE 形态）：draft 行直写改 approved 并伪造
    批准时间 -> 拒绝，状态仍为 draft。"""
    pool = _pool(tmp_path)
    validation.register_rule(pool, RID, "v1", status="draft",
                             actor="tester", reason="登记草稿")
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE rule_registry SET status='approved',"
            " approved_by='伪造人',"
            " approved_at='2020-01-01T00:00:00+08:00',"
            " approval_basis='伪造依据'"
            " WHERE rule_id=? AND version=?", (RID, "v1"))
    pool.conn.rollback()
    assert _registry_row(pool)["status"] == "draft"


def test_forged_history_approved_insert_blocked(tmp_path):
    """A2 复刻：无对应 approved registry 行时，伪造
    registry_history approved 流转记录 -> 拒绝。"""
    pool = _pool(tmp_path)
    validation.register_rule(pool, RID, "v1", status="draft",
                             actor="tester", reason="登记草稿")
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "INSERT INTO registry_history (entity_type, entity_id,"
            " entity_version, field, old_value, new_value, actor,"
            " reason, basis, created_at) VALUES ('rule', ?, 'v1',"
            " 'status', 'draft', 'approved', '伪造人', '伪造',"
            " '伪造依据', '2020-01-01T00:00:00+08:00')", (RID,))
    pool.conn.rollback()
    rows = pool.conn.execute(
        "SELECT COUNT(*) c FROM registry_history"
        " WHERE new_value='approved'").fetchone()["c"]
    assert rows == 0


def test_history_update_delete_still_blocked(tmp_path):
    """既有防护回归：registry_history UPDATE/DELETE 仍被拒。"""
    pool = _pool(tmp_path)
    validation.register_rule(
        pool, RID, "v1", status="approved", approved_by="unit-test",
        actor="unit-test", reason="正路登记",
        approval_basis="test-only approval (tmp db)")
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE registry_history SET actor='篡改'"
            " WHERE entity_id=?", (RID,))
    pool.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "DELETE FROM registry_history WHERE entity_id=?",
            (RID,))
    pool.conn.rollback()


def test_app_path_approval_still_works(tmp_path):
    """正常路径回归：应用层 register_rule(approved) 与
    set_rule_status(draft->approved) 在触发器下天然通过，
    registry 与 history 双行齐全。"""
    pool = _pool(tmp_path)
    validation.register_rule(
        pool, RID, "v1", status="approved", approved_by="unit-test",
        actor="unit-test", reason="正路登记",
        approval_basis="test-only approval (tmp db)")
    row = _registry_row(pool)
    assert row["status"] == "approved"
    assert row["approved_by"] == "unit-test"
    validation.register_rule(pool, RID, "v2", status="draft",
                             actor="tester", reason="登记草稿")
    validation.set_rule_status(
        pool, RID, "v2", "approved", actor="approver",
        reason="复核通过", approval_basis="五项证据齐全")
    assert _registry_row(pool, "v2")["status"] == "approved"
    hist = pool.conn.execute(
        "SELECT COUNT(*) c FROM registry_history"
        " WHERE new_value='approved'").fetchone()["c"]
    assert hist == 2
