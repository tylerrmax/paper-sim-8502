"""A股历史K线回填：为 MA20 提供指标预热数据。

只回填 2026-10-08 之前的数据，不触碰现有 live 数据。
回填数据仅用于指标计算，信号生成由 VAL_START guard 限制（见 run_daily.py）。
"""
import sqlite3
import sys
from datetime import date

sys.path.insert(0, "/home/hatch/workspace/paper-trading-sim")
from data.quotes import fetch_kline

DB = "/home/hatch/workspace/paper-trading-sim/db/sim.db"
CUTOFF = date(2026, 10, 8)  # 只回填此日期之前
START = date(2026, 7, 1)    # 回填起点，保证 60+ 交易日

SYMBOLS = ["002446", "688305"]


def main():
    con = sqlite3.connect(DB)
    for sym in SYMBOLS:
        print(f"Fetching {sym} from {START}...")
        rows = fetch_kline(sym, START)
        # 只保留 CUTOFF 之前的数据
        rows = [r for r in rows if r[0] < CUTOFF]
        print(f"  got {len(rows)} bars before {CUTOFF}")
        if not rows:
            print(f"  WARN: no data for {sym}")
            continue
        for r in rows:
            d, o, h, l, c, v = r
            con.execute(
                "INSERT OR REPLACE INTO market_data(symbol,date,open,high,low,close,volume,market)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (sym, d.isoformat(), o, h, l, c, v, "a"),
            )
        con.commit()
        print(f"  inserted {len(rows)} rows for {sym}")
    con.close()
    print("Done.")


if __name__ == "__main__":
    main()
