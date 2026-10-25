#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多重校验选股评分：母板 Watchlist 16 只（不含 BTC），三面打分 + 硬闸门。

总分 100 = 技术面 45 + 新闻面 35 + 母板面 20。

技术面 45：
- 趋势/均线 15：收盘>MA20 +6；MA5>MA10 +3；MA10>MA20 +3；MA20 较5根前上行 +3。
- MACD 10：柱>0 +5；柱较前一根放大 +3；DIF>0 +2。
- RSI 健康区间 10：45–65→10；40–45/65–70→7；35–40/70–75→4；
  30–35/75–80→2；<30→3（超卖待修复）；≥80→0。
- 量能/接近突破 10：量比（量/20日均量）≥1.5→5、≥1.2→4、≥1.0→3、≥0.8→2、其余→1；
  收盘距20日高点：≥高点→5、≥97%→4、≥94%→3、≥90%→1、其余→0。

新闻面 35：engine/news_sentiment 板块情绪折算（Serenity 为主，Danny 封顶；
市场级风险条目不进个股分）。

母板面 20：
- 板块共振评分归一 12：共振自动评分 / 母板最高共振分(60) × 12。
- 催化落地 5：Watchlist 催化落地=是 → 5。
- 待突破 3：收盘距关键突破位 ≤5%（含已突破）→ 3，其余 0。

硬闸门（任一触发 → 总评级封顶 B，并列明原因）：
- 数据 <60 根，或最新数据距 asof >4 天；
- 收盘 < MA20 且均线空头排列（MA5<MA10<MA20）；
- RSI ≥ 80（追高）；
- 价格 < 母板止损参考；
- 单日涨幅 > 7%。

评级：S ≥75、A ≥60、B ≥45、C <45（闸门触发时 S/A 降为 B）。

用法: python3 -m engine.screen [--asof YYYY-MM-DD] [--db PATH]
只读：只读 market_data 与 config，不写库、不产生任何账户信号。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(BASE, "config", "workbook_map.json")
DEFAULT_DB = os.path.join(BASE, "db", "sim.db")

from engine.indicators import macd, rsi  # noqa: E402
from engine.news_sentiment import sector_sentiment  # noqa: E402
from engine.signals import moving_average  # noqa: E402


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


def _tech_score(bars):
    """bars: [(date,open,high,low,close,volume)] 升序。返回 (tech45, detail dict)。"""
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]
    close = closes[-1]
    ma5 = moving_average(closes, 5)
    ma10 = moving_average(closes, 10)
    ma20 = moving_average(closes, 20)

    trend = 0.0
    if ma20[-1] is not None and close > ma20[-1]:
        trend += 6
    if ma5[-1] is not None and ma10[-1] is not None and ma5[-1] > ma10[-1]:
        trend += 3
    if ma10[-1] is not None and ma20[-1] is not None and ma10[-1] > ma20[-1]:
        trend += 3
    if len(closes) >= 25 and ma20[-1] is not None and ma20[-6] is not None \
            and ma20[-1] > ma20[-6]:
        trend += 3

    dif, dea, hist = macd(closes)
    macd_pts = 0.0
    if hist[-1] is not None and hist[-1] > 0:
        macd_pts += 5
    if len(hist) >= 2 and hist[-1] is not None and hist[-2] is not None \
            and hist[-1] > hist[-2]:
        macd_pts += 3
    if dif[-1] is not None and dif[-1] > 0:
        macd_pts += 2

    rsi_v = rsi(closes)[-1]
    rsi_pts = _rsi_points(rsi_v)

    vol_pts = 1.0
    if len(vols) >= 20:
        vma = sum(vols[-20:]) / 20
        if vma > 0:
            vr = vols[-1] / vma
            vol_pts = 5.0 if vr >= 1.5 else 4.0 if vr >= 1.2 else \
                3.0 if vr >= 1.0 else 2.0 if vr >= 0.8 else 1.0
    prox_pts = 0.0
    if len(closes) >= 20:
        high20 = max(closes[-20:])
        if high20 > 0:
            ratio = close / high20
            prox_pts = 5.0 if ratio >= 1.0 else 4.0 if ratio >= 0.97 else \
                3.0 if ratio >= 0.94 else 1.0 if ratio >= 0.90 else 0.0
    detail = {"trend": trend, "macd": macd_pts, "rsi": rsi_pts,
              "volume_proximity": vol_pts + prox_pts,
              "ma5": ma5[-1], "ma10": ma10[-1], "ma20": ma20[-1],
              "rsi_v": rsi_v, "macd_hist": hist[-1]}
    return trend + macd_pts + rsi_pts + vol_pts + prox_pts, detail


def _grade(total):
    if total >= 75:
        return "S"
    if total >= 60:
        return "A"
    if total >= 45:
        return "B"
    return "C"


def score_symbol(symbol, meta, bars, asof, resonance_max, entries=None):
    """单只评分。bars 升序 [(date,open,high,low,close,volume)]，date 为 str。"""
    name = meta.get("name") or symbol
    sector_label = meta.get("sector_label")
    sector_key = meta.get("sector_key")
    gates = []

    if not bars:
        return {"symbol": symbol, "name": name, "sector_label": sector_label,
                "sector_key": sector_key, "close": None, "last_date": None,
                "tech": 0.0, "news": 0.0, "board": 0.0, "total": 0.0,
                "grade": "C", "capped": False, "gates": ["无行情数据"],
                "evidence": "无行情数据"}

    dates = [b[0] for b in bars]
    closes = [b[4] for b in bars]
    close = closes[-1]
    last_date = _to_date(dates[-1])

    tech, td = _tech_score(bars)

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
        if chg > 7:
            gates.append("单日涨幅%.1f%%>7%%" % chg)

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

    return {"symbol": symbol, "name": name, "sector_label": sector_label,
            "sector_key": sector_key, "close": close, "last_date": dates[-1],
            "chg_pct": round(chg, 2) if chg is not None else None,
            "rsi": round(td["rsi_v"], 1) if td["rsi_v"] is not None else None,
            "tech": round(tech, 2), "news": round(news, 2),
            "board": return_board, "total": round(total, 2),
            "grade": grade, "capped": capped, "gates": gates,
            "news_raw": sent["raw"], "evidence": "；".join(ev_parts)}


def screen_all(db_path=None, asof=None, map_path=None, entries=None):
    """对 workbook_map 全部标的评分，按总分降序返回 list[dict]。只读。"""
    asof = _to_date(asof) if asof else date.today()
    wmap = load_map(map_path)
    sectors = wmap.get("sectors", {})
    resonance_max = max((s.get("resonance_score") or 0 for s in sectors.values()),
                        default=0) or 60.0
    con = sqlite3.connect(db_path or DEFAULT_DB)
    rows = []
    for symbol, meta in wmap["symbols"].items():
        meta = dict(meta)
        sec = sectors.get(meta.get("sector_label") or "", {})
        meta["_resonance_score"] = sec.get("resonance_score")
        bars = con.execute(
            "SELECT date,open,high,low,close,volume FROM market_data"
            " WHERE symbol=? ORDER BY date", (symbol,)).fetchall()
        rows.append(score_symbol(symbol, meta, bars, asof, resonance_max, entries))
    con.close()
    rows.sort(key=lambda r: -r["total"])
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD，默认今天")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    args = ap.parse_args()
    asof = _to_date(args.asof) if args.asof else date.today()
    rows = screen_all(args.db, asof)
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
