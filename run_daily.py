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


def _record_shadow_samples(db):
    """影子样本记录（四项补齐② G2）：主链完成后固定执行——
    ① 选股落库一期（screen_all，record=True）；今日已有落库
       （页面访问先行）则跳过，避免同 asof 双写。
    ② 绩效回填（tools/screen_perf.run）：全集+分组落 screen_perf、
       因子分项落 screen_perf_factors、拦截前向落 blocked_perf。
    只写影子表，与成交链路物理隔离（见 tests/test_shadow_isolation.py）。
    失败由调用方捕获并打印 FAIL 行，不静默、不回滚主链产物。"""
    today_str = str(date.today())
    already = 0
    con = sqlite3.connect(db)
    try:
        already = con.execute(
            "SELECT COUNT(*) FROM screen_runs WHERE asof=?",
            (today_str,)).fetchone()[0]
    except sqlite3.OperationalError:
        already = 0  # 表尚不存在（首跑）→ 下面 screen_all 自行建表落库
    finally:
        con.close()
    if already:
        print("影子记录：今日 screen_runs 已落库（%d 期），跳过选股落库"
              % already)
    else:
        from engine.screen import screen_all
        rows = screen_all(db)
        print("影子记录：screen_all 落库一期（%d 只，asof=%s）"
              % (len(rows), today_str))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "screen_perf_tool", os.path.join(BASE, "tools", "screen_perf.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    summary = mod.run(db)
    print("影子记录：绩效回填完成（全集 %d 条，影子组 %d 条，拦截样本 %d 条）"
          % (summary["total"], summary["groups"]["shadow"],
             summary["blocked"]))


def _check_workbook_map():
    """母板映射核对（四项补齐① 甲2）：从封版母板重新推导映射并与
    config/workbook_map.json 逐项 diff。一致打印一行；不一致打印
    FAIL 行 + 差异摘要（现行 json 绝不自动覆盖，是否重跑生成器
    由人工决定）。失败由调用方捕获打印 FAIL 行，不拖垮主链。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "check_workbook_map_tool",
        os.path.join(BASE, "tools", "check_workbook_map.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ok, diffs, summary = mod.run()
    if ok:
        print("母板映射核对：与封版母板推导结果逐项一致"
              "（标的 %d、板块 %d）"
              % (summary["symbols"], summary["sectors"]))
    else:
        print("FAIL 母板映射核对：%d 项不一致（现行 workbook_map.json "
              "未自动覆盖）：%s" % (len(diffs), "；".join(diffs[:5])))


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

    # 4. 影子样本记录（补齐② G2）：选股落库 + 绩效/因子/拦截前向回填。
    #    在主链产物（日报）落定后执行；失败不得拖垮主链，但必须在
    #    输出里留 FAIL 行供 nightly cron 报告，严禁静默吞掉。
    try:
        _record_shadow_samples(DB)
    except Exception as e:  # noqa: BLE001
        print("FAIL 影子记录（screen/perf）: %s" % e)

    # 5. 母板映射核对（补齐① 甲2）：derive+diff，只报不一致、不自动
    #    覆盖现行映射；失败留 FAIL 行，不拖垮主链产物。
    try:
        _check_workbook_map()
    except Exception as e:  # noqa: BLE001
        print("FAIL 母板映射核对: %s" % e)


if __name__ == "__main__":
    main()
