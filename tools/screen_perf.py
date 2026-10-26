#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多重校验选股绩效追踪（P2-9）+ 影子样本补齐（四项补齐②，2026-10-27）：
依托 screen_runs 审计表，把每期评分**全集**的前向收益回填到
screen_perf 表，供验证页统计命中率与闸门误杀率。

口径（样本外）：评分参数自 RULE_VERSION 冻结起不回溯修改；只统计
screen_runs 里真实落库的当期名单（事后不可补录、不可挑样本）。
同一 asof 若有多期落库（页面访问与夜跑双写），只取最新一期回填，
避免同日样本重复计数。

分组（grp，三组分开统计、互不混样）：
- sa：未被封顶的 S/A 级（原 P2-9 口径）。
- shadow：被硬闸门封顶的标的（grade 已压到 B、capped=1）——它们恰是
  「闸门是否误杀」的待评样本，单独入册、单独统计。
- other：其余未封顶的 B/C 级，全集登记、留档观察，不进上面两组。

- 基准价 = results_json 中该标的的 as-of 收盘（评分当时所见价格）。
- fwdN_ret = asof 后第 N 根该标的行情的收盘 / 基准价 - 1（不足 N 根留空）。
- fwdN_excess = fwdN_ret - 同期全池（workbook 16 只）平均 fwdN_ret
  （无板块指数可用，以池内平均作相对基准，口径在此声明）。
- 数据不足的字段如实留空（NULL），不插值、不编造。

拦截样本前向回填（G3）：blocked_signals 中被执行层拦截的 BUY，
按执行日后第 5/20 根行情收盘 vs 拦截基准价（exec_price，缺失时
signal_price）算「若成交」收益，落独立影子表 blocked_perf——只读
回填，严禁反哺执行（不写 blocked_signals 事实字段、不碰成交三表）。

