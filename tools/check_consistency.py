#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统一致性检查（只读）。同步/部署前跑；有 error 退出码为 1。

  python3 tools/check_consistency.py [db路径] [--config config/strategy.yaml] [--strict] [--json]

error：账本分叉/负持仓、现金重放不符、NAV≠现金+持仓、OHLC 非法、成交量单位切换、表缺失、完整性失败、
       行情日期落后、daily_report 缺最新日
warn ：成交量偶发异常、历史成交无法追溯到信号(legacy)、无持仓的待执行卖出、个别标的行情陈旧、加密日报滞后
--strict：warn 也算失败
"""
import json
import os
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REQUIRED = ["market_data", "signals", "fills", "positions", "daily_report", "approvals"]


def _add(res, sev, code, msg):
    res.append({"severity": sev, "code": code, "msg": msg})


def run_checks(db_path, cfg_path=None):
    import yaml
    from engine import data_gate
    res = []
    con = sqlite3.connect(db_path)
    q = lambda s, *a: con.execute(s, a).fetchall()
    cfg_path = cfg_path or os.path.join(ROOT, "config", "strategy.yaml")
    cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
    try:
        tables = {r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in REQUIRED:
            if t not in tables:
                _add(res, "error", "missing_table", f"缺少表 {t}")
        if any(r["code"] == "missing_table" for r in res):
            return res
        ic = q("PRAGMA integrity_check")[0][0]
        if ic != "ok":
            _add(res, "error", "integrity", f"SQLite 完整性检查失败：{ic}")

        # 1 持仓 == fills 重放；无负持仓
        rep = {}
        for a, s, side, qty in q("SELECT account,symbol,side,qty FROM fills ORDER BY date,id"):
            rep[(a, s)] = rep.get((a, s), 0.0) + (qty if side == "BUY" else -qty)
        pos = {(a, s): qt for a, s, qt in q("SELECT account,symbol,qty FROM positions")}
        for k in sorted(set(rep) | set(pos)):
            if abs(rep.get(k, 0) - pos.get(k, 0)) > 1e-6:
                _add(res, "error", "ledger_fork", f"{k}: fills重放={rep.get(k, 0):.4f} ≠ positions={pos.get(k, 0):.4f}")
            if rep.get(k, 0) < -1e-9:
                _add(res, "error", "negative_position", f"{k}: 卖出超过买入")

        # 2 现金重放（本金 + 成交现金流）== 最新日报现金
        for acc in cfg.get("accounts", []):
            i, principal = acc["id"], float(acc["principal"])
            cash = principal + sum((-(qty * p + fee) if side == "BUY" else qty * p - fee)
                                   for qty, p, fee, side in q("SELECT qty,price,fee,side FROM fills WHERE account=?", i))
            last = q("SELECT date,cash FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", i)
            if not last:
                _add(res, "error", "no_report", f"{i} 没有任何账户日报")
                continue
            if abs(cash - last[0][1]) > 0.05:
                _add(res, "error", "cash_replay", f"{i} 现金重放 {cash:,.2f} ≠ 日报 {last[0][1]:,.2f}（{last[0][0]}）")

        # 3 NAV == 现金 + 持仓市值（容忍四舍五入 0.05）
        bad = [r for r in q("SELECT date,account,nav,cash,position_value FROM daily_report WHERE symbol='TOTAL'")
               if abs(r[2] - r[3] - r[4]) > 0.05]
        if bad:
            _add(res, "error", "nav_equation", f"{len(bad)} 条日报 NAV≠现金+持仓，例：{bad[0]}")

        # 4 新鲜度
        asof = q("SELECT MAX(date) FROM market_data WHERE market IN ('a','hk','index')")[0][0]
        today = datetime.now().strftime("%Y-%m-%d")
        if asof and (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(asof, "%Y-%m-%d")).days > 4:
            _add(res, "error", "market_stale", f"行情最新日 {asof}，距今超过 4 天")
        for sym, mx, mk in q("SELECT symbol,MAX(date),MIN(market) FROM market_data GROUP BY symbol"):
            if asof and mk in ("a", "hk") and (datetime.strptime(asof, "%Y-%m-%d") - datetime.strptime(mx, "%Y-%m-%d")).days >= 2:
                _add(res, "warn", "symbol_stale", f"{sym} 行情停在 {mx}（其他到 {asof}）")
        rep_max = {a: d for a, d in q("SELECT account,MAX(date) FROM daily_report WHERE symbol='TOTAL' GROUP BY account")}
        top = max(rep_max.values()) if rep_max else None
        for a, d in rep_max.items():
            if top and (datetime.strptime(top, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days >= 2:
                _add(res, "warn", "report_lag", f"{a} 日报滞后：{d}（最新 {top}）")

        # 5 行情闸门
        for i in data_gate.check_db(con):
            sev = "error" if i["severity"] == "error" else "warn"
            _add(res, sev, i["rule"], f"{i['symbol']} {i['date']}：{i['detail']}")

        # 6 信号与成交
        if "signal_log" in tables:
            n = q("SELECT COUNT(*) FROM fills f WHERE f.signal_date IS NOT NULL AND f.id>COALESCE((SELECT v FROM meta_kv WHERE k='signal_log_since_fill_id'),0)"
                  " AND NOT EXISTS(SELECT 1 FROM signal_log s WHERE s.account=f.account AND s.symbol=f.symbol AND s.side=f.side AND s.date=f.signal_date)")[0][0] \
                if "meta_kv" in tables else 0
            if n:
                _add(res, "error", "fill_without_signal", f"{n} 笔迁移后成交在 signal_log 中找不到信号")
        legacy = q("SELECT COUNT(*) FROM fills f WHERE NOT EXISTS(SELECT 1 FROM "
                   f"{'signal_log' if 'signal_log' in tables else 'signals'} s WHERE s.account=f.account AND s.symbol=f.symbol AND s.side=f.side AND s.date=f.signal_date)")[0][0]
        if legacy:
            _add(res, "warn", "legacy_unlinked_fills", f"{legacy} 笔历史成交无法追溯到信号（signals 表被重算覆盖）")
        for d, a, s in q("SELECT d.date,d.account,d.symbol FROM signals d WHERE d.side='SELL' AND d.status='待执行' "
                         "AND NOT EXISTS(SELECT 1 FROM positions p WHERE p.account=d.account AND p.symbol=d.symbol AND p.qty>0)"):
            _add(res, "warn", "orphan_sell", f"{a}/{s} {d} 待执行卖出，但无持仓")
    finally:
        con.close()
    return res


def main(argv):
    strict, as_json = "--strict" in argv, "--json" in argv
    args = [a for a in argv if not a.startswith("--")]
    cfg = argv[argv.index("--config") + 1] if "--config" in argv else None
    if cfg in args:
        args.remove(cfg)
    db = args[0] if args else os.path.join(ROOT, "db", "sim.db")
    res = run_checks(db, cfg)
    errs = [r for r in res if r["severity"] == "error" or (strict and r["severity"] == "warn")]
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        for r in res:
            print(("❌" if r["severity"] == "error" else "⚠️ "), f"[{r['code']}]", r["msg"])
        print(f"\n{'❌ 未通过' if errs else '✅ 通过'}：error {sum(r['severity']=='error' for r in res)}，warn {sum(r['severity']=='warn' for r in res)}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
