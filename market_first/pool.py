"""Unified Candidate Pool：统一候选池。

以 (market, symbol) 归并候选；多来源/多次发现历史不覆盖——
每次 ingest 都追加一行 discovery_events 与一条 candidate_history。
candidate_history 为 append-only（本模块只 INSERT、从不 UPDATE/DELETE）。
"""
from __future__ import annotations

import json
from datetime import datetime

from market_first.db import connect, init_db

_ALLOWED_FIELDS = frozenset({
    "research_status",
    "validation_status",
    "promotion_status",
    "risk_status",
    "name",
})


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _to_dict(row):
    return dict(row) if row is not None else None


def _to_dicts(rows):
    return [dict(r) for r in rows]


class CandidatePool:
    def __init__(self, db_path=None):
        self.db_path = init_db(db_path)
        self.conn = connect(self.db_path)

    # -- 写入 -----------------------------------------------------------
    def ingest(self, event, actor="discovery"):
        """摄入一个发现事件，返回 (candidate_id, created_bool)。"""
        now = _now_iso()
        row = self.conn.execute(
            "SELECT id FROM candidates WHERE market=? AND symbol=?",
            (event.market, event.symbol),
        ).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO candidates (market, symbol, name,"
                " first_seen_at, last_seen_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event.market, event.symbol, event.name,
                 event.discovered_at, event.discovered_at, now, now),
            )
            candidate_id = cur.lastrowid
            created = True
        else:
            candidate_id = row["id"]
            created = False
            if event.name:
                self.conn.execute(
                    "UPDATE candidates SET last_seen_at=?, name=?,"
                    " updated_at=? WHERE id=?",
                    (event.discovered_at, event.name, now, candidate_id),
                )
            else:
                self.conn.execute(
                    "UPDATE candidates SET last_seen_at=?, updated_at=?"
                    " WHERE id=?",
                    (event.discovered_at, now, candidate_id),
                )
        self.conn.execute(
            "INSERT INTO discovery_events (candidate_id, source_id,"
            " market, symbol, discovered_at, trigger_condition,"
            " price_at_discovery, price_data_time, evidence_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, event.source_id, event.market, event.symbol,
             event.discovered_at, event.trigger_condition,
             event.price_at_discovery, event.price_data_time,
             json.dumps(event.evidence or {}, ensure_ascii=False), now),
        )
        self.conn.execute(
            "INSERT INTO candidate_history (candidate_id, field,"
            " old_value, new_value, actor, reason, created_at)"
            " VALUES (?, 'discovery_event', NULL, ?, ?, NULL, ?)",
            (candidate_id, event.source_id, actor, now),
        )
        self.conn.commit()
        return candidate_id, created

    def record_history(self, cid, field, old, new, actor, reason=None):
        self.conn.execute(
            "INSERT INTO candidate_history (candidate_id, field,"
            " old_value, new_value, actor, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, field, old, new, actor, reason, _now_iso()),
        )
        self.conn.commit()

    def set_fields(self, cid, actor, reason=None, **fields):
        """更新白名单字段；每个实际变化的字段写一条 history。"""
        bad = set(fields) - _ALLOWED_FIELDS
        if bad:
            raise ValueError(
                "不允许修改的字段：%s（仅允许 %s）"
                % (sorted(bad), sorted(_ALLOWED_FIELDS))
            )
        current = self.get_by_id(cid)
        if current is None:
            raise ValueError("候选不存在：id=%s" % (cid,))
        now = _now_iso()
        for fname, new_val in fields.items():
            old_val = current.get(fname)
            if old_val == new_val:
                continue
            self.conn.execute(
                "UPDATE candidates SET %s=?, updated_at=? WHERE id=?"
                % fname,
                (new_val, now, cid),
            )
            self.conn.execute(
                "INSERT INTO candidate_history (candidate_id, field,"
                " old_value, new_value, actor, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, fname,
                 None if old_val is None else str(old_val),
                 None if new_val is None else str(new_val),
                 actor, reason, now),
            )
        self.conn.commit()

    # -- 读取 -----------------------------------------------------------
    def get_by_id(self, cid):
        row = self.conn.execute(
            "SELECT * FROM candidates WHERE id=?", (cid,),
        ).fetchone()
        return _to_dict(row)

    def get(self, market, symbol):
        row = self.conn.execute(
            "SELECT * FROM candidates WHERE market=? AND symbol=?",
            (market, symbol),
        ).fetchone()
        return _to_dict(row)

    def list_candidates(self):
        rows = self.conn.execute(
            "SELECT * FROM candidates ORDER BY id ASC",
        ).fetchall()
        return _to_dicts(rows)

    def events_for(self, cid):
        rows = self.conn.execute(
            "SELECT * FROM discovery_events WHERE candidate_id=?"
            " ORDER BY id ASC",
            (cid,),
        ).fetchall()
        return _to_dicts(rows)

    def history_for(self, cid):
        rows = self.conn.execute(
            "SELECT * FROM candidate_history WHERE candidate_id=?"
            " ORDER BY id ASC",
            (cid,),
        ).fetchall()
        return _to_dicts(rows)

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
