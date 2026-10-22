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


def test_no_password_leak():
    """页面输出不包含敏感信息（token/密码）。"""
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    full_text = " ".join([str(m.value) for m in at.markdown] +
                         [str(t.value) for t in at.text])
    sensitive = ["ghp_", "github_pat_", "password", "token"]
    for s in sensitive:
        assert s.lower() not in full_text.lower(), f"页面泄漏敏感词: {s}"
