#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三重校验选股：量化技术面 × X 博主新闻市场面 × 母板板块共振。

设计原则
- 宁缺毋滥：三层全部通过才输出为"优质标的"；缺输入一律判"待校验"，绝不默认放行。
- 风控优先于消息：系统硬门槛（可买数量、仓位上限、display_only、策略启用）不过，消息再好也不入选。
- 新闻面以否决为主：可验证利空一票否决；利好只确认、不独立创造候选。
- 全程留证据链（每层结论、原因、引用的简报/板块数据），可审计、可回放。
- 纯函数 + 严格输入校验，便于单测；不依赖 Streamlit。

本模块只做筛选与校验，不构成投资建议。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- 阈值（可被 config.screen 覆盖）
DEFAULTS = {
    "min_bars": 60,            # MA60 需要
    "max_stale_days": 3,       # 行情/输入最大允许陈旧天数（自然日）
    "min_tech_score": 1,       # 技术分下限（趋势±1 + 动量±1，范围 -2~+2）
    "rsi_max": 70,             # 过热
    "pctb_max": 0.95,          # 贴布林上轨
    "r5_max": 15.0,            # 5日涨幅过大（利好大概率已部分 price-in）
    "news_window_bdays": 3,    # 简报有效期（交易日，仅按周一至周五计，不含节假日）
    "news_min_sources": 2,     # 无可核验来源时，利好至少需要的独立来源数
    "sector_min_rank": 70.0,   # 板块强度百分位下限
    "sector_min_breadth": 0.5, # 板块上涨家数占比下限（若提供）
}

DIRECTIONS = ("利好", "利空", "中性")
STATUS_PASS, STATUS_VETO, STATUS_WAIT, STATUS_FAIL = "PASS", "VETO", "WAIT", "FAIL"
STATUS_CN = {STATUS_PASS: "过关", STATUS_VETO: "否决", STATUS_WAIT: "待校验", STATUS_FAIL: "未过关"}


