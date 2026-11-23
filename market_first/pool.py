"""Unified Candidate Pool：统一候选池。

以 (market, symbol) 归并候选；多来源/多次发现历史不覆盖——
每次 ingest 都追加一行 discovery_events 与一条 candidate_history。
candidate_history 为 append-only（本模块只 INSERT、从不 UPDATE/DELETE）。
"""
from __future__ import annotations

import json
import uuid
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
    def __init__(self, db_path=None, clock=None):
        """clock：可信测试时钟夹具（P0-5，2026-11-23）。

        缺省 None -> ingest 落字时刻取真实服务器时间
        _now_iso()，生产行为不变。测试可注入 clock（无参
        callable，返回 ISO 时刻字符串）或在 ingest 前赋值
        pool.clock，以应用层路径模拟「发现即入库」等入库
        时刻——替代旧测试直写 UPDATE discovery_events SET
        created_at 的手法（该 UPDATE 已被 DB 触发器禁止，
        见 db.ensure_redteam_schema）。夹具落字经 ingest
        受控通道（方案 A 通道标记同事务绑定），构造不变。
        """
        self.db_path = init_db(db_path)
        self.conn = connect(self.db_path)
        self.clock = clock

    # -- 写入 -----------------------------------------------------------
    def ingest(self, event, actor="discovery"):
        """摄入一个发现事件，返回 (candidate_id, created_bool)。

        受控通道（P0-3 方案 A，2026-11-23）：事件行落字前，
        在同一事务内先写一条 ingest_channel_marks 通道标记
        （mark=uuid4，标记行 created_at 为真实服务器时间，
        不随夹具时钟），事件行以 ingest_mark_id 绑定该标
        记。DB 触发器 trg_rt_discovery_events_insert_channel
        据此区分受控写序与直写 INSERT：无有效标记的事件行
        created_at 必须为服务器当前时间，NULL/历史回填被
        拒；经本通道落字的历史时刻（夹具模拟）与 NULL
        （缺锚前提）带有效标记、合法通过。
        """
        now = self.clock() if self.clock else _now_iso()
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
        mark_cur = self.conn.execute(
            "INSERT INTO ingest_channel_marks (candidate_id, mark,"
            " created_at) VALUES (?, ?, ?)",
            (candidate_id, uuid.uuid4().hex, _now_iso()),
        )
        mark_id = mark_cur.lastrowid
        self.conn.execute(
            "INSERT INTO discovery_events (candidate_id, source_id,"
            " market, symbol, discovered_at, trigger_condition,"
            " price_at_discovery, price_data_time, evidence_json,"
            " created_at, ingest_mark_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, event.source_id, event.market, event.symbol,
             event.discovered_at, event.trigger_condition,
             event.price_at_discovery, event.price_data_time,
             json.dumps(event.evidence or {}, ensure_ascii=False),
             now, mark_id),
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
        """更新白名单字段；每个实际变化的字段写一条 history。

        MF-PROMO-001 封口（2026-11-23 红队集中修复）：本通用
        写原语禁止把 promotion_status 改 'approved'，无条件
        拒绝——approved 只能经 promotion.decide 正式审批
        （带 approver 与 basis、落 approvals 记录）进入；
        promotion 状态机的合法流转走专用写通道
        _transition_promotion_status，不经本函数。
        """
        bad = set(fields) - _ALLOWED_FIELDS
        if bad:
            raise ValueError(
                "不允许修改的字段：%s（仅允许 %s）"
                % (sorted(bad), sorted(_ALLOWED_FIELDS))
            )
        if fields.get("promotion_status") == "approved":
            raise ValueError(
                "promotion_status 不得经 set_fields 直改 approved："
                "只能经 promotion.decide 审批进入（MF-PROMO-001）")
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

    def _transition_promotion_status(self, cid, actor, reason, new_status):
        """promotion 状态机专用写通道（仅供 market_first.promotion
        的 _apply_transition 调用，调用方已完成合法性判定）。

        与 set_fields 的区别：本通道是 promotion_status 流转的
        唯一字段级写入口（MF-PROMO-001）；set_fields 对
        promotion_status='approved' 无条件拒绝。写字段的同时
        写一条 candidate_history（与 set_fields 同口径）。
        """
        current = self.get_by_id(cid)
        if current is None:
            raise ValueError("候选不存在：id=%s" % (cid,))
        old_val = current.get("promotion_status")
        if old_val == new_status:
            return
        now = _now_iso()
        self.conn.execute(
            "UPDATE candidates SET promotion_status=?, updated_at=?"
            " WHERE id=?",
            (new_status, now, cid),
        )
        self.conn.execute(
            "INSERT INTO candidate_history (candidate_id, field,"
            " old_value, new_value, actor, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid, "promotion_status",
             None if old_val is None else str(old_val),
             str(new_status), actor, reason, now),
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
