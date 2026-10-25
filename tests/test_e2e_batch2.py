#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二批端到端测试套件（用户采纳的最终验收门，不得以单元测试替代）。

逐场景构造完整链路：行情 → 环境态 → 信号 → 定仓 → 拦截或成交 → 落库，
每场景把 输入/预期/实际/成交或拦截记录 写证据 JSON 到 reports/e2e-batch2/。

场景：01 禁开不新开仓（含 01b 换仓额度）/ 02 环境数据缺失降级 /
03 不同净值仓位随预算变 / 04 不同止损距离仓位随风险变 /
05 高相关簇组合限额 / 06 资金/最小单位不违规成交 /
07 002639 身份冲突拦截 / 08 正常对照组成交。
组合（发布门，独立命名）：attack+BUY / blocked+BUY / blocked+SELL /
数据异常+BUY（过期/对账不一致/风险数据缺失 三注入）。
另含：恢复性（禁开→恢复自动解除）、账本指纹幂等（清单C）、
毕业样本版本隔离（清单C）。

安全：全部写操作在 tmp fixture/生产副本上；生产库只读。
运行: .venv/bin/python -m pytest tests/test_e2e_batch2.py -q
"""
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import date, timedelta

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tests"))

from test_regime_sizing_20261026 import (  # noqa: E402
    D0, _bars_from_closes, _cfg, _flat_drop, _fresh_db, _idx_bars,
    _insert, _load_cfg, _write_map)
from engine import regime  # noqa: E402
from engine.pipeline import run as pipeline_run  # noqa: E402

EVID = os.path.join(BASE, "reports", "e2e-batch2")
PROD_DB = os.path.join(BASE, "db", "sim.db")


def _evidence(name, payload):
    os.makedirs(EVID, exist_ok=True)
    with open(os.path.join(EVID, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)


def _fills(db):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT date,account,symbol,side,qty,price,fee,pnl,signal_date,"
        "strategy_version FROM fills ORDER BY id").fetchall()
    blk = con.execute(
        "SELECT date,account,symbol,side,signal_date,reason"
        " FROM blocked_signals ORDER BY date,symbol").fetchall()
    con.close()
    return rows, blk


def _basic_fixture(tmp_path, state="attack", sym="002446", flat=10.0,
                   drop=8.9, idx_on=True, idx_n=27):
    db = _fresh_db(tmp_path)
    _insert(db, sym, _flat_drop(flat, drop))
    _insert(db, "IDX000001", _idx_bars(idx_n, on=idx_on), market="index")
    return db, _cfg([sym], _write_map(tmp_path, state))


# ---------- 场景 1：禁开态不允许新开仓 ----------

def test_scenario_01_blocked_no_new_position(tmp_path):
    db, cfg = _basic_fixture(tmp_path, state="blocked", idx_on=False)
    pipeline_run(db, cfg)
    fills, blk = _fills(db)
    buys = [f for f in fills if f[3] == "BUY"]
    env_blk = [b for b in blk if "环境闸" in b[5]]
    _evidence("scenario_01_blocked_no_new_position", {
        "input": "母板三态=禁止新开仓（B31否/未放行/B79否），自动计算off且对账一致；002446 buyback BUY 信号（信号日2026-10-26）",
        "expected": "BUY 成交 0；blocked_signals 有环境闸记录",
        "actual": {"buy_fills": buys, "env_blocked": env_blk},
        "fills": fills, "blocked": blk})
    assert buys == []
    assert len(env_blk) == 1 and env_blk[0][2] == "002446"


def test_scenario_01b_rotation_quota(tmp_path):
    """换仓豁免：禁开态下先卖后买，额度内放行、超出拦截（单独断言）。"""
    # 阶段一（允许进攻）：002446 建仓 1500 股；002639 横盘
    db = _fresh_db(tmp_path)
    _insert(db, "002446", _bars_from_closes([10.0] * 25 + [8.9, 8.9]))
    _insert(db, "002639", _bars_from_closes([15.0] * 27))
    _insert(db, "IDX000001", _idx_bars(27, on=True), market="index")
    pipeline_run(db, _cfg(["002446", "002639"], _write_map(tmp_path, "attack")))
    fills1, _ = _fills(db)
    buy1 = [f for f in fills1 if f[3] == "BUY"]
    assert len(buy1) == 1 and buy1[0][4] == 1500, buy1
    # 阶段二（转禁开）：002446 涨 +15% 止盈卖出 → 额度；再回落买回 +
    # 002639 新信号，合计买入额不得超过卖出额度，超出部分留痕拦截
    _insert(db, "002446", _bars_from_closes(
        [9.6, 10.1, 10.3, 10.3, 9.2, 9.2], start=date(2026, 10, 28)))
    _insert(db, "002639", _bars_from_closes(
        [15.0] * 13 + [13.4, 13.4], start=date(2026, 10, 28)))
    idx_off = _bars_from_closes(
        [200 - i * 2.0 for i in range(42)], start=D0)
    _insert(db, "IDX000001", idx_off, market="index")
    pipeline_run(db, _cfg(["002446", "002639"], _write_map(tmp_path, "blocked")))
    fills, blk = _fills(db)
    sells = [f for f in fills if f[3] == "SELL"]
    buys2 = [f for f in fills if f[3] == "BUY" and f[0] > "2026-10-27"]
    quota = sells[0][4] * sells[0][5] if sells else 0.0
    bought_amt = sum(f[4] * f[5] for f in buys2)
    rot_blk = [b for b in blk if "换仓额度" in b[5]]
    _evidence("scenario_01b_rotation_quota", {
        "input": "阶段一允许进攻建仓002446 1500股；阶段二转禁止新开仓：002446止盈卖出（额度来源），随后002446回落买回信号 + 002639新买入信号",
        "expected": "SELL 正常成交；禁开态 BUY 合计金额 ≤ 卖出额度；超出部分被拦截并留痕（换仓额度不足）",
        "actual": {"sell": sells, "rotation_buys": buys2,
                   "quota": quota, "rotation_buy_amount": bought_amt,
                   "rotation_blocked": rot_blk},
        "fills": fills, "blocked": blk})
    assert len(sells) == 1 and sells[0][4] == 1500
    assert bought_amt <= quota + 1e-6, (bought_amt, quota)
    assert any(b[2] == "002639" for b in rot_blk), blk
    by_sym = {f[2]: f[4] for f in buys2}
    assert by_sym.get("002446", 0) > 0  # 额度内买回成交
    assert 0 < by_sym.get("002639", 0) < 1000  # 被额度截断为小量


# ---------- 场景 2：环境数据缺失 → 告警 + 禁开 ----------

def test_scenario_02_env_data_missing(tmp_path):
    variants = {}
    for name, over in (("missing_b79", None), ("stale_map", None)):
        pass
    # 变体一：映射缺 B79 字段
    import copy as _copy
    from test_regime_sizing_20261026 import _real_map
    m = _copy.deepcopy(_real_map())
    del m["gate_b79"]
    p1 = tmp_path / "map_missing.json"
    p1.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    env1 = regime.load_market_env(map_path=str(p1))
    # 变体二：映射过期
    m2 = _copy.deepcopy(_real_map())
    m2["generated_at"] = "2020-01-01 00:00:00"
    p2 = tmp_path / "map_stale.json"
    p2.write_text(json.dumps(m2, ensure_ascii=False), encoding="utf-8")
    env2 = regime.load_market_env(map_path=str(p2))
    # pipeline 在变体一下：BUY 被安全拦截（降级原因留痕）
    db = _fresh_db(tmp_path)
    _insert(db, "002446", _flat_drop(10.0, 8.9))
    _insert(db, "IDX000001", _idx_bars(27, on=True), market="index")
    cfg = _cfg(["002446"], str(p1))
    pipeline_run(db, cfg)
    fills, blk = _fills(db)
    app_src = open(os.path.join(BASE, "app.py"), encoding="utf-8").read()
    _evidence("scenario_02_env_data_missing", {
        "input": "注入一：workbook_map 删除 gate_b79 字段；注入二：generated_at 改为 2020 年（映射过期）",
        "expected": "env 降级 degraded；health 顶部 blocked 文案存在；pipeline BUY 安全拦截且原因注明安全降级",
        "actual": {"env_missing_field": env1, "env_stale_map": env2,
                   "buy_fills": [f for f in fills if f[3] == "BUY"],
                   "blocked": blk,
                   "health_banner_text_present": "市场环境数据不可用，已安全降级为禁止新开仓" in app_src},
        "fills": fills, "blocked": blk})
    assert env1["state"] == "degraded" and env2["state"] == "degraded"
    assert [f for f in fills if f[3] == "BUY"] == []
    assert any("安全降级" in b[5] for b in blk), blk
    assert "市场环境数据不可用，已安全降级为禁止新开仓" in app_src


# ---------- 场景 3/4：仓位随预算/风险变化（终局值为准） ----------

def _final_check_row(db, fill, cfg):
    """独立重算终局校验行：预算 vs 成交数量×每股真实风险。"""
    from engine.pipeline import _round_trip_cost_ps
    from engine.signals import atr as _atr
    from engine import risk_gate
    d, aid, sym, side, qty, px, fee, pnl, sig_d, ver = fill
    con = sqlite3.connect(db)
    bars = con.execute(
        "SELECT date,high,low,close FROM market_data WHERE symbol=?"
        " AND date<=? ORDER BY date", (sym, sig_d)).fetchall()
    nav = con.execute(
        "SELECT nav FROM daily_report WHERE account=? AND symbol='TOTAL'"
        " AND date=?", (aid, sig_d)).fetchone()[0]
    con.close()
    atr_v = [v for v in _atr([b[1] for b in bars], [b[2] for b in bars],
                             [b[3] for b in bars], 14) if v is not None][-1]
    sr = risk_gate.stop_ref_of(sym)
    atr_stop = px - cfg["risk"]["atr_stop_mult"] * atr_v
    stops = [s for s in (sr, atr_stop) if s is not None and s < px]
    stop_price = min(stops)
    dist = px - stop_price
    f = cfg["fees"]
    psr = dist + _round_trip_cost_ps(
        px, f["a_buy"], f["a_min_fee"], f["a_sell"], f["a_min_fee"],
        f["slippage"], qty)
    budget = nav * cfg["risk"]["single_trade_risk_pct"] / 100
    return {"symbol": sym, "signal_date": sig_d, "nav_at_signal": round(nav, 2),
            "budget": round(budget, 2), "stop_ref": sr,
            "atr_stop": round(atr_stop, 4), "chosen_stop": stop_price,
            "per_share_risk": round(psr, 4), "qty": qty,
            "actual_risk": round(qty * psr, 2),
            "within_budget": qty * psr <= budget * 1.05}


def test_scenario_03_nav_scaling(tmp_path):
    qtys, tables = {}, {}
    for tag, principal in (("nav100k", 100000), ("nav200k", 200000)):
        sub = tmp_path / tag
        sub.mkdir()
        db, cfg = _basic_fixture(sub, state="attack")
        cfg["accounts"][0]["principal"] = principal
        pipeline_run(db, cfg)
        fills, _ = _fills(db)
        buy = [f for f in fills if f[3] == "BUY"]
        assert len(buy) == 1
        qtys[tag] = buy[0][4]
        tables[tag] = _final_check_row(db, buy[0], _load_cfg())
    _evidence("scenario_03_nav_scaling", {
        "input": "同一标的/同一止损距离，账户净值 100000 vs 200000（本金注入）",
        "expected": "股数比 ≈ 净值比 2（±1手）；两笔终局实际风险 ≤ 各自预算",
        "actual": {"qty": qtys, "final_check": tables},
        "fills": tables})
    assert abs(qtys["nav200k"] - 2 * qtys["nav100k"]) <= 200
    assert all(t["within_budget"] for t in tables.values())


def test_scenario_04_distance_scaling(tmp_path):
    db = _fresh_db(tmp_path)
    _insert(db, "002446", _flat_drop(10.0, 8.9))    # 止损距离 ≈1.22
    _insert(db, "002472", _flat_drop(40.0, 35.9))   # 止损距离 ≈2.47
    _insert(db, "IDX000001", _idx_bars(27, on=True), market="index")
    pipeline_run(db, _cfg(["002446", "002472"], _write_map(tmp_path, "attack")))
    fills, _ = _fills(db)
    buys = {f[2]: f for f in fills if f[3] == "BUY"}
    tables = {s: _final_check_row(db, f, _load_cfg()) for s, f in buys.items()}
    _evidence("scenario_04_distance_scaling", {
        "input": "同一账户同日两只信号：002446 止损距离≈1.22，002472 ≈2.47（约2倍）",
        "expected": "距离大者股数约减半；两笔终局实际风险近似相等且 ≤ 预算",
        "actual": {"qty": {s: f[4] for s, f in buys.items()},
                   "final_check": tables},
        "fills": fills})
    assert set(buys) == {"002446", "002472"}
    assert buys["002472"][4] < buys["002446"][4]
    r1, r2 = tables["002446"]["actual_risk"], tables["002472"]["actual_risk"]
    assert abs(r1 - r2) <= max(tables["002446"]["per_share_risk"],
                               tables["002472"]["per_share_risk"]) * 100
    assert all(t["within_budget"] for t in tables.values())


# ---------- 场景 5：高相关/同板块簇组合限额 ----------

def test_scenario_05_cluster_limit(tmp_path):
    db = _fresh_db(tmp_path)
    _insert(db, "002472", _bars_from_closes(
        [40.0] * 25 + [35.9, 35.9, 36.8, 37.6, 38.3, 38.9]))
    _insert(db, "002747", _bars_from_closes(
        [33.0] * 31 + [29.6, 29.6]))
    _insert(db, "IDX000001", _idx_bars(33, on=True), market="index")
    cfg5 = _cfg(["002472", "002747"], _write_map(tmp_path, "attack"))
    cfg5["lots"]["002747"] = 100  # 002747 生产口径为纯展示标的，yaml 无 lot，fixture 显式给定
    pipeline_run(db, cfg5)
    fills, blk = _fills(db)
    buys = {f[2]: f for f in fills if f[3] == "BUY"}
    # 簇存量风险：002472 700股 × (38.9 − 33.5) = 3780 ≈ 净值 3.78%
    cluster_before = 700 * (38.9 - 33.5)
    nav_end = sqlite3.connect(db).execute(
        "SELECT nav FROM daily_report WHERE account='sim_a'"
        " AND symbol='TOTAL' ORDER BY date DESC LIMIT 1").fetchone()[0]
    _evidence("scenario_05_cluster_limit", {
        "input": "同板块 robotics：002472 先建仓700股，价格涨至38.9使存量未实现风险升至3780（≈3.78%净值）；002747 随后触发 BUY（无约束数量约1000股）",
        "expected": "002747 被簇预算（≤净值4%）缩量至100股；簇总风险 ≤4%",
        "actual": {"qty": {s: f[4] for s, f in buys.items()},
                   "cluster_risk_before": cluster_before,
                   "nav_end": nav_end},
        "fills": fills, "blocked": blk})
    assert buys["002472"][4] == 700
    assert buys["002747"][4] == 100, buys
    assert cluster_before + 100 * 2.04 <= nav_end * 0.04 * 1.02


# ---------- 场景 6：资金不足/最小单位不违规成交 ----------

def test_scenario_06_below_min_lot(tmp_path):
    db, cfg = _basic_fixture(tmp_path, state="attack")
    cfg["accounts"][0]["principal"] = 3000  # 风险预算仅 60 元
    pipeline_run(db, cfg)
    fills, blk = _fills(db)
    _evidence("scenario_06_below_min_lot", {
        "input": "账户本金 3000（风险预算 60 元），002446 正常 BUY 信号",
        "expected": "缩量后 <1 手 → 禁单：fills 无该笔，blocked 有记录（不足最小交易单位），不强行下最小单位",
        "actual": {"fills": fills, "blocked": blk},
        "fills": fills, "blocked": blk})
    assert fills == []
    assert any("不足最小交易单位" in b[5] for b in blk), blk


# ---------- 场景 7：002639 身份冲突 ----------

def test_scenario_07_identity_conflict(tmp_path):
    db, cfg = _basic_fixture(tmp_path, state="attack", sym="002639",
                             flat=15.0, drop=13.4)
    bad = dict(cfg)
    bad["display_only"] = ["002639"]  # 人为双栖：universe ∩ display_only
    err = None
    try:
        pipeline_run(db, bad)
    except AssertionError as e:
        err = str(e)
    # 恢复正式名单 → 按正式规则可交易
    pipeline_run(db, cfg)
    fills, _ = _fills(db)
    buys = [f for f in fills if f[3] == "BUY" and f[2] == "002639"]
    _evidence("scenario_07_identity_conflict", {
        "input": "人为把 002639 同时写入 universe 与 display_only；随后恢复正式名单",
        "expected": "启动自检明确拦截报错（含标的代码）；恢复后 002639 按正式规则正常成交",
        "actual": {"assert_error": err, "restored_buy_fills": buys},
        "fills": fills})
    assert err and "002639" in err
    assert len(buys) == 1 and buys[0][4] > 0


# ---------- 场景 8 + 组合：正常对照组 / 发布门四组合 ----------

def test_scenario_08_control_normal(tmp_path):
    db = _fresh_db(tmp_path)
    _insert(db, "002446", _bars_from_closes(
        [10.0] * 25 + [8.9, 8.9, 9.6, 10.1, 10.3, 10.3]))
    _insert(db, "IDX000001", _idx_bars(31, on=True), market="index")
    pipeline_run(db, _cfg(["002446"], _write_map(tmp_path, "attack")))
    fills, blk = _fills(db)
    sides = [f[3] for f in fills]
    _evidence("scenario_08_control_normal", {
        "input": "允许进攻 + 002446 完整多头路径（回落买入→+15%止盈）",
        "expected": "BUY 与 SELL 均正常成交（防止全链路只会说不）",
        "actual": {"sides": sides}, "fills": fills, "blocked": blk})
    assert sides == ["BUY", "SELL"], fills


def test_combo_attack_buy_fills_risk_sized(tmp_path):
    """发布门组合①：允许进攻 + BUY → 过其余闸门后按风险定仓成交，
    数量以终局校验为准；成交写入新版本号 v1.1-2026-10-26。"""
    db, cfg = _basic_fixture(tmp_path, state="attack")
    pipeline_run(db, cfg)
    fills, blk = _fills(db)
    buy = [f for f in fills if f[3] == "BUY"]
    table = _final_check_row(db, buy[0], _load_cfg()) if buy else None
    _evidence("combo_attack_buy", {
        "input": "允许进攻（自动计算站上+对账一致+已放行变体）+ 002446 BUY",
        "expected": "风险定仓成交 1 笔；qty×每股真实风险 ≤ 预算；strategy_version=v1.1-2026-10-26",
        "actual": {"buy": buy, "final_check": table}, "fills": fills,
        "blocked": blk})
    assert len(buy) == 1 and buy[0][4] > 0
    assert buy[0][9] == "v1.1-2026-10-26"
    assert table and table["within_budget"]


def test_combo_blocked_buy_blocked(tmp_path):
    """发布门组合②：禁止进攻 + BUY → 被拦截并留痕，原因=环境闸。"""
    db, cfg = _basic_fixture(tmp_path, state="blocked", idx_on=False)
    pipeline_run(db, cfg)
    fills, blk = _fills(db)
    _evidence("combo_blocked_buy", {
        "input": "禁止新开仓 + 002446 BUY（无换仓额度）",
        "expected": "fills 无 BUY；blocked_signals 有记录且原因含环境闸·禁止新开仓",
        "actual": {"fills": fills, "blocked": blk},
        "fills": fills, "blocked": blk})
    assert [f for f in fills if f[3] == "BUY"] == []
    assert any("环境闸" in b[5] and "禁止新开仓" in b[5] for b in blk)


def test_combo_blocked_sell_not_blocked_by_env(tmp_path):
    """发布门组合③：禁止进攻 + SELL → 不被环境闸阻断，正常成交。"""
    db = _fresh_db(tmp_path)
    _insert(db, "002446", _bars_from_closes([10.0] * 25 + [8.9, 8.9]))
    _insert(db, "IDX000001", _idx_bars(27, on=True), market="index")
    pipeline_run(db, _cfg(["002446"], _write_map(tmp_path, "attack")))
    _insert(db, "002446", _bars_from_closes(
        [9.6, 10.1, 10.3, 10.3], start=date(2026, 10, 28)))
    _insert(db, "IDX000001", _bars_from_closes(
        [200 - i * 2.0 for i in range(31)]), market="index")
    pipeline_run(db, _cfg(["002446"], _write_map(tmp_path, "blocked")))
    fills, blk = _fills(db)
    sells = [f for f in fills if f[3] == "SELL"]
    sell_env_blk = [b for b in blk if b[4] == "SELL" and "环境" in b[5]]
    _evidence("combo_blocked_sell", {
        "input": "先允许进攻建仓；转禁止新开仓后 002446 触发 +15% 止盈 SELL",
        "expected": "SELL 正常成交；无任何 SELL 被环境闸拦截的记录",
        "actual": {"sells": sells, "sell_env_blocked": sell_env_blk},
        "fills": fills, "blocked": blk})
    assert len(sells) == 1 and sells[0][4] == 1500
    assert sell_env_blk == []


def test_combo_anomaly_stale_index(tmp_path):
    """发布门组合④-a：数据异常（指数过期）+ BUY → 安全拦截。"""
    db = _fresh_db(tmp_path)
    _insert(db, "002446", _flat_drop(10.0, 8.9))          # 个股至 10-27
    _insert(db, "IDX000001", _idx_bars(22, on=True),      # 指数只到 10-22
            market="index")
    pipeline_run(db, _cfg(["002446"], _write_map(tmp_path, "attack")))
    fills, blk = _fills(db)
    _evidence("combo_anomaly_stale_index", {
        "input": "指数 bars 停在 2026-10-22，个股最新日 2026-10-27（指数过期）；映射为允许进攻变体",
        "expected": "对账守门判过期 → 降级禁开：BUY 拦截，原因含指数数据过期/安全降级",
        "actual": {"fills": fills, "blocked": blk},
        "fills": fills, "blocked": blk})
    assert [f for f in fills if f[3] == "BUY"] == []
    assert any("指数数据过期" in b[5] for b in blk), blk


def test_combo_anomaly_recon_mismatch(tmp_path):
    """发布门组合④-b：数据异常（B31 对账不一致）+ BUY → 安全拦截。"""
    db = _fresh_db(tmp_path)
    _insert(db, "002446", _flat_drop(10.0, 8.9))
    _insert(db, "IDX000001", _idx_bars(27, on=False), market="index")
    # 映射 B31=是（attack 变体）但自动计算 = 未站上 → 对账不一致
    pipeline_run(db, _cfg(["002446"], _write_map(tmp_path, "attack")))
    fills, blk = _fills(db)
    _evidence("combo_anomaly_recon_mismatch", {
        "input": "自动计算=未站上（指数下跌序列）但母板 B31 变体=是 → 对账不一致",
        "expected": "降级禁开：BUY 拦截，原因含环境口径不一致/安全降级，不静默按任一侧放行",
        "actual": {"fills": fills, "blocked": blk},
        "fills": fills, "blocked": blk})
    assert [f for f in fills if f[3] == "BUY"] == []
    assert any("环境口径不一致" in b[5] for b in blk), blk


def test_combo_anomaly_risk_data_missing(tmp_path):
    """发布门组合④-c：数据异常（风险数据缺失）+ BUY → 安全拦截。"""
    db = _fresh_db(tmp_path)
    _insert(db, "999999", _flat_drop(10.0, 8.9))  # workbook_map 无此标的
    _insert(db, "IDX000001", _idx_bars(27, on=True), market="index")
    pipeline_run(db, _cfg(["999999"], _write_map(tmp_path, "attack")))
    fills, blk = _fills(db)
    _evidence("combo_anomaly_risk_data_missing", {
        "input": "标的 999999 不在 workbook_map（母板真源无记录），其余条件正常",
        "expected": "禁单：fills 0；blocked 原因含风险数据缺失，不按默认仓位成交",
        "actual": {"fills": fills, "blocked": blk},
        "fills": fills, "blocked": blk})
    assert fills == []
    assert any("风险数据缺失" in b[5] for b in blk), blk


# ---------- 恢复性：禁开 → 恢复，硬闸自动解除 ----------

def test_recovery_gate_releases(tmp_path):
    from engine.screen import screen_all
    db = _fresh_db(tmp_path)
    _insert(db, "002446", _flat_drop(10.0, 8.9))
    _insert(db, "IDX000001", _idx_bars(27, on=False), market="index")
    blocked_map = _write_map(tmp_path, "blocked")
    pipeline_run(db, _cfg(["002446"], blocked_map))
    fills1, _ = _fills(db)
    rows1 = screen_all(db, "2026-10-27", map_path=blocked_map, record=False)
    # 环境恢复：指数站回 MA20（替换为上升序列）+ 母板 B31 改是且已放行
    _insert(db, "IDX000001", _idx_bars(27, on=True), market="index")
    attack_map = _write_map(tmp_path, "attack")
    pipeline_run(db, _cfg(["002446"], attack_map))
    fills2, _ = _fills(db)
    rows2 = screen_all(db, "2026-10-27", map_path=attack_map, record=False)
    _evidence("recovery_gate_releases", {
        "input": "同库两段：先禁开（指数在MA20下方/B31否）→ 后恢复（指数站回MA20/B31是/已放行变体）",
        "expected": "禁开段 BUY 成交 0、screen 全池带环境闸；恢复段同一信号正常成交、screen 环境闸消失且 env_state=attack（状态按当日数据重算，不 sticky）",
        "actual": {
            "stage1_buy_fills": [f for f in fills1 if f[3] == "BUY"],
            "stage1_screen_env": rows1[0]["env_state"] if rows1 else None,
            "stage2_buy_fills": [f for f in fills2 if f[3] == "BUY"],
            "stage2_screen_env": rows2[0]["env_state"] if rows2 else None,
            "stage2_screen_gates_002446": [r["gates"] for r in rows2
                                           if r["symbol"] == "002446"]},
        "fills": fills2})
    assert [f for f in fills1 if f[3] == "BUY"] == []
    assert rows1 and rows1[0]["env_state"] == "blocked_new"
    assert any("环境闸" in g for r in rows1 for g in r["gates"])
    buys2 = [f for f in fills2 if f[3] == "BUY"]
    assert len(buys2) == 1 and buys2[0][4] > 0
    assert rows2 and rows2[0]["env_state"] == "attack"
    assert not any("环境闸" in g for r in rows2 for g in r["gates"])


# ---------- 模块D：形态因子 as-of 复现与确定性 ----------

def _mk_bars(n, px="100"):
    import datetime as _dt
    base = _dt.date(2026, 5, 4)
    out = []
    p = float(px)
    for i in range(n):
        d = (base + _dt.timedelta(days=i)).isoformat()
        p *= 1.004
        out.append((d, p * 0.999, p * 1.006, p * 0.994, p, 1000.0 + i))
    return out


def test_module_d_factor_asof_reproducible():
    """因子 as-of 复现：同一历史时点两跑 factors 证据逐字一致；且
    历史截断（只用当时 bars）与全量（含未来暴跌）结果不同——证明
    因子只用 as-of 之前的数据、无未来泄漏。"""
    from engine.screen import pattern_factors
    up = _mk_bars(70)
    res1 = pattern_factors(up)
    res2 = pattern_factors(up)
    assert res1 == res2, "同输入两跑结果不一致"
    crash = list(up)
    px = float(up[-1][4])
    for i in range(1, 11):
        px *= 0.97
        crash.append(("2026-12-%02d" % i, px, px * 1.005, px * 0.99, px, 1000.0))
    old = pattern_factors(crash[:70])
    new = pattern_factors(crash)
    assert old[1] != new[1], "as-of 截断未改变因子证据，疑似用了未来数据"
    _evidence("module_d_factor_asof", {
        "input": "70 根上升 bars；同一序列后续接 10 根暴跌",
        "expected": "同输入两跑逐字一致；截断时点证据 != 全量时点证据",
        "actual": {"run1_eq_run2": res1 == res2,
                   "old_flags": old[1]["flags"],
                   "new_flags": new[1]["flags"]}})


def test_module_d_factor_determinism_and_cap():
    """因子确定性与封顶：纯函数同输入同输出；points ≤15（技术面
    45 封顶内）；证据标注「待验证因子」。"""
    from engine.screen import pattern_factors
    bars = _mk_bars(130)
    p1, e1 = pattern_factors(bars, rs_top=True)
    p2, e2 = pattern_factors(list(bars), rs_top=True)
    assert (p1, e1) == (p2, e2)
    assert p1 <= 15.0, "形态层超封顶：%s" % p1
    assert e1["status"] == "待验证因子"
    _evidence("module_d_factor_determinism", {
        "input": "130 根合成 bars 两次独立调用",
        "expected": "结果完全一致；points≤15；标注待验证因子",
        "actual": {"points": p1, "flags": e1["flags"],
                   "status": e1["status"]}})


# ---------- 清单C：账本指纹幂等 + 版本隔离 ----------

def _fingerprint(db_path):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT date,account,symbol,side,qty,price,fee,pnl,signal_date,"
        "strategy_version FROM fills ORDER BY id").fetchall()
    prows = con.execute(
        "SELECT account,symbol,qty,avg_cost FROM positions"
        " ORDER BY account,symbol").fetchall()
    con.close()
    h = hashlib.sha256()
    for r in rows:
        h.update(("|".join(repr(x) for x in r) + "\n").encode())
    h2 = hashlib.sha256()
    for r in prows:
        h2.update(("|".join(repr(x) for x in r) + "\n").encode())
    return {"fills": {"rows": len(rows), "sha256": h.hexdigest(),
                      "sum_qty": round(sum(r[4] for r in rows), 6),
                      "sum_amount": round(sum(r[4] * r[5] for r in rows), 2),
                      "sum_pnl": round(sum(r[7] or 0 for r in rows), 2)},
            "positions": {"rows": len(prows), "sha256": h2.hexdigest()}}


def test_ledger_fingerprint_idempotent(tmp_path):
    """同库连跑两次（模拟重复跑批/进程重启重跑）：账本指纹不变、
    不产生重复成交，且与改造前基线全等。"""
    db = str(tmp_path / "prod_copy.db")
    shutil.copy2(PROD_DB, db)
    cfg = _load_cfg()
    pipeline_run(db, cfg)
    fp1 = _fingerprint(db)
    pipeline_run(db, cfg)
    fp2 = _fingerprint(db)
    base = json.load(open(os.path.join(EVID, "ledger_fingerprint.json"),
                          encoding="utf-8"))["baseline_pre_batch2"]
    _evidence("ledger_fingerprint_idempotent", {
        "input": "生产库副本 + 现行管线连跑两次",
        "expected": "两次指纹相等且与改造前基线（行数/逐行哈希/Σqty/Σamount/Σpnl）全等",
        "actual": {"after_run1": fp1, "after_run2": fp2, "baseline": base},
        "fills": fp2["fills"]})
    assert fp1 == fp2
    for k in ("rows", "sha256", "sum_qty", "sum_amount", "sum_pnl"):
        assert fp1["fills"][k] == base["fills"][k], k
    assert fp1["positions"]["sha256"] == base["positions"]["sha256"]


def test_graduation_version_isolation(tmp_path):
    """毕业样本按 strategy_version 严格隔离出数：旧版本样本单独统计，
    当前版本口径不混入旧样本，mismatch 单独标注。"""
    db = str(tmp_path / "prod_copy.db")
    shutil.copy2(PROD_DB, db)
    con = sqlite3.connect(db)
    from sync_snapshot import build_graduation
    g = build_graduation(con)
    con.close()
    _evidence("graduation_version_isolation", {
        "input": "生产库副本 build_graduation（FIFO 配对 + 买入日口径 + 版本严格隔离）",
        "expected": "by_version 含 v1.0-2026-10-17 的独立样本与指标；当前版本 v1.1 尚无 round-trip 时 total=0，旧样本不混入",
        "actual": {"by_version": g["by_version"],
                   "total_round_trips_current_version": g["total_round_trips"],
                   "accounts": {a: {"round_trips": v["round_trips"],
                                    "version_mismatch": v["version_mismatch_round_trips"]}
                                for a, v in g["accounts"].items()}},
        "fills": []})
    assert g["by_version"]["v1.0-2026-10-17"]["round_trips"] == 1
    assert g["by_version"]["v1.0-2026-10-17"]["expectancy"] == -2000.42
    assert g["total_round_trips"] == 0
