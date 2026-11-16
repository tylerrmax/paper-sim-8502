"""Trading Handoff（模拟交接）：已批准候选 -> 模拟交接载荷。

硬边界：只写新库 handoffs 表且 status 恒为 'simulated'；
绝不写生产 approvals / signals / trade_intents，绝不生成
真实交易意图。未获人工批准（promotion_status != 'approved'
或无 approved 的 approvals 行）一律抛 HandoffNotApprovedError。
"""
from __future__ import annotations

import json
from datetime import date, datetime

from market_first import db as mf_db

SIMULATED_NOTE = (
    "模拟交接：未写入生产 approvals/signals/trade_intents，"
    "未生成真实交易意图"
)


class HandoffNotApprovedError(Exception):
    """候选未获人工批准，模拟交接被拒。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _acquire_conn(pool):
    """返回 (conn, owned)：优先复用 pool 的连接；pool 未暴露连接
    时用 db.connect(pool.db_path) 新开（db_path 属性名兼容 getattr）。"""
    conn = getattr(pool, "conn", None)
    if conn is not None:
        return conn, False
    return mf_db.connect(getattr(pool, "db_path", None)), True


def _release(conn, owned):
    if owned and conn is not None:
        conn.close()


def _ids(conn, table, cid):
    rows = conn.execute(
        "SELECT id FROM %s WHERE candidate_id=? ORDER BY id ASC" % table,
        (cid,),
    ).fetchall()
    return [r[0] for r in rows]


def _approvals(conn, cid):
    rows = conn.execute(
        "SELECT id, decision FROM approvals WHERE candidate_id=?"
        " ORDER BY id ASC",
        (cid,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def build_payload(pool, cid) -> dict:
    """组装模拟交接载荷（含完整来源追溯链，不落库）。"""
    cand = pool.get_by_id(cid)
    if cand is None:
        raise ValueError("候选不存在：id=%s" % (cid,))
    conn, owned = _acquire_conn(pool)
    try:
        approvals = _approvals(conn, cid)
        latest_decision = approvals[-1][1] if approvals else None
        approved_ids = [aid for aid, dec in approvals if dec == "approved"]
        approval_id = approved_ids[-1] if approved_ids else None
        research_ids = _ids(conn, "research_reports", cid)
        validation_ids = _ids(conn, "validation_samples", cid)
    finally:
        _release(conn, owned)
    discovery_ids = [e["id"] for e in pool.events_for(cid)]
    return {
        "date": date.today().isoformat(),
        "account": None,
        "symbol": cand.get("symbol"),
        "market": cand.get("market"),
        "side": "BUY",
        "intent_placeholder": True,
        "decision": latest_decision,
        "source_trace": {
            "candidate_id": cid,
            "discovery_event_ids": discovery_ids,
            "research_report_ids": research_ids,
            "validation_sample_ids": validation_ids,
            "approval_id": approval_id,
        },
        "status": "simulated",
        "note": SIMULATED_NOTE,
    }


def simulate_handoff(pool, cid) -> dict:
    """模拟交接：仅 approved 候选可执行。

    promotion_status != 'approved' 或无 approved 的 approvals 行
    -> HandoffNotApprovedError。成功时写 handoffs 一行
    （status='simulated'），返回 payload 并附加 'handoff_id'。
    """
    cand = pool.get_by_id(cid)
    if cand is None:
        raise ValueError("候选不存在：id=%s" % (cid,))
    status = cand.get("promotion_status")
    conn, owned = _acquire_conn(pool)
    try:
        approvals = _approvals(conn, cid)
    finally:
        _release(conn, owned)
    approved_ids = [aid for aid, dec in approvals if dec == "approved"]
    if status != "approved" or not approved_ids:
        raise HandoffNotApprovedError(
            "模拟交接被拒：candidate_id=%s promotion_status=%s，"
            "approved 审批行=%s（须先经人工 decide 批准）"
            % (cid, status, approved_ids or "无")
        )
    payload = build_payload(pool, cid)
    approval_id = payload["source_trace"]["approval_id"]
    conn, owned = _acquire_conn(pool)
    try:
        cur = conn.execute(
            "INSERT INTO handoffs (candidate_id, approval_id,"
            " payload_json, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (cid, approval_id,
             json.dumps(payload, ensure_ascii=False),
             "simulated", _now_iso()),
        )
        handoff_id = cur.lastrowid
        conn.commit()
    finally:
        _release(conn, owned)
    payload["handoff_id"] = handoff_id
    return payload