本工具只写影子表：screen_perf / screen_perf_factors / blocked_perf。

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
    capped INTEGER DEFAULT 0,
    gates TEXT,
    grp TEXT,
    fwd5_date TEXT, fwd5_ret REAL, fwd5_excess REAL,
    fwd20_date TEXT, fwd20_ret REAL, fwd20_excess REAL,
    updated_at TEXT,
    PRIMARY KEY(run_id, symbol))"""

# 旧库（本工具早期版本建的表）缺列时的补列口径（幂等迁移）。
_PERF_EXTRA_COLS = (("capped", "INTEGER DEFAULT 0"),
                    ("gates", "TEXT"),
                    ("grp", "TEXT"))

# 模块D（2026-10-26）：按因子追踪——候选命中的每个形态因子
# （含负标记）单独一行，承接同一标的的 5/20 日相对收益，供窗口
# 成熟后逐因子评判去留。全部因子为「待验证因子」，本表只记录
# 样本外表现，不构成有效性结论。
# 2026-10-27（补齐②）：落行范围由 S/A 组扩到全集，因子命中与
# 评级解耦记录；grp 列标明样本组别（sa/shadow/other），各因子
# 5/20 日 excess 按组独立统计。
FACTOR_DDL = """CREATE TABLE IF NOT EXISTS screen_perf_factors(
    run_id INTEGER NOT NULL,
    asof TEXT NOT NULL,
    symbol TEXT NOT NULL,
    factor TEXT NOT NULL,
    grp TEXT,
    fwd5_excess REAL,
    fwd20_excess REAL,
    updated_at TEXT,
    PRIMARY KEY(run_id, symbol, factor))"""

_FACTOR_EXTRA_COLS = (("grp", "TEXT"),)

# 拦截样本前向回填（补齐② G3）：与 blocked_signals 以
# (date, account, symbol, side) 对齐；只收 BUY（拦截闸门均为买入
# 侧），SELL 拦截不生成「若成交」样本（如实不记，不用负收益冒充）。
BLOCKED_DDL = """CREATE TABLE IF NOT EXISTS blocked_perf(
    date TEXT NOT NULL,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    signal_date TEXT,
    base_price REAL,
    fwd5_date TEXT, fwd5_ret REAL,
    fwd20_date TEXT, fwd20_ret REAL,
    updated_at TEXT,
    PRIMARY KEY(date, account, symbol, side))"""


def _ensure_columns(con, table, cols):
    """幂等补列：旧库缺列时逐列 ALTER TABLE ADD COLUMN。"""
    existing = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
    for name, decl in cols:
        if name not in existing:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))


def ensure_tables(con):
    """建/补全部影子表（幂等）：新库按新 DDL 建，旧库补列。"""
    con.execute(DDL)
    con.execute(FACTOR_DDL)
    con.execute(BLOCKED_DDL)
    _ensure_columns(con, "screen_perf", _PERF_EXTRA_COLS)
    _ensure_columns(con, "screen_perf_factors", _FACTOR_EXTRA_COLS)


def _group_of(row):
    """样本组别：封顶→shadow；未封顶 S/A→sa；其余→other。"""
    if row.get("capped"):
        return "shadow"
    if row.get("grade") in ("S", "A"):
        return "sa"
    return "other"


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


def _fwd_from_price(bars, asof, base_price, n):
    """以给定基准价算 asof 后第 n 根收盘的收益（拦截样本口径）。"""
    if not base_price:
        return None, None
    after = [(d, c) for d, c in bars if d > asof]
    if len(after) < n:
        return None, None
    d_n, c_n = after[n - 1]
    return d_n, c_n / base_price - 1.0


def backfill_blocked_perf(con, now=None):
    """G3：给 blocked_signals 的 BUY 拦截补 5/20 日前向收益。

    只读 blocked_signals 事实行、只写 blocked_perf 影子表；返回落行数。
    blocked_signals 不存在（管线从未运行过的库）时返回 0。
    """
    con.execute(BLOCKED_DDL)
    if not con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='blocked_signals'").fetchone():
        return 0
    now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bars_cache = {}
    n = 0
    rows = con.execute(
        "SELECT date, signal_date, account, symbol, side, signal_price,"
        " exec_price FROM blocked_signals WHERE side='BUY'").fetchall()
    for bdate, sig_date, acct, sym, side, sig_px, exec_px in rows:
        if sym not in bars_cache:
            bars_cache[sym] = con.execute(
                "SELECT date,close FROM market_data WHERE symbol=?"
                " ORDER BY date", (sym,)).fetchall()
        bars = bars_cache[sym]
        base = exec_px if exec_px is not None else sig_px
        d5, ret5 = _fwd_from_price(bars, bdate, base, 5)
        d20, ret20 = _fwd_from_price(bars, bdate, base, 20)
        con.execute(
            "INSERT OR REPLACE INTO blocked_perf"
            "(date,account,symbol,side,signal_date,base_price,fwd5_date,"
            "fwd5_ret,fwd20_date,fwd20_ret,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (bdate, acct, sym, side, sig_date, base, d5, ret5, d20, ret20,
             now))
        n += 1
    return n


def run(db_path=DEFAULT_DB):
    """全量回填一遍（幂等）。返回摘要 dict，供夜跑与测试复用。"""
    con = sqlite3.connect(db_path)
    ensure_tables(con)
    # 全池 bars（相对基准与个股 fwd 共用）
    symbols = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM market_data")]
    bars = {s: con.execute(
        "SELECT date,close FROM market_data WHERE symbol=? ORDER BY date",
        (s,)).fetchall() for s in symbols}

    try:
        all_runs = con.execute(
            "SELECT id, asof, results_json FROM screen_runs"
            " ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        all_runs = []
    # 同一 asof 只取最新一期（页面/夜跑双写去重，样本不重复计）
    latest = {}
    for run_id, asof, rj in all_runs:
        latest[asof] = (run_id, rj)
    runs = sorted((rid, asof, rj) for asof, (rid, rj) in latest.items())

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_new = n_upd = 0
    grp_counts = {"sa": 0, "shadow": 0, "other": 0}
    for run_id, asof, rj in runs:
        rows = json.loads(rj)
        if not rows:
            continue
        # 同期全池平均 fwd（相对基准）
        pool5 = [v for s in symbols
                 for v in [_fwd(bars[s], asof, 5)[1]] if v is not None]
        pool20 = [v for s in symbols
                  for v in [_fwd(bars[s], asof, 20)[1]] if v is not None]
        avg5 = sum(pool5) / len(pool5) if pool5 else None
        avg20 = sum(pool20) / len(pool20) if pool20 else None
        for r in rows:
            sym = r["symbol"]
            grp = _group_of(r)
            grp_counts[grp] += 1
            d5, ret5 = _fwd(bars.get(sym, []), asof, 5)
            d20, ret20 = _fwd(bars.get(sym, []), asof, 20)
            ex5 = (ret5 - avg5) if ret5 is not None and avg5 is not None \
                else None
            ex20 = (ret20 - avg20) if ret20 is not None and avg20 is not None \
                else None
            cur = con.execute(
                "SELECT fwd5_ret, fwd20_ret FROM screen_perf"
                " WHERE run_id=? AND symbol=?", (run_id, sym)).fetchone()
            con.execute(
                "INSERT OR REPLACE INTO screen_perf"
                "(run_id,asof,symbol,grade,total,base_close,capped,gates,"
                "grp,fwd5_date,fwd5_ret,fwd5_excess,fwd20_date,fwd20_ret,"
                "fwd20_excess,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, asof, sym, r.get("grade"), r.get("total"),
                 r.get("close"), 1 if r.get("capped") else 0,
                 json.dumps(r.get("gates") or [], ensure_ascii=False),
                 grp, d5, ret5, ex5, d20, ret20, ex20, now))
            if cur is None:
                n_new += 1
            elif cur[0] is None or cur[1] is None:
                n_upd += 1
            # 按因子落行（模块D+补齐②）：命中因子（含负标记）逐个
            # 承接同标的的相对收益；与评级解耦，全集落行、按组统计。
            # 旧版本 results_json 无 factors 字段时跳过。
            for _f in (r.get("factors") or {}).get("flags") or []:
                con.execute(
                    "INSERT OR REPLACE INTO screen_perf_factors"
                    "(run_id,asof,symbol,factor,grp,fwd5_excess,"
                    "fwd20_excess,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, asof, sym, _f, grp, ex5, ex20, now))
    n_blocked = backfill_blocked_perf(con, now)
    con.commit()

    tot = con.execute("SELECT COUNT(*) FROM screen_perf").fetchone()[0]

    def _mature(grp):
        return con.execute(
            "SELECT COUNT(*), AVG(fwd5_excess) FROM screen_perf"
            " WHERE grp=? AND fwd5_ret IS NOT NULL", (grp,)).fetchone()

    m_sa = _mature("sa")
    m_sh = _mature("shadow")
    b5 = con.execute(
        "SELECT COUNT(*) FROM blocked_perf WHERE fwd5_ret IS NOT NULL"
    ).fetchone()[0]
    b20 = con.execute(
        "SELECT COUNT(*) FROM blocked_perf WHERE fwd20_ret IS NOT NULL"
    ).fetchone()[0]
    con.close()

    def _pct(v):
        return ("%.2f%%" % (v * 100)) if v is not None else "—"

    print("screen_perf：共 %d 条全集记录（S/A 组 %d、影子组 %d、其他组 %d；"
          "新增 %d、回填更新 %d）"
          % (tot, grp_counts["sa"], grp_counts["shadow"],
             grp_counts["other"], n_new, n_upd))
    print("  S/A 组已满 5 日 %d 条，平均 5 日相对收益 %s；"
          "影子组已满 5 日 %d 条，平均 5 日相对收益 %s"
          % (m_sa[0], _pct(m_sa[1]), m_sh[0], _pct(m_sh[1])))
    print("blocked_perf：共 %d 条拦截样本（已满 5 日 %d、已满 20 日 %d）"
          % (n_blocked, b5, b20))
    return {"total": tot, "groups": grp_counts, "new": n_new,
            "updated": n_upd, "blocked": n_blocked,
            "blocked_fwd5": b5, "blocked_fwd20": b20}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    run(args.db)


if __name__ == "__main__":
    main()
