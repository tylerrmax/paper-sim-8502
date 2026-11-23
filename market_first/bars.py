#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market-First 行情层：统一 bars 获取（只读，不产任何交易指令）。

缓存隔离（第一轮审计修复 P0-4，硬约束）：
- 调 market_data 包时恒传 use_cache=False，绝不读写生产共享
  ~/workspace/market_data/cache.db；不修改行情包任何文件/配置。
- 研发自有缓存 = market_first 自己的库内 fetched_bars 表：
  取数成功后写入自有表，同日重跑可先查自有表（provenance 标注
  'market_first fetched_bars cache' 以便审计区分）。
- 库路径解析：get_bars(db_path=...) 入参 > configure() 设置 >
  market_first.db.DEFAULT_DB_PATH。

主源：~/workspace/market_data 统一行情包（腾讯/新浪等适配器），
经 get_bars_as_tuples 取 [(date, open, high, low, close, volume), ...]，
date 对象统一转 'YYYY-MM-DD' 字符串，与 engine.screen 的 bars 格式一致
（日期升序、close 在下标 4）。

回退：主源抛异常（含 DataUnavailable）或返回空时，先查自有
fetched_bars 已缓存行，再只读查询生产库 sim.db 的 market_data 表
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

PROV_PACKAGE = "market_data package (tencent/sina, use_cache=False)"
PROV_SIMDB = "sim.db market_data fallback"
PROV_OWN_CACHE = "market_first fetched_bars cache"

_configured_db_path = None

if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)

try:  # 模块级绑定便于检视/替换；实际调用时函数内仍会新鲜导入一次
    from market_data import get_bars_as_tuples as _pkg_get_bars_as_tuples
except Exception:  # noqa: BLE001 - 包不可用时由函数内回退路径处理
    _pkg_get_bars_as_tuples = None


def configure(db_path):
    """设置研发自有库路径（fetched_bars 所在库）。"""
    global _configured_db_path
    _configured_db_path = str(db_path) if db_path else None


def _resolve_db_path(db_path=None):
    if db_path:
        return str(db_path)
    if _configured_db_path:
        return _configured_db_path
    from market_first.db import DEFAULT_DB_PATH
    return DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


# ---------------------------------------------------------------------------
# 研发自有缓存（market_first 库内 fetched_bars 表）
# ---------------------------------------------------------------------------

def _own_conn(db_path):
    from market_first.db import connect, ensure_fix1_schema
    conn = connect(db_path)
    ensure_fix1_schema(conn)
    return conn


def _own_cache_read(symbol, start_str, end_str, db_path):
    """读自有 fetched_bars：返回 (rows, min_date, max_date, fetched_today)。

    rows 为 (date, open, high, low, close, volume) 元组列表（同
    symbol+date 多 source 时取先写入的一行）；读失败返回空。
    """
    try:
        conn = _own_conn(db_path)
        try:
            sql = ("SELECT date, open, high, low, close, volume,"
                   " fetched_at FROM fetched_bars WHERE symbol = ?"
                   " AND date >= ?")
            params = [str(symbol), start_str]
            if end_str:
                sql += " AND date <= ?"
                params.append(end_str)
            sql += " ORDER BY date ASC"
            raw = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 自有缓存不可用不阻断主源
        return [], None, None, False
    by_date = {}
    fetched_today = False
    today = date.today().isoformat()
    for r in raw:
        d = str(r[0])[:10]
        if d not in by_date:
            by_date[d] = (d, float(r[1]), float(r[2]), float(r[3]),
                          float(r[4]), float(r[5]))
        if str(r[6] or "")[:10] == today:
            fetched_today = True
    rows = [by_date[d] for d in sorted(by_date)]
    dates = [r[0] for r in rows]
    return rows, (dates[0] if dates else None), \
        (dates[-1] if dates else None), fetched_today


