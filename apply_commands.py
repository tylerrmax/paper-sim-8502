#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
执行从手机应用取回的 commands，写回 sim.db 并重算管线、刷新快照。
用法：echo '<pullcommands返回的commands数组JSON>' | python3 apply_commands.py
输出：{"applied_ids":[...]}
"""
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import yaml  # noqa: E402
from engine.pipeline import run, write_report  # noqa: E402

DB = os.environ.get("PAPER_SIM_DB", os.path.join(BASE, "db", "sim.db"))


def main():
    commands = json.load(sys.stdin)
    if not commands:
        print(json.dumps({"applied_ids": []}))
        return
    cfg = yaml.safe_load(open(os.path.join(BASE, "config", "strategy.yaml"), encoding="utf-8"))
    con = sqlite3.connect(DB)
    # exactly-once 台账：已处理过的指令 id 不再重放（防网页端重试/重复投递导致重复成交）
    con.execute("CREATE TABLE IF NOT EXISTS processed_commands("
                "id TEXT PRIMARY KEY, applied_at TEXT)")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seen = {r[0] for r in con.execute("SELECT id FROM processed_commands")}
    applied, duplicates = [], []
    for c in commands:
        cid = c.get("id")
        if not cid:
            print("WARN 指令缺少 id，已跳过", file=sys.stderr)
            continue
        if cid in seen:
            duplicates.append(cid)
            continue
        kind = c.get("kind")
        p = json.loads(c.get("payload_json") or "{}")
        # payload 也可能直接平铺在命令上（兼容）
        p = {**{k: v for k, v in c.items() if k not in ("id", "kind", "payload_json", "status", "ts")}, **p}
        if kind == "set_status" and p.get("status") in ("running", "paused"):
            con.execute("INSERT INTO control(id,status,mode,updated) VALUES(1,?,"
                        "COALESCE((SELECT mode FROM control WHERE id=1),'auto'),?)"
                        " ON CONFLICT(id) DO UPDATE SET status=excluded.status,updated=excluded.updated",
                        (p["status"], now))
            applied.append(cid)
        elif kind == "set_mode" and p.get("mode") in ("auto", "manual"):
            con.execute("INSERT INTO control(id,status,mode,updated) VALUES(1,"
                        "COALESCE((SELECT status FROM control WHERE id=1),'running'),?,?)"
                        " ON CONFLICT(id) DO UPDATE SET mode=excluded.mode,updated=excluded.updated",
                        (p["mode"], now))
            applied.append(cid)
        elif kind in ("approve", "skip") and all(k in p for k in ("date", "account", "symbol", "side")):
            con.execute("INSERT OR REPLACE INTO approvals(date,account,symbol,side,decision,updated)"
                        " VALUES(?,?,?,?,?,?)",
                        (p["date"], p["account"], p["symbol"], p["side"],
                         "approved" if kind == "approve" else "skipped", now))
            applied.append(cid)
    con.commit()
    con.close()
    if applied:
        # 重算管线（不拉取行情，纯重放，快）；全重复时跳过，无状态变化
        asof = run(DB, cfg)
        write_report(DB, cfg, asof)
        subprocess.run([sys.executable, os.path.join(BASE, "sync_snapshot.py")], check=True)
        # 管线重算成功后才记为已处理：若重算前崩溃，重试可安全重放（各环节幂等）
        con2 = sqlite3.connect(DB)
        for cid in applied:
            con2.execute("INSERT OR IGNORE INTO processed_commands(id, applied_at)"
                         " VALUES(?, ?)", (cid, now))
        con2.commit()
        con2.close()
    print(json.dumps({"applied_ids": applied, "duplicate_ids": duplicates}))


if __name__ == "__main__":
    main()
