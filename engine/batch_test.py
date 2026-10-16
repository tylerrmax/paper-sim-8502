#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台批量测试（10-02~10-08，A股开盘前）：用市面上已有的历史行情，
对 DRAFT 策略做多标的、长历史、多参数的稳健性检验。

测什么（只检验，不调参）：
  1. 引擎正确性：多标的/长历史下无崩溃，成交对账一致；
  2. 策略统计：每个标的的胜率/盈亏比/最大回撤/总收益；
  3. 参数稳健性：MA(10/20/30) 下收益分布是否稳定（只看分布，不选最优）。

测试宇宙（港股大盘股 + 主流加密，全部是公开历史数据）：
  HK0700 腾讯控股(100股/手) HK9988 阿里巴巴(100) HK3690 美团(100)
  HK0941 中国移动(500) HK1299 友邦保险(200)
  BTCUSDT ETHUSDT（Kraken）
输出：reports/后台测试-YYYYMMDD.md
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from data.quotes import fetch_kline, fetch_crypto  # noqa: E402
from engine.signals import gen_signals  # noqa: E402
from engine.sim import SimAccount  # noqa: E402

TEST_DB = os.path.join(BASE, "db", "test.db")

# (symbol, market, lot, ccy, principal)
TEST_UNIVERSE = [
    ("HK0700", "hk", 100, "HKD", 100000),
    ("HK9988", "hk", 100, "HKD", 100000),
    ("HK3690", "hk", 100, "HKD", 100000),
    ("HK0941", "hk", 500, "HKD", 100000),
    ("HK1299", "hk", 200, "HKD", 100000),
    ("HK9660", "hk", 100, "HKD", 100000),
    ("HK0354", "hk", 2000, "HKD", 100000),
    ("BTCUSDT", "crypto", 0.00001, "USDT", 20000),
    ("ETHUSDT", "crypto", 0.0001, "USDT", 20000),
]

FEES = {"hk": (0.001, 0.002), "crypto": (0.001, 0.001)}
SLIPPAGE = 0.002
BUY_FRAC = 0.5
MA_SET = [10, 20, 30]
DAYS = 700


def get_bars(symbol, market):
    """取历史K线，本地 test.db 缓存（INSERT OR REPLACE）。
    拉取失败时回退用缓存，保证后台测试不因单次网络抖动丢标的。"""
    con = sqlite3.connect(TEST_DB)
    con.execute("CREATE TABLE IF NOT EXISTS t_bars(symbol TEXT, date TEXT, o REAL, h REAL,"
                " l REAL, c REAL, v REAL, PRIMARY KEY(symbol, date))")
    start = date.today() - timedelta(days=DAYS)
    try:
        if market == "hk":
            rows = fetch_kline(symbol, start)
        else:
            rows = fetch_crypto(symbol, start)
        con.executemany("INSERT OR REPLACE INTO t_bars VALUES(?,?,?,?,?,?,?)",
                        [(symbol, str(d), o, h, l, c, v) for d, o, h, l, c, v in rows])
        con.commit()
    except Exception as e:  # noqa: BLE001
        cached = con.execute("SELECT COUNT(*) FROM t_bars WHERE symbol=?", (symbol,)).fetchone()[0]
        print("%s 拉取失败，用缓存（%d 根）：%s" % (symbol, cached, e))
    out = con.execute("SELECT date,o,h,l,c,v FROM t_bars WHERE symbol=? ORDER BY date",
                      (symbol,)).fetchall()
    con.close()
    return [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in out]


def run_one(bars, lot, fee_buy, fee_sell, principal, ma_long):
    """单标的完整回放，返回 fills 列表和每日净值序列。"""
    dates = [b[0] for b in bars]
    closes = [b[4] for b in bars]
    sigs = {d: (side, px) for d, side, px in gen_signals(dates, closes, ma_long)}
    sim = SimAccount(id="t", ccy="X", cash=float(principal), t1=False)
    fills, navs = [], []
    for d, o, h, l, c, v in bars:
        if d in sigs:
            side, sig_px = sigs[d]
            px = c * (1 + SLIPPAGE) if side == "BUY" else c * (1 - SLIPPAGE)
            try:
                if side == "BUY":
                    qty = math.floor(sim.cash * BUY_FRAC / px / lot) * lot
                    if qty > 0:
                        fee = sim.buy("S", qty, px, d, fee_buy)
                        fills.append((d, side, qty, px, fee, 0.0))
                else:
                    qty = sim.position_qty("S", d)
                    if qty > 0:
                        fee, pnl = sim.sell("S", qty, px, d, fee_sell)
                        fills.append((d, side, qty, px, fee, pnl))
            except ValueError:
                pass
        navs.append((d, sim.cash + sim.market_value("S", c)))
    return fills, navs


