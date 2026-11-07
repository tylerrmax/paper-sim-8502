#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 定点批专项测试（2026-11-08）：

任务一：六页不再渲染关注子集表（A股/港股/加密 · 关注），「全部行情」
折叠区保留且含关注标的，非工作台页改为与工作台一致的一行 caption。
任务二：06 验证页——拦截证据用真实列名（rule_name/trade_date）渲染出
记录行、不再出现「拦截记录读取失败」；系统检查表含「同步」行（六项）；
历史验收报告统一折叠归档（份数与 reports/public/*.md 一致、原报告
expander 保留）；工作台两处同根因查询（优先级队列风控拦截行、最近
拦截行）恢复渲染。

AppTest 每页运行较慢，六页各跑一次、模块内缓存共享。
"""
import glob
import os
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

BASE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
APP_PATH = os.path.join(BASE, "app.py")
DB_PATH = os.path.join(BASE, "db", "sim.db")
TABS = ["🏠 工作台", "📝 交易", "💰 账户", "🔍 选股", "🏦 机构影子", "🛡 验证"]

_cache = {}


@pytest.fixture(autouse=True)
def _no_screen_record(monkeypatch):
    monkeypatch.setenv("PAPER_SIM_NO_SCREEN_RECORD", "1")


def _run(tab):
    if tab not in _cache:
        at = AppTest.from_file(APP_PATH, default_timeout=120)
        at.session_state["main_nav"] = tab
        at.run()
        assert not at.exception, f"{tab} 加载异常: {at.exception}"
        _cache[tab] = at
    return _cache[tab]


def _all_text(at):
    parts = []
    for kind in ("header", "subheader", "markdown", "caption", "text",
                 "error", "warning", "success", "info"):
        for el in getattr(at, kind, []):
            if getattr(el, "value", ""):
                parts.append(str(el.value))
    return "\n".join(parts)


def _db(sql, args=()):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


# ---------------- 任务一：关注子集表取消 ----------------

@pytest.mark.parametrize("tab", TABS)
def test_no_focus_subset_tables(tab):
    at = _run(tab)
    txt = _all_text(at)
    for title in ("A股 · 关注", "港股 · 关注", "加密 · 关注"):
        assert title not in txt, f"{tab} 仍渲染关注子集表 {title}"
    labels = [e.label for e in at.expander]
    assert any(str(l).startswith("📊 全部行情") for l in labels), \
        f"{tab} 缺少「全部行情」折叠区"


def test_focus_caption_and_universe_kept():
    """非工作台页（交易）显示统一 caption；全部行情含关注标的（持仓）。"""
    held = [r[0] for r in _db("SELECT symbol FROM positions WHERE qty>0")]
    if not held:
        pytest.skip("当前库无持仓，关注集口径检查不适用")
    at = _run("📝 交易")
    txt = _all_text(at)
    assert "关注标的行情已收纳至下方「全部行情」" in txt
    assert any(s in txt for s in held), "全部行情中应含持仓（关注）标的"


# ---------------- 任务二(a)：验证页拦截证据 ----------------

def test_verify_block_evidence_renders():
    rows = _db("SELECT symbol, rule_name FROM risk_block_events"
               " ORDER BY ts DESC LIMIT 8")
    if not rows:
        pytest.skip("当前库无拦截记录")
    at = _run("🛡 验证")
    txt = _all_text(at)
    assert "拦截记录读取失败" not in txt
    assert any(sym in txt for sym, _ in rows), "拦截证据应渲染出记录行（标的）"
    assert any(rule in txt for _, rule in rows), "拦截证据应渲染出规则名"


def test_workbench_block_rows_render():
    """任务二(b) 同根因两处：工作台优先级队列的 env_gate 拦截行与
    「最近拦截」行此前被错误列名静默吞掉，修复后应渲染。"""
    env = _db("SELECT symbol FROM risk_block_events WHERE rule_name='env_gate'"
              " ORDER BY trade_date DESC, ts DESC LIMIT 1")
    latest = _db("SELECT symbol FROM risk_block_events"
                 " ORDER BY trade_date DESC, ts DESC LIMIT 1")
    if not env or not latest:
        pytest.skip("当前库无拦截记录")
    at = _run("🏠 工作台")
    txt = _all_text(at)
    assert "风控拦截" in txt and env[0][0] in txt, "优先级队列应含 env_gate 风控拦截行"
    assert "最近拦截：" in txt and latest[0][0] in txt, "今日执行卡应含最近拦截行"


# ---------------- 任务二(c)：检查表六项含同步行 ----------------

def test_verify_check_table_has_sync_row():
    at = _run("🛡 验证")
    target = None
    for el in at.table:
        try:
            cols = list(el.value.columns)
        except Exception:
            continue
        if "检查项" in cols:
            target = el.value
            break
    assert target is not None, "未找到系统检查表"
    items = list(target["检查项"])
    for name in ("账本指纹", "行情新鲜度", "同步", "选股运行", "机构抓取", "数据健康"):
        assert name in items, f"检查表缺「{name}」行"
    assert items.index("行情新鲜度") < items.index("同步") < items.index("选股运行"), \
        "「同步」行应在行情新鲜度之后、选股运行之前"
    sync_row = target[target["检查项"] == "同步"].iloc[0]
    assert sync_row["证据来源"] == "build_info.json"
    assert sync_row["结论"] in ("通过", "待核验")
    if sync_row["结论"] == "通过":
        assert "·" in str(sync_row["当前值"]), "通过时当前值应为 同步时间 · 短SHA"


# ---------------- 任务二(d)：报告统一折叠归档 ----------------

def test_verify_reports_archived():
    reports = sorted(glob.glob(os.path.join(BASE, "reports", "public", "*.md")),
                     reverse=True)
    at = _run("🛡 验证")
    subheaders = [str(e.value) for e in at.subheader]
    assert "📄 验收报告" not in subheaders, "平铺小标题应已去掉"
    labels = [str(e.label) for e in at.expander]
    if not reports:
        assert "暂无报告" in _all_text(at)
        return
    archive = f"📄 历史验收报告（归档 · {len(reports)} 份）"
    assert archive in labels, f"缺归档 expander：{archive}"
    for rp in reports:
        with open(rp, encoding="utf-8") as f:
            content = f.read()
        first = content.lstrip().splitlines()[0] if content.strip() else ""
        title = (first.lstrip("# ").strip() if first.startswith("#")
                 else os.path.splitext(os.path.basename(rp))[0])
        assert title in labels, f"报告原 expander 应保留：{title}"
        assert content.strip()[:20] in _all_text(at), "报告全文应保留可达"
