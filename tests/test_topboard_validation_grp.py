#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TOP 榜验证列 grp 口径回归（四项补齐②b，2026-10-27）。

背景：②落地后 screen_perf 是全集分组登记（grp=sa/shadow/other）。
选股页 TOP 榜的"验证列"（app._topboard_data）设计口径是正常入选
样本（S/A）的前向表现，聚合时必须只计 sa 组：影子组（被硬闸门
封顶）与 other 组样本不得无标注混入。历史行 grp 为空时，按
tools/screen_perf.py 的 _group_of 口径推导（未封顶且 S/A → sa，
封顶 → shadow）。

测法：app.py 是 Streamlit 脚本、不能直接 import，故用 ast 从源码
抽出 _topboard_data 函数本体 exec 后对 fixture 库调用——测的就是
上线代码原文，函数改名/改签名会让本测试响亮失败。
"""
import ast
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")


def _load_topboard_data():
    with open(APP_PATH, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_topboard_data":
            seg = ast.get_source_segment(src, node)
            ns = {"sqlite3": sqlite3}
            exec(compile(seg, APP_PATH, "exec"), ns)
            return ns["_topboard_data"]
    raise AssertionError("app.py 中未找到 _topboard_data")


_PERF_DDL = """CREATE TABLE screen_perf(
    run_id INTEGER NOT NULL, asof TEXT NOT NULL, symbol TEXT NOT NULL,
    grade TEXT, total REAL, base_close REAL, capped INTEGER DEFAULT 0,
    gates TEXT, grp TEXT,
    fwd5_date TEXT, fwd5_ret REAL, fwd5_excess REAL,
    fwd20_date TEXT, fwd20_ret REAL, fwd20_excess REAL,
    updated_at TEXT, PRIMARY KEY(run_id, symbol))"""

_RUNS_DDL = """CREATE TABLE screen_runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT NOT NULL,
    asof TEXT NOT NULL, rule_version TEXT NOT NULL,
    news_sha256 TEXT, map_sha256 TEXT, results_json TEXT NOT NULL)"""


def _fixture_db(tmp_path):
    db = str(tmp_path / "sim.db")
    con = sqlite3.connect(db)
    con.execute(_RUNS_DDL)
    con.execute(_PERF_DDL)
    rows = [{"symbol": s, "total": 90.0 - i}
            for i, s in enumerate(["AAA", "BBB", "CCC", "DDD"])]
    con.execute(
        "INSERT INTO screen_runs(run_at,asof,rule_version,results_json)"
        " VALUES('2026-10-20 17:12:00','2026-10-20','test',?)",
        (json.dumps(rows),))
    ins = ("INSERT INTO screen_perf(run_id,asof,symbol,grade,capped,grp,"
           "fwd5_ret,fwd20_ret) VALUES(?,'2026-10-20',?,?,?,?,?,?)")
    # AAA：sa 一条（应计）+ shadow 一条 + other 一条（均不得混入；
    # 同一标的不同组别来自不同期，run_id 各异）
    con.execute(ins, (1, "AAA", "A", 0, "sa", 0.10, 0.20))
    con.execute(ins, (2, "AAA", "B", 1, "shadow", -0.50, -0.60))
    con.execute(ins, (3, "AAA", "B", 0, "other", 0.99, 0.99))
    # BBB：历史行 grp 为空、未封顶 A 级 → 按 _group_of 推导为 sa（应计）
    con.execute(ins, (1, "BBB", "A", 0, None, 0.05, 0.06))
    # CCC：历史行 grp 为空但已封顶 → 推导为 shadow（不得计）
    con.execute(ins, (1, "CCC", "B", 1, None, 0.30, 0.30))
    # DDD：sa 组但 5 日未到期（fwd5 为空）→ n=0
    con.execute(ins, (1, "DDD", "S", 0, "sa", None, None))
    con.commit()
    con.close()
    return db


def test_topboard_validation_counts_sa_only(tmp_path):
    fn = _load_topboard_data()
    asof, _ver, rows = fn(_fixture_db(tmp_path))
    assert asof == "2026-10-20"
    perf = {r["symbol"]: r["_perf"] for r in rows}
    # AAA 只计 sa 那一条：n=1、均值就是 sa 的 0.10/0.20、胜率 100%
    assert perf["AAA"]["n"] == 1
    assert abs(perf["AAA"]["avg5"] - 0.10) < 1e-9
    assert abs(perf["AAA"]["avg20"] - 0.20) < 1e-9
    assert perf["AAA"]["win5"] == 1.0
    # BBB 历史行（grp 空、未封顶 A）计入
    assert perf["BBB"]["n"] == 1
    assert abs(perf["BBB"]["avg5"] - 0.05) < 1e-9
    # CCC 历史行（grp 空、已封顶）不计入
    assert perf["CCC"]["n"] == 0
    assert perf["CCC"]["avg5"] is None
    # DDD sa 但未到期：n=0
    assert perf["DDD"]["n"] == 0
