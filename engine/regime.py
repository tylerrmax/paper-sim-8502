#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场环境状态机（模块A，2026-10-26 定版；工作流纲领第 1 步）。

最终设计 = **同口径自动计算 + 母板 B31 每日对账**（同一条规则的两种
执行/校验方式，不是两套规则）：
- 自动计算：上证指数收盘 vs MA20，用与个股同源（腾讯）、同交易日的
  指数 bars（market_data 的 IDX000001）计算；口径与 B31 定义严格
  一致（收盘站上 MA20 = 可进攻）。这是判定执行源。
- 每日对账：自动结果与 workbook_map 抽出的母板手填 B31 逐日比对；
  **不一致、任一侧缺失、或数据过期（指数最新日落后于个股市场最新
  日）→ degraded：顶部 BLOCKED + 按禁开处理**。对账结果（计算值/
  B31 值/是否一致/指数日期/MA20 数值）写入日报与 health，可查。
- 对账一致时，verdict = 自动计算值（与 B31 相等），再与母板放行
  状态（实盘验证!E14）、B79（参数设置!B79）组合出三态。

三态（不引入新阈值）：
- attack     允许进攻：verdict=站上 且 已放行 → 正常口径；
- cautious   谨慎交易：verdict=站上、未放行、B79=是 → 允许新建仓，
             但套母板验证期参数（参数设置 B78 买入仓位系数 0.5、
             B80 有效总仓位上限 0.3，由本状态机给出、仓位模块只消费）；
- blocked_new 禁止新开仓：verdict=未站上；或 未放行且 B79=否。禁的是
             “开新仓”，不禁换仓（先卖后买，额度逻辑在 pipeline）；
             SELL 永不受环境闸影响；
- degraded   安全降级（fail-closed）：对账不一致/缺失/过期、母板字段
             读取失败、workbook_map 缺失或过期 → 按禁止新开仓处理，
             绝不读不到就默认放行。

状态每跑批按当日数据重算，不缓存、不 sticky：环境恢复（指数站回
MA20 且对账一致）后硬闸按规则自动解除。
港股不另设环境闸：用户券商操作 A/H 同步，环境以母板口径统一判定。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(BASE, "config", "workbook_map.json")
INDEX_SYMBOL = "IDX000001"
INDEX_NAME = "上证指数"
MA_WINDOW = 20

ATTACK = "attack"
CAUTIOUS = "cautious"
BLOCKED_NEW = "blocked_new"
DEGRADED = "degraded"
STATE_LABELS = {
    ATTACK: "允许进攻",
    CAUTIOUS: "谨慎交易",
    BLOCKED_NEW: "禁止新开仓",
    DEGRADED: "禁止新开仓（数据不可用·安全降级）",
}


def default_xlsx_path():
    """母板 xlsx 路径（与 build_workbook_map 默认源一致）。"""
    env = os.environ.get("PAPER_SIM_XLSX")
    if env:
        return env
    return os.path.expanduser(
        "~/workspace/your_files/交易母版V8.2.1-Final.xlsx")


def _degraded(problems, fields=None):
    return {"state": DEGRADED, "label": STATE_LABELS[DEGRADED],
            "b31": None, "b79": None, "released": None,
            "buy_coef": None, "total_cap": None,
            "degraded_reason": "；".join(problems), "problems": problems,
            "fields": fields or {}}


def auto_regime(con, asof=None):
    """同源同日自动计算：读 market_data 的 IDX000001 bars（≤asof，
    或全部）→ {known, on, close, ma20, n, last_date}。
    与 B31 同一条规则：收盘 > MA20 = 站上。bars 不足 20 根 → known=False
    （对账侧视为缺失 → 降级，不臆测）。"""
    if asof:
        rows = con.execute(
            "SELECT date, close FROM market_data WHERE symbol=? AND date<=?"
            " ORDER BY date", (INDEX_SYMBOL, str(asof)[:10])).fetchall()
    else:
        rows = con.execute(
            "SELECT date, close FROM market_data WHERE symbol=?"
            " ORDER BY date", (INDEX_SYMBOL,)).fetchall()
    out = regime_from_closes([r[1] for r in rows])
    out["last_date"] = rows[-1][0] if rows else None
    return out


