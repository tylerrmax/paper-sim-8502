#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多重校验选股评分：母板 Watchlist 16 只（不含 BTC），三面打分 + 硬闸门。

总分 100 = 技术面 45 + 新闻面 35 + 母板面 20。

技术面 45 = 基础项 30 + 形态因子层 ≤15（模块D，2026-10-26）：
- 趋势/均线 10：收盘>MA20 +5；MA5>MA10 +2；MA10>MA20 +2；MA20 较5根前上行 +1。
- MACD 7：柱>0 +3；柱较前一根放大 +2；DIF>0 +2。
- RSI 健康区间 7：按旧 10 分制映射（10→7、7→5、4→3、2→1、3→2、0→0）：
  45–65 最佳；≥80→0。
- 量能/接近突破 6：量比≥1.5→3、≥1.2→2.5、≥1.0→2、≥0.8→1、其余→0.5；
  收盘距20日高点：≥高点→3、≥97%→2.5、≥94%→2、≥90%→1、其余→0。
- 形态因子层 ≤15（全部为「待验证因子」，只调分与证据、不单独放行；
  逐项证据写入 results_json 的 factors 字段，screen_perf 按因子追踪）：
  斐波那契支撑 +4（近120日摆动高低点 0.5/0.618 回撤位 ±1.5% 内回踩
  且未跌破 0.618；收盘跌破 0.618 位 -1.5% → 负标记 fib_breakdown）；
  上升通道近突破 +4（90 日对数回归通道斜率>0 且距上轨 ≤3%；与母板
  面“待突破”重复时计分取母板面、本项只列证据不计分，避免双计）；
  箱体突破 +4（60 日箱体宽度 ≤均价 12% 后收盘站上箱顶且量 ≥1.5×
  20 日均量；突破后 3 日内收回箱内 → 负标记 box_false_breakout）；
  相对强度 +3（20/60 日涨幅减上证同期涨幅双正且 60 日居池内前 1/3）；
  回踩确认 +3（近 2–10 日有 ≥3% 上涨段后回踩 MA20 ±3% 不破且缩量）。

新闻面 35：engine/news_sentiment 板块情绪折算（Serenity 为主，Danny 封顶；
市场级风险条目不进个股分）。

母板面 20：
- 板块共振评分归一 12：共振自动评分 / 母板最高共振分(60) × 12。
- 催化落地 5：Watchlist 催化落地=是 → 5。
- 待突破 3：收盘距关键突破位 ≤5%（含已突破）→ 3，其余 0。

硬闸门（任一触发 → 总评级封顶 B，并列明原因）：
- 数据 <60 根，或最新数据落后同市场最新日期 >4 天（分市场日历，P1-7）；
  同市场整体停更（市场最新日期距 asof >10 天）同样触发；
- 收盘 < MA20 且均线空头排列（MA5<MA10<MA20）；
- RSI ≥ 80（追高）；
- 价格 < 母板止损参考；
- 单日涨幅超限：A股 >7%，港股 >15%（港股无涨跌停，分市场阈值，P1-7）；
- 新闻面断更（最新证据距 asof >7 天或新闻账缺失）：新闻分不可信（P0-4）；
- 市场环境闸（模块A，母板真源）：workbook_map 母板字段判定为禁止
  新开仓（B31=否，或未放行且 B79=否）或数据不可用降级 → 全池附加
  环境闸（封顶 B、S/A 清空），证据行明示原因，不静默；谨慎交易
  （B31=是、未放行、B79=是）不封顶但标注验证期参数。指数自算只作
  交叉核对（regime_* 字段），不作判定源。

交易计划三价（P1-11，工作流纲领第 2 步）：每个候选输出 plan_trigger /
plan_stop / plan_target —— 触发价=母板关键突破位（workbook_map.breakout）、
止损位=母板 stop_ref、目标位=触发+(触发−止损)×2.5（按冻结毕业标准盈亏比
2.5 计算，证据行标注口径）。母板缺突破位/止损位时对应字段为 None 并在
证据行标注“母板未设”，绝不编造。

评级：S ≥75、A ≥60、B ≥45、C <45（闸门触发时 S/A 降为 B）。

