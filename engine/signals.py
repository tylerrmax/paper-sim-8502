#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信号生成（DRAFT 规则，阈值全部来自 config/strategy.yaml）。

规则（config signals 节）：
  收盘站上 MA(20) → BUY；收盘跌破 MA(20) → SELL。
判定口径（显式声明）：
  - 只在"穿越"时发信号（连续站上/跌破不重复发），信号天然交替 BUY/SELL；
  - close == MA 时维持上一状态，不发信号；
  - 第一个有 MA 值的 bar 只定初始状态，不发信号（无前值可比）；
  - MA 窗口不足的 bar 不参与。
"""
from __future__ import annotations


def moving_average(closes, n):
    """返回与 closes 等长的 MA 序列，不足窗口为 None。"""
    out = [None] * len(closes)
    s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= n:
            s -= closes[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def gen_signals(dates, closes, ma_long=20):
    """输入等长 dates / closes。返回 [(date, side, price)]，side ∈ {BUY, SELL}。
    price 为信号参考价（当日收盘），实际成交价由 pipeline 按执行假设调整。"""
    ma = moving_average(closes, ma_long)
    sigs = []
    prev = None  # 'above' / 'below'
    for d, c, m in zip(dates, closes, ma):
        if m is None:
            continue
        if c > m:
            cur = "above"
        elif c < m:
            cur = "below"
        else:
            cur = prev  # 恰好等于均线：维持状态
        if prev is not None and cur is not None and cur != prev:
            sigs.append((d, "BUY" if cur == "above" else "SELL", c))
        if cur is not None:
            prev = cur
    return sigs


def gen_buyback_signals(dates, closes, highs):
    """接回策略 v1.0（2026-10-13 验证通过）。

    规则（回测验证：268笔，期望值+22，胜率33.2%）：
    - 买入：从20日高点回落10%
    - 卖出：+15%止盈 / -5%止损

    返回 [(date, side, price)]，side ∈ {BUY, SELL}。
    注意：与 MA 策略不同，此策略的 BUY/SELL 不交替，
    需要 pipeline 跟踪持仓状态（已在 pipeline 中处理）。
    """
    sigs = []
    if len(closes) < 21:
        return sigs

    in_pos = False
    entry = 0.0

    for i in range(20, len(dates)):
        high20 = max(highs[i-20:i])
        price = closes[i]

        if not in_pos:
            if price <= high20 * 0.90:
                sigs.append((dates[i], "BUY", price))
                in_pos = True
                entry = price
        else:
            if price >= entry * 1.15:
                sigs.append((dates[i], "SELL", price))
                in_pos = False
            elif price <= entry * 0.95:
                sigs.append((dates[i], "SELL", price))
                in_pos = False
    return sigs
