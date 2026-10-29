#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
机构集体建仓监控 · 影子模块（2026-10-30 用户拍板方案 1 重标定）。

口径（唯一定义，展示与落库都按此标注）：
- 数据源 = 龙虎榜席位明细（东财数据中心公开接口），计数单位是
  「机构专用」席位记录与符合定义的交易日，**不是**独立机构数量：
  「机构专用」是匿名聚合席位，一条记录可能来自多家机构、同一家
  机构也可能拆多条，本模块从不把它换算成"几家机构"。
- 审计边界（用户 2026-10-30 定版，必须保留）：「机构专用席位
  净买」不等于具名机构真实建仓。影子监控的价值在于验证这类公开
  席位数据能否产生稳定、可复核的统计信号，而不是预先假设它
  具有预测能力。
- 两个连续天数口径严格区分：
  * 连续出现交易日 consec_appear_days：在已抓取的交易日序列里，
    从最近一日往回、该股当日有机构专用席位记录（净额任意符号）
    的连续交易日数；某交易日未上榜/无机构专用记录即中断。
  * 连续净买交易日 consec_net_buy_days：同上，但要求当日机构
    专用净额 > 0。
  * 「无记录」与「抓取失败」严格分开：抓取失败的日子只登记
    state='failed'、不落数据；连续天数回溯遇到失败日即停止
    并标 certain=False（已计天数成立、更早未知），绝不把失败
    当真实缺席中断、也不跨越失败日拼接天数。
- 原始记录全量落 inst_lhb_raw（买卖两侧席位明细），聚合可复算。

交易联动边界：本模块只落库、评分展示、盘后评估；绝不生成
signals、绝不创建交易意图、绝不进入排队队列、不改任何现有
策略参数。抓取失败/空响应按 data_health 口径判态并响亮返回，
绝不用旧缓存伪装当日数据。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime

INST_SEAT = "机构专用"
_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _default_get(url, timeout=25):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _report_rows(report, date_str, get, seat_filter=True):
    params = {
        "reportName": report, "columns": "ALL",
        "filter": "(TRADE_DATE='%s')" % date_str,
        "pageNumber": 1, "pageSize": 500,
        "source": "WEB", "client": "WEB",
    }
    out, page = [], 1
    while True:
        params["pageNumber"] = page
        d = get(_API + "?" + urllib.parse.urlencode(params))
        res = d.get("result") or {}
        rows = res.get("data") or []
        out.extend(rows)
        if page >= (res.get("pages") or 1):
            return out
        page += 1


def _name_map(date_str, get):
    """当日龙虎榜汇总给代码→名称（席位明细本身不带个股名称）。"""
    try:
        rows = _report_rows("RPT_DAILYBILLBOARD_DETAILSNEW", date_str, get)
        return {r["SECURITY_CODE"]: r.get("SECURITY_NAME_ABBR", "")
                for r in rows}
    except Exception:  # noqa: BLE001 - 名称只是展示字段，缺失不阻断
        return {}


def _aggregate_events(buys, sells, names):
    """按 (symbol, TRADE_ID 事件) 聚合机构专用席位，再选当日事件。

    金额口径（市场通行）：机构买入额 = 买入明细表机构专用行的
    BUY 合计；机构卖出额 = 卖出明细表机构专用行的 SELL 合计。
    同股同日多事件（一日类 + 连续三日累计类等）金额互相重叠，
    直接相加会重复计：当日值优先取一日类事件（EXPLANATION 不含
    "连续"），同类多事件取 TRADE_ID 最小者；只有累计事件时取
    累计事件并标 window='cum'。seat_*_records = 参与聚合的机构
    专用席位记录条数（匿名席位条数，不是机构家数）。"""
    events = {}

    def _ev(r):
        key = (r["SECURITY_CODE"], str(r.get("TRADE_ID") or ""))
        e = events.setdefault(key, {
            "symbol": r["SECURITY_CODE"],
            "name": names.get(r["SECURITY_CODE"], ""),
            "trade_id": str(r.get("TRADE_ID") or ""),
            "window": "cum" if "连续" in (r.get("EXPLANATION") or "")
                      else "1d",
            "inst_buy": 0.0, "inst_sell": 0.0,
            "seat_buy_records": 0, "seat_sell_records": 0})
        return e

    for r in buys:
        if r.get("OPERATEDEPT_NAME") != INST_SEAT:
            continue
        e = _ev(r)
        e["inst_buy"] += float(r.get("BUY") or 0)
        e["seat_buy_records"] += 1
    for r in sells:
        if r.get("OPERATEDEPT_NAME") != INST_SEAT:
            continue
        e = _ev(r)
        e["inst_sell"] += float(r.get("SELL") or 0)
        e["seat_sell_records"] += 1
    by_symbol = {}
    for e in events.values():
        by_symbol.setdefault(e["symbol"], []).append(e)
    rows = []
    for sym, evs in by_symbol.items():
        evs.sort(key=lambda e: (e["window"] != "1d", e["trade_id"]))
        pick = evs[0]
        pick = dict(pick)
        pick["inst_net"] = pick["inst_buy"] - pick["inst_sell"]
        pick["event_count"] = len(evs)
        rows.append(pick)
    rows.sort(key=lambda x: -x["inst_net"])
    return rows


