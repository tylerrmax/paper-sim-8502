#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""待办自动隔离（第二层 P4）回归测试。

- 规则层：tmp 夹具 DB（从仓内 DB 复制后清空五表、按场景重建），
  断言 R1/R2/R3 命中集合、approvals 写入与审计 JSONL 精确一致、
  二次运行写入 0（幂等）、dry-run 零写入、日志故障时 DB 一行不动。
- 界面层：AppTest 在 tmp 仓库副本上跑（本机态 / 公网只读态），
  断言隔离后工作台待办计数下降、交易页已隔离区可见且带恢复按钮、
  只读态下写按钮全部禁用并出现只读标注。
"""
import json
import os
import re
import shutil
import sqlite3
from datetime import date, timedelta

import pytest
from streamlit.testing.v1 import AppTest

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SRC_DB = os.path.join(REPO, "db", "sim.db")
import sys
sys.path.insert(0, REPO)
from engine.todo_autoskip import run_autoskip  # noqa: E402

ACCT = "sim_a"


def _trading_days(n=12):
    """最近 n 个工作日（升序），末位为最近交易日。"""
    days, d = [], date.today()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(days)


def build_fixture_db(path):
    """构造场景 DB，返回 (days, expect_isolated)。

    场景（asof = days[-1]，年龄 = 信号日之后的交易日数）：
    - S1 SELL 300827 @days[-2]：无持仓、无成交 → R1 隔离
    - S2 SELL 002446 @days[-2]：有持仓（fills+positions）→ 保留
    - S3 BUY  600150 @days[-3]：年龄 2 → 保留
    - S4 BUY  688305 @days[-8]：年龄 7、未成交 → R2 隔离
    - S5 BUY  002472 @days[-4]：被 S6 取代 → R3 隔离
    - S6 BUY  002472 @days[-2]：最新 → 保留
    - S7 BUY  002812 @days[-2]：已有 fill → 非待办，勿动
    - S8 SELL 002639 @days[-2]：approvals 已 approved（人工已表态）→ 勿动
    """
    days = _trading_days()
    shutil.copy2(SRC_DB, path)
    con = sqlite3.connect(path)
    for t in ("signals", "approvals", "fills", "positions"):
        con.execute("DELETE FROM %s" % t)
    for d in days:
        con.execute(
            "INSERT OR REPLACE INTO market_data"
            "(symbol,date,open,high,low,close,volume,market)"
            " VALUES('IDX000001',?,3600,3610,3590,3605,100000000,'index')",
            (d,))

    def sig(d, sym, side, px):
        con.execute(
            "INSERT INTO signals(date,account,symbol,side,price,status)"
            " VALUES(?,?,?,?,?,'待执行')", (d, ACCT, sym, side, px))

    sig(days[-2], "300827", "SELL", 29.4)          # S1
    sig(days[-2], "002446", "SELL", 7.0)           # S2
    sig(days[-3], "600150", "BUY", 34.11)          # S3
    sig(days[-8], "688305", "BUY", 43.02)          # S4
    sig(days[-4], "002472", "BUY", 33.0)           # S5
    sig(days[-2], "002472", "BUY", 33.5)           # S6
    sig(days[-2], "002812", "BUY", 42.0)           # S7
    sig(days[-2], "002639", "SELL", 13.5)          # S8
    # S2 的持仓来源成交 + 当前持仓
    con.execute(
        "INSERT INTO fills(date,account,symbol,side,qty,price,fee,pnl,"
        "signal_date,strategy_version) VALUES(?,?,?, 'BUY',7200,6.94386,"
        "6.22,0,?,'v1.1-2026-10-26')", (days[-10], ACCT, "002446", days[-10]))
    con.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,last_price,"
        "market_value) VALUES(?,?,7200,6.94386,6.76,48672.0)",
        (ACCT, "002446"))
    # S7 的成交（signal_date 联结）
    con.execute(
        "INSERT INTO fills(date,account,symbol,side,qty,price,fee,pnl,"
        "signal_date,strategy_version) VALUES(?,?,?, 'BUY',500,42.0,3.7,0,"
        "?,'v1.1-2026-10-26')", (days[-1], ACCT, "002812", days[-2]))
    # S8 人工已批准
    con.execute(
        "INSERT INTO approvals(date,account,symbol,side,decision,updated)"
        " VALUES(?,?,?, 'SELL','approved','2026-11-07 00:00:00')",
        (days[-2], ACCT, "002639"))
    con.commit()
    con.close()
    expect = {(days[-2], "300827", "SELL"): "R1",
              (days[-8], "688305", "BUY"): "R2",
              (days[-4], "002472", "BUY"): "R3"}
    return days, expect


def _approvals(db):
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = con.execute(
        "SELECT date,account,symbol,side,decision FROM approvals"
        " ORDER BY date,symbol").fetchall()
    con.close()
    return rows


def _pending_counts(db):
    """与 app.py 工作台完全相同的两条计数口径。"""
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    orders = con.execute(
        "SELECT COUNT(*) FROM signals s"
        " LEFT JOIN fills f ON s.date=f.signal_date AND s.account=f.account"
        "   AND s.symbol=f.symbol AND s.side=f.side"
        " LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account"
        "   AND ap.symbol=s.symbol AND ap.side=s.side"
        " WHERE f.id IS NULL AND s.date >= date('now','-7 days')"
        "   AND (ap.decision IS NULL OR ap.decision <> 'skipped')").fetchone()[0]
    sigs = con.execute(
        "SELECT COUNT(*) FROM signals s"
        " LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account"
        "   AND ap.symbol=s.symbol AND ap.side=s.side"
        " LEFT JOIN fills f ON f.date=s.date AND f.account=s.account"
        "   AND f.symbol=s.symbol AND f.side=s.side"
        " WHERE s.date>=date('now','-60 days') AND ap.date IS NULL"
        "   AND f.date IS NULL").fetchone()[0]
    con.close()
    return orders, sigs


# ---------------- 规则层 ----------------

def test_rules_writes_and_audit(tmp_path):
    db = str(tmp_path / "sim.db")
    days, expect = build_fixture_db(db)
    log = str(tmp_path / "auto_skip_log.jsonl")
    decs = run_autoskip(db, log_path=log)
    got = {(d["date"], d["symbol"], d["side"]): d["rule"] for d in decs}
    assert got == expect
    # approvals 写入集合精确一致（另加 S8 的 approved 原样保留）
    rows = _approvals(db)
    skipped = {(r[0], r[2], r[3]) for r in rows if r[4] == "skipped"}
    assert skipped == set(expect)
    assert (days[-2], ACCT, "002639", "SELL", "approved") in rows
    # JSONL 精确：3 行，规则与证据齐全
    lines = open(log, encoding="utf-8").read().strip().split("\n")
    assert len(lines) == 3
    recs = {(r["date"], r["symbol"], r["side"]): r
            for r in (json.loads(x) for x in lines)}
    assert set(recs) == set(expect)
    r1 = recs[(days[-2], "300827", "SELL")]
    assert r1["rule"] == "R1"
    assert r1["evidence"]["pos_qty_now"] == 0
    assert r1["evidence"]["open_qty_at_signal_date"] == 0
    r2 = recs[(days[-8], "688305", "BUY")]
    assert r2["rule"] == "R2" and r2["evidence"]["age_trading_days"] == 7
    r3 = recs[(days[-4], "002472", "BUY")]
    assert r3["rule"] == "R3" and r3["evidence"]["superseded_by"] == days[-2]


def test_idempotent_second_run_writes_zero(tmp_path):
    db = str(tmp_path / "sim.db")
    build_fixture_db(db)
    log = str(tmp_path / "auto_skip_log.jsonl")
    first = run_autoskip(db, log_path=log)
    assert len(first) == 3
    before_rows = _approvals(db)
    before_log = open(log, encoding="utf-8").read()
    second = run_autoskip(db, log_path=log)
    assert second == []
    assert _approvals(db) == before_rows
    assert open(log, encoding="utf-8").read() == before_log


def test_dry_run_writes_nothing(tmp_path):
    db = str(tmp_path / "sim.db")
    days, expect = build_fixture_db(db)
    log = str(tmp_path / "auto_skip_log.jsonl")
    decs = run_autoskip(db, log_path=log, dry_run=True)
    assert {(d["date"], d["symbol"], d["side"]): d["rule"]
            for d in decs} == expect
    assert [r for r in _approvals(db) if r[4] == "skipped"] == []
    assert not os.path.exists(log)


def test_log_failure_leaves_db_untouched(tmp_path):
    db = str(tmp_path / "sim.db")
    build_fixture_db(db)
    blocker = tmp_path / "blocker"
    blocker.write_text("x")  # 文件冒充目录 → JSONL 打开必然失败
    bad_log = str(blocker / "auto_skip_log.jsonl")
    with pytest.raises(Exception):
        run_autoskip(db, log_path=bad_log)
    assert [r for r in _approvals(db) if r[4] == "skipped"] == []


def test_pending_counts_drop_by_isolated(tmp_path):
    db = str(tmp_path / "sim.db")
    build_fixture_db(db)
    o0, s0 = _pending_counts(db)
    # 待建(7自然日窗口):S1,S2,S3,S5,S6,S8(S4 在窗口外,S7 已成交)
    # 待审批(60日,ap 为空且 f.date=s.date 无成交):S1..S7
    # (面B按成交日联结,S7 成交日≠信号日仍被计入——app 既有口径,本批未动)
    assert (o0, s0) == (6, 7)
    run_autoskip(db, log_path=str(tmp_path / "log.jsonl"))
    o1, s1 = _pending_counts(db)
    # 待建窗口(7自然日)内被隔离:S1,S5 → -2;待审批窗口内被隔离:3 条 → -3
    assert (o1, s1) == (o0 - 2, s0 - 3)


# ---------------- 界面层（AppTest） ----------------

def _build_app_tree(tmp_path, autoskip=True):
    tree = str(tmp_path / "tree")
    shutil.copytree(
        REPO, tree,
        ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__",
                                      "evidence", "tests",
                                      "pytest-of-root", ".tmp_pw"))
    db = os.path.join(tree, "db", "sim.db")
    days, expect = build_fixture_db(db)
    if autoskip:
        log = os.path.join(tree, "reports", "auto_skip_log.jsonl")
        run_autoskip(db, log_path=log)
    return tree, days, expect


def _goto_trade(at):
    for w in at.radio:
        opts = [str(o) for o in w.options]
        if any("交易" in o for o in opts):
            w.set_value([o for o in opts if "交易" in o][0])
            break
    at.run()
    assert not at.exception, f"交易页异常: {at.exception}"


def _workbench_counts(at):
    for m in at.markdown:
        v = str(m.value)
        mo = re.search(r"待审批 <b class=\"num\">(\d+)</b> · "
                       r"待建条件单 <b class=\"num\">(\d+)</b>", v)
        if mo:
            return int(mo.group(1)), int(mo.group(2))
    raise AssertionError("工作台未找到待办计数")


@pytest.fixture(autouse=True)
def _no_screen_record(monkeypatch):
    monkeypatch.setenv("PAPER_SIM_NO_SCREEN_RECORD", "1")


def test_apptest_counts_drop_and_isolated_zone(tmp_path):
    tree_pre, days, expect = _build_app_tree(tmp_path / "pre", autoskip=False)
    at = AppTest.from_file(os.path.join(tree_pre, "app.py"),
                           default_timeout=60)
    at.run()
    assert not at.exception
    sig_pre, ord_pre = _workbench_counts(at)

    tree, _, _ = _build_app_tree(tmp_path / "post", autoskip=True)
    at2 = AppTest.from_file(os.path.join(tree, "app.py"),
                            default_timeout=60)
    at2.run()
    assert not at2.exception
    sig_post, ord_post = _workbench_counts(at2)
    assert (sig_pre - sig_post, ord_pre - ord_post) == (3, 2)

    _goto_trade(at2)
    labels = [str(e.label) for e in at2.expander]
    assert any("已隔离" in x for x in labels), labels
    texts = " ".join(str(m.value) for m in at2.markdown)
    assert "自动隔离·R1" in texts
    restore = [b for b in at2.button if "恢复" in str(b.label)]
    assert restore and all(not b.disabled for b in restore)
    skip = [b for b in at2.button
            if "跳过" in str(b.label) or "已建单" in str(b.label)]
    assert skip and all(not b.disabled for b in skip)
    captions = " ".join(str(c.value) for c in at2.caption)
    assert "只读展示" not in captions


def test_apptest_cloud_readonly_disables_write_buttons(tmp_path, monkeypatch):
    tree, days, expect = _build_app_tree(tmp_path, autoskip=True)
    monkeypatch.setenv("STREAMLIT_CLOUD", "1")
    at = AppTest.from_file(os.path.join(tree, "app.py"),
                           default_timeout=60)
    at.run()
    assert not at.exception
    _goto_trade(at)
    captions = " ".join(str(c.value) for c in at.caption)
    assert "只读展示，处理请用本机版" in captions
    write_btns = [b for b in at.button
                  if any(k in str(b.label)
                         for k in ("已建单", "跳过", "恢复"))]
    assert write_btns, "只读态下应仍渲染写按钮（禁用态）"
    assert all(b.disabled for b in write_btns), \
        [(str(b.label), b.disabled) for b in write_btns]
