#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""engine/live_calc.py 单测：时间戳、成交量单位校验、合并、回退（不联网）。"""
import os, sys
from datetime import datetime, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import live_calc as L


def _bars(n=60, start="2026-08-01", vol=1000.0, base=10.0):
    d0 = datetime.strptime(start, "%Y-%m-%d")
    return [((d0 + timedelta(days=i)).strftime("%Y-%m-%d"), base + i * 0.1,
             base + i * 0.1 + 0.2, base + i * 0.1 - 0.2, base + i * 0.1, vol)
            for i in range(n)]


def test_parse_ts_and_unparseable():
    assert L.parse_ts("2026-11-04 14:25:03").hour == 14
    assert L.parse_ts("Binance实时") is None and L.parse_ts("") is None


def test_quote_time_summary_ignores_unparseable():
    r = L.quote_time_summary({"600150": {"time": "2026-11-04 15:00:03"},
                              "BTCUSDT": {"time": "Binance实时"}})
    assert r["newest"].strftime("%H:%M") == "15:00" and list(r["per_market"]) == ["a"]


def test_lag():
    a = L.parse_ts("2026-11-04 15:00:00"); b = L.parse_ts("2026-11-02 01:06:47")
    assert L.human_lag(a, b) == "2天13小时" and L.lag_level(a, b) == "warn"
    assert L.human_lag(b, a) == "" and L.lag_level(None, b) == "unknown"


def test_volume_unit_checks():
    db = _bars()
    same = [r for r in db[-10:]]
    assert L.check_volume_units(db, same)["status"] == "ok"
    x100 = [(r[0],) + r[1:5] + (r[5] * 100,) for r in db[-10:]]
    c = L.check_volume_units(db, x100)
    assert c["status"] == "unit_x100" and c["factor"] == 0.01
    c = L.check_volume_units(db, [(r[0],) + r[1:5] + (r[5] * 0.01,) for r in db[-10:]])
    assert c["status"] == "unit_x0.01" and c["factor"] == 100.0
    assert L.check_volume_units(db, [(r[0],) + r[1:5] + (r[5] * 7,) for r in db[-10:]])["status"] == "inconsistent"
    assert L.check_volume_units(db, [])["status"] == "unverified"


def test_merge_converts_units_and_appends_new_day():
    db = _bars(60)
    last = db[-1]
    nd = (datetime.strptime(last[0], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    fresh = [(r[0],) + r[1:5] + (r[5] * 100,) for r in db[-6:]] + [(nd, 17, 17.5, 16.8, 17.2, 50000.0)]
    bars, info = L.merge_bars(db, fresh, today=nd)
    assert bars[-1][0] == nd and info["source"] == "live" and info["provisional"]
    assert bars[-1][5] == 500.0  # 5万 ×0.01 → 账本口径
    assert len(bars) == 61


def test_merge_untrusted_volume_zeroed_and_price_mismatch_not_stitched():
    db = _bars(60)
    nd = "2026-12-01"
    odd = [(r[0],) + r[1:5] + (r[5] * 7,) for r in db[-6:]] + [(nd, 17, 17, 17, 17, 99.0)]
    bars, _ = L.merge_bars(db, odd)
    assert bars[-1][5] == 0.0
    bad_px = [(r[0], r[1], r[2], r[3], r[4] * 1.5, r[5]) for r in db[-6:]] + [(nd, 17, 17, 17, 17, 99.0)]
    bars, info = L.merge_bars(db, bad_px)
    assert len(bars) == 60 and info["source"] == "ledger" and "note" in info


def test_fallback_when_fresh_empty_and_tags():
    db = _bars(60)
    bars, info = L.merge_bars(db, [])
    assert info["source"] == "ledger" and len(bars) == 60
    assert L.tech_tags(db[:10]) == "-" and "MACD" in L.tech_tags(db)


def test_fetch_never_raises():
    def boom(url): raise OSError("x")
    assert L.fetch_fresh_bars("600150", getter=boom) == []
    assert L.fetch_fresh_bars("BTCUSDT", getter=boom) == []


def test_parse_tencent_volume_units():
    row = ["2026-11-03", "10", "10.5", "10.8", "9.9", "1000"]
    assert L.parse_tencent_kline([row], "600150")[0][5] == 100000.0   # 手→股
    assert L.parse_tencent_kline([row], "688305")[0][5] == 1000.0     # 科创板即股
    assert L.parse_tencent_kline([row], "HK9660")[0][5] == 1000.0
