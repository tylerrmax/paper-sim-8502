# -*- coding: utf-8 -*-
"""RT-2 审计记录唯一绑定对抗测试（2026-11-23 V3 审计补正 P0-2）。

缺口（V3 独立审计）：既有底层闸靠字段逐字相等判断审计
行对应，同一审计行可被多条排除反复引用；被引用的审计
行事后可改可删，证据生命周期不闭合。

修复：
- sample_exclusions.audit_history_id 显式引用审计行 id
  （candidate_history.id），UNIQUE 索引 + 触发器双重
  保证一对一；
- 触发器校验绑定必备与引用逐字对应（原字段对应校验
  保留为冗余校验）；
- trg_rt_candidate_history_no_update_bound /
  no_delete_bound：被任一排除引用的审计行禁止改删；
- exclude_sample 落字时写入绑定 id。

口径修订（P0-2 第 5 项）：本机制证明审计证据的存在性、
唯一性与不可变性——「不可变、一对一关联的审计证据」；
不声称证明审计行与排除行同出一事务（同源性），该界限
在设计文档与 GAPS 中明示。
"""
import sqlite3

import pytest

from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.validation import (create_sample, exclude_sample,
                                     excluded_sample_ids)

BARS = [
    ("2026-11-12", 25.0, 25.5, 24.5, 25.2, 1000.0),
    ("2026-11-13", 26.0, 26.5, 25.8, 26.25, 1000.0),
    ("2026-11-16", 26.8, 27.2, 26.6, 27.00, 1000.0),
    ("2026-11-17", 27.1, 27.4, 26.9, 27.30, 1000.0),
]

REASON = "审计裁定排除"
DECIDER = "independent_audit"
NOW = "2026-11-23T10:00:00+08:00"


def _pool_with_candidate(tmp_path, symbol="002623", market="SZ"):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market=market, symbol=symbol, name="测试标的",
        source_id="inst_lhb_live",
        discovered_at="2026-11-13T16:30:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    pool.clock = lambda: ev.discovered_at
    cid, _ = pool.ingest(ev)
    return pool, cid


def _pool_with_sample(tmp_path):
    pool, cid = _pool_with_candidate(tmp_path)
    ev_id = pool.events_for(cid)[0]["id"]
    sid = create_sample(pool, cid, "inst_lhb_live", kind="forward",
                        discovery_event_id=ev_id, bars=BARS)
    return pool, cid, sid


def _second_sample(pool, cid, entry_date="2026-11-17"):
    """同候选的第二条样本行（直写行，仅作排除对象）。"""
    cur = pool.conn.execute(
        "INSERT INTO validation_samples (candidate_id, source_id,"
        " kind, entry_date, created_at) VALUES (?, 'inst_lhb_live',"
        " 'backfill', ?, ?)", (cid, entry_date, NOW))
    pool.conn.commit()
    return cur.lastrowid


def _audit_row_for(pool, xid):
    return pool.conn.execute(
        "SELECT h.* FROM sample_exclusions e"
        " JOIN candidate_history h ON h.id = e.audit_history_id"
        " WHERE e.id=?", (xid,)).fetchone()


def _direct_audit(pool, cid, reason=REASON, actor=DECIDER):
    cur = pool.conn.execute(
        "INSERT INTO candidate_history (candidate_id, field,"
        " old_value, new_value, actor, reason, created_at) VALUES"
        " (?, 'sample_exclusion', NULL, 'forward_stats', ?, ?, ?)",
        (cid, actor, reason, NOW))
    pool.conn.commit()
    return cur.lastrowid


def _direct_exclusion(pool, sid, audit_history_id,
                      reason=REASON, decided_by=DECIDER):
    pool.conn.execute(
        "INSERT INTO sample_exclusions (sample_id, excluded_from,"
        " reason, decided_by, created_at, audit_history_id) VALUES"
        " (?, 'forward_stats', ?, ?, ?, ?)",
        (sid, reason, decided_by, NOW, audit_history_id))


# --- 绑定正对照 -----------------------------------------------------------

def test_exclude_sample_writes_explicit_binding(tmp_path):
    """正常路径：exclude_sample 落字的排除行带 audit_history_id，
    且恰指向那条逐字对应的审计行。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    xid = exclude_sample(pool, sid, "forward_stats", REASON,
                         decided_by=DECIDER)
    row = pool.conn.execute(
        "SELECT * FROM sample_exclusions WHERE id=?",
        (xid,)).fetchone()
    assert row["audit_history_id"] is not None
    audit = _audit_row_for(pool, xid)
    assert audit is not None
    assert audit["candidate_id"] == cid
    assert audit["field"] == "sample_exclusion"
    assert audit["actor"] == DECIDER
    assert audit["reason"] == REASON
    assert excluded_sample_ids(pool) == {sid}


def test_direct_insert_with_explicit_binding_passes(tmp_path):
    """DB 层正对照：审计行先行 + 排除行显式绑定其 id -> 通过。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    hid = _direct_audit(pool, cid)
    _direct_exclusion(pool, sid, hid)
    pool.conn.commit()
    assert excluded_sample_ids(pool) == {sid}


