#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待办自动隔离（8502 第二层 P4，2026-11-07 批准方案）。

背景：工作台/交易页的待办（待建条件单、待审批信号、风控拦截列表）
全部由 signals × fills × approvals 三表联查得出，人工点「跳过/隔离」
即往 approvals 写一条 decision='skipped'（键 = date/account/symbol/side，
date 为信号日，与 app.py 的 _ap_state 完全一致），各列表随即排除该条。
本模块只处理这条既有通道：把「客观已死」的待办按规则自动写 skipped，
不建新表、不改 schema、不删任何 signals/fills/blocked_signals 记录，
不碰信号生成、撮合、订单校验与风控拦截规则本身。

候选集（与界面口径一致）：无对应 fill（按 fills.signal_date 联结）、
且 approvals 无任何记录（人工已表态的一律不碰）、信号日在 asof
往前 60 天窗口内（与待审批计数窗口一致）。

规则（命中任一即隔离，优先级 R1 > R3 > R2，JSONL 里留 also_matched）：
- R1 无持仓 SELL：side=SELL，positions 现无持仓，且按 fills 计算
  信号日时点未平仓数量 <= 0。
- R3 被取代：同 (account, symbol, side) 存在更晚 signal_date 的信号，
  且其已成交或仍待处理（未被 skipped）。
- R2 过期信号：信号日距 asof > 5 个交易日（交易日以 market_data
  指数日期序列计数；无指数序列时退回 A股日历口径）且从未成交。

写入顺序（故障安全）：先把判定结果追加写审计 JSONL 并落盘，
成功后才写 approvals；JSONL 写失败则抛错、DB 一行不动，
杜绝「已隔离但无留痕」。函数幂等：已写 skipped 的条目不再是候选，
重复运行第二次写入 0 条。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG = os.path.join(BASE, "reports", "auto_skip_log.jsonl")
WINDOW_DAYS = 60
EXPIRE_TRADING_DAYS = 5


def _trading_days(con):
    """交易日序列：market_data 指数日期（数据驱动，与页面 asof 同源）。"""
    rows = con.execute(
        "SELECT DISTINCT date FROM market_data WHERE market='index'"
        " ORDER BY date").fetchall()
    return [r[0] for r in rows]


def _age_trading_days(days, signal_date, asof):
    """signal_date 之后、asof（含）之前的交易日数。"""
    if days:
        return sum(1 for d in days if signal_date < d <= asof)
    # 无指数序列时退回 A股日历口径（周末+法定假日，engine.execution）
    from engine.execution import is_stock_trading_day
    d0 = datetime.strptime(signal_date, "%Y-%m-%d").date()
    d1 = datetime.strptime(asof, "%Y-%m-%d").date()
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        if is_stock_trading_day(d):
            n += 1
    return n


def _open_qty_at(con, account, symbol, on_date):
    """fills 推算某时点未平仓数量（BUY 为正、SELL 为负，按成交日累计）。"""
    r = con.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN qty ELSE -qty END), 0)"
        " FROM fills WHERE account=? AND symbol=? AND date<=?",
        (account, symbol, on_date)).fetchone()
    return float(r[0] or 0)


def _pos_qty_now(con, account, symbol):
    r = con.execute(
        "SELECT qty FROM positions WHERE account=? AND symbol=?",
        (account, symbol)).fetchone()
    return float(r[0]) if r else 0.0


def _has_fill(con, sig):
    r = con.execute(
        "SELECT COUNT(*) FROM fills WHERE signal_date=? AND account=?"
        " AND symbol=? AND side=?",
        (sig["date"], sig["account"], sig["symbol"], sig["side"])).fetchone()
    return r[0] > 0


