#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
板块新闻情绪：读 config/sector_news.json 证据账，按日期衰减折算板块情绪。

口径（显式声明）：
- 衰减：证据距 asof ≤3天 ×1.0、≤7天 ×0.8、≤14天 ×0.5、>14天 0（过期不计）。
- 来源权重：Serenity 1.0（产业链基本面，第一优先）、Danny 0.4（纯技术面）。
- Danny 封顶：同一板块内 Danny 贡献合计的绝对值封顶 DANNY_CAP=1.2（原始单位），
  防止高频技术帖淹没基本面证据。
- 单条贡献 = direction × strength × 来源权重 × 衰减；板块原始情绪为贡献之和。
- 折算新闻面得分（满分35）：score = clamp(raw, -RAW_FULL, RAW_FULL) / RAW_FULL × 35，
  RAW_FULL=5.0（原始情绪 ±5 视为打满）。
- 继承：liquid_cooling（液冷）是 ai_infra 子集，可继承 ai_infra 的证据
  （继承条目按原贡献计入，证据字符串标注“继承ai_infra”）。
- scope=market_risk 的条目（如市场级去杠杆）只作风险提示，永不进个股新闻分，
  由 market_risk() 单独返回。
- 铁律：sector_key 严格按证据账记录；“存储”证据只在 semi，不会流向 energy_storage。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_PATH = os.path.join(BASE, "config", "sector_news.json")

SOURCE_WEIGHT = {"Serenity": 1.0, "Danny": 0.4}
DANNY_CAP = 1.2        # Danny 同板块贡献绝对值封顶（原始单位）
RAW_FULL = 5.0         # 原始情绪打满值 -> 新闻面 35 分
NEWS_MAX = 35.0
INHERITS = {"liquid_cooling": ["ai_infra"]}  # 子集板块 -> 可继承的父板块


def _to_date(v):
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def decay_factor(age_days):
    """日期衰减：≤3天 1.0 / ≤7天 0.8 / ≤14天 0.5 / >14天 0。

    注意：本函数只负责 age>=0 的衰减档位；未来日期（age<0）的处理在
    _contribution 里单独拦截（返回 0），不在此压成 0 天满权。
    """
    age = max(0, int(age_days))
    if age <= 3:
        return 1.0
    if age <= 7:
        return 0.8
    if age <= 14:
        return 0.5
    return 0.0


def load_news(path=None):
    with open(path or NEWS_PATH, encoding="utf-8") as f:
        return json.load(f).get("entries", [])


def _contribution(entry, asof):
    age = (asof - _to_date(entry["date"])).days
    if age < 0:
        # 未来数据泄漏拦截（P0-2）：asof 之后发布的证据在该时点尚不存在，
        # 一律不计分——不得按 0 天满权计入历史时点评分。
        return 0.0
    dec = decay_factor(age)
    if dec == 0:
        return 0.0
    w = SOURCE_WEIGHT.get(entry.get("source"), 0.4)
    return entry["direction"] * entry["strength"] * w * dec


STALE_DAYS = 7  # 最新证据距 asof 超过 7 天视为新闻面断更（P0-4）


def news_freshness(asof, entries=None, path=None):
    """新闻账新鲜度：{latest, age_days, stale, missing}。

    missing=True 表示账文件缺失/损坏/无条目；stale=True 表示最新条目
    距 asof > STALE_DAYS 天。供健康检查与评分证据标注使用，区分
    “确实无新闻”与“简报断更”——后者不得静默归零（审计 §2 缺口2）。
    """
    asof = _to_date(asof)
    if entries is None:
        try:
            entries = load_news(path)
        except Exception:
            return {"latest": None, "age_days": None, "stale": True,
                    "missing": True}
    if not entries:
        return {"latest": None, "age_days": None, "stale": True,
                "missing": True}
    latest = max(_to_date(e["date"]) for e in entries)
    age = (asof - latest).days
    return {"latest": latest.isoformat(), "age_days": age,
            "stale": age > STALE_DAYS, "missing": False}


def sector_sentiment(sector_key, asof, entries=None):
    """返回 {raw, score35, items:[{...entry, contribution, inherited}]}。

    items 只含实际计入（衰减>0、非 market_risk）的证据，按贡献绝对值降序。
    """
    asof = _to_date(asof)
    entries = entries if entries is not None else load_news()
    keys = [sector_key] + INHERITS.get(sector_key, [])
    items = []
    for e in entries:
        if e.get("scope") == "market_risk":
            continue
        if e["sector_key"] not in keys:
            continue
        c = _contribution(e, asof)
        if c == 0:
            continue
        items.append({**e, "contribution": round(c, 3),
                      "inherited": e["sector_key"] != sector_key})
    # Danny 封顶：按贡献绝对值从大到小保留，累计绝对值不超过 DANNY_CAP
    danny = sorted((i for i in items if i["source"] == "Danny"),
                   key=lambda i: -abs(i["contribution"]))
    kept, acc = set(), 0.0
    for i in danny:
        if acc + abs(i["contribution"]) <= DANNY_CAP + 1e-9:
            kept.add(id(i))
            acc += abs(i["contribution"])
    items = [i for i in items if i["source"] != "Danny" or id(i) in kept]
    raw = sum(i["contribution"] for i in items)
    score = max(-RAW_FULL, min(RAW_FULL, raw)) / RAW_FULL * NEWS_MAX
    items.sort(key=lambda i: -abs(i["contribution"]))
    return {"raw": round(raw, 3), "score35": round(score, 2), "items": items}


def market_risk(asof, entries=None):
    """市场级风险提示（不进个股分）：返回未过期 market_risk 条目+衰减后强度。"""
    asof = _to_date(asof)
    entries = entries if entries is not None else load_news()
    out = []
    for e in entries:
        if e.get("scope") != "market_risk" and e["sector_key"] != "market":
            continue
        c = _contribution(e, asof)
        if c != 0:
            out.append({**e, "contribution": round(c, 3)})
    return out


def all_sector_sentiments(asof, entries=None):
    """全部板块情绪汇总（供页面 chips）：按 sector_key 分组，含 market_risk。"""
    asof = _to_date(asof)
    entries = entries if entries is not None else load_news()
    keys = sorted({e["sector_key"] for e in entries})
    out = []
    for k in keys:
        if k == "market":
            risks = market_risk(asof, entries)
            if risks:
                out.append({"sector_key": k, "raw": round(sum(r["contribution"] for r in risks), 3),
                            "score35": None, "items": risks, "is_market_risk": True})
            continue
        s = sector_sentiment(k, asof, entries)
        if s["items"]:
            out.append({"sector_key": k, **s, "is_market_risk": False})
    out.sort(key=lambda x: -abs(x["raw"]))
    return out


if __name__ == "__main__":
    today = date.today()
    for s in all_sector_sentiments(today):
        print("%s raw=%s score35=%s items=%d" % (
            s["sector_key"], s["raw"], s["score35"], len(s["items"])))