def fetch_inst_daily(date_str, get=None):
    """抓某日龙虎榜机构专用席位聚合 + 原始席位记录。
    返回 (agg_rows, raw_rows, state, reason)：
    agg_rows = [{symbol, name, trade_id, window, inst_buy,
    inst_sell, inst_net, seat_buy_records, seat_sell_records}]；
    raw_rows = 当日全部席位明细（买卖两侧，含非机构席位，供复算）。
    网络失败→disconnected，结构异常→anomalous。"""
    from engine import data_health
    get = get or _default_get
    try:
        buys = _report_rows("RPT_BILLBOARD_DAILYDETAILSBUY", date_str, get)
        sells = _report_rows("RPT_BILLBOARD_DAILYDETAILSSELL", date_str, get)
    except Exception as e:  # noqa: BLE001
        return [], [], data_health.DISCONNECTED, "龙虎榜接口失败：%s" % e
    names = _name_map(date_str, get)
    raw = ([dict(r, _kind="buy") for r in buys]
           + [dict(r, _kind="sell") for r in sells])
    try:
        rows = _aggregate_events(buys, sells, names)
    except (KeyError, TypeError, ValueError) as e:
        return [], [], data_health.ANOMALOUS, "龙虎榜数据结构异常：%s" % e
    return rows, raw, data_health.NORMAL, \
        "机构专用席位聚合 %d 只（原始席位 %d 条）" % (len(rows), len(raw))


