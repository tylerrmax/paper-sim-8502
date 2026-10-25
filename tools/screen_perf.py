#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多重校验选股绩效追踪（P2-9）：依托 screen_runs 审计表，把每期 S/A 名单
的 5/20 日后收益回填到 screen_perf 表，供验证页统计命中率。

口径（样本外）：评分参数自 RULE_VERSION 冻结起不回溯修改；只统计
screen_runs 里真实落库的当期名单（事后不可补录、不可挑样本）。
- 基准价 = results_json 中该标的的 as-of 收盘（评分当时所见价格）。
- fwdN_ret = asof 后第 N 根该标的行情的收盘 / 基准价 - 1（不足 N 根留空）。
- fwdN_excess = fwdN_ret - 同期全池（workbook 16 只）平均 fwdN_ret
  （无板块指数可用，以池内平均作相对基准，口径在此声明）。
- 数据不足的字段如实留空（NULL），不插值、不编造。

用法: python3 tools/screen_perf.py [--db PATH]
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BASE, "db", "sim.db")

DDL = """CREATE TABLE IF NOT EXISTS screen_perf(
    run_id INTEGER NOT NULL,
    asof TEXT NOT NULL,
    symbol TEXT NOT NULL,
    grade TEXT,
    total REAL,
    base_close REAL,
    fwd5_date TEXT, fwd5_ret REAL, fwd5_excess REAL,
    fwd20_date TEXT, fwd20_ret REAL, fwd20_excess REAL,
    updated_at TEXT,
    PRIMARY KEY(run_id, symbol))"""

# 模块D（2026-10-26）：按因子追踪——S/A 候选命中的每个形态因子
# （含负标记）单独一行，承接同一标的的 5/20 日相对收益，供窗口
# 成熟后逐因子评判去留。全部因子为「待验证因子」，本表只记录
# 样本外表现，不构成有效性结论。
FACTOR_DDL = """CREATE TABLE IF NOT EXISTS screen_perf_factors(
    run_id INTEGER NOT NULL,
    asof TEXT NOT NULL,
    symbol TEXT NOT NULL,
    factor TEXT NOT NULL,
    fwd5_excess REAL,
    fwd20_excess REAL,
    updated_at TEXT,
    PRIMARY KEY(run_id, symbol, factor))"""


def _fwd(bars, asof, n):
    """bars: [(date, close)] 升序（全量）。返回 asof 后第 n 根的 (date, ret)。"""
    base = None
    after = []
    for d, c in bars:
        if d <= asof:
            base = c
        else:
            after.append((d, c))
    if base is None or len(after) < n or not base:
        return None, None
    d_n, c_n = after[n - 1]
    return d_n, c_n / base - 1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    con = sqlite3.connect(args.db)
    con.execute(DDL)
    con.execute(FACTOR_DDL)
    # 全池 bars（相对基准与个股 fwd 共用）
    symbols = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM market_data")]
    bars = {s: con.execute(
        "SELECT date,close FROM market_data WHERE symbol=? ORDER BY date",
        (s,)).fetchall() for s in symbols}

    try:
        runs = con.execute(
            "SELECT id, asof, results_json FROM screen_runs ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        runs = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_new = n_upd = 0
    for run_id, asof, rj in runs:
        rows = json.loads(rj)
        picked = [r for r in rows if r.get("grade") in ("S", "A")]
        if not picked:
            continue
        # 同期全池平均 fwd（相对基准）
        pool5 = [v for s in symbols
                 for v in [_fwd(bars[s], asof, 5)[1]] if v is not None]
        pool20 = [v for s in symbols
                  for v in [_fwd(bars[s], asof, 20)[1]] if v is not None]
        avg5 = sum(pool5) / len(pool5) if pool5 else None
        avg20 = sum(pool20) / len(pool20) if pool20 else None
        for r in picked:
            sym = r["symbol"]
            d5, ret5 = _fwd(bars.get(sym, []), asof, 5)
            d20, ret20 = _fwd(bars.get(sym, []), asof, 20)
            cur = con.execute(
                "SELECT fwd5_ret, fwd20_ret FROM screen_perf"
                " WHERE run_id=? AND symbol=?", (run_id, sym)).fetchone()
            con.execute(
                "INSERT OR REPLACE INTO screen_perf"
                "(run_id,asof,symbol,grade,total,base_close,fwd5_date,"
                "fwd5_ret,fwd5_excess,fwd20_date,fwd20_ret,fwd20_excess,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, asof, sym, r.get("grade"), r.get("total"),
                 r.get("close"), d5, ret5,
                 (ret5 - avg5) if ret5 is not None and avg5 is not None else None,
                 d20, ret20,
                 (ret20 - avg20) if ret20 is not None and avg20 is not None else None,
                 now))
            if cur is None:
                n_new += 1
            elif cur[0] is None or cur[1] is None:
                n_upd += 1
            # 按因子落行（模块D）：命中因子（含负标记）逐个承接同标的
            # 的相对收益；旧版本 results_json 无 factors 字段时跳过。
            _ex5 = (ret5 - avg5) if ret5 is not None and avg5 is not None \
                else None
            _ex20 = (ret20 - avg20) if ret20 is not None and avg20 is not None \
                else None
            for _f in (r.get("factors") or {}).get("flags") or []:
                con.execute(
                    "INSERT OR REPLACE INTO screen_perf_factors"
                    "(run_id,asof,symbol,factor,fwd5_excess,fwd20_excess,"
                    "updated_at) VALUES(?,?,?,?,?,?,?)",
                    (run_id, asof, sym, _f, _ex5, _ex20, now))
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM screen_perf").fetchone()[0]
    m5 = con.execute(
        "SELECT COUNT(*), AVG(fwd5_excess) FROM screen_perf"
        " WHERE fwd5_ret IS NOT NULL").fetchone()
    print("screen_perf：共 %d 条 S/A 记录（新增 %d、回填更新 %d）；"
          "已满 5 日 %d 条，平均 5 日相对收益 %s"
          % (tot, n_new, n_upd, m5[0],
             ("%.2f%%" % (m5[1] * 100)) if m5[1] is not None else "—"))
    con.close()


if __name__ == "__main__":
    main()