时点口径（P0-2，真 as-of）：行情只取 date<=asof 的 bars；新闻只计
date<=asof 的证据（未来证据由 news_sentiment 拦截计 0）；母板映射的
版本由 workbook_map.json 的 generated_at + source_sha256 标识。

审计落库（P0-3）：screen_all 每次运行向 screen_runs 写一行
（asof/rule_version/新闻账与映射的 sha256/results_json），同 asof
重跑结果逐字一致；页面缓存命中不调用本函数、不会重复写。

用法: python3 -m engine.screen [--asof YYYY-MM-DD] [--db PATH] [--no-record]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(BASE, "config", "workbook_map.json")
DEFAULT_DB = os.path.join(BASE, "db", "sim.db")
RULE_VERSION = "screen-v1.5-2026-10-26"

from engine.indicators import macd, rsi  # noqa: E402
from engine.news_sentiment import news_freshness, sector_sentiment  # noqa: E402
from engine.signals import moving_average  # noqa: E402


def market_of(symbol):
    """市场归属：HK 前缀=港股，USDT 后缀=加密，其余=A股。"""
    if str(symbol).startswith("HK"):
        return "hk"
    if str(symbol).endswith("USDT"):
        return "crypto"
    return "a"


# 单日涨幅追高闸门（分市场，P1-7）：A股有涨跌停 7%；港股无涨跌停 15%
CHG_LIMIT = {"a": 7.0, "hk": 15.0, "crypto": 25.0}

# 目标位盈亏比（P1-11）：与冻结毕业标准盈亏比硬线一致（≥2.5）
RR_TARGET = 2.5


def _trade_plan(meta):
    """交易计划三价：触发=母板关键突破位、止损=母板 stop_ref、
    目标=触发+(触发−止损)×2.5。任一母板值缺失 → 目标为 None，不编造。"""
    trigger = meta.get("breakout")
    stop = meta.get("stop_ref")
    target = None
    if trigger is not None and stop is not None:
        target = round(trigger + (trigger - stop) * RR_TARGET, 4)
    return trigger, stop, target


def _plan_note(trigger, stop, target):
    if trigger is not None and stop is not None:
        return ("交易计划：触发 %.2f / 止损 %.2f / 目标 %.2f"
                "（目标位按2.5倍盈亏比计算）" % (trigger, stop, target))
    miss = []
    if trigger is None:
        miss.append("关键突破位")
    if stop is None:
        miss.append("止损位")
    return "交易计划：母板未设（缺%s）" % "/".join(miss)


def _to_date(v):
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def load_map(path=None):
    with open(path or MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


def _rsi_points(v):
    if v is None:
        return 0.0
    if 45 <= v <= 65:
        return 10.0
    if 40 <= v < 45 or 65 < v <= 70:
        return 7.0
    if 35 <= v < 40 or 70 < v <= 75:
        return 4.0
    if 30 <= v < 35 or 75 < v < 80:
        return 2.0
    if v < 30:
        return 3.0
    return 0.0  # >=80


_RSI10_TO_7 = {10.0: 7.0, 7.0: 5.0, 4.0: 3.0, 2.0: 1.0, 3.0: 2.0, 0.0: 0.0}


def _tech_score(bars):
    """bars: [(date,open,high,low,close,volume)] 升序。

    返回 (基础项分 ≤30, detail dict)。模块D 起技术面 45 = 基础 30 +
    形态因子层 ≤15（pattern_factors 单独计算后在 score_symbol 合并）。
    """
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]
    close = closes[-1]
    ma5 = moving_average(closes, 5)
    ma10 = moving_average(closes, 10)
    ma20 = moving_average(closes, 20)

    trend = 0.0
    if ma20[-1] is not None and close > ma20[-1]:
        trend += 5
    if ma5[-1] is not None and ma10[-1] is not None and ma5[-1] > ma10[-1]:
        trend += 2
    if ma10[-1] is not None and ma20[-1] is not None and ma10[-1] > ma20[-1]:
        trend += 2
    if len(closes) >= 25 and ma20[-1] is not None and ma20[-6] is not None \
            and ma20[-1] > ma20[-6]:
        trend += 1

    dif, dea, hist = macd(closes)
    macd_pts = 0.0
    if hist[-1] is not None and hist[-1] > 0:
        macd_pts += 3
    if len(hist) >= 2 and hist[-1] is not None and hist[-2] is not None \
            and hist[-1] > hist[-2]:
        macd_pts += 2
    if dif[-1] is not None and dif[-1] > 0:
        macd_pts += 2

    rsi_v = rsi(closes)[-1]
    rsi_pts = _RSI10_TO_7.get(_rsi_points(rsi_v), 0.0)

    vol_pts = 0.5
    if len(vols) >= 20:
        vma = sum(vols[-20:]) / 20
        if vma > 0:
            vr = vols[-1] / vma
            vol_pts = 3.0 if vr >= 1.5 else 2.5 if vr >= 1.2 else \
                2.0 if vr >= 1.0 else 1.0 if vr >= 0.8 else 0.5
    prox_pts = 0.0
    if len(closes) >= 20:
        high20 = max(closes[-20:])
        if high20 > 0:
            ratio = close / high20
            prox_pts = 3.0 if ratio >= 1.0 else 2.5 if ratio >= 0.97 else \
                2.0 if ratio >= 0.94 else 1.0 if ratio >= 0.90 else 0.0
    detail = {"trend": trend, "macd": macd_pts, "rsi": rsi_pts,
              "volume_proximity": vol_pts + prox_pts,
              "ma5": ma5[-1], "ma10": ma10[-1], "ma20": ma20[-1],
              "rsi_v": rsi_v, "macd_hist": hist[-1]}
    return trend + macd_pts + rsi_pts + vol_pts + prox_pts, detail


