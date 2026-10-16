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
