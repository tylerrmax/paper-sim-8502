# -*- coding: utf-8 -*-
"""P0-5 受控通道标记测试（2026-11-23，P0-3 方案 A 完整落地）。

缺口（V3 独立审计 P0-3 未实施部分）：discovery_events 直接
INSERT 的 created_at 为 NULL 或历史回填时无 DB 层校验；
以有限时间窗口拦截与可信时钟夹具结构性冲突（夹具固定字
符串的历史时刻年龄随墙钟无限增长），V3 补正批只封了未来
时间。本批按用户定案的方案 A 落地：

- pool.ingest 受控通道：与事件行同一事务内先落
  ingest_channel_marks 标记行，事件行以 ingest_mark_id
  一对一绑定；
- trg_rt_discovery_events_insert_channel：无有效标记的
  INSERT，created_at 为 NULL / 不可解析 / 早于服务器
  时间 120 秒以上 -> RAISE(ABORT)；声称标记但标记不存
  在、跨候选或已被消费 -> RAISE(ABORT)；
- 标记行 append-only（禁 UPDATE），被引用后禁 DELETE。

证明力边界（如实登记，照 P0-2 修订口径）：标记机制证明
事件行「经受控写序落字」的形态——标记存在、属同一候
选、一对一、绑定后不可改删；它不证明调用方身份，直写
者自造标记行整套伪造仍可绕过（本文件
test_forged_mark_full_set_bypass_measured 实测放行），
属 SQLite 单文件模型固有界限，待审计裁定。未来时间拒
绝由既有 trg_rt_discovery_events_created_at_insert_future
管辖（test_market_first_p0_5_insert_anchor.py 已覆盖，
本文件不重复）。
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool


def _event(discovered_at="2026-11-16T09:00:00+08:00",
           symbol="603893"):
    return DiscoveryEvent(
        market="SH", symbol=symbol, name="测试标的",
        source_id="inst_lhb_stored", discovered_at=discovered_at,
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})


def _pool_with_candidate(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    cid, _ = pool.ingest(_event())
    return pool, cid


def _direct_insert_event(pool, cid, created_at, mark_id=None,
                         symbol="603893"):
    pool.conn.execute(
        "INSERT INTO discovery_events (candidate_id, source_id,"
        " market, symbol, discovered_at, trigger_condition,"
        " price_at_discovery, price_data_time, evidence_json,"
        " created_at, ingest_mark_id) VALUES (?,"
        " 'inst_lhb_stored', 'SH', ?,"
        " '2026-11-16T09:00:00+08:00', 'inst_net>0', 10.0,"
        " '2026-11-16', '{}', ?, ?)",
        (cid, symbol, created_at, mark_id))


def _direct_insert_mark(pool, cid, mark):
    cur = pool.conn.execute(
        "INSERT INTO ingest_channel_marks (candidate_id, mark,"
        " created_at) VALUES (?, ?, ?)",
        (cid, mark, _iso(datetime.now().astimezone())))
    return cur.lastrowid


def _event_count(pool, cid):
    return pool.conn.execute(
        "SELECT COUNT(*) c FROM discovery_events"
        " WHERE candidate_id=?", (cid,)).fetchone()["c"]


def _iso(dt):
    return dt.astimezone().isoformat(timespec="seconds")


# --- 无标记直写：NULL / 回填 / 不可解析 -----------------------------

def test_direct_insert_null_created_at_without_mark_blocked(tmp_path):
    """直写 INSERT created_at=NULL 且无通道标记 -> 拒绝，
    行不落库（缺锚事件不得绕过受控通道直写产生）。"""
    pool, cid = _pool_with_candidate(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_event(pool, cid, None)
    pool.conn.rollback()
    assert _event_count(pool, cid) == 1


def test_direct_insert_backfilled_created_at_without_mark_blocked(
        tmp_path):
    """直写 INSERT created_at 为两天前（天级历史回填）且无
    通道标记 -> 拒绝，行不落库。回填量以相对偏移构造，
    与墙钟无关。"""
    pool, cid = _pool_with_candidate(tmp_path)
    backfilled = _iso(datetime.now().astimezone()
                      - timedelta(days=2))
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_event(pool, cid, backfilled)
    pool.conn.rollback()
    assert _event_count(pool, cid) == 1


def test_direct_insert_unparseable_created_at_without_mark_blocked(
        tmp_path):
    """直写 INSERT created_at 为不可解析字符串且无标记 ->
    拒绝（不可解析即无可信锚，不得落字）。"""
    pool, cid = _pool_with_candidate(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_event(pool, cid, "not-a-timestamp")
    pool.conn.rollback()
    assert _event_count(pool, cid) == 1


def test_direct_insert_recent_past_within_tolerance_passes(tmp_path):
    """正对照（范围边界）：无标记直写 created_at 为 60 秒
    前——仍属服务器当前时间口径（120 秒窗口内，与 registry
    approved_at 同口径）-> 放行。本闸拦的是 NULL 与明显
    回填，不拦当前时刻附近的落字。"""
    pool, cid = _pool_with_candidate(tmp_path)
    recent = _iso(datetime.now().astimezone()
                  - timedelta(seconds=60))
    _direct_insert_event(pool, cid, recent)
    pool.conn.commit()
    assert _event_count(pool, cid) == 2


# --- 受控通道正常路径 -------------------------------------------------

def test_channel_ingest_binds_mark_one_to_one(tmp_path):
    """通道正对照：时钟夹具的历史时刻经 ingest 落字，事件
    行带 ingest_mark_id 绑定一条同候选标记行；标记行
    created_at 为真实服务器时间（非夹具时刻）；第二次
    ingest 绑定另一条标记，一对一不复用。"""
    pool = CandidatePool(
        db_path=str(tmp_path / "mf.db"),
        clock=lambda: "2026-11-10T16:30:00+08:00")
    cid, _ = pool.ingest(_event("2026-09-15T09:00:00+08:00"))
    ev1 = pool.events_for(cid)[0]
    assert ev1["created_at"] == "2026-11-10T16:30:00+08:00"
    mark1 = pool.conn.execute(
        "SELECT * FROM ingest_channel_marks WHERE id=?",
        (ev1["ingest_mark_id"],)).fetchone()
    assert mark1 is not None
    assert mark1["candidate_id"] == cid
    stamped = datetime.fromisoformat(mark1["created_at"])
    assert abs(datetime.now().astimezone().timestamp()
               - stamped.timestamp()) < 300
    pool.clock = lambda: "2026-11-13T16:30:00+08:00"
    pool.ingest(_event("2026-09-16T09:00:00+08:00"))
    ev2 = pool.events_for(cid)[1]
    assert ev2["ingest_mark_id"] != ev1["ingest_mark_id"]
    n = pool.conn.execute(
        "SELECT COUNT(*) c FROM ingest_channel_marks").fetchone()["c"]
    assert n == 2


def test_channel_ingest_null_created_at_premise_constructible(
        tmp_path):
    """缺锚前提经通道可构造：clock 返回 None 时 ingest 落
    created_at=NULL 的事件行（带有效标记），行存在且绑定
    成立——test_missing_ingest_time_rejects_forward 的被测
    前提在本闸下继续成立（该用例本身未改动）。"""
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    pool.clock = lambda: None
    cid, _ = pool.ingest(_event())
    row = pool.events_for(cid)[0]
    assert row["created_at"] is None
    assert row["ingest_mark_id"] is not None
    mark = pool.conn.execute(
        "SELECT * FROM ingest_channel_marks WHERE id=?",
        (row["ingest_mark_id"],)).fetchone()
    assert mark["candidate_id"] == cid


# --- 标记引用对抗：复用 / 悬空 / 跨候选 --------------------------------

def test_consumed_mark_reuse_blocked(tmp_path):
    """复用对抗：直写 INSERT 引用已被合法事件消费的标记
    id（created_at 取当前时间，隔离一对一规则本身）->
    拒绝，一对一绑定不可绕过。"""
    pool, cid = _pool_with_candidate(tmp_path)
    used_mark = pool.events_for(cid)[0]["ingest_mark_id"]
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_event(
            pool, cid, _iso(datetime.now().astimezone()),
            mark_id=used_mark)
    pool.conn.rollback()
    assert _event_count(pool, cid) == 1


def test_dangling_mark_reference_blocked(tmp_path):
    """悬空引用对抗：直写 INSERT 声称不存在的标记 id ->
    拒绝（声称通道标记就必须真实存在且有效）。"""
    pool, cid = _pool_with_candidate(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_event(
            pool, cid, _iso(datetime.now().astimezone()),
            mark_id=999999)
    pool.conn.rollback()
    assert _event_count(pool, cid) == 1


def test_cross_candidate_mark_reference_blocked(tmp_path):
    """跨候选对抗：候选 A 的未消费标记被候选 B 的回填事件
    引用 -> 拒绝（标记只证明其所属候选的通道落字）。"""
    pool, cid_a = _pool_with_candidate(tmp_path)
    ev_b = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id="inst_lhb_live",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 1.0})
    cid_b, _ = pool.ingest(ev_b)
    mark_a = _direct_insert_mark(pool, cid_a, "mark-for-a-only")
    pool.conn.commit()
    backfilled = _iso(datetime.now().astimezone()
                      - timedelta(days=2))
    with pytest.raises(sqlite3.IntegrityError):
        _direct_insert_event(pool, cid_b, backfilled,
                             mark_id=mark_a, symbol="002623")
    pool.conn.rollback()
    assert _event_count(pool, cid_b) == 1


# --- 标记伪造（整套一致伪造）实测与标记不可变性 -------------------------

def test_forged_mark_full_set_bypass_measured(tmp_path):
    """伪造实测（如实登记）：直写者自造一条标记行、再按其
    id 写回填事件行——整套一致伪造在 DB 层放行，本机制不
    证明调用方身份（与 RT-1/RT-2 同级固有界限）。但伪造
    标记落字后即被绑定锁定：UPDATE 与 DELETE 均被拒，
    伪造行为本身留下不可删除的证据行。"""
    pool, cid = _pool_with_candidate(tmp_path)
    forged_mark = _direct_insert_mark(pool, cid, "forged-mark-1")
    backfilled = _iso(datetime.now().astimezone()
                      - timedelta(days=30))
    _direct_insert_event(pool, cid, backfilled,
                         mark_id=forged_mark)
    pool.conn.commit()
    assert _event_count(pool, cid) == 2
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE ingest_channel_marks SET mark='x'"
            " WHERE id=?", (forged_mark,))
    pool.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "DELETE FROM ingest_channel_marks WHERE id=?",
            (forged_mark,))
    pool.conn.rollback()
    row = pool.conn.execute(
        "SELECT * FROM ingest_channel_marks WHERE id=?",
        (forged_mark,)).fetchone()
    assert row["mark"] == "forged-mark-1"


def test_bound_mark_update_and_delete_blocked(tmp_path):
    """合法通道标记的不可变性：ingest 绑定的标记行 UPDATE
    （改 mark 值）与 DELETE 均被拒，标记行逐字不变。"""
    pool, cid = _pool_with_candidate(tmp_path)
    mark_id = pool.events_for(cid)[0]["ingest_mark_id"]
    before = dict(pool.conn.execute(
        "SELECT * FROM ingest_channel_marks WHERE id=?",
        (mark_id,)).fetchone())
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "UPDATE ingest_channel_marks SET mark='tampered'"
            " WHERE id=?", (mark_id,))
    pool.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        pool.conn.execute(
            "DELETE FROM ingest_channel_marks WHERE id=?",
            (mark_id,))
    pool.conn.rollback()
    after = dict(pool.conn.execute(
        "SELECT * FROM ingest_channel_marks WHERE id=?",
        (mark_id,)).fetchone())
    assert after == before


def test_unbound_mark_delete_allowed_scope_boundary(tmp_path):
    """范围边界正对照：从未被任何事件引用的标记行可删
    除——它不构成任何事件的通道证据，删除不改变任何已
    落字事件的可验证性；闸只锁定已绑定标记。"""
    pool, cid = _pool_with_candidate(tmp_path)
    orphan = _direct_insert_mark(pool, cid, "orphan-mark")
    pool.conn.commit()
    pool.conn.execute(
        "DELETE FROM ingest_channel_marks WHERE id=?",
        (orphan,))
    pool.conn.commit()
    n = pool.conn.execute(
        "SELECT COUNT(*) c FROM ingest_channel_marks"
        " WHERE id=?", (orphan,)).fetchone()["c"]
    assert n == 0