# ---------------------------------------------------------------- 工具
def _d(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def bdays_between(d_from, d_to):
    """(d_from, d_to] 之间的周一至周五天数（不含节假日，已知局限）。d_to < d_from 返回负数。"""
    a, b = _d(d_from), _d(d_to)
    sign = 1
    if b < a:
        a, b, sign = b, a, -1
    n, cur = 0, a
    while cur < b:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return sign * n


def _norm_symbol(s, tracked):
    """把简报/板块里的代码规整到标的池里的写法；匹配不上返回 None。"""
    s = str(s or "").strip().upper()
    if not s:
        return None
    if s in tracked:
        return s
    if s.isdigit():
        z = s.zfill(6)
        if z in tracked:
            return z
        hk = "HK" + s[-4:]
        if hk in tracked:
            return hk
    if s.startswith("HK") and s[2:].isdigit() and ("HK" + s[2:][-4:]) in tracked:
        return "HK" + s[2:][-4:]
    return None


# ---------------------------------------------------------------- 输入加载与严格校验
def validate_briefs(raw, tracked):
    """返回 (items, errors)。顶层结构不对 → items=None。"""
    errors = []
    if not isinstance(raw, dict) or "asof" not in raw or not isinstance(raw.get("items"), list):
        return None, ["简报文件格式错误：需要 {\"asof\":\"YYYY-MM-DD\",\"items\":[...]}"]
    try:
        _d(raw["asof"])
    except Exception:
        return None, [f"简报 asof 不是 YYYY-MM-DD：{raw.get('asof')}"]
    items = []
    for i, it in enumerate(raw["items"], 1):
        try:
            if not isinstance(it, dict):
                raise ValueError("不是对象")
            for k in ("date", "source", "target", "direction", "fact"):
                if not str(it.get(k, "")).strip():
                    raise ValueError(f"缺少字段 {k}")
            if it["direction"] not in DIRECTIONS:
                raise ValueError(f"direction 必须是 {DIRECTIONS}")
            _d(it["date"])
            items.append({
                "date": str(it["date"])[:10], "source": str(it["source"]).strip(),
                "target": str(it["target"]).strip(), "direction": it["direction"],
                "event_type": str(it.get("event_type", "")).strip(),
                "fact": str(it["fact"]).strip(),
                "evidence": str(it.get("evidence", "") or "").strip(),
            })
        except Exception as e:
            errors.append(f"第 {i} 条简报被忽略：{e}")
    return items, errors


def validate_sectors(raw, tracked):
    """返回 (sectors, errors)。"""
    errors = []
    if not isinstance(raw, dict) or "asof" not in raw or not isinstance(raw.get("sectors"), list):
        return None, ["板块文件格式错误：需要 {\"asof\":\"YYYY-MM-DD\",\"sectors\":[...]}"]
    try:
        _d(raw["asof"])
    except Exception:
        return None, [f"板块 asof 不是 YYYY-MM-DD：{raw.get('asof')}"]
    out = []
    for i, sc in enumerate(raw["sectors"], 1):
        try:
            name = str(sc.get("name", "")).strip()
            if not name:
                raise ValueError("缺少 name")
            rank = float(sc["rank_pct"])
            if not 0 <= rank <= 100:
                raise ValueError("rank_pct 需在 0~100")
            members = [m for m in (_norm_symbol(x, tracked) for x in sc.get("members", [])) if m]
            out.append({
                "name": name, "aliases": [str(a).strip() for a in sc.get("aliases", []) if str(a).strip()],
                "members": members, "rank_pct": rank,
                "ret_5d": float(sc["ret_5d"]), "ret_20d": float(sc.get("ret_20d", 0.0)),
                "breadth": (float(sc["breadth"]) if sc.get("breadth") is not None else None),
            })
        except Exception as e:
            errors.append(f"第 {i} 个板块被忽略：{e}")
    return out, errors


def load_inputs(base_dir, tracked, asof):
    """读取 screen_inputs/{briefs,sector_board}.json。
    返回 {"briefs": {...}, "sectors": {...}}；status ∈ ok / missing / stale / invalid。"""
    res = {}
    spec = (("briefs", "briefs.json", validate_briefs, "items"),
            ("sectors", "sector_board.json", validate_sectors, "sectors"))
    for key, fname, validator, field in spec:
        path = os.path.join(base_dir, "screen_inputs", fname)
        info = {"status": "missing", "asof": None, field: [], "errors": [], "path": fname}
        if os.path.exists(path):
            try:
                raw = json.load(open(path, encoding="utf-8"))
            except Exception as e:
                info.update(status="invalid", errors=[f"无法解析 {fname}：{type(e).__name__}"])
                res[key] = info
                continue
            data, errs = validator(raw, tracked)
            if data is None:
                info.update(status="invalid", errors=errs)
            else:
                info.update(asof=str(raw["asof"])[:10], errors=errs)
                info[field] = data
                age = (_d(asof) - _d(info["asof"])).days
                info["status"] = "stale" if age > DEFAULTS["max_stale_days"] else "ok"
                if info["status"] == "stale":
                    info["errors"].append(f"{fname} 已陈旧：asof {info['asof']}，距今 {age} 天")
        res[key] = info
    return res


# ---------------------------------------------------------------- 技术状态
def tech_state(bars):
    """bars: [(date,open,high,low,close,volume)] 升序。数据不足返回 None。"""
    from engine.indicators import adx, bollinger, ma_align, macd, rsi
    if len(bars) < 30:
        return None
    c = [b[4] for b in bars]
    h = [b[2] for b in bars]
    l = [b[3] for b in bars]
    px = c[-1]

    def sma(n):
        return sum(c[-n:]) / n if len(c) >= n else None

    ma20, ma60 = sma(20), sma(60)
    _, _, hist = macd(c)
    up, _, lo = bollinger(c)
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]
    atr = sum(tr[-14:]) / 14 if len(tr) >= 14 else None
    return {
        "date": bars[-1][0], "bars": len(bars), "px": px, "ma20": ma20, "ma60": ma60,
        "align": ma_align(c), "macd_hist": hist[-1], "rsi": rsi(c)[-1], "adx": adx(h, l, c)[-1],
        "pctb": ((px - lo[-1]) / (up[-1] - lo[-1])) if up[-1] and up[-1] != lo[-1] else None,
        "atr": atr, "atr_pct": (atr / px * 100) if atr else None,
        "r1": (px / c[-2] - 1) * 100,
        "r5": (px / c[-6] - 1) * 100 if len(c) > 6 else None,
        "r20": (px / c[-21] - 1) * 100 if len(c) > 21 else None,
        "dd20": (px / max(c[-20:]) - 1) * 100,
    }


