#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
录入用户券商实际成交（截图→人工录入）。
用法：
  python3 record_real_fill.py --signal-date 2026-10-09 --account sim_hk \\
      --symbol HK9660 --side BUY --real-date 2026-10-10 \\
      --real-price 4.130 --real-qty 5800 [--note 备注]
与 fills 表按 (signal_date, account, symbol, side) 关联，用于滑点统计。
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "db", "sim.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal-date", required=True, help="对应模拟信号日期 YYYY-MM-DD")
    ap.add_argument("--account", required=True, choices=["sim_a", "sim_hk", "sim_crypto"])
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--side", required=True, choices=["BUY", "SELL"])
    ap.add_argument("--real-date", required=True, help="券商实际成交日期 YYYY-MM-DD")
    ap.add_argument("--real-price", required=True, type=float, help="券商实际成交价")
    ap.add_argument("--real-qty", required=True, type=float, help="券商实际数量")
    ap.add_argument("--note", default="")
    a = ap.parse_args()

    con = sqlite3.connect(DB)
    # 分批成交：同一信号允许多条实盘记录（如分两批买入），
    # 滑点按数量加权平均。仅拦截完全相同的重复录入（防手抖双击）。
    dup = con.execute(
        "SELECT id FROM real_fills WHERE signal_date=? AND account=? AND symbol=? AND side=?"
        " AND real_date=? AND real_price=? AND real_qty=?",
        (a.signal_date, a.account, a.symbol, a.side,
         a.real_date, a.real_price, a.real_qty)).fetchone()
    if dup:
        print(f"完全相同的记录已存在 id={dup[0]}，跳过（防重复提交）")
        return
    # 关联检查：模拟侧应有对应 fill（无则警告但仍记录）
    sim = con.execute(
        "SELECT price FROM fills WHERE date=? AND account=? AND symbol=? AND side=?",
        (a.signal_date, a.account, a.symbol, a.side)).fetchone()
    if not sim:
        print(f"WARN 模拟侧无对应成交 ({a.signal_date} {a.symbol} {a.side})，仍记录实盘")
    else:
        from engine.execution import calc_slippage
        slip = calc_slippage(sim[0], a.real_price, a.side)
        print(f"模拟价 {sim[0]} → 实际价 {a.real_price}，滑点 {slip*100:+.2f}%")
    con.execute(
        "INSERT INTO real_fills(signal_date,account,symbol,side,real_date,real_price,real_qty,note,created)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (a.signal_date, a.account, a.symbol, a.side, a.real_date,
         a.real_price, a.real_qty, a.note,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    print("OK 实盘成交已登记")
    con.close()


if __name__ == "__main__":
    main()
