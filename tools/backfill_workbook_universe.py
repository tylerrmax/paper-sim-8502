#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
母板 Watchlist 选股池日线回填（display_only 隔离）。

用途：把 config/workbook_map.json 中尚无行情的标的（母板 Watchlist 16 只
与 8502 池的缺口）按 display_only 等价方式补进 db/sim.db 的 market_data，
供选股评分使用。

隔离保证：
- 只写 market_data，不写 signals/fills/positions；
- 标的必须已登记在 strategy.yaml 的 display_only（或已在 workbook_map 中
  且不在任何账户 universe 里），否则拒绝回填——防止误把新标的带进交易宇宙。
- 行情源：data.quotes.fetch_kline（腾讯 qfq 主源，与母板同口径，新浪备）。

用法: python3 tools/backfill_workbook_universe.py [--start YYYY-MM-DD] [--symbols ...]
幂等：INSERT OR REPLACE，可重跑。
"""
import argparse
import os
import sqlite3
import sys
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import yaml  # noqa: E402

from data.quotes import fetch_kline  # noqa: E402
from engine.screen import load_map  # noqa: E402

DB = os.path.join(BASE, "db", "sim.db")
DEFAULT_START = date(2026, 5, 1)  # 约120个交易日，满足60根硬闸门+指标预热


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START.isoformat())
    ap.add_argument("--symbols", default=None, help="逗号分隔，默认 workbook 全部缺口")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()

    cfg = yaml.safe_load(open(os.path.join(BASE, "config", "strategy.yaml"),
                              encoding="utf-8"))
    universe = {s for syms in cfg.get("universe", {}).values() for s in syms}
    display_only = set(cfg.get("display_only", []))
    wmap = load_map()

    con = sqlite3.connect(args.db)
    have = {r[0]: r[1] for r in con.execute(
        "SELECT symbol, COUNT(*) FROM market_data GROUP BY symbol")}

    if args.symbols:
        targets = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        targets = [s for s in wmap["symbols"] if have.get(s, 0) < 60]

    for sym in targets:
        if sym in universe and have.get(sym, 0) >= 60:
            print("SKIP %s：已在交易宇宙且数据充足，不动" % sym)
            continue
        if sym not in display_only and sym not in universe:
            print("REFUSE %s：未登记 display_only，拒绝回填（防误入交易宇宙）" % sym)
            continue
        print("Fetching %s from %s ..." % (sym, start))
        rows = fetch_kline(sym, start)
        print("  got %d bars" % len(rows))
        if not rows:
            print("  WARN %s 无数据，跳过" % sym)
            continue
        con.executemany(
            "INSERT OR REPLACE INTO market_data(symbol,date,open,high,low,close,volume,market)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [(sym, d.isoformat(), o, h, l, c, v, "a") for d, o, h, l, c, v in rows])
        con.commit()
        n, last = con.execute(
            "SELECT COUNT(*), MAX(date) FROM market_data WHERE symbol=?",
            (sym,)).fetchone()
        print("  %s 入库后共 %d 根，最新 %s" % (sym, n, last))
    con.close()
    print("Done.")


if __name__ == "__main__":
    main()