def tech_score(t):
    """趋势 ±1 + 动量 ±1，范围 -2~+2。RSI/布林/涨幅只作"过热"标签，不加分。"""
    trend = 1 if (t["px"] > t["ma20"] and t["align"] > 0) else -1 if (t["px"] < t["ma20"] and t["align"] < 0) else 0
    mom = 1 if t["macd_hist"] > 0 else -1 if t["macd_hist"] < 0 else 0
    return trend + mom


# ---------------------------------------------------------------- 第一层：量化技术面 + 系统硬门槛
def layer_quant(ctx, th):
    """ctx: {t, display_only, strategy, buy_ok, buy_errs, suggest_qty, signal, limit_pct, acct}
    返回 {status, hard_fail:bool, reasons:[...], tags:[...], score}"""
    t, reasons, tags = ctx.get("t"), [], []
    if t is None:
        return {"status": STATUS_FAIL, "hard_fail": True, "reasons": ["行情数据不足(<30根)"], "tags": [], "score": None}
    hard = []
    if t["bars"] < th["min_bars"]:
        hard.append(f"K线仅{t['bars']}根，不足{th['min_bars']}根（MA60不可用）")
    if ctx.get("stale_days", 0) > th["max_stale_days"]:
        hard.append(f"行情已陈旧{ctx['stale_days']}天")
    if ctx.get("display_only"):
        hard.append("只展示(display_only)，系统禁止买入")
    if ctx.get("strategy") == "disabled":
        hard.append("该账户策略停用，无信号来源")
    if not ctx.get("buy_ok") or not ctx.get("suggest_qty"):
        if ctx.get("zero_qty_reason"):            # 先给出根因（如仓位已达上限），再附校验原话
            hard.append(ctx["zero_qty_reason"])
        errs = "；".join(ctx.get("buy_errs") or []) or "可买数量为0"
        hard.append(f"系统买入校验未过：{errs}")
    sg = ctx.get("signal")
    if sg and sg.get("status") == "已过期":
        hard.append("最新系统信号已过期")
    if sg and sg.get("side") == "SELL" and sg.get("status") == "待执行":
        hard.append("系统最新信号为卖出（待执行）")
    # 不可追：涨跌停日
    lp = ctx.get("limit_pct")
    if lp and abs(t["r1"]) >= lp * 100 - 0.3:
        hard.append(f"当日涨跌幅{t['r1']:+.1f}%≈涨跌停，难成交")

    score = tech_score(t)
    soft = []
    if score < th["min_tech_score"]:
        soft.append(f"技术分{score:+d}<{th['min_tech_score']:+d}")
    if t["rsi"] is not None and t["rsi"] > th["rsi_max"]:
        soft.append(f"RSI {t['rsi']:.0f} 超买")
        tags.append("RSI超买")
    if t["pctb"] is not None and t["pctb"] > th["pctb_max"]:
        soft.append("贴布林上轨")
        tags.append("贴布林上轨")
    if t["r5"] is not None and t["r5"] > th["r5_max"]:
        soft.append(f"5日涨幅{t['r5']:+.1f}%过大")
        tags.append("5日涨幅过大")
    if t["rsi"] is not None and t["rsi"] < 30:
        tags.append("RSI超卖")

    if hard:
        return {"status": STATUS_FAIL, "hard_fail": True, "reasons": hard + soft, "tags": tags, "score": score}
    if soft:
        return {"status": STATUS_FAIL, "hard_fail": False, "reasons": soft, "tags": tags, "score": score}
    return {"status": STATUS_PASS, "hard_fail": False,
            "reasons": [f"技术分{score:+d}，无过热，系统买入校验通过（可买{ctx['suggest_qty']}）"],
            "tags": tags, "score": score}


