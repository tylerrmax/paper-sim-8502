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

# ---- 敏感数据：从私有仓库拉取（公网部署时） ----
def _fetch_private_data():
    """从私有仓库 paper-sim-8502-data 拉取 db/sim.db 和 config/strategy.yaml。
    本地运行时文件已存在则跳过；公网时通过 Streamlit Secrets 的 GITHUB_PAT 拉取。"""
    db_path = os.path.join(BASE, "db", "sim.db")
    cfg_path = os.path.join(BASE, "config", "strategy.yaml")
    if os.path.exists(db_path) and os.path.exists(cfg_path):
        return  # 本地已有
    try:
        pat = st.secrets.get("GITHUB_PAT", "")
        if not pat:
            return
        import urllib.request
        import base64
        for remote, local in [("db/sim.db", db_path), ("config/strategy.yaml", cfg_path)]:
            url = f"https://api.github.com/repos/tylerrmax/paper-sim-8502-data/contents/{remote}"
            req = urllib.request.Request(url, headers={
                "Authorization": f"token {pat}",
                "Accept": "application/vnd.github.v3.raw"
            })
            os.makedirs(os.path.dirname(local), exist_ok=True)
            with urllib.request.urlopen(req, timeout=30) as r, open(local, "wb") as f:
                f.write(r.read())
    except Exception:
        pass  # 拉取失败时保持现状，由后续逻辑报错

_fetch_private_data()
sys.path.insert(0, BASE)
DB = os.path.join(BASE, "db", "sim.db")
CFG_PATH = os.path.join(BASE, "config", "strategy.yaml")

# 云端模式检测：Streamlit Cloud 文件系统是临时的，写操作不持久
# 通过环境变量 STREAMLIT_CLOUD 判断
IS_CLOUD = os.environ.get("STREAMLIT_CLOUD") == "1" or "streamlit" in os.environ.get("HOSTNAME", "")
CLOUD_DATA_ASOF = ""  # 云端数据推送时间（从 db/sim.db 的 mtime 推断）
if IS_CLOUD:
    # 云端用只读模式：复制 DB 到临时位置避免锁定问题
    import tempfile, shutil
    _tmp = os.path.join(tempfile.gettempdir(), "sim_cloud.db")
    try:
        shutil.copy2(DB, _tmp)
        DB = _tmp
        # 记录仓库内 db/sim.db 的推送时间（GitHub commit 时间近似）
        _src_db = os.path.join(BASE, "db", "sim.db")
        if os.path.exists(_src_db):
            import datetime as _dt
            _mtime = os.path.getmtime(_src_db)
            CLOUD_DATA_ASOF = _dt.datetime.fromtimestamp(_mtime).strftime("%Y-%m-%d %H:%M")
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

