#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二层 P2：页面数据新鲜度五态（engine/page_freshness）固定时钟矩阵测试
+ AppTest 集成（当前数据无红条误报 / 注入旧 asof 时红黄条真实渲染）。

日期锚点（2026 年）：10-30 周五、11-02 周一、11-03 周二、11-04 周三、
11-06 周五、11-07 周六。均为 A股交易日（不在 CN_HOLIDAYS_2026 内）。
"""
import os
from datetime import date, datetime

import pytest
from streamlit.testing.v1 import AppTest

from engine import page_freshness as pf

APP_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app.py"))


@pytest.fixture(autouse=True)
def _no_screen_record(monkeypatch):
    """同 test_apptest.py：回归测试不污染 screen_runs 审计表。"""
    monkeypatch.setenv("PAPER_SIM_NO_SCREEN_RECORD", "1")


def _dt(y, m, d, hh=18, mm=0):
    return datetime(y, m, d, hh, mm)


# ---------- 五态矩阵（任务书指定场景） ----------

def test_mon_evening_friday_asof_is_delayed():
    state, detail = pf.classify(date(2026, 10, 30), _dt(2026, 11, 2, 18))
    assert state == pf.DELAYED
    assert detail["expected"] == "2026-11-02"
    assert detail["lag_trading_days"] == 1


def test_tue_evening_friday_asof_is_stale():
    state, detail = pf.classify(date(2026, 10, 30), _dt(2026, 11, 3, 18))
    assert state == pf.STALE
    assert detail["expected"] == "2026-11-03"
    assert detail["lag_trading_days"] == 2


def test_saturday_friday_asof_is_closed():
    state, detail = pf.classify(date(2026, 11, 6), _dt(2026, 11, 7, 10))
    assert state == pf.CLOSED
    assert detail["expected"] == "2026-11-06"
    assert detail["lag_trading_days"] == 0
    assert detail["today_is_trading_day"] is False


def test_wed_morning_tuesday_asof_is_ok():
    state, detail = pf.classify(date(2026, 11, 3), _dt(2026, 11, 4, 10))
    assert state == pf.OK
    assert detail["expected"] == "2026-11-03"


def test_wed_evening_same_day_asof_is_ok():
    state, detail = pf.classify(date(2026, 11, 4), _dt(2026, 11, 4, 18))
    assert state == pf.OK
    assert detail["expected"] == "2026-11-04"
    assert detail["lag_trading_days"] == 0


def test_none_asof_is_unavailable():
    state, detail = pf.classify(None, _dt(2026, 11, 4, 18))
    assert state == pf.UNAVAILABLE
    assert detail["asof"] is None
    assert detail["lag_trading_days"] is None
    assert detail["expected"] == "2026-11-04"


def test_injected_holiday_turns_delayed_into_closed():
    """注入 2026-11-04（周三）为假日：当日非交易日，expected 回退到周二，
    asof=周二 → closed；不注入时同一输入为 delayed（对照，证明注入生效）。"""
    now = _dt(2026, 11, 4, 18)
    asof = date(2026, 11, 3)
    state_inj, detail_inj = pf.classify(asof, now, holidays={"2026-11-04"})
    assert state_inj == pf.CLOSED
    assert detail_inj["expected"] == "2026-11-03"
    assert detail_inj["today_is_trading_day"] is False
    state_ctl, _ = pf.classify(asof, now)
    assert state_ctl == pf.DELAYED


# ---------- 边界与故障注入（单元级） ----------

def test_cutoff_boundary_1730():
    """17:29 期望仍为上一交易日；17:30 起期望为当日。"""
    assert pf.expected_data_date(_dt(2026, 11, 4, 17, 29)) == date(2026, 11, 3)
    assert pf.expected_data_date(_dt(2026, 11, 4, 17, 30)) == date(2026, 11, 4)


def test_incident_replica_five_trading_days_stale():
    """复刻 2026-11-02~06 停滞：周五 18:00 页面仍停在上一周五 → stale，
    落后 5 个交易日（周一至周五）。"""
    state, detail = pf.classify(date(2026, 10, 30), _dt(2026, 11, 6, 18))
    assert state == pf.STALE
    assert detail["lag_trading_days"] == 5


def test_str_and_datetime_asof_accepted():
    state, _ = pf.classify("2026-11-04", _dt(2026, 11, 4, 18))
    assert state == pf.OK
    state, _ = pf.classify(datetime(2026, 11, 4, 15, 0), _dt(2026, 11, 4, 18))
    assert state == pf.OK


def test_custom_is_trading_day_callable():
    """注入自定义日历函数（全周无休）时按其口径判定。"""
    always = lambda d: True  # noqa: E731
    # 周六在自定义日历下是交易日，18:00 期望为当日
    state, detail = pf.classify(date(2026, 11, 7), _dt(2026, 11, 7, 18),
                                is_trading_day=always)
    assert state == pf.OK
    assert detail["expected"] == "2026-11-07"


# ---------- AppTest 集成 ----------

_FRESHNESS_WORDS = ("数据已过期", "数据延迟", "数据不可用")


def _banner_texts(at):
    errs = [str(e.value) for e in at.error]
    warns = [str(w.value) for w in at.warning]
    return errs, warns


def _db_asof():
    """工作区 DB 的页面数据日期（与 app.py 页眉同一 SQL）。"""
    import sqlite3
    db = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "db", "sim.db"))
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT MAX(date) FROM daily_report WHERE symbol='TOTAL'").fetchone()
    con.close()
    return date.fromisoformat(row[0]) if row and row[0] else None


def test_apptest_banners_match_classification():
    """集成契约（不打桩）：应用渲染的新鲜度横幅必须与纯函数对同一
    asof/同一时刻的判定一致——ok/closed 无横幅，delayed 黄条，
    stale/unavailable 红条。工作区 DB 为已提交快照（asof=2026-10-30），
    在 2026-11-07 凌晨实时运行下判定为 stale 并真实渲染红条，
    即 11-02~06 停滞事故在页面层的等价复现。"""
    from datetime import timedelta, timezone
    now_cst = datetime.now(timezone(timedelta(hours=8)))
    state, _ = pf.classify(_db_asof(), now_cst)
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    assert not at.exception, f"应用加载异常: {at.exception}"
    errs, warns = _banner_texts(at)
    has_err = any(any(w in t for w in ("数据已过期", "数据不可用"))
                  for t in errs)
    has_warn = any("数据延迟" in t for t in warns)
    if state in (pf.OK, pf.CLOSED):
        assert not has_err and not has_warn, (errs, warns)
    elif state == pf.DELAYED:
        assert has_warn and not has_err, (errs, warns)
    else:  # STALE / UNAVAILABLE
        assert has_err, (errs, warns)


def test_apptest_ok_state_no_banner(monkeypatch):
    """无误报：判定为 ok（数据新鲜）时页面不得出现新鲜度红/黄条。"""
    def _fake_ok(data_asof, now, **kw):
        return pf.OK, {"asof": "2026-11-06", "expected": "2026-11-06",
                       "lag_trading_days": 0, "today_is_trading_day": True}

    monkeypatch.setattr("engine.page_freshness.classify", _fake_ok)
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    assert not at.exception, f"应用加载异常: {at.exception}"
    errs, warns = _banner_texts(at)
    assert not any(any(w in t for w in _FRESHNESS_WORDS) for t in errs), errs
    assert not any(any(w in t for w in _FRESHNESS_WORDS) for t in warns), warns


def test_apptest_stale_banner_rendered(monkeypatch):
    """故障注入：把判定替换为 stale（模拟停滞旧数据），页面必须渲染红条。"""
    def _fake_stale(data_asof, now, **kw):
        return pf.STALE, {"asof": "2026-10-30", "expected": "2026-11-06",
                          "lag_trading_days": 5, "today_is_trading_day": True}

    monkeypatch.setattr("engine.page_freshness.classify", _fake_stale)
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    assert not at.exception, f"应用加载异常: {at.exception}"
    errs, _ = _banner_texts(at)
    assert any("数据已过期" in t and "2026-10-30" in t for t in errs), errs


def test_apptest_delayed_banner_rendered(monkeypatch):
    """故障注入：判定为 delayed 时页面必须渲染黄条。"""
    def _fake_delayed(data_asof, now, **kw):
        return pf.DELAYED, {"asof": "2026-11-05", "expected": "2026-11-06",
                            "lag_trading_days": 1, "today_is_trading_day": True}

    monkeypatch.setattr("engine.page_freshness.classify", _fake_delayed)
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    assert not at.exception, f"应用加载异常: {at.exception}"
    _, warns = _banner_texts(at)
    assert any("数据延迟" in t and "2026-11-05" in t for t in warns), warns
