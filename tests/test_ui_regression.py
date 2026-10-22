#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""8502 页面结构/视觉合同测试。与 tests/test_app_smoke.py 的运行期烟雾测试互补。"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
module = open(os.path.join(ROOT, "engine", "decision_desk.py"), encoding="utf-8").read()
passed, failed = [], []

def check(name, condition):
    (passed if condition else failed).append(name)
    print(f"{'✅' if condition else '❌'} {name}")

try:
    ast.parse(src)
    check("app.py 语法有效", True)
except SyntaxError:
    check("app.py 语法有效", False)

nav_match = re.search(r'NAV_OPTIONS\s*=\s*(\[[^\]]*\])', src)
navs = eval(nav_match.group(1)) if nav_match else []
check("顶部导航有 6 个板块", len(navs) == 6)
check("顶部导航不再占据侧边栏", 'st.radio("主导航", NAV_OPTIONS, horizontal=True' in src and 'st.sidebar.radio("板块"' not in src)
for label in navs:
    check(f"导航有页面分支：{label}", f'tab == "{label}"' in src)
check("行情条只在工作台渲染一次", src.count("render_ticker()") == 2 and src.index('elif tab == "🏠 工作台":') < src.index("render_ticker()", src.index('elif tab == "🏠 工作台":')))
check("决策台连接现有数据库和配置", "render_decision_desk(q, CFG, accts" in src)
check("行情复核显示数据日期", "数据日期：" in module and "quote_state" in module)
check("仓位计算有风险/现金/总仓位三重限制", all(k in module for k in ("risk_qty", "cash_qty", "exposure_qty")))
check("决策台不直接写入数据库", not re.search(r'\b(?:INSERT\s+INTO|UPDATE\s+\w+|DELETE\s+FROM)\b', module, re.I) and "sqlite3.connect" not in module)
check("风险上限读取既有 strategy.yaml", "single_trade_risk_pct" in module and "max_stock_position_pct" in module)
check("红涨绿跌语义统一", ".pos { color: #f6465d; } .neg { color: #2ebd85; }" in src)
check("无旧白色主题色残留", not re.search(r"#fff(?:fff)?\b", src, re.I))
check("没有残留旧涨跌绿码", "#00c853" not in src)
check("decision desk tests exist", os.path.isfile(os.path.join(ROOT, "tests", "test_decision_desk.py")))

print(f"\n通过 {len(passed)}/{len(passed) + len(failed)}")
sys.exit(1 if failed else 0)
