#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market-First 交易日历与收盘确认（fix3 P0-A，MF-BENCH-003）。

数据来源与边界（写死）：
- 休市日/半日市表为 2025–2026 年公开口径整理：国务院办公厅
  节假日安排通知与沪深北交易所休市安排（A股/北交所/可转债）、
  香港交易所假期与半日市安排（港股）。表中仅列「工作日休市」
  日期，周末由星期规则处理；港股半日市为农历新年前夕、中秋
  节当日、冬至、圣诞前夕、元旦前夕（收市 12:00）。
- 覆盖年份仅 2025–2026。表外年份或未知市场 is_trading_day
  返回 None（日历未知），bar_close_confirmed 对此回退 fix2 的
  固定时刻时间规则并视为兜底，不得把回退当作日历确认。
- 收盘确认时刻 = 收盘时刻 + 30 分钟缓冲：A股/北交所/可转债
  全日 15:30；港股全日 16:30、半日市 12:30。
- 时区：A股/北交所/可转债 Asia/Shanghai，港股 Asia/Hong_Kong
  （两者同为 UTC+8、无夏令时，但按市场分别取时区计算）。
"""
from __future__ import annotations

from datetime import date, time
from zoneinfo import ZoneInfo

COVERED_YEARS = (2025, 2026)

MARKET_TZ = {
    "SH": "Asia/Shanghai",
    "SZ": "Asia/Shanghai",
    "BJ": "Asia/Shanghai",
    "CB": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
}

_CN_MARKETS = frozenset({"SH", "SZ", "BJ", "CB"})

# A股/北交所/可转债工作日休市（国务院/交易所公开安排整理）
CN_HOLIDAYS = frozenset({
    # 2025：元旦 / 春节 / 清明 / 劳动节 / 端午 / 中秋·国庆
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-03", "2025-02-04",
    "2025-04-04",
    "2025-05-01", "2025-05-02", "2025-05-05",
    "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03",
    "2025-10-06", "2025-10-07", "2025-10-08",
    # 2026：元旦 / 春节 / 清明 / 劳动节 / 端午 / 中秋 / 国庆
    "2026-01-01", "2026-01-02",
    "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20",
    "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
    "2026-10-07",
})

# 港股工作日休市（香港交易所假期表整理）
HK_HOLIDAYS = frozenset({
    # 2025
    "2025-01-01",
    "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-04-04", "2025-04-18", "2025-04-21",
    "2025-05-01", "2025-05-05",
    "2025-07-01",
    "2025-10-01", "2025-10-07", "2025-10-29",
    "2025-12-25", "2025-12-26",
    # 2026
    "2026-01-01",
    "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-04-03", "2026-04-06",
    "2026-05-01", "2026-05-25",
    "2026-06-19",
    "2026-07-01",
    "2026-10-01", "2026-10-19",
    "2026-12-25", "2026-12-28",
})

# 港股半日市（无午市，收市 12:00）：农历新年前夕 / 中秋节
# 当日 / 冬至 / 圣诞前夕 / 元旦前夕。A股无半日市安排。
HK_HALF_DAYS = frozenset({
    "2025-01-28", "2025-10-06", "2025-12-22", "2025-12-24",
    "2025-12-31",
    "2026-02-16", "2026-09-25", "2026-12-22", "2026-12-24",
    "2026-12-31",
})

_CLOSE_CONFIRM_FULL = {"CN": time(15, 30), "HK": time(16, 30)}
_CLOSE_CONFIRM_HALF_HK = time(12, 30)


def market_group(market) -> str | None:
    """市场 -> 日历组 'CN' / 'HK'；未知市场返回 None。"""
    m = str(market or "").upper()
    if m in _CN_MARKETS:
        return "CN"
    if m == "HK":
        return "HK"
    return None


def market_tz(market) -> ZoneInfo | None:
    tz = MARKET_TZ.get(str(market or "").upper())
    return ZoneInfo(tz) if tz else None


def _covered(date_str: str) -> bool:
    try:
        return int(str(date_str)[:4]) in COVERED_YEARS
    except (TypeError, ValueError):
        return False


def is_trading_day(market, date_str) -> bool | None:
    """该市场该日是否为交易日；日历未知（市场未知/年份未
    覆盖）返回 None，调用方必须与 True/False 区分处理。"""
    group = market_group(market)
    if group is None:
        return None
    d = str(date_str)[:10]
    if not _covered(d):
        return None
    try:
        dt = date.fromisoformat(d)
    except ValueError:
        return None
    if dt.weekday() >= 5:
        return False
    holidays = CN_HOLIDAYS if group == "CN" else HK_HOLIDAYS
    return d not in holidays


def is_half_day(market, date_str) -> bool:
    """港股半日市（收市 12:00）；其余市场/年份一律 False。"""
    if market_group(market) != "HK":
        return False
    return str(date_str)[:10] in HK_HALF_DAYS


def close_confirm_time(market, date_str) -> time | None:
    """收盘确认时刻（市场本地时间，收盘 + 30 分钟缓冲）。

    非交易日返回 None；日历未知返回 None（调用方回退 fix2
    固定时刻规则）；港股半日市 12:30、全日 16:30；A股 15:30。
    """
    group = market_group(market)
    if group is None:
        return None
    trading = is_trading_day(market, date_str)
    if trading is None or not trading:
        return None
    if group == "HK" and is_half_day(market, date_str):
        return _CLOSE_CONFIRM_HALF_HK
    return _CLOSE_CONFIRM_FULL[group]
