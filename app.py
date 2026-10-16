#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立端对端交易验证终端（8502）
定位：唯一最终系统，跑过验证终端（paper-sim-web）后将其下线，只留最强。
五大板块：总览 / 交易 / 验证 / 风控 / 系统
绝对独立：数据独立 + 计算独立 + 决策独立 + 验证独立 + 回执独立
唯一外部依赖：行情源 + 人工券商下单
运行：~/workspace/paper-trading-sim/.venv/bin/streamlit run app.py --server.port 8502
"""
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

import streamlit as st
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
DB = os.path.join(BASE, "db", "sim.db")
CFG_PATH = os.path.join(BASE, "config", "strategy.yaml")

# 云端模式检测：Streamlit Cloud 文件系统是临时的，写操作不持久
# 通过环境变量 STREAMLIT_CLOUD 判断
IS_CLOUD = os.environ.get("STREAMLIT_CLOUD") == "1" or "streamlit" in os.environ.get("HOSTNAME", "")
if IS_CLOUD:
    # 云端用只读模式：复制 DB 到临时位置避免锁定问题
    import tempfile, shutil
    _tmp = os.path.join(tempfile.gettempdir(), "sim_cloud.db")
    try:
        shutil.copy2(DB, _tmp)
        DB = _tmp
    except Exception:
        pass


def load_cfg():
    with open(CFG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


CFG = load_cfg()

# 策略名中文映射
STRATEGY_CN = {
    "buyback": "接回策略",
    "ma20": "MA20趋势",
}


def strategy_of(aid):
    """分账户策略（2026-10-16 用户选A），回退全局。"""
    sig = CFG.get("signals", {})
    return sig.get("per_account", {}).get(aid) or sig.get("strategy", "ma20")


def strategy_desc(s):
    if s == "buyback":
        return "20日高点回撤10%买入，+15%止盈 / -5%止损"
    return "收盘站上20日线买入，跌破20日线卖出"


def q(sql, args=()):
    con = sqlite3.connect(DB)
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


def get_control():
    r = q("SELECT status, mode FROM control WHERE id=1")
    return r[0] if r else ("running", "auto")


def set_control(status=None, mode=None):
    con = sqlite3.connect(DB)
    cur = get_control()
    ns = status or cur[0]
    nm = mode or cur[1]
    con.execute(
        "INSERT OR REPLACE INTO control(id,status,mode,updated) VALUES(1,?,?,?)",
        (ns, nm, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    con.commit()
    con.close()


# ============ 页面配置 ============
st.set_page_config(page_title="独立交易终端", layout="wide", page_icon="📊")

# 深色交易终端风格（对标专业终端）
st.markdown("""
<style>
.main-title { font-size: 28px; font-weight: 800; margin-bottom: 4px; letter-spacing: 1px; }
.sub-title { font-size: 13px; color: #8a8f9c; margin-bottom: 16px; }
.kpi-card { background: #10141b; border: 1px solid #1e2532; border-radius: 10px;
            padding: 14px 16px; margin-bottom: 10px; }
.kpi-label { font-size: 12px; color: #8a8f9c; margin-bottom: 4px; }
.kpi-value { font-size: 22px; font-weight: 700; font-family: ui-monospace, monospace; }
.kpi-value.up { color: #2ebd85; } .kpi-value.down { color: #f6465d; }
.kpi-value.big { font-size: 28px; }
.strategy-badge { display: inline-block; padding: 3px 12px; border-radius: 12px;
                  font-size: 12px; font-weight: 600; }
.strategy-badge.buyback { background: rgba(46,189,133,.15); color: #2ebd85; }
.strategy-badge.ma20 { background: rgba(240,185,11,.15); color: #f0b90b; }
.section-head { font-size: 18px; font-weight: 700; margin: 20px 0 12px 0;
                padding-bottom: 8px; border-bottom: 2px solid #1e2532; }
.alert-banner { border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; font-size: 14px; }
.alert-banner.warn { background: rgba(240,185,11,.12); border: 1px solid rgba(240,185,11,.3);
                     color: #f0b90b; }
.alert-banner.info { background: rgba(24,144,255,.1); border: 1px solid rgba(24,144,255,.3);
                     color: #1890ff; }
.alert-banner.success { background: rgba(46,189,133,.1); border: 1px solid rgba(46,189,133,.3);
                        color: #2ebd85; }
.status-card { background: #ffffff; border-radius: 10px; padding: 16px;
               margin-bottom: 12px; color: #333; }
.status-card .sc-label { font-size: 13px; color: #999; margin-bottom: 8px; }
.status-card .sc-value { font-size: 26px; font-weight: 700; }
.status-card .sc-value.paused { color: #999; }
.status-card .sc-value.nodata { color: #ccc; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">独立交易终端</div>', unsafe_allow_html=True)

# ============ 滚动行情条（全页面顶部） ============
def render_ticker():
    """滚动行情条：所有标的最新价 + 涨跌幅"""
    rows = q("""SELECT m.symbol, m.close,
                  (SELECT close FROM market_data WHERE symbol=m.symbol AND date < m.date
                   ORDER BY date DESC LIMIT 1) as prev
                FROM market_data m
                INNER JOIN (SELECT symbol, MAX(date) as md FROM market_data GROUP BY symbol) t
                ON m.symbol = t.symbol AND m.date = t.md""")
    if not rows:
        return
    items = []
    for sym, close, prev in rows:
        if close and prev:
            chg = (close - prev) / prev * 100
            color = "#2ebd85" if chg >= 0 else "#f6465d"
            arrow = "▲" if chg >= 0 else "▼"
            items.append(
                f'<span style="margin-right:24px; white-space:nowrap;">'
                f'<b>{sym}</b> <span style="font-family:ui-monospace">{close:.2f}</span> '
                f'<span style="color:{color}">{arrow}{abs(chg):.2f}%</span></span>')
        elif close:
            items.append(f'<span style="margin-right:24px; white-space:nowrap;">'
                         f'<b>{sym}</b> {close:.2f}</span>')
    ticker_html = "".join(items)
    st.markdown(f"""
    <div style="overflow:hidden; background:#0d1117; border:1px solid #1e2532;
                border-radius:8px; padding:8px 0; margin-bottom:16px;">
      <div style="display:inline-block; white-space:nowrap; animation: ticker 30s linear infinite;">
        {ticker_html}<span style="margin-right:48px;"></span>{ticker_html}
      </div>
    </div>
    <style>
    @keyframes ticker {{ 0% {{ transform: translateX(0); }} 100% {{ transform: translateX(-50%); }} }}
    </style>
    """, unsafe_allow_html=True)

render_ticker()

# ============ 顶栏：系统状态 + 策略 ============
status, mode = get_control()
ver = CFG.get("meta", {}).get("version", "unknown")

top1, top2, top3, top4 = st.columns(4)
with top1:
    st.markdown(f'<div class="kpi-card"><div class="kpi-label">系统状态</div>'
                f'<div class="kpi-value">{"🟢 运行中" if status == "running" else "⏸ 已暂停"}</div></div>',
                unsafe_allow_html=True)
with top2:
    st.markdown(f'<div class="kpi-card"><div class="kpi-label">执行模式</div>'
                f'<div class="kpi-value">{"⚡ 自动" if mode == "auto" else "✋ 手动"}</div></div>',
                unsafe_allow_html=True)
with top3:
    st.markdown(f'<div class="kpi-card"><div class="kpi-label">规则版本</div>'
                f'<div class="kpi-value" style="font-size:16px">{ver}</div></div>',
                unsafe_allow_html=True)
with top4:
    # 各账户策略徽标
    badges = " ".join(
        f'<span class="strategy-badge {strategy_of(a["id"])}">{a["id"]}:{STRATEGY_CN.get(strategy_of(a["id"]), strategy_of(a["id"]))}</span>'
        for a in CFG["accounts"])
    st.markdown(f'<div class="kpi-card"><div class="kpi-label">分账户策略（2026-10-16 选A）</div>'
                f'<div style="margin-top:6px">{badges}</div></div>',
                unsafe_allow_html=True)

# ============ 导航（对标人工交易终端工作流） ============
tab = st.sidebar.radio("板块", ["🏠 工作台", "📈 行情与策略", "📝 人工条件单", "💰 成交与本账", "🛡 风控与审计", "⚙ 运维"])
st.sidebar.markdown("---")

# 侧边栏：控制
st.sidebar.header("控制")
st.sidebar.write("状态：%s　模式：%s" % ("运行中" if status == "running" else "已暂停",
                                        "自动" if mode == "auto" else "手动"))
c1, c2 = st.sidebar.columns(2)
if c1.button("暂停" if status == "running" else "开始"):
    from engine.pipeline import run, write_report
    set_control(status="paused" if status == "running" else "running")
    cfg2 = load_cfg()
    asof = run(DB, cfg2)
    write_report(DB, cfg2, asof)
    st.rerun()
if c2.button("切手动" if mode == "auto" else "切自动"):
    from engine.pipeline import run, write_report
    set_control(mode="manual" if mode == "auto" else "auto")
    cfg2 = load_cfg()
    asof = run(DB, cfg2)
    write_report(DB, cfg2, asof)
    st.rerun()
if st.sidebar.button("🔄 刷新数据（拉取+重算）"):
    with st.spinner("拉取行情并重算…"):
        r = subprocess.run([sys.executable, os.path.join(BASE, "run_daily.py")],
                           capture_output=True, text=True, timeout=300)
    if r.returncode == 0:
        st.sidebar.success("刷新完成")
    else:
        st.sidebar.error(r.stderr[-500:] if r.stderr else "失败")
    st.rerun()

accts = ([r[0] for r in q("SELECT DISTINCT account FROM daily_report ORDER BY account")]
         or ["sim_a", "sim_hk", "sim_crypto"])
ACCT_CN = {"sim_a": "A股", "sim_hk": "港股", "sim_crypto": "加密"}
acct = st.sidebar.selectbox("账户", accts, format_func=lambda x: f"{x}（{ACCT_CN.get(x, x)}）")

# ================================================================
# 🏠 工作台：系统状态 + 今日信号 + 账户总览
# ================================================================
if tab == "🏠 工作台":
    st.markdown('<div class="section-head">🏠 工作台</div>', unsafe_allow_html=True)

    # 系统状态横幅（暂停时警告）
    if status != "running":
        st.markdown('<div class="alert-banner warn">'
                    '⏸ 系统暂停：新建系统默认暂停。已挂在券商的条件单需在券商端自行检查/撤销。'
                    '</div>', unsafe_allow_html=True)

    # 行情新鲜度检查
    latest_a = q("SELECT MAX(date) FROM market_data WHERE symbol IN ('002446','688305')")
    latest_hk = q("SELECT MAX(date) FROM market_data WHERE symbol LIKE 'HK%'")
    today_str = datetime.now().strftime("%Y-%m-%d")
    stale = []
    if latest_a and latest_a[0][0]:
        # 简单判断：最新日期是否早于今天（考虑周末）
        if latest_a[0][0] < today_str:
            stale.append(f"A股({latest_a[0][0]})")
    if latest_hk and latest_hk[0][0]:
        if latest_hk[0][0] < today_str:
            stale.append(f"港股({latest_hk[0][0]})")
    if stale:
        st.markdown(f'<div class="alert-banner warn">'
                    f'⚠ 行情新鲜度检查未通过：{", ".join(stale)}。数据不是最新，信号可能滞后。'
                    f'</div>', unsafe_allow_html=True)

    # 状态卡片行（白卡片，对标截图）
    sc1, sc2, sc3 = st.columns(3)
    with sc1:
        sc_val = "已暂停" if status != "running" else "运行中"
        sc_cls = "paused" if status != "running" else ""
        st.markdown(f'<div class="status-card"><div class="sc-label">交易控制</div>'
                    f'<div class="sc-value {sc_cls}">{sc_val}</div></div>',
                    unsafe_allow_html=True)
    with sc2:
        a_info = latest_a[0][0] if latest_a and latest_a[0][0] else "未知·无数据"
        st.markdown(f'<div class="status-card"><div class="sc-label">A股行情</div>'
                    f'<div class="sc-value nodata" style="font-size:20px">{a_info}</div></div>',
                    unsafe_allow_html=True)
    with sc3:
        hk_info = latest_hk[0][0] if latest_hk and latest_hk[0][0] else "未知·无数据"
        st.markdown(f'<div class="status-card"><div class="sc-label">港股行情</div>'
                    f'<div class="sc-value nodata" style="font-size:20px">{hk_info}</div></div>',
                    unsafe_allow_html=True)

    # 今日信号
    st.subheader("今日信号")
    today = datetime.now().strftime("%Y-%m-%d")
    sig_today = q("""SELECT symbol, side, price FROM signals
                     WHERE account=? AND date=? ORDER BY symbol""", (acct, today))
    if sig_today:
        for s, side, px in sig_today:
            color = "🟢" if side == "BUY" else "🔴"
            st.write(f"{color} **{s}** {side} @ {px}")
    else:
        st.write("今日暂无信号")

    # 账户总览
    st.subheader("账户总览")
    row = q("SELECT date,nav,cash,position_value,pnl_day,ret_total FROM daily_report"
            " WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (acct,))
    if row:
        asof, nav, cash, pv, pnl_day, ret = row[0]
        ccy = {"sim_a": "CNY", "sim_hk": "HKD", "sim_crypto": "USDT"}.get(acct, "")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(f"总资产（{ccy}）", f"{nav:,.2f}", f"{ret*100:+.2f}%累计")
        m2.metric("现金", f"{cash:,.2f}")
        m3.metric("持仓市值", f"{pv:,.2f}")
        m4.metric("当日盈亏", f"{pnl_day:+,.2f}")
        st.caption(f"数据截至 {asof} · 策略：{STRATEGY_CN.get(strategy_of(acct), strategy_of(acct))}（{strategy_desc(strategy_of(acct))}）")
    else:
        st.info("该账户暂无数据")

    # 净值曲线
    hist = q("SELECT date,nav FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date", (acct,))
    if hist:
        st.subheader("净值曲线")
        st.line_chart({d: n for d, n in hist})

    # 三账户并排（对标验证终端）
    st.subheader("三账户并排")
    cols = st.columns(3)
    for i, aid in enumerate(["sim_a", "sim_hk", "sim_crypto"]):
        r = q("SELECT nav,pnl_day,ret_total FROM daily_report"
              " WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (aid,))
        with cols[i]:
            s = strategy_of(aid)
            if r:
                nav_, pnl_, ret_ = r[0]
                ccy = {"sim_a": "CNY", "sim_hk": "HKD", "sim_crypto": "USDT"}.get(aid, "")
                st.markdown(
                    f'<div class="kpi-card">'
                    f'<div class="kpi-label">{aid}（{ACCT_CN.get(aid, aid)}）'
                    f' <span class="strategy-badge {s}">{STRATEGY_CN.get(s, s)}</span></div>'
                    f'<div class="kpi-value">{nav_:,.0f} <span style="font-size:12px">{ccy}</span></div>'
                    f'<div class="kpi-label">当日 <span style="color:{"#2ebd85" if pnl_ >= 0 else "#f6465d"}">'
                    f'{pnl_:+,.0f}</span> · 累计 {ret_*100:+.2f}%</div></div>',
                    unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="kpi-card"><div class="kpi-label">{aid}</div>'
                            f'<div class="kpi-value">—</div></div>', unsafe_allow_html=True)

    # 待审批面板（手动模式）
    if mode == "manual" and not IS_CLOUD:
        st.subheader("⏳ 待审批信号")
        pend = q("""SELECT s.date, s.account, s.symbol, s.side, s.price FROM signals s
                    LEFT JOIN approvals a ON s.date=a.date AND s.account=a.account
                      AND s.symbol=a.symbol AND s.side=a.side
                    LEFT JOIN fills f ON s.date=f.date AND s.account=f.account
                      AND s.symbol=f.symbol AND s.side=f.side
                    WHERE a.date IS NULL AND f.id IS NULL
                      AND s.date >= date('now','-7 days')
                    ORDER BY s.date DESC LIMIT 20""")
        if pend:
            for d, a, s, sd, px in pend:
                c1, c2, c3 = st.columns([3, 1, 1])
                with c1:
                    st.write(f"**{s}** {a} {sd} @ {px}（{d}）")
                with c2:
                    if st.button("✅ 批准", key=f"ap_{d}_{a}_{s}_{sd}"):
                        con = sqlite3.connect(DB)
                        con.execute(
                            "INSERT INTO approvals(date,account,symbol,side,decision,updated)"
                            " VALUES(?,?,?,?,?,?)",
                            (d, a, s, sd, "approved",
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        con.commit()
                        con.close()
                        st.rerun()
                with c3:
                    if st.button("⏭ 跳过", key=f"sk_{d}_{a}_{s}_{sd}"):
                        con = sqlite3.connect(DB)
                        con.execute(
                            "INSERT INTO approvals(date,account,symbol,side,decision,updated)"
                            " VALUES(?,?,?,?,?,?)",
                            (d, a, s, sd, "skipped",
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        con.commit()
                        con.close()
                        st.rerun()
        else:
            st.write("暂无待审批信号")

    # 事件流（最近动态）
    st.subheader("📡 事件流")
    events = []
    for d, a, s, sd, qty, px in q(
            "SELECT date,account,symbol,side,qty,price FROM fills ORDER BY date DESC LIMIT 5"):
        events.append((d, f"💰 {a} {s} {sd} {qty}股 @ {px}"))
    for d, a, s, sd in q(
            "SELECT date,account,symbol,side FROM signals ORDER BY date DESC LIMIT 5"):
        events.append((d, f"📶 {a} {s} {sd} 信号"))
    for d, a, s, sd, dc in q(
            "SELECT date,account,symbol,side,decision FROM approvals ORDER BY updated DESC LIMIT 5"):
        icon = "✅" if dc == "approved" else "⏭"
        events.append((d, f"{icon} {a} {s} {sd} {dc}"))
    events.sort(reverse=True)
    for d, txt in events[:10]:
        st.write(f"`{d}` {txt}")
    if not events:
        st.write("暂无事件")

# ================================================================
# 📈 行情与策略：实时行情 + 策略规则 + 触发线
# ================================================================
elif tab == "📈 行情与策略":
    st.markdown('<div class="section-head">📈 行情与策略</div>', unsafe_allow_html=True)

    # 策略规则展示
    st.subheader("策略规则")
    for a in CFG["accounts"]:
        aid = a["id"]
        s = strategy_of(aid)
        st.markdown(f'**{aid}（{ACCT_CN.get(aid, aid)}）** '
                    f'<span class="strategy-badge {s}">{STRATEGY_CN.get(s, s)}</span> — {strategy_desc(s)}',
                    unsafe_allow_html=True)
    st.caption(f"规则版本：{ver} · {CFG.get('meta', {}).get('note', '')}")

    # 行情表（最新价）
    st.subheader("最新行情")
    quotes = q("""SELECT m.symbol, m.date, m.close FROM market_data m
                  INNER JOIN (SELECT symbol, MAX(date) as md FROM market_data GROUP BY symbol) t
                  ON m.symbol = t.symbol AND m.date = t.md
                  ORDER BY m.symbol""")
    if quotes:
        st.table([{"代码": s, "日期": d, "收盘": f"{c:.2f}" if c else "—"}
                  for s, d, c in quotes])
    else:
        st.write("暂无行情数据")

    # 回执录入
    st.subheader("🧾 券商回执录入")
    if IS_CLOUD:
        st.info("☁️ 云端版为只读展示，回执录入请使用本地版本。")
    else:
        st.caption("在券商成交后，在此录入实际成交，回执独立对账")
    if not IS_CLOUD:
        with st.form("receipt_form"):
            r_sym = st.text_input("代码（如 002446）")
            r_side = st.selectbox("方向", ["买入", "卖出"])
            r_price = st.number_input("成交价", min_value=0.0, format="%.4f")
            r_qty = st.number_input("数量（股）", min_value=0, step=100)
            r_date = st.date_input("成交日期", value=datetime.now().date())
            if st.form_submit_button("录入回执"):
                if r_sym and r_price > 0 and r_qty > 0:
                    con = sqlite3.connect(DB)
                    con.execute("""INSERT OR REPLACE INTO broker_receipts
                                   (date, account, symbol, side, price, qty, created_at)
                                   VALUES(?,?,?,?,?,?,?)""",
                                (r_date.strftime("%Y-%m-%d"), acct, r_sym,
                                 "BUY" if r_side == "买入" else "SELL",
                                 r_price, r_qty,
                                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    con.commit()
                    con.close()
                    st.success(f"已录入：{r_sym} {r_side} {r_qty}股 @ {r_price}")
                else:
                    st.error("请填写完整的代码、价格和数量")

    # 持仓
    st.subheader("持仓")
    pos = q("SELECT symbol,qty,avg_cost,last_price,market_value FROM positions WHERE account=? AND qty>0", (acct,))
    if pos:
        st.table([{"标的": s, "数量": qty, "均价": f"{avg:.2f}", "现价": f"{last:.2f}", "市值": f"{mv:,.2f}"}
                  for s, qty, avg, last, mv in pos])
    else:
        st.write("空仓")

# ================================================================
# 📝 人工条件单：待建条件单提醒 + 券商回执录入
# ================================================================
elif tab == "📝 人工条件单":
    st.markdown('<div class="section-head">📝 人工条件单</div>', unsafe_allow_html=True)
    st.markdown('<div class="alert-banner info">'
                'Web不连接券商、不发送订单。看完下面的信号后，前往券商 App 手动建立条件单，'
                '成交后再回到这里录入回执。'
                '</div>', unsafe_allow_html=True)

    # 待建条件单提醒（数据提醒，用户看完自行去券商手动下单）
    st.subheader("待建条件单")
    sigs = q("""SELECT s.date, s.symbol, s.side, s.price FROM signals s
                LEFT JOIN fills f ON s.date=f.date AND s.account=f.account
                  AND s.symbol=f.symbol AND s.side=f.side
                WHERE s.account=? AND f.id IS NULL
                  AND s.date >= date('now','-7 days')
                ORDER BY s.date DESC, s.symbol LIMIT 10""", (acct,))
    if sigs:
        st.table([{"信号日": d, "代码": s, "方向": "买入" if sd == "BUY" else "卖出",
                   "触发价": px}
                  for d, s, sd, px in sigs])
    else:
        st.write("近7天无待执行信号")

    # 券商回执录入
    st.subheader("🧾 券商回执录入")
    st.caption("在券商成交后，在此录入实际成交，回执独立对账")
    if not IS_CLOUD:
        with st.form("receipt_form2"):
            r_sym = st.text_input("代码（如 002446）", key="r2_sym")
            r_side = st.selectbox("方向", ["买入", "卖出"], key="r2_side")
            r_price = st.number_input("成交价", min_value=0.0, format="%.4f", key="r2_price")
            r_qty = st.number_input("数量（股）", min_value=0, step=100, key="r2_qty")
            r_date = st.date_input("成交日期", value=datetime.now().date(), key="r2_date")
            if st.form_submit_button("录入回执"):
                if r_sym and r_price > 0 and r_qty > 0:
                    con = sqlite3.connect(DB)
                    con.execute("""INSERT OR REPLACE INTO broker_receipts
                                   (date, account, symbol, side, price, qty, created_at)
                                   VALUES(?,?,?,?,?,?,?)""",
                                (r_date.strftime("%Y-%m-%d"), acct, r_sym,
                                 "BUY" if r_side == "买入" else "SELL",
                                 r_price, r_qty,
                                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                    con.commit()
                    con.close()
                    st.success(f"已录入：{r_sym} {r_side} {r_qty}股 @ {r_price}")
                else:
                    st.error("请填写完整的代码、价格和数量")

    # 已录入回执
    st.subheader("已录入回执")
    receipts = q("""SELECT date, symbol, side, price, qty FROM broker_receipts
                    WHERE account=? ORDER BY date DESC LIMIT 20""", (acct,))
    if receipts:
        st.table([{"日期": d, "代码": s, "方向": "买入" if sd == "BUY" else "卖出",
                   "成交价": px, "数量": qty}
                  for d, s, sd, px, qty in receipts])
    else:
        st.write("暂无回执记录")

# ================================================================
# 💰 成交与本账：持仓 + 成交 + 净值
# ================================================================
elif tab == "💰 成交与本账":
    st.markdown('<div class="section-head">💰 成交与本账</div>', unsafe_allow_html=True)

    # 持仓
    st.subheader("当前持仓")
    pos = q("SELECT symbol,qty,avg_cost,last_price,market_value FROM positions WHERE account=? AND qty>0", (acct,))
    if pos:
        # 持仓浮盈亏明细
        rows = []
        for s, qty, avg, last, mv in pos:
            upl = (last - avg) * qty if last and avg else 0
            upl_pct = (last - avg) / avg * 100 if last and avg else 0
            rows.append({"标的": s, "数量": qty, "均价": f"{avg:.2f}", "现价": f"{last:.2f}",
                         "市值": f"{mv:,.2f}",
                         "浮盈亏": f"{upl:+,.0f} ({upl_pct:+.1f}%)"})
        st.table(rows)

        # TradingView 级 K线（每个持仓标的）
        st.subheader("持仓K线（TradingView 级）")
        from engine.tv_charts import kline_html
        # 取该账户的买卖信号用于标记
        sig_marks = q("SELECT date,side FROM signals WHERE account=? AND symbol=? "
                      "ORDER BY date", (acct, "__none__"))  # 占位，下面按标的重查
        for s, qty, avg, last, mv in pos:
            bars = q("SELECT date,open,high,low,close,volume FROM market_data"
                     " WHERE symbol=? ORDER BY date DESC LIMIT 120", (s,))
            if len(bars) >= 20:
                bars = bars[::-1]
                # 关键位线：成本 / 止损 / 止盈
                pls = [{"price": round(avg, 4), "title": f"成本 {avg:.2f}",
                        "color": "#1890ff"}]
                strat = strategy_of(acct)
                if strat == "buyback":
                    pls.append({"price": round(avg * 0.95, 4),
                                "title": f"止损 {avg*0.95:.2f} (-5%)",
                                "color": "#f6465d"})
                    pls.append({"price": round(avg * 1.15, 4),
                                "title": f"止盈 {avg*1.15:.2f} (+15%)",
                                "color": "#2ebd85"})
                # 信号标记
                sm = q("SELECT date,side FROM signals WHERE account=? AND symbol=? "
                       "ORDER BY date DESC LIMIT 20", (acct, s))
                marks = []
                for d, sd in sm:
                    if sd == "BUY":
                        marks.append({"time": d, "position": "belowBar",
                                      "shape": "arrowUp", "text": "买",
                                      "color": "#2ebd85"})
                    else:
                        marks.append({"time": d, "position": "aboveBar",
                                      "shape": "arrowDown", "text": "卖",
                                      "color": "#f6465d"})
                upl = (last - avg) * qty if last and avg else 0
                upl_pct = (last - avg) / avg * 100 if last and avg else 0
                st.write(f"**{s}** 成本 {avg:.2f} · 现价 {last:.2f} · "
                         f"浮盈亏 {upl:+,.0f} ({upl_pct:+.1f}%)")
                st.components.v1.html(
                    kline_html(f"{acct}_{s}", bars, price_lines=pls,
                               markers=marks, height=420),
                    height=430)
            else:
                st.write(f"**{s}**：数据不足（{len(bars)}根）")
    else:
        st.write("空仓")

    # 近期成交
    st.subheader("近期成交")
    fl = q("SELECT date,symbol,side,qty,price,fee,pnl FROM fills WHERE account=? ORDER BY date DESC LIMIT 20", (acct,))
    if fl:
        st.table([{"日期": d, "标的": s, "方向": sd, "数量": qty, "成交价": px, "费用": fee, "盈亏": pnl}
                  for d, s, sd, qty, px, fee, pnl in fl])
    else:
        st.write("暂无成交")

# ================================================================
# 🛡 风控与审计：KPI 验证 + 风控指标 + 审计
# ================================================================
elif tab == "🛡 风控与审计":
    st.markdown('<div class="section-head">🛡 风控与审计</div>', unsafe_allow_html=True)

    # ---- 验证 KPI 卡片 ----
    st.subheader("验证 KPI")
    st.caption("毕业标准：期望值>0 · 盈亏比≥3 · 最大回撤≤10% · 样本20-30笔 · 执行分≥90 · 零数据事故 · 股票与加密分开算")

    # 从 graduation 数据取 KPI（sync_snapshot.build_graduation 口径）
    sys.path.insert(0, BASE)
    try:
        from sync_snapshot import build_graduation
        con = sqlite3.connect(DB)
        g = build_graduation(con)
        con.close()
        d = g["accounts"].get(acct, {})
        tg = g["targets"]

        k1, k2, k3 = st.columns(3)
        with k1:
            rt = d.get("round_trips") or 0
            ok = tg["sample_min"] <= rt <= tg["sample_max"]
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">样本（round-trip）{"✅" if ok else ""}</div>'
                        f'<div class="kpi-value">{"🟢" if ok else ""} {rt} 笔</div>'
                        f'<div class="kpi-label">目标 {tg["sample_min"]}-{tg["sample_max"]} 笔</div></div>',
                        unsafe_allow_html=True)
        with k2:
            wlr = d.get("win_loss_ratio")
            ok = wlr is not None and wlr >= tg["win_loss_ratio"]
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">盈亏比（平均盈利/平均亏损）{"✅" if ok else ""}</div>'
                        f'<div class="kpi-value {"up" if ok else ""}">{wlr if wlr is not None else "—"}</div>'
                        f'<div class="kpi-label">硬线 ≥ {tg["win_loss_ratio"]}</div></div>',
                        unsafe_allow_html=True)
        with k3:
            pf = d.get("profit_factor")
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">盈利因子 PF（参考）</div>'
                        f'<div class="kpi-value">{pf if pf is not None else "—"}</div></div>',
                        unsafe_allow_html=True)

        k4, k5, k6 = st.columns(3)
        with k4:
            mdd = d.get("max_drawdown")
            ok = mdd is not None and mdd <= tg["max_drawdown"]
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">最大回撤{"✅" if ok else ""}</div>'
                        f'<div class="kpi-value {"up" if ok else "down"}">{mdd*100:.2f}%</div>'
                        f'<div class="kpi-label">硬线 ≤ {tg["max_drawdown"]*100:.0f}%</div></div>',
                        unsafe_allow_html=True)
        with k5:
            exp = d.get("expectancy")
            ok = exp is not None and exp > 0
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">期望值{"✅" if ok else ""}</div>'
                        f'<div class="kpi-value {"up" if ok else "down"}">{exp if exp is not None else "—"}</div>'
                        f'<div class="kpi-label">硬线 > 0</div></div>',
                        unsafe_allow_html=True)
        with k6:
            vr = d.get("val_return")
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">验证期收益</div>'
                        f'<div class="kpi-value">{vr*100:+.2f}%</div></div>' if vr is not None else
                        f'<div class="kpi-card"><div class="kpi-label">验证期收益</div>'
                        f'<div class="kpi-value">—</div></div>',
                        unsafe_allow_html=True)

        excl = d.get("excluded_round_trips", 0)
        if excl:
            st.warning(f"⚠ 有 {excl} 笔成交因策略错配被剔除出毕业样本（历史保留，见 graduation_exclusions 表）")
    except Exception as e:
        st.error(f"毕业数据读取失败：{e}")

    # ---- 风控指标 ----
    st.subheader("风控指标")
    row = q("SELECT nav,cash,position_value FROM daily_report"
            " WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (acct,))
    if row:
        nav, cash, pv = row[0]
        pos_ratio = pv / nav if nav else 0
        c1, c2, c3 = st.columns(3)
        with c1:
            ok = pos_ratio <= 0.7
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">仓位 {"✅" if ok else "⚠"}</div>'
                        f'<div class="kpi-value {"up" if ok else "down"}">{pos_ratio*100:.1f}%</div>'
                        f'<div class="kpi-label">上限 70%</div></div>', unsafe_allow_html=True)
        with c2:
            pos_r = q("SELECT symbol,market_value FROM positions WHERE account=? AND qty>0 ORDER BY market_value DESC", (acct,))
            conc = (pos_r[0][1] / nav) if pos_r and nav else 0
            sym = pos_r[0][0] if pos_r else "—"
            ok = conc <= 0.4
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">集中度（{sym}）{"✅" if ok else "⚠"}</div>'
                        f'<div class="kpi-value {"up" if ok else "down"}">{conc*100:.1f}%</div>'
                        f'<div class="kpi-label">上限 40%</div></div>', unsafe_allow_html=True)
        with c3:
            last_buy = q("""SELECT qty*price FROM fills WHERE account=? AND side='BUY'
                            ORDER BY date DESC LIMIT 1""", (acct,))
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">最近单笔投入</div>'
                        f'<div class="kpi-value">{last_buy[0][0]:,.0f}</div>' if last_buy else
                        f'<div class="kpi-card"><div class="kpi-label">最近单笔投入</div>'
                        f'<div class="kpi-value">—</div></div>', unsafe_allow_html=True)

    # 破止损仍持仓检查
    st.subheader("破止损仍持仓")
    breached = q("""SELECT p.symbol, p.qty, p.avg_cost, p.last_price
                    FROM positions p WHERE p.account=? AND p.qty>0""", (acct,))
    strat = strategy_of(acct)
    found = False
    for s, qty_, avg_, last_ in breached:
        if strat == "buyback" and last_ and avg_:
            if (last_ - avg_) / avg_ <= -0.05:
                st.error(f"🔴 {s} 现价 {last_:.2f} 已跌破止损线（成本 {avg_:.2f}，-{abs((last_-avg_)/avg_*100):.1f}%），仍持有 {qty_} 股")
                found = True
    if not found:
        st.success("✅ 无破止损持仓")

    # ---- 审计 ----
    st.subheader("审计")
    excl_r = q("SELECT date, account, symbol, side, reason FROM graduation_exclusions ORDER BY date DESC")
    if excl_r:
        st.write("**毕业样本剔除记录**（成交保留，仅不计入毕业指标）：")
        for d, a, s, sd, reason in excl_r:
            st.write(f"- {d} {a} {s} {sd}: {reason}")
    else:
        st.write("无剔除记录")

    # ---- 底部验证仪表盘（5指标 × 3账户，对标验证终端） ----
    st.subheader("📊 验证仪表盘")
    try:
        from sync_snapshot import build_graduation
        con = sqlite3.connect(DB)
        g = build_graduation(con)
        con.close()
        tg = g["targets"]
        # 表头
        hdr = ["指标"] + [f"{aid}（{ACCT_CN.get(aid, aid)}）" for aid in ["sim_a", "sim_hk", "sim_crypto"]]
        rows = []
        # 样本数
        r = ["样本数"]
        for aid in ["sim_a", "sim_hk", "sim_crypto"]:
            d = g["accounts"].get(aid, {})
            rt = d.get("round_trips") or 0
            ok = tg["sample_min"] <= rt <= tg["sample_max"]
            r.append(f"{'✅' if ok else '⏳'} {rt}")
        rows.append(r)
        # 盈亏比
        r = ["盈亏比"]
        for aid in ["sim_a", "sim_hk", "sim_crypto"]:
            d = g["accounts"].get(aid, {})
            wlr = d.get("win_loss_ratio")
            ok = wlr is not None and wlr >= tg["win_loss_ratio"]
            r.append(f"{'✅' if ok else ''} {wlr if wlr else '—'}")
        rows.append(r)
        # 最大回撤
        r = ["最大回撤"]
        for aid in ["sim_a", "sim_hk", "sim_crypto"]:
            d = g["accounts"].get(aid, {})
            mdd = d.get("max_drawdown")
            ok = mdd is not None and mdd <= tg["max_drawdown"]
            r.append(f"{'✅' if ok else ''} {mdd*100:.1f}%" if mdd is not None else "—")
        rows.append(r)
        # 期望值
        r = ["期望值"]
        for aid in ["sim_a", "sim_hk", "sim_crypto"]:
            d = g["accounts"].get(aid, {})
            exp = d.get("expectancy")
            ok = exp is not None and exp > 0
            r.append(f"{'✅' if ok else ''} {exp}" if exp is not None else "—")
        rows.append(r)
        # 验证期收益
        r = ["验证期收益"]
        for aid in ["sim_a", "sim_hk", "sim_crypto"]:
            d = g["accounts"].get(aid, {})
            vr = d.get("val_return")
            r.append(f"{vr*100:+.1f}%" if vr is not None else "—")
        rows.append(r)
        st.table([dict(zip(hdr, row)) for row in rows])
        st.caption(f"硬线：样本{tg['sample_min']}-{tg['sample_max']}笔 · 盈亏比≥{tg['win_loss_ratio']} · "
                   f"回撤≤{tg['max_drawdown']*100:.0f}% · 期望值>0")
    except Exception as e:
        st.error(f"仪表盘读取失败：{e}")

# ================================================================
# ⚙ 运维：数据源 / 规则版本 / 指令日志
# ================================================================
elif tab == "⚙ 运维":
    st.markdown('<div class="section-head">⚙ 运维</div>', unsafe_allow_html=True)

    st.subheader("数据源")
    ds = CFG.get("data_sources", {})
    for k, v in ds.items():
        st.write(f"- **{k}**: {v}")

    st.subheader("规则版本")
    st.write(f"- 版本：{ver}")
    st.write(f"- 说明：{CFG.get('meta', {}).get('note', '')}")
    st.write("- 分账户策略：")
    for a in CFG["accounts"]:
        aid = a["id"]
        s = strategy_of(aid)
        st.write(f"  - {aid}（{ACCT_CN.get(aid, aid)}）: **{STRATEGY_CN.get(s, s)}** — {strategy_desc(s)}")

    st.subheader("指令日志")
    logs = q("SELECT date, account, symbol, side, decision, updated FROM approvals ORDER BY updated DESC LIMIT 20")
    if logs:
        st.table([{"日期": d, "账户": a, "标的": s, "方向": sd, "决策": dc, "时间": t}
                  for d, a, s, sd, dc, t in logs])
    else:
        st.write("暂无指令记录")

    # 数据事故自查
    st.subheader("数据完整性")
    fills_n = q("SELECT COUNT(*) FROM fills")[0][0]
    sig_n = q("SELECT COUNT(*) FROM signals")[0][0]
    st.write(f"- fills 全量 {fills_n} 笔 · signals 全量 {sig_n} 笔")
    st.caption("审计口径：毕业样本 = fills(SELL, 验证期) − graduation_exclusions")

st.caption("独立交易终端 · 8502 · 人工执行 · 规则版本 %s" % ver)
