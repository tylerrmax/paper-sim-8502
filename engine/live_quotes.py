#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
近实时行情（2026-10-20）：只用于"展示"，不参与信号校验 / 风控 / pipeline / 验证 KPI
（这些一律用 market_data 官方收盘价）。

来源（全部免费、无需 key）：
  A股  ：新浪 hq.sinajs.cn（需 Referer，秒级）→ 失败回退腾讯 qt.gtimg.cn
  港股 ：yfinance（约 15 分钟延迟）→ 失败回退腾讯 qt.gtimg.cn（同为延迟行情）
  加密 ：Binance 公共行情 data-api.binance.vision → api.binance.com
任一来源失败：回退到 market_data 最新收盘价，并标注"收盘价"。

返回统一格式：{symbol: {"price", "change_pct", "time", "source", "delay", "live"}}
本模块不依赖 streamlit，缓存（st.cache_data ttl）由 app.py 包一层。
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.request
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

TIMEOUT = 5
CN_TZ = ZoneInfo("Asia/Shanghai")
UA = "Mozilla/5.0"

DELAY_A = "实时（仅供参考）"
DELAY_HK = "延迟15分"
DELAY_CRYPTO = "实时（仅供参考）"
DELAY_CLOSE = "收盘价"


def market_of(symbol):
    s = str(symbol).upper()
    if s.endswith("USDT"):
        return "crypto"
    if s.startswith("HK"):
        return "hk"
    return "a"


def is_market_open(market, now=None):
    """交易时段（北京时间）：A股 9:30-11:30/13:00-15:00，港股 9:30-12:00/13:00-16:00，加密 24h。
    不含节假日日历（节假日会多刷几次，但取到的仍是最近一笔，无副作用）。"""
    if market == "crypto":
        return True
    now = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    if now.weekday() >= 5:
        return False
    t = now.time()
    if market == "a":
        return dtime(9, 30) <= t <= dtime(11, 30) or dtime(13, 0) <= t <= dtime(15, 0)
    if market == "hk":
        return dtime(9, 30) <= t <= dtime(12, 0) or dtime(13, 0) <= t <= dtime(16, 0)
    return False


def _get(url, referer=None, encoding="utf-8"):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode(encoding, errors="ignore")


def _a_code(sym):
    return ("sh" if str(sym)[0] in "69" else "sz") + str(sym)


def _hk_code(sym):
    return "hk" + re.sub(r"\D", "", sym).zfill(5)


# ---------------- A股：新浪 ----------------
def parse_sina(text, symbols):
    out = {}
    for sym in symbols:
        m = re.search(rf'hq_str_{_a_code(sym)}="([^"]*)"', text)
        if not m or not m.group(1):
            continue
        p = m.group(1).split(",")
        try:
            price, prev = float(p[3]), float(p[2])
        except (ValueError, IndexError):
            continue
        if price <= 0:  # 停牌/集合竞价前为 0
            continue
        out[sym] = {"price": price, "change_pct": (price - prev) / prev * 100 if prev else 0.0,
                    "time": f"{p[30]} {p[31]}" if len(p) > 31 else "",
                    "source": "新浪", "delay": DELAY_A, "live": True}
    return out


def fetch_a_sina(symbols):
    if not symbols:
        return {}
    url = "https://hq.sinajs.cn/list=" + ",".join(_a_code(s) for s in symbols)
    return parse_sina(_get(url, referer="https://finance.sina.com.cn", encoding="gbk"), symbols)


