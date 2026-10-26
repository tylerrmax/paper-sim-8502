#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""影子样本补齐（四项补齐②）隔离回归测试。

锁定两组事实（核查报告 workspace/shadow_isolation/findings.md）：
记录面（补齐后成立）：
  1. 被硬闸门封顶的标的以影子组（grp='shadow'）入册并回填前向
     收益，与 S/A 组分开统计、互不混样；因子落行同步扩到影子组。
  2. blocked_signals 的 BUY 拦截有 5/20 日前向收益回填
     （blocked_perf），且回填只读事实表、不动成交三表。
  3. 夜跑 run_daily 在主链后固定挂影子记录步骤，失败留 FAIL 行。
隔离面（结构锁，防回归）：
  4. 影子表写满假数据（含一只满分 S 级）后，pipeline 的
     signals/fills/approvals 摘要逐项不变；blocked 行永不生成审批。
  5. 毕业口径 build_graduation 不读任何影子表：塞满/清空影子表
     两次计算结果逐字段相等。
  6. engine/pipeline.py 全文零 'screen' 引用（分离锁）；
     screen 侧文件（engine/screen.py、tools/screen_perf.py）的
     写目标只限影子表 {screen_runs, screen_perf,
     screen_perf_factors, blocked_perf}。

安全：全部写操作只在 tmp fixture/生产库副本上；生产 db/sim.db
仅以文件复制方式读取。运行:
.venv/bin/python -m pytest tests/test_shadow_isolation.py -q
"""
import ast
import importlib.util
import json
import os
import re
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

from engine.pipeline import run as pipeline_run  # noqa: E402
from engine.screen import record_screen_run  # noqa: E402

SHADOW_TABLES = {"screen_runs", "screen_perf", "screen_perf_factors",
                 "blocked_perf"}

BLOCKED_DDL = """CREATE TABLE IF NOT EXISTS blocked_signals(
    date TEXT, signal_date TEXT, account TEXT, symbol TEXT, side TEXT,
    signal_price REAL, exec_price REAL, stop_ref REAL, reason TEXT,
    created_at TEXT,
    PRIMARY KEY(date, account, symbol, side))"""


def _load_perf_tool():
    spec = importlib.util.spec_from_file_location(
        "screen_perf_tool",
        os.path.join(BASE, "tools", "screen_perf.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_cfg():
    with open(os.path.join(BASE, "config", "strategy.yaml"),
              encoding="utf-8") as f:
        return yaml.safe_load(f)


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


def _weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _insert_bars(db, symbol, dates, closes, market="a"):
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT OR REPLACE INTO market_data"
        "(symbol,date,open,high,low,close,volume,market)"
        " VALUES(?,?,?,?,?,?,?,?)",
        [(symbol, d.isoformat(), c, c, c, c, 1000, market)
         for d, c in zip(dates, closes)])
    con.commit()
    con.close()


def _count(db, table):
    con = sqlite3.connect(db)
    try:
        n = con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    except sqlite3.OperationalError:
        n = 0
    con.close()
    return n


# ---------- 1. 封顶标的进影子组（G1） ----------

def test_capped_symbol_tracked_as_shadow(tmp_path):
    perf = _load_perf_tool()
    db = _fresh_fixture_db(tmp_path)
    dates = _weekdays(date(2026, 8, 3), 60)
    asof = dates[30]
    closes_a = [10.0 + 0.1 * i for i in range(60)]   # 被封顶标的 TA
    closes_b = [20.0 + 0.2 * i for i in range(60)]   # 正常 S 级 TB
    _insert_bars(db, "TA", dates, closes_a)
    _insert_bars(db, "TB", dates, closes_b)

    rows = [
        {"symbol": "TA", "grade": "B", "total": 74.4, "capped": True,
         "gates": ["价格<母板止损参考（16.19<16.30）"],
         "close": closes_a[30], "factors": {"flags": ["因子甲"]}},
        {"symbol": "TB", "grade": "S", "total": 88.0, "capped": False,
         "gates": [], "close": closes_b[30],
         "factors": {"flags": ["因子甲", "因子乙"]}},
        {"symbol": "TC", "grade": "C", "total": 30.0, "capped": False,
         "gates": [], "close": None, "factors": {"flags": []}},
    ]
    con = sqlite3.connect(db)
    record_screen_run(con, asof, rows, "news_sha", "map_sha")
    con.close()

    summary = perf.run(db)
    assert summary["groups"] == {"sa": 1, "shadow": 1, "other": 1}

    con = sqlite3.connect(db)
    got = {r[0]: r for r in con.execute(
        "SELECT symbol, grp, capped, gates, fwd5_ret, fwd5_excess"
        " FROM screen_perf")}
    # 影子组：TA 在册、标记齐全、前向收益按行情回填
    assert got["TA"][1] == "shadow" and got["TA"][2] == 1
    assert "母板止损参考" in json.loads(got["TA"][3])[0]
    exp_ret_a = closes_a[35] / closes_a[30] - 1.0
    exp_ret_b = closes_b[35] / closes_b[30] - 1.0
    pool_avg = (exp_ret_a + exp_ret_b) / 2.0
    assert got["TA"][4] == pytest.approx(exp_ret_a)
    assert got["TA"][5] == pytest.approx(exp_ret_a - pool_avg)
    # S/A 组：TB 在册，且 TA 不混入 sa 组
    assert got["TB"][1] == "sa"
    assert con.execute(
        "SELECT COUNT(*) FROM screen_perf WHERE grp='sa' AND symbol='TA'"
    ).fetchone()[0] == 0
    # 因子落行扩到影子组且带组别；与评级解耦
    fac = {(r[0], r[1]): r[2] for r in con.execute(
        "SELECT symbol, factor, grp FROM screen_perf_factors")}
    assert fac[("TA", "因子甲")] == "shadow"
    assert fac[("TB", "因子甲")] == "sa"
    assert fac[("TB", "因子乙")] == "sa"
    con.close()


def test_perf_dedupes_same_asof_runs(tmp_path):
    """同 asof 双写（页面+夜跑）只取最新一期回填，样本不重复计。"""
    perf = _load_perf_tool()
    db = _fresh_fixture_db(tmp_path)
    dates = _weekdays(date(2026, 8, 3), 40)
    asof = dates[10]
    _insert_bars(db, "TB", dates, [20.0 + 0.2 * i for i in range(40)])
    row = {"symbol": "TB", "grade": "A", "total": 70.0, "capped": False,
           "gates": [], "close": 22.0, "factors": {"flags": []}}
    con = sqlite3.connect(db)
    record_screen_run(con, asof, [row], "s1", "m1")
    record_screen_run(con, asof, [row], "s2", "m2")  # 同 asof 第二期
    con.close()
    perf.run(db)
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM screen_perf").fetchone()[0]
    run_ids = [r[0] for r in con.execute("SELECT run_id FROM screen_perf")]
    con.close()
    assert n == 1 and run_ids == [2]  # 只回填最新一期


# ---------- 2. 拦截样本前向回填只读（G3） ----------

def test_blocked_forward_fill_is_readonly(tmp_path):
    perf = _load_perf_tool()
    db = _fresh_fixture_db(tmp_path)
    dates = _weekdays(date(2026, 10, 1), 45)
    exec_day = "2026-10-20"
    idx_exec = [d.isoformat() for d in dates].index(exec_day)
    closes = []
    for i in range(len(dates)):
        k = i - idx_exec
        closes.append(10.0 if k <= 0 else 10.0 + 0.1 * k)
    _insert_bars(db, "TC", dates, closes)
    _insert_bars(db, "TD", dates, closes)

    con = sqlite3.connect(db)
    con.execute(BLOCKED_DDL)
    con.execute(
        "INSERT INTO blocked_signals VALUES(?,?,?,?,?,?,?,?,?,?)",
        (exec_day, "2026-10-19", "sim_a", "TC", "BUY", 10.0, 10.0, 9.5,
         "跌破母板止损参考（测试）", "2026-10-20 17:12:00"))
    con.execute(
        "INSERT INTO blocked_signals VALUES(?,?,?,?,?,?,?,?,?,?)",
        (exec_day, "2026-10-19", "sim_a", "TD", "SELL", 10.0, 10.0, None,
         "测试 SELL 拦截", "2026-10-20 17:12:00"))
    con.commit()
    before_blocked = con.execute(
        "SELECT * FROM blocked_signals ORDER BY symbol").fetchall()
    n = perf.backfill_blocked_perf(con)
    con.commit()
    # 只收 BUY；SELL 不生成「若成交」样本
    assert n == 1
    row = con.execute(
        "SELECT base_price, fwd5_ret, fwd20_ret FROM blocked_perf"
        " WHERE symbol='TC'").fetchone()
    assert row[0] == 10.0
    assert row[1] == pytest.approx(0.05)   # 第 5 根：10.5 / 10.0 - 1
    assert row[2] == pytest.approx(0.20)   # 第 20 根：12.0 / 10.0 - 1
    assert con.execute(
        "SELECT COUNT(*) FROM blocked_perf WHERE symbol='TD'"
    ).fetchone()[0] == 0
    # 事实表逐字不变、成交三表纹丝不动
    assert con.execute(
        "SELECT * FROM blocked_signals ORDER BY symbol").fetchall() \
        == before_blocked
    for t in ("fills", "signals", "approvals"):
        assert con.execute(
            "SELECT COUNT(*) FROM %s" % t).fetchone()[0] == 0
    con.close()


# ---------- 3. 影子数据永不进成交链路（G4/G5） ----------

def test_screen_data_never_creates_signals(tmp_path):
    db = _fresh_fixture_db(tmp_path)
    cfg = _load_cfg()
    dates = _weekdays(date(2026, 9, 21), 30)
    _insert_bars(db, "002446", dates, [10.0] * 30)  # 横盘：无信号
    pipeline_run(db, cfg)
    base = {t: _count(db, t)
            for t in ("signals", "fills", "approvals", "blocked_signals")}
    assert base["signals"] == 0 and base["fills"] == 0

    # 影子侧塞满假数据：一只满分 S 级 + 一条 BUY 拦截留痕
    perfect = {"symbol": "002446", "grade": "S", "total": 100.0,
               "capped": False, "gates": [], "close": 10.0,
               "factors": {"flags": ["因子甲"]}}
    con = sqlite3.connect(db)
    record_screen_run(con, dates[-1], [perfect], "s", "m")
    con.execute(
        "INSERT OR IGNORE INTO blocked_signals VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("2026-10-30", "2026-10-29", "sim_a", "002446", "BUY", 10.0, 10.0,
         7.70, "测试拦截留痕", "2026-10-30 17:12:00"))
    con.commit()
    con.close()
    _load_perf_tool().run(db)  # 影子回填全跑一遍（含 blocked_perf）
    base["blocked_signals"] += 1  # 仅多出手工塞入的这一条

    pipeline_run(db, cfg)
    after = {t: _count(db, t)
             for t in ("signals", "fills", "approvals", "blocked_signals")}
    assert after == base          # 摘要逐项不变
    assert after["approvals"] == 0  # blocked 行永不生成审批


def test_graduation_ignores_shadow_tables(tmp_path):
    prod_copy = str(tmp_path / "copy.db")
    shutil.copy2(PROD_DB, prod_copy)
    from sync_snapshot import build_graduation
    con = sqlite3.connect(prod_copy)
    g1 = build_graduation(con)
    # 影子表塞满假数据
    perf = _load_perf_tool()
    perf.ensure_tables(con)
    perfect = {"symbol": "002446", "grade": "S", "total": 100.0,
               "capped": False, "gates": [], "close": 10.0,
               "factors": {"flags": ["因子甲"]}}
    record_screen_run(con, date(2026, 10, 26), [perfect], "s", "m")
    con.execute(
        "INSERT OR REPLACE INTO screen_perf(run_id,asof,symbol,grade,"
        "total,base_close,grp,fwd5_ret,fwd20_ret,updated_at)"
        " VALUES(999,'2026-10-26','002446','S',100.0,10.0,'sa',9.9,9.9,"
        "'2026-10-27 00:00:00')")
    con.execute(
        "INSERT OR IGNORE INTO blocked_signals VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("2026-10-26", "2026-10-23", "sim_a", "002446", "BUY", 10.0, 10.0,
         7.70, "测试假拦截", "2026-10-27 00:00:00"))
    con.commit()
    g2 = build_graduation(con)
    con.close()
    assert g1 == g2  # 全部输出字段逐一相等


# ---------- 4. 分离锁（G5） ----------

def test_pipeline_has_no_screen_reference():
    path = os.path.join(BASE, "engine", "pipeline.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "screen" not in src  # 与核查证据一致：全文零命中
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
    assert not any("screen" in m for m in imported)


def test_screen_side_writes_only_shadow_tables():
    pat = re.compile(
        r"(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|CREATE\s+TABLE(?:\s+IF\s+NOT"
        r"\s+EXISTS)?)\s+(\w+)", re.IGNORECASE)
    for rel in ("engine/screen.py", "tools/screen_perf.py"):
        with open(os.path.join(BASE, rel), encoding="utf-8") as f:
            targets = {m.group(1) for m in pat.finditer(f.read())}
        assert targets, rel
        assert targets <= SHADOW_TABLES, (rel, targets - SHADOW_TABLES)


def test_nightly_wires_shadow_step():
    with open(os.path.join(BASE, "run_daily.py"), encoding="utf-8") as f:
        src = f.read()
    # 主链（日报）之后才跑影子步骤；失败留 FAIL 行，不得静默
    assert src.index("write_report") < src.index("_record_shadow_samples(DB)")
    assert "FAIL 影子记录（screen/perf）" in src
    assert "screen_all" in src and "screen_perf" in src