def env_from_map(wmap, xlsx_path=None, auto=None, stock_last_date=None):
    """由 workbook_map dict + 自动计算结果判定环境态（fail-closed）。

    auto 为 auto_regime() 的返回（None = 本次无 DB 上下文，仅按母板
    字段判定，对账记 reconciled=None）；stock_last_date 为个股市场
    最新交易日（过期判定：指数最新日落后于它 → 数据过期 → 降级）。
    xlsx_path 用于映射过期判定：母板文件 mtime 晚于 map 的
    generated_at → 映射已过期 → 降级。xlsx 不存在时不以此判过期
    （部署环境可能只有 map），但字段缺失仍降级。"""
    if not wmap or not isinstance(wmap, dict):
        return _degraded(["workbook_map 缺失或不可读"])
    fields = {
        "b31": wmap.get("b31_manual") or {},
        "b79": wmap.get("gate_b79") or {},
        "release": wmap.get("release") or {},
        "v8_risk": wmap.get("v8_risk") or {},
        "generated_at": wmap.get("generated_at"),
        "source": wmap.get("source"),
        "source_sha256": wmap.get("source_sha256"),
    }
    problems = []
    # ---- 过期判定 ----
    gen_raw = wmap.get("generated_at")
    gen_dt = None
    if gen_raw:
        try:
            gen_dt = datetime.strptime(str(gen_raw)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            problems.append("workbook_map generated_at 不可解析（%s）" % gen_raw)
    else:
        problems.append("workbook_map 缺 generated_at")
    xp = xlsx_path or default_xlsx_path()
    if gen_dt is not None and xp and os.path.exists(xp):
        if os.path.getmtime(xp) > gen_dt.timestamp() + 1:
            problems.append("母板映射已过期：母板文件更新于 %s，映射生成于 %s"
                            % (datetime.fromtimestamp(
                                os.path.getmtime(xp)).strftime("%Y-%m-%d %H:%M:%S"),
                               gen_raw))
    # ---- 三字段 ----
    b31 = fields["b31"].get("value")
    b79 = fields["b79"].get("value")
    released = fields["release"].get("released")
    if b31 is None:
        problems.append("母板 B31（大盘指数站上20日线）读取失败")
    if b79 is None:
        problems.append("母板 B79（未放行时允许新开/接回买入）读取失败")
    if released is None:
        problems.append("母板放行状态（实盘验证 E14）读取失败")
    # ---- 每日对账：自动计算 vs 母板 B31 ----
    reconciled = None
    if auto is not None:
        if not auto.get("known"):
            problems.append("指数数据不足：自动计算不可用（bars %d 根，"
                            "指数最新日 %s）" % (auto.get("n", 0),
                                                 auto.get("last_date")))
        elif stock_last_date and auto.get("last_date") and \
                auto["last_date"] < stock_last_date:
            problems.append("指数数据过期：指数最新日 %s 落后于个股市场"
                            "最新日 %s" % (auto["last_date"], stock_last_date))
        elif b31 is not None:
            reconciled = bool(auto["on"]) == bool(b31)
            if not reconciled:
                problems.append(
                    "环境口径不一致：自动计算（上证收盘 %.2f vs MA20 %.2f → %s，"
                    "指数日 %s）vs 母板B31（%s）"
                    % (auto["close"], auto["ma20"],
                       "站上20日线" if auto["on"] else "未站上20日线",
                       auto.get("last_date"),
                       "是" if b31 else "否"))
    if problems:
        out = _degraded(problems, fields)
        out["auto"] = auto
        out["reconciled"] = reconciled
        out["stock_last_date"] = stock_last_date
        return out
    # 对账一致时 verdict = 自动计算值（与 B31 相等）；无 DB 上下文时
    # verdict = B31。判定执行源是自动计算，对账是其守门。
    verdict = bool(auto["on"]) if (auto and auto.get("known")) else bool(b31)
    # ---- 三态组合（不引入新阈值） ----
    out = {"b31": b31, "b79": b79, "released": released, "verdict": verdict,
           "auto": auto, "reconciled": reconciled,
           "stock_last_date": stock_last_date,
           "buy_coef": None, "total_cap": None,
           "degraded_reason": None, "problems": [], "fields": fields}
    if verdict is False:
        if auto and auto.get("known"):
            _rsn = ("自动计算与母板B31对账一致：上证未站上20日线"
                    "（收盘 %.2f < MA20 %.2f，指数日 %s），B31=否"
                    % (auto["close"], auto["ma20"], auto.get("last_date")))
        else:
            _rsn = "B31=否（指数未站上20日线）"
        out.update(state=BLOCKED_NEW, label=STATE_LABELS[BLOCKED_NEW],
                   reason=_rsn)
    elif released is False and b79 is False:
        out.update(state=BLOCKED_NEW, label=STATE_LABELS[BLOCKED_NEW],
                   reason="未放行且 B79=否（未放行禁新开）")
    elif released is False and b79 is True:
        v8 = fields["v8_risk"]
        coef = (v8.get("buy_coef") or {}).get("value")
        cap = (v8.get("total_pos_cap") or {}).get("value")
        if coef is None or cap is None:
            out2 = _degraded(
                ["谨慎交易参数（母板 B78 仓位系数 / B80 总仓位上限）读取失败"],
                fields)
            out2["auto"] = auto
            out2["reconciled"] = reconciled
            out2["stock_last_date"] = stock_last_date
            return out2
        out.update(state=CAUTIOUS, label=STATE_LABELS[CAUTIOUS],
                   buy_coef=float(coef), total_cap=float(cap),
                   reason="指数站上20日线且对账一致、未放行、B79=是：套母板验证期参数")
    else:
        out.update(state=ATTACK, label=STATE_LABELS[ATTACK],
                   reason="指数站上20日线且对账一致、已放行")
    return out


def load_market_env(map_path=None, xlsx_path=None, db_path=None, con=None):
    """读 workbook_map + （可选）DB 指数数据，判定环境态。
    文件缺失/损坏、对账不一致/缺失/过期 → degraded（fail-closed）。
    con 优先于 db_path；两者皆无时仅按母板字段判定（reconciled=None，
    供纯映射单元场景使用——生产路径 pipeline/app/screen 均传 DB）。"""
    p = map_path or MAP_PATH
    try:
        with open(p, encoding="utf-8") as f:
            wmap = json.load(f)
    except Exception as e:  # noqa: BLE001 - 读不到一律降级，不放行
        return _degraded(["workbook_map 读取失败（%s）" % e])
    auto, stock_last = None, None
    _con, _own = con, False
    if _con is None and db_path:
        import sqlite3
        try:
            _con = sqlite3.connect(db_path)
            _own = True
        except Exception as e:  # noqa: BLE001
            out = _degraded(["指数数据库读取失败（%s）" % e])
            out["auto"] = None
            return out
    if _con is not None:
        try:
            syms = list((wmap.get("symbols") or {}).keys())
            if syms:
                q = ",".join("?" * len(syms))
                r = _con.execute(
                    "SELECT MAX(date) FROM market_data WHERE symbol IN (%s)"
                    % q, syms).fetchone()
            else:
                r = _con.execute(
                    "SELECT MAX(date) FROM market_data WHERE symbol<>?",
                    (INDEX_SYMBOL,)).fetchone()
            stock_last = r[0] if r else None
            auto = auto_regime(_con)
        except Exception as e:  # noqa: BLE001
            out = _degraded(["指数数据读取失败（%s）" % e])
            out["auto"] = None
            return out
        finally:
            if _own:
                _con.close()
    return env_from_map(wmap, xlsx_path=xlsx_path, auto=auto,
                        stock_last_date=stock_last)


def allows_new_position(env):
    """环境态是否允许新建仓（换仓额度另行处理；降级/禁开 → False）。"""
    return (env or {}).get("state") in (ATTACK, CAUTIOUS)


# ---------- 指数交叉核对（仅展示，不作准入判定） ----------

def regime_from_closes(closes):
    """上证收盘 vs MA20 的交叉核对计算（展示用）。closes 升序。"""
    closes = [c for c in closes if c is not None]
    if len(closes) < MA_WINDOW:
        return {"known": False, "on": None,
                "close": closes[-1] if closes else None,
                "ma20": None, "n": len(closes)}
    ma20 = sum(closes[-MA_WINDOW:]) / MA_WINDOW
    close = closes[-1]
    return {"known": True, "on": close > ma20, "close": close,
            "ma20": ma20, "n": len(closes)}


def regime_for_con(con, on_date, symbol=INDEX_SYMBOL):
    """从 sqlite 连接读 ≤on_date 的指数 bars 做交叉核对（展示用）。"""
    rows = con.execute(
        "SELECT date, close FROM market_data WHERE symbol=? AND date<=?"
        " ORDER BY date DESC LIMIT ?",
        (symbol, str(on_date)[:10], MA_WINDOW)).fetchall()
    closes = [r[1] for r in reversed(rows)]
    out = regime_from_closes(closes)
    out["last_date"] = rows[0][0] if rows else None
    return out


def regime_for_db(db_path, asof):
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        return regime_for_con(con, asof)
    finally:
        con.close()
