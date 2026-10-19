#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 sim.db 的最新状态打包成 pushSnapshot 动作的 JSON payload。
用法：python3 sync_snapshot.py
（脚本内直接原子写入 /tmp/sim_snapshot.json；由 cron agent 读取后调用
 artifact.invoke_action pushsnapshot。不要再用 shell 重定向。）
"""
import json
import os
import sqlite3
import sys
import threading
from datetime import date, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import yaml  # noqa: E402

DB = os.environ.get("PAPER_SIM_DB", os.path.join(BASE, "db", "sim.db"))
# 快照输出路径：生产固定 /tmp/sim_snapshot.json；测试时可覆盖，避免污染生产快照
SNAPSHOT_PATH = os.environ.get("PAPER_SIM_SNAPSHOT", "/tmp/sim_snapshot.json")
CFG = yaml.safe_load(open(os.path.join(BASE, "config", "strategy.yaml"), encoding="utf-8"))
RULE_VERSION = CFG.get("meta", {}).get("version", "unknown")
CUTOFF = (date.today() - timedelta(days=60)).isoformat()
VAL_START = {"sim_crypto": "2026-10-03",  # 2026-10-02 用户拍板：加密提前起算
             "sim_a": "2026-10-08", "sim_hk": "2026-10-08"}
VAL_END = "2026-12-31"
# 验证期起点（分账户）：毕业指标只统计此日期之后的成交


def build_stock_pool(con, accounts):
    """股票池总资产（CNY）：A股NAV + 港股NAV×当日HKDCNY。
    明细仍原生币种记账；合计仅用于整池一览，附汇率标注。"""
    nav_a = next((a["nav"] for a in accounts if a["account"] == "sim_a"), 0.0)
    nav_hk = next((a["nav"] for a in accounts if a["account"] == "sim_hk"), 0.0)
    fx = con.execute("SELECT hkdcny FROM fx_rate ORDER BY date DESC LIMIT 1").fetchone()
    hkdcny = float(fx[0]) if fx and fx[0] else None
    total = round(nav_a + nav_hk * hkdcny, 2) if hkdcny else None
    return {"label": "总资产", "ccy": "CNY", "total_cny": total,
            "hkdcny": hkdcny,
            "components": [
                {"account": "sim_a", "label": "A股", "nav": round(nav_a, 2), "ccy": "CNY",
                 "nav_cny": round(nav_a, 2)},
                {"account": "sim_hk", "label": "港股", "nav": round(nav_hk, 2), "ccy": "HKD",
                 "nav_cny": round(nav_hk * hkdcny, 2) if hkdcny else None}],
            "note": "港股按当日HKDCNY折算；明细仍按原生币种记账"}


def build_rotation_gate():
    """轮动门槛卡：BTC.D（CoinGecko，每日）/ ETH-BTC（Kraken，每日）/
    季节指数（每周一人工研究，标日期）。只读展示，不触发任何交易。"""
    import urllib.request

    def _ujson(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    items = []
    # 1) BTC.D
    btc_d = None
    try:
        g = _ujson("https://api.coingecko.com/api/v3/global")
        btc_d = round(float(g["data"]["market_cap_percentage"]["btc"]), 2)
    except Exception:
        pass
    items.append({"key": "btc_d", "label": "BTC.D", "value": btc_d, "unit": "%",
                  "threshold": "有效跌破55%", "source": "CoinGecko·每日",
                  "status": ("击穿" if btc_d is not None and btc_d < 55 else
                             "接近" if btc_d is not None and btc_d <= 56 else
                             "未达" if btc_d is not None else "暂无"),
                  "met": bool(btc_d is not None and btc_d < 55)})
    # 2) ETH/BTC
    eth_btc = None
    try:
        t = _ujson("https://api.kraken.com/0/public/Ticker?pair=ETHXBT")
        key = next(k for k in t["result"].keys())
        eth_btc = round(float(t["result"][key]["c"][0]), 6)
    except Exception:
        pass
    items.append({"key": "eth_btc", "label": "ETH/BTC", "value": eth_btc, "unit": "",
                  "threshold": "放量突破0.034", "source": "Kraken·每日",
                  "status": ("突破" if eth_btc is not None and eth_btc >= 0.034 else
                             "接近" if eth_btc is not None and eth_btc >= 0.033 else
                             "未达" if eth_btc is not None else "暂无"),
                  "met": bool(eth_btc is not None and eth_btc >= 0.034)})
    # 3) 季节指数（每周一研究写入，标日期）
    season, season_date = None, None
    wp = os.path.join(BASE, "data", "rotation_gate_weekly.json")
    if os.path.exists(wp):
        try:
            w = json.load(open(wp, encoding="utf-8"))
            season, season_date = w.get("season_index"), w.get("date")
        except Exception:
            pass
    items.append({"key": "season", "label": "Altcoin季节指数", "value": season, "unit": "/100",
                  "threshold": "≥75", "source": "每周一研究" + ("·" + season_date if season_date else ""),
                  "status": ("确认" if season is not None and season >= 75 else
                             "接近" if season is not None and season >= 70 else
                             "未达" if season is not None else "暂无（周一更新）"),
                  "met": bool(season is not None and season >= 75)})
    met = sum(1 for i in items if i["met"])
    return {"rule": "三项中满足两项（击穿/确认/突破）才重议是否从BTC轮动",
            "met": met, "total": len(items), "items": items,
            "note": "只看不操作；到线由每周一的轮动观察推送提醒"}


def build_mirror(con):
    """实盘镜像：只读展示，不进验证账。以 USD 计价；CNY 成本按固定汇率一次性
    折算为 USD，此后不再随每日汇率波动（避免每日换算焦虑）。"""
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
        from market_data import get_bars_as_tuples as _md_bars
        from market_data.adapters.sina_fx import fetch_hkdcny as _md_fx
        _USE_MD = True
    except ImportError:
        _USE_MD = False
        from data.quotes import _fetch_kraken, fetch_hkdcny
    FROZEN_FX = 6.6635  # 固定折算汇率：与系统估值口径一致，仅折算一次
    cfg = json.load(open(os.path.join(BASE, "config", "real_positions.json"), encoding="utf-8"))
    fx = con.execute("SELECT usdcny_est FROM fx_rate ORDER BY date DESC LIMIT 1").fetchone()
    usdcny = float(fx[0]) if fx and fx[0] else None
    if not usdcny:
        try:
            _, usdcny = _md_fx() if _USE_MD else fetch_hkdcny()
        except Exception:
            usdcny = None
    btc_close = con.execute(
        "SELECT close FROM market_data WHERE symbol='BTCUSDT' ORDER BY date DESC LIMIT 1").fetchone()
    btc_usd = float(btc_close[0]) if btc_close else None
    out = []
    for p in cfg["positions"]:
        sym = p["symbol"]
        price = None
        try:
            if sym == "BTC":
                price = btc_usd
            elif sym == "DOGE":
                if _USE_MD:
                    rows = _md_bars("DOGEUSDT", date.today() - timedelta(days=3))
                else:
                    rows = _fetch_kraken("DOGEUSD", date.today() - timedelta(days=3))
                price = float(rows[-1][4]) if rows else None
        except Exception:
            price = None
        qty, cost = float(p["quantity"]), float(p["cost_cny"])
        total_cost = qty * cost
        mv = qty * price * usdcny if (price and usdcny) else None
        fpnl = mv - total_cost if mv is not None else None
        # USD 口径（展示用）：成本一次性按固定汇率折算
        cost_usd = round(cost / FROZEN_FX, 6)
        total_cost_usd = round(qty * cost_usd, 2)
        mv_usd = round(qty * price, 2) if price else None
        fpnl_usd = round(mv_usd - total_cost_usd, 2) if mv_usd is not None else None
        out.append({
            "symbol": sym, "qty": qty, "cost_cny": cost, "location": p.get("location"),
            "price_usd": round(price, 6) if price else None, "usdcny": usdcny,
            "total_cost_cny": round(total_cost, 2),
            "market_value_cny": round(mv, 2) if mv is not None else None,
            "floating_pnl_cny": round(fpnl, 2) if fpnl is not None else None,
            "floating_pnl_pct": round(fpnl / total_cost * 100, 2) if fpnl is not None and total_cost else None,
            "cost_usd": cost_usd, "total_cost_usd": total_cost_usd,
            "market_value_usd": mv_usd, "floating_pnl_usd": fpnl_usd,
            "floating_pnl_pct_usd": round(fpnl_usd / total_cost_usd * 100, 2)
            if fpnl_usd is not None and total_cost_usd else None,
        })
    return {"note": "只读镜像，不进验证账", "usdcny": usdcny, "frozen_fx": FROZEN_FX,
            "fx_note": "CNY成本按6.6635一次性折算为USD，此后不随每日汇率波动",
            "positions": out}


def build_execution(con, pending_signals):
    """执行桥：滑点跟踪 + 下单指引 + 港股可操作性。
    - slippage: fills ⨝ real_fills，信号价 vs 实际成交价
    - guidance: 每条待审批信号的下单指引（执行日/是否可操作/建议）
    """
    from engine.execution import (next_trading_day, is_operable,
                                  order_guidance, calc_slippage)
    # 滑点明细：60天窗口内的模拟成交 × 实盘登记（分批成交按数量加权平均）
    slips = []
    for f in con.execute(
            "SELECT f.date,f.account,f.symbol,f.side,f.qty,f.price,"
            " GROUP_CONCAT(r.real_date), GROUP_CONCAT(r.real_price),"
            " GROUP_CONCAT(r.real_qty)"
            " FROM fills f LEFT JOIN real_fills r"
            " ON r.signal_date=f.date AND r.account=f.account"
            " AND r.symbol=f.symbol AND r.side=f.side"
            " WHERE f.date>=? GROUP BY f.date,f.account,f.symbol,f.side"
            " ORDER BY f.date DESC", (CUTOFF,)):
        sim_px = f[5]
        if f[7]:  # 有实盘登记
            prices = [float(x) for x in f[7].split(",")]
            qtys = [float(x) for x in f[8].split(",")]
            tot = sum(qtys)
            wavg = sum(p * q for p, q in zip(prices, qtys)) / tot if tot else None
            real_date = f[6].split(",")[0]
        else:
            wavg, real_date, tot = None, None, None
        slip = calc_slippage(sim_px, wavg, f[3]) if wavg else None
        slips.append({"date": f[0], "account": f[1], "symbol": f[2],
                      "side": f[3], "qty": f[4], "sim_price": sim_px,
                      "real_date": real_date, "real_price": round(wavg, 6) if wavg else None,
                      "real_qty": tot, "batches": len(qtys) if f[7] else 0,
                      "slippage_pct": round(slip * 100, 3) if slip is not None else None})
    # 下单指引：每条待审批信号
    guides = []
    for p in pending_signals:
        acct = p["account"]
        market = "crypto" if acct == "sim_crypto" else "stock"
        exec_date = next_trading_day(p["date"], market, acct)
        ok, _ = is_operable(acct, exec_date)
        g = order_guidance(p["symbol"], p["side"], p["price"], exec_date, ok)
        # 口径声明：可操作性按公开假日表判断，券商实际安排若有差异以券商为准
        g["calendar_note"] = "按公开假日表"
        guides.append({"date": p["date"], "account": acct,
                       "symbol": p["symbol"], "side": p["side"],
                       "signal_price": p["price"], **g})
    return {"slippage": slips, "guidance": guides}


def _paired_round_trips(con, aid):
    """按 FIFO 把 SELL 与其对应的 BUY 配对（同标的、按日期+id 顺序；同日买卖也能配上，分批建仓/平仓按数量消耗）。
    返回 [{date, symbol, pnl, open_date, versions, excluded}]；找不到买入的卖出 open_date=None（数据异常，不计入样本）。"""
    rows = con.execute(
        "SELECT f.date, f.symbol, f.side, f.qty, f.pnl, f.strategy_version,"
        " EXISTS(SELECT 1 FROM graduation_exclusions e WHERE e.date=f.date AND e.account=f.account"
        "        AND e.symbol=f.symbol AND e.side=f.side)"
        " FROM fills f WHERE f.account=? ORDER BY f.date, f.id", (aid,)).fetchall()
    lots, out = {}, []
    for d, sym, side, qty, pnl, ver, excl in rows:
        if side == "BUY":
            lots.setdefault(sym, []).append([float(qty), d, ver])
            continue
        remaining, open_date, versions = float(qty), None, set()
        q = lots.setdefault(sym, [])
        while remaining > 1e-9 and q:
            take = min(remaining, q[0][0])
            open_date = q[0][1] if open_date is None else open_date
            versions.add(q[0][2])
            q[0][0] -= take
            remaining -= take
            if q[0][0] <= 1e-9:
                q.pop(0)
        versions.add(ver)
        out.append({"date": d, "symbol": sym, "pnl": pnl, "open_date": open_date,
                    "versions": versions, "excluded": bool(excl)})
    return out


def build_graduation(con):
    """毕业进度：只统计验证期（10-08起）的已完成 round-trip（SELL）。
    2026-10-16 起：graduation_exclusions 表中的成交从样本中剔除（历史保留，仅不计入毕业指标）。
    2026-10-17 起：按 strategy_version 隔离，策略变更后的新样本不与旧版本混合。
    2026-10-18 起：严格版本隔离（IS NULL 的旧成交排除）；验证期按买入日（开仓日）口径，
    即 SELL 对应的 BUY 日期必须 >= VAL_START，避免验证期前开仓的计入样本。"""
    accts, total_rt = {}, 0
    cur_ver = CFG.get("meta", {}).get("version", "unknown")
    for a in CFG["accounts"]:
        aid = a["id"]
        vs = VAL_START.get(aid, "2026-10-08")
        rts = _paired_round_trips(con, aid)
        # 样本条件：未被排除；卖出与其消耗的全部买入都属于当前策略版本；开仓日(最早被消耗买入)>=验证期起点
        pnls, n_ver_mismatch = [], 0
        for t in rts:
            if t["excluded"] or t["pnl"] is None or t["open_date"] is None or t["open_date"] < vs:
                continue
            if t["versions"] != {cur_ver}:
                n_ver_mismatch += 1
                continue
            pnls.append(float(t["pnl"]))
        n_excluded = sum(1 for t in rts if t["excluded"] and t["date"] >= vs)
        n = len(pnls)
        gp = sum(p for p in pnls if p > 0)
        gl = -sum(p for p in pnls if p < 0)
        wins = [p for p in pnls if p > 0]
        losses = [-p for p in pnls if p < 0]
        # 盈亏比 = 平均盈利 / 平均亏损；盈利因子 = 总盈利 / 总亏损（两者不同，都展示）
        wlr = round((sum(wins) / len(wins)) / (sum(losses) / len(losses)), 2) \
            if wins and losses else None
        pf = round(gp / gl, 2) if gl > 0 else None
        navs = [float(r[0]) for r in con.execute(
            "SELECT nav FROM daily_report WHERE account=? AND symbol='TOTAL' AND date>=? ORDER BY date",
            (aid, vs)).fetchall()]
        peak, mdd = -1.0, 0.0
        for v in navs:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak)
        # 验证期收益：(验证期末净值 - 验证期初净值) / 验证期初净值（与回撤/样本同口径）
        val_return = round((navs[-1] - navs[0]) / navs[0], 4) \
            if navs and navs[0] else None
        accts[aid] = {
            "val_start": vs,
            "round_trips": n,
            "excluded_round_trips": n_excluded,
            "expectancy": round(sum(pnls) / n, 2) if n else None,
            "expectancy_ci_low": (round(sum(pnls) / n - 1.96 * (sum((p - sum(pnls) / n) ** 2 for p in pnls) / (n - 1)) ** 0.5 / n ** 0.5, 2)
                                  if n >= 2 else None),
            "win_rate": round(len(wins) / n, 4) if n else None,
            "version_mismatch_round_trips": n_ver_mismatch,
            "win_loss_ratio": wlr,
            "profit_factor": pf,
            "max_drawdown": round(mdd, 4),
            "val_return": val_return,
        }
        total_rt += n
    return {
        "period": {"start": VAL_START, "end": VAL_END},
        "targets": {"win_loss_ratio": 2.5, "max_drawdown": 0.10,
                    "sample_min": 20, "sample_max": 30, "execution_min": 90},
        # 2026-10-02 用户最终拍板：3倍线落在盈亏比（平均盈利/平均亏损）；
        # 盈利因子（总盈利/总亏损）仅作参考展示，不设硬线。
        "total_round_trips": total_rt,
        "accounts": accts,
        "execution_score": None,
        "execution_note": "执行分由训练系统人工登记",
    }


def atomic_write_json(path, payload):
    """原子写入 JSON：先写临时文件再 os.replace，读取方永远拿到完整文件，
    不会出现写一半的 torn read。临时文件名带 pid+tid，多进程/多线程并发写
    同一目标时互不踩踏（曾实测两进程共用 path.tmp 导致后 replace 者
    FileNotFoundError）；os.replace 本身原子，最后写完者胜出。"""
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    row = con.execute("SELECT status, mode FROM control WHERE id=1").fetchone()
    control = {"status": row["status"], "mode": row["mode"]} if row else {"status": "running", "mode": "auto"}

    asof_row = con.execute("SELECT MAX(date) AS m FROM daily_report").fetchone()
    asof = asof_row["m"] or date.today().isoformat()

    accounts, equity = [], {}
    # 全量已实现盈亏（不受 60 天快照窗口截断影响，前端不得自行对 fills 求和）
    realized_all = {r["account"]: r["s"] for r in con.execute(
        "SELECT account, COALESCE(SUM(pnl),0) AS s FROM fills GROUP BY account")}
    # 待审批信号数：近 60 天窗口内无审批记录、且尚未成交的信号
    #（已成交的是历史事实，不应再列为待审批）
    pending_all = {r["account"]: r["c"] for r in con.execute(
        "SELECT s.account AS account, COUNT(*) AS c FROM signals s"
        " LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account"
        " AND ap.symbol=s.symbol AND ap.side=s.side"
        " LEFT JOIN fills f ON f.signal_date=s.date AND f.account=s.account"
        " AND f.symbol=s.symbol AND f.side=s.side"
        " WHERE s.date>=? AND ap.date IS NULL AND f.signal_date IS NULL GROUP BY s.account", (CUTOFF,))}
    # 待审批信号明细（供网页版行内批准/跳过；artifact 忽略本字段）
    pending_signals = [dict(r) for r in con.execute(
        "SELECT s.account AS account, s.date AS date, s.symbol AS symbol,"
        " s.side AS side, s.price AS price FROM signals s"
        " LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account"
        " AND ap.symbol=s.symbol AND ap.side=s.side"
        " LEFT JOIN fills f ON f.signal_date=s.date AND f.account=s.account"
        " AND f.symbol=s.symbol AND f.side=s.side"
        " WHERE s.date>=? AND ap.date IS NULL AND f.signal_date IS NULL"
        " ORDER BY s.date DESC, s.account", (CUTOFF,))]
    for a in CFG["accounts"]:
        aid = a["id"]
        r = con.execute(
            "SELECT nav,cash,position_value,pnl_day,ret_total FROM daily_report"
            " WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (aid,)).fetchone()
        base = {"account": aid, "ccy": a["ccy"],
                "realized_pnl": round(float(realized_all.get(aid, 0.0)), 2),
                "pending_approvals": int(pending_all.get(aid, 0))}
        if r:
            base.update({"nav": r["nav"], "cash": r["cash"],
                         "position_value": r["position_value"], "pnl_day": r["pnl_day"],
                         "ret_total": r["ret_total"]})
            accounts.append(base)
        else:
            # 尚无数据（如 sim_a 休市中）：以期初本金占位，明确未启动
            base.update({"nav": float(a["principal"]), "cash": float(a["principal"]),
                         "position_value": 0.0, "pnl_day": 0.0, "ret_total": 0.0})
            accounts.append(base)
        equity[aid] = [[x["date"], x["nav"]] for x in con.execute(
            "SELECT date,nav FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date", (aid,))]

    positions = [dict(r) for r in con.execute(
        "SELECT account,symbol,qty,avg_cost,last_price,market_value FROM positions WHERE qty>0")]
    signals = [dict(r) for r in con.execute(
        "SELECT account,date,symbol,side,price FROM signals WHERE date>=? ORDER BY date DESC", (CUTOFF,))]
    fills = [dict(r) for r in con.execute(
        "SELECT account,date,symbol,side,qty,price,fee,pnl FROM fills WHERE date>=? ORDER BY date DESC", (CUTOFF,))]
    # 审批状态（供网页版信号全生命周期展示：待审批/已批准待成交/已跳过）
    approvals = [dict(r) for r in con.execute(
        "SELECT account,date,symbol,side,decision FROM approvals WHERE date>=?", (CUTOFF,))]
    mirror = build_mirror(con)
    graduation = build_graduation(con)
    rotation_gate = build_rotation_gate()
    stock_pool = build_stock_pool(con, accounts)
    execution = build_execution(con, pending_signals)
    con.close()

    payload = {"asof": asof, "control": control, "rule_version": RULE_VERSION,
               "accounts": accounts, "equity": equity,
               "positions": positions, "signals": signals, "fills": fills,
               "real_mirror": mirror, "rotation_gate": rotation_gate,
               "stock_pool": stock_pool, "pending_signals": pending_signals,
               "approvals": approvals, "graduation": graduation,
               "execution": execution}
    # 原子写入：先写临时文件再 rename，避免读取方拿到写一半的 JSON
    # （2026-10-05 曾出现 git-sync 在写入瞬间读取导致解析失败）
    atomic_write_json(SNAPSHOT_PATH, payload)
    print(json.dumps({"asof": asof, "accounts": len(accounts),
                      "equity_points": sum(len(v) for v in equity.values()),
                      "positions": len(positions), "signals": len(signals),
                      "fills": len(fills), "control": control}, ensure_ascii=False))


if __name__ == "__main__":
    main()
