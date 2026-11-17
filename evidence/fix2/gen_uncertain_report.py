#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix2-2 证据生成：在独立演示库中构造同时含 confirmed 与
uncertain 两类事件的候选，经真实 ResearchEngine 产出研究
报告，把 research_reports 行的 factors/evidence 原文导出为
audit_focus_uncertain_report.json 供逐字核查。

场景（同一候选 002623 亚玛顿）：
- 2026-11-13 事件对：带 trade_id=T001，live/stored 双路
  -> confirmed duplicate，只计一次（inst_net=100）。
- 2026-11-12 事件对：无 trade_id、全元组相同、来源路径
  不同 -> uncertain，两条都保留计入（inst_net=40 x2）。
期望 total_inst_net = 100 + 40 + 40 = 180。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from market_first.models import DiscoveryEvent  # noqa: E402
from market_first.pool import CandidatePool  # noqa: E402
from market_first.research import InstFlowModule, ResearchEngine  # noqa: E402

BASE = os.path.join(os.path.dirname(__file__), "..", "..")
DB = os.path.join(BASE, "market_first", "market_first_fix2_demo.db")
OUT = os.path.join(os.path.dirname(__file__),
                   "audit_focus_uncertain_report.json")

if os.path.exists(DB):
    os.remove(DB)
pool = CandidatePool(db_path=DB)

CONFIRMED = {
    "data_date": "2026-11-13", "trade_id": "T001",
    "inst_buy": 500.0, "inst_sell": 400.0, "inst_net": 100.0,
    "seat_buy_records": 3, "seat_sell_records": 2,
}
UNCERTAIN = {
    "data_date": "2026-11-12",
    "inst_buy": 240.0, "inst_sell": 200.0, "inst_net": 40.0,
    "seat_buy_records": 2, "seat_sell_records": 1,
}

cid = None
for source, evidence, when in (
        ("inst_lhb_live", CONFIRMED, "2026-11-13T18:01:00+08:00"),
        ("inst_lhb_stored", CONFIRMED, "2026-11-16T09:00:00+08:00"),
        ("inst_lhb_live", UNCERTAIN, "2026-11-12T18:01:00+08:00"),
        ("inst_lhb_stored", UNCERTAIN, "2026-11-16T09:05:00+08:00")):
    ev = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id=source, discovered_at=when,
        trigger_condition="inst_net>0", evidence=dict(evidence))
    cid, _ = pool.ingest(ev)

engine = ResearchEngine(pool, modules=[InstFlowModule()])
report_id = engine.run(cid, bars=[], data_asof="2026-11-16",
                       data_version="fix2-demo-1")

row = pool.conn.execute(
    "SELECT * FROM research_reports WHERE id = ?",
    (report_id,)).fetchone()
out = {
    "scenario": ("confirmed pair (trade_id=T001, live+stored) + "
                 "uncertain pair (no trade_id, identical tuple, "
                 "live+stored) on one candidate"),
    "expected_total_inst_net": 180.0,
    "report_row": {
        "id": row["id"], "candidate_id": row["candidate_id"],
        "engine_version": row["engine_version"],
        "computed_at": row["computed_at"],
        "total_score": row["total_score"],
        "factors": json.loads(row["factors_json"]),
        "evidence": json.loads(row["evidence_json"]),
    },
}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("wrote", OUT)
print(json.dumps(out["report_row"]["evidence"], ensure_ascii=False,
                 indent=2))
