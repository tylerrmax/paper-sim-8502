#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
页面数据新鲜度五态判定（第二层 P2 · Cloud 数据过期强提示的纯函数后端）。

背景：8502 页面此前只在页眉小字显示「数据日期 X · 同步于 Y」。2026-11-02~06
数据停滞 5 天时页面无任何醒目告警，旧数据被静默展示。本模块为展示层提供
与渲染解耦的新鲜度判定：过期/不可用必须强提示，禁止静默展示旧数据或
伪装实时。本模块不参与任何行情计算与交易逻辑。

五态（classify 返回）：
- ok          正常：asof == expected 且今天是交易日；
- closed      休市：今天非交易日且 asof == expected（正常态，非故障）；
- delayed     延迟：asof 落后 expected 恰好 1 个交易日；
- stale       过期：asof 落后 expected ≥ 2 个交易日；
- unavailable 不可用：asof 为 None（读不到数据日期 / 库为空）。

expected（期望数据日期）的定义（与生产链节奏对齐：夜跑 17:12 入库、
17:30 同步发布，故 17:30 为当日数据应到线）：
- 交易日 17:30 后 → 当日；
- 交易日 17:30 前 → 上一交易日；
- 非交易日 → 最近一个交易日。

交易日历：默认复用 engine.execution.is_stock_trading_day（A股口径：
周末 + 2026 年法定假日，与执行桥/管线同一口径，避免两套日历漂移）。
可注入 is_trading_day 函数覆盖；或注入 holidays 集合（此时日历 =
周末规则 + 该集合，集合完全替代默认假日表，便于测试与年度切换）。

约定：now 应传市场时区（Asia/Shanghai）的时间——云端服务器为 UTC，
调用方负责换算；本模块只读取 now 的日期与时刻分量，不做时区推断。
纯函数、无 IO、无 streamlit 依赖。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

OK = "ok"
CLOSED = "closed"
DELAYED = "delayed"
STALE = "stale"
UNAVAILABLE = "unavailable"

# 当日数据应到线：夜跑 17:12 入库、17:30 同步发布之后，当日数据应已到达
CUTOFF = time(17, 30)

_MAX_SCAN_DAYS = 370  # 日历回溯/前进的防御上限（防注入日历恒假时死循环）


def _weekend_calendar(holidays):
    """周末规则 + 给定假日集合（元素可为 date 或 'YYYY-MM-DD' 字符串）。"""
    hol = set()
    for h in holidays or ():
        hol.add(h.isoformat() if isinstance(h, date) else str(h)[:10])

    def _is_td(d):
        return d.weekday() < 5 and d.isoformat() not in hol

    return _is_td


def _resolve_calendar(is_trading_day, holidays):
    if is_trading_day is not None:
        return is_trading_day
    if holidays is not None:
        return _weekend_calendar(holidays)
    try:
        # 默认复用仓内既有交易日工具（与执行桥同一口径）
        from engine.execution import is_stock_trading_day
        return is_stock_trading_day
    except Exception:
        return _weekend_calendar(None)


def _prev_trading_day(d, is_td):
    """d 之前最近的交易日（不含 d）。"""
    cur = d - timedelta(days=1)
    for _ in range(_MAX_SCAN_DAYS):
        if is_td(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


def _last_trading_day_on_or_before(d, is_td):
    cur = d
    for _ in range(_MAX_SCAN_DAYS):
        if is_td(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


def _expected(now, is_td):
    today = now.date()
    if is_td(today):
        if now.time() >= CUTOFF:
            return today
        return _prev_trading_day(today, is_td)
    return _last_trading_day_on_or_before(today, is_td)


def expected_data_date(now, is_trading_day=None, holidays=None):
    """给定当前时刻，页面数据本应到达的日期（见模块 docstring 定义）。"""
    return _expected(now, _resolve_calendar(is_trading_day, holidays))


def _trading_day_lag(asof, expected, is_td):
    """asof 落后 expected 的交易日数（统计 (asof, expected] 内的交易日）。"""
    if asof >= expected:
        return 0
    lag = 0
    cur = asof
    for _ in range(_MAX_SCAN_DAYS):
        cur += timedelta(days=1)
        if is_td(cur):
            lag += 1
        if cur >= expected:
            break
    return lag


def classify(data_asof, now, is_trading_day=None, holidays=None):
    """五态判定。返回 (state, detail)。

    data_asof: 页面数据日期（date / datetime / 'YYYY-MM-DD' / None）。
    now: 市场时区的当前时刻（datetime；naive/aware 均可，只取日期与时刻）。
    detail: {"asof": 'YYYY-MM-DD' 或 None, "expected": 'YYYY-MM-DD',
             "lag_trading_days": int 或 None（不可用时）,
             "today_is_trading_day": bool}
    """
    is_td = _resolve_calendar(is_trading_day, holidays)
    if isinstance(data_asof, datetime):
        data_asof = data_asof.date()
    elif isinstance(data_asof, str):
        data_asof = date.fromisoformat(data_asof[:10]) if data_asof else None
    expected = _expected(now, is_td)
    today_is_td = bool(is_td(now.date()))
    if data_asof is None:
        return UNAVAILABLE, {
            "asof": None,
            "expected": expected.isoformat(),
            "lag_trading_days": None,
            "today_is_trading_day": today_is_td,
        }
    lag = _trading_day_lag(data_asof, expected, is_td)
    detail = {
        "asof": data_asof.isoformat(),
        "expected": expected.isoformat(),
        "lag_trading_days": lag,
        "today_is_trading_day": today_is_td,
    }
    if lag <= 0:
        return (OK if today_is_td else CLOSED), detail
    if lag == 1:
        return DELAYED, detail
    return STALE, detail