def evaluate(con, asof=None):
    """只判定不写入。返回 [decision, ...]，每条含 rule 与判定证据。"""
    days = _trading_days(con)
    if asof is None:
        asof = days[-1] if days else datetime.now().strftime("%Y-%m-%d")
    win_start = (datetime.strptime(asof, "%Y-%m-%d")
                 - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    cands = con.execute(
        "SELECT s.date, s.account, s.symbol, s.side, s.price FROM signals s"
        " LEFT JOIN fills f ON f.signal_date=s.date AND f.account=s.account"
        "   AND f.symbol=s.symbol AND f.side=s.side"
        " LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account"
        "   AND ap.symbol=s.symbol AND ap.side=s.side"
        " WHERE f.id IS NULL AND ap.date IS NULL AND s.date>=?"
        " ORDER BY s.date, s.account, s.symbol, s.side",
        (win_start,)).fetchall()
    # 全部信号（供 R3 查更新者，含已成交/已表态者）
    all_sigs = con.execute(
        "SELECT date, account, symbol, side FROM signals").fetchall()
    out = []
    for d, acct, sym, side, px in cands:
        sig = {"date": d, "account": acct, "symbol": sym, "side": side}
        matched, evidence = [], {}
        # R1 无持仓 SELL
        if side == "SELL":
            pos_now = _pos_qty_now(con, acct, sym)
            open_at = _open_qty_at(con, acct, sym, d)
            if pos_now <= 0 and open_at <= 0:
                matched.append("R1")
                evidence["R1"] = {"pos_qty_now": pos_now,
                                  "open_qty_at_signal_date": open_at}
        # R3 被取代
        newer = None
        for d2, a2, s2, sd2 in all_sigs:
            if (a2, s2, sd2) == (acct, sym, side) and d2 > d:
                newer = max(newer, d2) if newer else d2
        if newer is not None:
            nsig = {"date": newer, "account": acct, "symbol": sym, "side": side}
            nrow = con.execute(
                "SELECT decision FROM approvals WHERE date=? AND account=?"
                " AND symbol=? AND side=?",
                (newer, acct, sym, side)).fetchone()
            ndecision = nrow[0] if nrow else None
            if _has_fill(con, nsig) or ndecision != "skipped":
                matched.append("R3")
                evidence["R3"] = {"superseded_by": newer,
                                  "newer_state": ("filled" if _has_fill(con, nsig)
                                                  else "pending")}
        # R2 过期
        age = _age_trading_days(days, d, asof)
        if age > EXPIRE_TRADING_DAYS:
            matched.append("R2")
            evidence["R2"] = {"age_trading_days": age, "asof": asof}
        if matched:
            rule = matched[0]
            out.append({"date": d, "account": acct, "symbol": sym,
                        "side": side, "price": px, "rule": rule,
                        "also_matched": matched[1:],
                        "evidence": evidence[rule]})
    return out


def run_autoskip(db_path, asof=None, log_path=None, dry_run=False):
    """执行自动隔离，返回本次隔离的 decision 列表。

    dry_run=True 时只判定、不写 approvals、不写 JSONL（影子演算用）。
    非 dry_run 时先写 JSONL 并落盘，成功后才写 approvals（单事务）。
    """
    log_path = log_path or DEFAULT_LOG
    con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True) \
        if dry_run else sqlite3.connect(db_path)
    try:
        decisions = evaluate(con, asof=asof)
        if dry_run or not decisions:
            return decisions
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 1) 先落审计 JSONL（失败则抛错，DB 不动）
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            for dec in decisions:
                f.write(json.dumps({"ts": now, "source": "auto", **dec},
                                   ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        # 2) 再写 approvals（键法与 app.py _ap_state 完全一致）
        for dec in decisions:
            con.execute(
                "INSERT OR REPLACE INTO approvals"
                "(date,account,symbol,side,decision,updated)"
                " VALUES(?,?,?,?,?,?)",
                (dec["date"], dec["account"], dec["symbol"], dec["side"],
                 "skipped", now))
        con.commit()
        return decisions
    finally:
        con.close()
