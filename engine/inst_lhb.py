#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
机构专用席位连续净买监控 · 影子模块（2026-10-30 用户拍板方案 1 重标定）。

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
  * 「完全未登记」与失败同样处理（2026-10-31 专项整改 P1-01）：
    连续计算前先用可信 A 股交易日历（market_data 的 A 股日期
    集合，或调用方显式传入的日历）与抓取登记做差集检查；区间
    内应有交易日缺登记时不得跨越拼接，受影响结果标
    certain=False 并在 consecutive_detail 里给出 missing_dates
    与原因；缺失日不等于净买为零。交易日历缺失/过期（最新日
    早于最新抓取登记日）时 calendar_ok=False，任何连续结果
    都不得标确定。序列头部（最新侧）的失败/未登记日按既有
    口径跳过、以最近成功日为起点，缺失日期仍如实记录。
- 原始记录全量落 inst_lhb_raw（买卖两侧席位明细），聚合可复算。
- 数据字典（2026-10-31 专项整改 P2-08）：inst_lhb_daily.name
  仅为展示字段（来自当日龙虎榜汇总的名称映射），不参与金额
  计算、净买判断或任何评分；inst_lhb_raw 本身不含名称字段，
  聚合复算不需要也不使用名称。
- 样本时间口径（P2-01）：事件日 2026-10-16~10-29 为 backfill
  段，其时间戳为事后重构、不可冒充真实历史可用时间；正式
  因子研究默认只用 2026-10-30 起具有真实采集时间证据的
  forward 样本，回溯性探索必须单独标识。

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
        state TEXT, fetched_at TEXT, reason TEXT)""")
    # 迁移（可重复执行）：旧表补 reason 列（失败原因供视图展示，
    # 抓取失败必须可追踪——验收口径 2026-10-30）。
    cols = [r[1] for r in con.execute(
        "PRAGMA table_info(inst_lhb_fetch_days)")]
    if cols and "reason" not in cols:
        con.execute(
            "ALTER TABLE inst_lhb_fetch_days ADD COLUMN reason TEXT")


def store_daily(con, date_str, agg_rows, raw_rows=None, state="normal"):
    """落库一日数据：聚合 + 原始记录 + 抓取日登记（连续天数口径
    以抓取日序列为分母）。返回聚合条数。"""
    ensure_inst_tables(con)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 重复抓取收敛：先清当日旧行再写入（同一交易日重复执行不
    # 重复累计，数据源修订/条数变少也以最新一次为准）。
    con.execute("DELETE FROM inst_lhb_daily WHERE date=?", (date_str,))
    con.execute("DELETE FROM inst_lhb_raw WHERE date=?", (date_str,))
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
        "(date,agg_rows,raw_rows,state,fetched_at,reason)"
        " VALUES(?,?,?,?,?,?)",
        (date_str, 0, 0, "failed",
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), reason or ""))
    # 已是失败日时刷新失败原因；成功日不被失败记录降级（上行
    # INSERT OR IGNORE 已保证），此处 UPDATE 只命中 failed 行。
    con.execute(
        "UPDATE inst_lhb_fetch_days SET reason=?"
        " WHERE date=? AND state='failed'", (reason or "", date_str))


def ingest_day(con, date_str, get=None):
    """一日标准入口：抓取成功→落库（聚合+原始+抓取日登记）；
    失败/异常→只登记失败日，不落任何当日数据、不伪装缺席。
    返回 (state, reason)。"""
    from engine import data_health
    rows, raw, state, reason = fetch_inst_daily(date_str, get=get)
    if state == data_health.NORMAL and not rows:
        # 空数据守卫：接口成功但聚合为 0 —— 缺失不能当作零，
        # 更不能覆盖已落库的好数据；按失败登记、保留旧数据。
        reason = "接口返回空数据（疑当日未发布完全）"
        record_fetch_failure(con, date_str, reason)
        return "failed", reason
    if state == data_health.NORMAL:
        store_daily(con, date_str, rows, raw, state)
    else:
        record_fetch_failure(con, date_str, reason)
    return state, reason


def _calendar_dates(con, calendar):
    """可信 A 股交易日历来源：调用方显式传入的日历优先，其次
    market_data 中 market='a' 的 distinct 日期集合（与
    ingest_recent 的目标日来源同源）。返回日期集合（可为空）。"""
    if calendar is not None:
        return {str(d)[:10] for d in calendar}
    try:
        return {r[0] for r in con.execute(
            "SELECT DISTINCT date FROM market_data WHERE market='a'")}
    except Exception:  # noqa: BLE001 — 无 market_data 表（空库/测试库）
        return set()


def _consec_detail(con, symbol, upto_date, require_net_positive,
                   calendar=None):
    """连续天数明细。返回 dict(days, certain, missing_dates,
    calendar_ok, uncertainty)。

    回溯规则（2026-10-31 整改 P1-01，含交易日历差集校验）：
    - 计算区间 = [最早抓取登记日, anchor]，anchor = upto_date
      或最新抓取登记日；区间内的应有交易日以交易日历为准。
    - 日历可信（calendar_ok）：日历非空且其在 anchor 之前的
      最新日期不早于区间内最新抓取登记日（否则日历过期、
      无法覆盖校验区间 → 不可信）。
    - 日历可信时逐「应有交易日」回溯：成功日且该股有记录
      （净买口径另要求净额>0）→ 计数；成功日但无记录/净额
      不达标 → 真实中断，certain=True；失败日或完全未登记日
      → 序列头部（尚未数到成功日）跳过并记录，序列中部则
      停止计数、certain=False、missing_dates/原因如实给出，
      绝不跨越拼接；未登记日不等于净买为零。
    - 日历缺失/不可信时退回已登记序列回溯，但 certain 一律
      False（无法排除未登记交易日被跳过），原因记
      calendar_unavailable。"""
    ensure_inst_tables(con)
    fetch_map = dict(con.execute(
        "SELECT date, state FROM inst_lhb_fetch_days").fetchall())
    info = {"days": 0, "certain": False, "missing_dates": [],
            "calendar_ok": False, "uncertainty": ""}
    if not fetch_map:
        info["uncertainty"] = "no_fetch_days"
        return info
    anchor = upto_date or max(fetch_map)
    win_min = min(fetch_map)
    in_window = {d: st for d, st in fetch_map.items() if d <= anchor}
    if not in_window:
        info["uncertainty"] = "no_fetch_days"
        return info
    cal = _calendar_dates(con, calendar)
    cal_le = [d for d in cal if d <= anchor]
    calendar_ok = bool(cal_le) and max(cal_le) >= max(in_window)
    info["calendar_ok"] = calendar_ok
    nets = dict(con.execute(
        "SELECT date, inst_net FROM inst_lhb_daily WHERE symbol=?",
        (symbol,)).fetchall())
    n = 0
    started = False

    def _hit(d):
        """成功日的计数判定：True=计数，False=真实中断。"""
        if d not in nets:
            return False
        if require_net_positive and not nets[d] > 0:
            return False
        return True

    if calendar_ok:
        expected = sorted((d for d in cal if win_min <= d <= anchor),
                          reverse=True)
        for d in expected:
            st = in_window.get(d)
            if st is None:  # 应有交易日完全没有抓取登记
                info["missing_dates"].append(d)
                if not started:
                    continue  # 序列头部：以最近成功日为口径起点
                info["days"] = n
                info["uncertainty"] = "unregistered_trading_day"
                return info  # 中部缺口：截断，绝不跨越拼接
            if st == "failed":
                if not started:
                    continue  # 序列头部失败：同上
                info["days"] = n
                info["uncertainty"] = "fetch_failed_day"
                return info
            started = True
            if not _hit(d):
                info["days"] = n
                info["certain"] = True  # 真实缺席/净额不达标中断
                return info
            n += 1
        info["days"] = n
        info["certain"] = started
        return info
    # 日历缺失/过期：无法做差集校验，结果一律不确定。
    info["uncertainty"] = "calendar_unavailable"
    for d in sorted(in_window, reverse=True):
        if in_window[d] == "failed":
            if not started:
                continue
            info["days"] = n
            return info
        started = True
        if not _hit(d):
            info["days"] = n
            return info
        n += 1
    info["days"] = n
    return info


def _consec(con, symbol, upto_date, require_net_positive, calendar=None):
    """返回 (days, certain)。语义见 _consec_detail。"""
    det = _consec_detail(con, symbol, upto_date, require_net_positive,
                         calendar=calendar)
    return det["days"], det["certain"]


def consecutive_appear_days(con, symbol, upto_date=None, calendar=None):
    """连续出现交易日：已抓取交易日序列中，该股有机构专用
    席位记录（净额任意符号）的连续天数（从最近一日往回数）。"""
    return _consec(con, symbol, upto_date, require_net_positive=False,
                   calendar=calendar)[0]


def consecutive_net_buy_days(con, symbol, upto_date=None, calendar=None):
    """连续净买交易日：同上，但要求当日机构专用净额 > 0。"""
    return _consec(con, symbol, upto_date, require_net_positive=True,
                   calendar=calendar)[0]


def consecutive_detail(con, symbol, upto_date=None, calendar=None):
    """两口径天数 + 确定性标注。certain=False 的情形：失败日/
    未登记交易日截断（missing_dates 给出未登记日期）、交易
    日历缺失或过期（calendar_ok=False）。"""
    a = _consec_detail(con, symbol, upto_date,
                       require_net_positive=False, calendar=calendar)
    b = _consec_detail(con, symbol, upto_date,
                       require_net_positive=True, calendar=calendar)
    reasons = {r for r in (a["uncertainty"], b["uncertainty"]) if r}
    return {"appear_days": a["days"], "appear_certain": a["certain"],
            "net_buy_days": b["days"], "net_buy_certain": b["certain"],
            "missing_dates": sorted(set(a["missing_dates"])
                                    | set(b["missing_dates"])),
            "calendar_ok": a["calendar_ok"] and b["calendar_ok"],
            "uncertainty": ";".join(sorted(reasons))}


def latest_success_date(con):
    """最近一个成功抓取日（state 非 failed）；无样本返回 None。"""
    ensure_inst_tables(con)
    row = con.execute(
        "SELECT MAX(date) FROM inst_lhb_fetch_days"
        " WHERE state<>'failed'").fetchone()
    return row[0] if row else None


def recent_fetch_status(con, limit=10):
    """近 N 个抓取日的状态（供视图展示新鲜度与失败原因）：
    [{date, state, agg_rows, raw_rows, reason, fetched_at}]，
    按日期倒序。state='failed' 即抓取失败，reason 为失败原因。"""
    ensure_inst_tables(con)
    return [{"date": d, "state": st, "agg_rows": a, "raw_rows": r,
             "reason": rs or "", "fetched_at": fa}
            for d, st, a, r, rs, fa in con.execute(
                "SELECT date,state,agg_rows,raw_rows,reason,fetched_at"
                " FROM inst_lhb_fetch_days ORDER BY date DESC LIMIT ?",
                (limit,))]


def ingest_recent(con, get=None, days=2):
    """夜间抓取入口：对 market_data 中最近 days 个 A 股交易日
    逐日 ingest（当日 + 前一交易日刷新收敛——龙虎榜当日傍晚
    可能未发布完全，次日以最终数据覆盖）。空数据守卫：接口
    返回成功但聚合为 0 时按失败登记（原因=疑未发布），绝不
    把缺失当作零落库污染连续天数分母。返回 [(date,state,
    reason)]；无 A 股行情数据时返回空列表。"""
    ensure_inst_tables(con)
    try:
        targets = [r[0] for r in con.execute(
            "SELECT DISTINCT date FROM market_data WHERE market='a'"
            " ORDER BY date DESC LIMIT ?", (days,))]
    except Exception:  # noqa: BLE001 — 无 market_data 表（空库）
        return []
    out = []
    for d in reversed(targets):
        state, reason = ingest_day(con, d, get=get)
        con.commit()
        out.append((d, state, reason))
    return out


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
