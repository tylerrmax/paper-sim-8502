#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
UI 回归测试：8502 独立交易终端 UI 收敛改版
只测 UI 层（CSS/导航/状态徽标/按钮状态机），不碰交易逻辑。
"""
import ast
import re
import sys

APP = "app.py"
src = open(APP).read()
tree = ast.parse(src)

passed, failed = [], []

def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail and not cond else ""))

# 1. 无白色卡片混搭
check("无 #fff/#ffffff 白色背景", not re.search(r"#fff(?:fff)?\b", src, re.I))
check("无 background:white", "background:white" not in src.lower().replace(" ", ""))

# 2. 六导航入口
for nav in ["🏠 工作台", "📈 行情与信号", "📝 交易执行", "💰 持仓与账本", "🛡 验证与风控", "⚙ 系统与审计"]:
    check(f"导航存在：{nav}", nav in src)
# 旧导航名已消除
for old in ["📈 行情与策略", "📝 人工条件单", "💰 成交与本账", "🛡 风控与审计", '"⚙ 运维"']:
    check(f"旧导航已移除：{old}", old not in src)

# 3. 行情四态
for qs in ["qs-live", "qs-close", "qs-stale", "qs-unknown"]:
    check(f"行情徽标CSS：{qs}", qs in src)
check("quote_state 四态函数", "def quote_state" in src)
for state in ['"live"', '"close"', '"stale"', '"unknown"']:
    check(f"四态返回值：{state}", state in src)
check("快照永不标实时声明", "快照永不标为实时" in src or "永不把快照伪装成实时" in src)

# 4. 验证四态
for vs in ["vs-pass", "vs-fail", "vs-insufficient", "vs-anomaly"]:
    check(f"验证徽标CSS：{vs}", vs in src)
check("vs_badge 函数", "def vs_badge" in src)
check("_vs_state 函数", "def _vs_state" in src)
check("阻断原因优先", "阻断" in src)

# 5. 按钮四态
for bs in ["btn-submitted", "btn-running", "btn-done", "btn-failed"]:
    check(f"按钮状态CSS：{bs}", bs in src)
check("回执四态状态机", "receipt_state" in src)
check("审批四态状态机", "_ap_state" in src)
check("写入后校验", "chk > 0" in src)

# 6. 重复功能消除
check("回执表单只出现一次", src.count('st.form("receipt_form') == 1, f"count={src.count('st.form(\"receipt_form')}")
check("行情与信号页无回执表单", "receipt_form" not in src.split('elif tab == "📈 行情与信号"')[1].split('elif tab == "📝 交易执行"')[0] if 'elif tab == "📈 行情与信号"' in src else True)

# 7. 数字格式化助手
for fn in ["def fmt_money", "def fmt_pct", "def fmt_num"]:
    check(f"格式化函数：{fn}", fn in src)

# 8. 语法有效
check("AST 解析通过", True)

# 9. 交易逻辑未动（抽查关键函数签名）
eng = open("engine/sim.py").read()
check("sim.py Lot 含 buy_fee", "buy_fee" in eng)
check("sim.py sell 扣费", "pnl -= fee" in eng)
pipe = open("engine/pipeline.py").read()
check("pipeline 风控未移除", "single_trade_risk_pct" in pipe)

print(f"\n通过 {len(passed)}/{len(passed)+len(failed)}")
sys.exit(1 if failed else 0)
