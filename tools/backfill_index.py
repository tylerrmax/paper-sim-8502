#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上证指数日线回填（市场环境闸，第 12 项）。

把上证指数（sh000001）日线写入 db/sim.db 的 market_data，
symbol=IDX000001、market='index'。隔离保证：
- display-only 数据：IDX000001 永不进 strategy.yaml 的任何 universe，
  不产生信号/成交，仅供 engine.regime 的环境判定与页面展示；
- 只写 market_data，INSERT OR REPLACE 幂等，可重跑；
- 行情源 data.quotes.fetch_index_kline（腾讯 bfq 主源，与个股同接口，
  新浪备），与母板 B31“上证站上 20 日线”同标的同口径。

用法: python3 tools/backfill_index.py [--start YYYY-MM-DD] [--db PATH]
"""
import argparse
import os
import sqlite3
import sys
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from data.quotes import fetch_index_kline  # noqa: E402
from engine.regime import INDEX_SYMBOL  # noqa: E402

DB = os.path.join(BASE, "db", "sim.db")
DEFAULT_START = date(2026, 1, 1)  # 至 2026-10-23 约 193 根，≥120 根要求


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START.isoformat())
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()

    rows = fetch_index_kline(start)
    print("fetched %d bars (%s ~ %s)" % (len(rows), rows[0][0] if rows else "-",
                                         rows[-1][0] if rows else "-"))
    if not rows:
        print("WARN 无数据，未写入")
        return
    con = sqlite3.connect(args.db)
    con.executemany(
        "INSERT OR REPLACE INTO market_data(symbol,date,open,high,low,close,volume,market)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(INDEX_SYMBOL, d.isoformat(), o, h, l, c, v, "index")
         for d, o, h, l, c, v in rows])
    con.commit()
    n, first, last = con.execute(
        "SELECT COUNT(*), MIN(date), MAX(date) FROM market_data WHERE symbol=?",
        (INDEX_SYMBOL,)).fetchone()
    closes = [r[0] for r in con.execute(
        "SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 20",
        (INDEX_SYMBOL,)).fetchall()]
    con.close()
    ma20 = sum(closes) / len(closes) if closes else None
    print("market_data[%s]: %d 根（%s ~ %s），最新收盘 %.2f，MA20 %.2f → %s"
          % (INDEX_SYMBOL, n, first, last, closes[0] if closes else -1,
             ma20 or -1,
             "站上（可进攻）" if closes and ma20 and closes[0] > ma20 else "未站上（只观察）"))


if __name__ == "__main__":
    main()
