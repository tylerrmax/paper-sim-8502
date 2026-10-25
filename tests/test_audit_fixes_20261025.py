#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""架构审计最小改造清单（2026-10-25）专项验收测试。

对应报告 §十一的验收测法与主代理 7 项特别验收：
① 禁入优先：价<stop_ref 的 buyback BUY 跑 pipeline → fills 0、blocked 1
② screen 真 as-of：asof=2026-10-20 → close 为 10-20 收盘、新闻只含 ≤10-20
③ 可复现：同 asof 连跑两次 → screen_runs 两行 results_json 逐字一致
④ 新闻断更：sector_news 改旧 30 天 → 健康检查 blocked + 评分标注断更
⑤ 港股闸门：HK9660 用 10-24 数据不再触发 8.1% 追高闸（其余闸门照常）
⑥ 港股费用：pipeline 与 order_validate 同源 = max(额×0.108%, 50)
⑦ 002446 三处“是否破止损”结论一致（母板 stop_ref 7.70，已破）

安全：所有 pipeline/screen 写操作只在 tmp 副本/合成 fixture 上；
生产 db/sim.db 只读打开。运行: .venv/bin/python -m pytest tests/test_audit_fixes_20261025.py -q
"""
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, timedelta

import pytest
import yaml

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
PROD_DB = os.path.join(BASE, "db", "sim.db")
APP_PATH = os.path.join(BASE, "app.py")

from engine import news_sentiment  # noqa: E402
from engine.news_sentiment import news_freshness, sector_sentiment  # noqa: E402
from engine.pipeline import _fee_for, run as pipeline_run  # noqa: E402
from engine.screen import screen_all  # noqa: E402


def _load_cfg():
    with open(os.path.join(BASE, "config", "strategy.yaml"),
              encoding="utf-8") as f:
        return yaml.safe_load(f)


def _prod_copy(tmp_path):
    db = str(tmp_path / "copy.db")
    shutil.copy2(PROD_DB, db)
    return db


# ---------- ① 禁入优先（合成 fixture，全新库） ----------

def _fresh_fixture_db(tmp_path):
    db = str(tmp_path / "fix.db")
    r = subprocess.run([sys.executable, os.path.join(BASE, "init_db.py"),
                        "--db", db], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db)
    con.execute("UPDATE control SET status='running', mode='auto' WHERE id=1")
    con.commit()
    con.close()
    return db


def _insert_bars(db, symbol, bars):
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT OR REPLACE INTO market_data(symbol,date,open,high,low,close,volume,market)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(symbol,) + b + ("a",) for b in bars])
    con.commit()
    con.close()


def _flat_then_drop(flat, drop_close, n_flat=25):
    """25 根横盘（高点=flat）+ 1 根跌至 drop_close（触发 buyback BUY）
    + 1 根执行日。信号日 = 第 26 根 = 2026-10-18（≥ VAL_START）。"""
    start = date(2026, 9, 23)
    out = []
    for i in range(n_flat):
        d = (start + timedelta(days=i)).isoformat()
        out.append((d, flat, flat, flat, flat, 100000.0))
    d = (start + timedelta(days=n_flat)).isoformat()
    out.append((d, flat, flat, drop_close * 0.99, drop_close, 100000.0))
    d2 = (start + timedelta(days=n_flat + 1)).isoformat()
    out.append((d2, drop_close, drop_close, drop_close, drop_close, 100000.0))
    return out


def _cfg_sim_a_only():
    cfg = copy.deepcopy(_load_cfg())
    cfg["accounts"] = [a for a in cfg["accounts"] if a["id"] == "sim_a"]
    cfg["universe"] = {"sim_a": ["002446"]}
    return cfg


def test_blocked_buyback_below_stop_ref(tmp_path):
    """① 信号价 7.60 < stop_ref 7.70 的 buyback BUY：信号照常生成，
    执行被禁入闸拦下 → fills 0、blocked_signals 1 条且原因正确。"""
    db = _fresh_fixture_db(tmp_path)
    _insert_bars(db, "002446", _flat_then_drop(8.5, 7.6))  # 7.6 ≤ 8.5×0.9=7.65
    pipeline_run(db, _cfg_sim_a_only())
    con = sqlite3.connect(db)
    fills = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    sigs = con.execute(
        "SELECT side,price FROM signals WHERE symbol='002446'").fetchall()
    blk = con.execute(
        "SELECT symbol,signal_price,stop_ref,reason FROM blocked_signals").fetchall()
    con.close()
    assert ("BUY", 7.6) in sigs, f"BUY 信号应照常生成，实际 {sigs}"
    assert fills == 0, f"禁入标的不应成交，fills={fills}"
    assert len(blk) == 1 and blk[0][0] == "002446"
    assert blk[0][1] == 7.6 and blk[0][2] == 7.7
    assert "跌破母板止损参考" in blk[0][3]


def test_buyback_above_stop_ref_still_fills(tmp_path):
    """① 对照组：信号价 8.90 > stop_ref 7.70 的 buyback BUY 应正常成交，
    证明禁入闸只拦破止损、不误伤正常信号。"""
    db = _fresh_fixture_db(tmp_path)
    _insert_bars(db, "002446", _flat_then_drop(10.0, 8.9))  # 8.9 ≤ 10×0.9=9.0
    pipeline_run(db, _cfg_sim_a_only())
    con = sqlite3.connect(db)
    fills = con.execute(
        "SELECT side,qty FROM fills WHERE symbol='002446'").fetchall()
    blk = con.execute("SELECT COUNT(*) FROM blocked_signals").fetchone()[0]
    con.close()
    assert len(fills) == 1 and fills[0][0] == "BUY" and fills[0][1] > 0
    assert blk == 0


# ---------- ②③ screen 真 as-of + 可复现（生产库副本） ----------

def test_screen_asof_truncation(tmp_path):
    """② asof=2026-10-20：002446 close 必须是 10-20 收盘 7.19（不是
    10-23 的 7.30）；新闻只计 ≤10-20 条目（semi 只剩 Danny QCOM +0.4）。"""
    db = _prod_copy(tmp_path)
    rows = screen_all(db, "2026-10-20", record=False)
    r = {x["symbol"]: x for x in rows}["002446"]
    assert r["last_date"] == "2026-10-20"
    assert abs(r["close"] - 7.19) < 1e-9
    s = sector_sentiment("semi", date(2026, 10, 20))
    assert all(i["date"] <= "2026-10-20" for i in s["items"])
    assert abs(s["raw"] - 0.4) < 1e-9  # Danny QCOM 1×0.4×衰减1.0
    rows25 = screen_all(db, "2026-10-25", record=False)
    t20 = {x["symbol"]: x["total"] for x in rows}
    t25 = {x["symbol"]: x["total"] for x in rows25}
    assert t20 != t25, "不同 asof 结果必须不同（改造前逐值相同是 bug）"


def test_screen_runs_reproducible(tmp_path):
    """③ 同 asof 连跑两次（落库）→ screen_runs 两行，results_json 逐字一致。"""
    db = _prod_copy(tmp_path)
    screen_all(db, "2026-10-20", record=True)
    screen_all(db, "2026-10-20", record=True)
    con = sqlite3.connect(db)
    got = con.execute(
        "SELECT results_json, rule_version, news_sha256, map_sha256"
        " FROM screen_runs WHERE asof='2026-10-20' ORDER BY id").fetchall()
    con.close()
    assert len(got) == 2
    assert got[0][0] == got[1][0], "results_json 必须逐字一致"
    assert got[0][1] == got[1][1] and got[0][2] and got[0][3]


# ---------- ④ 新闻断更 ----------

OLD_ENTRIES = [{"date": "2026-09-25", "direction": 1, "strength": 3,
                "source": "Serenity", "sector_key": "semi",
                "evidence": "旧证据（30 天前）"}]


def test_news_stale_flagged_not_silent(tmp_path):
    """④ 新闻账最新条目距 asof 30 天：新鲜度判定 stale、评分全员
    附加断更闸门并在证据行标注——不是静默 0 分。"""
    assert news_freshness(date(2026, 10, 25), OLD_ENTRIES)["stale"] is True
    db = _prod_copy(tmp_path)
    rows = screen_all(db, "2026-10-25", entries=OLD_ENTRIES, record=False)
    for r in rows:
        assert any("新闻面断更" in g for g in r["gates"]), r["symbol"]
        assert "新闻面断更" in r["evidence"], r["symbol"]
        assert r["grade"] not in ("S", "A")


def test_health_check_blocks_on_stale_news(monkeypatch):
    """④ 健康检查：load_news 返回 30 天前条目时，check_system_health
    必须给出含“新闻面断更”的阻断原因（AppTest 跑真实 app 代码路径，
    并断言顶部阻断横幅实际显示该原因）。"""
    monkeypatch.setattr(news_sentiment, "load_news",
                        lambda path=None: OLD_ENTRIES)
    monkeypatch.setenv("PAPER_SIM_NO_SCREEN_RECORD", "1")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    assert not at.exception, f"应用加载异常: {at.exception}"
    texts = ([str(m.value) for m in at.markdown] +
             [str(e.value) for e in at.error] +
             [str(t.value) for t in at.text])
    assert any("新闻面断更" in t for t in texts), "顶部阻断原因应含新闻面断更"


# ---------- ⑤ 港股闸门分市场 ----------

def test_hk_chg_gate_uses_15pct(tmp_path):
    """⑤ HK9660 10-23 收盘 3.785（较 10-22 +8.1%）：港股阈值 15%，
    不再触发追高闸；但跌破母板止损（3.785<3.95）等其余闸门照常。"""
    db = _prod_copy(tmp_path)
    rows = screen_all(db, "2026-10-25", record=False)
    r = {x["symbol"]: x for x in rows}["HK9660"]
    assert abs(r["close"] - 3.785) < 1e-9
    assert not any("单日涨幅" in g for g in r["gates"]), r["gates"]
    assert any("止损参考" in g for g in r["gates"]), r["gates"]


# ---------- ⑥ 港股费用同源 ----------

def test_hk_fee_pipeline_matches_validate():
    """⑥ HK 小额买单：pipeline _fee_for 读 yaml → (0.00108, 50)；
    sim.buy 实算 fee = max(1000×0.108%, 50) = 50；order_validate 的
    _fee_est 扣除滑点后的费率部分同为 50。"""
    cfg = _load_cfg()
    rate, min_fee = _fee_for("sim_hk", "BUY", cfg)
    assert rate == cfg["fees"]["hk_buy"] == 0.00108
    assert min_fee == cfg["fees"]["hk_min_fee"] == 50
    from engine.sim import SimAccount
    sim = SimAccount(id="sim_hk", ccy="HKD", cash=100000.0, t1=False)
    fee = sim.buy("HK00001", 100, 10.0, "2026-10-24", rate, min_fee)
    assert fee == 50.0  # 名义额 1000，比例费 1.08 < 最低 50
    from engine.order_validate import _fee_est
    est = _fee_est(cfg, "sim_hk", 1000.0)
    assert abs((est - 1000.0 * cfg["fees"]["slippage"]) - 50.0) < 1e-9


# ---------- P1-11 交易计划三价 ----------

def test_trade_plan_three_prices(tmp_path):
    """P1-11：每个候选带 plan_trigger/plan_stop/plan_target；
    002639 手算复核：触发 14.9（母板突破位）、止损 12.8（stop_ref）、
    目标 = 14.9 + (14.9-12.8)×2.5 = 20.15；证据行标注 2.5 倍口径；
    落库 results_json 同步带三字段。"""
    db = _prod_copy(tmp_path)
    rows = screen_all(db, "2026-10-25", record=True)
    by = {x["symbol"]: x for x in rows}
    r = by["002639"]
    assert r["grade"] in ("S", "A", "B")
    assert r["plan_trigger"] == 14.9 and r["plan_stop"] == 12.8
    assert abs(r["plan_target"] - 20.15) < 1e-9
    assert "目标位按2.5倍盈亏比计算" in r["evidence"]
    for x in rows:  # 全池公式一致性
        assert x["plan_target"] == round(
            x["plan_trigger"] + (x["plan_trigger"] - x["plan_stop"]) * 2.5, 4)
    con = sqlite3.connect(db)
    saved = json.loads(con.execute(
        "SELECT results_json FROM screen_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()[0])
    con.close()
    s = {x["symbol"]: x for x in saved}["002639"]
    assert s["plan_trigger"] == 14.9 and s["plan_stop"] == 12.8
    assert abs(s["plan_target"] - 20.15) < 1e-9


def test_trade_plan_missing_motherboard_values():
    """P1-11：母板缺突破位/止损位 → 字段 None、证据标注“母板未设”，不编造。"""
    from engine.screen import score_symbol
    meta = {"name": "测试标的", "sector_label": None, "sector_key": None}
    r = score_symbol("TEST1", meta, [], date(2026, 10, 25), 60.0)
    assert r["plan_trigger"] is None and r["plan_stop"] is None
    assert r["plan_target"] is None
    assert "母板未设" in r["evidence"]
    meta2 = dict(meta, breakout=10.0)  # 只缺止损位
    r2 = score_symbol("TEST2", meta2, [], date(2026, 10, 25), 60.0)
    assert r2["plan_trigger"] == 10.0 and r2["plan_target"] is None
    assert "母板未设" in r2["evidence"] and "止损位" in r2["evidence"]


# ---------- ⑦ 002446 三处止损结论一致 ----------

def test_002446_stop_conclusion_consistent(tmp_path, monkeypatch):
    """⑦ 002446 现价 7.30 < 母板 stop_ref 7.70：
    - 数据层：stop_ref_of('002446') == 7.7（app 与 screen 同源 workbook_map）；
    - 选股页：screen 闸门含“价格<母板止损参考（7.30<7.70）”；
    - 工作台：AppTest 真实渲染出“破止损仍持仓 … 母板止损参考 7.70”警报；
    - 代码层：app.py 不再有成本×0.95 的破止损判定（旧口径已清除）。"""
    src = open(APP_PATH, encoding="utf-8").read()
    assert "_avg * 0.95" not in src and "成本×0.95" not in src
    assert src.count("stop_ref_of(") >= 3  # 定义 + 工作台 + 验证页
    db = _prod_copy(tmp_path)
    rows = screen_all(db, "2026-10-25", record=False)
    r = {x["symbol"]: x for x in rows}["002446"]
    assert any("7.30<7.70" in g for g in r["gates"]), r["gates"]
    monkeypatch.setenv("PAPER_SIM_NO_SCREEN_RECORD", "1")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    assert not at.exception, f"应用加载异常: {at.exception}"
    texts = " ".join([str(m.value) for m in at.markdown] +
                     [str(e.value) for e in at.error])
    assert "破止损仍持仓" in texts and "002446" in texts and "7.70" in texts
