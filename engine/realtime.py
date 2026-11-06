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
from datetime import datetime, timezone

_TIMEOUT = 8

# ---- 心跳埋点（第二层 P3）：只记录、只告警，绝不影响取数主流程 ----
_HB_FAILURES = 0  # 埋点失败计数（写日志异常时累加，仅供诊断）


def _hb(source, stage, ok, detail=None):
    global _HB_FAILURES
    try:
        try:
            from engine import heartbeat
        except ImportError:
            import heartbeat
        heartbeat.record(source, stage, ok, detail=detail)
    except Exception:
        _HB_FAILURES += 1


def _hb_check(source, quotes, src_times=None):
    """有效性校验并记录 valid 阶段。只观测，不改变取数结果。"""
    global _HB_FAILURES
    try:
        try:
            from engine import heartbeat
        except ImportError:
            import heartbeat
        ok, reason = heartbeat.check_quotes(quotes, src_times=src_times)
    except Exception:
        _HB_FAILURES += 1
        return False
    _hb(source, "valid", ok, reason)
    return ok


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
    _hb("cn_hk", "request", True, f"{len(codes)} symbols")
    try:
        text = _http_get(url)
    except Exception as e:
        _hb("cn_hk", "request", False, type(e).__name__)
        return {}
    _hb("cn_hk", "response", True, f"{len(text)} chars")
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
                # 港股时间字段位置不同，取原始日期时间拼接
                t = ""
                for i, p in enumerate(parts):
                    if len(p) == 8 and p.isdigit() and p.startswith("202"):
                        # 找到 YYYYMMDD，后面跟时间
                        if i + 1 < len(parts) and ":" in parts[i + 1]:
                            t = f"{p[:4]}-{p[4:6]}-{p[6:8]} {parts[i+1]}"
                        break
            else:
                # A股格式：1~名称~代码~现价~昨收~今开~...~YYYYMMDDHHMMSS
                price = float(parts[3]) if parts[3] else 0
                prev = float(parts[4]) if parts[4] else 0
                # parts[30] = YYYYMMDDHHMMSS，如 20261016161500
                raw = parts[30] if len(parts) > 30 else ""
                if len(raw) >= 14 and raw.isdigit():
                    t = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}:{raw[12:14]}"
                elif len(raw) == 8 and raw.isdigit():
                    t = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
                else:
                    t = raw
            chg = (price - prev) / prev * 100 if prev else 0
            if price > 0:
                out[sym] = {"price": price, "change_pct": round(chg, 2),
                            "time": t.strip()}
        except (ValueError, IndexError):
            continue
    if _hb_check("cn_hk", out) and out:
        _hb("cn_hk", "persisted", True, f"{len(out)} quotes delivered")
    return out


def fetch_crypto(symbols=("BTCUSDT",)):
    """Binance 24h ticker。返回 {symbol: {price, change_pct, time}}"""
    out = {}
    src_times = []
    responses = 0
    _hb("crypto", "request", True, f"{len(symbols)} symbols")
    for sym in symbols:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym.upper()}"
        try:
            req = urllib.request.Request(url,
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req,
                                        timeout=_TIMEOUT) as r:
                d = json.loads(r.read().decode())
            responses += 1
            if d.get("closeTime"):
                src_times.append(datetime.fromtimestamp(
                    d["closeTime"] / 1000.0, tz=timezone.utc))
            price = float(d["lastPrice"])
            chg = float(d["priceChangePercent"])
            out[sym] = {"price": price, "change_pct": round(chg, 2),
                        "time": "Binance实时"}
        except Exception:
            continue
    _hb("crypto", "response", responses > 0,
        f"{responses} response(s)")
    if _hb_check("crypto", out, src_times=src_times or None) and out:
        _hb("crypto", "persisted", True, f"{len(out)} quotes delivered")
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