def test_direct_insert_without_binding_blocked(tmp_path):
    """字段逐字对应的审计行存在，但排除行不带绑定 id ->
    拒绝：字段相等不再足以放行，绑定必备。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    _direct_audit(pool, cid)
    with pytest.raises(sqlite3.IntegrityError):
        _direct_exclusion(pool, sid, None)
    pool.conn.rollback()
    assert excluded_sample_ids(pool) == set()


# --- 对抗 1：旧审计行复用 ---------------------------------------------------

def test_bound_audit_row_reuse_blocked(tmp_path):
    """已被一条排除绑定的审计行，再被第二条排除引用 ->
    一对一校验拒绝，第二条样本不被排除。"""
    pool, cid, sid1 = _pool_with_sample(tmp_path)
    xid = exclude_sample(pool, sid1, "forward_stats", REASON,
                         decided_by=DECIDER)
    hid = pool.conn.execute(
        "SELECT audit_history_id FROM sample_exclusions"
        " WHERE id=?", (xid,)).fetchone()["audit_history_id"]
    sid2 = _second_sample(pool, cid)
    with pytest.raises(sqlite3.IntegrityError):
        _direct_exclusion(pool, sid2, hid)
    pool.conn.rollback()
    assert excluded_sample_ids(pool) == {sid1}


# --- 对抗 2：跨样本复用 -----------------------------------------------------

def test_cross_candidate_audit_reuse_blocked(tmp_path):
    """甲候选的审计行被拿去绑定乙候选样本的排除 -> 引用
    对应校验拒绝（审计行 candidate_id 与样本候选不一致）。"""
    pool, cid1, sid1 = _pool_with_sample(tmp_path)
    ev = DiscoveryEvent(
        market="SH", symbol="603893", name="另一标的",
        source_id="inst_lhb_live",
        discovered_at="2026-11-13T16:30:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 1.0})
    cid2, _ = pool.ingest(ev)
    sid2 = _second_sample(pool, cid2)
    hid = _direct_audit(pool, cid1)
    with pytest.raises(sqlite3.IntegrityError):
        _direct_exclusion(pool, sid2, hid)
    pool.conn.rollback()
    assert excluded_sample_ids(pool) == set()


# --- 对抗 3：事后删除被引用审计行 ---------------------------------------------

def test_bound_audit_row_delete_blocked(tmp_path):
    """排除完成后直写 DELETE 其被绑定的审计行 -> 拒绝，
    审计行与排除行均原样保留。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    xid = exclude_sample(pool, sid, "forward_stats", REASON,
                         decided_by=DECIDER)
    hid = pool.conn.execute(
        "SELECT audit_history_id FROM sample_exclusions"
        " WHERE id=?", (xid,)).fetchone()["audit_history_id"]
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "DELETE FROM candidate_history WHERE id=?", (hid,))
    pool.conn.rollback()
    assert _audit_row_for(pool, xid) is not None
    assert excluded_sample_ids(pool) == {sid}


# --- 对抗 4：事后修改被引用审计行 ---------------------------------------------

@pytest.mark.parametrize("sql", [
    "UPDATE candidate_history SET reason='事后改理由' WHERE id=?",
    "UPDATE candidate_history SET actor='someone_else' WHERE id=?",
    "UPDATE candidate_history SET new_value='judge_stats' WHERE id=?",
    "UPDATE candidate_history SET candidate_id=999 WHERE id=?",
])
def test_bound_audit_row_update_blocked(tmp_path, sql):
    """排除完成后直写 UPDATE 其被绑定的审计行任一字段 ->
    拒绝，审计行逐字不变。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    xid = exclude_sample(pool, sid, "forward_stats", REASON,
                         decided_by=DECIDER)
    hid = pool.conn.execute(
        "SELECT audit_history_id FROM sample_exclusions"
        " WHERE id=?", (xid,)).fetchone()["audit_history_id"]
    before = dict(pool.conn.execute(
        "SELECT * FROM candidate_history WHERE id=?",
        (hid,)).fetchone())
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(sql, (hid,))
    pool.conn.rollback()
    after = dict(pool.conn.execute(
        "SELECT * FROM candidate_history WHERE id=?",
        (hid,)).fetchone())
    assert after == before


# --- 范围边界 -----------------------------------------------------------------

def test_unbound_history_row_not_covered_by_binding_guard(tmp_path):
    """未被任何排除引用的历史行不在绑定闸范围（删除放行）：
    rt2 既有「删未引用不匹配审计行」场景口径不变。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    hid = _direct_audit(pool, cid, reason="未被引用的审计行")
    pool.conn.execute(
        "DELETE FROM candidate_history WHERE id=?", (hid,))
    pool.conn.commit()
    assert pool.conn.execute(
        "SELECT COUNT(*) c FROM candidate_history WHERE id=?",
        (hid,)).fetchone()["c"] == 0