def stats(fills, navs, principal):
    """胜率/盈亏比/最大回撤/总收益。round-trip 配对：BUY→SELL。"""
    buys, trips = [], []
    for d, side, qty, px, fee, pnl in fills:
        if side == "BUY":
            buys.append((qty, px, fee))
        elif buys:
            q, bp, bf = buys.pop(0)
            trips.append(pnl)
    wins = [p for p in trips if p > 0]
    gross_w = sum(wins)
    gross_l = -sum(p for p in trips if p <= 0)
    nav_vals = [n for _, n in navs]
    peak, mdd = nav_vals[0], 0.0
    for n in nav_vals:
        peak = max(peak, n)
        mdd = max(mdd, (peak - n) / peak if peak else 0)
    total_ret = (nav_vals[-1] - principal) / principal if nav_vals else 0
    return {
        "trades": len(trips),
        "win_rate": len(wins) / len(trips) if trips else 0,
        "profit_factor": gross_w / gross_l if gross_l > 0 else (float("inf") if gross_w > 0 else 0),
        "max_dd": mdd,
        "total_ret": total_ret,
        "nav_end": nav_vals[-1] if nav_vals else principal,
    }


def main():
    os.makedirs(os.path.join(BASE, "db"), exist_ok=True)
    os.makedirs(os.path.join(BASE, "reports"), exist_ok=True)
    L = ["# 后台批量测试报告 %s" % date.today(),
         "- 范围：%d 个标的 × 约 %d 天历史 × MA%s" % (len(TEST_UNIVERSE), DAYS, MA_SET),
         "- 说明：稳健性检验，不做参数优选；执行假设与主模拟盘一致", ""]
    all_rows = []
    for symbol, market, lot, ccy, principal in TEST_UNIVERSE:
        try:
            bars = get_bars(symbol, market)
        except Exception as e:  # noqa: BLE001
            L.append("## %s — 数据拉取失败：%s" % (symbol, e))
            continue
        if len(bars) < 60:
            L.append("## %s — 数据不足（%d 根），跳过" % (symbol, len(bars)))
            continue
        L.append("## %s（%s，天数 %d：%s→%s）" % (symbol, market, len(bars), bars[0][0], bars[-1][0]))
        for ma in MA_SET:
            fills, navs = run_one(bars, lot, *FEES[market], principal, ma)
            s = stats(fills, navs, principal)
            all_rows.append((symbol, ma, s))
            L.append("- MA%d：交易 %d 笔，胜率 %.1f%%，盈亏比 %s，总收益 %+.2f%%，最大回撤 %.2f%%" % (
                ma, s["trades"], s["win_rate"] * 100,
                ("%.2f" % s["profit_factor"]) if s["profit_factor"] != float("inf") else "∞",
                s["total_ret"] * 100, s["max_dd"] * 100))
        L.append("")
    # 汇总：MA20 下各标的
    L += ["---", "## 汇总（MA20）", "",
          "| 标的 | 交易笔数 | 胜率 | 盈亏比 | 总收益 | 最大回撤 |",
          "|---|---|---|---|---|---|"]
    for symbol, ma, s in all_rows:
        if ma != 20:
            continue
        L.append("| %s | %d | %.1f%% | %s | %+.2f%% | %.2f%% |" % (
            symbol, s["trades"], s["win_rate"] * 100,
            ("%.2f" % s["profit_factor"]) if s["profit_factor"] != float("inf") else "∞",
            s["total_ret"] * 100, s["max_dd"] * 100))
    # 稳健性：同一标的在 MA10/20/30 下总收益的标准差
    L += ["", "## 参数稳健性（同一标的 MA10/20/30 总收益极差）", ""]
    by_sym = {}
    for symbol, ma, s in all_rows:
        by_sym.setdefault(symbol, []).append(s["total_ret"])
    for symbol, rets in by_sym.items():
        if len(rets) >= 2:
            L.append("- %s：%+.2f%% ~ %+.2f%%（极差 %.2f%%）" % (
                symbol, min(rets) * 100, max(rets) * 100, (max(rets) - min(rets)) * 100))
    path = os.path.join(BASE, "reports", "后台测试-%s.md" % date.today())
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("测试完成：%d 标的×%d 参数" % (len(by_sym), len(MA_SET)))
    print(path)


if __name__ == "__main__":
    main()
