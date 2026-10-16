#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主源滞后时的备用源补缺测试（data/quotes.py::_fill_gap_from_backup）。
纯内存 mock，不碰网络、不写生产库。
固定"现在"为 2026-10-13（周二）19:00 -> 最近交易日 = 2026-10-13。
"""
import os
import sys
from datetime import date
from unittest import mock

BASE = os.path.expanduser("~/workspace/paper-trading-sim")
sys.path.insert(0, BASE)

import data.quotes as Q  # noqa: E402

NOW_FROZEN = date(2026, 10, 13)  # 周二
EXPECT = date(2026, 10, 13)

ok = []


def check(name, cond, detail=""):
    ok.append(bool(cond))
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))


def row(d, c):
    return (d, c - 1.0, c + 1.0, c - 2.0, c, 1000.0)


def frozen_expected(*a, **k):
    return EXPECT


with mock.patch.object(Q, "_latest_trading_day", frozen_expected):
    # 1. 主源滞后一天，备用源有缺失 bar -> 补上，主源口径优先
    primary = [row(date(2026, 10, 10), 50.0), row(date(2026, 10, 12), 53.72)]
    backup = [row(date(2026, 10, 12), 53.00),  # 重叠日：应保留主源 53.72
              row(date(2026, 10, 13), 54.10)]
    with mock.patch.object(Q, "_fetch_tencent", return_value=primary), \
         mock.patch.object(Q, "_fetch_sina_a", return_value=backup) as sina:
        out = Q.fetch_kline("688305", date(2026, 10, 8))
    check("滞后补缺：补上 10-13", [r[0] for r in out] == [date(2026, 10, 10), date(2026, 10, 12), date(2026, 10, 13)],
          "got=%s" % [str(r[0]) for r in out])
    check("重叠日保留主源收盘 53.72", out[1][4] == 53.72, "got=%s" % out[1][4])
    check("补缺 bar 用备用源 54.10", out[2][4] == 54.10, "got=%s" % out[2][4])
    check("滞后时确实调了备用源", sina.called)

    # 2. 主源已新鲜 -> 不调备用源
    fresh = [row(date(2026, 10, 12), 53.72), row(date(2026, 10, 13), 54.20)]
    with mock.patch.object(Q, "_fetch_tencent", return_value=fresh), \
         mock.patch.object(Q, "_fetch_sina_a") as sina:
        out = Q.fetch_kline("688305", date(2026, 10, 8))
    check("新鲜时不调备用源", not sina.called)
    check("新鲜时原样返回", out == fresh)

    # 3. 主源滞后且备用源也无新数据 -> 原样返回，不崩
    with mock.patch.object(Q, "_fetch_tencent", return_value=primary), \
         mock.patch.object(Q, "_fetch_sina_a", return_value=[]):
        out = Q.fetch_kline("688305", date(2026, 10, 8))
    check("备用源无数据时原样返回", out == primary)

    # 4. 备用源异常 -> 原样返回，不崩
    with mock.patch.object(Q, "_fetch_tencent", return_value=primary), \
         mock.patch.object(Q, "_fetch_sina_a", side_effect=RuntimeError("boom")):
        out = Q.fetch_kline("688305", date(2026, 10, 8))
    check("备用源异常时原样返回", out == primary)

    # 5. 港股不走补缺（_fetch_sina_a 只服务 A股）
    hk_rows = [row(date(2026, 10, 10), 4.0), row(date(2026, 10, 12), 4.03)]
    with mock.patch.object(Q, "_fetch_tencent", return_value=hk_rows), \
         mock.patch.object(Q, "_fetch_sina_a") as sina:
        out = Q.fetch_kline("HK9660", date(2026, 10, 8))
    check("港股不调备用源补缺", not sina.called)
    check("港股原样返回", out == hk_rows)

    # 6. 主源返回空 -> 走原有整体切换路径
    with mock.patch.object(Q, "_fetch_tencent", return_value=[]), \
         mock.patch.object(Q, "_fetch_sina_a", return_value=backup):
        out = Q.fetch_kline("688305", date(2026, 10, 8))
    check("主源空时整体切备用", out == backup)


# 7. 最近交易日口径：周六回退周五；工作日 17 点前用前一天
from datetime import datetime  # noqa: E402
check("周六->周五", Q._latest_trading_day(datetime(2026, 10, 10, 12, 0)) == date(2026, 10, 9))
check("周二17点后->当天", Q._latest_trading_day(datetime(2026, 10, 13, 19, 0)) == date(2026, 10, 13))
check("周一10点->上周五", Q._latest_trading_day(datetime(2026, 10, 12, 10, 0)) == date(2026, 10, 9))

print("\n%d/%d PASS" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
