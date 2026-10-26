#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Streamlit AppTest 回归测试（2026-10-17 用户要求纳入持续回归）。
通过 pytest 自动运行应用代码、模拟用户输入、检查界面输出，
替代每次升级的人工点页面验证。"""
import os

import pytest
from streamlit.testing.v1 import AppTest

# 以测试文件位置定位 app.py，不依赖运行时工作目录
APP_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app.py"))


@pytest.fixture(autouse=True)
def _no_screen_record(monkeypatch):
    """回归测试不污染 screen_runs 审计表（P0-3 落库只记真实运行）。
    用 monkeypatch 作用域设置，避免进程级环境变量污染同跑的其他测试。"""
    monkeypatch.setenv("PAPER_SIM_NO_SCREEN_RECORD", "1")


def test_app_loads():
    """应用能正常加载，无异常。"""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    assert not at.exception, f"应用加载异常: {at.exception}"


def test_nav_options_exist():
    """导航存在（五板块单选，含工作台）。"""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    assert not at.exception, f"应用加载异常: {at.exception}"
    # 检查 radio 的选项中是否存在主导航
    nav_found = False
    for widget in at.radio:
        opts = [str(o) for o in widget.options]
        if any("工作台" in o for o in opts):
            nav_found = True
            break
    assert nav_found, "未找到导航选项（应有含“工作台”的 radio）"


def test_screen_page_auto_only():
    """模块D：选股页全自动——无手动筛选决策控件（无条件筛选面板的
    输入框/下拉/筛选按钮），且自动结果区（多重校验）存在。"""
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    assert not at.exception
    for widget in at.radio:
        if any("选股" in str(o) for o in widget.options):
            widget.set_value([o for o in widget.options
                              if "选股" in str(o)][0])
            break
    at.run()
    assert not at.exception, f"选股页异常: {at.exception}"
    labels = ([str(w.label) for w in at.number_input] +
              [str(w.label) for w in at.selectbox] +
              [str(b.label) for b in at.button])
    bad = [x for x in labels if "筛选" in x or x.startswith("涨幅")
           or x.startswith("RSI") or x == "MACD"]
    assert not bad, "选股页仍有手动筛选控件：%r" % bad
    full_text = " ".join([str(m.value) for m in at.markdown] +
                         [str(s.value) for s in at.subheader] +
                         [str(h.value) for h in at.header])
    assert "条件筛选" not in full_text, "选股页仍有手动条件筛选面板残留"
    assert "多重校验" in full_text, "选股页自动筛选结果区缺失"
    assert "screen-v1.6" in " ".join(str(c.value) for c in at.caption), \
        "选股页未标注 RULE_VERSION/asof（模块D页面要求）"


def test_screen_topboard_present():
    """TOP标的榜：选股页顶部有 TOP 榜区块（数据来自最近一期
    screen_runs 落库；验证列样本不足时须如实标注积累中）。"""
    at = AppTest.from_file(APP_PATH, default_timeout=60)
    at.run()
    assert not at.exception
    for widget in at.radio:
        if any("选股" in str(o) for o in widget.options):
            widget.set_value([o for o in widget.options
                              if "选股" in str(o)][0])
            break
    at.run()
    assert not at.exception, f"选股页异常: {at.exception}"
    full_text = " ".join([str(m.value) for m in at.markdown] +
                         [str(s.value) for s in at.subheader] +
                         [str(h.value) for h in at.header] +
                         [str(c.value) for c in at.caption])
    assert "TOP标的榜" in full_text, "选股页 TOP 标的榜区块缺失"
    # 有落库评分时榜单必出表或积累中标注（screen_perf 尚无成熟样本）
    tbl_text = " ".join(str(c) for t in at.table for c in t.value) \
        if hasattr(at, "table") else ""
    assert ("积累中" in (full_text + tbl_text)) or at.table, \
        "TOP 榜既无表格也无积累中标注"


def test_no_password_leak():
    """页面输出不包含敏感信息（token/密码）。"""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    full_text = " ".join([str(m.value) for m in at.markdown] +
                         [str(t.value) for t in at.text])
    sensitive = ["ghp_", "github_pat_", "password", "token"]
    for s in sensitive:
        assert s.lower() not in full_text.lower(), f"页面泄漏敏感词: {s}"
