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


@st.cache_data(ttl=600, show_spinner=False)
def _fetch_ns_flow():
    """互联互通资金（东方财富 datacenter，单位：百万元）。
    净买额自 2024-08 起停止披露，改用成交额 + 北向持股市值。
    返回 {north_amt, north_date, north_prev, south_amt, south_date, south_prev, north_hold}，失败为 {}。"""
    import json as _json
    import urllib.request as _ur
    try:
        base = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
                "?reportName=RPT_MUTUAL_DEAL_HISTORY"
                "&columns=TRADE_DATE,MUTUAL_TYPE,DEAL_AMT"
                "&sortColumns=TRADE_DATE&sortTypes=-1&pageSize=6&pageNumber=1"
                "&filter=(MUTUAL_TYPE%3D%22{mt}%22)&source=WEB&client=WEB")
        per = {}
        for mt in ("001", "002", "003", "004"):
            req = _ur.Request(base.format(mt=mt),
                              headers={"User-Agent": "Mozilla/5.0"})
            with _ur.urlopen(req, timeout=12) as r:
                data = _json.loads(r.read().decode("utf-8"))
            per[mt] = {row["TRADE_DATE"][:10]: (row.get("DEAL_AMT") or 0)
                       for row in (data.get("result") or {}).get("data") or []}

        def _series(mts):
            dates = sorted(set().union(*[set(per[m]) for m in mts]), reverse=True)
            out = []
            for d in dates:
                if all(d in per[m] for m in mts):
                    out.append((d, sum(per[m][d] for m in mts) / 100.0))  # 百万元→亿元
                if len(out) >= 2:
                    break
            return out

        north, south = _series(("001", "002")), _series(("003", "004"))
        hold = None
        try:
            req = _ur.Request(
                "https://push2his.eastmoney.com/api/qt/kamt.kline/get"
                "?fields1=f1,f3,f5&fields2=f51,f52&klt=101&lmt=1"
                "&ut=b2884a393a59ad64002292a3e90d46a5",
                headers={"User-Agent": "Mozilla/5.0"})
            with _ur.urlopen(req, timeout=12) as r:
                kd = _json.loads(r.read().decode("utf-8")).get("data") or {}
            s2n = (kd.get("s2n") or [None])[-1]
            if s2n:
                hold = float(str(s2n).split(",")[3]) / 1e8  # 万元→万亿元
        except Exception:
            hold = None
        if not north and not south:
            return {}
        return {
            "north_amt": north[0][1] if north else None,
            "north_date": north[0][0] if north else "",
            "north_prev": north[1][1] if len(north) > 1 else None,
            "south_amt": south[0][1] if south else None,
            "south_date": south[0][0] if south else "",
            "south_prev": south[1][1] if len(south) > 1 else None,
            "north_hold": hold,
        }
    except Exception:
        return {}


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
    "disabled": "已停用",
}


def strategy_of(aid):
    """分账户策略（2026-10-16 用户选A），回退全局。"""
    sig = CFG.get("signals", {})
    return sig.get("per_account", {}).get(aid) or sig.get("strategy", "ma20")


def strategy_desc(s):
    if s == "buyback":
        return "20日高点回撤10%买入，+15%止盈 / 策略止损-5%"
    if s == "disabled":
        return "已停用，不生成新信号"
    return "收盘站上20日线买入，跌破20日线卖出"


def q(sql, args=()):
    con = sqlite3.connect(DB)
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


@st.cache_data(ttl=600, show_spinner=False)
def _screen_cached(db_path, day_str):
    """多重校验选股评分（engine.screen，母板16只）；按日缓存。"""
    from engine.screen import screen_all
    return screen_all(db_path, day_str)


@st.cache_data(ttl=600, show_spinner=False)
def _sector_news_cached(day_str):
    """板块新闻面（engine.news_sentiment 全部板块情绪）；按日缓存。"""
    from engine.news_sentiment import all_sector_sentiments
    return all_sector_sentiments(day_str)


def _topboard_data(db_path):
    """TOP标的榜数据：最近一次 screen_runs 的 results_json（评分全集，
    母板观察池口径）按总分降序取 TOP8，合并 screen_perf 样本外验证聚合。

    验证列口径（按 symbol 聚合 screen_perf 的 sa 组，如实统计、不插值）：
    只计正常入选样本（grp='sa'）——影子组（被硬闸门封顶）与 other 组
    不混入本列，各组分开统计（补齐②b，2026-10-27）。历史行 grp 为空时
    按 tools/screen_perf.py 的 _group_of 口径推导组别（未封顶且评级
    S/A → sa；封顶 → shadow），与现行登记逻辑一致。
    n = 该标的 fwd5_ret 非空条数（已满 5 日的 sa 样本数）；
    5日平均前向收益 = AVG(fwd5_ret)，20日平均 = AVG(fwd20_ret 非空部分)，
    5日胜率 = fwd5_ret>0 的样本占比。表缺失/无样本 → 聚合为空，
    页面对 n<5 一律标注「未验证·积累中」，不呈现任何估算数字。
    返回 (asof, rule_version, rows)；无落库记录时 rows 为空。"""
    import json as _json
    try:
        con = sqlite3.connect(db_path)
        run = con.execute(
            "SELECT asof, rule_version, results_json FROM screen_runs"
            " ORDER BY id DESC LIMIT 1").fetchone()
        perf = {}
        try:
            for sym, n5, a5, a20, win5 in con.execute(
                    "SELECT symbol, COUNT(fwd5_ret), AVG(fwd5_ret),"
                    " AVG(fwd20_ret),"
                    " AVG(CASE WHEN fwd5_ret IS NULL THEN NULL"
                    "          WHEN fwd5_ret > 0 THEN 1.0 ELSE 0.0 END)"
                    " FROM screen_perf"
                    " WHERE grp='sa'"
                    "    OR (COALESCE(grp,'')='' AND COALESCE(capped,0)=0"
                    "        AND grade IN ('S','A'))"
                    " GROUP BY symbol"):
                perf[sym] = {"n": n5 or 0, "avg5": a5, "avg20": a20,
                             "win5": win5}
        except sqlite3.OperationalError:
            perf = {}  # screen_perf 尚未建表：全部按积累中呈现
        con.close()
    except sqlite3.OperationalError:
        return None, None, []  # screen_runs 尚未落库
    if not run:
        return None, None, []
    try:
        rows = _json.loads(run[2])
    except (TypeError, ValueError):
        return run[0], run[1], []
    rows = sorted(rows, key=lambda r: (-(r.get("total") or 0),
                                       str(r.get("symbol") or "")))[:8]
    for r in rows:
        r["_perf"] = perf.get(r.get("symbol"),
                               {"n": 0, "avg5": None, "avg20": None,
                                "win5": None})
    return run[0], run[1], rows