def ensure_inst_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS inst_lhb_daily(
        date TEXT, symbol TEXT, name TEXT,
        inst_buy REAL, inst_sell REAL, inst_net REAL,
        seat_buy_records INTEGER, seat_sell_records INTEGER,
        window TEXT, trade_id TEXT,
        fetched_at TEXT,
        PRIMARY KEY(date, symbol))""")
    con.execute("""CREATE TABLE IF NOT EXISTS inst_lhb_raw(
        date TEXT, kind TEXT, seq INTEGER, symbol TEXT, seat_name TEXT,
        trade_id TEXT, explanation TEXT,
        buy REAL, sell REAL, net REAL,
        PRIMARY KEY(date, kind, seq))""")
    con.execute("""CREATE TABLE IF NOT EXISTS inst_lhb_fetch_days(
        date TEXT PRIMARY KEY, agg_rows INTEGER, raw_rows INTEGER,
        state TEXT, fetched_at TEXT)""")


def store_daily(con, date_str, agg_rows, raw_rows=None, state="normal"):
    """落库一日数据：聚合 + 原始记录 + 抓取日登记（连续天数口径
    以抓取日序列为分母）。返回聚合条数。"""
    ensure_inst_tables(con)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for a in agg_rows:
        con.execute(
            "INSERT OR REPLACE INTO inst_lhb_daily"
            "(date,symbol,name,inst_buy,inst_sell,inst_net,"
            "seat_buy_records,seat_sell_records,window,trade_id,fetched_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (date_str, a["symbol"], a.get("name", ""), a["inst_buy"],
             a["inst_sell"], a["inst_net"], a.get("seat_buy_records"),
             a.get("seat_sell_records"), a.get("window"),
             a.get("trade_id"), now))
    n_raw = 0
    for seq, r in enumerate(raw_rows or []):
        con.execute(
            "INSERT OR REPLACE INTO inst_lhb_raw"
            "(date,kind,seq,symbol,seat_name,trade_id,explanation,"
            "buy,sell,net) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (date_str, r.get("_kind", ""), seq,
             r.get("SECURITY_CODE", ""), r.get("OPERATEDEPT_NAME", ""),
             str(r.get("TRADE_ID") or ""), r.get("EXPLANATION") or "",
             float(r.get("BUY") or 0), float(r.get("SELL") or 0),
             float(r.get("NET") or 0)))
        n_raw += 1
    con.execute(
        "INSERT OR REPLACE INTO inst_lhb_fetch_days"
        "(date,agg_rows,raw_rows,state,fetched_at) VALUES(?,?,?,?,?)",
        (date_str, len(agg_rows), n_raw, state, now))
    return len(agg_rows)


def record_fetch_failure(con, date_str, reason=""):
    """登记抓取失败日（state='failed'）。失败日 ≠ 真实缺席日：
    连续天数回溯遇到失败日即停止计数并标注不确定——不把失败
    当缺席中断已计天数，也不无声跨越失败日拼接更长天数。
    已成功落库的日子不被失败记录降级（INSERT OR IGNORE）。"""
    ensure_inst_tables(con)
    con.execute(
        "INSERT OR IGNORE INTO inst_lhb_fetch_days"
        "(date,agg_rows,raw_rows,state,fetched_at) VALUES(?,?,?,?,?)",
        (date_str, 0, 0, "failed",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def ingest_day(con, date_str, get=None):
    """一日标准入口：抓取成功→落库（聚合+原始+抓取日登记）；
    失败/异常→只登记失败日，不落任何当日数据、不伪装缺席。
    返回 (state, reason)。"""
    from engine import data_health
    rows, raw, state, reason = fetch_inst_daily(date_str, get=get)
    if state == data_health.NORMAL:
        store_daily(con, date_str, rows, raw, state)
    else:
        record_fetch_failure(con, date_str, reason)
    return state, reason


def _consec(con, symbol, upto_date, require_net_positive):
    """返回 (days, certain)。回溯已登记的抓取日序列：
    - 成功日且该股有记录（净买口径另要求净额>0）→ 计数；
    - 成功日但该股无记录/净额不达标 → 真实中断，certain=True；
    - 失败日 → 停止计数，certain=False（已计天数仍成立，
      但更早的天数未知，不拼接、不冒充完整连续）。"""
    ensure_inst_tables(con)
    if upto_date:
        day_rows = con.execute(
            "SELECT date, state FROM inst_lhb_fetch_days WHERE date<=?"
            " ORDER BY date DESC", (upto_date,)).fetchall()
    else:
        day_rows = con.execute(
            "SELECT date, state FROM inst_lhb_fetch_days"
            " ORDER BY date DESC").fetchall()
    nets = dict(con.execute(
        "SELECT date, inst_net FROM inst_lhb_daily WHERE symbol=?",
        (symbol,)).fetchall())
    n = 0
    started = False
    for d, st in day_rows:
        if st == "failed":
            if not started:
                continue  # 最新日抓取失败：以最近成功日为口径起点
            return n, False  # 序列中间失败：停止并标不确定
        started = True
        if d not in nets:
            return n, True  # 真实缺席 → 中断
        if require_net_positive and not nets[d] > 0:
            return n, True
        n += 1
    return n, started


def consecutive_appear_days(con, symbol, upto_date=None):
    """连续出现交易日：已抓取交易日序列中，该股有机构专用
    席位记录（净额任意符号）的连续天数（从最近一日往回数）。"""
    return _consec(con, symbol, upto_date, require_net_positive=False)[0]


def consecutive_net_buy_days(con, symbol, upto_date=None):
    """连续净买交易日：同上，但要求当日机构专用净额 > 0。"""
    return _consec(con, symbol, upto_date, require_net_positive=True)[0]


def consecutive_detail(con, symbol, upto_date=None):
    """两口径天数 + 确定性标注（失败日截断时 certain=False）。"""
    a, ac = _consec(con, symbol, upto_date, require_net_positive=False)
    b, bc = _consec(con, symbol, upto_date, require_net_positive=True)
    return {"appear_days": a, "appear_certain": ac,
            "net_buy_days": b, "net_buy_certain": bc}


def shadow_board(con, min_net_buy_days=2, limit=20):
    """影子榜单（展示/盘后评估口径，绝不产生交易动作）：最新
    抓取日机构专用净买为正、且连续净买交易日 >= 阈值的标的，
    同时给出连续出现交易日，两个口径并列标注。"""
    ensure_inst_tables(con)
    row = con.execute(
        "SELECT MAX(date) FROM inst_lhb_fetch_days"
        " WHERE state<>'failed'").fetchone()
    if not row or not row[0]:
        return []
    latest = row[0]
    out = []
    for sym, name, net in con.execute(
            "SELECT symbol,name,inst_net FROM inst_lhb_daily"
            " WHERE date=? AND inst_net>0 ORDER BY inst_net DESC",
            (latest,)):
        det = consecutive_detail(con, sym, upto_date=latest)
        if det["net_buy_days"] >= min_net_buy_days:
            out.append({
                "date": latest, "symbol": sym, "name": name,
                "inst_net": net,
                "consec_net_buy_days": det["net_buy_days"],
                "consec_appear_days": det["appear_days"],
                "streak_certain": det["appear_certain"]
                and det["net_buy_certain"],
                "caliber": "龙虎榜机构专用席位（匿名聚合，非机构家数）",
            })
    return out[:limit]