def _rs_excess(bars, idx_bars, n):
    """标的近 n 日涨幅 − 上证同期涨幅（日期对齐优先，缺日回退根数对齐）。

    bars/idx_bars 均为 ≤asof 截断后的升序序列。数据不足返回 None。
    """
    if len(bars) <= n or not idx_bars or len(idx_bars) <= n:
        return None
    closes = [b[4] for b in bars]
    if not closes[-1 - n]:
        return None
    sym_ret = closes[-1] / closes[-1 - n] - 1.0
    idx_by_date = {d: c for d, c in idx_bars}
    d_end, d_start = bars[-1][0], bars[-1 - n][0]
    if d_end in idx_by_date and d_start in idx_by_date \
            and idx_by_date[d_start]:
        idx_ret = idx_by_date[d_end] / idx_by_date[d_start] - 1.0
    else:
        ic = [c for _, c in idx_bars]
        if not ic[-1 - n]:
            return None
        idx_ret = ic[-1] / ic[-1 - n] - 1.0
    return sym_ret - idx_ret


def pattern_factors(bars, idx_bars=None, rs_top=False, board_near_breakout=False):
    """形态因子层（模块D，全部「待验证因子」）：纯函数、同输入同输出、
    只用传入的 ≤asof bars（as-of 安全由调用方截断保证）。

    返回 (points≤15, evidence dict)。因子只调分不单独放行；负标记
    （fib_breakdown / box_false_breakout）只进证据与绩效追踪、不扣分。
    board_near_breakout=True 时通道因子与母板面“待突破”重复，计分
    取母板面、本项 points 计 0（证据照常列示，dedup 标注）。
    """
    import math
    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    vols = [b[5] for b in bars]
    close = closes[-1]
    ev = {"status": "待验证因子"}
    flags = []
    points = 0.0

    # ---- 斐波那契回撤（近 120 日摆动高低点） ----
    fib = {"available": False}
    if len(bars) >= 30:
        w = bars[-120:]
        swing_hi = max(b[2] for b in w)
        swing_lo = min(b[3] for b in w)
        if swing_hi > swing_lo > 0:
            rng = swing_hi - swing_lo
            lv = {f: round(swing_hi - f * rng, 4) for f in (0.382, 0.5, 0.618)}
            fib = {"available": True, "swing_high": swing_hi,
                   "swing_low": swing_lo, "levels": lv}
            if close < lv[0.618] * 0.985:
                fib["mark"] = "fib_breakdown"
                flags.append("fib_breakdown")
            else:
                for f in (0.5, 0.618):
                    if abs(close - lv[f]) / lv[f] <= 0.015:
                        fib["mark"] = "fib_support"
                        fib["support_level"] = f
                        flags.append("fib_support")
                        points += 4.0
                        break
    ev["fib"] = fib

    # ---- 上升通道近突破（90 日对数价格回归通道） ----
    chan = {"available": False}
    if len(closes) >= 30:
        ys = [math.log(c) for c in closes[-90:] if c > 0]
        n = len(ys)
        if n >= 30:
            xs = list(range(n))
            mx = sum(xs) / n
            my = sum(ys) / n
            sxx = sum((x - mx) ** 2 for x in xs)
            sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            slope = sxy / sxx if sxx else 0.0
            intercept = my - slope * mx
            resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
            upper = math.exp(intercept + slope * (n - 1) + max(resid))
            dist = (upper - close) / close if close else None
            chan = {"available": True, "slope_per_bar": round(slope, 6),
                    "upper": round(upper, 4),
                    "dist_to_upper_pct": round(dist * 100, 2)
                    if dist is not None else None}
            if slope > 0 and dist is not None and -0.005 <= dist <= 0.03:
                chan["mark"] = "channel_near_breakout"
                flags.append("channel_near_breakout")
                if board_near_breakout:
                    chan["scored"] = False
                    chan["dedup"] = "与母板面待突破重复，计分取母板面"
                else:
                    chan["scored"] = True
                    points += 4.0
    ev["channel"] = chan

    # ---- 箱体突破（60 日收敛后放量站上箱顶）/ 假突破标记 ----
    box = {"available": False}
    if len(bars) >= 62:
        win = bars[-61:-1]
        box_hi = max(b[2] for b in win)
        box_lo = min(b[3] for b in win)
        mean_c = sum(b[4] for b in win) / len(win)
        width = (box_hi - box_lo) / mean_c if mean_c else None
        vma20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else None
        vr = vols[-1] / vma20 if vma20 else None
        box = {"available": True, "box_high": box_hi, "box_low": box_lo,
               "width_pct": round(width * 100, 2) if width is not None else None,
               "vol_ratio": round(vr, 2) if vr is not None else None}
        if width is not None and width <= 0.12 and close > box_hi \
                and vr is not None and vr >= 1.5:
            box["mark"] = "box_breakout"
            flags.append("box_breakout")
            points += 4.0
        elif len(bars) >= 66:
            old = bars[-64:-4]
            old_hi = max(b[2] for b in old)
            if any(c > old_hi for c in closes[-3:]) and close <= old_hi:
                box["mark"] = "box_false_breakout"
                flags.append("box_false_breakout")
    ev["box"] = box

    # ---- 相对强度（20/60 日相对上证超额，60 日须居池内前 1/3） ----
    rs20 = _rs_excess(bars, idx_bars, 20)
    rs60 = _rs_excess(bars, idx_bars, 60)
    rs_ev = {"available": rs20 is not None and rs60 is not None,
             "excess20_pct": round(rs20 * 100, 2) if rs20 is not None else None,
             "excess60_pct": round(rs60 * 100, 2) if rs60 is not None else None,
             "pool_top_third_60": bool(rs_top)}
    if rs20 is not None and rs60 is not None and rs20 > 0 and rs60 > 0 \
            and rs_top:
        rs_ev["mark"] = "relative_strength"
        flags.append("relative_strength")
        points += 3.0
    ev["relative_strength"] = rs_ev

    # ---- 回踩确认（上涨段后回踩 MA20 不破且缩量） ----
    pb = {"available": False}
    ma20 = moving_average(closes, 20)
    if len(closes) >= 25 and ma20[-1] is not None:
        seg = closes[-10:-2]
        high_i = max(range(len(seg)), key=lambda i: seg[i])
        high_close = seg[high_i]
        rise = high_close / close - 1.0 if close else None
        up_vol = vols[-10 + high_i - 4:-10 + high_i + 1]
        up_avg = sum(up_vol) / len(up_vol) if up_vol else None
        pb_avg = sum(vols[-3:]) / 3
        near_ma = ma20[-1] * 0.99 <= close <= ma20[-1] * 1.03
        pb = {"available": True,
              "rise_from_high_pct": round(rise * 100, 2)
              if rise is not None else None,
              "ma20": ma20[-1], "near_ma20": bool(near_ma),
              "pullback_vol_ratio": round(pb_avg / up_avg, 2)
              if up_avg else None}
        if rise is not None and rise >= 0.03 and near_ma \
                and up_avg and pb_avg < up_avg:
            pb["mark"] = "pullback_confirm"
            flags.append("pullback_confirm")
            points += 3.0
    ev["pullback"] = pb

    ev["flags"] = flags
    ev["points_raw"] = round(points, 2)
    points = min(15.0, points)
    ev["points"] = round(points, 2)
    return points, ev


