#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金面计算（纯函数，不依赖 Streamlit，便于单测）。

- compute_overall：三个独立币种账户折算人民币后的「整体资金」视图
- merge_amt_series：互联互通成交额按日合并（沪+深 / 港股通沪+深）

口径说明（界面同样会展示）：
- 汇率取 fx_rate 表最新一行：hkdcny = 1 港元折合人民币；usdcny_est = 1 美元(USDT)折合人民币，
  后者为 HKDCNY×7.8 的联系汇率估算（声明值，非实时 USDCNH）。
- 缺汇率的账户不参与合计，并在 excluded 中列出，绝不拿默认汇率凑数。
"""
from __future__ import annotations

ACCOUNT_CCY = {"sim_a": "CNY", "sim_hk": "HKD", "sim_crypto": "USDT"}
ACCOUNT_ORDER = ("sim_a", "sim_hk", "sim_crypto")


def fx_rates(row):
    """fx_rate 最新行 (hkdcny, usdcny_est) → {币种: 折合人民币的汇率}；缺失的币种不出现。"""
    rates = {"CNY": 1.0}
    if row:
        hk = row[0] if len(row) > 0 else None
        us = row[1] if len(row) > 1 else None
        if hk:
            rates["HKD"] = float(hk)
        if us:
            rates["USDT"] = float(us)
    return rates


def compute_overall(core, rates, limits, names=None):
    """整体资金。

    core:   {account: {"nav","pnl","cash","pos","ret"}}，金额为账户原币种
    rates:  fx_rates() 的结果
    limits: {account: 仓位上限(0~1)}
    返回 {
      "accounts": [ {id,name,ccy,nav,nav_cny,cash_cny,pos_cny,pnl_cny,ret,pos_ratio,limit,over} ... ],
      "total": {nav_cny,cash_cny,pos_cny,pnl_cny,pnl_pct,pos_ratio,cash_ratio},
      "segments": [ {key,label,value,pct} ... ]   # 现金 + 各账户持仓，占折合总资产
      "excluded": [account, ...]                   # 因缺汇率未计入合计
    }
    """
    names = names or {}
    rows, excluded = [], []
    for aid in ACCOUNT_ORDER:
        c = core.get(aid)
        if not c:
            continue
        ccy = ACCOUNT_CCY[aid]
        rate = rates.get(ccy)
        if rate is None:
            excluded.append(aid)
            continue
        nav = float(c.get("nav") or 0)
        pos = float(c.get("pos") or 0)
        cash = float(c.get("cash") or 0)
        pnl = float(c.get("pnl") or 0)
        lim = limits.get(aid)
        ratio = (pos / nav) if nav else 0.0
        rows.append({
            "id": aid, "name": names.get(aid, aid), "ccy": ccy,
            "nav": nav, "nav_cny": nav * rate, "cash_cny": cash * rate,
            "pos_cny": pos * rate, "pnl_cny": pnl * rate,
            "ret": float(c.get("ret") or 0), "pos_ratio": ratio,
            "limit": lim, "over": bool(lim is not None and ratio > lim + 1e-9),
        })

    nav_t = sum(r["nav_cny"] for r in rows)
    cash_t = sum(r["cash_cny"] for r in rows)
    pos_t = sum(r["pos_cny"] for r in rows)
    pnl_t = sum(r["pnl_cny"] for r in rows)
    prev = nav_t - pnl_t
    total = {
        "nav_cny": nav_t, "cash_cny": cash_t, "pos_cny": pos_t, "pnl_cny": pnl_t,
        "pnl_pct": (pnl_t / prev) if prev else 0.0,
        "pos_ratio": (pos_t / nav_t) if nav_t else 0.0,
        "cash_ratio": (cash_t / nav_t) if nav_t else 0.0,
    }

    segs = []
    if nav_t:
        segs.append({"key": "cash", "label": "现金", "value": cash_t, "pct": cash_t / nav_t})
        for r in rows:
            segs.append({"key": r["id"], "label": f"{r['name']}持仓",
                         "value": r["pos_cny"], "pct": r["pos_cny"] / nav_t})
    return {"accounts": rows, "total": total, "segments": segs, "excluded": excluded}


def merge_amt_series(per, mts, limit=20):
    """互联互通成交额按日合并。

    per:  {mutual_type: {YYYY-MM-DD: 成交额(百万元)}}
    mts:  参与合并的类型（如 ("001","002") 为沪股通+深股通）
    只保留「所有类型都有数据」的交易日，单位换算为亿元（百万元/100），按日期升序取最近 limit 个。
    """
    if not mts or any(m not in per for m in mts):
        return []
    common = set(per[mts[0]])
    for m in mts[1:]:
        common &= set(per[m])
    out = [(d, sum((per[m][d] or 0) for m in mts) / 100.0) for d in sorted(common)]
    return out[-limit:]
