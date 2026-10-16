#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟撮合引擎骨架：费用 / T+1 / 涨跌停。
纯逻辑，易单测；规则阈值全部来自 config/strategy.yaml，不硬编码。
"""
from dataclasses import dataclass, field


@dataclass
class Lot:
    qty: float
    cost: float
    buy_date: str  # YYYY-MM-DD，T+1 检查用


@dataclass
class SimAccount:
    id: str
    ccy: str
    cash: float
    t1: bool = True
    positions: dict = field(default_factory=dict)  # symbol -> [Lot]

    def buy(self, symbol, qty, price, day, fee_rate, min_fee=0.0):
        amount = qty * price
        fee = max(amount * fee_rate, min_fee)
        if self.cash < amount + fee - 1e-9:
            raise ValueError("现金不足: need %.2f have %.2f" % (amount + fee, self.cash))
        self.cash -= amount + fee
        self.positions.setdefault(symbol, []).append(Lot(qty, price, day))
        return fee

    def sell(self, symbol, qty, price, day, fee_rate):
        lots = self.positions.get(symbol, [])
        avail_lots = [l for l in lots if l.qty > 1e-9 and (not self.t1 or l.buy_date < day)]
        avail = sum(l.qty for l in avail_lots)
        if avail < qty - 1e-9:
            raise ValueError("可卖不足(T+1): want %.4f avail %.4f" % (qty, avail))
        left, pnl = qty, 0.0
        for lot in avail_lots:
            if left <= 1e-9:
                break
            take = min(lot.qty, left)
            pnl += take * (price - lot.cost)
            lot.qty -= take
            left -= take
        amount = qty * price
        fee = amount * fee_rate
        self.cash += amount - fee
        self.positions[symbol] = [l for l in lots if l.qty > 1e-9]
        return fee, pnl

    def position_qty(self, symbol, day=None):
        lots = self.positions.get(symbol, [])
        if day and self.t1:
            return sum(l.qty for l in lots if l.buy_date < day)
        return sum(l.qty for l in lots)

    def market_value(self, symbol, price):
        return self.position_qty(symbol) * price


def check_limit(price, prev_close, limit_pct):
    """是否触及涨跌停（A股）。返回 'up' / 'down' / None。"""
    if prev_close <= 0:
        return None
    chg = (price - prev_close) / prev_close
    if chg >= limit_pct - 1e-9:
        return "up"
    if chg <= -limit_pct + 1e-9:
        return "down"
    return None


def calc_fee(side, amount, fee_rate, min_fee=0.0):
    return max(amount * fee_rate, min_fee)
