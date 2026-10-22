#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI 回归测试（静态检查）：8502 独立交易终端。
只检查 UI 层约定（导航/配色语义/状态徽标/风控阈值来源），不碰交易逻辑。
运行时行为见 tests/test_app_smoke.py，语言一致性见 tests/test_zh_simplified.py。
用法： python3 tests/test_ui_regression.py
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
tv = open(os.path.join(ROOT, "engine", "tv_charts.py"), encoding="utf-8").read()
yaml_txt = open(os.path.join(ROOT, "config", "strategy.yaml"), encoding="utf-8").read()
ast.parse(src)

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail and not cond else ""))


# 1. 导航：六个板块，且每个板块都有对应分支
m = re.search(r'st\.sidebar\.radio\("板块",\s*(\[[^\]]*\])', src)
navs = eval(m.group(1)) if m else []
check("导航含 6 个板块", len(navs) == 6, str(navs))
for n in navs:
    check(f"板块有分支：{n}", (f'tab == "{n}"' in src))

# 2. 配色语义：红涨绿跌集中定义，不再散落两套色码
check("THEME_UP/THEME_DOWN 集中定义", "THEME_UP =" in src and "THEME_DOWN =" in src)
check("无遗留交易所色 #ff4d4f 用于涨跌", "#ff4d4f" not in src.replace('border:1px solid #ff4d4f', '').replace('color:#ff4d4f;">⛔ 阻断', ''))
check("无 #00c853 绿色码", "#00c853" not in src)
check(".pos 为红(涨)/.neg 为绿(跌)", ".pos { color: #f6465d; } .neg { color: #2ebd85; }" in src)
check("成交量柱红涨绿跌", 'rgba(246,70,93,0.5)" if c >= o' in tv)
check("K线红涨绿跌", "upColor: '#f6465d', downColor: '#2ebd85'" in tv)
check("图表日期使用 zh-CN", "locale: 'zh-CN'" in tv)

# 3. 无白色文字/背景混搭
check("无 #fff/#ffffff", not re.search(r"#fff(?:fff)?\b", src, re.I))

# 4. 状态徽标
for cls in ["qs-live", "qs-close", "qs-stale", "qs-unknown", "vs-pass", "vs-fail",
            "vs-insufficient", "vs-anomaly"]:
    check(f"徽标样式：{cls}", cls in src)
check("快照永不标为实时（仅实时接口时间戳新鲜才标实时）", "DB 快照永不标为实时" in src)

# 5. 风控阈值来自配置（不再硬编码）
for key in ["buyback_stop_pct", "buyback_take_pct", "max_concentration_pct"]:
    check(f"strategy.yaml 含 {key}", key in yaml_txt)
    check(f"app.py 读取 {key}", key in src)
check("无硬编码止损 0.95 / 止盈 1.15", "* 0.95" not in src and "* 1.15" not in src)

# 6. 交易流程
check("待建单排除已审批", src.count("ap.date IS NULL") >= 3)
check("审批结果回读校验", "chk > 0" in src)
check("回执表单只出现一次", src.count('st.form("receipt_form') == 1)
check("订单显示数量与金额", "qty_est * px" in src)

# 7. 云端稳健性（曾因缺初始化导致 NameError）
check("CLOUD_DATA_ASOF 先初始化", re.search(r'^CLOUD_DATA_ASOF = ""', src, re.M) is not None)
check("secrets 开关容错", "def _secret_flag" in src)

# 8. 性能与刷新
check("行情条局部刷新 st.fragment", "run_every=30" in src)
check("实时行情缓存", "def _cached_live" in src and "ttl=15" in src)
check("技术指标缓存", "def _tech_tags_cached" in src)
check("统一表格助手 show_table", "def show_table" in src)

print(f"\n通过 {len(passed)}/{len(passed) + len(failed)}")
sys.exit(1 if failed else 0)
