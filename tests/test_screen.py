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


# ---------- 斐波收复0.618（screen-v1.6，待验证因子 +3） ----------
# 定版依据：fib_study 历史验证——三段路径仅「深回撤后收复0.618」有
# 微弱相对边际；0.382 介入 / 0.5 站稳为负证据，明确不实现、不加分。

from datetime import timedelta as _td  # noqa: E402

from engine import screen as _screen_mod  # noqa: E402
from engine.screen import _fib_reclaim_618, pattern_factors  # noqa: E402

_FIB_D0 = date(2026, 1, 1)


def _fib_bars(closes, vols=None):
    out = []
    for i, c in enumerate(closes):
        v = vols[i] if vols else 1000.0
        out.append(((_FIB_D0 + _td(days=i)).isoformat(),
                    float(c), float(c) * 1.001, float(c) * 0.999,
                    float(c), float(v)))
    return out


def _lin(a, b, n):
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def test_fib_reclaim_618_triggers():
    """(a) 标准深跌收复：冲高腿 10→15 后回撤收盘跌破 0.618 位
    （11.91），数日内收盘收复且仍站其上 → 触发，+3 分。"""
    closes = [10.0] * 20 + _lin(10, 15, 20) + _lin(15, 11.5, 15) \
        + _lin(11.5, 12.6, 5)
    bars = _fib_bars(closes)
    rec = _fib_reclaim_618(bars)
    assert rec is not None, "标准收复形态未触发"
    assert rec["days_since_reclaim"] <= 10
    assert abs(rec["level_618"] - 11.9095) < 0.01
    _, ev = pattern_factors(bars)
    assert "fib_reclaim_618" in ev["flags"]
    assert ev["fib_reclaim_618"]["points"] == 3.0
    assert ev["fib_reclaim_618"]["mark"] == "fib_reclaim_618"


def test_fib_reclaim_618_shallow_pullback_no_trigger():
    """(b) 只浅回撤到 0.5 区域（最低 12.6 > 0.618 位 11.91），从未
    收盘跌破 0.618 位 → 不触发（0.382/0.5 两段按验证结论不加分）。"""
    closes = [10.0] * 20 + _lin(10, 15, 20) + _lin(15, 12.6, 10) \
        + _lin(12.6, 13.6, 10)
    bars = _fib_bars(closes)
    assert _fib_reclaim_618(bars) is None
    _, ev = pattern_factors(bars)
    assert "fib_reclaim_618" not in ev["flags"]
    assert ev["fib_reclaim_618"]["available"] is False


def test_fib_reclaim_618_stale_no_trigger():
    """(c) 破过 0.618 并收复，但收复日距今已 >10 个交易日 → 不触发
    （因子只认新鲜收复）。"""
    closes = [10.0] * 15 + _lin(10, 15, 15) + _lin(15, 11.5, 12) \
        + _lin(11.5, 12.4, 4) \
        + [12.4, 12.5, 12.6, 12.5, 12.7, 12.6, 12.8, 12.6, 12.7, 12.8,
           12.6, 12.7, 12.8, 12.7]
    bars = _fib_bars(closes)
    assert len(bars) >= 60
    assert _fib_reclaim_618(bars) is None
    _, ev = pattern_factors(bars)
    assert "fib_reclaim_618" not in ev["flags"]


def test_fib_reclaim_618_fell_back_no_trigger():
    """(d) 收复后又跌回 0.618 位下方（最新收盘 < L618）→ 不触发。"""
    closes = [10.0] * 20 + _lin(10, 15, 20) + _lin(15, 11.5, 15) \
        + _lin(11.5, 12.5, 4) + [11.94, 11.67, 11.4]
    bars = _fib_bars(closes)
    assert _fib_reclaim_618(bars) is None
    _, ev = pattern_factors(bars)
    assert "fib_reclaim_618" not in ev["flags"]


def test_fib_reclaim_618_layer_cap():
    """(e) 形态层封顶：新因子与他人叠加时层总分仍 ≤15。
    上半：有机四因子叠加（斐波支撑+箱体突破+相对强度+收复0.618）
    原始分 14，未触封顶、原样计分；
    下半：平底突破序列有机原始分恰为 15（支撑+通道+箱体+相对
    强度），再经检测函数注入收复 +3 → 原始分 18、计分仍封顶 15。
    注入只替换检测函数本身（计分累加与封顶走生产代码），检测函数
    的有机行为由 (a)–(d) 与上半覆盖。"""
    # 上半：有机叠加 14 分
    s1 = [10.0, 13.0, 16.0, 13.0, 10.0] + [10.0] * 5 + _lin(10, 13, 10) \
        + _lin(13, 11.05, 10) + [11.0, 11.1] * 35 \
        + [11.4, 11.9, 12.1, 12.2, 12.3]
    vols1 = [1000.0] * len(s1)
    vols1[-1] = 3000.0
    bars1 = _fib_bars(s1, vols1)
    idx1 = [(b[0], 3000.0) for b in bars1]
    p1, ev1 = pattern_factors(bars1, idx_bars=idx1, rs_top=True)
    assert "fib_reclaim_618" in ev1["flags"]
    assert ev1["points_raw"] == 14.0 and p1 == 14.0, ev1["flags"]

    # 下半：有机 15 分 + 注入收复 3 分 → 封顶 15
    cap = _lin(30, 10, 20) + _lin(10, 17.6, 10) + [18.0] * 80 + [20.0]
    vols2 = [1000.0] * len(cap)
    vols2[-1] = 3000.0
    bars2 = _fib_bars(cap, vols2)
    idx2 = [(b[0], 3000.0) for b in bars2]
    p2, ev2 = pattern_factors(bars2, idx_bars=idx2, rs_top=True)
    assert ev2["points_raw"] == 15.0 and p2 == 15.0, ev2["flags"]
    orig = _screen_mod._fib_reclaim_618
    _screen_mod._fib_reclaim_618 = lambda b: {
        "leg_start_date": "s", "leg_high_date": "h", "leg_low": 1.0,
        "leg_high": 2.0, "level_618": 1.4, "broke_date": "b",
        "reclaim_date": "r", "days_since_reclaim": 1}
    try:
        p3, ev3 = pattern_factors(bars2, idx_bars=idx2, rs_top=True)
    finally:
        _screen_mod._fib_reclaim_618 = orig
    assert "fib_reclaim_618" in ev3["flags"]
    assert ev3["points_raw"] == 18.0, ev3["points_raw"]
    assert p3 == 15.0, "形态层未封顶：%s" % p3
