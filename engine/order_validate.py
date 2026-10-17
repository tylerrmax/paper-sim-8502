#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
订单级校验：任何关键数据缺失或不一致时，禁止生成可执行的订单指引。
校验项：标的、账户、买卖方向、实际持仓、可用资金、交易单位、价格有效性、信号日期、风险限额
"""
import sqlite3
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "db", "sim.db")

# 交易单位：A股100股/手，港股按标的（简化：100股），加密0.0001
LOT_SIZE = {
    "sim_a": 100,
    "sim_hk": 100,
    "sim_crypto": 0.0001,
}

def validate_order(account, symbol, side, qty, price, signal_date, db_path=DB):
    """返回 (ok: bool, errors: list[str], warnings: list[str])"""
    errors = []
    warnings = []
    con = sqlite3.connect(db_path)

    # 1. 标的有效性
    if not symbol:
        errors.append("标的代码缺失")
    else:
        exists = con.execute("SELECT 1 FROM market_data WHERE symbol=? LIMIT 1", (symbol,)).fetchone()
        if not exists:
            errors.append(f"标的 {symbol} 无行情数据")

    # 2. 账户有效性
    if account not in ("sim_a", "sim_hk", "sim_crypto"):
        errors.append(f"未知账户 {account}")

    # 3. 买卖方向
    if side not in ("BUY", "SELL"):
        errors.append(f"非法方向 {side}")

    # 4. 价格有效性
    if not price or price <= 0:
        errors.append(f"价格无效 {price}")
    else:
        # 偏离最新收盘价超过20%告警（可能是脏数据）
        row = con.execute("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,)).fetchone()
        if row and row[0]:
            dev = abs(price - row[0]) / row[0]
            if dev > 0.2:
                warnings.append(f"价格偏离收盘价 {dev*100:.1f}%")

    # 5. 信号日期有效性
    if not signal_date:
        errors.append("信号日期缺失")
    else:
        # 信号日期不能是未来
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        if signal_date > today:
            errors.append(f"信号日期 {signal_date} 在未来")

    # 6. 交易单位
    lot = LOT_SIZE.get(account, 1)
    if qty and lot >= 1:
        if abs(qty % lot) > 1e-9:
            errors.append(f"数量 {qty} 不是 {lot} 的整数倍")

    # 7. 实际持仓检查（SELL时）
    if side == "SELL" and symbol:
        pos = con.execute("SELECT qty FROM positions WHERE account=? AND symbol=?", (account, symbol)).fetchone()
        avail = pos[0] if pos else 0
        if qty and qty > avail + 1e-9:
            errors.append(f"卖出 {qty} 超过持仓 {avail}")

    # 8. 可用资金检查（BUY时）
    if side == "BUY" and qty and price:
        need = qty * price * 1.01  # 预留1%费用
        nav_row = con.execute("SELECT cash FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (account,)).fetchone()
        cash = nav_row[0] if nav_row else 0
        if need > cash:
            errors.append(f"需资金 {need:.2f} 超过可用 {cash:.2f}")

    # 9. 风险限额：单笔不超过账户20%
    if qty and price and account:
        nav_row = con.execute("SELECT nav FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (account,)).fetchone()
        nav = nav_row[0] if nav_row else 0
        if nav > 0 and (qty * price / nav) > 0.2:
            warnings.append(f"单笔占比 {(qty*price/nav)*100:.1f}% 超过20%")

    con.close()
    return (len(errors) == 0, errors, warnings)