# ---------------------------------------------------------------- 第二层：新闻/市场面（X 博主简报）
def layer_news(sym, sector_names, brief_info, asof, th):
    """sector_names：该标的所属板块名及别名（让"商业航天"这类主题简报也能命中成分股）。"""
    if brief_info["status"] != "ok":
        why = {"missing": "未提供简报文件", "stale": "简报已过期", "invalid": "简报文件格式错误"}[brief_info["status"]]
        extra = (brief_info.get("errors") or [])[:1]
        return {"status": STATUS_WAIT, "reasons": [why] + extra, "evidence": []}
    targets = {sym} | set(sector_names)
    win = th["news_window_bdays"]
    rel = [it for it in brief_info["items"]
           if (_norm_symbol(it["target"], {sym}) == sym or it["target"] in targets)
           and 0 <= bdays_between(it["date"], asof) <= win]
    bear = [it for it in rel if it["direction"] == "利空"]
    bull = [it for it in rel if it["direction"] == "利好"]
    ev = [{"date": it["date"], "source": it["source"], "direction": it["direction"],
           "fact": it["fact"], "evidence": it["evidence"]} for it in rel]
    verified_bear = [b for b in bear if b["evidence"]]
    if verified_bear:
        b = verified_bear[0]
        return {"status": STATUS_VETO, "evidence": ev,
                "reasons": [f"可验证利空：{b['fact']}（{b['source']} {b['date']}，佐证：{b['evidence']}）"]}
    if bear:
        b = bear[0]
        return {"status": STATUS_WAIT, "evidence": ev,
                "reasons": [f"存在未核实利空：{b['fact']}（{b['source']} {b['date']}），核实前不予通过"]}
    if not bull:
        return {"status": STATUS_FAIL, "evidence": ev,
                "reasons": [f"近{win}个交易日简报中无针对该标的/所属板块的利好确认"]}
    sources = {b["source"] for b in bull}
    verified = [b for b in bull if b["evidence"]]
    if verified or len(sources) >= th["news_min_sources"]:
        how = f"有公告/链接佐证（{verified[0]['evidence']}）" if verified else f"{len(sources)}个独立来源"
        return {"status": STATUS_PASS, "evidence": ev, "reasons": [f"利好确认：{bull[0]['fact']}；{how}"]}
    return {"status": STATUS_FAIL, "evidence": ev,
            "reasons": [f"仅单一来源({bull[0]['source']})且无可核验佐证，不足以确认"]}


