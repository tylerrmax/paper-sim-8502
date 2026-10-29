#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行情入库闸门（纯函数，不碰网络）。

用法（pipeline 入库前）：
    from engine.data_gate import check_bars, record_quarantine
    issues = check_bars(symbol, bars, market)      # bars: [(date,o,h,l,c,v)] 升序
    bad = {(i["symbol"], i["date"]) for i in issues if i["severity"] == "error"}
    # error 级不得写入 market_data；warn 级可写入但需登记 data_quarantine 供下游屏蔽

规则：OHLC 逻辑、价格/成交量非法、重复日期、单日异常涨跌幅、成交量相对近期中位数暴增/骤降。
成交量比较用"已接受"的近 10 根中位数，连续异常日不会把基准带歪。
"""
from datetime import datetime
from statistics import median

VOL_RATIO = 20.0          # 超过近期中位数 20 倍（或不足 1/20）视为异常
PRICE_JUMP = {"a": 0.25, "hk": 0.40, "index": 0.15, "crypto": 0.40}   # 单日涨跌幅上限（含 ST/创业板余量）

DDL = """CREATE TABLE IF NOT EXISTS data_quarantine (
    symbol TEXT NOT NULL, date TEXT NOT NULL, rule TEXT NOT NULL,
    detail TEXT, detected_at TEXT,
    PRIMARY KEY (symbol, date, rule))"""


def _issue(sym, date, rule, severity, detail):
    return {"symbol": sym, "date": date, "rule": rule, "severity": severity, "detail": detail}


def check_bars(symbol, bars, market="a"):
    out, seen, accepted_vol, prev_close = [], set(), [], None
    run = []          # 连续被判异常的 (date, volume, ratio)，用于识别"单位整体切换"
    for d, o, h, l, c, v in bars:
        if d in seen:
            out.append(_issue(symbol, d, "duplicate_date", "error", "同一日期出现多根K线"))
            continue
        seen.add(d)
        try:
            datetime.strptime(str(d)[:10], "%Y-%m-%d")
        except Exception:
            out.append(_issue(symbol, d, "bad_date", "error", f"日期格式非法：{d}"))
            continue
        if None in (o, h, l, c, v) or min(o, h, l, c) <= 0 or v < 0:
            out.append(_issue(symbol, d, "bad_value", "error", "价格非正数或成交量为负/缺失"))
            continue
        if h < max(o, c, l) - 1e-9 or l > min(o, c, h) + 1e-9:
            out.append(_issue(symbol, d, "ohlc_inconsistent", "error",
                              f"OHLC 自相矛盾 o={o} h={h} l={l} c={c}"))
            continue
        lim = PRICE_JUMP.get(market, 0.40)
        if prev_close and abs(c / prev_close - 1) > lim:
            out.append(_issue(symbol, d, "price_jump", "warn",
                              f"单日涨跌幅 {(c / prev_close - 1) * 100:+.1f}% 超过 {lim * 100:.0f}%"))
        prev_close = c
        base = [x for x in accepted_vol[-10:] if x > 0]
        flagged = False
        if len(base) >= 5 and v > 0:
            med = median(base)
            ratio = v / med
            if ratio > VOL_RATIO or ratio < 1 / VOL_RATIO:
                flagged = True
                out.append(_issue(symbol, d, "volume_outlier", "warn",
                                  f"成交量{v:,.0f}为近期中位数{med:,.0f}的{ratio:.1f}倍"))
                run.append((d, v, ratio))
                # 连续3天都在同一数量级偏离 → 不是偶发异常，而是口径/单位整体切换（如 手→股 ×100）
                if len(run) >= 3 and all(r > VOL_RATIO for _, _, r in run[-3:]):
                    rs = sorted(r for _, _, r in run[-3:])
                    if rs[-1] / rs[0] < 4:
                        first = run[-3][0]
                        out[:] = [i for i in out if not (i["rule"] == "volume_outlier"
                                                         and i["date"] >= first and i["symbol"] == symbol)]
                        out.append(_issue(symbol, first, "volume_unit_shift", "error",
                                          f"自{first}起成交量持续为此前的约{rs[1]:.0f}倍，疑似单位切换（手→股×100）；"
                                          f"历史与近期口径不一致，量能类指标不可用"))
                        accepted_vol[:] = [x for _, x, _ in run[-3:]]
                        run.clear()
                        flagged = True
                        continue
        if not flagged:
            accepted_vol.append(v)
            run.clear()
    return out


def check_db(con, asof=None):
    """对 market_data 全表跑闸门。返回 issues 列表。"""
    out = []
    for sym, mk in con.execute("SELECT symbol, MIN(market) FROM market_data GROUP BY symbol").fetchall():
        bars = con.execute("SELECT date,open,high,low,close,volume FROM market_data "
                           "WHERE symbol=? ORDER BY date", (sym,)).fetchall()
        out += check_bars(sym, bars, mk or "a")
    return out


def record_quarantine(con, issues, now=None):
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con.execute(DDL)
    con.executemany("INSERT OR IGNORE INTO data_quarantine(symbol,date,rule,detail,detected_at) VALUES (?,?,?,?,?)",
                    [(i["symbol"], i["date"], i["rule"], i["detail"], now) for i in issues])
    con.commit()


def quarantined(con):
    """返回 {(symbol,date)}，供下游（指标/图表/选股）屏蔽异常量；表不存在返回空集。"""
    try:
        return {(s, d) for s, d in con.execute("SELECT symbol,date FROM data_quarantine")}
    except Exception:
        return set()