# ---------------- 腾讯（A股/港股回退） ----------------
def parse_tencent(text, symbols):
    out = {}
    for sym in symbols:
        code = _hk_code(sym) if market_of(sym) == "hk" else _a_code(sym)
        m = re.search(rf'v_{code}="([^"]*)"', text)
        if not m or not m.group(1):
            continue
        p = m.group(1).split("~")
        try:
            price, prev = float(p[3]), float(p[4])
        except (ValueError, IndexError):
            continue
        if price <= 0:
            continue
        ts = p[30] if len(p) > 30 else ""
        if re.fullmatch(r"\d{14}", ts):  # A股格式 20261020150003
            ts = datetime.strptime(ts, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
        ts = ts.replace("/", "-")
        hk = market_of(sym) == "hk"
        out[sym] = {"price": price, "change_pct": (price - prev) / prev * 100 if prev else 0.0,
                    "time": ts, "source": "腾讯", "delay": DELAY_HK if hk else DELAY_A, "live": True}
    return out


def fetch_tencent(symbols):
    if not symbols:
        return {}
    codes = [_hk_code(s) if market_of(s) == "hk" else _a_code(s) for s in symbols]
    return parse_tencent(_get("https://qt.gtimg.cn/q=" + ",".join(codes), encoding="gbk"), symbols)


# ---------------- 港股：yfinance ----------------
def fetch_hk_yf(symbols):
    if not symbols:
        return {}
    import yfinance as yf  # 延迟导入：未安装时由上层回退
    out = {}
    for sym in symbols:
        num = re.sub(r"\D", "", sym).lstrip("0").zfill(4)
        try:
            h = yf.Ticker(f"{num}.HK").history(period="2d", interval="1m", timeout=TIMEOUT)
        except Exception:
            continue
        if h is None or h.empty:
            continue
        last = h.iloc[-1]
        ts = h.index[-1]
        prev_day = h[h.index.date < ts.date()]
        prev = float(prev_day["Close"].iloc[-1]) if len(prev_day) else None
        price = float(last["Close"])
        out[sym] = {"price": price, "change_pct": (price - prev) / prev * 100 if prev else 0.0,
                    "time": ts.tz_convert(CN_TZ).strftime("%Y-%m-%d %H:%M:%S") if ts.tzinfo else str(ts),
                    "source": "yfinance", "delay": DELAY_HK, "live": True}
    return out


# ---------------- 加密：Binance 公共行情 ----------------
BINANCE_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")


def parse_binance(payload, sym):
    d = json.loads(payload) if isinstance(payload, str) else payload
    price = float(d["lastPrice"])
    ts = datetime.fromtimestamp(int(d["closeTime"]) / 1000, CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return {"price": price, "change_pct": float(d.get("priceChangePercent", 0)), "time": ts,
            "source": "Binance", "delay": DELAY_CRYPTO, "live": True}


def fetch_crypto(symbols):
    out = {}
    for sym in symbols:
        for host in BINANCE_HOSTS:
            try:
                out[sym] = parse_binance(_get(f"{host}/api/v3/ticker/24hr?symbol={sym}"), sym)
                break
            except Exception:
                continue
    return out


# ---------------- 回退：官方收盘价 ----------------
def close_fallback(db_path, symbols):
    out = {}
    con = sqlite3.connect(db_path)
    try:
        for sym in symbols:
            r = con.execute("SELECT close, date FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 2",
                            (sym,)).fetchall()
            if not r or not r[0][0]:
                continue
            chg = (r[0][0] - r[1][0]) / r[1][0] * 100 if len(r) > 1 and r[1][0] else 0.0
            out[sym] = {"price": float(r[0][0]), "change_pct": chg, "time": r[0][1],
                        "source": "market_data", "delay": DELAY_CLOSE, "live": False}
    finally:
        con.close()
    return out


def _try(fn, syms):
    try:
        return fn(syms) or {}
    except Exception:
        return {}


def get_quotes(symbols, db_path, fetchers=None):
    """按市场拉近实时行情；每个来源失败/缺失的标的依次回退，最后回退到收盘价。
    fetchers 可注入（单测用）：{"a": [fn...], "hk": [fn...], "crypto": [fn...]}"""
    fetchers = fetchers or {"a": [fetch_a_sina, fetch_tencent],
                            "hk": [fetch_hk_yf, fetch_tencent],
                            "crypto": [fetch_crypto]}
    out = {}
    for mkt, fns in fetchers.items():
        todo = [s for s in symbols if market_of(s) == mkt]
        for fn in fns:
            if not todo:
                break
            got = _try(fn, todo)
            out.update({k: v for k, v in got.items() if k in todo})
            todo = [s for s in todo if s not in out]
    missing = [s for s in symbols if s not in out]
    if missing:
        out.update(close_fallback(db_path, missing))
    return out