def get_control():
    r = q("SELECT status, mode FROM control WHERE id=1")
    return r[0] if r else ("running", "auto")


def check_system_health():
    """系统健康检查：返回 (blocked: bool, reasons: list[str])
    2026-10-18起：发现阻断性问题时顶部状态必须显示阻断，不得显示"正常运行"。
    2026-10-25（P0-4）：新增三项新鲜度硬检查——行情滞后、新闻面断更、
    母板映射过期，任一触发即阻断，不再静默用旧数据冒充正常。"""
    reasons = []
    # 1. SELL信号无持仓
    rows = q("""SELECT s.date, s.account, s.symbol FROM signals s
                LEFT JOIN positions p ON p.account=s.account AND p.symbol=s.symbol
                WHERE s.side='SELL' AND s.status='待执行'
                  AND (p.qty IS NULL OR p.qty <= 0)""")
    _ACCT_CN = {"sim_a": "A股", "sim_hk": "港股", "sim_crypto": "加密货币"}
    for d, a, sym in rows:
        reasons.append(f"SELL信号无持仓: {_ACCT_CN.get(a, a)} {sym} ({d})")
    # 2. 待执行信号中有已过期的
    rows = q("SELECT COUNT(*) FROM signals WHERE status='已过期'")
    if rows and rows[0][0] > 0:
        reasons.append(f"{rows[0][0]}个信号已过期未处理")
    # 3. 新鲜度硬检查（P0-4）：三重数据来源任一过期/缺失即阻断
    from datetime import date as _date
    _today = _date.today()
    try:
        # 3a. 行情：A/港股最新行情距今 >10 天（阈值取在最长正常休市
        # ——春节/国庆连休含周末约 ≤10 天——之外，再久必是断更）
        r = q("SELECT MAX(date) FROM market_data WHERE symbol NOT LIKE '%USDT'")
        _md = r[0][0] if r and r[0][0] else None
        if not _md:
            reasons.append("行情数据缺失：market_data 为空")
        elif (_today - _date.fromisoformat(str(_md)[:10])).days > 10:
            reasons.append(f"行情滞后：最新行情 {_md} 距今超 10 天，疑似断更")
        # 3b. 新闻面：sector_news 最新证据 >7 天或文件缺失 = 断更
        from engine.news_sentiment import news_freshness
        _nf = news_freshness(_today)
        if _nf["missing"]:
            reasons.append("新闻面断更：sector_news.json 缺失或无条目，新闻分不可信")
        elif _nf["stale"]:
            reasons.append(f"新闻面断更：最新证据 {_nf['latest']} 距今 {_nf['age_days']} 天>7 天，新闻分不可信")
        # 3c. 母板映射：generated_at 早于母板 xlsx 的 mtime = 映射过期未重跑
        import json as _json
        _map_p = os.path.join(BASE, "config", "workbook_map.json")
        _xlsx_p = os.path.expanduser("~/workspace/your_files/交易母版V8.2.1-Final.xlsx")
        with open(_map_p, encoding="utf-8") as _f:
            _gen = str(_json.load(_f).get("generated_at") or "")
        if not _gen:
            reasons.append("母板映射无版本信息（generated_at 缺失），请重跑 build_workbook_map")
        elif os.path.exists(_xlsx_p):
            _gen_d = _date.fromisoformat(_gen[:10])
            _x_d = _date.fromtimestamp(os.path.getmtime(_xlsx_p))
            if _gen_d < _x_d:
                reasons.append(f"母板映射过期：映射生成于 {_gen[:10]}，母板文件已于 {_x_d} 更新，请重跑 build_workbook_map")
        # 3d. 市场环境数据可用性（模块A，fail-closed）：母板环境字段
        # （B31/放行/B79）读取失败、映射缺失或过期 → 环境态降级，
        # 顶部 blocked 明示已按安全规则禁止新开仓。
        from engine.regime import load_market_env as _load_env
        _env_h = _load_env(db_path=DB)
        if _env_h.get("state") == "degraded":
            reasons.append("市场环境数据不可用，已安全降级为禁止新开仓"
                           f"（{_env_h.get('degraded_reason') or '未知原因'}；"
                           "请重跑 build_workbook_map 或检查母板文件）")
    except Exception as _e:
        reasons.append(f"新鲜度检查异常: {_e}")
    return (len(reasons) > 0, reasons)


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
.strategy-badge.disabled { background: rgba(138,143,156,.15); color: #8a8f9c; }
/* 左侧栏已移除：导航与控制移至顶部 */
section[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display: none !important; }
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
    # 区分：数据日期（交易日）vs 同步时间（文件推送时间）
    # 周末无新数据时，数据日期停在周五属正常，不应误报为同步中断
    data_date = q("SELECT MAX(date) FROM daily_report WHERE symbol='TOTAL'")
    data_date_str = data_date[0][0] if data_date and data_date[0][0] else ""
    if data_date_str:
        st.caption(f"数据日期 {data_date_str} · 同步于 {CLOUD_DATA_ASOF}")
    else:
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
    # DB 快照日期判定（快照永不标为实时：DB 是日批快照，只标数据日期；
    # “实时”仅指行情源直拉的盘中报价区，且须带 ●实时 标识）
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

def _ap_state(key, decision, d, acct, s, sd):
    """审批按钮四态：已提交→执行中→已生效/失败，写入后校验"""
    sk = f"apstate_{key}"
    st.session_state[sk] = "已提交"
    st.session_state[sk] = "执行中"
    try:
        con = sqlite3.connect(DB)
        con.execute(
            "INSERT OR REPLACE INTO approvals(date,account,symbol,side,decision,updated)"
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
            # BUY 开仓 @价格 | 策略止损 @价格（signals.stop_price 为 ATR 策略
            # 参数，P1-5 更名，与母板止损参考 stop_ref 区分，不得混称“止损”）
            stop_s = f" | 策略止损 @{stop_px:.2f}" if stop_px else ""
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
        if regime == "trend": tags.append("趋势")
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
        if sym == "IDX000001":
            continue  # 上证指数是环境闸数据（display-only），不进行情条
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
blocked, block_reasons = check_system_health()

top1, top2, top3 = st.columns(3)
with top1:
    if blocked:
        st.markdown(f'<div class="kpi-card" style="border:1px solid #ff4d4f;"><div class="kpi-label">系统状态</div>'
                    f'<div class="kpi-value" style="color:#ff4d4f;">⛔ 阻断</div></div>',
                    unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="kpi-card"><div class="kpi-label">系统状态</div>'
                    f'<div class="kpi-value">{"🟢 运行中" if status == "running" else "⏸ 已暂停"}</div></div>',
                    unsafe_allow_html=True)
with top2:
    st.markdown(f'<div class="kpi-card"><div class="kpi-label">执行模式</div>'
                f'<div class="kpi-value">{"⚡ 自动" if mode == "auto" else "✋ 手动"}</div></div>',
                unsafe_allow_html=True)
with top3:
    # 版本号简化显示：主版本大字，生效日期小字（完整版本串仍用于毕业样本隔离）
    _vmain, _, _vdate = str(ver).partition("-")
    _vsub = f'<div class="kpi-label">{_vdate} 起生效</div>' if _vdate else ""
    st.markdown(f'<div class="kpi-card"><div class="kpi-label">策略版本</div>'
                f'<div class="kpi-value" style="font-size:16px">{_vmain}</div>{_vsub}</div>',
                unsafe_allow_html=True)
# 账户策略：默认折叠隐藏（2026-10-25 用户要求），顶栏只留三张等高卡，保持简洁
_ACCT = {"sim_a": "A股", "sim_hk": "港股", "sim_crypto": "加密货币"}
_rows = []
for a in CFG["accounts"]:
    _sid = strategy_of(a["id"])
    _sname = STRATEGY_CN.get(_sid, _sid)
    if _sid == "disabled":
        _badge = f'<span class="strategy-badge disabled">{_ACCT.get(a["id"], a["id"])} · 已停用</span>'
        _desc = "不生成新信号 · 持仓与净值照常跟踪 · 策略待重做"
    else:
        _badge = (f'<span class="strategy-badge {_sid}">{_ACCT.get(a["id"], a["id"])} · '
                  f'{_sname} · 运行中</span>')
        _desc = strategy_desc(_sid)
    _rows.append(
        f'<div style="margin-top:6px;">{_badge}'
        f'<div style="color:#8a8f9c;font-size:12px;margin-top:2px;">{_desc}</div></div>')
with st.expander("账户策略", expanded=False):
    st.markdown("".join(_rows), unsafe_allow_html=True)

# 阻断原因展示（2026-10-18起：有阻断时必须清晰显示）
if blocked:
    st.error("⛔ 系统阻断：" + "；".join(block_reasons[:5]))

# 数据健康四态指示（安全补充③，2026-10-30）：由各市场最新 bar 日期
# 与参照日期的真实差距判定（engine.data_health），非正常态带数据
# 时间与失效标注；后端冻结由 pipeline/regime 强制，本行只是同步显示。
try:
    from engine import data_health as _dh
    _ref_stock = q("SELECT MAX(date) FROM market_data"
                   " WHERE market IN ('a','hk')")[0][0]
    _today = datetime.now().strftime("%Y-%m-%d")
    _hparts = []
    for _mkt, _label in (("a", "A股"), ("hk", "港股"), ("crypto", "加密")):
        _last = q("SELECT MAX(date) FROM market_data WHERE market=?",
                  (_mkt,))[0][0]
        _ref = _today if _mkt == "crypto" else _ref_stock
        _state, _reason = _dh.classify(last_bar_date=_last, ref_date=_ref)
        _icon = {"normal": "🟢", "delayed": "🟡",
                 "disconnected": "🔴", "anomalous": "🔴"}[_state]
        _txt = "%s %s %s" % (_icon, _label, _dh.LABELS[_state])
        if _state != _dh.NORMAL:
            _txt += "（%s）" % _dh.label_stale(_state, _last)
        _hparts.append(_txt)
    st.caption("数据健康：" + " · ".join(_hparts))
except Exception:
    st.caption("数据健康：未知（判定失败，不影响后端冻结口径）")

# ============ 导航（顶部横排；左侧栏已移除） ============
tab = st.radio("板块", ["🏠 工作台", "🔍 选股", "🏦 机构集体建仓监控", "📝 交易", "💰 账户", "🛡 验证"],
               horizontal=True, label_visibility="collapsed", key="main_nav")

# 控制（本地模式可用；云端只读时不渲染）
if not IS_CLOUD:
    with st.expander("⚙️ 控制", expanded=False):
        st.write("状态：%s　模式：%s" % ("运行中" if status == "running" else "已暂停",
                                                "自动" if mode == "auto" else "手动"))
        c1, c2, c3 = st.columns(3)
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
        if c3.button("🔄 刷新数据（拉取+重算）"):
            with st.spinner("拉取行情并重算…"):
                r = subprocess.run([sys.executable, os.path.join(BASE, "run_daily.py")],
                                   capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                st.success("刷新完成")
            else:
                st.error(r.stderr[-500:] if r.stderr else "失败")
            st.rerun()

accts = ([r[0] for r in q("SELECT DISTINCT account FROM daily_report ORDER BY account")]
         or ["sim_a", "sim_hk", "sim_crypto"])
ACCT_CN = {"sim_a": "A股", "sim_hk": "港股", "sim_crypto": "加密货币"}
def _load_workbook_map():
    """母板映射（单一基准）：板块/止损/突破/共振与标的名册同源于此。"""
    try:
        import json as _json
        with open(os.path.join(BASE, "config", "workbook_map.json"),
                  encoding="utf-8") as _f:
            return _json.load(_f)
    except Exception:
        return {}


WORKBOOK_MAP = _load_workbook_map()

# 标的名册单一来源（P2-10）：从 workbook_map 派生，不再手写第二份名单；
# BTC 不在母板 Watchlist 内，单独补入。
SYMBOL_CN = {sym: (meta.get("name") or sym)
             for sym, meta in WORKBOOK_MAP.get("symbols", {}).items()}
SYMBOL_CN.setdefault("BTCUSDT", "比特币")


def stop_ref_of(sym):
    """母板止损参考（唯一口径）：无映射/无止损位时返回 None。"""
    meta = WORKBOOK_MAP.get("symbols", {}).get(sym) or {}
    return meta.get("stop_ref")
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

    # ---- 市场环境（模块A，母板真源三态）：判定只读母板字段 ----
    try:
        from engine import regime as _regime_app
        _env_w = _regime_app.load_market_env(db_path=DB)
        _fld = _env_w.get("fields") or {}
        _b31_s = {True: "是", False: "否"}.get((_fld.get("b31") or {}).get("value"), "读取失败")
        _rel_s = {True: "已放行", False: "未放行"}.get((_fld.get("release") or {}).get("released"), "读取失败")
        _b79_s = {True: "是", False: "否"}.get((_fld.get("b79") or {}).get("value"), "读取失败")
        _state_color = {"attack": "#2ebd85", "cautious": "#f0b90b"}.get(
            _env_w["state"], "#f6465d")
        _extra = ""
        if _env_w["state"] == "cautious":
            _extra = (f' · 验证期参数：仓位系数 {_env_w.get("buy_coef")}、'
                      f'总仓位上限 {_env_w.get("total_cap")}')
        elif _env_w["state"] == "degraded":
            _extra = f' · 原因：{_env_w.get("degraded_reason")}'
        elif _env_w.get("reason"):
            _extra = f' · {_env_w["reason"]}'
        # 自动计算 + 每日对账状态（判定执行源与对账结果，可查）
        _xref = ""
        _auto_w = _env_w.get("auto") or {}
        if _auto_w.get("known"):
            _recon_s = {True: "对账一致", False: "对账不一致"}.get(
                _env_w.get("reconciled"), "对账不可用")
            _xref = (f'<div style="color:#8a8f9c;font-size:12px;">自动计算：'
                     f'上证 {_auto_w["close"]:,.2f} vs MA20 {_auto_w["ma20"]:,.2f} → '
                     f'{"站上" if _auto_w["on"] else "未站上"}（指数日 {_auto_w.get("last_date")}）'
                     f' · 与母板B31 {_recon_s}</div>')
        st.markdown(
            f'<div class="kpi-card"><div class="kpi-label">市场环境（母板真源 · '
            f'B31={_b31_s} / 放行={_rel_s} / B79={_b79_s}）</div>'
            f'<div style="font-size:16px;"><b style="color:{_state_color};">'
            f'{"🟢 " if _env_w["state"] == "attack" else "🟡 " if _env_w["state"] == "cautious" else "⛔ "}'
            f'{_env_w["label"]}</b>'
            f'<span style="color:#8a8f9c;font-size:12px;">{_extra}</span></div>'
            f'{_xref}</div>', unsafe_allow_html=True)
    except Exception as _e:
        st.caption(f"市场环境暂不可用：{_e}")

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
            <div class="num" style="font-size:22px;margin:8px 0;color:#e8eaf0;"><span style="font-size:13px;color:#999;">A股 </span>¥{_fmt(core.get("sim_a", {}).get("nav", 0))}</div>
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
            <div class="num" style="font-size:22px;margin:8px 0;color:#e8eaf0;"><span style="font-size:13px;color:#999;">A股 </span>¥{_fmt(core.get("sim_a", {}).get("pos", 0))}</div>
            <div class="num" style="color:#888;font-size:14px;">HK${_fmt(core.get("sim_hk", {}).get("pos", 0))}</div>
            <div class="num" style="color:#888;font-size:14px;">${_fmt(core.get("sim_crypto", {}).get("pos", 0))}</div>
            </div>''', unsafe_allow_html=True)
    with c4:
        st.markdown(f'''<div style="background:#1a1a2e;border:1px solid #1e2532;border-radius:8px;padding:16px;">
            <div style="color:#888;font-size:13px;">可用资金</div>
            <div class="num" style="font-size:22px;margin:8px 0;color:#e8eaf0;"><span style="font-size:13px;color:#999;">A股 </span>¥{_fmt(core.get("sim_a", {}).get("cash", 0))}</div>
            <div class="num" style="color:#888;font-size:14px;">HK${_fmt(core.get("sim_hk", {}).get("cash", 0))}</div>
            <div class="num" style="color:#888;font-size:14px;">${_fmt(core.get("sim_crypto", {}).get("cash", 0))}</div>
            </div>''', unsafe_allow_html=True)

    # ---- 今日待办（最优先）：一眼知道要做什么 ----
    # 待建条件单
    pending_orders = q("""SELECT COUNT(*) FROM signals s
        LEFT JOIN fills f ON s.date=f.signal_date AND s.account=f.account
          AND s.symbol=f.symbol AND s.side=f.side
        LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account
          AND ap.symbol=s.symbol AND ap.side=s.side
        WHERE f.id IS NULL AND s.date >= date('now','-7 days')
          AND (ap.decision IS NULL OR ap.decision <> 'skipped')""")[0][0] or 0
    # 待审批信号
    pending_signals = q("""SELECT COUNT(*) FROM signals s
        LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account
          AND ap.symbol=s.symbol AND ap.side=s.side
        LEFT JOIN fills f ON f.date=s.date AND f.account=s.account
          AND f.symbol=s.symbol AND f.side=s.side
        WHERE s.date>=date('now','-60 days') AND ap.date IS NULL AND f.date IS NULL""")[0][0] or 0
    if pending_orders > 0 or pending_signals > 0:
        st.markdown('''<div style="background:#1a1a2e;border:1px solid #f0b90b;border-radius:8px;
            padding:12px 16px 4px;margin-bottom:8px;"><b>📋 今日待办</b>（点击直接进入处理）</div>''',
            unsafe_allow_html=True)

        def _goto_trade():
            st.session_state["main_nav"] = "📝 交易"
        _tc = st.columns(2)
        if pending_orders:
            _tc[0].button(f"待建条件单 {pending_orders} 个 → 去处理",
                          on_click=_goto_trade, key="todo_orders")
        if pending_signals:
            _tc[1].button(f"待审批信号 {pending_signals} 个 → 去处理",
                          on_click=_goto_trade, key="todo_signals")
    else:
        st.markdown('''<div style="background:#1a1a2e;border:1px solid #2ebd85;border-radius:8px;
            padding:12px 16px;margin-bottom:16px;">✅ 今日无待办</div>''',
            unsafe_allow_html=True)

    # ---- 1. 风险警报区 ----
    alerts = []
    if status != "running":
        alerts.append(("warn", "⏸ 系统已暂停：新信号不会成交。已挂在券商的条件单请在券商端自行检查/撤销。"))
    # 破止损持仓检查（P1-5：唯一口径 = 母板止损参考 stop_ref；
    # 此前用成本-5%口径与母板冲突，同标的在不同页面结论相反）
    try:
        _bs = q("""SELECT p.symbol, p.qty, p.avg_cost, m.close FROM positions p
                   JOIN (SELECT symbol, MAX(date) md FROM market_data GROUP BY symbol) t
                   ON p.symbol=t.symbol
                   JOIN market_data m ON m.symbol=t.symbol AND m.date=t.md
                   WHERE p.account=? AND p.qty>0""", (acct,))
        for _s, _q, _avg, _last in _bs:
            _sr = stop_ref_of(_s)
            if _sr and _last and _last < _sr:
                alerts.append(("danger", f"🔴 破止损仍持仓：{_s} 现价{_last:.2f} < 母板止损参考 {_sr:.2f}"))
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
    # 互联互通资金（北向/南向）：成交额 + 北向持股市值，净买额 2024-08 起停止披露
    _ns = _fetch_ns_flow()
    if _ns:
        def _ns_delta(cur, prev):
            if cur is None or not prev:
                return ""
            chg = (cur - prev) / prev * 100
            arrow = "▲" if chg >= 0 else "▼"
            return f'<span style="color:#8a8f9c;font-size:13px;">较前日 {arrow} {abs(chg):.1f}%</span>'
        nc1, nc2 = st.columns(2)
        with nc1:
            amt = f"{_ns['north_amt']:,.0f} 亿" if _ns.get("north_amt") is not None else "—"
            hold = f"持股市值 {_ns['north_hold']:.2f} 万亿" if _ns.get("north_hold") else ""
            st.markdown(
                f'<div class="kpi-card"><div class="kpi-label">北向资金（沪股通+深股通）· 成交额 {_ns.get("north_date", "")}</div>'
                f'<div class="kpi-value">{amt}</div>'
                f'<div style="margin-top:4px;">{_ns_delta(_ns.get("north_amt"), _ns.get("north_prev"))}'
                f'<span style="color:#8a8f9c;font-size:13px;">{hold}</span></div></div>',
                unsafe_allow_html=True)
        with nc2:
            amt = f"{_ns['south_amt']:,.0f} 亿" if _ns.get("south_amt") is not None else "—"
            st.markdown(
                f'<div class="kpi-card"><div class="kpi-label">南向资金（港股通）· 成交额 {_ns.get("south_date", "")}</div>'
                f'<div class="kpi-value">{amt}</div>'
                f'<div style="margin-top:4px;">{_ns_delta(_ns.get("south_amt"), _ns.get("south_prev"))}</div></div>',
                unsafe_allow_html=True)

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
    # 事件流：信号 / 审批 / 成交按时间倒序，复盘与对账用
    st.subheader("事件流")
    st.caption("信号、审批、成交的时间线——看系统最近做了什么、还欠什么。")
    _EV_STYLE = {
        "成交": ("#2ebd85", "成交"),
        "信号": ("#f0b90b", "信号"),
        "审批": ("#1890ff", "审批"),
    }
    events = []
    for d, a, s, sd, qty, px in q(
            "SELECT date,account,symbol,side,qty,price FROM fills ORDER BY date DESC LIMIT 6"):
        events.append((d, "成交", f"{ACCT_CN.get(a, a)} {sym_name(s)} {sd} {qty}股 @ {px}"))
    for d, a, s, sd in q(
            "SELECT date,account,symbol,side FROM signals ORDER BY date DESC LIMIT 6"):
        events.append((d, "信号", f"{ACCT_CN.get(a, a)} {sym_name(s)} {sd}"))
    for d, a, s, sd, dc in q(
            "SELECT date,account,symbol,side,decision FROM approvals ORDER BY updated DESC LIMIT 6"):
        dc_cn = "批准" if dc == "approved" else ("跳过" if dc == "skipped" else dc)
        events.append((d, "审批", f"{ACCT_CN.get(a, a)} {sym_name(s)} {sd} · {dc_cn}"))
    events.sort(key=lambda e: e[0], reverse=True)
    if events:
        rows_html = "".join(
            f'<div style="display:flex;gap:10px;align-items:baseline;padding:5px 0;'
            f'border-bottom:1px solid #1e2532;">'
            f'<span style="color:#8a8f9c;font-size:12px;min-width:86px;">{d}</span>'
            f'<span style="color:{_EV_STYLE[k][0]};font-size:12px;font-weight:700;min-width:32px;">{k}</span>'
            f'<span style="font-size:13px;">{txt}</span></div>'
            for d, k, txt in events[:10])
        st.markdown(rows_html, unsafe_allow_html=True)
    else:
        st.write("暂无事件")

# ================================================================
# 📈 行情与信号：实时行情 + 策略规则 + 触发线
# ================================================================
elif tab == "🔍 选股":
    st.header("🔍 选股")
    # 模块D（2026-10-26）：原「🎯 条件筛选」手动面板（2026-10-17 试验版，
    # 涨幅/RSI/MACD 手动条件 + 独立出结果）已删除——它是与三重校验并存的
    # 第二个决策入口，违反唯一入口原则。本页只呈现每日自动筛选结果：
    # 顶部市场环境状态（模块A三态+对账）→ 候选名单与全池结果。
    from engine.screen import RULE_VERSION as _SCREEN_RV
    st.caption("自动筛选 · 运行时点（asof）：%s · 规则版本 %s · 结果与 "
               "screen_runs 落库一致（页面不提供手动筛选入口）"
               % (datetime.now().strftime("%Y-%m-%d"), _SCREEN_RV))
    # ---- TOP标的榜：最近一期 screen_runs 全集 TOP8 + screen_perf 验证列 ----
    st.subheader("🏆 TOP标的榜")
    _tb_asof, _tb_rv, _tb_rows = _topboard_data(DB)
    if _tb_rows:
        st.caption(f"最近一期落库评分（asof {_tb_asof} · {_tb_rv}）按总分 "
                   "TOP8；验证列 = screen_perf 样本外前向收益（S/A 样本）")
        _tb_table = []
        for _i, r in enumerate(_tb_rows, 1):
            def _px(v):
                return f"{v:.2f}" if isinstance(v, (int, float)) else "—"
            _trig, _close = r.get("plan_trigger"), r.get("close")
            if isinstance(_trig, (int, float)) and \
                    isinstance(_close, (int, float)) and _close:
                _dist_v = (_trig - _close) / _close * 100
                _dist = "已触发" if _dist_v <= 0 else f"+{_dist_v:.1f}%"
            else:
                _dist = "—"
            if r.get("capped"):
                _note = "⛔封顶：" + "；".join(r.get("gates") or [])
            elif r.get("gates"):
                _note = "；".join(r["gates"])
            else:
                _note = ""
            _p = r.get("_perf") or {"n": 0}
            if (_p.get("n") or 0) < 5:
                _verify = f"未验证·积累中（n={_p.get('n') or 0}）"
            elif _p.get("avg20") is not None:
                _verify = (f"n={_p['n']} · 5日 {_p['avg5'] * 100:+.2f}% · "
                           f"20日 {_p['avg20'] * 100:+.2f}% · "
                           f"5日胜率 {_p['win5'] * 100:.0f}%")
            else:
                _verify = (f"n={_p['n']} · 5日 {_p['avg5'] * 100:+.2f}% · "
                           f"20日 — · 5日胜率 {_p['win5'] * 100:.0f}%")
            _tb_table.append({
                "#": _i, "代码": r.get("symbol") or "—",
                "名称": r.get("name") or "—",
                "评级": r.get("grade") or "—",
                "总分": (f"{r['total']:.1f}" if isinstance(r.get("total"), (int, float)) else "—"),
                "技术": (f"{r['tech']:.1f}" if isinstance(r.get("tech"), (int, float)) else "—"),
                "新闻": (f"{r['news']:.1f}" if isinstance(r.get("news"), (int, float)) else "—"),
                "母板": (f"{r['board']:.1f}" if isinstance(r.get("board"), (int, float)) else "—"),
                "触发价": _px(r.get("plan_trigger")),
                "止损位": _px(r.get("plan_stop")),
                "目标位": _px(r.get("plan_target")),
                "距触发": _dist, "验证（样本外）": _verify, "标注": _note})
        st.table(_tb_table)
    else:
        st.caption("暂无 screen_runs 落库记录（夜跑评分后生成）")
    # ---- 多重校验·优质标的：技术面45 + 新闻面35 + 母板面20，硬闸门封顶，只列 S/A ----
    st.subheader("🏆 多重校验·优质标的")
    _today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        _srows = _screen_cached(DB, _today_str)
    except Exception as _e:  # 评分失败不拖垮整页
        _srows = []
        st.caption(f"评分暂不可用：{_e}")
    # 市场环境闸（模块A，母板真源）：禁开/降级时全池已封顶 B、S/A
    # 为空——必须明示原因（只观察），不得静默呈现为“无优质标的”；
    # 谨慎态标注验证期参数。
    if _srows and _srows[0].get("env_state") == "blocked_new":
        st.warning("⛔ 市场环境闸：当前「禁止新开仓」——候选池只观察，"
                   "S/A 名单已清空。原因："
                   f"{_srows[0].get('env_reason') or '母板口径禁开'}")
    elif _srows and _srows[0].get("env_state") == "degraded":
        st.error("⛔ 市场环境数据不可用，已安全降级为禁止新开仓——"
                 "候选池只观察，S/A 名单已清空。原因："
                 f"{_srows[0].get('env_reason') or '未知'}。"
                 "请重跑 build_workbook_map。")
    elif _srows and _srows[0].get("env_state") == "cautious":
        st.info("🟡 谨慎交易（母板未放行·B79=是）：可新建仓，但套验证期"
                "参数（仓位系数 0.5、总仓位上限 0.3）。")
    _sa = [r for r in _srows if r["grade"] in ("S", "A")]
    _GRADE_BG = {"S": "#f0b90b", "A": "#f6465d"}
    if _sa:
        for r in _sa:
            _bg = _GRADE_BG[r["grade"]]
            st.markdown(
                f"<span style='background:{_bg};color:#111;border-radius:4px;"
                f"padding:1px 7px;font-weight:700;'>{r['grade']}</span> "
                f"**{r['symbol']} {r['name']}** · {r['total']:.1f} 分 "
                f"（技 {r['tech']:.0f} / 讯 {r['news']:.0f} / 板 {r['board']:.0f}）"
                f" · @{r['close']:.2f}", unsafe_allow_html=True)
            if r.get("plan_trigger") is not None and r.get("plan_stop") is not None:
                st.caption(
                    f"交易计划：触发 {r['plan_trigger']:.2f} / "
                    f"止损 {r['plan_stop']:.2f} / 目标 {r['plan_target']:.2f}"
                    "（目标位按2.5倍盈亏比计算）")
            else:
                st.caption("交易计划：母板未设（缺关键突破位/止损位）")
            st.caption(r["evidence"])
    elif _srows:
        _top = _srows[0]
        st.info(f"当前无 S/A 级标的。最高 {_top['grade']} 级："
                f"{_top['symbol']} {_top['name']} {_top['total']:.1f} 分"
                + (f"（闸门：{'；'.join(_top['gates'])}）" if _top["gates"] else ""))
    # ---- 板块新闻面：X 博主证据账折算的板块情绪 ----
    st.subheader("📰 板块新闻面")
    try:
        from engine.screen import load_map as _load_wmap
        _wmap = _load_wmap()
        _label_by_key = {v.get("sector_key"): k for k, v in _wmap.get("sectors", {}).items()}
        _label_by_key.update({"semi_equip": "半导体设备", "market": "市场整体"})
        _chips = []
        for s in _sector_news_cached(_today_str):
            _label = _label_by_key.get(s["sector_key"], s["sector_key"])
            _arrow = "▲" if s["raw"] > 0 else "▼"
            _color = "#f6465d" if s["raw"] > 0 else "#2ebd85"
            _latest = max(str(i["date"]) for i in s["items"])
            _srcs = "/".join(sorted({i["source"] for i in s["items"]}))
            _chips.append(
                f"<span style='display:inline-block;background:#1a1a2e;"
                f"border:1px solid #1e2532;border-radius:12px;padding:3px 10px;"
                f"margin:2px 4px 2px 0;color:{_color};'>"
                f"{_label} {_arrow} {s['raw']:+.1f} · {_srcs} {_latest[5:]}</span>")
        if _chips:
            st.markdown("".join(_chips), unsafe_allow_html=True)
        else:
            st.caption("近14天无未过期新闻证据")
    except Exception as _e:
        st.caption(f"板块新闻面暂不可用：{_e}")
    # 模块E裁决（2026-10-26 用户拍板删除）：原「🔎 全市场搜索」手动查图控件
    # （不产出候选/订单、与全自动唯一入口冲突）已移除，不得回流。
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
    # 模块E裁决（2026-10-26 用户拍板删除）：原「📰 持仓资讯」外链目录
    # （已被证据制板块新闻面覆盖）已移除，不得回流。
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
        # 只保留关键提示：纯文字，无表情符号
        tags = []
        if rsi_v and rsi_v > 70: tags.append("超买")
        elif rsi_v and rsi_v < 30: tags.append("超卖")
        tags.append("MACD多头" if (macd_h and macd_h > 0) else "MACD空头")
        if regime == "trend": tags.append("趋势")
        if align > 0.5: tags.append("均线多头")
        elif align < -0.5: tags.append("均线空头")
        if last_c and upper[-1] and last_c > upper[-1]: tags.append("破上轨")
        if last_c and lower[-1] and last_c < lower[-1]: tags.append("破下轨")
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
                LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account
                  AND ap.symbol=s.symbol AND ap.side=s.side
                WHERE s.account=? AND f.id IS NULL
                  AND s.date >= date('now','-7 days')
                  AND (ap.decision IS NULL OR ap.decision <> 'skipped')
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
            # SELL用当前持仓做数量；BUY用pipeline的position_sizer同逻辑
            # （2026-10-18定：app校验必须用pipeline结果，避免校验数量与实际下单数量不一致）
            qty_est = None
            if sd == "BUY":
                from engine.pipeline import suggest_buy_qty_from_db
                qty_est = suggest_buy_qty_from_db(DB, acct, s, px, CFG)
            if sd == "SELL":
                pos = q("SELECT qty FROM positions WHERE account=? AND symbol=?", (acct, s))
                qty_est = pos[0][0] if pos else 0
            ok, errs, warns = validate_order(acct, s, sd, qty_est, px, d)
            validated.append((d, s, sd, px, ok, errs, warns))
        # 拦截：有校验失败的先提示
        failed = [v for v in validated if not v[4]]
        if failed:
            st.warning(f"⛔ {len(failed)} 条信号校验未通过，已拦截（不生成订单指引）")
            st.caption("校验不通过无法建单。不再处理这条就点「跳过/隔离」：跳过后不再计入待办，模拟盘也不会成交。")
            for d, s, sd, px, ok, errs, warns in failed:
                _fc1, _fc2 = st.columns([3, 1])
                with _fc1:
                    st.markdown(f"**{sym_name(s)}** {sd} @{px} "
                                f'<span style="color:#666;font-size:12px;">{d}</span>',
                                unsafe_allow_html=True)
                    st.caption("; ".join(errs))
                with _fc2:
                    if st.button("⏭ 跳过/隔离", key=f"block_skip_{d}_{s}_{sd}"):
                        _ap_state(f"{d}_{s}_{sd}", "skipped", d, acct, s, sd)
                        if st.session_state.get(f"apstate_{d}_{s}_{sd}") == "已生效":
                            st.rerun()  # 立即重查：已跳过的信号当场从待办/列表消失
                        st.info(st.session_state.get(f"apstate_{d}_{s}_{sd}", "失败"))
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
            # 逐条建单/跳过（落库到 approvals，刷新后不再回到待办）
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
                    for w in warns:
                        st.caption(f"⚠️ {w}")
                with c2:
                    if st.button("✅ 已建单", key=f"done_{d}_{s}_{sd}"):
                        _ap_state(f"{d}_{s}_{sd}", "approved", d, acct, s, sd)
                        st.success(st.session_state.get(f"apstate_{d}_{s}_{sd}", "失败"))
                with c3:
                    if st.button("⏭ 跳过", key=f"skip_{d}_{s}_{sd}"):
                        _ap_state(f"{d}_{s}_{sd}", "skipped", d, acct, s, sd)
                        if st.session_state.get(f"apstate_{d}_{s}_{sd}") == "已生效":
                            st.rerun()  # 立即重查：已跳过的信号当场从待办/列表消失
                        st.info(st.session_state.get(f"apstate_{d}_{s}_{sd}", "失败"))

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
            r = list(r); r[1] = ACCT_CN.get(r[1], r[1])
            w.writerow(r)
        st.download_button("下载CSV", out.getvalue(), f"对账单_{ACCT_CN.get(acct, acct)}.csv", "text/csv", key="dl_stmt")

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

    else:
        st.write("空仓")

    # 持仓K线：固定为实盘4只（2026-10-25用户指定：中国船舶换科德数控，加港股两只）
    # 成本为用户2026-09-28确认的实盘成本，只作价格线与相对成本对照，不进模拟账
    st.subheader("持仓K线")
    from engine.tv_charts import kline_html
    _KLINE_WATCH = [("002446", "sim_a", 11.2), ("688305", "sim_a", 64.0),
                    ("HK9660", "sim_hk", 7.9), ("HK0354", "sim_hk", 5.2)]
    for _sym, _sacct, _cost in _KLINE_WATCH:
        _bars = q("SELECT date,open,high,low,close,volume FROM market_data"
                  " WHERE symbol=? ORDER BY date DESC LIMIT 120", (_sym,))
        if len(_bars) < 20:
            st.write(f"**{sym_name(_sym)}**：数据不足（{len(_bars)}根）")
            continue
        _bars = _bars[::-1]
        _last = _bars[-1][4]
        _pct = (_last - _cost) / _cost * 100 if _cost else 0
        _pls = [{"price": round(_cost, 4), "title": f"实盘成本 {_cost:.2f}",
                 "color": "#1890ff"}]
        _sm = q("SELECT date,side FROM signals WHERE account=? AND symbol=? "
                "ORDER BY date DESC LIMIT 20", (_sacct, _sym))
        _marks = []
        for _d, _sd in _sm:
            if _sd == "BUY":
                _marks.append({"time": _d, "position": "belowBar", "shape": "arrowUp",
                               "text": "买", "color": "#2ebd85"})
            else:
                _marks.append({"time": _d, "position": "aboveBar", "shape": "arrowDown",
                               "text": "卖", "color": "#f6465d"})
        st.write(f"**{sym_name(_sym)}** 实盘成本 {_cost:.2f} · 现价 {_last:.2f} · "
                 f"相对成本 {_pct:+.1f}%")
        st.components.v1.html(
            kline_html(f"watch_{_sym}", _bars, price_lines=_pls,
                       markers=_marks, height=420),
            height=430)

    # 近期成交
    st.subheader("近期成交")
    fl = q("SELECT date,symbol,side,qty,price,fee,pnl FROM fills WHERE account=? ORDER BY date DESC LIMIT 20", (acct,))
    if fl:
        st.table([{"日期": d, "标的": sym_name(s), "方向": sd, "数量": qty, "成交价": px, "费用": fee, "盈亏": pnl}
                  for d, s, sd, qty, px, fee, pnl in fl])
    else:
        st.write("暂无成交")

# ================================================================
# 🏦 机构集体建仓监控（方案1 影子监控 · 只读）：龙虎榜机构专用
# 席位净买/连续天数；不生成信号、不建意图、不影响交易主链。
# ================================================================
elif tab == "🏦 机构集体建仓监控":
    st.markdown('<div class="section-head">🏦 机构集体建仓监控（影子 · 只读）</div>',
                unsafe_allow_html=True)
    st.caption("口径：龙虎榜「机构专用」席位——匿名聚合席位，非机构家数；"
               "机构专用席位净买不等于具名机构真实建仓。本页仅作影子监控与"
               "盘后评估，不生成信号、不创建交易意图、不改变策略参数。"
               "数据源：东方财富龙虎榜公开数据（夜间抓取）。")
    try:
        from engine import inst_lhb as _inst
        _con = sqlite3.connect(DB)
        try:
            _status = _inst.recent_fetch_status(_con, limit=10)
            _latest_ok = _inst.latest_success_date(_con)
            if not _status:
                st.info("尚无生产样本：夜间抓取尚未首次运行，页面不展示任何数据。")
            else:
                _last = _status[0]
                st.write("最近成功抓取日期：**%s** · 最近一次抓取 %s（%s）"
                         % (_latest_ok or "无",
                            _last["date"],
                            "成功" if _last["state"] != "failed" else "失败"))
                st.markdown("**抓取状态（近 10 个抓取日）**")
                st.table([{
                    "日期": s["date"],
                    "状态": "成功" if s["state"] != "failed" else "失败",
                    "聚合标的": s["agg_rows"] if s["state"] != "failed" else "—",
                    "原始席位记录": s["raw_rows"] if s["state"] != "failed" else "—",
                    "失败原因": s["reason"] if s["state"] == "failed" else "—",
                } for s in _status])
                if _latest_ok:
                    _board = _inst.shadow_board(_con, min_net_buy_days=2)
                    st.markdown("**影子榜（%s · 当日净买为正且连续净买 ≥2 交易日）**"
                                % _latest_ok)
                    if _board:
                        st.table([{
                            "代码": b["symbol"], "名称": b["name"] or "",
                            "当日净买(万)": f"{b['inst_net'] / 10000:,.0f}",
                            "连续净买交易日": b["consec_net_buy_days"],
                            "连续出现交易日": b["consec_appear_days"],
                            "天数确定性": "确定" if b["streak_certain"]
                            else "不确定（序列含抓取失败日）",
                        } for b in _board])
                    else:
                        st.write("当日无符合条件的标的。")
                    _day_rows = _con.execute(
                        "SELECT symbol,name,inst_buy,inst_sell,inst_net,"
                        "seat_buy_records,seat_sell_records,window"
                        " FROM inst_lhb_daily WHERE date=?"
                        " ORDER BY inst_net DESC", (_latest_ok,)).fetchall()
                    st.markdown("**当日聚合全表（%s · 共 %d 只 · 供复算核对）**"
                                % (_latest_ok, len(_day_rows)))
                    st.table([{
                        "代码": r[0], "名称": r[1] or "",
                        "买入(万)": f"{(r[2] or 0) / 10000:,.0f}",
                        "卖出(万)": f"{(r[3] or 0) / 10000:,.0f}",
                        "净买(万)": f"{(r[4] or 0) / 10000:,.0f}",
                        "买席位记录": r[5], "卖席位记录": r[6],
                        "窗口": "累计" if r[7] == "cum" else "一日",
                    } for r in _day_rows])
                    st.caption("窗口=累计：该股当日只有多日累计类事件，金额为累计口径，"
                               "与一日类不相加（聚合规则见 engine/inst_lhb）。")
        finally:
            _con.close()
    except Exception:  # noqa: BLE001 — 影子视图故障隔离，不影响交易功能
        st.info("机构监控数据暂不可用（不影响交易功能）。")

# ================================================================
# 🛡 验证与风控：KPI 验证 + 风控指标 + 审计
# ================================================================
# ================================================================
# 🛡 验证与风控：验证 KPI + 风控指标 + 审计
# ================================================================
elif tab == "🛡 验证":
    st.markdown('<div class="section-head">🛡 验证与风控</div>', unsafe_allow_html=True)

    # 页内账户切换：上方为单账户详情，下方仪表盘为跨账户摘要
    acct = st.radio("账户", accts, format_func=lambda x: ACCT_CN.get(x, x),
                    horizontal=True, key="verify_acct")

    # ---- 单账户详情：验证 KPI 卡片（四态徽标，阻断原因优先） ----
    st.subheader(f"单账户详情 · {ACCT_CN.get(acct, acct)}")
    st.caption("毕业标准：期望值>0 · 盈亏比≥2.5（扣费后） · 最大回撤≤10% · 样本20-30笔 · 执行分≥90 · 零数据事故 · 股票与加密分开算")

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

    # 破止损仍持仓检查（P1-5：唯一口径 = 母板止损参考 stop_ref，
    # 与工作台/选股页一致；此前成本-5%口径已更名“浮亏提示”，见下方）
    st.subheader("破止损仍持仓")
    breached = q("""SELECT p.symbol, p.qty, p.avg_cost, p.last_price
                    FROM positions p WHERE p.account=? AND p.qty>0""", (acct,))
    found = False
    for s, qty_, avg_, last_ in breached:
        _sr = stop_ref_of(s)
        if _sr and last_ and last_ < _sr:
            st.error(f"🔴 {s} 现价 {last_:.2f} 已跌破母板止损参考 {_sr:.2f}，仍持有 {qty_} 股")
            found = True
    if not found:
        st.success("✅ 无破止损持仓")
    # 浮亏提示：成本 -5% 仅提示浮亏幅度，不再冒充止损判定（P1-5 更名）
    for s, qty_, avg_, last_ in breached:
        if avg_ and last_ and (last_ - avg_) / avg_ <= -0.05:
            st.caption(f"浮亏提示：{s} 现价 {last_:.2f} 较成本 {avg_:.2f} "
                       f"{(last_ - avg_) / avg_ * 100:.1f}%（仅提示浮亏，不作止损判定）")

    # ---- 审计（隐藏式：默认折叠，表面只留验证仪表盘等关键信息） ----
    excl_r = q("SELECT date, account, symbol, side, reason FROM graduation_exclusions ORDER BY date DESC")
    with st.expander(f"审计 · 毕业样本剔除记录（{len(excl_r)} 条）", expanded=False):
        if excl_r:
            st.write("成交保留，仅不计入毕业指标：")
            for d, a, s, sd, reason in excl_r:
                st.write(f"- {d} {ACCT_CN.get(a, a)} {s} {sd}: {reason}")
        else:
            st.write("无剔除记录")

    # ---- 多重校验绩效（P2-9）：screen_runs/screen_perf 样本外追踪 ----
    try:
        _sp = q("SELECT COUNT(*), SUM(CASE WHEN fwd5_ret IS NOT NULL THEN 1 ELSE 0 END),"
                " AVG(fwd5_excess), SUM(CASE WHEN fwd5_excess>0 THEN 1 ELSE 0 END)"
                " FROM screen_perf"
                " WHERE grp='sa'"
                "    OR (COALESCE(grp,'')='' AND COALESCE(capped,0)=0"
                "        AND grade IN ('S','A'))")
        _n, _m5, _avg5, _hit5 = (_sp[0] if _sp else (0, None, None, None))
    except Exception:
        _n, _m5, _avg5, _hit5 = 0, None, None, None
    if _n and _m5:
        st.caption(f"多重校验绩效（样本外）：S/A 累计 {_n} 条，已满 5 日 {_m5} 条，"
                   f"5 日平均相对收益 {_avg5 * 100:+.2f}%，命中率 {_hit5 / _m5 * 100:.0f}%"
                   "（相对全池平均，tools/screen_perf.py 回填）")
    else:
        st.caption("多重校验绩效：样本积累中（S/A 名单自 screen_runs 落库起逐期追踪，"
                   "5/20 日后收益由 tools/screen_perf.py 回填，暂无已到期样本）")

    # ---- 跨账户摘要（5指标 × 3账户） ----
    st.subheader("跨账户摘要")
    try:
        from sync_snapshot import build_graduation
        con = sqlite3.connect(DB)
        g = build_graduation(con)
        con.close()
        tg = g["targets"]
        # 表头
        hdr = ["指标"] + [f"{ACCT_CN.get(aid, aid)}" for aid in ["sim_a", "sim_hk", "sim_crypto"]]
        rows = []
        # 样本数
        r = ["样本数"]
        for aid in ["sim_a", "sim_hk", "sim_crypto"]:
            d = g["accounts"].get(aid, {})
            rt = d.get("round_trips") or 0
            ok = rt >= tg["sample_min"]
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
