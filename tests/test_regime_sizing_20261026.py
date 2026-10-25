#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二批改造（模块A 环境状态机 + 模块B 风险定仓）专项验收测试。

覆盖：
- A 状态机矩阵：三态组合、字段缺失/映射过期 fail-closed 降级；
- A 每日对账：自动计算 vs 母板 B31 一致/不一致、指数过期、数据不足；
- A 同步受控：同一批 BUY 信号在 允许进攻/谨慎/禁开 三态下 pipeline
  成交量 = 全量 / 系数0.5 且受 0.3 上限 / 0；
- B sizer 单元：风险额跨价格相等、距离翻倍股数减半、簇/现金/总仓位
  三顶、止损无效、数据缺失、不足一手、谨慎系数、终局校验；
- B 新旧对比：旧算法金额相等 vs 新算法风险相等（金额显著不同）；
- C 生效日分流：2026-10-26 前信号走 legacy 路径，数量逐笔不变。

安全：写操作只在 tmp fixture 库；生产库只读。运行:
.venv/bin/python -m pytest tests/test_regime_sizing_20261026.py -q
"""
import copy
import json
import math
import os
import sqlite3
import subprocess
import sys
from datetime import date, timedelta

import pytest
import yaml

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from engine import regime  # noqa: E402
from engine.pipeline import (  # noqa: E402
    position_sizer, position_sizer_v2, run as pipeline_run)

REAL_MAP = os.path.join(BASE, "config", "workbook_map.json")
D0 = date(2026, 10, 1)


def _load_cfg():
    with open(os.path.join(BASE, "config", "strategy.yaml"),
              encoding="utf-8") as f:
        return yaml.safe_load(f)


def _real_map():
    with open(REAL_MAP, encoding="utf-8") as f:
        return json.load(f)


def _write_map(tmp_path, state="attack", **over):
    """生成 workbook_map 变体：state 定三态字段，over 覆盖任意字段。
    generated_at 刷新为当前时间（非过期）；过期用例单独改写。"""
    m = copy.deepcopy(_real_map())
    if state == "attack":
        m["b31_manual"].update(raw="是", value=True)
        m["release"].update(raw="已放行", released=True)
        m["gate_b79"].update(raw="否", value=False)
    elif state == "cautious":
        m["b31_manual"].update(raw="是", value=True)
        m["release"].update(raw="未放行:先整改", released=False)
        m["gate_b79"].update(raw="是", value=True)
    elif state == "blocked":
        m["b31_manual"].update(raw="否", value=False)
        m["release"].update(raw="未放行:先整改", released=False)
        m["gate_b79"].update(raw="否", value=False)
    m.update(over)
    from datetime import datetime
    m["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = tmp_path / "map_variant.json"
    p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _fresh_db(tmp_path):
    db = str(tmp_path / "fix.db")
    r = subprocess.run([sys.executable, os.path.join(BASE, "init_db.py"),
                        "--db", db], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    con.execute("UPDATE control SET status='running', mode='auto' WHERE id=1")
    con.commit()
    con.close()
    return db


def _insert(db, symbol, bars, market="a"):
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT OR REPLACE INTO market_data(symbol,date,open,high,low,close,volume,market)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(symbol,) + b + (market,) for b in bars])
    con.commit()
    con.close()


def _bars_from_closes(closes, start=D0):
    """closes 序列 → bars（open=前收，high=max，low=min×0.995）。"""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        d = (start + timedelta(days=i)).isoformat()
        o = prev
        out.append((d, o, max(o, c), min(o, c) * 0.995, c, 100000.0))
        prev = c
    return out


def _idx_bars(n, on, start=D0):
    closes = [100 + i * 0.6 for i in range(n)] if on \
        else [200 - i * 2.0 for i in range(n)]
    return _bars_from_closes(closes, start)


def _cfg(universe, map_path=None, principal=100000):
    cfg = copy.deepcopy(_load_cfg())
    cfg["accounts"] = [dict(a, principal=principal) if a["id"] == "sim_a" else a
                       for a in cfg["accounts"] if a["id"] == "sim_a"]
    cfg["universe"] = {"sim_a": universe}
    cfg["display_only"] = []
    if map_path:
        cfg["env_map_path"] = map_path
    return cfg


def _flat_drop(flat, drop, n_flat=25, extra_flat=0):
    closes = [flat] * n_flat + [drop] + [drop] * (1 + extra_flat)
    return _bars_from_closes(closes)


# ---------- A 状态机矩阵（纯映射，无 DB 上下文） ----------

def test_env_state_matrix(tmp_path):
    p = _write_map(tmp_path, "attack")
    env = regime.load_market_env(map_path=p)
    assert env["state"] == "attack" and env["buy_coef"] is None
    p = _write_map(tmp_path, "cautious")
    env = regime.load_market_env(map_path=p)
    assert env["state"] == "cautious"
    assert env["buy_coef"] == 0.5 and env["total_cap"] == 0.3  # 母板 B78/B80
    p = _write_map(tmp_path, "blocked")
    env = regime.load_market_env(map_path=p)
    assert env["state"] == "blocked_new"
    # B31=是、未放行、B79=否 → 同样禁开（未放行禁新开）
    m = _real_map()
    m["b31_manual"].update(raw="是", value=True)
    m["release"].update(released=False)
    m["gate_b79"].update(raw="否", value=False)
    assert regime.env_from_map(m)["state"] == "blocked_new"


def test_env_fail_closed(tmp_path):
    # 字段缺失（删 gate_b79）→ 降级
    m = _real_map()
    del m["gate_b79"]
    env = regime.env_from_map(m)
    assert env["state"] == "degraded" and "B79" in env["degraded_reason"]
    # 映射过期（generated_at 远早于母板 mtime）→ 降级
    m2 = _real_map()
    m2["generated_at"] = "2020-01-01 00:00:00"
    env2 = regime.env_from_map(m2)
    assert env2["state"] == "degraded" and "过期" in env2["degraded_reason"]
    # 文件不存在 → 降级
    env3 = regime.load_market_env(map_path=str(tmp_path / "nope.json"))
    assert env3["state"] == "degraded"


def test_env_reconciliation():
    m = _real_map()  # 真实值：B31=否、未放行、B79=否
    auto_off = {"known": True, "on": False, "close": 3690.48,
                "ma20": 3803.88, "n": 193, "last_date": "2026-10-23"}
    env = regime.env_from_map(m, auto=auto_off, stock_last_date="2026-10-23")
    assert env["state"] == "blocked_new" and env["reconciled"] is True
    assert "对账一致" in env["reason"]
    # 对账不一致（自动算站上、B31=否）→ 降级 BLOCKED
    auto_on = dict(auto_off, on=True, close=3900.0)
    env2 = regime.env_from_map(m, auto=auto_on, stock_last_date="2026-10-23")
    assert env2["state"] == "degraded" and env2["reconciled"] is False
    assert "环境口径不一致" in env2["degraded_reason"]
    # 指数过期（指数日落后于个股最新日）→ 降级
    env3 = regime.env_from_map(m, auto=auto_off, stock_last_date="2026-10-24")
    assert env3["state"] == "degraded" and "指数数据过期" in env3["degraded_reason"]
    # 自动计算不可用（bars 不足）→ 降级
    auto_bad = {"known": False, "on": None, "close": None, "ma20": None,
                "n": 5, "last_date": "2026-10-23"}
    env4 = regime.env_from_map(m, auto=auto_bad, stock_last_date="2026-10-23")
    assert env4["state"] == "degraded" and "自动计算不可用" in env4["degraded_reason"]


def test_env_current_real_baseline():
    """当前真实基线：指数在 MA20 下方、B31=否 → 禁开且对账一致
    （不是 degraded）。数据来自生产库只读 + 真实 workbook_map。"""
    env = regime.load_market_env(db_path=os.path.join(BASE, "db", "sim.db"))
    assert env["state"] == "blocked_new", env
    assert env["reconciled"] is True
    assert env["auto"]["on"] is False and env["b31"] is False


# ---------- A 同步受控：三态翻转同一批信号 ----------

FLIP_SYMS = [("002446", 10.0, 8.9), ("002639", 15.0, 13.4), ("002472", 40.0, 35.9)]


def _flip_fixture(tmp_path, state):
    db = _fresh_db(tmp_path)
    for sym, flat, drop in FLIP_SYMS:
        _insert(db, sym, _flat_drop(flat, drop))
    _insert(db, "IDX000001", _idx_bars(27, on=(state != "blocked")),
            market="index")
    return db, _cfg([s for s, _, _ in FLIP_SYMS],
                    _write_map(tmp_path, state))


def _buy_fills(db):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT symbol, qty FROM fills WHERE side='BUY' ORDER BY symbol").fetchall()
    blk = con.execute(
        "SELECT symbol, reason FROM blocked_signals").fetchall()
    con.close()
    return dict(rows), blk


def test_pipeline_three_state_flip(tmp_path):
    import pathlib
    # 允许进攻：三只全量成交（v2 风险定仓；执行序 002446→002472→002639，
    # 002639 在攻击态受 70% 总仓位上限约束）
    pathlib.Path(tmp_path / "a").mkdir(exist_ok=True)
    db, cfg = _flip_fixture(tmp_path / "a", "attack")
    pipeline_run(db, cfg)
    full, _ = _buy_fills(db)
    assert set(full) == {"002446", "002639", "002472"} and all(
        q > 0 for q in full.values()), full
    # 谨慎交易：系数 0.5（002446 数量约半）+ 总仓位上限 0.3 绑定
    # （002639 被 0.3 上限压到系数减半以下）
    pathlib.Path(tmp_path / "c").mkdir(exist_ok=True)
    db2, cfg2 = _flip_fixture(tmp_path / "c", "cautious")
    pipeline_run(db2, cfg2)
    caut, _ = _buy_fills(db2)
    assert set(caut) == set(full)
    assert abs(caut["002446"] * 2 - full["002446"]) <= 200, (caut, full)
    assert caut["002639"] * 2 < full["002639"], (caut, full)
    assert caut["002472"] * 2 <= full["002472"] + 200, (caut, full)
    con = sqlite3.connect(db2)
    nav = con.execute(
        "SELECT nav, position_value FROM daily_report WHERE account='sim_a'"
        " AND symbol='TOTAL' ORDER BY date DESC LIMIT 1").fetchone()
    con.close()
    assert nav[1] <= nav[0] * 0.30 * 1.02, nav  # 谨慎态总仓位 ≤30%
    # 禁止新开仓：0 成交 + 每只留环境闸拦截痕
    pathlib.Path(tmp_path / "b").mkdir(exist_ok=True)
    db3, cfg3 = _flip_fixture(tmp_path / "b", "blocked")
    pipeline_run(db3, cfg3)
    none, blk = _buy_fills(db3)
    assert none == {}, none
    env_blk = [r for r in blk if "环境闸" in r[1]]
    assert {r[0] for r in env_blk} == {"002446", "002639", "002472"}, blk
    con = sqlite3.connect(db3)
    sigs = con.execute(
        "SELECT COUNT(*) FROM signals WHERE side='BUY'").fetchone()[0]
    con.close()
    assert sigs >= 3  # 信号照常生成，只是执行被拦


# ---------- C 生效日分流：旧信号 legacy 逐笔不变 ----------

def test_old_signal_legacy_path_unchanged(tmp_path):
    """信号日 2026-10-18（<生效日）：即使环境禁开、无指数数据，
    仍走 legacy 路径且数量与旧算法逐笔一致（手算：现金 50% 被
    buyback 单笔风险顶 4400 股封顶）。"""
    db = _fresh_db(tmp_path)
    closes = [9.0] * 25 + [8.0] + [8.0]  # 信号日 2026-10-18，跌幅触发 buyback
    bars = _bars_from_closes(closes, start=date(2026, 9, 23))
    _insert(db, "002446", bars)
    cfg = _cfg(["002446"], _write_map(tmp_path, "blocked"))
    pipeline_run(db, cfg)
    con = sqlite3.connect(db)
    fills = con.execute(
        "SELECT qty, price, strategy_version FROM fills WHERE side='BUY'").fetchall()
    con.close()
    assert len(fills) == 1
    # 与 legacy position_sizer 同输入直算结果逐笔一致（数量+价格口径）
    qty_legacy, _ = position_sizer(
        _StubSim(100000.0), "sim_a", "002446", fills[0][1],
        _load_cfg(), "buyback", 0.5, _load_cfg()["lots"])
    assert fills[0][0] == qty_legacy, (fills, qty_legacy)


class _StubSim:
    def __init__(self, cash):
        self.cash = cash
        self.positions = {}


# ---------- B sizer 单元 ----------

def _ctx(**kw):
    base = {"nav": 1000000.0, "cash": 1000000.0, "entry_px": 10.0,
            "lot": 100, "risk_pct": 0.02, "buy_fee_rate": 0.00025,
            "buy_min_fee": 5.0, "sell_fee_rate": 0.00125,
            "sell_min_fee": 5.0, "slippage": 0.002,
            "max_pos_pct": 0.70, "pos_value": 0.0,
            "stop_dist": 1.0, "stop_price": 9.0,
            "cluster_existing_risk": 0.0, "cluster_budget_pct": 0.04,
            "cluster_label": "测试簇", "data_ok": True,
            "data_missing_reason": ""}
    base.update(kw)
    return base


def _psr(ctx):
    """独立重算每股风险（与 sizer 同公式，供终局断言对照）。"""
    from engine.pipeline import _round_trip_cost_ps
    budget = ctx["nav"] * ctx["risk_pct"] * (ctx.get("env_buy_coef") or 1)
    p0 = ctx["stop_dist"] + _round_trip_cost_ps(
        ctx["entry_px"], ctx["buy_fee_rate"], ctx["buy_min_fee"],
        ctx["sell_fee_rate"], ctx["sell_min_fee"], ctx["slippage"], None)
    pre = budget / p0
    return ctx["stop_dist"] + _round_trip_cost_ps(
        ctx["entry_px"], ctx["buy_fee_rate"], ctx["buy_min_fee"],
        ctx["sell_fee_rate"], ctx["sell_min_fee"], ctx["slippage"], pre)


def test_sizer_risk_equal_across_prices():
    """同止损距离（绝对值 1.0）、不同价格：风险额近似相等（±1 手
    每股风险），金额显著不同（B-7① 新算法侧）。"""
    # max_pos_pct=1.0 + 现金不绑定：隔离总仓位/现金顶，专测风险公式本身
    c1 = _ctx(entry_px=10.0, stop_price=9.0, stop_dist=1.0, max_pos_pct=1.0,
              cash=100000000.0)
    c2 = _ctx(entry_px=30.0, stop_price=29.0, stop_dist=1.0, max_pos_pct=1.0,
              cash=100000000.0)
    q1, r1 = position_sizer_v2(c1)
    q2, r2 = position_sizer_v2(c2)
    assert q1 > 0 and q2 > 0, (r1, r2)
    risk1, risk2 = q1 * _psr(c1), q2 * _psr(c2)
    assert abs(risk1 - risk2) <= max(_psr(c1), _psr(c2)) * 100
    assert abs(q1 * 10.0 - q2 * 30.0) > 100000  # 金额显著不同（约 2.7 倍）
    # 终局校验：实际风险 ≤ 预算（仅向下取整误差）
    assert risk1 <= 20000 + 1e-6 and risk2 <= 20000 + 1e-6


def test_sizer_distance_double_halves():
    c1 = _ctx(stop_dist=1.0, stop_price=9.0)
    c2 = _ctx(stop_dist=2.0, stop_price=8.0)
    q1, _ = position_sizer_v2(c1)
    q2, _ = position_sizer_v2(c2)
    assert q1 > 0 and q2 > 0
    # 每股风险含固定成本项，距离翻倍时股数约减半（相对误差 ≤5%+1手）
    assert abs(q2 * 2 - q1) <= q1 * 0.05 + 200


def test_sizer_cluster_cap_scales():
    """簇存量风险 3.5% 净值 → 本笔被缩到 ≤0.5% 净值风险。"""
    c = _ctx(cluster_existing_risk=35000.0)
    q, r = position_sizer_v2(c)
    assert q > 0, r
    assert q * _psr(c) <= 5000 * 1.001
    c_full = _ctx(cluster_existing_risk=40000.0)
    q2, r2 = position_sizer_v2(c_full)
    assert q2 == 0 and "风险簇预算已满" in r2


def test_sizer_cash_and_total_caps():
    c = _ctx(cash=5000.0)
    q, r = position_sizer_v2(c)
    assert q > 0
    assert q * 10.0 + max(q * 10.0 * 0.00025, 5.0) <= 5000.0 + 1e-6
    c2 = _ctx(pos_value=690000.0)  # 总仓位上限剩 1% 空间
    q2, _ = position_sizer_v2(c2)
    assert q2 > 0 and 690000.0 + q2 * 10.0 <= 700000.0 + 1e-6


def test_sizer_hard_blocks():
    q, r = position_sizer_v2(_ctx(stop_price=10.5, stop_dist=-0.5))
    assert q == 0 and "止损无效" in r
    q, r = position_sizer_v2(_ctx(stop_price=None, stop_dist=None,
                                  stop_ref_raw=None, atr_stop_raw=None))
    assert q == 0 and "止损无效" in r
    q, r = position_sizer_v2(_ctx(data_ok=False,
                                  data_missing_reason="bars 仅 3 根"))
    assert q == 0 and "风险数据缺失" in r
    q, r = position_sizer_v2(_ctx(nav=3000.0, cash=3000.0))
    assert q == 0 and "不足最小交易单位" in r


def test_sizer_cautious_inputs_from_A():
    full, _ = position_sizer_v2(_ctx())
    half, _ = position_sizer_v2(_ctx(env_buy_coef=0.5))
    assert abs(half * 2 - full) <= 200
    capped, _ = position_sizer_v2(_ctx(env_total_cap=0.3,
                                       pos_value=299000.0))
    assert capped > 0 and 299000.0 + capped * 10.0 <= 300000.0 + 1e-6


def test_legacy_vs_v2_amount_contrast():
    """B-7① 对照：旧算法两只价格不同的标的买入金额相同（现金 50%）；
    新算法风险额相同而金额显著不同。"""
    cfg = _load_cfg()
    sim = _StubSim(100000.0)
    qa, _ = position_sizer(sim, "sim_a", "X1", 10.0, cfg, "ma20", 0.5,
                           cfg["lots"])
    qb, _ = position_sizer(sim, "sim_a", "X2", 30.0, cfg, "ma20", 0.5,
                           cfg["lots"])
    assert qa * 10.0 == 50000.0 and qb * 30.0 == 49980.0  # 旧：金额近似相等（现金50%，差仅取整）
    c1 = _ctx(nav=100000.0, cash=100000000.0, entry_px=10.0,
              stop_price=9.0, stop_dist=1.0, max_pos_pct=1.0)
    c2 = _ctx(nav=100000.0, cash=100000000.0, entry_px=30.0,
              stop_price=29.0, stop_dist=1.0, max_pos_pct=1.0)
    q1, _ = position_sizer_v2(c1)
    q2, _ = position_sizer_v2(c2)
    assert abs(q1 * _psr(c1) - q2 * _psr(c2)) <= _psr(c2) * 100
    assert q2 * 30.0 > q1 * 10.0 * 2.5  # 新：金额显著不同（约 2.7 倍）


def test_pipeline_missing_workbook_record_blocked(tmp_path):
    """B-4②/B-7③：标的不在 workbook_map（母板真源无记录）→ 信号
    生成但执行被禁单（风险数据缺失），不按默认仓位成交。"""
    db = _fresh_db(tmp_path)
    _insert(db, "999999", _flat_drop(10.0, 8.9))
    cfg = _cfg(["999999"], _write_map(tmp_path, "attack"))
    _insert(db, "IDX000001", _idx_bars(27, on=True), market="index")
    pipeline_run(db, cfg)
    con = sqlite3.connect(db)
    fills = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    blk = con.execute("SELECT reason FROM blocked_signals").fetchall()
    con.close()
    assert fills == 0
    assert any("风险数据缺失" in r[0] for r in blk), blk