# ---------------------------------------------------------------- 第三层：母板板块共振
def layer_sector(sym, t, sector_info, th):
    if sector_info["status"] != "ok":
        why = {"missing": "未提供母板板块数据", "stale": "板块数据已过期", "invalid": "板块文件格式错误"}[sector_info["status"]]
        return {"status": STATUS_WAIT, "reasons": [why] + (sector_info.get("errors") or [])[:1], "sectors": []}
    mine = [s for s in sector_info["sectors"] if sym in s["members"]]
    if not mine:
        return {"status": STATUS_WAIT, "reasons": ["该标的未出现在母板任何板块的成分中（待补映射）"], "sectors": []}
    best = max(mine, key=lambda s: s["rank_pct"])
    reasons, ok = [], True
    if best["rank_pct"] < th["sector_min_rank"]:
        ok = False
        reasons.append(f"板块「{best['name']}」强度百分位{best['rank_pct']:.0f}<{th['sector_min_rank']:.0f}")
    if best["ret_5d"] <= 0:
        ok = False
        reasons.append(f"板块「{best['name']}」5日涨幅{best['ret_5d']:+.1f}%≤0")
    if best["breadth"] is not None and best["breadth"] < th["sector_min_breadth"]:
        ok = False
        reasons.append(f"板块上涨家数占比{best['breadth']*100:.0f}%<{th['sector_min_breadth']*100:.0f}%")
    # 共振＝个股与板块同向：个股技术分为正且不逆板块（个股20日涨幅不低于板块20日涨幅-5个百分点）
    if t is not None and t.get("r20") is not None and t["r20"] < best["ret_20d"] - 5:
        ok = False
        reasons.append(f"个股20日{t['r20']:+.1f}%明显弱于板块{best['ret_20d']:+.1f}%（掉队，非共振）")
    if ok:
        reasons = [f"板块「{best['name']}」强度{best['rank_pct']:.0f}分位、5日{best['ret_5d']:+.1f}%，个股同向"]
    return {"status": STATUS_PASS if ok else STATUS_FAIL, "reasons": reasons,
            "sectors": [{"name": s["name"], "rank_pct": s["rank_pct"], "ret_5d": s["ret_5d"]} for s in mine]}


