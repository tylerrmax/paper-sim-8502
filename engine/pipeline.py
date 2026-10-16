#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日线模拟管线：signals / positions / daily_report 全量重算（幂等）；
fills 追加制——历史成交一旦产生即为事实，切换暂停/手动等控制状态
不得删除历史成交，只决定新信号是否成交。
（2026-10-02 修复：此前 DELETE fills 会导致切手动模式时历史成交被清空。）

执行假设（声明，见 config/strategy.yaml）：
  - 信号日收盘价成交：BUY 按 close*(1+slippage)，SELL 按 close*(1-slippage)；
  - BUY 用账户现金的 buy_frac，股票向下取整到 lot_size，加密到 min_qty；
  - SELL 全平可用仓位；A股 T+1（engine.sim 已处理）；
  - A股涨停日不执行 BUY、跌停日不执行 SELL（简化）；
  - 同一日多标的信号按 symbol 字母序执行；
  - 无数据标的自动跳过（A股休市期间 sim_a 为空盘）。
"""
from __future__ import annotations

import fcntl
import math
import os
import sqlite3

from engine.sim import SimAccount, Lot, check_limit
from engine.signals import gen_signals, gen_buyback_signals

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "db", "sim.db")
REPORT_DIR = os.path.join(BASE, "reports")

# 验证期起点（分账户）：与 sync_snapshot.py 的 VAL_START 保持一致。
# 回填的历史 K 线仅用于指标预热（MA20 计算），此日期之前的信号禁止入库，
# 避免污染验证窗口、避免 manual 模式下积压历史待审批。
VAL_START = {"sim_crypto": "2026-10-03",
             "sim_a": "2026-10-08", "sim_hk": "2026-10-08"}


def _fee_for(account_id, side, cfg):
    f = cfg["fees"]
    if account_id == "sim_a":
        return (f["a_buy"] if side == "BUY" else f["a_sell"]), 5.0
    if account_id == "sim_hk":
        return (f["hk_buy"] if side == "BUY" else f["hk_sell"]), 0.0
    return f["crypto"], 0.0


def _replay_fill(sim, s, side, qty, price, fee, d):
    """历史成交重放进 sim 状态（零漂移，不重新过门禁）。
    调用方保证按日期顺序调用，以维持 T+1 等时间相关逻辑正确。"""
    if side == "BUY":
        sim.cash -= qty * price + fee
        sim.positions.setdefault(s, []).append(Lot(qty, price, d))
    else:
        pos_lots = sim.positions.get(s, [])
        left = qty
        for lot in pos_lots:
            if left <= 1e-9:
                break
            if lot.qty <= 1e-9:
                continue
            if sim.t1 and not (lot.buy_date < d):
                continue
            take = min(lot.qty, left)
            lot.qty -= take
            left -= take
        sim.cash += qty * price - fee
        sim.positions[s] = [l for l in pos_lots if l.qty > 1e-9]


def run(db_path=DB, cfg=None):
    """日线管线入口（进程级串行）。

    并发安全：两个 run_daily / apply_commands / app 重算同时执行时，
    若不串行，后启动者的 existing_fills 快照会漏掉先启动者未提交的新成交，
    导致 fills 重复插入。fcntl 排他锁按库文件隔离（db_path + ".run.lock"），
    进程崩溃时锁随 fd 关闭自动释放，不留僵尸锁。
    同一进程内多线程调 run() 不在此锁保护范围（生产路径均为多进程）。
    """
    lock_path = db_path + ".run.lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return _run_locked(db_path, cfg)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _run_locked(db_path=DB, cfg=None):
    if cfg is None:
        import yaml
        cfg = yaml.safe_load(open(os.path.join(BASE, "config", "strategy.yaml"), encoding="utf-8"))
    con = sqlite3.connect(db_path)
    # signals/positions/daily_report 全量重算；fills 追加制：历史成交是事实，
    # 控制状态（暂停/手动）只决定新信号是否成交，不得删除历史成交。
    for t in ("signals", "positions", "daily_report"):
        con.execute("DELETE FROM %s" % t)
    existing_fills = {}
    for r in con.execute(
            "SELECT date,account,symbol,side,qty,price,fee FROM fills ORDER BY date,id"):
        existing_fills[(r[0], r[1], r[2], r[3])] = (r[4], r[5], r[6])
    # 网页版控制：paused 只记信号不成交；manual 需 approvals 表批准才成交
    row = con.execute("SELECT status, mode FROM control WHERE id=1").fetchone()
    status, mode = row if row else ("running", "auto")
    # fail-safe：control 被直接写坏（非法值）时不得 fail-open 当 running/auto；
    # 未知 status 一律视为 paused（不产生新成交），未知 mode 一律视为 manual（需审批）。
    if status not in ("running", "paused"):
        print("WARN control.status 非法值 %r，已 fail-safe 视为 paused" % (status,))
        status = "paused"
    if mode not in ("auto", "manual"):
        print("WARN control.mode 非法值 %r，已 fail-safe 视为 manual" % (mode,))
        mode = "manual"

    buy_frac = cfg.get("sizing", {}).get("buy_frac", 0.5)
    slippage = cfg["fees"].get("slippage", 0.0)
    lots = cfg.get("lots", {})
    boards = cfg.get("boards", {})
    ma_long = cfg["signals"]["ma_windows"][-1]

    asof = None
    for acct in cfg["accounts"]:
        aid, ccy = acct["id"], acct["ccy"]
        symbols = cfg["universe"].get(aid, [])
        bars = {}
        for s in symbols:
            rows = con.execute(
                "SELECT date,open,high,low,close,volume FROM market_data"
                " WHERE symbol=? ORDER BY date", (s,)).fetchall()
            if rows:
                bars[s] = rows
        if not bars:
            continue  # 无数据（如休市中的 A 股）：空盘跳过
        # 管道健壮性：同一账户内各标的日历应一致；若某标的最新 bar 早于账户内
        # 最大日期，说明缺行情（接口失败/漏入库），WARN 留痕；缺失日的持仓估值
        # 沿用该标的上个收盘价（不编造新 bar、不把持仓归零）。
        last_by_sym = {s: blist[-1][0] for s, blist in bars.items()}
        max_last = max(last_by_sym.values())
        for s in sorted(last_by_sym):
            if last_by_sym[s] < max_last:
                print("WARN %s 最新行情 %s，早于账户 %s 内最新日期 %s；缺失日估值沿用上个收盘价"
                      % (s, last_by_sym[s], aid, max_last))
        sim = SimAccount(id=aid, ccy=ccy, cash=float(acct["principal"]),
                         t1=bool(cfg["rules"].get("a_t1")) and aid == "sim_a")
        # 预计算信号
        sig_by_date = {}
        closes_by_sym = {}
        # 策略选择：分账户（2026-10-16 用户选A），优先读 signals.per_account[aid]，回退到全局 strategy
        _sig_cfg = cfg.get("signals", {})
        strategy = _sig_cfg.get("per_account", {}).get(aid) or _sig_cfg.get("strategy", "ma20")
        for s, blist in bars.items():
            dates = [r[0] for r in blist]
            closes = [r[4] for r in blist]
            closes_by_sym[s] = dict(zip(dates, closes))
            if strategy == "buyback":
                highs = [r[2] for r in blist]
                gen = gen_buyback_signals(dates, closes, highs)
            else:
                gen = gen_signals(dates, closes, ma_long)
            for d, side, px in gen:
                sig_by_date.setdefault(d, []).append((s, side, px))
        prev_close = {}
        for s, blist in bars.items():
            for i, r in enumerate(blist):
                prev_close[(s, r[0])] = blist[i - 1][4] if i > 0 else None

        dates = sorted(set(d for bl in bars.values() for d, *_ in bl))
        asof = dates[-1] if asof is None or dates[-1] > asof else asof
        prev_nav = float(acct["principal"])
        last_px = {}  # 标的 -> 上个已知收盘价；缺 bar 的日期沿用（不归零、不编造）
        for d in dates:
            seen = set()
            for s, side, sig_px in sorted(sig_by_date.get(d, [])):
                # 验证期闸（仅 sim_a）：回填的历史 K 线仅做 MA20 指标预热，
                # VAL_START 之前的信号禁止入库，避免污染验证窗口、
                # 避免 manual 模式下积压历史待审批。港股/加密的历史信号为
                # 自然积累的合法记录，不在此限。
                if aid == "sim_a" and d < VAL_START.get(aid, "2026-10-08"):
                    continue
                con.execute(
                    "INSERT INTO signals(date,account,symbol,side,price) VALUES(?,?,?,?,?)",
                    (d, aid, s, side, sig_px))
                close = closes_by_sym[s][d]
                px = close * (1 + slippage) if side == "BUY" else close * (1 - slippage)
                # A股涨跌停保护
                if aid == "sim_a":
                    pc = prev_close.get((s, d))
                    lim = cfg["rules"]["limit_pct"].get(boards.get(s, "main"), 0.10)
                    hit = check_limit(close, pc, lim) if pc else None
                    if (side == "BUY" and hit == "up") or (side == "SELL" and hit == "down"):
                        continue
                fee_rate, min_fee = _fee_for(aid, side, cfg)
                key = (d, aid, s, side)
                seen.add(key)
                if key in existing_fills:
                    # 历史成交：按存储值原样重放进 sim 状态（零漂移），不重新过门禁
                    qty, price, fee = existing_fills[key]
                    _replay_fill(sim, s, side, qty, price, fee, d)
                    continue
                # 新信号门禁：明确跳过的信号永不成交（任何模式）；
                # 暂停只记信号不成交；手动模式需批准才成交
                ap = con.execute(
                    "SELECT decision FROM approvals WHERE date=? AND account=?"
                    " AND symbol=? AND side=?", (d, aid, s, side)).fetchone()
                ap = ap[0] if ap else None
                if ap == "skipped":
                    continue
                if status == "paused":
                    continue
                if mode == "manual" and ap != "approved":
                    continue
                try:
                    if side == "BUY":
                        lot = float(lots.get(s, 1))
                        qty = math.floor(sim.cash * buy_frac / px / lot) * lot
                        if qty <= 0:
                            continue
                        fee = sim.buy(s, qty, px, d, fee_rate, min_fee)
                        pnl = 0.0
                    else:
                        qty = sim.position_qty(s, d)
                        if qty <= 0:
                            continue
                        fee, pnl = sim.sell(s, qty, px, d, fee_rate)
                except ValueError:
                    continue  # 现金/可卖不足：信号保留，无成交
                con.execute(
                    "INSERT INTO fills(date,account,symbol,side,qty,price,fee,pnl)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (d, aid, s, side, qty, round(px, 6), round(fee, 4), round(pnl, 2)))
            # 历史成交补重放：bar 缺失导致信号未重新生成时（如行情漏入库），
            # 该日期的历史成交仍必须重放，否则持仓会被静默丢弃。
            for (fd, faid, fs, fside), (qty, price, fee) in existing_fills.items():
                if faid != aid or fd != d or (fd, faid, fs, fside) in seen:
                    continue
                _replay_fill(sim, fs, fside, qty, price, fee, fd)
            # 当日收盘净值（缺 bar 的标的沿用上个收盘价估值，已在上方 WARN 留痕）
            pos_val, cash = 0.0, sim.cash
            for s in bars:
                if d in closes_by_sym[s]:
                    last_px[s] = closes_by_sym[s][d]
                if s in last_px:
                    pos_val += sim.market_value(s, last_px[s])
            nav = cash + pos_val
            pnl_day = nav - prev_nav
            ret_total = (nav - acct["principal"]) / acct["principal"]
            con.execute(
                "INSERT INTO daily_report(date,account,symbol,nav,cash,position_value,pnl_day,ret_total)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (d, aid, "TOTAL", round(nav, 2), round(cash, 2),
                 round(pos_val, 2), round(pnl_day, 2), round(ret_total, 6)))
            prev_nav = nav
        # 期末持仓快照（只记有仓位的）
        for s, blist in bars.items():
            q = sim.position_qty(s)
            if q <= 0:
                continue
            last = blist[-1][4]
            lots_s = sim.positions.get(s, [])
            avg = sum(l.qty * l.cost for l in lots_s) / q if q > 0 else 0.0
            con.execute(
                "INSERT INTO positions(account,symbol,qty,avg_cost,last_price,market_value)"
                " VALUES(?,?,?,?,?,?)",
                (aid, s, q, round(avg, 6), last, round(q * last, 2)))
    con.commit()
    con.close()
    return asof


def write_report(db_path=DB, cfg=None, asof=None):
    """生成 Markdown 日报到 reports/，返回文件路径。"""
    if cfg is None:
        import yaml
        cfg = yaml.safe_load(open(os.path.join(BASE, "config", "strategy.yaml"), encoding="utf-8"))
    # 测试时可覆盖日报输出目录，避免测试跑批覆盖生产日报
    report_dir = os.environ.get("PAPER_SIM_REPORTS", REPORT_DIR)
    os.makedirs(report_dir, exist_ok=True)
    con = sqlite3.connect(db_path)
    if asof is None:
        r = con.execute("SELECT MAX(date) FROM daily_report").fetchone()
        asof = r[0] if r and r[0] else "nodata"
    L = ["# 模拟日报 %s" % asof, ""]
    for acct in cfg["accounts"]:
        aid = acct["id"]
        row = con.execute(
            "SELECT nav,cash,position_value,pnl_day,ret_total FROM daily_report"
            " WHERE date=? AND account=? AND symbol='TOTAL'", (asof, aid)).fetchone()
        # 各市场日历不同（如加密 UTC 日K在跑批时未收盘）：用账户自身最新日期，
        # 避免“本期无数据”误报；滞后时在标题注明截至日期。
        sec_date = asof
        if not row:
            r2 = con.execute(
                "SELECT date,nav,cash,position_value,pnl_day,ret_total FROM daily_report"
                " WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1",
                (aid,)).fetchone()
            if not r2:
                L += ["## %s（%s）— 本期无数据" % (aid, acct["ccy"]), ""]
                continue
            sec_date = r2[0]
            nav, cash, pv, pnl_day, ret = r2[1], r2[2], r2[3], r2[4], r2[5]
        else:
            nav, cash, pv, pnl_day, ret = row
        title = "## %s（%s）" % (aid, acct["ccy"])
        if sec_date != asof:
            title += " · 截至 %s" % sec_date
        L += [title,
              "- 初始资金 %s → 总资产 %s（累计 %+.2f%%）" % (
                  f"{acct['principal']:,.0f}", f"{nav:,.2f}", ret * 100),
              "- 现金 %s / 持仓市值 %s / 当日盈亏 %+.2f" % (
                  f"{cash:,.2f}", f"{pv:,.2f}", pnl_day),
              "", "持仓："]
        unit = "枚" if aid == "sim_crypto" else "股"
        for s, q, avg, last, mv in con.execute(
                "SELECT symbol,qty,avg_cost,last_price,market_value FROM positions"
                " WHERE account=? AND qty>0", (aid,)):
            L.append("- %s：%s %s @均价 %s，现价 %s，市值 %s" % (s, f"{q:,.4f}".rstrip("0").rstrip("."),
                                                              unit, avg, last, f"{mv:,.2f}"))
        L += ["", "今日信号与成交："]
        n = 0
        for d, s, side, px in con.execute(
                "SELECT date,symbol,side,price FROM signals WHERE date=? AND account=?"
                " ORDER BY symbol", (asof, aid)):
            f = con.execute(
                "SELECT qty,price,fee,pnl FROM fills WHERE date=? AND account=?"
                " AND symbol=? AND side=?", (d, aid, s, side)).fetchone()
            if f:
                L.append("- %s %s %s %s @%s（费 %s，盈亏 %+.2f）" % (s, side, f[0], unit, f[1], f[2], f[3]))
            else:
                L.append("- %s %s 信号 @%s（未成交）" % (s, side, px))
            n += 1
        if n == 0:
            L.append("- 无")
        L.append("")
    L += ["---", "口径声明：",
          "- 数据：%s；%s；%s" % (cfg["data_sources"]["a_hk"],
                                 cfg["data_sources"]["crypto"], cfg["data_sources"]["fx"]),
          "- 执行：信号日收盘价成交，滑点 %s%%；BUY 用现金 %s%%，SELL 全平；费用见配置" % (
              cfg["fees"]["slippage"] * 100, cfg.get("sizing", {}).get("buy_frac", 0.5) * 100),
          "- 规则版本：%s（DRAFT）" % cfg["meta"]["version"]]
    path = os.path.join(report_dir, "日报-%s.md" % asof)
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    con.close()
    return path


if __name__ == "__main__":
    asof = run()
    print(write_report(asof=asof))
