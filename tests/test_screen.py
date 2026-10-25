#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多重校验选股最小单测：衰减/权重/封顶/铁律/闸门/评级边界。

运行: .venv/bin/python -m pytest tests/test_screen.py -q
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.news_sentiment import (decay_factor, load_news, market_risk,
                                   sector_sentiment)
from engine.screen import _grade, score_symbol

ASOF = date(2026, 10, 25)


def test_decay_boundaries():
    assert decay_factor(0) == 1.0 and decay_factor(3) == 1.0
    assert decay_factor(4) == 0.8 and decay_factor(7) == 0.8
    assert decay_factor(8) == 0.5 and decay_factor(14) == 0.5
    assert decay_factor(15) == 0.0


def test_seed_semi_dominated_by_serenity():
    entries = load_news()
    s = sector_sentiment("semi", ASOF, entries)
    # Serenity +3 +2（均2天内 ×1.0）+ Danny QCOM 1×0.4×0.8(5天)=0.32
    assert abs(s["raw"] - 5.32) < 1e-6
    assert s["score35"] == 35.0  # 原始情绪 5.32 超过打满线 5.0


def test_memory_never_leaks_to_energy_storage():
    """铁律：存储（memory）证据只进 semi，绝不进 energy_storage（储能）。"""
    entries = load_news()
    assert sector_sentiment("energy_storage", ASOF, entries)["items"] == []
    assert sector_sentiment("energy_storage", ASOF, entries)["score35"] == 0.0


def test_liquid_cooling_inherits_ai_infra():
    entries = load_news()
    s = sector_sentiment("liquid_cooling", ASOF, entries)
    assert s["raw"] == 2.0
    assert s["items"] and s["items"][0]["inherited"] is True


def test_market_risk_not_in_stock_score():
    entries = load_news()
    risks = market_risk(ASOF, entries)
    assert len(risks) == 1 and risks[0]["sector_key"] == "market"
    for key in ("semi", "ai_infra", "optical_comm"):
        items = sector_sentiment(key, ASOF, entries)["items"]
        assert all(i["sector_key"] != "market" for i in items)


def test_danny_cap():
    entries = [
        {"sector_key": "semi", "direction": 1, "strength": 3,
         "source": "Danny", "date": "2026-10-25", "evidence": "a"},
        {"sector_key": "semi", "direction": 1, "strength": 3,
         "source": "Danny", "date": "2026-10-25", "evidence": "b"},
        {"sector_key": "semi", "direction": 1, "strength": 3,
         "source": "Danny", "date": "2026-10-25", "evidence": "c"},
    ]
    s = sector_sentiment("semi", ASOF, entries)
    # 每条 3×0.4×1.0=1.2，封顶 1.2 -> 只计 1 条
    assert abs(s["raw"] - 1.2) < 1e-6 and len(s["items"]) == 1


def _bars(closes, vol=1000.0):
    base = date(2026, 8, 17)  # 70 根合成 bars 恰好结束于 2026-10-25（ASOF）
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append(((base + timedelta(days=i)).isoformat(),
                    prev, max(prev, c), min(prev, c), c, vol))
        prev = c
    return out


META = {"name": "测试", "sector_label": "半导体", "sector_key": "semi",
        "resonance_flag": False, "catalyst": False, "breakout": 999.0,
        "stop_ref": 1.0, "_resonance_score": 12.0}


def test_gate_caps_grade_when_below_stop():
    closes = [20.0 + i * 0.1 for i in range(70)]  # 稳步上行，无其它闸门
    meta = dict(META, stop_ref=999.0)  # 收盘远低于止损参考 -> 触发闸门
    r = score_symbol("000001", meta, _bars(closes), ASOF, 60.0, entries=[])
    assert any("止损参考" in g for g in r["gates"])
    assert r["grade"] in ("B", "C")  # 封顶 B：不得为 S/A


def test_no_gate_clean_uptrend():
    closes = [20.0 + i * 0.1 for i in range(70)]
    r = score_symbol("000001", dict(META), _bars(closes), ASOF, 60.0, entries=[])
    assert r["gates"] == []
    # 修订（2026-10-26，模块D）：原断言 tech>30 按旧基础项 45 分标定；
    # 新计分结构技术面 45 = 基础项 30 + 形态因子层 ≤15，匀速上行无
    # 形态命中时 tech≈26 为正确值。规则依据：模块D计分纪律（形态层
    # 封顶技术面 45 中的 15 分，RULE_VERSION screen-v1.5）。
    assert r["tech"] > 24


def test_grade_boundaries():
    assert _grade(75) == "S" and _grade(74.9) == "A"
    assert _grade(60) == "A" and _grade(45) == "B" and _grade(44.9) == "C"
