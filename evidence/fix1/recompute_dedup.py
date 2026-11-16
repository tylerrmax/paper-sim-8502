#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立复算（不依赖 market_first.research）：直读演示库
discovery_events，按审计口径手工去重并汇总 inst_net，与
research_reports 中 inst_flow 的 factors/evidence 对比。

去重口径（与修复指令一致）：
- 键优先 (market, symbol, evidence.data_date, evidence.trade_id)
- trade_id 缺失退化为 (market, symbol, data_date, inst_buy,
  inst_sell, seat_buy_records, seat_sell_records) 全等
"""
import json
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else (
    "/home/hatch/workspace/l2-remediation/market-first/"
    "market_first/market_first_e2e.db")

conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
conn.row_factory = sqlite3.Row

cands = conn.execute(
    "SELECT DISTINCT candidate_id FROM discovery_events").fetchall()
ok = True
for (cid,) in [(r["candidate_id"],) for r in cands]:
    events = conn.execute(
        "SELECT * FROM discovery_events WHERE candidate_id=?"
        " ORDER BY id", (cid,)).fetchall()
    seen = {}
    kept_ids, dropped_ids = [], []
    total = 0.0
    for ev in events:
        ej = json.loads(ev["evidence_json"] or "{}")
        data_date = ej.get("data_date") or ev["price_data_time"]
        tid = ej.get("trade_id")
        if tid not in (None, ""):
            key = ("tid", ev["market"], ev["symbol"], data_date,
                   str(tid))
        else:
            key = ("tuple", ev["market"], ev["symbol"], data_date,
                   ej.get("inst_buy"), ej.get("inst_sell"),
                   ej.get("seat_buy_records"),
                   ej.get("seat_sell_records"))
        if key in seen:
            dropped_ids.append(ev["id"])
            continue
        seen[key] = ev["id"]
        kept_ids.append(ev["id"])
        net = ej.get("inst_net")
        if isinstance(net, (int, float)):
            total += float(net)
    rep = conn.execute(
        "SELECT factors_json, evidence_json FROM research_reports"
        " WHERE candidate_id=? ORDER BY id DESC LIMIT 1",
        (cid,)).fetchone()
    print("candidate_id=%s raw=%d kept=%s dropped=%s "
          "independent_total_inst_net=%s"
          % (cid, len(events), kept_ids, dropped_ids, total))
    if rep is None:
        print("  (no research report for this candidate; skip compare)")
        continue
    factors = json.loads(rep["factors_json"])["inst_flow"]["factors"]
    ev_mod = json.loads(rep["evidence_json"])["modules"]["inst_flow"]
    match = (factors.get("total_inst_net") == total
             and factors.get("events_count") == len(kept_ids)
             and ev_mod.get("dedup", {}).get("deduped_event_ids")
             == kept_ids
             and ev_mod.get("dedup", {}).get("dropped_duplicate_ids")
             == dropped_ids)
    print("  report total_inst_net=%s events_count=%s "
          "MATCH=%s" % (factors.get("total_inst_net"),
                        factors.get("events_count"), match))
    ok = ok and match
conn.close()
print("RECOMPUTE RESULT:", "MATCH" if ok else "MISMATCH")
sys.exit(0 if ok else 1)
