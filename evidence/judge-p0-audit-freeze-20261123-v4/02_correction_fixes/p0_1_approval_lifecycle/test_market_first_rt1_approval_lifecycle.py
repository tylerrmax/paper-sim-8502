# -*- coding: utf-8 -*-
"""RT-1 审批记录持续有效性对抗测试（2026-11-23 V3 审计补正 P0-1）。

缺口（V3 独立审计）：既有底层闸只校验「进入 approved 的
瞬间」存在有效 approvals 行；事后直写 DELETE 该行、或
UPDATE 其 candidate_id/decision/approver/basis 等关键
字段使其失效或转移，均无拦截——候选可继续停留在无有效
审批依据的 approved 状态。

修复（db.ensure_redteam_schema，_V3C_TRIGGERS_SQL）：
- trg_rt_approvals_no_delete_live：被删行是有效批准
  （decision='approved'、approver/basis 非空）且候选当前
  promotion_status='approved' 时，DELETE -> RAISE(ABORT)；
- trg_rt_approvals_no_update_keyfields_live：同等条件下，
  candidate_id/decision/approver/basis/decided_at 任一
  实际变化 -> RAISE(ABORT)。

范围边界：候选未处于 approved、或行本身非有效批准
（rejected 记录等）不在此闸范围；decide 正常路径只
INSERT approvals，天然通过（本文件正对照实证）。
"""
import sqlite3
from datetime import date, timedelta

import pytest

from market_first import bars as bars_mod
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
        reason="rt1 lifecycle test",
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


def _pool_with_candidate(tmp_path, symbol="603893"):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol=symbol, name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    pool.clock = lambda: ev.discovered_at
    cid, _ = pool.ingest(ev)
    return pool, cid


def _insert_approvals(pool, cid, decision="approved",
                      approver="alice", basis="依据充分"):
    cur = pool.conn.execute(
        "INSERT INTO approvals (candidate_id, decision, approver,"
        " decided_at, basis, versions_json) VALUES (?, ?, ?,"
        " '2026-11-23T10:00:00+08:00', ?, '{}')",
        (cid, decision, approver, basis))
    pool.conn.commit()
    return cur.lastrowid


def _approved_candidate(tmp_path):
    """构造「已 approved + 有效 approvals 行」的候选（经既有
    底层闸的正对照路径落字）。"""
    pool, cid = _pool_with_candidate(tmp_path)
    aid = _insert_approvals(pool, cid)
    pool.conn.execute(
        "UPDATE candidates SET promotion_status='approved'"
        " WHERE id=?", (cid,))
    pool.conn.commit()
    assert pool.get_by_id(cid)["promotion_status"] == "approved"
    return pool, cid, aid


