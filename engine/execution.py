#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
执行桥：连接模拟信号与人工实盘下单。
- 交易日历：A股/港股（周末+法定假日）、加密（7×24）
- 下一交易日计算
- 下单指引生成（信号价 + 建议方式）
- 港股可操作性判断（用户约束：A股休市时港股也无法交易）
- 滑点统计（模拟价 vs 实际成交价）
纯逻辑，易单测。
"""
from datetime import date, timedelta

# 2026 年 A股法定假日（休市）。用户约束是"A股休市时港股也无法交易"，
# 故券商可操作性统一按 A股口径判断。
# 数据来源：国务院办公厅 2026 年放假安排（主要节假日）。
CN_HOLIDAYS_2026 = frozenset([
    # 元旦
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22",
    # 清明
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋+国庆
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
])

# 2026 年港股独立休市日（港股休、A股开）。数据来源：港交所 2026 年交易日历。
HK_HOLIDAYS_2026 = frozenset([
    "2026-04-03",  # 耶稣受难日
    "2026-04-06",  # 复活节星期一
    "2026-04-07",  # 清明节后（港股休市）
    "2026-05-25",  # 佛诞
    "2026-12-25",  # 圣诞节
    "2026-12-28",  # 圣诞节后（港股休市）
])


def is_hk_trading_day(d):
    """港股是否交易日（港交所口径：周末 + 港股独立假日 + A股假日）"""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    if d.weekday() >= 5:
        return False
    iso = d.isoformat()
    if iso in HK_HOLIDAYS_2026 or iso in CN_HOLIDAYS_2026:
        return False
    return True


def is_stock_trading_day(d):
    """A股/港股是否交易日（用户口径）。d: date 或 YYYY-MM-DD 字符串。"""
    if isinstance(d, str):
        d = date.fromisoformat(d)
    if d.weekday() >= 5:  # 周六日
        return False
    if d.isoformat() in CN_HOLIDAYS_2026:
        return False
    return True


def next_trading_day(d, market="stock", account=None):
    """信号日之后的下一个可交易日。
    market: stock=股票, crypto=加密(每天)。
    account=sim_hk 时取交集：A股开且港股开（港股有独立休市日）。
    """
    if isinstance(d, str):
        d = date.fromisoformat(d)
    if market == "crypto":
        return (d + timedelta(days=1)).isoformat()
    is_open = is_hk_trading_day if account == "sim_hk" else is_stock_trading_day
    nxt = d + timedelta(days=1)
    for _ in range(15):  # 最长连休兜底
        if is_open(nxt):
            return nxt.isoformat()
        nxt += timedelta(days=1)
    return nxt.isoformat()


def is_operable(signal_account, exec_date):
    """券商侧是否可操作。用户约束：A股休市时港股也无法交易。

    sim_hk 的信号若执行日撞上 A股休市 → 不可操作。
    sim_a 本身只在交易日产生信号，理论上总是可操作（防御性检查）。
    sim_crypto 7×24 总是可操作。
    返回 (可操作: bool, 原因: str)。
    """
    if signal_account == "sim_crypto":
        return True, ""
    if not is_stock_trading_day(exec_date):
        return False, "A股休市·券商侧不可操作"
    return True, ""


def order_guidance(symbol, side, signal_price, exec_date, operable):
    """下单指引文案。返回 dict: {exec_date, operable, reason, advice}。"""
    if not operable:
        return {"exec_date": exec_date, "operable": False,
                "reason": "A股休市·券商侧不可操作", "advice": "—"}
    if side == "BUY":
        advice = f"限价 {signal_price:.2f}×1.01 买入（或开盘市价）"
    else:
        advice = f"限价 {signal_price:.2f}×0.99 卖出（或开盘市价）"
    # 港股 3 位小数、加密 1 位
    s = symbol.upper()
    if s.startswith("HK"):
        px = f"{signal_price:.3f}"
    elif s.endswith("USDT"):
        px = f"{signal_price:.1f}"
    else:
        px = f"{signal_price:.2f}"
    if side == "BUY":
        advice = f"限价 {px} 上浮1% 买入（或开盘市价）"
    else:
        advice = f"限价 {px} 下浮1% 卖出（或开盘市价）"
    return {"exec_date": exec_date, "operable": True, "reason": "",
            "advice": advice}


def calc_slippage(sim_price, real_price, side):
    """滑点（相对信号价）。BUY: (real-sim)/sim；SELL: (sim-real)/sim。
    正值=成本增加（不利），负值=有利。返回小数。"""
    if not sim_price:
        return None
    if side == "BUY":
        return (real_price - sim_price) / sim_price
    return (sim_price - real_price) / sim_price
