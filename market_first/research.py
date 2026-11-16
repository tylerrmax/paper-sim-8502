#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market-First 研究层：只产证据与分项分数，不产任何交易指令。

边界（架构硬约束）：
- 研究报告（research_reports）只含 factors/total_score/evidence，
  禁止出现 side/action/order/buy/sell/instruction/qty/position_size
  等交易指令字段；落库前 assert_no_instruction_keys 递归拦截，
  命中即抛 ValueError、整份报告不写。
- 模块 compute() 为纯计算（除 ResearchEngine.run 的落库/状态更新外），
  同输入同输出；数据不足时 subtotal=None（缺失不记 0 分充数）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime

ENGINE_VERSION = "mf-research-1.0"

FORBIDDEN_KEYS = {"side", "action", "order", "buy", "sell",
                  "instruction", "qty", "position_size"}


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def assert_no_instruction_keys(obj):
    """递归检查 dict 键（小写比对），命中禁用键抛 ValueError。

    覆盖嵌套 dict 与 list/tuple 内的 dict；非 dict 键忽略。
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in FORBIDDEN_KEYS:
                raise ValueError(
                    "forbidden instruction key in research output: %r" % k)
            assert_no_instruction_keys(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            assert_no_instruction_keys(item)


def _resolve_conn(pool):
    """从 CandidatePool 解析 sqlite 连接 -> (conn, owns_conn)。

    依次尝试：pool.conn/connection/_conn/db（Connection 实例）、
    pool.db_path/path/db_file/_db_path/db（路径，经 market_first.db.connect
    优先、sqlite3.connect 兜底）。解析不到抛 AttributeError。
    """
    for attr in ("conn", "connection", "_conn", "db"):
        c = getattr(pool, attr, None)
        if isinstance(c, sqlite3.Connection):
            return c, False
    for attr in ("db_path", "path", "db_file", "_db_path", "db"):
        p = getattr(pool, attr, None)
        if p:
            try:
                from market_first.db import connect as _mf_connect
                return _mf_connect(str(p)), True
            except Exception:  # noqa: BLE001
                return sqlite3.connect(str(p)), True
    raise AttributeError(
        "cannot resolve sqlite connection from pool %r: no conn/db_path"
        % (pool,))


class TrendPatternModule:
    """趋势 + 形态模块：复用 engine.screen.pattern_factors 形态分，
    自算均线趋势分。

    trend_points 规则：
    - close > ma20 得 +5，否则 -5；
    - ma60 可用时 ma20 > ma60 得 +5，否则 -5；
    - ma60 不可用时该项 0。
    subtotal = pattern_points + trend_points。
    """

    module_id = "trend_pattern"
    model_version = "mf-trend-1.0"

    def compute(self, candidate: dict, bars, context: dict) -> dict:
        bars = bars or []
        n = len(bars)
        if n < 30:
            factors = {"available": False, "reason": "insufficient_bars",
                       "bars": n}
            return {"factors": factors, "subtotal": None,
                    "evidence": dict(factors)}

        # 延迟导入：保证 engine 包路径（worktree 根）在 sys.path 上
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from engine.screen import pattern_factors
        pattern_points, pattern_ev = pattern_factors(bars)
        pattern_flags = []
        if isinstance(pattern_ev, dict):
            pattern_flags = pattern_ev.get("flags") or []

        closes = [float(b[4]) for b in bars]
        close = closes[-1]
        ma20 = sum(closes[-20:]) / 20.0
        ma60 = sum(closes[-60:]) / 60.0 if n >= 60 else None
        close_vs_ma20_pct = (close / ma20 - 1.0) * 100.0 if ma20 else None
        ret_20d = (close / closes[-21] - 1.0) if n >= 21 else None

        trend_points = 5.0 if close > ma20 else -5.0
        if ma60 is not None:
            trend_points += 5.0 if ma20 > ma60 else -5.0
        # ma60 不可用时该项 0（不加不减），factors 里注明

        factors = {
            "available": True,
            "pattern_points": float(pattern_points),
            "pattern_flags": list(pattern_flags),
            "trend_points": float(trend_points),
            "ma20": ma20,
            "ma60": ma60,
            "ma60_available": ma60 is not None,
            "close_vs_ma20_pct": (round(close_vs_ma20_pct, 4)
                                  if close_vs_ma20_pct is not None
                                  else None),
            "ret_20d": ret_20d,
        }
        if ma60 is None:
            factors["ma60_note"] = "insufficient_bars_for_ma60"
        evidence = {"pattern": pattern_ev,
                    "bars": n,
                    "close": close,
                    "ma20": ma20,
                    "ma60": ma60}
        return {"factors": factors,
                "subtotal": float(pattern_points) + float(trend_points),
                "evidence": evidence}


DEDUP_KEY_SPEC = (
    "(market, symbol, data_date, trade_id) when trade_id is present; "
    "fallback (market, symbol, data_date, inst_buy, inst_sell, "
    "seat_buy_records, seat_sell_records) full-tuple equality when "
    "trade_id is missing"
)


def _evidence_dict(event):
    """从 event dict 解析 evidence dict（evidence_json str/dict）。

    取不到返回 {}。event 本身若直接带 inst_* 字段（手工构造的
    context 事件），由调用方另行合并。
    """
    ej = event.get("evidence_json")
    if isinstance(ej, str):
        try:
            ej = json.loads(ej)
        except (ValueError, TypeError):
            ej = None
    if isinstance(ej, dict):
        return ej
    return {}


def dedup_inst_events(events):
    """机构事件去重（第一轮审计修复 P0-2）：同一底层机构事件经
    live/stored 两条取得路径重复入账时只保留一份。

    去重键：优先 (market, symbol, data_date, trade_id)；
    trade_id 缺失时退化为 (market, symbol, data_date, inst_buy,
    inst_sell, seat_buy_records, seat_sell_records) 全等才视为
    同一事件。键不同一律保留（不同日期、不同底层事件不得误删），
    禁止按 symbol 单独去重。候选池中的原始事件不在此删除，
    本函数只决定研究聚合的计入集合。

    返回 (kept, dropped)：元素均为 (event_id, event, evidence)，
    event_id 优先 event['id']，缺失时用输入序号。
    """
    kept = []
    dropped = []
    seen = {}
    for idx, ev in enumerate(events or []):
        if not isinstance(ev, dict):
            continue
        ev_id = ev.get("id")
        if ev_id is None:
            ev_id = idx
        evidence = _evidence_dict(ev)
        data_date = (evidence.get("data_date")
                     or ev.get("price_data_time")
                     or str(ev.get("discovered_at") or "")[:10])
        trade_id = evidence.get("trade_id")
        if trade_id not in (None, ""):
            key = ("tid", ev.get("market"), ev.get("symbol"),
                   data_date, str(trade_id))
        else:
            key = ("tuple", ev.get("market"), ev.get("symbol"),
                   data_date, evidence.get("inst_buy"),
                   evidence.get("inst_sell"),
                   evidence.get("seat_buy_records"),
                   evidence.get("seat_sell_records"))
        if key in seen:
            dropped.append((ev_id, ev, evidence))
        else:
            seen[key] = ev_id
            kept.append((ev_id, ev, evidence))
    return kept, dropped


class InstFlowModule:
    """机构资金流模块：读候选的 inst* discovery events 的 evidence_json
    中的 inst_net（机构专用席位净额，口径见 engine/inst_lhb.py）。

    聚合前先经 dedup_inst_events 去重：同一底层机构事件（同
    trade_id，或缺 id 时同全元组）只贡献一次净额，live/stored
    双路不得重复计数；不同日期/不同底层事件分别保留。

    无事件 -> subtotal=None（缺失不充数）；有事件时
    subtotal = +8.0（最新 inst_net>0）/ -8.0（<0）/ 0.0（=0）。
    """

    module_id = "inst_flow"
    model_version = "mf-inst-1.0"

    @staticmethod
    def _extract_inst_net(event):
        """从单个 event dict 取 inst_net（float），取不到返回 None。

        优先 event['inst_net'] 数值列；其次 evidence_json（str 需
        json.loads，或已解析 dict）顶层/一层嵌套的 'inst_net'。
        """
        if isinstance(event.get("inst_net"), (int, float)):
            return float(event["inst_net"])
        ej = event.get("evidence_json")
        if isinstance(ej, str):
            try:
                ej = json.loads(ej)
            except (ValueError, TypeError):
                ej = None
        if isinstance(ej, dict):
            if isinstance(ej.get("inst_net"), (int, float)):
                return float(ej["inst_net"])
            for v in ej.values():
                if isinstance(v, dict) and isinstance(
                        v.get("inst_net"), (int, float)):
                    return float(v["inst_net"])
        return None

    def compute(self, candidate: dict, bars, context: dict) -> dict:
        events = (context or {}).get("inst_events") or []
        kept, dropped = dedup_inst_events(events)
        dedup_info = {
            "raw_event_ids": [
                ev.get("id") if isinstance(ev, dict)
                and ev.get("id") is not None else i
                for i, ev in enumerate(events) if isinstance(ev, dict)],
            "deduped_event_ids": [eid for eid, _, _ in kept],
            "dropped_duplicate_ids": [eid for eid, _, _ in dropped],
            "dedup_key": DEDUP_KEY_SPEC,
        }
        parsed = []
        for ev_id, ev, _evidence in kept:
            net = self._extract_inst_net(ev)
            if net is None:
                continue
            when = str(ev.get("discovered_at") or ev.get("created_at")
                       or "")
            parsed.append((when, net))
        if not parsed:
            factors = {"available": False,
                       "reason": ("no_inst_events" if not events
                                  else "inst_net_missing")}
            return {"factors": factors, "subtotal": None,
                    "evidence": {"events_count": len(events),
                                 "dedup": dedup_info}}
        latest_when, latest_net = max(parsed, key=lambda t: t[0])
        total_net = sum(net for _, net in parsed)
        if latest_net > 0:
            subtotal = 8.0
        elif latest_net < 0:
            subtotal = -8.0
        else:
            subtotal = 0.0
        factors = {"available": True,
                   "events_count": len(parsed),
                   "raw_events_count": len(dedup_info["raw_event_ids"]),
                   "latest_inst_net": latest_net,
                   "total_inst_net": total_net}
        evidence = {"events_count": len(parsed),
                    "latest_discovered_at": latest_when,
                    "latest_inst_net": latest_net,
                    "total_inst_net": total_net,
                    "dedup": dedup_info}
        return {"factors": factors, "subtotal": subtotal,
                "evidence": evidence}


class ResearchEngine:
    """研究引擎：逐模块 compute -> 组装 factors -> 落 research_reports
    一行 -> pool.set_fields(research_status='done')。返回 report_id。"""

    def __init__(self, pool, modules=None):
        self.pool = pool
        self.modules = (list(modules) if modules is not None
                        else [TrendPatternModule(), InstFlowModule()])

    def run(self, candidate_id, bars, data_asof, data_version,
            context=None) -> int:
        bars = bars or []
        context = dict(context) if context else {}
        if "inst_events" not in context:
            rows = self.pool.events_for(candidate_id) or []
            context["inst_events"] = [
                r for r in rows
                if str(r.get("source_id") or "").startswith("inst")]
        candidate = self.pool.get_by_id(candidate_id) or {}

        factors = {}
        evidence_modules = {}
        subtotals = []
        for mod in self.modules:
            out = mod.compute(candidate, bars, context)
            factors[mod.module_id] = {
                "factors": out.get("factors"),
                "subtotal": out.get("subtotal"),
                "model_version": mod.model_version,
            }
            evidence_modules[mod.module_id] = out.get("evidence")
            if out.get("subtotal") is not None:
                subtotals.append(float(out["subtotal"]))
        total_score = sum(subtotals) if subtotals else None

        # 落库前硬拦截：研究报告禁止任何交易指令字段
        assert_no_instruction_keys(factors)
        assert_no_instruction_keys(evidence_modules)

        evidence = {
            "modules": evidence_modules,
            "bars_count": len(bars),
            "bars_first_date": bars[0][0] if bars else None,
            "bars_last_date": bars[-1][0] if bars else None,
        }
        conn, owns = _resolve_conn(self.pool)
        try:
            cur = conn.execute(
                "INSERT INTO research_reports "
                "(candidate_id, engine_version, computed_at, data_asof, "
                " data_version, factors_json, total_score, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (candidate_id, ENGINE_VERSION, _now_iso(),
                 str(data_asof), str(data_version),
                 json.dumps(factors, ensure_ascii=False), total_score,
                 json.dumps(evidence, ensure_ascii=False)))
            report_id = cur.lastrowid
            conn.commit()
        finally:
            if owns:
                conn.close()
        self.pool.set_fields(candidate_id, actor="research",
                             research_status="done")
        return report_id