def _grade(total):
    if total >= 75:
        return "S"
    if total >= 60:
        return "A"
    if total >= 45:
        return "B"
    return "C"


def score_symbol(symbol, meta, bars, asof, resonance_max, entries=None,
                 market_last=None, extra_gates=None, evidence_note=None,
                 idx_bars=None, rs_top=False):
    """单只评分。bars 升序 [(date,open,high,low,close,volume)]，date 为 str。

    market_last: 同市场最新行情日期（分市场新鲜度用）；extra_gates:
    调用方附加的闸门（如新闻面断更）；evidence_note: 证据行附加标注。
    """
    name = meta.get("name") or symbol
    sector_label = meta.get("sector_label")
    sector_key = meta.get("sector_key")
    gates = list(extra_gates or [])
    plan_trigger, plan_stop, plan_target = _trade_plan(meta)
    plan_note = _plan_note(plan_trigger, plan_stop, plan_target)

    if not bars:
        return {"symbol": symbol, "name": name, "sector_label": sector_label,
                "sector_key": sector_key, "close": None, "last_date": None,
                "tech": 0.0, "news": 0.0, "board": 0.0, "total": 0.0,
                "grade": "C", "capped": False,
                "plan_trigger": plan_trigger, "plan_stop": plan_stop,
                "plan_target": plan_target,
                "factors": {"status": "待验证因子", "flags": [],
                            "points": 0.0},
                "gates": gates + ["无行情数据"],
                "evidence": "无行情数据；" + plan_note}

    dates = [b[0] for b in bars]
    closes = [b[4] for b in bars]
    close = closes[-1]
    last_date = _to_date(dates[-1])

    tech_base, td = _tech_score(bars)
    # 形态因子层（模块D）：与母板面“待突破”同源重复时通道项不计分
    _bk = meta.get("breakout")
    _board_near = bool(_bk) and bool(close) and \
        (_bk - close) / close * 100 <= 5.0
    pattern_pts, pattern_ev = pattern_factors(
        bars, idx_bars=idx_bars, rs_top=rs_top,
        board_near_breakout=_board_near)
    tech = tech_base + pattern_pts

    sent = sector_sentiment(sector_key, asof, entries) if sector_key else \
        {"raw": 0.0, "score35": 0.0, "items": []}
    news = sent["score35"]

    # 母板面
    sector_info = {}
    board = 0.0
    res_score = None
    return_board = None
    # 共振评分在调用方注入 meta['_resonance_score']（来自 map.sectors）
    res_score = meta.get("_resonance_score")
    if res_score is not None and resonance_max:
        board += res_score / resonance_max * 12.0
    if meta.get("catalyst"):
        board += 5.0
    breakout = meta.get("breakout")
    if breakout:
        dist = (breakout - close) / close * 100 if close else None
        if dist is not None and dist <= 5.0:
            board += 3.0
    return_board = round(board, 2)

    # 硬闸门
    if len(bars) < 60:
        gates.append("数据不足60根（%d根）" % len(bars))
    # 新鲜度（分市场日历，P1-7）：相对同市场最新日期滞后 >4 天触发；
    # 同市场整体停更（市场最新距 asof >10 天，超出任何正常休市）全市场触发；
    # 无市场参照时回退旧口径（相对 asof 自然日）。
    if market_last is not None:
        mkt_last = _to_date(market_last)
        gap_mkt = (mkt_last - last_date).days
        if gap_mkt > 4:
            gates.append("最新数据 %s 落后同市场最新 %s 共%d天>4天"
                         % (dates[-1], market_last, gap_mkt))
        gap_mkt_asof = (asof - mkt_last).days
        if gap_mkt_asof > 10:
            gates.append("市场数据整体停更：同市场最新 %s 距 asof %d天"
                         % (market_last, gap_mkt_asof))
    else:
        stale = (asof - last_date).days
        if stale > 4:
            gates.append("最新数据 %s 距今%d天>4天" % (dates[-1], stale))
    ma5, ma10, ma20 = td["ma5"], td["ma10"], td["ma20"]
    if ma20 is not None and close < ma20 and ma5 is not None and ma10 is not None \
            and ma5 < ma10 < ma20:
        gates.append("收盘<MA20且均线空头排列")
    if td["rsi_v"] is not None and td["rsi_v"] >= 80:
        gates.append("RSI≥80追高（RSI=%.0f）" % td["rsi_v"])
    stop_ref = meta.get("stop_ref")
    if stop_ref is not None and close < stop_ref:
        gates.append("价格<母板止损参考（%.2f<%.2f）" % (close, stop_ref))
    chg = None
    if len(closes) >= 2 and closes[-2]:
        chg = (close - closes[-2]) / closes[-2] * 100
        chg_limit = CHG_LIMIT.get(market_of(symbol), 7.0)
        if chg > chg_limit:
            gates.append("单日涨幅%.1f%%>%s%%" % (chg, "%g" % chg_limit))

    total = max(0.0, min(100.0, tech + news + return_board))
    grade = _grade(total)
    capped = False
    if gates and grade in ("S", "A"):
        grade = "B"
        capped = True

    # 证据字符串：新闻证据（前2条）+ 共振分数
    ev_parts = []
    for it in sent["items"][:2]:
        ev_parts.append("%s（%s %s，%+.1f%s）" % (
            it["evidence"], it["source"], str(it["date"])[5:],
            it["contribution"], "，继承" + "ai_infra" if it.get("inherited") else ""))
    if not ev_parts:
        ev_parts.append("新闻：无未过期证据")
    if res_score is not None:
        ev_parts.append("共振：%s %.0f分" % (sector_label, res_score))
    if meta.get("catalyst"):
        ev_parts.append("催化已落地")
    if pattern_ev.get("flags"):
        ev_parts.append("待验证因子：%s（形态层 +%.1f 分）"
                        % ("、".join(pattern_ev["flags"]), pattern_pts))
    if evidence_note:
        ev_parts.append(evidence_note)
    ev_parts.append(plan_note)

    return {"symbol": symbol, "name": name, "sector_label": sector_label,
            "sector_key": sector_key, "close": close, "last_date": dates[-1],
            "chg_pct": round(chg, 2) if chg is not None else None,
            "rsi": round(td["rsi_v"], 1) if td["rsi_v"] is not None else None,
            "tech": round(tech, 2), "news": round(news, 2),
            "factors": pattern_ev, "pattern_points": pattern_pts,
            "board": return_board, "total": round(total, 2),
            "grade": grade, "capped": capped, "gates": gates,
            "plan_trigger": plan_trigger, "plan_stop": plan_stop,
            "plan_target": plan_target,
            "news_raw": sent["raw"], "evidence": "；".join(ev_parts)}


