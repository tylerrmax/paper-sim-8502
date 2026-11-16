#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market-First 行情层：统一 bars 获取（只读，不产任何交易指令）。

主源：~/workspace/market_data 统一行情包（腾讯/新浪等适配器），
经 get_bars_as_tuples 取 [(date, open, high, low, close, volume), ...]，
date 对象统一转 'YYYY-MM-DD' 字符串，与 engine.screen 的 bars 格式一致
（日期升序、close 在下标 4）。

回退：主源抛异常（含 DataUnavailable）或返回空时，只读查询生产库
~/workspace/paper-trading-sim/db/sim.db 的 market_data 表
（sqlite3 URI mode=ro，绝不写入生产库）。

再无数据：返回 ([], 'unavailable: ...')，provenance 如实标注失败原因，
绝不用旧数据/零值冒充。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime

_WORKSPACE = "/home/hatch/workspace"
_PROD_DB = "/home/hatch/workspace/paper-trading-sim/db/sim.db"

PROV_PACKAGE = "market_data package (tencent/sina)"
PROV_SIMDB = "sim.db market_data fallback"

if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

try:  # 模块级绑定便于检视/替换；实际调用时函数内仍会新鲜导入一次
    from market_data import get_bars_as_tuples as _pkg_get_bars_as_tuples
except Exception:  # noqa: BLE001 - 包不可用时由函数内回退路径处理
    _pkg_get_bars_as_tuples = None


def _to_date_str(v):
    """date/datetime/'YYYY-MM-DD' -> 'YYYY-MM-DD'。"""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def _to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _fallback_from_simdb(symbol, start_str, end_str):
    """只读查生产 sim.db market_data 表，返回 bars 列表（可能为空）。"""
    uri = "file:%s?mode=ro" % _PROD_DB
    con = sqlite3.connect(uri, uri=True)
    try:
        sql = ("SELECT date, open, high, low, close, volume FROM market_data "
               "WHERE symbol = ? AND date >= ?")
        params = [str(symbol), start_str]
        if end_str:
            sql += " AND date <= ?"
            params.append(end_str)
        sql += " ORDER BY date ASC"
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [(str(r[0])[:10], float(r[1]), float(r[2]), float(r[3]),
             float(r[4]), float(r[5])) for r in rows]


def get_bars(symbol, start, end=None):
    """取 bars -> (bars, provenance_str)。

    bars: [(date_str 'YYYY-MM-DD', open, high, low, close, volume), ...]
    日期升序。start/end 接受 datetime.date 或 'YYYY-MM-DD' 字符串。
    provenance 如实标注来源：主源成功为 PROV_PACKAGE，回退成功为
    PROV_SIMDB，两路皆无为 'unavailable: ...'。
    """
    start_str = _to_date_str(start)
    end_str = _to_date_str(end) if end is not None else None
    pkg_err = None

    # ---- 主源：market_data 统一行情包 ----
    try:
        if _WORKSPACE not in sys.path:
            sys.path.insert(0, _WORKSPACE)
        from market_data import get_bars_as_tuples
        raw = get_bars_as_tuples(str(symbol), _to_date(start),
                                 _to_date(end) if end is not None else None)
        bars = [(_to_date_str(b[0]), float(b[1]), float(b[2]),
                 float(b[3]), float(b[4]), float(b[5])) for b in (raw or [])]
        bars.sort(key=lambda b: b[0])
        if end_str:
            bars = [b for b in bars if b[0] <= end_str]
        if bars:
            return bars, PROV_PACKAGE
        pkg_err = "market_data package returned no bars"
    except Exception as e:  # noqa: BLE001 - 含 DataUnavailable，统一回退
        pkg_err = "%s: %s" % (type(e).__name__, e)

    # ---- 回退：生产 sim.db（只读） ----
    try:
        bars = _fallback_from_simdb(symbol, start_str, end_str)
        if bars:
            return bars, PROV_SIMDB
    except Exception as e:  # noqa: BLE001
        return [], ("unavailable: market_data failed (%s); "
                    "sim.db fallback failed (%s: %s)"
                    % (pkg_err, type(e).__name__, e))

    return [], "unavailable: %s; sim.db market_data has no rows" % pkg_err
