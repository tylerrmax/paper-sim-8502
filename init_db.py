#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新环境初始化：建表 + 控制行初始化 + 配置校验。
用法: python3 init_db.py [--db db/sim.db]
幂等，可重复执行。
"""
import argparse
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(BASE, "db", "sim.db"))
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    schema = os.path.join(BASE, "db", "schema.sql")
    if not os.path.exists(schema):
        print(f"缺 schema.sql: {schema}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(args.db)
    with open(schema) as f:
        con.executescript(f.read())
    # 控制行初始化（paused+manual 为安全默认，新环境不自动交易）
    con.execute(
        "INSERT OR IGNORE INTO control(id, status, mode, updated)"
        " VALUES(1, 'paused', 'manual', datetime('now'))")
    con.commit()

    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    con.close()
    print(f"初始化完成: {args.db}")
    print(f"表 ({len(tables)}): {', '.join(tables)}")
    # 校验关键表
    need = {"market_data", "signals", "fills", "positions", "daily_report",
            "control", "approvals", "broker_receipts", "graduation_exclusions"}
    missing = need - set(tables)
    if missing:
        print(f"缺表: {missing}", file=sys.stderr)
        sys.exit(1)
    print("关键表校验通过")

if __name__ == "__main__":
    main()