def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _sha256_obj(o):
    return _sha256_bytes(json.dumps(o, ensure_ascii=False, sort_keys=True,
                                    default=str).encode("utf-8"))


def _file_sha256(path):
    try:
        with open(path, "rb") as f:
            return _sha256_bytes(f.read())
    except OSError:
        return None


def ensure_screen_runs(con):
    con.execute("""CREATE TABLE IF NOT EXISTS screen_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_at TEXT NOT NULL,
        asof TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        news_sha256 TEXT,
        map_sha256 TEXT,
        results_json TEXT NOT NULL)""")


def record_screen_run(con, asof, rows, news_sha, map_sha):
    """筛选审计落库（P0-3）：一行 = 一次运行的完整输入版本与结果。"""
    ensure_screen_runs(con)
    con.execute(
        "INSERT INTO screen_runs(run_at,asof,rule_version,news_sha256,"
        "map_sha256,results_json) VALUES(?,?,?,?,?,?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), asof.isoformat(),
         RULE_VERSION, news_sha, map_sha,
         json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)))
    con.commit()


def screen_all(db_path=None, asof=None, map_path=None, entries=None,
               record=True):
    """对 workbook_map 全部标的评分，按总分降序返回 list[dict]。

    真 as-of（P0-2）：行情只取 date<=asof；新闻只计 date<=asof 证据。
    record=True 时把本次运行落库 screen_runs（P0-3）；页面缓存命中
    不会调用本函数，因此不会重复写。
    """
    asof = _to_date(asof) if asof else date.today()
    wmap = load_map(map_path)
    sectors = wmap.get("sectors", {})
    resonance_max = max((s.get("resonance_score") or 0 for s in sectors.values()),
                        default=0) or 60.0

    # 新闻面新鲜度（P0-4）：断更/缺失时全员附加闸门并在证据行标注，
    # 不再静默归零冒充“无新闻”。
    from engine.news_sentiment import load_news
    if entries is None:
        try:
            entries = load_news()
        except Exception:
            entries = []
    fresh = news_freshness(asof, entries)
    extra_gates, ev_note = [], None
    if fresh["missing"]:
        extra_gates = ["新闻面断更：新闻账缺失或无条目，新闻分不可信"]
        ev_note = "⚠新闻面断更（新闻账缺失），新闻分不可信"
    elif fresh["stale"]:
        extra_gates = ["新闻面断更：最新证据 %s 距 asof %d 天>7天，新闻分不可信"
                       % (fresh["latest"], fresh["age_days"])]
        ev_note = "⚠新闻面断更（最新证据 %s），新闻分不可信" % fresh["latest"]

    con = sqlite3.connect(db_path or DEFAULT_DB)
    # 先取全部 as-of 截断 bars，再算各市场最新日期（分市场日历）
    bars_by_sym = {}
    market_last = {}
    for symbol in wmap["symbols"]:
        bars = con.execute(
            "SELECT date,open,high,low,close,volume FROM market_data"
            " WHERE symbol=? AND date<=? ORDER BY date",
            (symbol, asof.isoformat())).fetchall()
        bars_by_sym[symbol] = bars
        if bars:
            m = market_of(symbol)
            if m not in market_last or bars[-1][0] > market_last[m]:
                market_last[m] = bars[-1][0]
    # 市场环境闸（模块A：同口径自动计算 + 母板 B31 对账）：自动计算用
    # ≤asof 的指数 bars（真 as-of），与 wmap 母板字段对账；不一致/
    # 缺失/过期 → 降级禁开。screen 无账户语境，不做换仓概念：禁开/
    # 降级态全池附加环境闸、S/A 清空，证据行写明指数值/MA20/B31
    # 对账状态（不许只写“无候选”）；谨慎态不封顶但标注验证期参数。
    from engine import regime as _regime_mod
    _idx_rows = con.execute(
        "SELECT date, close FROM market_data WHERE symbol=? AND date<=?"
        " ORDER BY date", (_regime_mod.INDEX_SYMBOL, asof.isoformat())).fetchall()
    _regime = _regime_mod.regime_from_closes([r[1] for r in _idx_rows])
    _auto = dict(_regime)
    _auto["last_date"] = _idx_rows[-1][0] if _idx_rows else None
    _stock_last = max(market_last.values()) if market_last else None
    _env = _regime_mod.env_from_map(wmap, auto=_auto,
                                    stock_last_date=_stock_last)
    _recon_s = {True: "对账一致", False: "对账不一致"}.get(
        _env.get("reconciled"), "对账不可用")
    _auto_s = ("上证 %.2f vs MA20 %.2f（指数日 %s）→ %s" % (
        _regime["close"], _regime["ma20"], _auto["last_date"],
        "站上" if _regime["on"] else "未站上")) if _regime.get("known") \
        else "指数数据不足，自动计算不可用"
    env_gates, env_note = [], None
    if _env["state"] == _regime_mod.DEGRADED:
        env_gates = ["市场环境数据不可用，已安全降级为禁止新开仓"
                     "（%s；%s；B31=%s，%s）"
                     % (_env.get("degraded_reason") or "", _auto_s,
                        {True: "是", False: "否"}.get(_env.get("b31"), "读取失败"),
                        _recon_s)]
        env_note = ("⛔市场环境数据不可用，已安全降级：候选池只观察不开仓"
                    "（%s；B31=%s，%s）"
                    % (_auto_s,
                       {True: "是", False: "否"}.get(_env.get("b31"), "读取失败"),
                       _recon_s))
    elif _env["state"] == _regime_mod.BLOCKED_NEW:
        env_gates = ["市场环境闸·禁止新开仓（%s；%s；B31=%s，%s），候选池只观察"
                     % (_env.get("reason") or "", _auto_s,
                        {True: "是", False: "否"}.get(_env.get("b31"), "读取失败"),
                        _recon_s)]
        env_note = ("⛔市场环境闸：禁止新开仓，候选池只观察（%s；B31=%s，%s）"
                    % (_auto_s,
                       {True: "是", False: "否"}.get(_env.get("b31"), "读取失败"),
                       _recon_s))
    elif _env["state"] == _regime_mod.CAUTIOUS:
        env_note = ("⚠谨慎交易（母板未放行·B79=是）：新建仓套验证期参数"
                    "（仓位系数 %s、总仓位上限 %s）；%s；B31=是，%s"
                    % (_env.get("buy_coef"), _env.get("total_cap"),
                       _auto_s, _recon_s))
    all_extra_gates = list(extra_gates) + env_gates
    if ev_note and env_note:
        all_ev_note = ev_note + "；" + env_note
    else:
        all_ev_note = ev_note or env_note
    # 相对强度池内排名（模块D）：60 日相对上证超额居池内前 1/3 才计分
    _idx_bars = [(r[0], r[1]) for r in _idx_rows]
    _rs60 = {}
    for _sym in wmap["symbols"]:
        _v = _rs_excess(bars_by_sym[_sym], _idx_bars, 60)
        if _v is not None:
            _rs60[_sym] = _v
    _ranked = sorted(_rs60.items(), key=lambda kv: (-kv[1], kv[0]))
    _rs_top = {s for s, _ in _ranked[:(len(_ranked) + 2) // 3]}
    rows = []
    for symbol, meta in wmap["symbols"].items():
        meta = dict(meta)
        sec = sectors.get(meta.get("sector_label") or "", {})
        meta["_resonance_score"] = sec.get("resonance_score")
        row = score_symbol(
            symbol, meta, bars_by_sym[symbol], asof, resonance_max, entries,
            market_last=market_last.get(market_of(symbol)),
            extra_gates=all_extra_gates, evidence_note=all_ev_note,
            idx_bars=_idx_bars, rs_top=symbol in _rs_top)
        row["regime_on"] = _regime.get("on")
        row["regime_close"] = _regime.get("close")
        row["regime_ma20"] = _regime.get("ma20")
        row["env_state"] = _env["state"]
        row["env_label"] = _env["label"]
        row["env_reconciled"] = _env.get("reconciled")
        row["env_reason"] = _env.get("degraded_reason") or _env.get("reason")
        rows.append(row)
    rows.sort(key=lambda r: -r["total"])
    if record and os.environ.get("PAPER_SIM_NO_SCREEN_RECORD"):
        record = False  # 测试/回归环境显式豁免，避免污染审计表
    if record:
        try:
            news_sha = _sha256_obj(entries)
            map_sha = _file_sha256(map_path or MAP_PATH) or _sha256_obj(wmap)
            record_screen_run(con, asof, rows, news_sha, map_sha)
        except Exception as e:  # 落库失败不拖垮评分，但必须留痕
            print("WARN screen_runs 落库失败: %s" % e, file=sys.stderr)
    con.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD，默认今天")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--no-record", action="store_true",
                    help="不写 screen_runs 审计表")
    args = ap.parse_args()
    asof = _to_date(args.asof) if args.asof else date.today()
    rows = screen_all(args.db, asof, record=not args.no_record)
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    print("多重校验选股评分（asof %s，共 %d 只）" % (asof, len(rows)))
    print("| 代码 | 名称 | 板块 | 收盘 | 总分 | 技45 | 讯35 | 板20 | 评级 | 闸门 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print("| %s | %s | %s | %s | %.1f | %.1f | %.1f | %.1f | %s%s | %s |" % (
            r["symbol"], r["name"], r["sector_label"] or "-",
            ("%.2f" % r["close"]) if r["close"] is not None else "-",
            r["total"], r["tech"], r["news"], r["board"], r["grade"],
            "（封顶）" if r["capped"] else "",
            "；".join(r["gates"]) if r["gates"] else "-"))
    print()
    for r in rows:
        print("- %s %s：%s" % (r["symbol"], r["name"], r["evidence"]))


if __name__ == "__main__":
    main()