def _approval_row(pool, aid):
    return pool.conn.execute(
        "SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()


def _valid_basis_count(pool, cid):
    return pool.conn.execute(
        "SELECT COUNT(*) c FROM approvals WHERE candidate_id=?"
        " AND decision='approved' AND approver IS NOT NULL"
        " AND trim(approver) <> '' AND basis IS NOT NULL"
        " AND trim(basis) <> ''", (cid,)).fetchone()["c"]


# --- 事后删除对抗 -------------------------------------------------------

def test_delete_live_approval_blocked(tmp_path):
    """候选 approved 时直写 DELETE 其有效 approvals 记录 ->
    拒绝；记录与状态均不变，候选不失去审批依据。"""
    pool, cid, aid = _approved_candidate(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute("DELETE FROM approvals WHERE id=?",
                          (aid,))
    pool.conn.rollback()
    assert _approval_row(pool, aid) is not None
    assert pool.get_by_id(cid)["promotion_status"] == "approved"
    assert _valid_basis_count(pool, cid) == 1


def test_delete_all_approvals_of_approved_candidate_blocked(tmp_path):
    """整表式 DELETE（WHERE candidate_id）同样被拒。"""
    pool, cid, aid = _approved_candidate(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "DELETE FROM approvals WHERE candidate_id=?", (cid,))
    pool.conn.rollback()
    assert _valid_basis_count(pool, cid) == 1


# --- 事后修改关键字段对抗 -----------------------------------------------

@pytest.mark.parametrize("sql,params", [
    ("UPDATE approvals SET decision='rejected' WHERE id=?", ()),
    ("UPDATE approvals SET approver='' WHERE id=?", ()),
    ("UPDATE approvals SET approver='mallory' WHERE id=?", ()),
    ("UPDATE approvals SET basis='' WHERE id=?", ()),
    ("UPDATE approvals SET basis='事后篡改依据' WHERE id=?", ()),
    ("UPDATE approvals SET decided_at='2020-01-01T00:00:00+08:00'"
     " WHERE id=?", ()),
])
def test_update_keyfields_live_approval_blocked(tmp_path, sql, params):
    """使有效批准失效或改写的关键字段 UPDATE -> 拒绝，
    记录逐字不变。"""
    pool, cid, aid = _approved_candidate(tmp_path)
    before = dict(_approval_row(pool, aid))
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(sql, (aid,) + params)
    pool.conn.rollback()
    after = dict(_approval_row(pool, aid))
    assert after == before
    assert _valid_basis_count(pool, cid) == 1


def test_update_candidate_id_transfer_blocked(tmp_path):
    """把有效批准转移给另一候选（改 candidate_id）-> 拒绝，
    原候选依据仍在、另一候选未凭空获得依据。"""
    pool, cid, aid = _approved_candidate(tmp_path)
    # 在同一库内另建一候选
    ev = DiscoveryEvent(
        market="SZ", symbol="000999", name="另一标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 1.0})
    cid2, _ = pool.ingest(ev)
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE approvals SET candidate_id=? WHERE id=?",
            (cid2, aid))
    pool.conn.rollback()
    assert _approval_row(pool, aid)["candidate_id"] == cid
    assert _valid_basis_count(pool, cid) == 1
    assert _valid_basis_count(pool, cid2) == 0


# --- 范围边界（不误伤） ---------------------------------------------------

def test_delete_approval_of_non_approved_candidate_allowed(tmp_path):
    """候选未处于 approved 时，其 approvals 行不是任何终态
    的依据，DELETE 不在此闸范围（回归：rt1_db_guard 既有
    清理场景口径不变）。"""
    pool, cid = _pool_with_candidate(tmp_path)
    aid = _insert_approvals(pool, cid)
    pool.conn.execute("DELETE FROM approvals WHERE id=?", (aid,))
    pool.conn.commit()
    assert _approval_row(pool, aid) is None


def test_update_rejected_record_allowed(tmp_path):
    """rejected 决策记录非 approved 依据，关键字段修改
    不在此闸范围。"""
    pool, cid = _pool_with_candidate(tmp_path)
    aid = _insert_approvals(pool, cid, decision="rejected",
                            approver="alice", basis="不通过")
    pool.conn.execute(
        "UPDATE approvals SET basis='补充说明' WHERE id=?", (aid,))
    pool.conn.commit()
    assert _approval_row(pool, aid)["basis"] == "补充说明"


# --- 正常路径正对照 -------------------------------------------------------

def test_decide_normal_path_and_lifecycle(tmp_path):
    """decide 全链正常落字（INSERT approvals + 流转），且
    落字后的有效记录随即受生命周期保护（DELETE 被拒）。"""
    pool, cid = _pool_with_candidate(tmp_path)
    for state in ("pending_research", "researching", "validating"):
        transition(pool, cid, state, actor="tester", reason="walk")
    _register_test_rule(pool)
    ev = pool.events_for(cid)[0]
    bars = _gen_bars()
    sid = validation.create_sample(
        pool, cid, ev["source_id"], kind="forward",
        discovery_event_id=ev["id"], bars=bars,
        fetched_at="2026-11-17T16:00:00+08:00")
    bars_mod.store_bars(pool.get_by_id(cid)["symbol"], bars,
                        "test-fixture", pool.db_path)
    validation.evaluate_sample(pool, sid, bars)
    out = validation.judge_validation(
        pool, cid, rule_id="test_rule",
        rule_version=_TestRule.version)
    assert out["decision"] == "passed"
    transition(pool, cid, "pending_approval", actor="tester",
               reason="walk")
    aid = decide(pool, cid, approver="alice", decision="approved",
                 basis="研究与验证完整")
    assert pool.get_by_id(cid)["promotion_status"] == "approved"
    assert _valid_basis_count(pool, cid) == 1
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute("DELETE FROM approvals WHERE id=?",
                          (aid,))
    pool.conn.rollback()
    assert _valid_basis_count(pool, cid) == 1
