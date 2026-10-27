#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""恢复演练：只靠 fills + 本金 + 行情，独立重建 positions 与账户现金/净值，并与库内数据比对。
  python3 tools/rebuild_from_fills.py [db路径] [--write 输出db路径]
默认只读比对（不一致退出码 1）。--write 把重建的 positions 写入"另存的副本"（绝不原地覆盖），
供库内数据损坏时人工核对后替换。"""
import os
import shutil
import sqlite3
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def rebuild(con, cfg):
    """返回 ({(acct,sym): {qty,avg_cost,last,mv}}, {acct: {cash,mv,nav}})"""
    pos = {}
    cash = {a["id"]: float(a["principal"]) for a in cfg.get("accounts", [])}
    for acct, sym, side, qty, price, fee in con.execute(
            "SELECT account,symbol,side,qty,price,fee FROM fills ORDER BY date,id"):
        p = pos.setdefault((acct, sym), {"qty": 0.0, "cost": 0.0})
        if side == "BUY":
            p["cost"] += qty * price + fee
            p["qty"] += qty
            cash[acct] = cash.get(acct, 0) - (qty * price + fee)
        else:
            avg = p["cost"] / p["qty"] if p["qty"] else 0
            p["cost"] -= avg * qty
            p["qty"] -= qty
            cash[acct] = cash.get(acct, 0) + qty * price - fee
    out = {}
    for (a, s), p in pos.items():
        if p["qty"] > 1e-9:
            r = con.execute("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1", (s,)).fetchone()
            last = r[0] if r else 0.0
            out[(a, s)] = {"qty": p["qty"], "avg_cost": p["cost"] / p["qty"], "last": last, "mv": p["qty"] * last}
    accts = {}
    for a, c in cash.items():
        mv = sum(v["mv"] for (aa, _), v in out.items() if aa == a)
        accts[a] = {"cash": c, "mv": mv, "nav": c + mv}
    return out, accts


def compare(con, cfg):
    pos, accts = rebuild(con, cfg)
    diffs = []
    db = {(a, s): (q, c) for a, s, q, c in con.execute("SELECT account,symbol,qty,avg_cost FROM positions")}
    for k in sorted(set(pos) | set(db)):
        r, d = pos.get(k), db.get(k)
        if r is None or d is None or abs(r["qty"] - d[0]) > 1e-6:
            diffs.append(f"持仓 {k}: 重建={r and r['qty']} 库内={d and d[0]}")
        elif abs(r["avg_cost"] - d[1]) > max(0.01, d[1] * 0.002):
            diffs.append(f"成本 {k}: 重建={r['avg_cost']:.4f} 库内={d[1]:.4f}（口径差异，需人工确认）")
    for a, v in accts.items():
        row = con.execute("SELECT date,cash FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (a,)).fetchone()
        if row and abs(row[1] - v["cash"]) > 0.05:
            diffs.append(f"现金 {a}: 重建={v['cash']:.2f} 日报={row[1]:.2f}（{row[0]}）")
    return pos, accts, diffs


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    out_path = argv[argv.index("--write") + 1] if "--write" in argv else None
    if out_path in args:
        args.remove(out_path)
    db = args[0] if args else os.path.join(ROOT, "db", "sim.db")
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config", "strategy.yaml"), encoding="utf-8"))
    con = sqlite3.connect(db)
    pos, accts, diffs = compare(con, cfg)
    for a, v in accts.items():
        print(f"{a}: 现金 {v['cash']:,.2f}  持仓市值 {v['mv']:,.2f}  净值 {v['nav']:,.2f}")
    for d in diffs:
        print("❌", d)
    if out_path:
        if os.path.abspath(out_path) == os.path.abspath(db):
            sys.exit("--write 不允许覆盖原库")
        shutil.copy2(db, out_path)
        w = sqlite3.connect(out_path)
        w.execute("DELETE FROM positions")
        w.executemany("INSERT INTO positions(account,symbol,qty,avg_cost,last_price,market_value) VALUES (?,?,?,?,?,?)",
                      [(a, s, v["qty"], v["avg_cost"], v["last"], v["mv"]) for (a, s), v in pos.items()])
        w.commit()
        print(f"已写入重建副本：{out_path}（请人工核对后再替换）")
    print("✅ 重建与库内一致" if not diffs else f"❌ {len(diffs)} 处不一致")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
