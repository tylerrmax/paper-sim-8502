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

# 结构迁移：schema.sql 历史版本缺失的列在此补齐（幂等）。
# 与 db/schema.sql 的 signals 定义保持一致；新库由 schema 直接建全，老库由此升级。
MIGRATIONS = {
    "signals": {
        "action_type": "TEXT DEFAULT '开仓'",
        "status": "TEXT DEFAULT '待执行'",
        "stop_price": "REAL",
        "strategy": "TEXT",
        "adx_value": "REAL",
        "range_protected": "INTEGER DEFAULT 0",
        "volume_confirmed": "INTEGER DEFAULT 0",
        "atr_multiple": "REAL",
    },
}


def migrate(con):
    """对 MIGRATIONS 中缺失的列执行 ALTER TABLE ADD COLUMN，返回补齐的列名列表。"""
    added = []
    for table, cols in MIGRATIONS.items():
        existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # 表不存在（不应发生，schema 已先执行）
        for col, ddl in cols.items():
            if col not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                added.append(f"{table}.{col}")
    return added


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
    added = migrate(con)
    if added:
        print(f"结构迁移补齐: {', '.join(added)}")
    # 控制行初始化（paused+manual 为安全默认，新环境不自动交易）
    con.execute(
        "INSERT OR IGNORE INTO control(id, status, mode, updated)"
        " VALUES(1, 'paused', 'manual', datetime('now'))")
    con.commit()

    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    sig_cols = {r[1] for r in con.execute("PRAGMA table_info(signals)")}
    con.close()
    need_cols = {"date", "account", "symbol", "side", "price", "action_type",
                 "status", "stop_price", "strategy", "adx_value",
                 "range_protected", "volume_confirmed", "atr_multiple"}
    miss_cols = need_cols - sig_cols
    if miss_cols:
        print(f"signals 缺列: {miss_cols}", file=sys.stderr)
        sys.exit(1)
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
