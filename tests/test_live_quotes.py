#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""近实时行情模块单元测试（全部 mock，不访问网络）。"""
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from engine import live_quotes as lq

DB = os.path.join(BASE, "db", "sim.db")

SINA = ('var hq_str_sz002446="盛路通信,7.090,7.110,7.190,7.240,7.050,7.180,7.190,12606583,90202375.580,'
        '13200,7.180,28900,7.170,176200,7.160,50000,7.150,65800,7.140,17000,7.190,90800,7.200,10600,7.210,'
        '43100,7.220,95700,7.230,2026-10-20,15:35:15,00";\nvar hq_str_sh688305="";')
TX_HK = ('v_hk09660="100~地平线机器人-W~09660~3.765~3.920~3.900~191054999.0~0~0~3.765~0~0~0~0~0~0~0~0~0'
         '~3.765~0~0~0~0~0~0~0~0~0~191054999.0~2026/10/20 16:08:26~-0.155~-3.95";')
BN = {"lastPrice": "81380.0", "priceChangePercent": "-1.319", "closeTime": 1792503191000}


def test_parse_sina():
    r = lq.parse_sina(SINA, ["002446", "688305"])
    assert r["002446"]["price"] == 7.19
    assert abs(r["002446"]["change_pct"] - (7.19 - 7.11) / 7.11 * 100) < 1e-9
    assert r["002446"]["time"] == "2026-10-20 15:35:15"
    assert r["002446"]["source"] == "新浪" and r["002446"]["live"]
    assert "688305" not in r  # 空串（无数据）不返回


def test_parse_tencent_hk():
    r = lq.parse_tencent(TX_HK, ["HK9660"])
    assert r["HK9660"]["price"] == 3.765
    assert r["HK9660"]["time"] == "2026-10-20 16:08:26"
    assert r["HK9660"]["delay"] == lq.DELAY_HK


def test_parse_binance():
    r = lq.parse_binance(BN, "BTCUSDT")
    assert r["price"] == 81380.0 and r["change_pct"] == -1.319 and r["source"] == "Binance"


def test_fallback_chain_and_close_fallback():
    def boom(_):
        raise TimeoutError("timeout")
    calls = []
    def tx(syms):
        calls.append(tuple(syms))
        return lq.parse_tencent(TX_HK, syms)
    out = lq.get_quotes(["002446", "HK9660", "BTCUSDT"], DB,
                        fetchers={"a": [boom], "hk": [boom, tx], "crypto": [boom]})
    assert out["HK9660"]["source"] == "腾讯"
    assert calls == [("HK9660",)]
    # A股/加密全部失败 → 回退 market_data 收盘价
    assert out["002446"]["source"] == "market_data" and not out["002446"]["live"]
    assert out["002446"]["delay"] == lq.DELAY_CLOSE
    assert out["BTCUSDT"]["source"] == "market_data"


def test_partial_source_falls_through():
    out = lq.get_quotes(["002446", "688305"], DB,
                        fetchers={"a": [lambda s: lq.parse_sina(SINA, s)]})
    assert out["002446"]["source"] == "新浪"
    assert out["688305"]["source"] == "market_data"


def test_market_hours():
    tz = lq.CN_TZ
    assert lq.is_market_open("a", datetime(2026, 10, 20, 10, 0, tzinfo=tz))
    assert not lq.is_market_open("a", datetime(2026, 10, 20, 12, 0, tzinfo=tz))
    assert not lq.is_market_open("a", datetime(2026, 10, 20, 15, 30, tzinfo=tz))
    assert lq.is_market_open("hk", datetime(2026, 10, 20, 15, 30, tzinfo=tz))
    assert not lq.is_market_open("hk", datetime(2026, 10, 24, 10, 0, tzinfo=tz))  # 周六
    assert lq.is_market_open("crypto")


def test_trading_logic_does_not_use_live_quotes():
    """信号校验/风控/pipeline/验证 KPI 不得引用实时行情。"""
    for f in ["engine/order_validate.py", "engine/pipeline.py", "engine/signal_guard.py", "sync_snapshot.py"]:
        assert "live_quotes" not in open(os.path.join(BASE, f), encoding="utf-8").read(), f


def test_fmt_price_no_scientific_notation():
    assert lq.fmt_price(82960.123, "BTCUSDT") == "82,960.12"
    assert lq.fmt_price(3.765000104904175, "HK9660") == "3.765"
    assert lq.fmt_price(7.19, "002446") == "7.19"
    assert lq.fmt_price(None) == "--"
    assert "e+" not in lq.fmt_price(1.23e9, "BTCUSDT")


def test_parse_sina_indices():
    txt = ('var hq_str_s_sh000001="上证指数,3735.3704,4.4504,0.12,4086930,69996975";\n'
           'var hq_str_rt_hkHSI="HSI,恒生指数,23990.050,24186.830,24035.000,23846.990,23867.740,-319.090,-1.320";')
    r = lq.parse_sina_indices(txt)
    assert r[0] == {"name": "上证指数", "price": 3735.3704, "change_pct": 0.12}
    assert r[1]["name"] == "恒生指数" and r[1]["price"] == 23867.74 and r[1]["change_pct"] == -1.32


def test_live_today_pnl():
    qs = {"A": {"price": 11.0, "change_pct": 10.0, "live": True},
          "B": {"price": 5.0, "change_pct": 0.0, "live": False}}
    pnl, val, n = lq.live_today_pnl([("A", 100), ("B", 100)], qs)
    assert abs(pnl - 100.0) < 1e-9 and val == 1100.0 and n == 1
