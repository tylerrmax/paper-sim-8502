#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时行情：盘中实时报价，绕过快照滞后。
- A股/港股：腾讯 qt.gtimg.cn（免费，无需认证）
- 加密：Binance 公开 API（免费，无需认证）
返回统一格式：{symbol: {"price": float, "change_pct": float, "time": str}}
"""
import json
import re
import urllib.request

_TIMEOUT = 8


def _http_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://gu.qq.com/",
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read().decode("gbk", errors="ignore")


def _tx_code(symbol):
    """转腾讯代码：002446->sz002446， 600150->sh600150， HK9660->hk09660"""
    s = symbol.upper()
    if s.startswith("HK"):
        num = re.sub(r"\D", "", s)
        return f"hk{num.zfill(5)}"
    if s.startswith(("00", "30")):
        return f"sz{s}"
    if s.startswith("60") or s.startswith("68"):
        return f"sh{s}"
    return s.lower()


def fetch_cn_hk(symbols):
    """批量拉 A股/港股实时行情。返回 {symbol: {price, change_pct, time}}"""
    codes = [_tx_code(s) for s in symbols]
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    try:
        text = _http_get(url)
    except Exception:
        return {}
    out = {}
    # v_sz002446="1~盛路通信~002446~6.93~6.78~6.78~...~20261016~14:55:03~..."
    # v_hk09660="100~地平线机器人-W~09660~3.85~3.90~..."
    for sym, code in zip(symbols, codes):
        m = re.search(rf'v_{re.escape(code)}="([^"]*)"', text)
        if not m:
            continue
        parts = m.group(1).split("~")
        try:
            if code.startswith("hk"):
                # 港股格式：100~名称~代码~现价~昨收~...~日期~时间
                price = float(parts[3]) if parts[3] else 0
                prev = float(parts[4]) if parts[4] else 0
                t = f"{parts[30]} {parts[31]}" if len(parts) > 31 else ""
            else:
                # A股格式：1~名称~代码~现价~昨收~今开~...~日期~时间
                price = float(parts[3]) if parts[3] else 0
                prev = float(parts[4]) if parts[4] else 0
                t = f"{parts[30]} {parts[31]}" if len(parts) > 31 else ""
            chg = (price - prev) / prev * 100 if prev else 0
            if price > 0:
                out[sym] = {"price": price, "change_pct": round(chg, 2),
                            "time": t.strip()}
        except (ValueError, IndexError):
            continue
    return out


def fetch_crypto(symbols=("BTCUSDT",)):
    """Binance 24h ticker。返回 {symbol: {price, change_pct, time}}"""
    out = {}
    for sym in symbols:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym.upper()}"
        try:
            req = urllib.request.Request(url,
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req,
                                        timeout=_TIMEOUT) as r:
                d = json.loads(r.read().decode())
            price = float(d["lastPrice"])
            chg = float(d["priceChangePercent"])
            out[sym] = {"price": price, "change_pct": round(chg, 2),
                        "time": "Binance实时"}
        except Exception:
            continue
    return out


def fetch_all(symbols):
    """统一入口：自动分流 A股/港股/加密。"""
    cn_hk = [s for s in symbols
             if not s.upper().endswith("USDT")]
    crypto = [s for s in symbols if s.upper().endswith("USDT")]
    out = {}
    if cn_hk:
        out.update(fetch_cn_hk(cn_hk))
    if crypto:
        out.update(fetch_crypto(crypto))
    return out
