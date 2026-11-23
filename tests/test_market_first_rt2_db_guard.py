# -*- coding: utf-8 -*-
"""RT-2 底层闸对抗测试（2026-11-23 补强批）：样本排除的 DB 层拦截。

背景（用户最终验收原则）：关键约束必须在正式入口和底层边界
同时成立。RT-2 已在 validation.exclude_sample 入口强制
reason/decided_by 非空、且排除行与 candidate_history 审计行
同事务原子写入，但 sample_exclusions 此前无 DB 层防护——
直写 SQL ``INSERT INTO sample_exclusions`` 可无审计留痕地
改变 forward_stats 样本构成。

修复（db.ensure_redteam_schema，设计补记 DESIGN-BOTTOM-LAYER.md）：
- trg_rt_sample_exclusions_audit_insert：每条排除 INSERT
  必须 reason/decided_by 非空，且已有逐字对应的
  candidate_history 审计行（field='sample_exclusion'、
  new_value=excluded_from、actor=decided_by、reason=reason），
  否则 RAISE(ABORT)；
- exclude_sample 写序配合改为先写审计行、后写排除行
  （应用层单次 commit），天然通过。

V3 补正 P0-2（2026-11-23）修订：排除行须以
audit_history_id 显式绑定审计行（一对一、被引用审计行
不可改删），口径为「不可变、一对一关联的审计证据」，
不再声称触发器证明同事务身份；本文件直写辅助函数已
迁移为显式传绑定 id（构造迁移，断言未改动，登记见
DELIVERY-V3-CORRECTION）。唯一绑定与四类复用/篡改对抗
见 test_market_first_rt2_audit_binding.py。

诚实残余：先伪造一条逐字对应的审计行再按其内容写排除
行，与应用写入不可区分，属 SQLite 单文件固有界限，
登记待审计裁定。
"""
import sqlite3

import pytest

from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.validation import (create_sample, exclude_sample,
                                     excluded_sample_ids, get_sample)

BARS = [
    ("2026-11-12", 25.0, 25.5, 24.5, 25.2, 1000.0),
    ("2026-11-13", 26.0, 26.5, 25.8, 26.25, 1000.0),
    ("2026-11-16", 26.8, 27.2, 26.6, 27.00, 1000.0),
    ("2026-11-17", 27.1, 27.4, 26.9, 27.30, 1000.0),
]

REASON = "审计裁定排除"
DECIDER = "independent_audit"
NOW = "2026-11-23T10:00:00+08:00"


def _pool_with_sample(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id="inst_lhb_live",
        discovered_at="2026-11-13T16:30:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    pool.clock = lambda: ev.discovered_at
    cid, _ = pool.ingest(ev)
    ev_id = pool.events_for(cid)[0]["id"]
    sid = create_sample(pool, cid, "inst_lhb_live", kind="forward",
                        discovery_event_id=ev_id, bars=BARS)
    return pool, cid, sid


def _exclusion_rows(pool):
    return pool.conn.execute(
        "SELECT COUNT(*) c FROM sample_exclusions").fetchone()["c"]


def _audit_rows(pool):
    return pool.conn.execute(
        "SELECT COUNT(*) c FROM candidate_history"
        " WHERE field='sample_exclusion'").fetchone()["c"]


def _direct_insert_exclusion(pool, sid, audit_history_id=None,
                             reason=REASON, decided_by=DECIDER):
    pool.conn.execute(
        "INSERT INTO sample_exclusions (sample_id, excluded_from,"
        " reason, decided_by, created_at, audit_history_id) VALUES"
        " (?, 'forward_stats', ?, ?, ?, ?)",
        (sid, reason, decided_by, NOW, audit_history_id))


def _direct_insert_audit(pool, cid, reason=REASON, actor=DECIDER):
    cur = pool.conn.execute(
        "INSERT INTO candidate_history (candidate_id, field,"
        " old_value, new_value, actor, reason, created_at) VALUES"
        " (?, 'sample_exclusion', NULL, 'forward_stats', ?, ?, ?)",
        (cid, actor, reason, NOW))
    return cur.lastrowid


# --- 直写 SQL 污染对抗 -------------------------------------------------

def test_direct_insert_without_audit_row_blocked(tmp_path):
    """字段齐全但无审计行绑定 -> 拒绝；排除不落字、
    统计构成不变、样本行不动。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_exclusion(pool, sid)
    pool.conn.rollback()
    assert _exclusion_rows(pool) == 0
    assert excluded_sample_ids(pool) == set()
    assert get_sample(pool, sid) is not None


def test_direct_insert_blank_fields_blocked(tmp_path):
    """reason / decided_by 空白直写 -> 拒绝（底层与入口同口径）。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    for kwargs in ({"reason": "   "}, {"decided_by": ""},
                   {"reason": None}, {"decided_by": None}):
        with pytest.raises(sqlite3.IntegrityError):
            _direct_insert_exclusion(pool, sid, **kwargs)
        pool.conn.rollback()
    assert _exclusion_rows(pool) == 0


def test_direct_insert_audit_mismatch_blocked(tmp_path):
    """审计行存在但与排除行不一致（理由不同 / 决定人不同）
    -> 拒绝：防「随便留一行痕再排除」。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    hid = _direct_insert_audit(pool, cid, reason="另一条理由")
    pool.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_exclusion(pool, sid, audit_history_id=hid)
    pool.conn.rollback()
    assert _exclusion_rows(pool) == 0

    pool.conn.execute(
        "DELETE FROM candidate_history WHERE field='sample_exclusion'")
    hid = _direct_insert_audit(pool, cid, actor="someone_else")
    pool.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_exclusion(pool, sid, audit_history_id=hid)
    pool.conn.rollback()
    assert _exclusion_rows(pool) == 0
    assert excluded_sample_ids(pool) == set()


# --- 底层正对照 ---------------------------------------------------------

def test_direct_insert_with_matching_audit_row_passes(tmp_path):
    """DB 层正对照：逐字对应的审计行先行落字、排除行显式
    绑定其 id 后，排除 INSERT 通过——闸的放行依据是被
    绑定的审计留痕本身。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    hid = _direct_insert_audit(pool, cid)
    _direct_insert_exclusion(pool, sid, audit_history_id=hid)
    pool.conn.commit()
    assert _exclusion_rows(pool) == 1
    assert excluded_sample_ids(pool) == {sid}


# --- 正常路径（正式入口天然通过） ----------------------------------------

def test_exclude_sample_normal_path_passes_db_guard(tmp_path):
    """exclude_sample（先审计行后排除行、同事务）在底层闸
    安装后仍天然通过，两行齐全且统计口径生效。"""
    pool, cid, sid = _pool_with_sample(tmp_path)
    xid = exclude_sample(pool, sid, "forward_stats", REASON,
                         decided_by=DECIDER)
    assert xid is not None
    assert _exclusion_rows(pool) == 1
    assert _audit_rows(pool) == 1
    assert excluded_sample_ids(pool) == {sid}
