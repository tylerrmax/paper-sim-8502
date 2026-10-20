#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号前置校验 / 风控硬拦截 / 验证 KPI 状态 单元测试（2026-10-20）。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from engine.signal_guard import check_signal, check_exit_price, risk_breaches


def test_sell_without_position_is_invalid():
    assert "无持仓，不能卖出" in check_signal("SELL", "002639", 0, [], price=15.2)
    assert "无持仓，不能卖出" in check_signal("SELL", "688305", None, [], price=45.33)


def test_sell_with_position_ok():
    assert check_signal("SELL", "002446", 7200, [], price=7.18) == []


def test_buy_display_only_is_invalid():
    issues = check_signal("BUY", "002639", 0, ["002639", "600150"], price=14.51)
    assert any("display_only" in i for i in issues)


def test_display_only_int_symbols_normalized():
    # yaml 未加引号时可能被解析成 int
    assert check_signal("BUY", "002639", 0, [2639], price=14.51)


def test_buy_normal_ok():
    assert check_signal("BUY", "002472", 0, ["002639"], price=33.22, stop_price=30.3) == []


def test_qty_must_be_positive():
    assert "数量缺失或为 0" in check_signal("BUY", "002472", 0, [], qty=0, price=33.22)
    assert check_signal("BUY", "002472", 0, [], qty=100, price=33.22) == []


def test_invalid_price():
    assert check_signal("BUY", "002472", 0, [], price=0)


def test_take_profit_below_current_price():
    issues = check_exit_price("SELL", 7.18, 7.19, "止盈")
    assert issues and "低于现价" in issues[0]
    assert check_exit_price("SELL", 7.18, 7.11, "止盈") == []
    # 止损卖出低于现价是正常的
    assert check_exit_price("SELL", 7.0, 7.19, "止损") == []


def test_buy_stop_must_be_below_price():
    assert check_exit_price("BUY", 10.0, stop_price=10.5)
    assert check_exit_price("BUY", 10.0, stop_price=9.0) == []


def test_risk_breaches():
    # 实际线上数据：仓位 85.2%、集中度 50%
    b = risk_breaches(102381.93, 87252.0, 51192.0, 0.70, 0.40)
    assert len(b) == 2
    assert "85.2%" in b[0] and "50.0%" in b[1]
    assert risk_breaches(100000, 50000, 30000, 0.70, 0.40) == []
    assert risk_breaches(0, 1, 1) == []


def _vs_state():
    """从 app.py 抽出 _vs_state（不执行整个 Streamlit 脚本）。"""
    import ast
    src = open(os.path.join(BASE, "app.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_vs_state")
    ns = {}
    exec(compile(ast.Module([fn], []), "app.py", "exec"), ns)
    return ns["_vs_state"]


def test_vs_state_zero_sample_is_insufficient_not_anomaly():
    f = _vs_state()
    assert f(None, 2.5, True, 0, 20) == "insufficient"
    assert f(None, 0, True, 0, 20) == "insufficient"
    assert f(None, 2.5, True, 25, 20) == "anomaly"
    assert f(3.0, 2.5, True, 25, 20) == "pass"
    assert f(1.0, 2.5, True, 25, 20) == "fail"


def test_order_validate_blocks_buy_when_over_limits(tmp_path):
    import shutil, sqlite3
    from engine import order_validate as ov
    db = tmp_path / "sim.db"
    shutil.copy(os.path.join(BASE, "db", "sim.db"), db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM daily_report WHERE account='sim_a' AND symbol='TOTAL'")
    con.execute("INSERT INTO daily_report(date,account,symbol,nav,cash,position_value,pnl_day,ret_total)"
                " VALUES('2026-10-19','sim_a','TOTAL',100000,15000,85000,0,0)")
    con.execute("DELETE FROM positions WHERE account='sim_a'")
    con.execute("INSERT INTO positions(account,symbol,qty,avg_cost,last_price,market_value)"
                " VALUES('sim_a','002446',7000,7,7.11,50000)")
    con.commit(); con.close()
    from datetime import date
    ok, errs, _ = ov.validate_order("sim_a", "002472", "BUY", 100, 36.06,
                                    date.today().isoformat(), db_path=str(db))
    assert not ok
    assert any("禁止新买入" in e and "总仓位" in e for e in errs)
    assert any("禁止新买入" in e and "集中度" in e for e in errs)
