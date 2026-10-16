#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技术指标：ADX、布林带宽、均线排列度（中期：市场状态识别用）。"""
from __future__ import annotations


def adx(highs, lows, closes, n=14):
    """Average Directional Index：返回与输入等长的 ADX 序列，不足窗口为 None。
    ADX > 25 趋势市，ADX < 20 震荡市。"""
    size = len(closes)
    out = [None] * size
    if size < n * 2 + 1:
        return out
    # True Range, +DM, -DM
    trs, pdms, mdms = [], [], []
    for i in range(1, size):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        pdms.append(up if up > dn and up > 0 else 0.0)
        mdms.append(dn if dn > up and dn > 0 else 0.0)
    # Wilder 平滑
    atr = sum(trs[:n])
    pdm_s = sum(pdms[:n])
    mdm_s = sum(mdms[:n])
    dxs = []
    for i in range(n, len(trs)):
        atr = atr - atr / n + trs[i]
        pdm_s = pdm_s - pdm_s / n + pdms[i]
        mdm_s = mdm_s - mdm_s / n + mdms[i]
        pdi = 100 * pdm_s / atr if atr else 0
        mdi = 100 * mdm_s / atr if atr else 0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0
        dxs.append(dx)
        if len(dxs) >= n:
            out[i + 1] = sum(dxs[-n:]) / n
    return out


def market_regime(adx_val):
    """市场状态：trend / transitional / range"""
    if adx_val is None:
        return "unknown"
    if adx_val > 25:
        return "trend"
    if adx_val < 20:
        return "range"
    return "transitional"


def rsi(closes, n=14):
    """RSI 相对强弱指数"""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i-1]
        gains.append(max(chg, 0))
        losses.append(max(-chg, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n-1) + gains[i]) / n
        al = (al * (n-1) + losses[i]) / n
        rs = ag / al if al else 0
        out[i+1] = 100 - 100 / (1 + rs)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """MACD：返回 (dif, dea, hist) 三个序列"""
    def ema(data, n):
        out, k = [], 2 / (n + 1)
        for i, v in enumerate(data):
            out.append(v if i == 0 else v * k + out[-1] * (1 - k))
        return out
    ef, es = ema(closes, fast), ema(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, signal)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    return dif, dea, hist


def bollinger(closes, n=20, k=2):
    """布林带：返回 (upper, mid, lower)"""
    upper, mid, lower = [None]*len(closes), [None]*len(closes), [None]*len(closes)
    for i in range(n-1, len(closes)):
        window = closes[i-n+1:i+1]
        m = sum(window) / n
        var = sum((x-m)**2 for x in window) / n
        sd = var ** 0.5
        mid[i], upper[i], lower[i] = m, m + k*sd, m - k*sd
    return upper, mid, lower


def ma_align(closes):
    """均线多头排列度：MA5>MA10>MA20>MA30 为 1（完全多头），反之为 -1"""
    if len(closes) < 30:
        return 0
    from engine.signals import moving_average
    ma5 = moving_average(closes, 5)[-1]
    ma10 = moving_average(closes, 10)[-1]
    ma20 = moving_average(closes, 20)[-1]
    ma30 = moving_average(closes, 30)[-1]
    if None in (ma5, ma10, ma20, ma30):
        return 0
    score = 0
    if ma5 > ma10: score += 1
    else: score -= 1
    if ma10 > ma20: score += 1
    else: score -= 1
    if ma20 > ma30: score += 1
    else: score -= 1
    return score / 3  # -1 ~ 1
