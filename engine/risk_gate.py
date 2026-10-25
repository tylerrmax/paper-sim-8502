#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
执行层风险禁入闸（P0-1，2026-10-25 用户拍板“禁入优先”）。

口径（唯一基准 = 母板）：
- 信号照常生成（signals.py / pipeline Phase2 不受影响）；
- 执行层拦截：BUY 执行时，信号价或执行价任一跌破母板止损参考
  stop_ref（config/workbook_map.json，源自母板 Watchlist），一律不执行，
  buyback（回落买入）也不例外。被拦信号由 pipeline 写 blocked_signals。
- stop_ref 只认母板：不在 workbook_map 中的标的（如 BTCUSDT）本闸不拦；
  map 中 stop_ref 为空的标的不拦。ATR 止损是策略执行参数，与本闸无关。
- 数据新鲜度：check_fresh() 供调用方按各自市场最新日期校验，过期可拦。

本模块只做判定，不写库、不改状态。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(BASE, "config", "workbook_map.json")

_cache = {"path": None, "mtime": None, "refs": {}}


def load_stop_refs(path=None):
    """{symbol: stop_ref}，只含 stop_ref 非空的标的。带 mtime 缓存。"""
    p = path or MAP_PATH
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return {}
    if _cache["path"] != p or _cache["mtime"] != mtime:
        with open(p, encoding="utf-8") as f:
            wmap = json.load(f)
        _cache["refs"] = {
            sym: meta["stop_ref"]
            for sym, meta in wmap.get("symbols", {}).items()
            if meta.get("stop_ref") is not None}
        _cache["path"], _cache["mtime"] = p, mtime
    return _cache["refs"]


def stop_ref_of(symbol, path=None):
    return load_stop_refs(path).get(symbol)


def _to_date(v):
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def check_buy(symbol, exec_price, sig_price=None, path=None):
    """BUY 禁入判定。返回 (blocked: bool, reason: str, stop_ref: float|None)。

    判定价取信号价与执行价的较低者：信号日收盘已跌破止损的，回升假象
    不放行；执行日跌破的同样拦。价格缺失（None）不拦（由上游数据闸处理）。
    """
    sr = stop_ref_of(symbol, path)
    if sr is None:
        return False, "", None
    cands = [p for p in (sig_price, exec_price) if p is not None]
    if not cands:
        return False, "", sr
    px = min(cands)
    if px < sr:
        return True, ("跌破母板止损参考：%s 判定价 %.2f < stop_ref %.2f"
                      "（禁入优先，buyback 不例外）" % (symbol, px, sr)), sr
    return False, "", sr


def check_fresh(symbol, last_bar_date, ref_date, limit_days=4):
    """数据新鲜度判定。返回 (blocked, reason)。

    last_bar_date 为该标的最新行情日期，ref_date 为参照日期（同市场
    最新日期或执行日）。滞后超过 limit_days 天视为数据过期。
    """
    if last_bar_date is None:
        return True, "%s 无行情数据" % symbol
    gap = (_to_date(ref_date) - _to_date(last_bar_date)).days
    if gap > limit_days:
        return True, ("%s 行情过期：最新 %s 距参照 %s 已 %d 天>%d 天"
                      % (symbol, last_bar_date, ref_date, gap, limit_days))
    return False, ""
