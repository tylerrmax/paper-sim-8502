#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立复算 v2（不导入 market_first.research）：直读指定库的
discovery_events，按 mf-dedup-2.0 三态口径独立分类并汇总
inst_net，与该库最新 research_reports 的 inst_flow
factors/evidence 逐项对比，输出 MATCH/MISMATCH。

三态口径（与补正指令一致，独立实现）：
- 有 trade_id 且 (market, symbol, data_date, trade_id) 相同
  -> confirmed duplicate，只计一次；
- 任一侧缺 trade_id -> 一律不合并；全元组相同的缺 id 事件
  标 uncertain、全部保留计入；
- 其余 independent 保留。
"""
import json
import sqlite3
import sys

DB = sys.argv[1]


def classify(events):
    """events: list[sqlite3.Row] -> (kept_ids, dropped_ids,
    uncertain_ids, total_inst_net)"""
    parsed = []
    for ev in events:
        ej = json.loads(ev["evidence_json"] or "{}")
        data_date = (ej.get("data_date") or ev["price_data_time"]
                     or str(ev["discovered_at"] or "")[:10])
        tid = ej.get("trade_id")
        has_id = tid not in (None, "")
        parsed.append({
            "id": ev["id"], "net": ej.get("inst_net"),
            "has_id": has_id,
            "tid_key": (ev["market"], ev["symbol"], data_date,
                        str(tid)) if has_id else None,
            "tuple_key": (ev["market"], ev["symbol"], data_date,
                          ej.get("inst_buy"), ej.get("inst_sell"),
                          ej.get("seat_buy_records"),
                          ej.get("seat_sell_records")),
            "dropped": False, "uncertain": False,
        })
    seen = set()
    for p in parsed:
        if not p["has_id"]:
            continue
        if p["tid_key"] in seen:
            p["dropped"] = True
        else:
            seen.add(p["tid_key"])
    id_tuples = {p["tuple_key"] for p in parsed
                 if p["has_id"] and not p["dropped"]}
    groups = {}
    for p in parsed:
        if not p["has_id"]:
            groups.setdefault(p["tuple_key"], []).append(p)
    for p in parsed:
        if p["has_id"]:
            continue
        if len(groups[p["tuple_key"]]) > 1 or \
                p["tuple_key"] in id_tuples:
            p["uncertain"] = True
    kept = [p for p in parsed if not p["dropped"]]
    total = sum(float(p["net"]) for p in kept
                if isinstance(p["net"], (int, float)))
    return ([p["id"] for p in kept],
            [p["id"] for p in parsed if p["dropped"]],
            [p["id"] for p in parsed if p["uncertain"]],
            total)


conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
conn.row_factory = sqlite3.Row
cands = [r["candidate_id"] for r in conn.execute(
    "SELECT DISTINCT candidate_id FROM discovery_events ORDER BY 1")]
all_ok = True
for cid in cands:
    events = conn.execute(
        "SELECT * FROM discovery_events WHERE candidate_id = ?"
        " ORDER BY id", (cid,)).fetchall()
    if not any((json.loads(e["evidence_json"] or "{}").get("inst_net")
                is not None) for e in events):
        print("candidate_id=%s: no inst events, skipped" % cid)
        continue
    kept, dropped, uncertain, total = classify(events)
    rep = conn.execute(
        "SELECT factors_json, evidence_json FROM research_reports"
        " WHERE candidate_id = ? ORDER BY id DESC LIMIT 1",
        (cid,)).fetchone()
    print("candidate_id=%s independent: kept=%s dropped=%s "
          "uncertain=%s total_inst_net=%s"
          % (cid, kept, dropped, uncertain, total))
    if rep is None:
        # 该候选在本库无研究报告（如 e2e 仅对主标的产报告）：
        # 无可比对象，记 SKIP 而非失败；独立分类结果已在上方
        # 打印，可与日后报告人工比对。
        print("  SKIP: no research report for this candidate in "
              "this DB (nothing to compare)")
        continue
    factors_all = json.loads(rep["factors_json"])
    evidence_all = json.loads(rep["evidence_json"])
    inst_factors = (factors_all.get("inst_flow") or {}).get(
        "factors") or {}
    inst_evidence = (evidence_all.get("modules") or {}).get(
        "inst_flow") or {}
    dedup = inst_evidence.get("dedup") or {}
    checks = {
        "kept_ids": (kept, dedup.get("deduped_event_ids")),
        "confirmed_dropped_ids": (
            dropped, dedup.get("confirmed_duplicate_dropped_ids")),
        "uncertain_ids": (uncertain, dedup.get("uncertain_event_ids")),
        "total_inst_net": (total, inst_factors.get("total_inst_net")),
        "dedup_version": ("mf-dedup-2.0", dedup.get("dedup_version")),
    }
    for name, (exp, got) in checks.items():
        match = (exp == got) or (
            isinstance(exp, float) and isinstance(got, (int, float))
            and abs(exp - float(got)) < 1e-9)
        print("  %-24s expected=%r report=%r -> %s"
              % (name, exp, got, "MATCH" if match else "MISMATCH"))
        all_ok = all_ok and match
print("OVERALL:", "MATCH" if all_ok else "MISMATCH")
sys.exit(0 if all_ok else 1)