def _own_cache_write(symbol, bars, source, db_path,
                     start_str=None, end_str=None):
    """取数成功后写入自有 fetched_bars 表（INSERT OR REPLACE），
    并登记 fetched_coverage 覆盖区间（供重跑命中判定）。"""
    if not bars:
        return 0
    try:
        conn = _own_conn(db_path)
        try:
            now = _now_iso()
            conn.executemany(
                "INSERT OR REPLACE INTO fetched_bars "
                "(symbol, date, open, high, low, close, volume,"
                " source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?,"
                " ?, ?)",
                [(str(symbol), str(b[0])[:10], float(b[1]),
                  float(b[2]), float(b[3]), float(b[4]), float(b[5]),
                  source, now) for b in bars])
            if start_str:
                conn.execute(
                    "INSERT OR REPLACE INTO fetched_coverage "
                    "(symbol, start, end, source, fetched_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (str(symbol), start_str, end_str, source, now))
            conn.commit()
            return len(bars)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 写自有缓存失败不阻断取数结果
        return 0


def store_bars(symbol, bars, source, db_path=None):
    """受控落库通道（RT-5，2026-11-23）：把行情写入自有
    fetched_bars 表，返回写入条数。

    get_bars 取数成功时本就经此路径落库；本公开入口供
    validation.evaluate_sample 的 bars 可信校验作依据，并供
    测试夹具模拟生产取数路径（夹具不得直写 fetched_bars
    SQL）。db_path 缺省取 bars 层配置路径。
    """
    own_db = _resolve_db_path(db_path)
    bars = list(bars or [])
    if not bars:
        return 0
    return _own_cache_write(
        symbol, bars, source, own_db,
        start_str=str(bars[0][0])[:10], end_str=str(bars[-1][0])[:10])


def _own_cache_covers(symbol, start_str, end_str, db_path):
    """自有缓存是否已覆盖所请区间：存在 coverage 记录，其
    start <= 所请 start，且（所请 end 非空时）其 end >= 所请
    end；（所请 end 为空时）该记录为今日所取。"""
    try:
        conn = _own_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT start, end, fetched_at FROM fetched_coverage"
                " WHERE symbol = ?", (str(symbol),)).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False
    today = date.today().isoformat()
    for cov_start, cov_end, fetched_at in rows:
        if cov_start > start_str:
            continue
        if end_str:
            if cov_end and cov_end >= end_str:
                return True
        elif str(fetched_at or "")[:10] == today:
            return True
    return False


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


def get_bars(symbol, start, end=None, db_path=None):
    """取 bars -> (bars, provenance_str)。

    bars: [(date_str 'YYYY-MM-DD', open, high, low, close, volume), ...]
    日期升序。start/end 接受 datetime.date 或 'YYYY-MM-DD' 字符串。
    provenance 如实标注来源：自有缓存命中为 PROV_OWN_CACHE，主源
    成功为 PROV_PACKAGE，回退成功为 PROV_SIMDB，两路皆无为
    'unavailable: ...'。主源恒传 use_cache=False（P0-4 隔离）。
    """
    start_str = _to_date_str(start)
    end_str = _to_date_str(end) if end is not None else None
    own_db = _resolve_db_path(db_path)
    pkg_err = None

    # ---- 0. 研发自有缓存（覆盖区间完整才命中） ----
    own_rows, _own_min, _own_max, _ft = _own_cache_read(
        symbol, start_str, end_str, own_db)
    if own_rows and _own_cache_covers(symbol, start_str, end_str,
                                      own_db):
        return own_rows, PROV_OWN_CACHE

    # ---- 1. 主源：market_data 统一行情包（恒 use_cache=False） ----
    try:
        if _WORKSPACE not in sys.path:
            sys.path.insert(0, _WORKSPACE)
        from market_data import get_bars_as_tuples
        raw = get_bars_as_tuples(str(symbol), _to_date(start),
                                 _to_date(end) if end is not None else None,
                                 use_cache=False)
        bars = [(_to_date_str(b[0]), float(b[1]), float(b[2]),
                 float(b[3]), float(b[4]), float(b[5])) for b in (raw or [])]
        bars.sort(key=lambda b: b[0])
        if end_str:
            bars = [b for b in bars if b[0] <= end_str]
        if bars:
            _own_cache_write(symbol, bars, PROV_PACKAGE, own_db,
                             start_str, end_str)
            return bars, PROV_PACKAGE
        pkg_err = "market_data package returned no bars"
    except Exception as e:  # noqa: BLE001 - 含 DataUnavailable，统一回退
        pkg_err = "%s: %s" % (type(e).__name__, e)

    # ---- 2. 回退一：自有缓存已有行（部分覆盖也如实返回） ----
    if own_rows:
        return own_rows, PROV_OWN_CACHE

    # ---- 3. 回退二：生产 sim.db（只读） ----
    try:
        bars = _fallback_from_simdb(symbol, start_str, end_str)
        if bars:
            _own_cache_write(symbol, bars, PROV_SIMDB, own_db,
                             start_str, end_str)
            return bars, PROV_SIMDB
    except Exception as e:  # noqa: BLE001
        return [], ("unavailable: market_data failed (%s); "
                    "sim.db fallback failed (%s: %s)"
                    % (pkg_err, type(e).__name__, e))

    return [], "unavailable: %s; sim.db market_data has no rows" % pkg_err