# 深色交易终端视觉规范（统一，禁止白色卡片混搭）
st.markdown("""
<style>
/* 全局：深色终端底 */
.main-title { font-size: 28px; font-weight: 800; margin-bottom: 4px; letter-spacing: 1px; color: #1a1a2e; }
@media (prefers-color-scheme: dark) { .main-title { color: #e8ecf1; } }
.sub-title { font-size: 13px; color: #8a8f9c; margin-bottom: 16px; }
/* 统一卡片：深色 */
.kpi-card { background: #10141b; border: 1px solid #1e2532; border-radius: 10px;
            padding: 14px 16px; margin-bottom: 10px; color: #e8ecf1; }
.kpi-label { font-size: 12px; color: #8a8f9c; margin-bottom: 4px; }
.kpi-value { font-size: 22px; font-weight: 700; font-family: ui-monospace, SFMono-Regular, monospace; color: #e8ecf1; }
.kpi-value.up { color: #2ebd85; } .kpi-value.down { color: #f6465d; }
.kpi-value.big { font-size: 28px; }
/* 数字统一：等宽字体 */
.num { font-family: ui-monospace, SFMono-Regular, monospace; font-variant-numeric: tabular-nums; }
.pos { color: #2ebd85; } .neg { color: #f6465d; } .flat { color: #8a8f9c; }
/* 策略徽标 */
.strategy-badge { display: inline-block; padding: 3px 12px; border-radius: 12px;
                  font-size: 12px; font-weight: 600; }
.strategy-badge.buyback { background: rgba(46,189,133,.15); color: #2ebd85; }
.strategy-badge.ma20 { background: rgba(240,185,11,.15); color: #f0b90b; }
/* 分区标题 */
.section-head { font-size: 18px; font-weight: 700; margin: 20px 0 12px 0;
                padding-bottom: 8px; border-bottom: 2px solid #1e2532; color: #e8ecf1; }
/* 横幅：四态 */
.alert-banner { border-radius: 8px; padding: 12px 16px; margin-bottom: 12px; font-size: 14px; }
.alert-banner.warn { background: rgba(240,185,11,.12); border: 1px solid rgba(240,185,11,.3); color: #f0b90b; }
.alert-banner.info { background: rgba(24,144,255,.1); border: 1px solid rgba(24,144,255,.3); color: #1890ff; }
.alert-banner.success { background: rgba(46,189,133,.1); border: 1px solid rgba(46,189,133,.3); color: #2ebd85; }
.alert-banner.danger { background: rgba(246,70,93,.1); border: 1px solid rgba(246,70,93,.35); color: #f6465d; }
/* 状态卡片：深色统一（替代原白色 .status-card） */
.status-card { background: #10141b; border: 1px solid #1e2532; border-radius: 10px;
               padding: 16px; margin-bottom: 12px; color: #e8ecf1; }
.status-card .sc-label { font-size: 13px; color: #8a8f9c; margin-bottom: 8px; }
.status-card .sc-value { font-size: 26px; font-weight: 700; font-family: ui-monospace, monospace; color: #e8ecf1; }
.status-card .sc-value.paused { color: #8a8f9c; }
.status-card .sc-value.nodata { color: #5a6270; }
/* 行情状态徽标：实时 / 最近收盘 / 已过期 / 未知 */
.qs { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 11px; font-weight: 700; }
.qs-live { background: rgba(46,189,133,.18); color: #2ebd85; border: 1px solid rgba(46,189,133,.4); }
.qs-close { background: rgba(24,144,255,.14); color: #1890ff; border: 1px solid rgba(24,144,255,.35); }
.qs-stale { background: rgba(240,185,11,.14); color: #f0b90b; border: 1px solid rgba(240,185,11,.35); }
.qs-unknown { background: rgba(138,143,156,.14); color: #8a8f9c; border: 1px solid rgba(138,143,156,.35); }
/* 验证状态徽标：达标 / 未达标 / 样本不足 / 数据异常 */
.vs { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 11px; font-weight: 700; }
.vs-pass { background: rgba(46,189,133,.18); color: #2ebd85; border: 1px solid rgba(46,189,133,.4); }
.vs-fail { background: rgba(246,70,93,.14); color: #f6465d; border: 1px solid rgba(246,70,93,.4); }
.vs-insufficient { background: rgba(240,185,11,.14); color: #f0b90b; border: 1px solid rgba(240,185,11,.35); }
.vs-anomaly { background: rgba(177,59,179,.16); color: #c77ddb; border: 1px solid rgba(177,59,179,.4); }
/* 按钮状态 */
.btn-state { display: inline-block; padding: 4px 12px; border-radius: 8px; font-size: 12px; font-weight: 600; }
.btn-submitted { background: rgba(24,144,255,.15); color: #1890ff; }
.btn-running { background: rgba(240,185,11,.15); color: #f0b90b; }
.btn-done { background: rgba(46,189,133,.15); color: #2ebd85; }
.btn-failed { background: rgba(246,70,93,.15); color: #f6465d; }
/* 表格：深色 */
table { color: #e8ecf1; }
/* 响应式：手机端卡片全宽 */
@media (max-width: 768px) {
  .main-title { font-size: 22px; }
  .kpi-value.big { font-size: 22px; }
  .status-card .sc-value { font-size: 20px; }
}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-title">独立交易终端</div>', unsafe_allow_html=True)

# 数据日期（交易员需要知道数据新旧，不说教）
if CLOUD_DATA_ASOF:
    st.caption(f"数据截至 {CLOUD_DATA_ASOF}")

# ============ 行情状态四态判定 + 数字格式化助手 ============
from datetime import datetime, timedelta

def quote_state(symbol, live_time_str, db_date_str):
    """行情四态：live(实时)/close(最近收盘)/stale(已过期)/unknown(未知)
    live: 实时接口返回且时间戳在15分钟内（交易时段）
    close: 非交易时段但有当日/上一交易日收盘数据
    stale: 数据超过2个自然日
    unknown: 无任何数据"""
    now = datetime.now()
    # 实时：有 live 时间戳且新鲜
    if live_time_str:
        try:
            lt = datetime.strptime(live_time_str.strip(), "%Y-%m-%d %H:%M:%S")
            if now - lt < timedelta(minutes=30):
                return "live", live_time_str.strip()
        except (ValueError, TypeError):
            pass
    # DB 快照日期判定
    if db_date_str:
        try:
            dd = datetime.strptime(db_date_str, "%Y-%m-%d").date()
            days_old = (now.date() - dd).days
            if days_old <= 1:
                return "close", db_date_str
            else:
                return "stale", db_date_str
        except (ValueError, TypeError):
            pass
    return "unknown", ""

QS_BADGE = {
    "live": '<span class="qs qs-live">●实时</span>',
    "close": '<span class="qs qs-close">最近收盘</span>',
    "stale": '<span class="qs qs-stale">已过期</span>',
    "unknown": '<span class="qs qs-unknown">未知</span>',
}
QS_CN = {"live": "实时", "close": "最近收盘", "stale": "已过期", "unknown": "未知"}

def fmt_money(v, ccy=""):
    """统一货币格式：千分位，2位小数"""
    try:
        s = f"{float(v):,.2f}"
    except (ValueError, TypeError):
        s = "--"
    return f"{s} {ccy}".strip() if ccy else s

def fmt_pct(v):
    """统一涨跌幅格式：+x.xx% / -x.xx%，着色"""
    try:
        f = float(v)
        cls = "pos" if f > 0 else ("neg" if f < 0 else "flat")
        arrow = "▲" if f > 0 else ("▼" if f < 0 else "")
        return f'<span class="num {cls}">{arrow}{abs(f):.2f}%</span>'
    except (ValueError, TypeError):
        return '<span class="num flat">--</span>'

def fmt_num(v, dec=2):
    """统一数字格式"""
    try:
        return f'<span class="num">{float(v):,.{dec}f}</span>'
    except (ValueError, TypeError):
        return '<span class="num flat">--</span>'

def _ap_state(key, decision, d, acct, s, sd):
    """审批按钮四态：已提交→执行中→已生效/失败，写入后校验"""
    sk = f"apstate_{key}"
    st.session_state[sk] = "已提交"
    st.session_state[sk] = "执行中"
    try:
        con = sqlite3.connect(DB)
        con.execute(
            "INSERT INTO approvals(date,account,symbol,side,decision,updated)"
            " VALUES(?,?,?,?,?,?)",
            (d, acct, s, sd, decision,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        con.commit()
        chk = con.execute(
            "SELECT COUNT(*) FROM approvals WHERE date=? AND account=? AND symbol=? AND side=? AND decision=?",
            (d, acct, s, sd, decision)).fetchone()[0]
        con.close()
        st.session_state[sk] = "已生效" if chk > 0 else "失败"
    except Exception:
        st.session_state[sk] = "失败"

def vs_badge(state):
    """验证四态徽标：pass/fail/insufficient/anomaly"""
    m = {"pass": '<span class="vs vs-pass">达标</span>',
         "fail": '<span class="vs vs-fail">未达标</span>',
         "insufficient": '<span class="vs vs-insufficient">样本不足</span>',
         "anomaly": '<span class="vs vs-anomaly">数据异常</span>'}
    return m.get(state, '<span class="vs vs-unknown">未知</span>')

def _vs_state(value, target, higher_is_better=True, sample_n=None, sample_min=None):
    """判定验证状态：先看样本是否足够，再看是否达标"""
    if value is None:
        return "anomaly"
    if sample_n is not None and sample_min is not None and sample_n < sample_min:
        return "insufficient"
    ok = (value >= target) if higher_is_better else (value <= target)
    return "pass" if ok else "fail"

# ============ 滚动行情条（四态：实时/最近收盘/已过期/未知） ============
def render_ticker():
    """作战大屏：代码 | 名称 | 现价 | 涨幅% | 技术信号 | 待建单，完整闭环"""
    try:
        from engine.realtime import fetch_all
        syms = [r[0] for r in q("SELECT DISTINCT symbol FROM market_data")]
        live = fetch_all(syms)
    except Exception:
        live = {}
        syms = []
    from engine.indicators import rsi, macd, bollinger, adx, market_regime, ma_align

    # 待建单：未成交的信号（新格式：方向 动作类型 @价格 | 止损 @价格）
    pending = {}
    for d, sym, side, px, act, stop_px in q("""SELECT s.date, s.symbol, s.side, s.price,
        s.action_type, s.stop_price FROM signals s
        LEFT JOIN fills f ON s.date=f.signal_date AND s.account=f.account
          AND s.symbol=f.symbol AND s.side=f.side
        WHERE f.id IS NULL AND s.date >= date('now','-7 days')"""):
        # 动作类型：老信号无值时推断
        if not act:
            act = "开仓" if side == "BUY" else "信号平仓"
        if side == "BUY":
            # BUY 开仓 @价格 | 止损 @价格
            stop_s = f" | 止损 @{stop_px:.2f}" if stop_px else ""
            pending[sym] = f"{side} {act} @{px:.2f}{stop_s}"
        else:
            # SELL 止盈/止损/信号平仓 @价格 + 持仓上下文
            pos = q("SELECT qty, avg_cost FROM positions WHERE symbol=? AND qty>0", (sym,))
            if pos:
                qty, cost = pos[0]
                # 浮盈
                cur = q("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,))
                cur_px = cur[0][0] if cur else px
                pnl_pct = (cur_px - cost) / cost * 100 if cost else 0
                # 判断止盈/止损：浮盈>0为止盈，<-5%为止损，否则信号平仓
                if not act or act == "信号平仓":
                    if pnl_pct > 0:
                        act = "止盈"
                    elif pnl_pct < -5:
                        act = "止损"
                hold_s = f"<br><span style='font-size:11px;color:#888;'>持仓：是 | 成本 {cost:.2f} | 浮盈 {pnl_pct:+.1f}%</span>"
            else:
                hold_s = f"<br><span style='font-size:11px;color:#888;'>无持仓，仅供观察</span>"
            pending[sym] = f"{side} {act} @{px:.2f}{hold_s}"

    def _tech_signals(sym):
        bars = q("SELECT close,high,low FROM market_data WHERE symbol=? ORDER BY date", (sym,))
        if len(bars) < 30:
            return "-"
        closes = [r[0] for r in bars]
        highs = [r[1] for r in bars]
        lows = [r[2] for r in bars]
        tags = []
        rsi_v = rsi(closes)[-1]
        if rsi_v and rsi_v > 70: tags.append("超买")
        elif rsi_v and rsi_v < 30: tags.append("超卖")
        _, _, hist = macd(closes)
        tags.append("MACD多" if (hist[-1] and hist[-1] > 0) else "MACD空")
        adx_v = adx(highs, lows, closes)[-1]
        regime = market_regime(adx_v)
        tags.append("趋势" if regime == "trend" else "震荡")
        align = ma_align(closes)
        if align > 0.5: tags.append("均线多")
        elif align < -0.5: tags.append("均线空")
        upper, _, lower = bollinger(closes)
        if closes[-1] > upper[-1]: tags.append("破上轨")
        if closes[-1] < lower[-1]: tags.append("破下轨")
        return " ".join(tags)

    def _row(sym):
        d = live.get(sym, {})
        price = d.get("price")
        chg = d.get("change_pct", 0)
        if price:
            price_s = f"{price}"
        else:
            row = q("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,))
            price = row[0][0] if row and row[0][0] else 0
            price_s = f"{price:.2f}" if price else "--"
            if row:
                prev = q("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1 OFFSET 1", (sym,))
                if prev and prev[0][0]:
                    chg = (price - prev[0][0]) / prev[0][0] * 100
        color = "#f6465d" if chg >= 0 else "#2ebd85"
        sign = "+" if chg >= 0 else ""
        code_show = sym[2:] if sym.startswith("HK") else sym
        name_show = SYMBOL_CN.get(sym, "")
        tech = _tech_signals(sym)
        order = pending.get(sym, "-")
        order_style = f'color:#f0b90b;font-weight:bold;' if order != "-" else 'color:#666;'
        return (f'<tr><td style="padding:8px;">{code_show}</td>'
                f'<td style="padding:8px;color:#1890ff;">{name_show}</td>'
                f'<td class="num" style="padding:8px;color:{color};text-align:right;">{price_s}</td>'
                f'<td style="padding:8px;color:{color};text-align:right;">{sign}{chg:.2f}%</td>'
                f'<td style="padding:8px;font-size:12px;color:#aaa;">{tech}</td>'
                f'<td style="padding:8px;{order_style}">{order}</td></tr>')

    # 分类
    a_syms, hk_syms, crypto_syms = [], [], []
    all_syms = sorted(set(list(live.keys()) + [r[0] for r in q("SELECT DISTINCT symbol FROM market_data")]))
    for sym in all_syms:
        if sym == "BTCUSDT":
            crypto_syms.append(sym)
        elif sym.startswith("HK"):
            hk_syms.append(sym)
        else:
            a_syms.append(sym)

    def _table(syms_list, title):
        if not syms_list:
            return ""
        # 按信号-操作优先级排序：有待建单的排前面
        syms_sorted = sorted(syms_list, key=lambda s: (0 if s in pending else 1, s))
        rows = "".join(_row(s) for s in syms_sorted)
        return (f'<div style="margin-bottom:16px;"><b>{title}</b>'
                f'<table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:4px;">'
                f'<tr style="color:#888;font-size:12px;border-bottom:1px solid #1e2532;">'
                f'<td style="padding:6px 8px;">代码</td><td style="padding:6px 8px;">名称</td>'
                f'<td style="padding:6px 8px;text-align:right;">现价</td>'
                f'<td style="padding:6px 8px;text-align:right;">涨幅%</td>'
                f'<td style="padding:6px 8px;">技术信号</td>'
                f'<td style="padding:6px 8px;">待建单</td></tr>'
                f'{rows}</table></div>')

    st.markdown(_table(a_syms, "A股") + _table(hk_syms, "港股") + _table(crypto_syms, "加密"),
                unsafe_allow_html=True)


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
    _ACCT = {"sim_a": "A股", "sim_hk": "港股", "sim_crypto": "加密货币"}
    badges = " ".join(
        f'<span class="strategy-badge {strategy_of(a["id"])}">{_ACCT.get(a["id"], a["id"])}：{STRATEGY_CN.get(strategy_of(a["id"]), strategy_of(a["id"]))}</span>'
        for a in CFG["accounts"])
    st.markdown(f'<div class="kpi-card"><div class="kpi-label">分账户策略（2026-10-16 选A）</div>'
                f'<div style="margin-top:6px">{badges}</div></div>',
                unsafe_allow_html=True)

# ============ 导航（对标人工交易终端工作流） ============
tab = st.sidebar.radio("板块", ["🏠 工作台", "🔍 选股", "📝 交易", "💰 账户", "🛡 验证"])
st.sidebar.markdown("---")

# 侧边栏：控制（云端只读时整个隐藏，不占地方）
if not IS_CLOUD:
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
ACCT_CN = {"sim_a": "A股", "sim_hk": "港股", "sim_crypto": "加密货币"}
SYMBOL_CN = {
    "002446": "盛路通信",
    "002472": "双环传动",
    "002639": "雪人股份",
    "002812": "恩捷股份",
    "300827": "上能电气",
    "600150": "中国船舶",
    "688305": "科德数控",
    "HK9660": "地平线机器人",
    "HK0354": "中软国际",
    "BTCUSDT": "比特币",
}
def sym_name(code):
    """标的显示名：代码+中文名，如 '002446 盛路通信'"""
    return f"{code} {SYMBOL_CN[code]}" if code in SYMBOL_CN else code
render_ticker()
# 标的代码→中文名（展示用，查不到时回退显示代码）

# 账户选择：工作台为三账户统一视图，不再用全局下拉；
# 需要按账户筛选的页面（交易/账户）使用页内选择器
acct = "sim_a"  # 默认，页内可覆盖

# ================================================================
# 🏠 工作台：系统状态 + 今日信号 + 账户总览
# ================================================================
if tab == "🏠 工作台":
    st.markdown('<div class="section-head">🏠 工作台</div>', unsafe_allow_html=True)

    # ---- 核心数据（验证终端优势沿用）：总资产/当日盈亏/持仓市值/可用资金 ----
    # 交易所标准：红涨绿跌，数字 vibrant
    st.markdown("**核心数据**")
    core = {}
    for aid in ["sim_a", "sim_hk", "sim_crypto"]:
        r = q("SELECT nav, pnl_day, cash, position_value, ret_total FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (aid,))
        if r:
            core[aid] = {"nav": r[0][0], "pnl": r[0][1] or 0, "cash": r[0][2] or 0, "pos": r[0][3] or 0, "ret": r[0][4] or 0}
    # 货币：A股CNY 港股HKD 加密USDT
    def _fmt(v): return f"{v:,.2f}"
    # 交易所色：涨红 #ff4d4f，跌绿 #00c853
    def _rg(v): return "#ff4d4f" if v >= 0 else "#00c853"
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        ra = core.get("sim_a", {}).get("ret", 0)
        rh = core.get("sim_hk", {}).get("ret", 0)
        rc = core.get("sim_crypto", {}).get("ret", 0)
        st.markdown(f'''<div style="background:#1a1a2e;border:1px solid #1e2532;border-radius:8px;padding:16px;">
            <div style="color:#888;font-size:13px;">账户总资产</div>
            <div class="num" style="font-size:22px;margin:8px 0;color:#ffffff;"><span style="font-size:13px;color:#999;">A股 </span>¥{_fmt(core.get("sim_a", {}).get("nav", 0))}</div>
            <div class="num" style="color:#ddd;font-size:16px;"><span style="font-size:13px;color:#999;">港股 </span>HK${_fmt(core.get("sim_hk", {}).get("nav", 0))}</div>
            <div class="num" style="color:#ddd;font-size:16px;"><span style="font-size:13px;color:#999;">加密货币 </span>${_fmt(core.get("sim_crypto", {}).get("nav", 0))}</div>
            <div style="font-size:12px;margin-top:4px;">
                <span style="color:{_rg(ra)};">{ra*100:+.2f}%</span>
                <span style="color:#666;"> | </span>
                <span style="color:{_rg(rh)};">{rh*100:+.2f}%</span>
                <span style="color:#666;"> | </span>
                <span style="color:{_rg(rc)};">{rc*100:+.2f}%</span>
            </div>
            </div>''', unsafe_allow_html=True)
    with c2:
        pnl_a = core.get("sim_a", {}).get("pnl", 0)
        pnl_hk = core.get("sim_hk", {}).get("pnl", 0)
        pnl_c = core.get("sim_crypto", {}).get("pnl", 0)
        st.markdown(f'''<div style="background:#1a1a2e;border:1px solid #1e2532;border-radius:8px;padding:16px;">
            <div style="color:#888;font-size:13px;">当日盈亏</div>
            <div class="num" style="font-size:22px;margin:8px 0;color:{_rg(pnl_a)};"><span style="font-size:13px;color:#999;">A股 </span>¥{_fmt(pnl_a)}</div>
            <div class="num" style="color:{_rg(pnl_hk)};font-size:14px;">HK${_fmt(pnl_hk)}</div>
            <div class="num" style="color:{_rg(pnl_c)};font-size:14px;">${_fmt(pnl_c)}</div>
            </div>''', unsafe_allow_html=True)
    with c3:
        st.markdown(f'''<div style="background:#1a1a2e;border:1px solid #1e2532;border-radius:8px;padding:16px;">
            <div style="color:#888;font-size:13px;">持仓市值</div>
            <div class="num" style="font-size:22px;margin:8px 0;color:#fff;"><span style="font-size:13px;color:#999;">A股 </span>¥{_fmt(core.get("sim_a", {}).get("pos", 0))}</div>
            <div class="num" style="color:#888;font-size:14px;">HK${_fmt(core.get("sim_hk", {}).get("pos", 0))}</div>
            <div class="num" style="color:#888;font-size:14px;">${_fmt(core.get("sim_crypto", {}).get("pos", 0))}</div>
            </div>''', unsafe_allow_html=True)
    with c4:
        st.markdown(f'''<div style="background:#1a1a2e;border:1px solid #1e2532;border-radius:8px;padding:16px;">
            <div style="color:#888;font-size:13px;">可用资金</div>
            <div class="num" style="font-size:22px;margin:8px 0;color:#fff;"><span style="font-size:13px;color:#999;">A股 </span>¥{_fmt(core.get("sim_a", {}).get("cash", 0))}</div>
            <div class="num" style="color:#888;font-size:14px;">HK${_fmt(core.get("sim_hk", {}).get("cash", 0))}</div>
            <div class="num" style="color:#888;font-size:14px;">${_fmt(core.get("sim_crypto", {}).get("cash", 0))}</div>
            </div>''', unsafe_allow_html=True)

    # ---- 今日待办（最优先）：一眼知道要做什么 ----
    # 待建条件单
    pending_orders = q("""SELECT COUNT(*) FROM signals s
        LEFT JOIN fills f ON s.date=f.signal_date AND s.account=f.account
          AND s.symbol=f.symbol AND s.side=f.side
        WHERE f.id IS NULL AND s.date >= date('now','-7 days')""")[0][0] or 0
    # 待审批信号
    pending_signals = q("""SELECT COUNT(*) FROM signals s
        LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account
          AND ap.symbol=s.symbol AND ap.side=s.side
        LEFT JOIN fills f ON f.date=s.date AND f.account=s.account
          AND f.symbol=s.symbol AND f.side=s.side
        WHERE s.date>=date('now','-60 days') AND ap.date IS NULL AND f.date IS NULL""")[0][0] or 0
    if pending_orders > 0 or pending_signals > 0:
        st.markdown(f'''<div style="background:#1a1a2e;border:1px solid #f0b90b;border-radius:8px;
            padding:12px 16px;margin-bottom:16px;">
            <b>📋 今日待办</b><br>
            {"· " + str(pending_orders) + "个待建条件单 → 📝交易页<br>" if pending_orders else ""}
            {"· " + str(pending_signals) + "个待审批信号" if pending_signals else ""}
            </div>''', unsafe_allow_html=True)
    else:
        st.markdown('''<div style="background:#1a1a2e;border:1px solid #2ebd85;border-radius:8px;
            padding:12px 16px;margin-bottom:16px;">✅ 今日无待办</div>''',
            unsafe_allow_html=True)

    # ---- 1. 风险警报区 ----
    alerts = []
    if status != "running":
        alerts.append(("warn", "⏸ 系统已暂停：新信号不会成交。已挂在券商的条件单请在券商端自行检查/撤销。"))
    # 破止损持仓检查（buyback）
    try:
        _bs = q("""SELECT p.symbol, p.qty, p.avg_cost, m.close FROM positions p
                   JOIN (SELECT symbol, MAX(date) md FROM market_data GROUP BY symbol) t
                   ON p.symbol=t.symbol
                   JOIN market_data m ON m.symbol=t.symbol AND m.date=t.md
                   WHERE p.account=? AND p.qty>0""", (acct,))
        for _s, _q, _avg, _last in _bs:
            if _avg and _last and _last < _avg * 0.95 and strategy_of(acct) == "buyback":
                alerts.append(("danger", f"🔴 破止损仍持仓：{_s} 现价{_last:.2f} < 成本{_avg:.2f}×0.95"))
    except Exception:
        pass
    for _lvl, _msg in alerts:
        st.markdown(f'<div class="alert-banner {_lvl}">{_msg}</div>', unsafe_allow_html=True)

    # ---- 2. 系统状态 + 行情新鲜度（四态） ----
    latest_a = q("SELECT MAX(date) FROM market_data WHERE symbol NOT LIKE 'HK%' AND symbol NOT LIKE '%USDT'")
    latest_hk = q("SELECT MAX(date) FROM market_data WHERE symbol LIKE 'HK%'")
    latest_c = q("SELECT MAX(date) FROM market_data WHERE symbol LIKE '%USDT'")
    def _fresh_card(label, rows):
        d = rows[0][0] if rows and rows[0][0] else ""
        st_, ts = quote_state("", "", d)
        badge = QS_BADGE[st_]
        val = ts if ts else "无数据"
        return (f'<div class="status-card"><div class="sc-label">{label} {badge}</div>'
                f'<div class="sc-value nodata" style="font-size:18px">{val}</div></div>')
    def _fund_flow_panel(aid):
        """主力资金：近5日净流入/流出（单位：万）"""
        ccy = {"sim_a": "万", "sim_hk": "万", "sim_crypto": "万"}.get(aid, "")
        rows = q("SELECT date, SUM(CASE WHEN side='BUY' THEN qty*price ELSE 0 END) as buy_amt,"
                 " SUM(CASE WHEN side='SELL' THEN qty*price ELSE 0 END) as sell_amt,"
                 " SUM(fee) as fees FROM fills WHERE account=? GROUP BY date"
                 " ORDER BY date DESC LIMIT 5", (aid,))
        if not rows:
            st.write("暂无")
            return
        for d, buy_amt, sell_amt, fees in rows:
            net = (sell_amt or 0) - (buy_amt or 0) - (fees or 0)
            net_wan = net / 10000
            color = "#00c853" if net >= 0 else "#ff4d4f"
            arrow = "▲" if net >= 0 else "▼"
            st.markdown(f'<div style="padding:6px 0;font-size:15px;"><span style="color:#aaa;">{d}</span> '
                        f'<span style="color:{color};font-weight:bold;">{arrow} {net_wan:+.2f}{ccy}</span></div>',
                        unsafe_allow_html=True)

    # 主力资金（三账户并排）
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        with st.expander("💰 A股资金流", expanded=False):
            _fund_flow_panel("sim_a")
    with fc2:
        with st.expander("💰 港股资金流", expanded=False):
            _fund_flow_panel("sim_hk")
    with fc3:
        with st.expander("💰 加密资金流", expanded=False):
            _fund_flow_panel("sim_crypto")

    # ---- 3. 三账户概览（并排） ----
    # ---- 4. 待处理信号（今日信号 + 待审批） ----
    # ---- 5. 当前账户总览 ----
        st.info("该账户暂无数据")

    # 净值曲线
    hist = q("SELECT date,nav FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date", (acct,))
    if hist:
        st.subheader("净值曲线")
        import pandas as pd
        df = pd.DataFrame(hist, columns=["date", "nav"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        st.line_chart(df["nav"])

    # 三账户并排（对标验证终端）
    # 事件流（最近动态）- 折叠
    with st.expander("📡 事件流", expanded=False):
        events = []
        for d, a, s, sd, qty, px in q(
                "SELECT date,account,symbol,side,qty,price FROM fills ORDER BY date DESC LIMIT 5"):
            events.append((d, f"💰 {ACCT_CN.get(a,a)} {sym_name(s)} {sd} {qty}股 @ {px}"))
        for d, a, s, sd in q(
                "SELECT date,account,symbol,side FROM signals ORDER BY date DESC LIMIT 5"):
            events.append((d, f"📶 {ACCT_CN.get(a,a)} {sym_name(s)} {sd}信号"))
        for d, a, s, sd, dc in q(
                "SELECT date,account,symbol,side,decision FROM approvals ORDER BY updated DESC LIMIT 5"):
            icon = "✅" if dc == "approved" else "⏭"
            events.append((d, f"{icon} {ACCT_CN.get(a,a)} {sym_name(s)} {sd} {dc}"))
        events.sort(reverse=True)
        for d, txt in events[:10]:
            st.write(f"`{d}` {txt}")
        if not events:
            st.write("暂无事件")

# ================================================================
# 📈 行情与信号：实时行情 + 策略规则 + 触发线
# ================================================================
elif tab == "🔍 选股":
    st.header("🔍 选股")
    # ---- 筛选器（Unusual Whales式）：涨幅/量比/RSI/MACD/资金 ----
    with st.expander("🎯 条件筛选", expanded=False):
        f1, f2, f3 = st.columns(3)
        with f1:
            chg_min = st.number_input("涨幅% ≥", value=-10.0, step=1.0, key="f_chg_min")
            chg_max = st.number_input("涨幅% ≤", value=10.0, step=1.0, key="f_chg_max")
        with f2:
            rsi_min = st.number_input("RSI ≥", value=0, step=5, key="f_rsi_min")
            rsi_max = st.number_input("RSI ≤", value=100, step=5, key="f_rsi_max")
        with f3:
            macd_f = st.selectbox("MACD", ["不限", "多头", "空头"], key="f_macd")
        if st.button("🔍 筛选", key="do_screen"):
            from engine.indicators import rsi, macd
            results = []
            for sym in SYMBOL_CN.keys():
                bars = q("SELECT date,open,high,low,close,volume FROM market_data WHERE symbol=? ORDER BY date", (sym,))
                if len(bars) < 30:
                    continue
                closes = [r[4] for r in bars]
                # 涨幅
                if len(closes) >= 2 and closes[-2]:
                    chg = (closes[-1] - closes[-2]) / closes[-2] * 100
                else:
                    continue
                if not (chg_min <= chg <= chg_max):
                    continue
                # RSI
                rsi_v = rsi(closes)[-1]
                if rsi_v is None or not (rsi_min <= rsi_v <= rsi_max):
                    continue
                # MACD
                _, _, hist = macd(closes)
                is_bull = hist[-1] and hist[-1] > 0
                if macd_f == "多头" and not is_bull:
                    continue
                if macd_f == "空头" and is_bull:
                    continue
                results.append((sym, chg, rsi_v, "多" if is_bull else "空", closes[-1]))
            if results:
                st.write(f"命中 {len(results)} 只：")
                for sym, chg, rsi_v, macd_s, px in sorted(results, key=lambda x: -abs(x[1])):
                    color = "#f6465d" if chg >= 0 else "#2ebd85"
                    st.markdown(f"<span style='color:{color};'>{sym} {SYMBOL_CN.get(sym,'')} "
                                f"{chg:+.2f}%</span> RSI {rsi_v:.0f} MACD{macd_s} @{px:.2f}",
                                unsafe_allow_html=True)
            else:
                st.info("无符合条件的标的")
    # ---- 全市场搜索：任意A股/港股代码，新浪实时K线 ----
    with st.expander("🔎 全市场搜索（任意代码）", expanded=False):
        s_code = st.text_input("输入代码，如 600519 / 002446 / 00700", key="mkt_search")
        if s_code:
            code = s_code.strip()
            # 判断市场前缀
            if code.startswith("HK") or (len(code) == 5 and code.isdigit()):
                sina_sym = f"hk{code[-5:]}"
                st.caption("港股：新浪静态K线不支持，跳转查看")
                st.markdown(f"[新浪港股行情](https://stock.finance.sina.com.cn/hkstock/quotes/{code[-5:]}.html) | "
                            f"[东方财富](https://quote.eastmoney.com/hk/{code[-5:]}.html)")
            elif len(code) == 6 and code.isdigit():
                prefix = "sh" if code[0] in "68" else "sz"
                sina_sym = f"{prefix}{code}"
                # 新浪K线图：日K/周K/分时切换
                ktype = st.radio("周期", ["日K", "周K", "分时"], horizontal=True, key="sina_ktype")
                kmap = {"日K": "daily", "周K": "weekly", "分时": "min"}
                import time
                img_url = f"https://image2.sinajs.cn/newchart/{kmap[ktype]}/n/{sina_sym}.gif?t={int(time.time())}"
                st.image(img_url, use_container_width=True)
                st.caption(f"数据来源：新浪财经 · {code}")
            else:
                st.warning("请输入6位A股代码或港股代码")
    st.caption("市场面 + 技术形态")
    from engine.indicators import rsi, macd, bollinger, ma_align, adx as calc_adx, market_regime
    from engine.signals import moving_average
    # ---- 市场面：涨跌分布 ----
    st.subheader("📊 涨跌分布")
    adv, dec, flat = 0, 0, 0
    for sym in SYMBOL_CN:
        rows = q("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 2", (sym,))
        if len(rows) == 2 and rows[0][0] and rows[1][0]:
            chg = (rows[0][0] - rows[1][0]) / rows[1][0]
            if chg > 0.001: adv += 1
            elif chg < -0.001: dec += 1
            else: flat += 1
    c1, c2, c3 = st.columns(3)
    c1.metric("上涨", adv)
    c2.metric("下跌", dec)
    c3.metric("持平", flat)
    # ---- 持仓资讯：每个标的新闻直达 ----
    with st.expander("📰 持仓资讯", expanded=False):
        for sym in sorted(SYMBOL_CN.keys()):
            nm = sym_name(sym)
            if len(sym) == 6 and sym.isdigit():
                # A股：新浪 + 东方财富新闻
                st.markdown(f"**{nm}** · "
                            f"[新浪](https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php?symbol={sym}) · "
                            f"[东财](https://quote.eastmoney.com/concept/{sym}.html?from=classic)")
            elif sym.startswith("HK"):
                code5 = sym[2:]
                st.markdown(f"**{nm}** · "
                            f"[新浪港股](https://stock.finance.sina.com.cn/hkstock/quotes/{code5}.html) · "
                            f"[东财](https://quote.eastmoney.com/hk/{code5}.html)")
            elif sym == "BTCUSDT":
                st.markdown(f"**{nm}** · [金色财经](https://www.jinse.cn) · [币世界](https://www.bishijie.com)")
    # ---- 技术形态：每个标的指标面板 ----
    st.subheader("📐 技术形态")
    import time as _time
    _ts = int(_time.time())
    for sym in sorted(SYMBOL_CN.keys()):
        bars = q("SELECT date,open,high,low,close,volume FROM market_data WHERE symbol=? ORDER BY date", (sym,))
        if len(bars) < 30:
            continue
        closes = [r[4] for r in bars]
        highs = [r[2] for r in bars]
        lows = [r[3] for r in bars]
        last_c = closes[-1]
        # 指标（只算标签，不堆数字）
        rsi_v = rsi(closes)[-1]
        dif, dea, hist = macd(closes)
        macd_h = hist[-1]
        upper, mid, lower = bollinger(closes)
        adx_v = calc_adx(highs, lows, closes)[-1]
        regime = market_regime(adx_v)
        align = ma_align(closes)
        tags = []
        if rsi_v and rsi_v > 70: tags.append("⚠️超买")
        elif rsi_v and rsi_v < 30: tags.append("💡超卖")
        tags.append("🟢MACD多" if (macd_h and macd_h > 0) else "🔴MACD空")
        if regime == "trend": tags.append("📈趋势")
        elif regime == "range": tags.append("📊震荡")
        if align > 0.5: tags.append("✅均线多")
        elif align < -0.5: tags.append("❌均线空")
        if last_c and upper[-1] and last_c > upper[-1]: tags.append("🔥破上轨")
        if last_c and lower[-1] and last_c < lower[-1]: tags.append("🧊破下轨")
        with st.expander(f"{sym_name(sym)} @ {last_c:.2f} | {' '.join(tags)}", expanded=False):
            # 专业K线（Lightweight Charts，多均线+成交量）
            from engine.tv_charts import kline_html
            html = kline_html(sym, bars, height=380)
            st.components.v1.html(html, height=400, scrolling=False)

elif tab == "📝 交易":
    st.markdown('<div class="section-head">📝 交易执行</div>', unsafe_allow_html=True)
    # 页内账户选择
    acct = st.radio("账户", accts, format_func=lambda x: ACCT_CN.get(x, x),
                    horizontal=True, key="trade_acct")

    # 待建条件单
    st.subheader("待建条件单")
    sigs = q("""SELECT s.date, s.symbol, s.side, s.price FROM signals s
                LEFT JOIN fills f ON s.date=f.signal_date AND s.account=f.account
                  AND s.symbol=f.symbol AND s.side=f.side
                WHERE s.account=? AND f.id IS NULL
                  AND s.date >= date('now','-7 days')
                ORDER BY s.date DESC, s.symbol LIMIT 20""", (acct,))
    if not sigs:
        st.write("近7天无待执行信号")
    else:
        from engine.order_validate import validate_order
        # 顶部操作栏：全选 + 复制（仅校验通过的订单可勾选）
        if "order_select_all" not in st.session_state:
            st.session_state.order_select_all = False
        # 预校验所有信号
        validated = []
        for d, s, sd, px in sigs:
            # SELL用当前持仓做数量，BUY暂估1手用于校验框架
            qty_est = None
            if sd == "SELL":
                pos = q("SELECT qty FROM positions WHERE account=? AND symbol=?", (acct, s))
                qty_est = pos[0][0] if pos else 0
            ok, errs, warns = validate_order(acct, s, sd, qty_est, px, d)
            validated.append((d, s, sd, px, ok, errs, warns))
        # 拦截：有校验失败的先提示
        failed = [v for v in validated if not v[4]]
        if failed:
            st.warning(f"⛔ {len(failed)} 条信号校验未通过，已拦截（不生成订单指引）")
            for d, s, sd, px, ok, errs, warns in failed:
                st.caption(f"{sym_name(s)} {sd} @{px} ({d}): {'; '.join(errs)}")
        valid_sigs = [v for v in validated if v[4]]
        if not valid_sigs:
            st.info("无可执行订单")
        else:
            # 顶部操作栏：全选 + 复制
            if "order_select_all" not in st.session_state:
                st.session_state.order_select_all = False
            top1, top2 = st.columns([1, 4])
            with top1:
                select_all = st.checkbox("全选", value=st.session_state.order_select_all,
                                         key="select_all_orders")
                st.session_state.order_select_all = select_all
            selected = []
            # 卡片列表：每行一个订单卡片，checkbox 在卡片内
            for d, s, sd, px, ok, errs, warns in valid_sigs:
                direction = "买入" if sd == "BUY" else "卖出"
            color = "#f6465d" if sd == "BUY" else "#2ebd85"
            nm = sym_name(s)
            key = f"order_{d}_{s}_{sd}"
            checked = st.checkbox(f"{nm} · {direction} · {px:.2f} · {d}",
                                  value=select_all, key=key)
            if checked:
                selected.append((s, direction, px, nm))
        with top2:
            if selected:
                if st.button(f"📋 复制 {len(selected)} 条", key="copy_selected"):
                    txt = "\n".join(f"{s} {d} 限价{px:.2f}" for s, d, px, _ in selected)
                    st.code(txt, language=None)
            else:
                st.caption("勾选后批量复制")
        st.divider()
        # 逐条建单/跳过
        for d, s, sd, px, ok, errs, warns in valid_sigs:
            direction = "买入" if sd == "BUY" else "卖出"
            color = "#f6465d" if sd == "BUY" else "#2ebd85"
            nm = sym_name(s)
            c1, c2, c3 = st.columns([3, 1, 1])
            with c1:
                st.markdown(f'<div style="padding:8px 0;"><b>{nm}</b> '
                            f'<span style="color:{color};font-weight:bold;">{direction}</span> '
                            f'<span class="num">{px:.2f}</span> '
                            f'<span style="color:#666;font-size:12px;">{d}</span></div>',
                            unsafe_allow_html=True)
            with c2:
                if st.button("✅ 已建单", key=f"done_{d}_{s}_{sd}"):
                    st.success("已标记")
            with c3:
                if st.button("⏭ 跳过", key=f"skip_{d}_{s}_{sd}"):
                    st.info("已跳过")

    # 券商回执录入（四态按钮反馈：已提交→执行中→已生效/失败）
    st.subheader("🧾 券商回执录入")
    if not IS_CLOUD:
        # 按钮状态机：session_state 持久化上次操作结果
        if "receipt_state" not in st.session_state:
            st.session_state.receipt_state = None  # None/已提交/执行中/已生效/失败
        rs = st.session_state.receipt_state
        if rs:
            badge_cls = {"已提交": "btn-submitted", "执行中": "btn-running",
                         "已生效": "btn-done", "失败": "btn-failed"}.get(rs, "btn-submitted")
            st.markdown(f'<span class="btn-state {badge_cls}">上次操作：{rs}</span>',
                        unsafe_allow_html=True)
        with st.form("receipt_form_exec"):
            r_sym = st.text_input("代码（如 002446）", key="rx_sym")
            r_side = st.selectbox("方向", ["买入", "卖出"], key="rx_side")
            r_price = st.number_input("成交价", min_value=0.0, format="%.4f", key="rx_price")
            r_qty = st.number_input("数量（股）", min_value=0, step=100, key="rx_qty")
            r_date = st.date_input("成交日期", value=datetime.now().date(), key="rx_date")
            submitted = st.form_submit_button("录入回执")
            if submitted:
                st.session_state.receipt_state = "已提交"
                if not (r_sym and r_price > 0 and r_qty > 0):
                    st.session_state.receipt_state = "失败"
                    st.markdown('<span class="btn-state btn-failed">失败：请填写完整的代码、价格和数量</span>',
                                unsafe_allow_html=True)
                else:
                    st.session_state.receipt_state = "执行中"
                    try:
                        con = sqlite3.connect(DB)
                        con.execute("""INSERT OR REPLACE INTO broker_receipts
                                       (date, account, symbol, side, price, qty, created_at)
                                       VALUES(?,?,?,?,?,?,?)""",
                                    (r_date.strftime("%Y-%m-%d"), acct, r_sym,
                                     "BUY" if r_side == "买入" else "SELL",
                                     r_price, r_qty,
                                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        con.commit()
                        # 校验写入确实生效
                        chk = con.execute(
                            "SELECT COUNT(*) FROM broker_receipts WHERE date=? AND account=? AND symbol=? AND price=? AND qty=?",
                            (r_date.strftime("%Y-%m-%d"), acct, r_sym, r_price, r_qty)).fetchone()[0]
                        con.close()
                        if chk > 0:
                            st.session_state.receipt_state = "已生效"
                            st.markdown(f'<span class="btn-state btn-done">已生效：{r_sym} {r_side} {r_qty}股 @ {r_price}</span>',
                                        unsafe_allow_html=True)
                        else:
                            st.session_state.receipt_state = "失败"
                            st.markdown('<span class="btn-state btn-failed">失败：写入后校验未找到记录</span>',
                                        unsafe_allow_html=True)
                    except Exception as e:
                        st.session_state.receipt_state = "失败"
                        st.markdown(f'<span class="btn-state btn-failed">失败：{str(e)[:200]}</span>',
                                    unsafe_allow_html=True)

    # 已录入回执
    st.subheader("已录入回执")
    receipts = q("""SELECT date, symbol, side, price, qty FROM broker_receipts
                    WHERE account=? ORDER BY date DESC LIMIT 20""", (acct,))
    if receipts:
        st.table([{"日期": d, "代码": sym_name(s), "方向": "买入" if sd == "BUY" else "卖出",
                   "成交价": px, "数量": qty}
                  for d, s, sd, px, qty in receipts])
    else:
        st.write("暂无回执记录")

# ================================================================
# 💰 持仓与账本：持仓 + 成交 + 净值
# ================================================================
elif tab == "💰 账户":
    st.markdown('<div class="section-head">💰 持仓与账本</div>', unsafe_allow_html=True)

    # 账户切换（标签式）
    acct_choice = st.radio("账户", ["A股", "港股", "加密货币"], horizontal=True, key="acct_tab")
    acct_map = {"A股": "sim_a", "港股": "sim_hk", "加密货币": "sim_crypto"}
    acct = acct_map[acct_choice]

    # 导出对账单
    if st.button("📥 导出对账单CSV", key="export_stmt"):
        import csv, io
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["日期", "账户", "标的", "方向", "数量", "价格", "费用", "盈亏"])
        for r in q("SELECT date,account,symbol,side,qty,price,fee,pnl FROM fills WHERE account=? ORDER BY date DESC LIMIT 500", (acct,)):
            w.writerow(r)
        st.download_button("下载CSV", out.getvalue(), f"对账单_{acct}.csv", "text/csv", key="dl_stmt")

    # 持仓
    st.subheader("当前持仓")
    pos = q("SELECT symbol,qty,avg_cost,last_price,market_value FROM positions WHERE account=? AND qty>0", (acct,))
    if pos:
        # 持仓浮盈亏明细
        rows = []
        for s, qty, avg, last, mv in pos:
            upl = (last - avg) * qty if last and avg else 0
            upl_pct = (last - avg) / avg * 100 if last and avg else 0
            rows.append({"标的": sym_name(s), "数量": qty, "均价": f"{avg:.2f}", "现价": f"{last:.2f}",
                         "市值": f"{mv:,.2f}",
                         "浮盈亏": f"{upl:+,.0f} ({upl_pct:+.1f}%)"})
        st.table(rows)

        # TradingView 级 K线（每个持仓标的）
        st.subheader("持仓K线")
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
                st.write(f"**{sym_name(s)}** 成本 {avg:.2f} · 现价 {last:.2f} · "
                         f"浮盈亏 {upl:+,.0f} ({upl_pct:+.1f}%)")
                st.components.v1.html(
                    kline_html(f"{acct}_{s}", bars, price_lines=pls,
                               markers=marks, height=420),
                    height=430)
            else:
                st.write(f"**{sym_name(s)}**：数据不足（{len(bars)}根）")
    else:
        st.write("空仓")

    # 近期成交
    st.subheader("近期成交")
    fl = q("SELECT date,symbol,side,qty,price,fee,pnl FROM fills WHERE account=? ORDER BY date DESC LIMIT 20", (acct,))
    if fl:
        st.table([{"日期": d, "标的": sym_name(s), "方向": sd, "数量": qty, "成交价": px, "费用": fee, "盈亏": pnl}
                  for d, s, sd, qty, px, fee, pnl in fl])
    else:
        st.write("暂无成交")

# ================================================================
# 🛡 验证与风控：KPI 验证 + 风控指标 + 审计
# ================================================================
# ================================================================
# 🛡 验证与风控：验证 KPI + 风控指标 + 审计
# ================================================================
elif tab == "🛡 验证":
    st.markdown('<div class="section-head">🛡 验证与风控</div>', unsafe_allow_html=True)

    # ---- 验证 KPI 卡片（四态徽标，阻断原因优先） ----
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

        rt = d.get("round_trips") or 0
        # 阻断原因优先展示
        blockers = []
        if rt < tg["sample_min"]:
            blockers.append(f"样本不足：当前 {rt} 笔，需要 {tg['sample_min']}-{tg['sample_max']} 笔")
        if blockers:
            for b in blockers:
                st.markdown(f'<div class="alert-banner warn">⛔ 阻断：{b}</div>',
                            unsafe_allow_html=True)

        k1, k2, k3 = st.columns(3)
        with k1:
            stt = _vs_state(rt, tg["sample_min"], True) if rt >= tg["sample_min"] else "insufficient"
            if rt > tg["sample_max"]:
                stt = "fail"
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">样本（round-trip） {vs_badge(stt)}</div>'
                        f'<div class="kpi-value">{rt} 笔</div>'
                        f'<div class="kpi-label">目标 {tg["sample_min"]}-{tg["sample_max"]} 笔</div></div>',
                        unsafe_allow_html=True)
        with k2:
            wlr = d.get("win_loss_ratio")
            stt = _vs_state(wlr, tg["win_loss_ratio"], True, rt, tg["sample_min"])
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">盈亏比（平均盈利/平均亏损） {vs_badge(stt)}</div>'
                        f'<div class="kpi-value">{wlr:.2f}' if wlr is not None else
                        f'<div class="kpi-card"><div class="kpi-label">盈亏比 {vs_badge(stt)}</div>'
                        f'<div class="kpi-value">—',
                        unsafe_allow_html=True)
            if wlr is not None:
                st.markdown(f'</div><div class="kpi-label">硬线 ≥ {tg["win_loss_ratio"]}</div></div>',
                            unsafe_allow_html=True)
        with k3:
            pf = d.get("profit_factor")
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">盈利因子 PF（参考）</div>'
                        f'<div class="kpi-value">{pf:.2f}' if pf is not None else
                        f'<div class="kpi-card"><div class="kpi-label">盈利因子 PF（参考）</div>'
                        f'<div class="kpi-value">—',
                        unsafe_allow_html=True)
            st.markdown('</div></div>', unsafe_allow_html=True)

        k4, k5, k6 = st.columns(3)
        with k4:
            mdd = d.get("max_drawdown")
            stt = _vs_state(mdd, tg["max_drawdown"], False, rt, tg["sample_min"])
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">最大回撤 {vs_badge(stt)}</div>'
                        f'<div class="kpi-value">{mdd*100:.2f}%' if mdd is not None else
                        f'<div class="kpi-card"><div class="kpi-label">最大回撤 {vs_badge(stt)}</div>'
                        f'<div class="kpi-value">—',
                        unsafe_allow_html=True)
            if mdd is not None:
                st.markdown(f'</div><div class="kpi-label">硬线 ≤ {tg["max_drawdown"]*100:.0f}%</div></div>',
                            unsafe_allow_html=True)
        with k5:
            exp = d.get("expectancy")
            stt = _vs_state(exp, 0, True, rt, tg["sample_min"])
            # 期望值硬线是 >0，用 >=0 近似，=0 算未达标
            if exp is not None and exp <= 0:
                stt = "fail" if not (rt < tg["sample_min"]) else "insufficient"
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">期望值 {vs_badge(stt)}</div>'
                        f'<div class="kpi-value">{exp:.2f}' if exp is not None else
                        f'<div class="kpi-card"><div class="kpi-label">期望值 {vs_badge(stt)}</div>'
                        f'<div class="kpi-value">—',
                        unsafe_allow_html=True)
            if exp is not None:
                st.markdown('</div><div class="kpi-label">硬线 > 0</div></div>',
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
        hdr = ["指标"] + [f"\1{ACCT_CN.get(aid, aid)}\2" for aid in ["sim_a", "sim_hk", "sim_crypto"]]
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
# ⚙ 系统与审计：数据源 / 规则版本 / 指令日志
# ================================================================
