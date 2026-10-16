#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账本一致性检查（只读）：验证 positions 表 == fills 汇总。
分叉时报错，用于跑批前断言，防止长期污染累计收益可信度。
2026-10-17 定位 sim_hk 分叉问题后建立。"""
import sqlite3
import sys

def check(db_path='db/sim.db'):
    con = sqlite3.connect(db_path)
    errors = []
    pos_rows = con.execute("SELECT account, symbol, qty FROM positions").fetchall()
    pos_map = {(a, s): q for a, s, q in pos_rows}
    fill_rows = con.execute("""
        SELECT account, symbol, SUM(CASE WHEN side='BUY' THEN qty ELSE -qty END)
        FROM fills GROUP BY account, symbol
    """).fetchall()
    fill_map = {(a, s): (q or 0) for a, s, q in fill_rows}
    all_keys = set(pos_map) | set(fill_map)
    for key in sorted(all_keys):
        pq = pos_map.get(key, 0)
        fq = fill_map.get(key, 0)
        if abs(pq - fq) > 1e-6:
            errors.append(f"分叉 {key}: positions={pq:.1f} vs fills净值={fq:.1f} (差 {pq-fq:.1f})")
    for (a, s), q in fill_map.items():
        if q < -1e-9:
            errors.append(f"负持仓 {a}/{s}: fills净值={q:.1f}（卖出超买入）")
    con.close()
    return errors

if __name__ == '__main__':
    errs = check(sys.argv[1] if len(sys.argv) > 1 else 'db/sim.db')
    if errs:
        print("❌ 账本不一致：")
        for e in errs:
            print(f"  - {e}")
        sys.exit(1)
    print("✅ 账本一致")
