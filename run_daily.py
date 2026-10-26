#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日管线（P0 已落地：拉数据→存DB→净值快照；P1 接入信号+模拟成交）。
用法: python3 run_daily.py [--date YYYY-MM-DD]
A股休市期间只跑港股+加密；10-08 起全量。
"""
import os
import sys
import json
import sqlite3
from datetime import date, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(os.path.dirname(BASE), ""))  # workspace 根，for market_data
try:
    from market_data import get_bars_as_tuples as _md_bars, DataUnavailable
    from market_data.adapters.sina_fx import fetch_hkdcny as _md_fx
    _USE_MD = True
except ImportError:
    _USE_MD = False
    from data.quotes import fetch_kline, fetch_crypto, fetch_hkdcny  # noqa: E402
from engine.pipeline import run as run_pipeline, write_report  # noqa: E402
import yaml  # noqa: E402

DB = os.environ.get("PAPER_SIM_DB", os.path.join(BASE, "db", "sim.db"))

# --- 数据统一迁移：market_data 优先，旧 data.quotes 兜底 ---
if _USE_MD:
    def fetch_kline(code, start):
        return _md_bars(code, start)

    def fetch_crypto(symbol="BTCUSDT", start=None):
        from datetime import timedelta as _td
        start = start or (date.today() - _td(days=365))
        return _md_bars(symbol, start)

    def fetch_hkdcny():
        return _md_fx()
# --- 迁移结束 ---


def init_db():
    os.makedirs(os.path.join(BASE, "db"), exist_ok=True)
    con = sqlite3.connect(DB)
    with open(os.path.join(BASE, "db", "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())
    con.commit()
    return con


def upsert_klines(con, symbol, market, rows):
    con.executemany(
        "INSERT OR REPLACE INTO market_data(symbol,date,open,high,low,close,volume,market)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(symbol, str(d), o, h, l, c, v, market) for d, o, h, l, c, v in rows],
    )


# A股验证冷启动：VAL_START=10-08（见 10-08 复市健康检查任务）。
# sim_a 只从 2026-10-08 起入库，不回填历史——回填会产生 VAL_START 之前的历史成交、
# 污染验证窗口；且 manual 模式下历史信号会变成积压的待审批。港股/加密保持 400 天回填。
A_SHARE_GO_LIVE = date(2026, 10, 8)


def build_plan(cfg, start, today):
    """按 strategy.yaml universe 组装拉取计划，不硬编码标的。
    返回 [(symbol, market, fetch_fn)]；A股在接入日之前整段跳过（INFO，不告警）。"""
    plan = []
    for aid, symbols in cfg.get("universe", {}).items():
        for sym in symbols:
            if aid == "sim_a":
                if today < A_SHARE_GO_LIVE:
                    print("INFO %s：sim_a 接入日 %s 未到，跳过（冷启动，验证从 10-08 起算）"
                          % (sym, A_SHARE_GO_LIVE))
                    continue
                # 腾讯 qfq 接口在 start==最新交易日时可能返回空窗口（如 688305 10-08），
                # 故取稍早窗口再按接入日过滤；入库仍只保留 >= 接入日的行。
                fetch_start = A_SHARE_GO_LIVE - timedelta(days=10)
                plan.append((sym, "a",
                             lambda s=sym, fs=fetch_start:
                                 [r for r in fetch_kline(s, fs) if r[0] >= A_SHARE_GO_LIVE]))
            elif sym.upper().endswith("USDT"):
                plan.append((sym, "crypto",
                             lambda s=sym: fetch_crypto(s, start)))
            else:
                plan.append((sym, "hk",
                             lambda s=sym: fetch_kline(s, start)))
    # 仅展示标的：每日跟随抓取，进 market_data 供终端展示，不进交易宇宙
    for sym in cfg.get("display_only", []):
        plan.append((sym, "a",
                     lambda s=sym: fetch_kline(s, date.today() - timedelta(days=10))))
    # 上证指数（市场环境闸数据，第12项）：display-only，永不进任何
    # universe/信号；走 data.quotes 专用指数函数（腾讯同源），不经
    # market_data 服务（其代码映射会把 IDX 误判为个股）。
    from data.quotes import fetch_index_kline
    from engine.regime import INDEX_SYMBOL
    plan.append((INDEX_SYMBOL, "index",
                 lambda: fetch_index_kline(start)))
    return plan


def main():
    day = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--date" else str(date.today())
    con = init_db()
    start = date.today() - timedelta(days=400)  # MA20 需要足够历史，首次全量回填
    cfg = yaml.safe_load(open(os.path.join(BASE, "config", "strategy.yaml"), encoding="utf-8"))

    plan = build_plan(cfg, start, date.today())
    for symbol, market, fn in plan:
        try:
            rows = fn()
            upsert_klines(con, symbol, market, rows)
            if rows:
                print("OK %s rows=%d last=%s" % (symbol, len(rows), rows[-1][0]))
            else:
                # 空结果≠成功：接口可能失败，明确告警并沿用库内旧数据（不编造）
                print("WARN %s 拉取到 0 行（接口疑似失败），沿用库内旧数据" % symbol)
        except Exception as e:  # noqa: BLE001
            print("FAIL %s: %s" % (symbol, e))

    hkdcny, usdcny = fetch_hkdcny()
    if hkdcny:
        con.execute(
            "INSERT OR REPLACE INTO fx_rate(date,hkdcny,usdcny_est) VALUES(?,?,?)",
            (day, hkdcny, usdcny),
        )
        print("OK HKDCNY=%.4f USDCNY(est)=%.4f" % (hkdcny, usdcny))
    else:
        print("WARN 汇率拉取失败，跳过（总览折算沿用上次值）")

    con.commit()
    n = con.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    print("market_data 总行数=%d" % n)

    # 3. 模拟管线：信号 → 撮合 → 日报（全量重算，幂等；cfg 在文件头已加载）
    asof = run_pipeline(DB, cfg)
    report = write_report(DB, cfg, asof)
    print("模拟管线完成，数据截至 %s" % asof)
    print("日报：%s" % report)
    con.close()


if __name__ == "__main__":
    main()
