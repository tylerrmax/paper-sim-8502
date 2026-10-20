#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信号前置校验（2026-10-20）：在信号生成/展示阶段就把无效信号隔离，
不再让单条无效信号把整个系统打成"阻断"。

纯函数，不读 DB，便于单元测试。返回问题列表（空列表 = 合法）。
规则：
  - SELL 必须有持仓（qty > 0）
  - BUY 不得是 display_only 标的
  - 已知数量时，数量必须 > 0
  - 价格必须 > 0
  - 止盈卖出价不得低于现价（低于现价说明信号价已失效）
  - 买入止损价必须低于买入价
"""
from __future__ import annotations

ISOLATED_STATUS = "已隔离"


def _norm(sym):
    s = str(sym).strip() if sym is not None else ""
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def check_signal(side, symbol, position_qty=0.0, display_only=(), qty=None, price=None,
                 current_price=None, action_type=None, stop_price=None):
    issues = []
    sym = _norm(symbol)
    disp = {_norm(x) for x in (display_only or [])}
    if side == "SELL" and not (position_qty and position_qty > 0):
        issues.append("无持仓，不能卖出")
    if side == "BUY" and sym in disp:
        issues.append("只展示(display_only)标的，禁止买入")
    if qty is not None and not qty > 0:
        issues.append("数量缺失或为 0")
    if price is None or not price > 0:
        issues.append(f"价格无效 {price}")
    issues.extend(check_exit_price(side, price, current_price, action_type, stop_price))
    return issues


def check_exit_price(side, price, current_price=None, action_type=None, stop_price=None):
    """止盈/止损价与现价的合理性校验。"""
    issues = []
    if not price or price <= 0:
        return issues
    if side == "SELL" and action_type == "止盈" and current_price and current_price > 0 \
            and price < current_price - 1e-9:
        issues.append(f"止盈价 {price:.2f} 低于现价 {current_price:.2f}，信号价已失效")
    if side == "BUY" and stop_price is not None and stop_price >= price - 1e-9:
        issues.append(f"止损价 {stop_price:.2f} 不低于买入价 {price:.2f}")
    return issues


def risk_breaches(nav, position_value, top_symbol_value, pos_cap=0.70, conc_cap=0.40):
    """账户级风控：返回超限描述列表（非空即禁止新 BUY）。"""
    out = []
    if not nav or nav <= 0:
        return out
    pr = (position_value or 0) / nav
    cr = (top_symbol_value or 0) / nav
    if pr > pos_cap + 1e-9:
        out.append(f"总仓位 {pr*100:.1f}% 超过上限 {pos_cap*100:.0f}%")
    if cr > conc_cap + 1e-9:
        out.append(f"单标的集中度 {cr*100:.1f}% 超过上限 {conc_cap*100:.0f}%")
    return out
