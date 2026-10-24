#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine/funds.py 单元测试（纯函数，无需 Streamlit / 网络）。
用法： python3 tests/test_funds.py   （或 pytest）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.funds import compute_overall, fx_rates, merge_amt_series  # noqa: E402

NAMES = {"sim_a": "A股", "sim_hk": "港股", "sim_crypto": "加密货币"}
LIMITS = {"sim_a": 0.7, "sim_hk": 0.7, "sim_crypto": 1.0}
CORE = {
    "sim_a": {"nav": 100000.0, "pnl": 1000.0, "cash": 60000.0, "pos": 40000.0, "ret": 0.01},
    "sim_hk": {"nav": 100000.0, "pnl": -2000.0, "cash": 10000.0, "pos": 90000.0, "ret": -0.02},
    "sim_crypto": {"nav": 20000.0, "pnl": 0.0, "cash": 20000.0, "pos": 0.0, "ret": 0.0},
}
RATES = fx_rates((0.85, 6.6))


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_fx_rates():
    assert fx_rates((0.85, 6.6)) == {"CNY": 1.0, "HKD": 0.85, "USDT": 6.6}
    assert fx_rates(None) == {"CNY": 1.0}
    assert "HKD" not in fx_rates((None, 6.6))


def test_overall_totals_and_conversion():
    ov = compute_overall(CORE, RATES, LIMITS, NAMES)
    t = ov["total"]
    # 100000 + 100000*0.85 + 20000*6.6 = 317000
    assert approx(t["nav_cny"], 317000.0)
    # 当日盈亏：1000 + (-2000*0.85) + 0 = -700
    assert approx(t["pnl_cny"], -700.0)
    # 持仓：40000 + 90000*0.85 = 116500
    assert approx(t["pos_cny"], 116500.0)
    assert approx(t["pos_ratio"], 116500.0 / 317000.0)
    assert approx(t["pnl_pct"], -700.0 / (317000.0 + 700.0))


def test_segments_sum_to_one():
    ov = compute_overall(CORE, RATES, LIMITS, NAMES)
    segs = ov["segments"]
    assert [s["key"] for s in segs] == ["cash", "sim_a", "sim_hk", "sim_crypto"]
    assert approx(sum(s["pct"] for s in segs), 1.0)


def test_position_limit_flag():
    ov = compute_overall(CORE, RATES, LIMITS, NAMES)
    by = {a["id"]: a for a in ov["accounts"]}
    assert by["sim_hk"]["over"] is True      # 90% > 70%
    assert by["sim_a"]["over"] is False      # 40% <= 70%
    assert by["sim_crypto"]["over"] is False


def test_missing_rate_excluded_not_guessed():
    ov = compute_overall(CORE, fx_rates(None), LIMITS, NAMES)
    assert [a["id"] for a in ov["accounts"]] == ["sim_a"]
    assert set(ov["excluded"]) == {"sim_hk", "sim_crypto"}
    assert approx(ov["total"]["nav_cny"], 100000.0)


def test_empty_and_zero_nav():
    ov = compute_overall({}, RATES, LIMITS, NAMES)
    assert ov["accounts"] == [] and ov["segments"] == [] and ov["total"]["nav_cny"] == 0
    z = compute_overall({"sim_a": {"nav": 0, "pnl": 0, "cash": 0, "pos": 0, "ret": 0}}, RATES, LIMITS, NAMES)
    assert z["total"]["pos_ratio"] == 0.0 and z["segments"] == []


def test_merge_amt_series():
    per = {"001": {"2026-10-20": 90000, "2026-10-21": 91000, "2026-10-22": 92000},
           "002": {"2026-10-21": 70000, "2026-10-22": 71000}}      # 10-20 深股通缺失
    s = merge_amt_series(per, ("001", "002"), 20)
    assert s == [("2026-10-21", 1610.0), ("2026-10-22", 1630.0)]    # 百万元→亿元，仅两边都有的日期
    assert merge_amt_series(per, ("001", "002"), 1) == [("2026-10-22", 1630.0)]
    assert merge_amt_series(per, ("001", "999"), 20) == []
    assert merge_amt_series({}, (), 20) == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for f in fns:
        try:
            f()
            print("✅", f.__name__)
        except AssertionError as e:
            bad += 1
            print("❌", f.__name__, e)
    print(f"\n通过 {len(fns) - bad}/{len(fns)}")
    sys.exit(1 if bad else 0)
