#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AppTest 渲染文本转储：逐页输出全部可见文本元素（截图替代证据）。"""
import os
import sys

os.environ["PAPER_SIM_NO_SCREEN_RECORD"] = "1"

from streamlit.testing.v1 import AppTest

BASE = os.path.expanduser("~/workspace/l2-remediation/ui1")
APP = os.path.join(BASE, "app.py")
TABS = ["🏠 工作台", "📝 交易", "💰 账户", "🔍 选股", "🏦 机构影子", "🛡 验证"]


def dump(tab):
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["main_nav"] = tab
    at.run()
    lines = [f"### TAB: {tab}", f"exception: {list(at.exception) if at.exception else 'none'}"]
    for kind in ("header", "subheader", "markdown", "caption", "text",
                 "error", "warning", "success", "info"):
        for el in getattr(at, kind, []):
            v = getattr(el, "value", "")
            if v:
                lines.append(f"[{kind}] {v}")
    for el in at.expander:
        lines.append(f"[expander] label={el.label!r}")
    for el in at.table:
        try:
            lines.append("[table]\n" + el.value.to_string())
        except Exception as e:
            lines.append(f"[table] <unreadable {e}>")
    for el in at.dataframe:
        try:
            lines.append("[dataframe]\n" + el.value.to_string())
        except Exception as e:
            lines.append(f"[dataframe] <unreadable {e}>")
    for el in at.metric:
        lines.append(f"[metric] {el.label} = {el.value}")
    return "\n".join(lines)


if __name__ == "__main__":
    outdir = sys.argv[1]
    os.makedirs(outdir, exist_ok=True)
    tabs = TABS if len(sys.argv) < 3 else [sys.argv[2]]
    for t in tabs:
        name = t.replace(" ", "_") + ".txt"
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
            f.write(dump(t))
        print("wrote", name)