# ---------------------------------------------------------------- 风险卡（仅过关标的）
def risk_card(t, nav, suggest_qty, lot, risk_cfg):
    """止损按 ATR×倍数；数量取"系统建议数量"与"单笔风险上限（净值×single_trade_risk_pct）"的较小者。"""
    mult = float(risk_cfg.get("atr_stop_mult", 2.0))
    risk_pct = float(risk_cfg.get("single_trade_risk_pct", 2.0)) / 100.0
    px, atr = t["px"], t["atr"]
    if not atr or atr <= 0 or not nav:
        return None
    stop = px - mult * atr
    per_share = px - stop
    qty_risk = (nav * risk_pct) / per_share
    qty = min(float(suggest_qty), qty_risk)
    qty = (int(qty // lot) * lot) if lot >= 1 else round(qty, 8)
    return {"px": px, "stop": stop, "stop_pct": (stop / px - 1) * 100, "atr_mult": mult,
            "qty": qty, "notional": qty * px, "risk_amt": qty * per_share,
            "risk_pct_of_nav": (qty * per_share / nav * 100) if nav else None,
            "qty_by_system": suggest_qty, "qty_by_risk": qty_risk}


# ---------------------------------------------------------------- 汇总
def combine(q, n, s):
    """三层结论 → 最终状态。风控硬门槛优先，其次新闻否决，再看是否缺输入。"""
    if q["hard_fail"]:
        return STATUS_FAIL, "未过系统硬门槛：" + q["reasons"][0]
    for lyr in (n, s):
        if lyr["status"] == STATUS_VETO:
            return STATUS_VETO, lyr["reasons"][0]
    if q["status"] == STATUS_FAIL:
        return STATUS_FAIL, "技术面未达标：" + "；".join(q["reasons"])
    if STATUS_FAIL in (n["status"], s["status"]):
        bad = n if n["status"] == STATUS_FAIL else s
        return STATUS_FAIL, ("新闻面：" if bad is n else "板块面：") + bad["reasons"][0]
    if STATUS_WAIT in (n["status"], s["status"]):
        miss = []
        if n["status"] == STATUS_WAIT:
            miss.append("新闻面（" + n["reasons"][0] + "）")
        if s["status"] == STATUS_WAIT:
            miss.append("板块面（" + s["reasons"][0] + "）")
        return STATUS_WAIT, "量化层已过，待校验：" + "；".join(miss)
    if not (q["status"] == n["status"] == s["status"] == STATUS_PASS):   # 兜底：未知状态一律不放行
        return STATUS_FAIL, "存在未识别的校验状态，保守不放行"
    return STATUS_PASS, "三层全部通过"


def run(db_path, cfg, base_dir, asof=None):
    """对 cfg.universe 全部标的做三重校验。返回 {asof, rows, summary, inputs}。"""
    from engine.order_validate import (_lot_for, _limit_pct, _latest_total, _norm,
                                       _position_cap, suggest_buy_qty, validate_order)
    th = {**DEFAULTS, **((cfg.get("screen") or {}))}
    con = sqlite3.connect(db_path)
    try:
        asof = asof or con.execute("SELECT MAX(date) FROM market_data").fetchone()[0]
        uni = []
        for acct, syms in (cfg.get("universe") or {}).items():
            for s in syms:
                uni.append((acct, _norm(s, acct)))
        tracked = {s for _, s in uni}
        disp = {_norm(x) for x in (cfg.get("display_only") or [])}
        per_acct = (cfg.get("signals") or {}).get("per_account", {})
        inputs = load_inputs(base_dir, tracked, asof)
        rows = []
        for acct, sym in uni:
            bars = con.execute("SELECT date,open,high,low,close,volume FROM market_data "
                               "WHERE symbol=? ORDER BY date", (sym,)).fetchall()
            t = tech_state(bars)
            ctx = {"t": t, "acct": acct, "display_only": sym in disp,
                   "strategy": per_acct.get(acct) or (cfg.get("signals") or {}).get("strategy")}
            suggest, ok, errs = 0, False, []
            if t:
                ctx["stale_days"] = (_d(asof) - _d(t["date"])).days
                suggest = suggest_buy_qty(acct, sym, t["px"], db_path)
                ok, errs, _w = validate_order(acct, sym, "BUY", suggest, t["px"], t["date"], db_path)
                ctx["limit_pct"] = _limit_pct(cfg, sym) if acct == "sim_a" else None
                sg = con.execute("SELECT date,side,price,status FROM signals WHERE account=? AND symbol=? "
                                 "ORDER BY date DESC LIMIT 1", (acct, sym)).fetchone()
                ctx["signal"] = ({"date": sg[0], "side": sg[1], "price": sg[2], "status": sg[3]} if sg else None)
            ctx.update(buy_ok=ok, buy_errs=errs, suggest_qty=suggest)
            if t and not suggest:
                tot = _latest_total(con, acct)
                cap = _position_cap(cfg, acct)
                if tot and tot[0] and (tot[2] or 0) / tot[0] >= cap - 1e-9:
                    over = (tot[2] or 0) - cap * tot[0]
                    ctx["zero_qty_reason"] = (f"账户仓位{(tot[2] or 0) / tot[0] * 100:.1f}%已达上限{cap * 100:.0f}%"
                                              f"（超出{max(over, 0):,.0f}），无开仓额度，需先减仓")
                elif tot and (tot[1] or 0) <= 0:
                    ctx["zero_qty_reason"] = "账户无可用现金"
            q = layer_quant(ctx, th)
            sec_names = set()
            for sc in inputs["sectors"]["sectors"]:
                if sym in sc["members"]:
                    sec_names |= {sc["name"], *sc["aliases"]}
            n = layer_news(sym, sec_names, inputs["briefs"], asof, th)
            s = layer_sector(sym, t, inputs["sectors"], th)
            status, why = combine(q, n, s)
            row = {"acct": acct, "sym": sym, "status": status, "why": why,
                   "quant": q, "news": n, "sector": s, "tech": t, "risk": None}
            if status == STATUS_PASS:
                tot = _latest_total(con, acct)
                row["risk"] = risk_card(t, (tot[0] if tot else 0), suggest, _lot_for(cfg, acct, sym),
                                        cfg.get("risk") or {})
            rows.append(row)
    finally:
        con.close()
    summary = {k: sum(1 for r in rows if r["status"] == k)
               for k in (STATUS_PASS, STATUS_VETO, STATUS_WAIT, STATUS_FAIL)}
    return {"asof": asof, "rows": rows, "summary": summary, "inputs": inputs, "thresholds": th}
