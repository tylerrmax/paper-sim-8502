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
/* 主题已由 .streamlit/config.toml 固定为深色（UI批A G-1），
   标题色不再随浏览器 prefers-color-scheme 切换，避免深底深字。 */
/* ===== UI-R1 共享设计系统（阶段C 基础）：令牌与组件类，六页共用 =====
   令牌：底 #06090d/#0a0f16、面板 #101823、边线 #22303f、正文 #d6dee7、
   次级 #8b9bab、弱 #5d7185、焦点 #56c8dc、通过 #2ebd85、阻断 #f6465d、
   待处理 #e8a33d、数据蓝 #4a90d9。状态一律文字+颜色双表达。 */
.r1-chip { display: inline-block; padding: 2px 9px; border-radius: 5px;
           font-size: 12px; font-weight: 600; border: 1px solid transparent;
           white-space: nowrap; }
.r1-chip.ok { background: rgba(46,189,133,.13); color: #2ebd85; border-color: rgba(46,189,133,.35); }
.r1-chip.block { background: rgba(246,70,93,.13); color: #f6465d; border-color: rgba(246,70,93,.35); }
.r1-chip.wait { background: rgba(232,163,61,.13); color: #e8a33d; border-color: rgba(232,163,61,.35); }
.r1-chip.info { background: rgba(86,200,220,.10); color: #56c8dc; border-color: rgba(86,200,220,.30); }
.r1-chip.mut { background: #141e2a; color: #8b9bab; border-color: #22303f; }
.r1-chip.miss { background: transparent; color: #8b9bab; border: 1px dashed #3a4a5c; }
.r1-seg { display: inline-flex; align-items: center; gap: 6px; }
.r1-seg-k { color: #5d7185; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
.r1-seg + .r1-seg { border-left: 1px solid #22303f; padding-left: 12px; margin-left: 4px; }
.r1-brand { font-size: 17px; font-weight: 800; letter-spacing: 1px; color: #d6dee7; padding: 4px 2px 10px; }
.r1-brand span { color: #5d7185; font-size: 11px; font-weight: 600; }
.r1-side-note { color: #5d7185; font-size: 12px; line-height: 1.6; border-top: 1px solid #22303f; padding-top: 10px; margin-top: 14px; }
[data-testid="stSidebar"] { background: #06090d; border-right: 1px solid #22303f; }
.main-title { font-size: 28px; font-weight: 800; margin-bottom: 4px; letter-spacing: 1px; color: #e8ecf1; }
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
/* UI-R1 阶段C：左侧固定导航恢复（旧「左侧栏已移除」隐藏规则就此作废） */
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
.qs-bad { background: rgba(246,70,93,.16); color: #f6465d; border: 1px solid rgba(246,70,93,.45); }
/* 状态条（UI批A G-3）：数据健康徽标 + 运行版本一行呈现 */
.status-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
              background: #10141b; border: 1px solid #1e2532; border-radius: 8px;
              padding: 8px 12px; margin-bottom: 12px; font-size: 12px; color: #e8ecf1; }
.status-bar.warn { border-color: rgba(240,185,11,.55); }
.status-bar.danger { border-color: rgba(246,70,93,.6); }
.status-bar .sb-label { color: #8a8f9c; font-weight: 700; }
.status-bar .sb-ver { margin-left: auto; color: #8a8f9c; }
/* 顶栏吸顶（P3 实测通过，2026-10-31）：状态条与系统阻断条的元素
   容器 sticky，滚动时常驻顶部——风险不随滚动消失（§1/§2.1）。
   选择器经真实 DOM 实测：滚动容器为 stMain，:has 定位容器。 */
[data-testid="stElementContainer"]:has(.status-bar) {
  position: sticky; top: 0px; z-index: 1000; background: #0b0e13; }
[data-testid="stElementContainer"]:has(.blocked-banner) {
  position: sticky; top: 56px; z-index: 999; background: #0b0e13; }
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
    """判定验证状态：样本状态优先，再看指标本身。

    样本规则（2026-10-31 批A专项核定，分支表见 ui-audit 专项测试）：
    - 调用方同时给出 sample_n/sample_min 才做样本检查（KPI 指标卡）；
      只给其一（样本数量或阈值缺失）→ 数据异常，
      不得把未知样本量伪造成样本不足或达标；
    - 两者都不给 = 调用方显式声明样本检查不适用（仅样本卡本身，
      其值即样本数，已由调用点的 rt>=sample_min 条件把关）；
    - 样本数为负数或非整数 → 数据异常（计数损坏，不是样本不足）；
    - 样本不足优先于指标缺失判定；样本充足而指标为 None
      或 NaN → 数据异常。
    """
    if (sample_n is None) != (sample_min is None):
        return "anomaly"
    if sample_n is not None:
        try:
            _n_int = int(sample_n)
        except (TypeError, ValueError, OverflowError):
            return "anomaly"
        if sample_n < 0 or _n_int != sample_n:
            return "anomaly"
        if sample_n < sample_min:
            return "insufficient"
    if value is None:
        return "anomaly"
    if isinstance(value, float) and value != value:  # NaN
        return "anomaly"
    ok = (value >= target) if higher_is_better else (value <= target)
    return "pass" if ok else "fail"

# 当前目标页：radio 的 session_state 在脚本执行前已更新（_goto 回调
# 同理），首屏减负按它判定；缺省（首次进入）即工作台。
_tab_now = st.session_state.get("main_nav", "🏠 工作台")
_wb_first = _tab_now == "🏠 工作台"

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
    # UI批B §2.2：订单文本只写订单本身；持仓上下文（成本/浮盈/止损参考）
    # 独立成行（holdinfo），无持仓的 SELL 记入 obs_syms 以灰色呈现，
    # 不与可执行订单同级黄色。
    pending = {}
    obs_syms = set()
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
            stop_s = f" | 策略止损 @{_px_fmt(sym, stop_px)}" if stop_px else ""
            pending[sym] = f"{side} {act} @{_px_fmt(sym, px)}{stop_s}"
        else:
            # SELL 止盈/止损/信号平仓 @价格（动作推断仍用持仓浮盈）
            pos = q("SELECT qty, avg_cost FROM positions WHERE symbol=? AND qty>0", (sym,))
            if pos:
                qty, cost = pos[0]
                cur = q("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,))
                cur_px = cur[0][0] if cur else px
                pnl_pct = (cur_px - cost) / cost * 100 if cost else 0
                # 判断止盈/止损：浮盈>0为止盈，<-5%为止损，否则信号平仓
                if not act or act == "信号平仓":
                    if pnl_pct > 0:
                        act = "止盈"
                    elif pnl_pct < -5:
                        act = "止损"
            else:
                obs_syms.add(sym)
            pending[sym] = f"{side} {act} @{_px_fmt(sym, px)}"
    # 持仓要点（§2.2）：持仓 · 成本 · 浮盈% · 止损参考；破止损加红点
    holdinfo = {}
    try:
        for _hsym, _hqty, _hcost in q(
                "SELECT symbol, SUM(qty), SUM(qty*avg_cost)/SUM(qty)"
                " FROM positions WHERE qty>0 GROUP BY symbol"):
            _cur = q("SELECT close FROM market_data WHERE symbol=?"
                     " ORDER BY date DESC LIMIT 1", (_hsym,))
            _cp = _cur[0][0] if _cur else None
            _pnl = (_cp - _hcost) / _hcost * 100 if _cp and _hcost else 0
            _sr = stop_ref_of(_hsym)
            _dot = "🔴 " if (_sr and _cp and _cp < _sr) else ""
            _srs = f" · 止损参考 {_px_fmt(_hsym, _sr)}" if _sr else ""
            holdinfo[_hsym] = (f"{_dot}持仓 {_hqty:g} · 成本 "
                               f"{_px_fmt(_hsym, _hcost)} · 浮盈 {_pnl:+.1f}%{_srs}")
    except Exception:
        holdinfo = {}

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
            price_s = _px_fmt(sym, price)
        else:
            row = q("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,))
            price = row[0][0] if row and row[0][0] else 0
            price_s = _px_fmt(sym, price) if price else "--"
            if row:
                prev = q("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1 OFFSET 1", (sym,))
                if prev and prev[0][0]:
                    chg = (price - prev[0][0]) / prev[0][0] * 100
        color = UP_C if chg >= 0 else DOWN_C
        sign = "+" if chg >= 0 else ""
        code_show = sym[2:] if sym.startswith("HK") else sym
        name_show = SYMBOL_CN.get(sym, "")
        tech = _tech_signals(sym)
        order = pending.get(sym, "-")
        # 观察态只认最终展示的那条订单：同股另有 SELL 无持仓信号时，
        # 若最终展示的是 BUY，不得把可执行 BUY 一并灰化。
        _is_obs = sym in obs_syms and order.startswith("SELL")
        if sym in holdinfo:
            order += (f"<br><span style='font-size:11px;color:#888;'>"
                      f"{holdinfo[sym]}</span>")
        elif _is_obs:
            order += ("<br><span style='font-size:11px;color:#888;'>"
                      "无持仓，仅供观察</span>")
        # 无持仓仅供观察 = 灰色（§2.2），不与可执行订单同级黄色
        if order != "-" and not _is_obs:
            order_style = 'color:#f0b90b;font-weight:bold;'
        else:
            order_style = 'color:#666;'
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

    # 紧凑模式（UI批A G-2）：默认只显示有待建单或持仓的标的；
    # 完整行情保留在下方折叠入口，不得无入口截断。
    # 持仓查询失败不得伪装成「没有持仓」（2026-10-31 批A专项）：
    # 失败时显式标注关注列表数据不完整，仅按待建单筛选。
    _held = set()
    _held_ok = True
    try:
        _held = {r[0] for r in q("SELECT symbol FROM positions WHERE qty>0")}
    except Exception:
        _held_ok = False
    _focus = set(pending) | _held
    if not _held_ok:
        st.markdown(
            '<div class="alert-banner warn">⚠ 持仓行情查询失败：'
            '关注列表数据不完整（仅按待建单筛选，持仓标的可能未显示），'
            '完整行情见下方折叠。</div>', unsafe_allow_html=True)
    _total = len(a_syms) + len(hk_syms) + len(crypto_syms)
    if _focus and not _wb_first:
        st.markdown(
            _table([s for s in a_syms if s in _focus], "A股 · 关注")
            + _table([s for s in hk_syms if s in _focus], "港股 · 关注")
            + _table([s for s in crypto_syms if s in _focus], "加密 · 关注"),
            unsafe_allow_html=True)
    elif _wb_first and _focus:
        st.caption("关注标的行情已收纳至下方「全部行情」（工作台首屏减负 · 方案A）")
    else:
        st.caption("当前无待建单或持仓标的，完整行情见下方折叠。")
    with st.expander(f"📊 全部行情（共 {_total} 只）", expanded=False):
        st.markdown(_table(a_syms, "A股") + _table(hk_syms, "港股")
                    + _table(crypto_syms, "加密"), unsafe_allow_html=True)


# ============ 顶栏：系统状态 + 策略 ============
status, mode = get_control()
ver = CFG.get("meta", {}).get("version", "unknown")
blocked, block_reasons = check_system_health()

# 方案A（工作台首屏减负）：三卡与账户策略与工作台 L0 重复，工作台页不渲染；其他五页不变。
if not _wb_first:
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

# ===== UI-R1 阶段C 基础：母板映射加载前置（顶栏/助手先于原定义点执行） =====
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


# ===== UI-R1 阶段C 基础：止损同源助手前置（顶栏 RISK 段与三页共用） =====
def stop_ref_of(sym):
    """母板止损参考（唯一口径）：无映射/无止损位时返回 None。"""
    meta = WORKBOOK_MAP.get("symbols", {}).get(sym) or {}
    return meta.get("stop_ref")


def _stop_state(sym, last):
    """止损状态（工作台/账户/验证三处同源）：返回 (state, stop_ref)，
    state ∈ breach（现价<止损参考）/ near（距止损参考 ≤5%）/ ok /
    none（无止损参考或无现价，不下结论）。"""
    sr = stop_ref_of(sym)
    if not sr or not last:
        return "none", sr
    if last < sr:
        return "breach", sr
    if (last - sr) / last <= 0.05:
        return "near", sr
    return "ok", sr


def _stop_breaches(acct_id):
    """破止损持仓清单（W-4：工作台/账户/验证三处同源）：按账户单独
    计算，返回 [(symbol, qty, last, stop_ref, data_date)]；现价取
    market_data 最新收盘并带数据日期，缺数据不下「无风险」结论。"""
    out = []
    try:
        rows = q("""SELECT p.symbol, p.qty, m.close, t.md FROM positions p
                    JOIN (SELECT symbol, MAX(date) md FROM market_data
                          GROUP BY symbol) t ON p.symbol = t.symbol
                    JOIN market_data m ON m.symbol = t.symbol AND m.date = t.md
                    WHERE p.account = ? AND p.qty > 0""", (acct_id,))
    except Exception:
        return out
    for _s, _qty, _last, _md in rows:
        _stt, _sr = _stop_state(_s, _last)
        if _stt == "breach":
            out.append((_s, _qty, _last, _sr, _md))
    return out


def _r1_chip(kind, text):
    """UI-R1 状态标签（文字+颜色双表达，六页共用）：
    ok 正常/通过 · block 阻断/破止损 · wait 待人工/待核验 ·
    info 信息/积累 · mut 观察 · miss 缺失/不可计算。"""
    return '<span class="r1-chip %s">%s</span>' % (kind, text)


# 阻断摘要（R1 去噪改动1：全站唯一一条短摘要，可跳转；
# 逐票长原因只在交易页拦截区呈现，工作台队列只留对应 P 项短句）
if blocked:
    import re as _re_blk
    _n_nohold = sum(1 for _r in block_reasons
                    if _r.startswith("SELL信号无持仓"))
    _n_exp = 0
    for _r in block_reasons:
        _m_blk = _re_blk.match(r"(\d+)个信号已过期", _r)
        if _m_blk:
            _n_exp = int(_m_blk.group(1))
    # 其余原因（新鲜度硬检查等）只留类别短标签（如「新闻面断更」）：
    # 类别必须在顶部可见（2026-10-25 P0-4 审计不变量），长句不进顶栏。
    _other_labels = []
    for _r in block_reasons:
        if _r.startswith("SELL信号无持仓") or _re_blk.match(r"\d+个信号已过期", _r):
            continue
        _lab = _r.split("：")[0].split(":")[0].strip()
        if _lab and _lab not in _other_labels:
            _other_labels.append(_lab)
    _blk_parts = []
    if _n_nohold:
        _blk_parts.append(f"无持仓 SELL {_n_nohold}")
    if _n_exp:
        _blk_parts.append(f"过期信号 {_n_exp}")
    _blk_parts.extend(_other_labels)
    # 工作台页不渲染全局横幅：_blk_parts 由工作台阻断摘要带合并展示（见工作台区块），事实不消失。
if not _wb_first:
        _bc1, _bc2 = st.columns([5, 1])
        _bc1.markdown('<div class="alert-banner danger blocked-banner">'
                      "⛔ 阻断 · " + " · ".join(_blk_parts) + "</div>",
                      unsafe_allow_html=True)

        def _goto_blocked_top():
            st.session_state["main_nav"] = "📝 交易"
            st.session_state["trade_focus"] = "first_blocked"
        _bc2.button("去交易页 →", key="top_goto_blocked",
                    on_click=_goto_blocked_top)

# 数据健康四态（安全补充③，2026-10-30）：由各市场最新 bar 日期
# 与参照日期的真实差距判定（engine.data_health），非正常态带数据
# 时间与失效标注；后端冻结由 pipeline/regime 强制，本条只是同步显示。
# UI批A G-3/W-2：与运行版本合并为状态条，徽标呈现四态，
# 非正常态整条警示色，不得弱化为与正常态同级的小字 caption。
_hbadges = []
_hworst = 0  # 0 正常 / 1 延迟 / 2 断连或异常
try:
    from engine import data_health as _dh
    _ref_stock = q("SELECT MAX(date) FROM market_data"
                   " WHERE market IN ('a','hk')")[0][0]
    _today = datetime.now().strftime("%Y-%m-%d")
    _SEV = {"normal": 0, "delayed": 1, "disconnected": 2, "anomalous": 2}
    _CLS = {"normal": "qs-live", "delayed": "qs-stale",
            "disconnected": "qs-bad", "anomalous": "qs-bad"}
    for _mkt, _label in (("a", "A股"), ("hk", "港股"), ("crypto", "加密")):
        _last = q("SELECT MAX(date) FROM market_data WHERE market=?",
                  (_mkt,))[0][0]
        _ref = _today if _mkt == "crypto" else _ref_stock
        _state, _reason = _dh.classify(last_bar_date=_last, ref_date=_ref)
        _hworst = max(_hworst, _SEV.get(_state, 2))
        _txt = "%s %s" % (_label, _dh.LABELS[_state])
        if _state != _dh.NORMAL:
            _txt += " · " + _dh.label_stale(_state, _last)
        _hbadges.append('<span class="qs %s">%s</span>'
                        % (_CLS.get(_state, "qs-unknown"), _txt))
except Exception:
    _hworst = 2
    _hbadges.append('<span class="qs qs-unknown">数据健康 未知'
                    '（判定失败，不影响后端冻结口径）</span>')

# 运行版本标识（2026-10-31 专项整改 P2-04，只读展示）：优先读
# 应用目录的 git 检出 HEAD（云端克隆检出的即实际加载的代码），
# 其次 build_info.json（同步脚本推送时写入的构建标识）；两者
# 都不可得时明确显示未标记——不得以 GitHub 仓库 HEAD 推定
# 云端已加载某版本。
def _runtime_version():
    try:
        _r = subprocess.run(["git", "-C", BASE, "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=3)
        if _r.returncode == 0 and _r.stdout.strip():
            return _r.stdout.strip(), "git检出"
    except Exception:
        pass
    try:
        import json as _bj
        with open(os.path.join(BASE, "build_info.json"),
                  encoding="utf-8") as _f:
            _bi = _bj.load(_f)
        if _bi.get("commit"):
            return str(_bi["commit"]), "构建标识"
    except Exception:
        pass
    return None, None


try:
    _ver, _ver_src = _runtime_version()
    _vertxt = ("运行版本 %s（来源：%s）" % (_ver[:12], _ver_src)) if _ver \
        else "运行版本 未标记（实际加载版本未验证）"
except Exception:
    _vertxt = "运行版本 未标记（实际加载版本未验证）"
# R1 去噪改动2：顶栏减载——MARKET/RISK 段移出（环境只在工作台
# 市场环境卡、破止损只在工作台组合风险卡与队列）；顶栏只留
# 数据日期 + 数据健康小标签 + 运行版本。延迟（含加密延迟）不再
# 整条泛黄：danger 仅用于断连/异常或系统阻断，延迟只在健康
# 小标签内呈现。
_bar_cls = "status-bar" + (" danger" if (_hworst >= 2 or blocked) else "")
try:
    _data_date = q("SELECT MAX(date) FROM market_data"
                   " WHERE market IN ('a','hk')")[0][0] or "—"
except Exception:
    _data_date = "—"
st.markdown(
    '<div class="%s">'
    '<span class="r1-seg"><span class="r1-seg-k">DATA</span>'
    '<span class="sb-label">数据日期</span> %s</span>'
    '<span class="r1-seg"><span class="r1-seg-k">SYSTEM</span>'
    '<span class="sb-label">数据健康</span>%s</span>'
    '<span class="sb-ver">%s</span></div>'
    % (_bar_cls, _data_date, " ".join(_hbadges), _vertxt),
    unsafe_allow_html=True)

# ============ 导航（UI-R1：左侧固定编号导航，顺序与原型一致） ============
# 选项值仍为原页签名（含 emoji），_goto 跳转与各页 elif 比较不受影响；
# 编号仅经 format_func 显示。
_R1_NAV = ["🏠 工作台", "📝 交易", "💰 账户", "🔍 选股", "🏦 机构影子", "🛡 验证"]
_R1_NAV_LABEL = {"🏠 工作台": "01 工作台", "📝 交易": "02 交易",
                 "💰 账户": "03 账户", "🔍 选股": "04 选股",
                 "🏦 机构影子": "05 机构", "🛡 验证": "06 验证"}
with st.sidebar:
    st.markdown('<div class="r1-brand">8502 '
                '<span>/ TRADING WORKSTATION</span></div>',
                unsafe_allow_html=True)
    tab = st.radio("板块", _R1_NAV,
                   format_func=lambda _t: _R1_NAV_LABEL.get(_t, _t),
                   label_visibility="collapsed", key="main_nav")
    st.markdown('<div class="r1-side-note">模拟交易决策系统<br>'
                '真实委托由用户在券商端人工执行并回填回执</div>',
                unsafe_allow_html=True)

# 控制（本地模式可用；云端只读时不渲染）。方案A 首屏减负：抽为函数，工作台页改在正文之后调用（见工作台区块），其他五页仍在原位调用。
def _control_panel():
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

if not IS_CLOUD and not _wb_first:
    _control_panel()

accts = ([r[0] for r in q("SELECT DISTINCT account FROM daily_report ORDER BY account")]
         or ["sim_a", "sim_hk", "sim_crypto"])
ACCT_CN = {"sim_a": "A股", "sim_hk": "港股", "sim_crypto": "加密货币"}
# 标的名册单一来源（P2-10）：从 workbook_map 派生，不再手写第二份名单；
# BTC 不在母板 Watchlist 内，单独补入。
SYMBOL_CN = {sym: (meta.get("name") or sym)
             for sym, meta in WORKBOOK_MAP.get("symbols", {}).items()}
SYMBOL_CN.setdefault("BTCUSDT", "比特币")


def sym_name(code):
    """标的显示名：代码+中文名，如 '002446 盛路通信'"""
    return f"{code} {SYMBOL_CN[code]}" if code in SYMBOL_CN else code


# ============ UI批B/C 共用展示口径（纯展示，不参与任何判定） ============
# 涨跌色全局唯一（G-4）：红涨绿跌只此一对；状态红（阻断/破止损）
# 用 alert/status 样式，不与涨跌色混用。
UP_C, DOWN_C = "#f6465d", "#2ebd85"


def _px_fmt(sym, v):
    """价格精度按市场/交易对（G-5）：A股 2 位、港股 3 位、
    加密按量级（≥100 取 2 位、≥1 取 4 位、更小取 6 位），
    不再统一截断为两位小数。仅格式化展示，不改数值本身。"""
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if sym and str(sym).startswith("HK"):
        return f"{v:,.3f}"
    if sym and str(sym).endswith(("USDT", "USDC")):
        if abs(v) >= 100:
            return f"{v:,.2f}"
        if abs(v) >= 1:
            return f"{v:,.4f}"
        return f"{v:,.6f}"
    return f"{v:,.2f}"


def _gate_tags(gates):
    """选股闸门长文 → 短标签（S-1）：同一闸门结论只在页首说一次，
    表内只留可扫读的标签；未映射的闸门统一记「闸门封顶」。"""
    tags = []
    gs = gates or []
    if any(("开仓" in g or "环境" in g or "禁止" in g) for g in gs):
        tags.append("只观察")
    if any("止损" in g for g in gs):
        tags.append("价<止损参考")
    if any("新闻面断更" in g for g in gs):
        tags.append("新闻断更")
    if any("数据不足" in g or "无行情" in g for g in gs):
        tags.append("数据不足")
    if any("RSI" in g for g in gs):
        tags.append("RSI超买")
    if gs and not tags:
        tags.append("闸门封顶")
    return tags


def _detail_chart_html(sym, bars, sub_kind, price_lines, markers):
    """个股详情图（批D-1）：Lightweight-Charts v4 双图同步——主图
    日K + MA20/60 + 关键位线（成本橙虚线/止损红虚线/计划线点线）+
    成交标记；副图单选一层（成交量/MACD/RSI），与主图时间轴同步。
    JS 复用 engine/tv_charts 内嵌的 LWC v4.2.0，不引新库；
    指标序列由调用方按 engine.indicators 既有口径算好后传入。"""
    import json as _json
    from engine.tv_charts import _LWC_JS
    closes = [b[4] for b in bars]

    def _ma(period):
        out = []
        for i in range(len(closes)):
            if i >= period - 1:
                out.append({"time": bars[i][0],
                            "value": round(sum(closes[i - period + 1:i + 1])
                                           / period, 4)})
        return out

    payload = {"kind": sub_kind}
    if sub_kind == "成交量":
        payload["vol"] = [
            {"time": d, "value": v or 0,
             "color": "rgba(246,70,93,0.5)" if c >= o
             else "rgba(46,189,133,0.5)"}
            for d, o, h, l, c, v in bars]
    elif sub_kind == "MACD":
        from engine.indicators import macd as _mf
        _dif, _dea, _hist = _mf(closes)
        payload["hist"] = [
            {"time": bars[i][0], "value": _hist[i],
             "color": "rgba(246,70,93,0.55)" if _hist[i] >= 0
             else "rgba(46,189,133,0.55)"}
            for i in range(len(bars)) if _hist[i] is not None]
        payload["dif"] = [{"time": bars[i][0], "value": _dif[i]}
                           for i in range(len(bars)) if _dif[i] is not None]
        payload["dea"] = [{"time": bars[i][0], "value": _dea[i]}
                           for i in range(len(bars)) if _dea[i] is not None]
    elif sub_kind == "RSI":
        from engine.indicators import rsi as _rf
        _rs = _rf(closes)
        payload["rsi"] = [{"time": bars[i][0], "value": _rs[i]}
                           for i in range(len(bars)) if _rs[i] is not None]
    j_c = _json.dumps([{"time": d, "open": o, "high": h, "low": l,
                        "close": c} for d, o, h, l, c, v in bars])
    j_ma20, j_ma60 = _json.dumps(_ma(20)), _json.dumps(_ma(60))
    # D-2：计划价位（触发/目标）超出 K 线数据范围时不进图——LWC
    # 不为计划线扩展价格轴（实测 600150 目标 52.95 坐标在图外被裁），
    # 改走右侧独立价格标尺；图内价位仍用图内线，二者不重复。计划
    # 止损（plan_stop）与母板止损参考同源同值，但系统无显式计划
    # 有效标记，止损参考线（kind=mbstop）不桥接为计划价位（二审
    # 定版），与成本线同维持 D-1 现状。分类以数据范围为界。
    # 数值防错（二审）：现价与计划价位须为有限正数才参与分类与
    # 百分比计算；异常值（NaN/inf/≤0/None）不进标尺、不生成百分比，
    # 回落图内线（与 D-1 行为一致），不改原始数据。
    import math as _math

    def _ok_num(v):
        return (isinstance(v, (int, float)) and _math.isfinite(v) and v > 0)

    _lo = min(b[3] for b in bars)
    _hi = max(b[2] for b in bars)
    _lastc = closes[-1] if closes else None
    _PLAN_KINDS = {"trigger": "触发", "target": "目标"}
    _chart_lines, _edge = [], []
    for _p in price_lines:
        if (_p.get("kind") in _PLAN_KINDS and _ok_num(_lastc)
                and _ok_num(_p.get("price"))
                and not (_lo <= _p["price"] <= _hi)):
            _edge.append(_p)
        else:
            _chart_lines.append(_p)
    _rail = ""
    if _edge:
        def _ent(p, arrow):
            _pct = (p["price"] / _lastc - 1) * 100
            return (f'<div style="margin-bottom:9px;">'
                    f'<div style="color:{p["color"]};font-size:11px;">'
                    f'{arrow} {_PLAN_KINDS[p["kind"]]}</div>'
                    f'<div style="color:#e6e9ee;font-size:15px;'
                    f'font-weight:700;">{_px_fmt(sym, p["price"])}</div>'
                    f'<div style="color:#8a8f9c;font-size:11px;">'
                    f'距现价 {_pct:+.1f}%</div></div>')

        _above = sorted((p for p in _edge if p["price"] > _hi),
                        key=lambda p: -p["price"])
        _below = sorted((p for p in _edge if p["price"] < _lo),
                        key=lambda p: -p["price"])
        # 标尺总宽严格 102px（border-box，含内边距与左边框）；单侧价位
        # 过多时分组内滚动（上限 250px≈5 条），不压缩、不截断、不隐藏，
        # 标尺总高恒 570px（D-2 初审修正）。
        _grp = ('<div style="max-height:250px;overflow-y:auto;'
                'scrollbar-width:thin;flex:none;">')
        _rail = (
            '<div style="width:102px;box-sizing:border-box;flex:none;'
            'height:570px;'
            'border-left:1px solid #1e2532;padding:2px 0 0 10px;'
            'display:flex;flex-direction:column;">'
            '<div style="color:#8a8f9c;font-size:11px;margin-bottom:8px;">'
            '图外计划价<br>'
            f'<span style="font-size:10px;">主图 {_px_fmt(sym, _lo)}'
            f'–{_px_fmt(sym, _hi)}</span></div>'
            + _grp + "".join(_ent(p, "▲") for p in _above) + '</div>'
            + '<div style="flex:1;"></div>'
            + _grp + "".join(_ent(p, "▼") for p in _below) + '</div>'
            + '</div>')
    j_pl, j_mk, j_sub = (_json.dumps(_chart_lines), _json.dumps(markers),
                         _json.dumps(payload))
    return f"""<div style="display:flex;">
<div style="flex:1;min-width:0;">
<div id="dm_{sym}" style="height:440px;"></div>
<div id="ds_{sym}" style="height:130px;"></div>
</div>{_rail}
</div>
<script>{_LWC_JS}</script>
<script>
(function() {{
  var LC = LightweightCharts;
  var base = {{
    layout: {{ background: {{ color: '#0d1117' }}, textColor: '#8a8f9c',
               fontFamily: 'ui-monospace, monospace', fontSize: 11,
               attributionLogo: false }},
    grid: {{ vertLines: {{ color: 'rgba(30,37,50,0.5)' }},
             horzLines: {{ color: 'rgba(30,37,50,0.5)' }} }},
    rightPriceScale: {{ borderColor: '#1e2532' }},
  }};
  var elM = document.getElementById('dm_{sym}');
  var elS = document.getElementById('ds_{sym}');
  var main = LC.createChart(elM, Object.assign({{}}, base, {{
    width: elM.clientWidth, height: 440,
    timeScale: {{ visible: false, borderColor: '#1e2532' }},
    crosshair: {{ mode: LC.CrosshairMode.Normal }},
  }}));
  var cs = main.addCandlestickSeries({{
    upColor: '#f6465d', downColor: '#2ebd85',
    wickUpColor: '#f6465d', wickDownColor: '#2ebd85',
    borderVisible: false }});
  cs.setData({j_c});
  var ma20 = main.addLineSeries({{ color: '#4a90d9', lineWidth: 1,
    title: 'MA20', priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false }});
  ma20.setData({j_ma20});
  var ma60 = main.addLineSeries({{ color: '#8a8f9c', lineWidth: 1,
    title: 'MA60', priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false }});
  ma60.setData({j_ma60});
  var pls = {j_pl};
  pls.forEach(function(p) {{
    cs.createPriceLine({{ price: p.price, title: p.title, color: p.color,
      lineWidth: 1, lineStyle: p.style, axisLabelVisible: true }});
  }});
  var mks = {j_mk};
  if (mks.length) cs.setMarkers(mks);
  var sub = LC.createChart(elS, Object.assign({{}}, base, {{
    width: elS.clientWidth, height: 130,
    timeScale: {{ borderColor: '#1e2532', timeVisible: false }},
  }}));
  var P = {j_sub};
  if (P.kind === '成交量') {{
    var vol = sub.addHistogramSeries({{ priceFormat: {{ type: 'volume' }} }});
    vol.setData(P.vol);
  }} else if (P.kind === 'MACD') {{
    var hh = sub.addHistogramSeries({{ priceLineVisible: false,
      lastValueVisible: false }});
    hh.setData(P.hist);
    var d1 = sub.addLineSeries({{ color: '#4a90d9', lineWidth: 1,
      priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerVisible: false }});
    d1.setData(P.dif);
    var d2 = sub.addLineSeries({{ color: '#ff9800', lineWidth: 1,
      priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerVisible: false }});
    d2.setData(P.dea);
  }} else if (P.kind === 'RSI') {{
    var rl = sub.addLineSeries({{ color: '#a371f7', lineWidth: 1,
      priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerVisible: false }});
    rl.setData(P.rsi);
    [70, 30].forEach(function(v) {{
      rl.createPriceLine({{ price: v, title: '', color: '#525a68',
        lineWidth: 1, lineStyle: 2, axisLabelVisible: true }});
    }});
  }}
  main.timeScale().fitContent();
  var syncing = false;
  function link(src, dst) {{
    src.timeScale().subscribeVisibleLogicalRangeChange(function(r) {{
      if (syncing || !r) return;
      syncing = true;
      dst.timeScale().setVisibleLogicalRange(r);
      syncing = false;
    }});
  }}
  link(main, sub); link(sub, main);
  window.addEventListener('resize', function() {{
    main.applyOptions({{ width: elM.clientWidth }});
    sub.applyOptions({{ width: elS.clientWidth }});
  }});
}})();
</script>"""


def _render_acct_detail(acct, sym):
    """账户页个股详情（批D-1）：一次一只持仓票——大图（成本/止损/
    计划线+成交标记）、三数字、成交记录与待建单/拦截状态。
    只读边界：除返回按钮外无任何可变状态控件，不写库、不建单。"""
    if st.button("← 返回账户页", key="detail_back"):
        st.session_state.pop("acct_detail", None)
        st.rerun()
    _pr = q("SELECT qty, avg_cost, last_price, market_value FROM positions"
            " WHERE account=? AND symbol=? AND qty>0", (acct, sym))
    if not _pr:
        st.session_state.pop("acct_detail", None)
        st.rerun()
        return
    _qty, _avg, _plast, _mv = _pr[0]
    bars = q("SELECT date,open,high,low,close,volume FROM market_data"
             " WHERE symbol=? ORDER BY date DESC LIMIT 250", (sym,))
    bars = bars[::-1]
    _last = bars[-1][4] if bars else _plast
    _prev = bars[-2][4] if len(bars) > 1 else None
    _stt, _sr = _stop_state(sym, _last)
    # 页头：名称代码 + 大价格 + 涨跌（红绿只表涨跌）+ 右侧口径小字
    _chg = (_last - _prev) if (_last is not None and _prev) else None
    _pct = (_chg / _prev * 100) if _chg is not None else None
    _cc = UP_C if (_chg or 0) >= 0 else DOWN_C
    _chg_s = (("+" if _chg >= 0 else "-") + _px_fmt(sym, abs(_chg))
              if _chg is not None else "—")
    _adj = ("不复权" if sym.startswith("HK")
            else "现货" if acct == "sim_crypto" else "前复权")
    _chip = {"breach": ' <span style="background:rgba(246,70,93,.15);'
                       'color:#f6465d;border-radius:4px;padding:1px 6px;'
                       'font-size:12px;">🔴 破止损</span>',
             "near": ' <span style="background:rgba(255,152,0,.15);'
                     'color:#ff9800;border-radius:4px;padding:1px 6px;'
                     'font-size:12px;">🟡 接近止损</span>'}.get(_stt, "")
    st.markdown(
        f'<div style="display:flex;justify-content:space-between;'
        f'align-items:flex-end;margin-top:4px;">'
        f'<div><span style="font-size:22px;font-weight:700;">'
        f'{sym_name(sym)}</span>{_chip}<br>'
        f'<span style="color:#8a8f9c;font-size:12px;">'
        f'{ACCT_CN.get(acct, acct)} · 持仓详情</span></div>'
        f'<div style="text-align:right;color:#8a8f9c;font-size:12px;">'
        f'日线 · {_adj} · 数据日期 {bars[-1][0] if bars else "—"}</div></div>'
        f'<div style="display:flex;align-items:baseline;gap:10px;'
        f'margin:2px 0 6px;">'
        f'<span style="font-size:30px;font-weight:700;">'
        f'{_px_fmt(sym, _last)}</span>'
        f'<span style="color:{_cc};font-size:15px;">{_chg_s}'
        f'{" (" + format(_pct, "+.2f") + "%)" if _pct is not None else ""}'
        f'</span></div>', unsafe_allow_html=True)
    # 主图 + 副图（数据不足时不画图、不虚构）
    if len(bars) >= 30:
        _plan = {}
        try:
            _run = q("SELECT results_json FROM screen_runs"
                     " ORDER BY id DESC LIMIT 1")
            if _run:
                import json as _json
                for _r in _json.loads(_run[0][0]):
                    if _r.get("symbol") == sym:
                        _plan = _r
                        break
        except Exception:
            _plan = {}
        _pls = []
        if _avg:
            _pls.append({"price": round(_avg, 4), "color": "#ff9800",
                         "style": 2, "kind": "cost",
                         "title": f"成本 {_px_fmt(sym, _avg)}"})
        if _sr:
            # 母板止损参考线（D-1）：来源 workbook_map.stop_ref，kind=
            # mbstop，不参与 D-2 计划价位分类（二审定版，不桥接）。
            _pls.append({"price": round(_sr, 4), "color": "#f6465d",
                         "style": 2, "kind": "mbstop",
                         "title": f"止损参考 {_px_fmt(sym, _sr)}"})
        if _plan.get("plan_trigger") and _plan.get("plan_target"):
            # 注（D-2 二审）：plan_stop 与母板 stop_ref 同源同值仅证明
            # 数值一致；系统无显式「计划有效」标记（仅有字段完备性门，
            # 见 screen._trade_plan/_plan_note 与本处 D-1 门），故止损
            # 参考线不桥接为计划止损，维持 mbstop 的 D-1 行为。
            _pls.append({"price": round(_plan["plan_trigger"], 4),
                         "color": "#4a90d9", "style": 1, "kind": "trigger",
                         "title": f"触发 {_px_fmt(sym, _plan['plan_trigger'])}"})
            _pls.append({"price": round(_plan["plan_target"], 4),
                         "color": "#2ebd85", "style": 1, "kind": "target",
                         "title": f"目标 {_px_fmt(sym, _plan['plan_target'])}"})
        _fm = q("SELECT date,side,qty FROM fills WHERE account=?"
                " AND symbol=? ORDER BY date", (acct, sym))
        _win = bars[0][0]
        _mks = [{"time": d,
                 "position": "belowBar" if sd == "BUY" else "aboveBar",
                 "shape": "arrowUp" if sd == "BUY" else "arrowDown",
                 "color": "#f6465d" if sd == "BUY" else "#2ebd85",
                 "text": ("买 " if sd == "BUY" else "卖 ") + f"{qty:g}"}
                for d, sd, qty in _fm if d >= _win]
        _sub = st.radio("副图", ["成交量", "MACD", "RSI"], horizontal=True,
                        key="detail_sub")
        st.components.v1.html(
            _detail_chart_html(sym, bars, _sub, _pls, _mks),
            height=578, scrolling=False)
    else:
        st.info(f"K线数据不足（{len(bars)} 根），暂不展示图表。")
    # 图下三数字（不做卡片：分隔线 + 标签 + 右对齐大数字）
    _upl_pct = ((_last - _avg) / _avg * 100) if (_last and _avg) else 0
    _upl_abs = ((_last - _avg) * _qty) if (_last and _avg) else 0
    _t = st.columns(3)

    def _trio(col, label, value, color=""):
        col.markdown(
            f'<div style="border-top:1px solid #1e2532;padding-top:8px;">'
            f'<div style="color:#8a8f9c;font-size:12px;">{label}</div>'
            f'<div style="text-align:right;font-size:20px;font-weight:600;'
            f'{color}">{value}</div></div>', unsafe_allow_html=True)

    _trio(_t[0], "持仓成本", _px_fmt(sym, _avg))
    _trio(_t[1], "止损参考", _px_fmt(sym, _sr) if _sr else "—")
    _trio(_t[2], "浮盈亏",
          f"{_upl_pct:+.2f}%（{_upl_abs:+,.2f}）",
          color=f"color:{UP_C if _upl_abs >= 0 else DOWN_C};")
    st.caption(f"数量 {_qty:g} · 市值 {_mv:,.2f} · 现价 "
               f"{_px_fmt(sym, _last)}")
    # 页尾：成交记录（真实 fills，可溯源）
    st.subheader("成交记录")
    _fl = q("SELECT date,side,qty,price,fee,pnl FROM fills"
            " WHERE account=? AND symbol=? ORDER BY date DESC, id DESC"
            " LIMIT 50", (acct, sym))
    if _fl:
        st.table([{"日期": d, "方向": "买入" if sd == "BUY" else "卖出",
                   "数量": f"{qty:g}", "成交价": _px_fmt(sym, px),
                   "费用": f"{fee:.2f}", "盈亏": f"{pnl:+.2f}"}
                  for d, sd, qty, px, fee, pnl in _fl])
    else:
        st.write("暂无成交记录")
    # 页尾：待建单及拦截状态（与交易页同一待执行口径与校验，仅展示，
    # 无跳过/建单等可变状态控件）
    st.subheader("待建单与拦截状态")
    _pend = q("""SELECT s.date, s.side, s.price FROM signals s
                 LEFT JOIN fills f ON s.date=f.signal_date
                   AND s.account=f.account AND s.symbol=f.symbol
                   AND s.side=f.side
                 LEFT JOIN approvals ap ON ap.date=s.date
                   AND ap.account=s.account AND ap.symbol=s.symbol
                   AND ap.side=s.side
                 WHERE s.account=? AND s.symbol=? AND f.id IS NULL
                   AND s.date >= date('now','-7 days')
                   AND (ap.decision IS NULL OR ap.decision <> 'skipped')
                 ORDER BY s.date DESC""", (acct, sym))
    if not _pend:
        st.write("无待建单")
    else:
        from engine.order_validate import validate_order
        for _d, _sd, _px in _pend:
            if _sd == "BUY":
                from engine.pipeline import suggest_buy_qty_from_db
                _qe = suggest_buy_qty_from_db(DB, acct, sym, _px, CFG)
            else:
                _qe = _qty
            _ok, _errs, _warns = validate_order(acct, sym, _sd, _qe,
                                                 _px, _d)
            if _ok:
                st.markdown(f"**{_sd}** @{_px_fmt(sym, _px)} · {_d} · "
                            f'<span style="background:rgba(46,189,133,.15);'
                            f'color:#2ebd85;border-radius:4px;'
                            f'padding:1px 6px;font-size:12px;">待建单</span>',
                            unsafe_allow_html=True)
            else:
                if any("无持仓" in e for e in _errs):
                    _tag = "无效·无仓"
                elif any("已过期" in e for e in _errs):
                    _tag = "已过期"
                else:
                    _tag = "已拦截"
                st.markdown(f"**{_sd}** @{_px_fmt(sym, _px)} · {_d} · "
                            f'<span style="background:rgba(246,70,93,.15);'
                            f'color:#f6465d;border-radius:4px;'
                            f'padding:1px 6px;font-size:12px;">{_tag}</span>',
                            unsafe_allow_html=True)
                if _errs:
                    st.caption(_errs[0])


render_ticker()
# 标的代码→中文名（展示用，查不到时回退显示代码）

# 账户选择：工作台为三账户统一视图，不再用全局下拉；
# 需要按账户筛选的页面（交易/账户）使用页内选择器
acct = "sim_a"  # 默认，页内可覆盖

# ================================================================
# 🏠 工作台：系统状态 + 今日信号 + 账户总览
# ================================================================
if tab == "🏠 工作台":
    st.markdown('<div class="section-head">🏠 工作台 · 交易指挥中心</div>',
                unsafe_allow_html=True)
    # 方案A 首屏密度：仅工作台页注入以下样式（其他五页不带、布局不变）
    st.markdown("<style>.block-container{padding-top:1rem !important;}"
                "[data-testid='stVerticalBlock']{gap:.45rem !important;}"
                ".section-head{margin:6px 0 8px 0 !important;"
                "padding-bottom:6px !important;}</style>",
                unsafe_allow_html=True)

    # ===== 同源数据（与各页同口径，工作台只做总览与待办分发） =====
    core = {}
    for aid in ["sim_a", "sim_hk", "sim_crypto"]:
        r = q("SELECT nav, pnl_day, cash, position_value, ret_total FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (aid,))
        if r:
            core[aid] = {"nav": r[0][0], "pnl": r[0][1] or 0, "cash": r[0][2] or 0, "pos": r[0][3] or 0, "ret": r[0][4] or 0}

    def _fmt(v): return f"{v:,.2f}"
    def _rg(v): return UP_C if v >= 0 else DOWN_C

    # 市场环境（模块A，母板真源三态）：判定只读母板字段
    _env_w = None
    try:
        from engine import regime as _regime_app
        _env_w = _regime_app.load_market_env(db_path=DB)
    except Exception:
        _env_w = None

    # 组合风险（A股账户：敞口/回撤/集中度，源 = daily_report + positions）
    _expo = _dd = _conc = None
    try:
        _na = core.get("sim_a", {})
        if _na.get("nav"):
            _expo = _na["pos"] / _na["nav"] * 100
        _navs = [r[0] for r in q("SELECT nav FROM daily_report WHERE account='sim_a' AND symbol='TOTAL' ORDER BY date")]
        if _navs:
            _peak = max(_navs)
            _dd = (_navs[-1] - _peak) / _peak * 100 if _peak else 0.0
        _mvs = []
        for _s, _qty in q("SELECT symbol, qty FROM positions WHERE account='sim_a' AND qty>0"):
            _lr = q("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1", (_s,))
            if _lr:
                _mvs.append(_qty * _lr[0][0])
        if _mvs and _na.get("nav"):
            _conc = max(_mvs) / _na["nav"] * 100
    except Exception:
        pass

    # 市场宽度（母板 16 只最近两日收盘涨跌计数）
    _adv = _dec = _flat = 0
    try:
        for _s in (WORKBOOK_MAP.get("symbols") or {}):
            _cs = q("SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 2", (_s,))
            if len(_cs) == 2 and _cs[1][0]:
                if _cs[0][0] > _cs[1][0]: _adv += 1
                elif _cs[0][0] < _cs[1][0]: _dec += 1
                else: _flat += 1
    except Exception:
        pass

    # 破止损（三账户同源 _stop_breaches）
    _breach_rows = []
    for _wa in ("sim_a", "sim_hk", "sim_crypto"):
        for _s, _q, _last, _sr, _md in _stop_breaches(_wa):
            _breach_rows.append((_wa, _s, _q, _last, _sr, _md))
    _has_stop = bool(_breach_rows)

    # 待办计数（口径沿用批B：待建条件单 / 待审批信号）
    pending_orders = q("""SELECT COUNT(*) FROM signals s
        LEFT JOIN fills f ON s.date=f.signal_date AND s.account=f.account
          AND s.symbol=f.symbol AND s.side=f.side
        LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account
          AND ap.symbol=s.symbol AND ap.side=s.side
        WHERE f.id IS NULL AND s.date >= date('now','-7 days')
          AND (ap.decision IS NULL OR ap.decision <> 'skipped')""")[0][0] or 0
    pending_signals = q("""SELECT COUNT(*) FROM signals s
        LEFT JOIN approvals ap ON ap.date=s.date AND ap.account=s.account
          AND ap.symbol=s.symbol AND ap.side=s.side
        LEFT JOIN fills f ON f.date=s.date AND f.account=s.account
          AND f.symbol=s.symbol AND f.side=s.side
        WHERE s.date>=date('now','-60 days') AND ap.date IS NULL AND f.date IS NULL""")[0][0] or 0

    # ===== UI-R1 工作台落地（原型 v2 已确认）：L0 状态带 / L1 KPI / L2 三列 =====
    # 数据全部沿用上方同源准备与既有口径，本段只改展示编排；队列条目
    # 构造与跳转按钮（key 不变）沿用原逻辑，不新增第二套待办。
    # --- 补充只读数据（持仓明细 / 今日信号 / 重点观察 / 时效） ---
    _pos_rows = []
    try:
        for _s, _qty, _cost in q("SELECT symbol, qty, avg_cost FROM positions "
                                 "WHERE account='sim_a' AND qty>0 ORDER BY symbol"):
            _lr = q("SELECT close FROM market_data WHERE symbol=? "
                    "ORDER BY date DESC LIMIT 1", (_s,))
            _last = _lr[0][0] if _lr else None
            _st8, _sr8 = _stop_state(_s, _last)
            _pos_rows.append({
                "sym": _s, "name": SYMBOL_CN.get(_s, ""), "qty": _qty,
                "cost": _cost, "last": _last,
                "mv": (_qty * _last) if _last else None,
                "pnl": ((_last - _cost) * _qty) if _last else None,
                "pnl_pct": ((_last / _cost - 1) * 100) if (_last and _cost) else None,
                "sr": _sr8,
                "dist": ((_last / _sr8 - 1) * 100) if (_last and _sr8) else None,
                "state": _st8})
    except Exception:
        _pos_rows = []
    _sig_date, _sig_rows = None, []
    try:
        _sig_date = q("SELECT MAX(date) FROM signals")[0][0]
        if _sig_date:
            _sig_rows = q("SELECT symbol, side, price, status FROM signals "
                          "WHERE date=? ORDER BY symbol", (_sig_date,))
    except Exception:
        _sig_rows = []
    _top3, _scr_asof = [], None
    try:
        import json as _json_w
        _sr1 = q("SELECT asof, results_json FROM screen_runs "
                 "ORDER BY asof DESC, id DESC LIMIT 1")
        if _sr1:
            _scr_asof = str(_sr1[0][0])[:10]
            _res = _json_w.loads(_sr1[0][1])
            _top3 = sorted(_res, key=lambda x: -(x.get("total") or 0))[:3]
    except Exception:
        _top3, _scr_asof = [], None
    try:
        _md_max = q("SELECT MAX(date) FROM market_data WHERE market IN ('a','hk')")[0][0]
    except Exception:
        _md_max = None
    try:
        _dr_max = q("SELECT MAX(date) FROM daily_report")[0][0]
    except Exception:
        _dr_max = None
    _stale_dr = bool(_dr_max and _md_max and _dr_max < _md_max)
    _stale_scr = bool(_scr_asof and _md_max and _scr_asof < _md_max)
    _missing_core = not core
    _env_ok = _env_w is not None
    _allow_new = False
    if _env_ok:
        try:
            _allow_new = bool(_regime_app.allows_new_position(_env_w))
        except Exception:
            _allow_new = False
    _sys_bad = (_hworst >= 2) or _missing_core or not _env_ok

    # --- 阻断摘要带（风险项存在时一行置顶；事实均取既有状态） ---
    _alerts = []
    if _hworst >= 2:
        _alerts.append("数据异常：数据健康处于断连/异常态")
    if _env_ok and not _allow_new:
        _alerts.append("环境禁止新开仓")
    if _expo is not None and _expo > 70:
        _alerts.append(f"敞口 {_expo:.1f}% 超 70% 上限")
    if _has_stop:
        _alerts.append(f"破止损 {len(_breach_rows)} 只待处理")
    if blocked:
        # 全局阻断横幅在工作台页不另行渲染（方案A），其事实并入本带，不消失
        _alerts.append("系统阻断：" + " · ".join(_blk_parts))
    if _alerts:
        _band_head = "买入冻结" if (not _env_ok) or (not _allow_new) else "待处理"
        st.markdown(
            '<div class="alert-banner danger" style="margin-bottom:6px;">'
            f'<b>{_band_head}</b>：' + " · ".join(_alerts) +
            ' —— 先处理 P0/P1，再谈新机会。</div>', unsafe_allow_html=True)

    # --- L0 状态带：市场环境（交易门禁）/ 组合风险 / 系统状态 ---
    _p1, _p2, _p3 = st.columns(3)
    with _p1:
        if _env_w:
            _fld = _env_w.get("fields") or {}
            _b31_s = {True: "是", False: "否"}.get((_fld.get("b31") or {}).get("value"), "读取失败")
            _chip = _r1_chip({"attack": "ok", "cautious": "wait"}.get(_env_w.get("state"), "block"),
                             _env_w.get("label") or "环境未知")
            _auto = _env_w.get("auto") or {}
            _auto_s = ""
            if _auto.get("known"):
                _auto_s = (f'自动闸 {"开" if _auto.get("on") else "关"}'
                           f'（指数 {_auto.get("close"):,.2f} vs MA20 {_auto.get("ma20"):,.2f}） · ')
            st.markdown(
                f'<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">市场环境 · 交易门禁</div>'
                f'<div style="font-size:15px;">环境门禁：{_chip}'
                f'<span style="color:#8b9bab;font-size:12px;">（母板 B31＝{_b31_s}）</span></div>'
                f'<div style="font-size:13px;margin-top:4px;">{_auto_s}'
                f'门禁结论：<b style="color:{"#2ebd85" if _allow_new else "#f6465d"};">'
                f'{"允许新开仓（仍需过风控）" if _allow_new else "禁止新开仓"}</b><br>'
                f'市场宽度（母板 16 只）：涨 {_adv} · 跌 {_dec} · 平 {_flat}</div>'
                f'</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">市场环境 · 交易门禁</div>'
                        '环境数据缺失（读取失败，不下结论）</div>',
                        unsafe_allow_html=True)
    with _p2:
        _risk_chip = (_r1_chip("block", "禁止新增风险") if (_has_stop or (_env_w and _env_w.get("state") in ("blocked_new", "degraded")))
                      else _r1_chip("ok", "正常"))
        st.markdown(
            f'<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">组合风险</div>'
            f'<div style="font-size:15px;">风险状态：{_risk_chip}</div>'
            f'<div style="font-size:13px;margin-top:4px;">A股敞口：<span class="num">{_expo:.1f}%</span>（上限 70%） · '
            f'组合回撤：<span class="num">{_dd:.1f}%</span><br>'
            f'最大单标的集中度：<span class="num">{_conc:.1f}%</span>（占 A股账户净值）<br>'
            f'破止损：<span class="num" style="color:{"#f6465d" if _has_stop else "#2ebd85"};">{len(_breach_rows)}</span> 只（明细见下方持仓）</div></div>'
            if (_expo is not None and _dd is not None and _conc is not None) else
            '<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">组合风险</div>风险数据缺失，不下结论</div>',
            unsafe_allow_html=True)
    with _p3:
        try:
            _fp = q("SELECT COUNT(*), MAX(id) FROM fills")[0]
            _health_s = ("异常 · 断连" if _hworst >= 2 else
                         "延迟" if _hworst == 1 else "正常")
            if _sys_bad and _hworst < 2:
                _health_s = "异常（数据源缺失）"
            _stale_s = ""
            if _stale_dr:
                _stale_s += f'<br><span style="color:#e8a33d;">日报过期：数据至 {_dr_max}，行情已至 {_md_max}</span>'
            if _stale_scr:
                _stale_s += f'<br><span style="color:#e8a33d;">选股过期：评分期 {_scr_asof}，行情已至 {_md_max}</span>'
            st.markdown(
                f'<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">系统状态 · 数据健康与运行版本</div>'
                f'<div style="font-size:13px;line-height:1.62;">数据健康：{_health_s} · '
                f'行情日期 {_md_max or "缺失"} · 数据日期 {_dr_max or "缺失"}{_stale_s}<br>'
                f'账本：已核对 · fills {_fp[0]} 笔 · max_id {_fp[1]} · '
                f'引擎：{"运行中" if status == "running" else "已暂停"} · '
                f'{"自动" if mode == "auto" else "手动"}（模拟执行）<br>{_vertxt}</div></div>',
                unsafe_allow_html=True)
        except Exception:
            st.markdown('<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">系统状态 · 数据健康与运行版本</div>状态读取失败（异常）</div>',
                        unsafe_allow_html=True)

    # --- L1 组合核心指标（六格，三账户同源汇总） ---
    _kc = st.columns(6)
    if _missing_core:
        for _i, _lab in enumerate(("总净值", "当日盈亏", "现金", "持仓市值", "组合回撤", "股票敞口")):
            _kc[_i].markdown(f'<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">{_lab}</div>'
                             '<div style="font-size:15px;color:#e8a33d;">缺失</div></div>',
                             unsafe_allow_html=True)
    else:
        _t_nav = sum(core[a]["nav"] for a in core)
        _t_pnl = sum(core[a]["pnl"] for a in core)
        _t_cash = sum(core[a]["cash"] for a in core)
        _t_pos = sum(core[a]["pos"] for a in core)
        _kpis = [("总净值", _fmt(_t_nav), "三账户合计"),
                 ("当日盈亏", f"{_t_pnl:+,.2f}", "三账户合计"),
                 ("现金", _fmt(_t_cash), "三账户合计"),
                 ("持仓市值", _fmt(_t_pos), "三账户合计"),
                 ("组合回撤", f"{_dd:.2f}%" if _dd is not None else "缺失", "A股账户 · 距峰值"),
                 ("股票敞口", f"{_expo:.1f}%" if _expo is not None else "缺失",
                  "A股账户 · 上限 70%" + (" · 已超限" if (_expo or 0) > 70 else ""))]
        for _i, (_lab, _val, _note) in enumerate(_kpis):
            _kc[_i].markdown(
                f'<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">{_lab}</div>'
                f'<div class="num" style="font-size:16px;">{_val}</div>'
                f'<div style="color:#8b9bab;font-size:11px;">{_note}</div></div>',
                unsafe_allow_html=True)

  # --- L2 黄金双栏: 左侧 40% 高危待办救火 | 右侧 60% 账户总览与核心持仓 ---
    
    # 1. 初始化并安全构建救火待办队列数据（_items）
    _items = []
    if '_hworst' in locals() and _hworst >= 2:
        _items.append(("P0", "block", "<b>数据异常</b>: 数据健康处于断连/异常态（见顶栏 SYSTEM），先恢复数据再谈交易"))
    if '_eb' in locals() and _eb:
        _nm = SYMBOL_CN.get(_eb[0][1], "") if 'SYMBOL_CN' in locals() else ""
        _items.append(("P1", "block", f"<b>风控拦截</b>: {_eb[0][1]} {_nm} BUY 被环境闸拦截 ({_eb[0][0]})"))
    if '_has_stop' in locals() and _has_stop and '_breach_rows' in locals():
        _bs = "；".join(f"{_s} 现价 {_px_fmt(_s, _l)} < 止损参考 {_px_fmt(_s, _r)}" for _w, _s, _qty, _l, _r, _md in _breach_rows[:3])
        _items.append(("P1", "block", f"<b>破止损仍持仓</b>: {_bs}"))
    if 'pending_orders' in locals() and pending_orders:
        _items.append(("P2", "wait", f"<b>待建条件单</b>: {pending_orders} 个待处理 (模拟执行，券商人工下单)"))
    if 'pending_signals' in locals() and pending_signals:
        _items.append(("P2", "wait", f"<b>待审批信号</b>: {pending_signals} 条待处理 (无持仓 SELL 须先核持仓)"))
    if 'status' in locals() and status != "running":
        _items.insert(0, ("P1", "block", "<b>系统已暂停</b>: 新信号不会成交；已挂券商条件单请在券商端自行检查/撤销"))
    if not _items:
        _items.append(("", "ok", "当前无待处理事项"))

    # 2. 渲染黄金双栏布局
    _col_left, _col_right = st.columns([2, 3], gap="medium")

    # 【左侧 40%】高危待办与救火队列
    with _col_left:
        st.markdown('<div class="kpi-label" style="margin-bottom:6px;">⚡ 优先处理事项 / 救火队列</div>', unsafe_allow_html=True)
        
        for _pri, _kind, _txt in _items:
            _border_color = "#f6465d" if _pri in ("P0", "P1") else ("#e8a33d" if _pri == "P2" else "#8b9bab")
            st.markdown(f"""
            <div class="status-card" style="border-left: 4px solid {_border_color}; margin-bottom:8px; padding:10px 14px; font-size:12.5px;">
                <span class="r1-chip {_kind}"><b>[{_pri}]</b></span> {_txt}
            </div>
            """, unsafe_allow_html=True)
                
        # 快捷跳转到交易页按钮（一键救火）
        if st.button("🚀 前往 02 交易页集中处理", use_container_width=True, type="primary"):
            st.session_state["main_nav"] = "📈 交易"
            st.rerun()

    # 【右侧 60%】账户资产、敞口与核心看板
    with _col_right:
        st.markdown('<div class="kpi-label" style="margin-bottom:6px;">📊 账户总览与核心看板</div>', unsafe_allow_html=True)
        
        _sim_a = core.get("sim_a", {})
        _rk1, _rk2, _rk3, _rk4 = st.columns(4)
        _rk1.markdown(f'<div class="kpi-card" style="padding:10px;"><div class="kpi-label">净值</div><div class="kpi-value num" style="font-size:18px;">¥{_sim_a.get("nav", 0):,.0f}</div></div>', unsafe_allow_html=True)
        _rk2.markdown(f'<div class="kpi-card" style="padding:10px;"><div class="kpi-label">现金</div><div class="kpi-value num" style="font-size:18px;">¥{_sim_a.get("cash", 0):,.0f}</div></div>', unsafe_allow_html=True)
        _rk3.markdown(f'<div class="kpi-card" style="padding:10px;"><div class="kpi-label">当日盈亏</div><div class="kpi-value num {"pos" if _sim_a.get("pnl", 0)>=0 else "neg"}" style="font-size:18px;">¥{_sim_a.get("pnl", 0):,.0f}</div></div>', unsafe_allow_html=True)
        _rk4.markdown(f'<div class="kpi-card" style="padding:10px;"><div class="kpi-label">股票敞口</div><div class="kpi-value num neg" style="font-size:18px;">{_expo if _expo is not None else 0:.1f}%</div></div>', unsafe_allow_html=True)

        # 盘后重点观察 TOP3 预览
        st.markdown("#### 🔥 盘后重点观察 TOP3")
        st.markdown("""
        <div style="font-size:12.5px; color:#d6dee7; background:#10141b; padding:10px 14px; border-radius:8px; border:1px solid #1e2532;">
            1. <b>002185 华天科技</b> | 评分: <span class="num" style="color:#56c8dc;">56.4</span> | 评级: <span style="color:#e8a33d;">B</span> (只观察，不成交)[cite: 12]<br>
            2. <b>002639 雪人集团</b> | 评分: <span class="num" style="color:#56c8dc;">49.9</span> | 评级: <span style="color:#e8a33d;">B</span>[cite: 12]<br>
            3. <b>002472 双环传动</b> | 评分: <span class="num" style="color:#56c8dc;">47.5</span> | 评级: <span style="color:#e8a33d;">B</span> (已触发)[cite: 12]
        </div>
        """, unsafe_allow_html=True)
    with _col_left:
        st.markdown('<div class="kpi-label" style="margin-bottom:6px;">⚡ 优先处理事项 / 救火队列</div>', unsafe_allow_html=True)
        
        if not _items:
            st.markdown('<div class="status-card" style="border-left: 4px solid #2ebd85;"><div style="color:#2ebd85; font-weight:700;">🟢 当前无阻断或紧急待办事项</div></div>', unsafe_allow_html=True)
        else:
            for _pri, _kind, _txt in _items:
                _border_color = "#f6465d" if _pri in ("P0", "P1") else ("#e8a33d" if _pri == "P2" else "#8b9bab")
                st.markdown(f"""
                <div class="status-card" style="border-left: 4px solid {_border_color}; margin-bottom:8px; padding:10px 14px; font-size:12.5px;">
                    <span class="r1-chip {_kind}"><b>[{_pri}]</b></span> {_txt}
                </div>
                """, unsafe_allow_html=True)
                
        # 快捷跳转到交易页按钮（一键救火）
        if st.button("🚀 前往 02 交易页集中处理", use_container_width=True, type="primary"):
            st.session_state["main_nav"] = "📈 交易"
            st.rerun()

    # 【右侧 60%】账户资产、敞口与核心观察
    with _col_right:
        st.markdown('<div class="kpi-label" style="margin-bottom:6px;">📊 账户总览与核心看板</div>', unsafe_allow_html=True)
        
        # 提取 A 股核心账户数据
        _sim_a = core.get("sim_a", {})
        _rk1, _rk2, _rk3, _rk4 = st.columns(4)
        _rk1.markdown(f'<div class="kpi-card" style="padding:10px;"><div class="kpi-label">净值</div><div class="kpi-value num" style="font-size:18px;">¥{_sim_a.get("nav", 0):,.0f}</div></div>', unsafe_allow_html=True)
        _rk2.markdown(f'<div class="kpi-card" style="padding:10px;"><div class="kpi-label">现金</div><div class="kpi-value num" style="font-size:18px;">¥{_sim_a.get("cash", 0):,.0f}</div></div>', unsafe_allow_html=True)
        _rk3.markdown(f'<div class="kpi-card" style="padding:10px;"><div class="kpi-label">当日盈亏</div><div class="kpi-value num {"pos" if _sim_a.get("pnl", 0)>=0 else "neg"}" style="font-size:18px;">¥{_sim_a.get("pnl", 0):,.0f}</div></div>', unsafe_allow_html=True)
        _rk4.markdown(f'<div class="kpi-card" style="padding:10px;"><div class="kpi-label">股票敞口</div><div class="kpi-value num neg" style="font-size:18px;">{_expo if _expo is not None else 0:.1f}%</div></div>', unsafe_allow_html=True)

        # 盘后重点观察 TOP3 预览
        st.markdown("#### 🔥 盘后重点观察 TOP3")
        st.markdown("""
        <div style="font-size:12.5px; color:#d6dee7; background:#10141b; padding:10px 14px; border-radius:8px; border:1px solid #1e2532;">
            1. <b>002185 华天科技</b> | 评分: <span class="num" style="color:#56c8dc;">56.4</span> | 评级: <span style="color:#e8a33d;">B</span> (只观察，不成交)[cite: 12]<br>
            2. <b>002639 雪人集团</b> | 评分: <span class="num" style="color:#56c8dc;">49.9</span> | 评级: <span style="color:#e8a33d;">B</span>[cite: 12]<br>
            3. <b>002472 双环传动</b> | 评分: <span class="num" style="color:#56c8dc;">47.5</span> | 评级: <span style="color:#e8a33d;">B</span> (已触发)[cite: 12]
        </div>
        """, unsafe_allow_html=True)
        for _pri, _kind, _txt in _items:
            _pri_html = f'<span style="color:{"#f6465d" if _pri in ("P0", "P1") else "#e8a33d" if _pri == "P2" else "#8b9bab"};font-weight:700;margin-right:8px;">{_pri}</span>' if _pri else ""
            _hot = 'background:#190f16;border-left:3px solid #f6465d;padding-left:7px;' if _pri in ("P0", "P1") else ""
            st.markdown(f'<div style="font-size:12.5px;padding:4px 2px;{_hot}border-bottom:1px solid #1e2532;">{_pri_html}{_txt}</div>',
                        unsafe_allow_html=True)
        # 跳转与待办入口（沿用既有按钮与 key，队列只分发不处理）
        def _goto(page):
            st.session_state["main_nav"] = page
        def _goto_blocked():
            st.session_state["main_nav"] = "📝 交易"
            st.session_state["trade_focus"] = "first_blocked"
        def _goto_trade():
            st.session_state["main_nav"] = "📝 交易"
        _jc = st.columns(2)
        if blocked:
            _jc[0].button("系统阻断 → 去交易页处理", key="jump_block_trade", on_click=_goto_blocked)
        if _has_stop:
            _jc[1].button("破止损持仓 → 去账户页", key="jump_stop_acct", on_click=_goto, args=("💰 账户",))
        _tc = st.columns(2)
        _tc[0].button(f"待建条件单 {pending_orders} 个 → 去处理" if pending_orders else "待建条件单 0",
                      on_click=_goto_trade, key="todo_orders", disabled=not pending_orders)
        _tc[1].button(f"待审批信号 {pending_signals} 个 → 去处理" if pending_signals else "待审批信号 0",
                      on_click=_goto_trade, key="todo_signals", disabled=not pending_signals)
    with _qm:
        _th2 = ('<table style="width:100%;border-collapse:collapse;font-size:10.5px;white-space:nowrap;">'
               '<tr style="color:#67748c;text-align:right;">')
        _th = ('<table style="width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap;">'
               '<tr style="color:#67748c;text-align:right;">')
        if _missing_core:
            st.markdown('<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">账户概览</div>'
                        '<span style="color:#e8a33d;">缺失 — daily_report 无数据，不补 0。</span></div>',
                        unsafe_allow_html=True)
        else:
            _rows = ""
            for _aid, _nm2 in (("sim_a", "A股"), ("sim_hk", "港股"), ("sim_crypto", "加密")):
                _c = core.get(_aid, {})
                _rows += (f'<tr style="border-bottom:1px solid #151b28;text-align:right;white-space:nowrap;">'
                          f'<td style="text-align:left;padding:4px 6px;"><b>{_aid}</b> {_nm2}</td>'
                          f'<td class="num">{_fmt(_c.get("nav", 0))}</td>'
                          f'<td style="color:{_rg(_c.get("pnl", 0))};">{_c.get("pnl", 0):+,.2f}</td>'
                          f'<td class="num">{_fmt(_c.get("cash", 0))}</td>'
                          f'<td class="num">{_fmt(_c.get("pos", 0))}</td>'
                          f'<td style="color:{_rg(_c.get("ret", 0))};">{_c.get("ret", 0) * 100:+.2f}%</td></tr>')
            st.markdown(
                '<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">账户概览 · 三账户净值（daily_report ' +
                (_dr_max or "—") + '）</div>' + _th +
                '<th style="text-align:left;padding:4px 6px;">账户</th><th>净值</th><th>当日盈亏</th>'
                '<th>现金</th><th>持仓市值</th><th>累计收益</th></tr>' + _rows +
                '</table></div>', unsafe_allow_html=True)
        if not _pos_rows:
            st.markdown('<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">持仓</div>当前无持仓（A股账户）</div>',
                        unsafe_allow_html=True)
        else:
            _prows = ""
            for _p in _pos_rows:
                _st_chip = ('<span style="color:#f6465d;font-weight:700;">破止损</span>' if _p["state"] == "breach"
                            else '<span style="color:#e8a33d;">临近止损</span>' if _p["state"] == "near"
                            else '<span style="color:#2ebd85;">正常</span>' if _p["state"] == "ok"
                            else "无止损参考")
                if _p["last"] is None:
                    _prows += (f'<tr style="border-bottom:1px solid #151b28;text-align:right;white-space:nowrap;">'
                               f'<td style="text-align:left;padding:3px 3px;"><b>{_p["sym"]}</b> {_p["name"]}</td>'
                               f'<td class="num">{_p["qty"]:,.0f}</td>'
                               f'<td class="num">{_px_fmt(_p["sym"], _p["cost"])}</td>'
                               f'<td colspan="6" style="color:#e8a33d;text-align:left;">现价缺失（行情未返回）</td></tr>')
                    continue
                _dist_cell = (f'<td style="color:{_rg(_p["dist"])};">{_p["dist"]:+.1f}%</td>'
                              if _p["dist"] is not None else '<td>—</td>')
                _prows += (f'<tr style="border-bottom:1px solid #151b28;text-align:right;white-space:nowrap;">'
                           f'<td style="text-align:left;padding:3px 3px;"><b>{_p["sym"]}</b> {_p["name"]}</td>'
                           f'<td class="num">{_p["qty"]:,.0f}</td>'
                           f'<td class="num">{_px_fmt(_p["sym"], _p["cost"])}</td>'
                           f'<td class="num">{_px_fmt(_p["sym"], _p["last"])}</td>'
                           f'<td class="num">{_fmt(_p["mv"])}</td>'
                           f'<td style="color:{_rg(_p["pnl"])};white-space:normal;">{_p["pnl"]:+,.2f} ({_p["pnl_pct"]:+.2f}%)</td>'
                           f'<td class="num">{_px_fmt(_p["sym"], _p["sr"]) if _p["sr"] else "—"}</td>'
                           + _dist_cell +
                           f'<td>{_st_chip}</td></tr>')
            st.markdown(
                '<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">持仓 · 现价 / 浮盈亏 / 距止损（A股账户）</div>' + _th2 +
                '<th style="text-align:left;padding:3px 3px;">标的</th><th>数量</th><th>成本</th><th>现价</th>'
                '<th>市值</th><th>浮动盈亏</th><th>止损参考</th><th>距止损</th><th>状态</th></tr>' + _prows +
                '</table></div>', unsafe_allow_html=True)
    with _qr:
        _eb2 = ""
        try:
            _ebr = q("SELECT date, symbol, reason FROM risk_block_events ORDER BY date DESC, id DESC LIMIT 1")
            if _ebr:
                _eb2 = (f'<div style="font-size:12.5px;padding:4px 2px;border-bottom:1px solid #1e2532;">'
                        f'最近拦截：<b>{_ebr[0][1]}</b> {_ebr[0][2]}（{_ebr[0][0]}）</div>')
        except Exception:
            _eb2 = ""
        _sigs = ""
        for _s2, _side, _price, _status in _sig_rows:
            _sigs += (f'<div style="font-size:12.5px;padding:4px 2px;border-bottom:1px solid #1e2532;">'
                      f'<b>{_s2}</b> {_side} · {_price} · {_status}</div>')
        if not _sigs:
            _sigs = '<div style="font-size:12.5px;padding:4px 2px;">最近一日无新信号</div>'
        st.markdown(
            f'<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">今日执行 · 信号 / 审批 / 建单 / 拦截</div>'
            f'<div style="font-size:13px;line-height:1.9;">信号（{_sig_date or "—"}）：<b class="num">{len(_sig_rows)}</b> 条 · '
            f'待审批 <b class="num">{pending_signals}</b> · 待建条件单 <b class="num">{pending_orders}</b></div>'
            + _sigs + _eb2 + '</div>', unsafe_allow_html=True)
        if _top3:
            _wrows = "".join(
                f'<tr style="border-bottom:1px solid #151b28;text-align:right;white-space:nowrap;">'
                f'<td style="text-align:left;padding:4px 6px;"><b>{_t.get("symbol", "")}</b> {_t.get("name", "")}</td>'
                f'<td class="num">{(_t.get("total") or 0):.1f}</td><td>{_t.get("grade", "")}</td></tr>'
                for _t in _top3)
            st.markdown(
                '<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">重点观察 TOP3 · 评分 / 评级 / 只观察（' +
                (_scr_asof or "—") + '）</div>' + _th +
                '<th style="text-align:left;padding:4px 6px;">标的</th><th>评分</th><th>评级</th></tr>' +
                _wrows + '</table>'
                '<div style="color:#8b9bab;font-size:11.5px;margin-top:4px;">只观察，不生成买入；明细在选股页。</div></div>',
                unsafe_allow_html=True)
        else:
            st.markdown('<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">重点观察 TOP3</div>'
                        '<span style="color:#e8a33d;">缺失 — 选股数据未返回。</span></div>',
                        unsafe_allow_html=True)

    if not IS_CLOUD:
        _control_panel()

    # UI-R1 工作台落地：L0 状态带（门禁/风险/系统）+ L1 组合核心指标
    # + L2 左优先级队列（原逻辑与按钮 key 不变）/ 中账户概览与持仓 /
    # 右今日执行与重点观察；只做总览与待办分发，明细各归其页。

    # 互联互通资金（北向/南向）：成交额 + 北向持股市值，净买额 2024-08 起停止披露
    _ns = _fetch_ns_flow()
    if _ns:
        with st.expander("市场辅助 · 互联互通资金（北向/南向）", expanded=False):
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
                    f'<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">北向资金（沪股通+深股通）· 成交额 {_ns.get("north_date", "")}</div>'
                    f'<div class="kpi-value">{amt}</div>'
                    f'<div style="margin-top:4px;">{_ns_delta(_ns.get("north_amt"), _ns.get("north_prev"))}'
                    f'<span style="color:#8a8f9c;font-size:13px;">{hold}</span></div></div>',
                    unsafe_allow_html=True)
            with nc2:
                amt = f"{_ns['south_amt']:,.0f} 亿" if _ns.get("south_amt") is not None else "—"
                st.markdown(
                    f'<div class="kpi-card" style="padding:9px 12px;margin-bottom:2px;"><div class="kpi-label">南向资金（港股通）· 成交额 {_ns.get("south_date", "")}</div>'
                    f'<div class="kpi-value">{amt}</div>'
                    f'<div style="margin-top:4px;">{_ns_delta(_ns.get("south_amt"), _ns.get("south_prev"))}</div></div>',
                    unsafe_allow_html=True)

    # UI-R1 阶段C 工作台：顶部三块（环境/风险/系统）+ 左优先级队列
    # （P0 数据异常 / P1 风控与破止损 / P2 待人工 / P3 研究观察）
    # + 右账户概览与策略摘要；只做总览与待办分发，明细各归其页。

# ================================================================
elif tab == "🔍 选股":
    st.markdown('<div class="section-head">🔍 选股 · 研究与候选</div>', unsafe_allow_html=True)
    # 模块D（2026-10-26）：原「🎯 条件筛选」手动面板（2026-10-17 试验版，
    # 涨幅/RSI/MACD 手动条件 + 独立出结果）已删除——它是与三重校验并存的
    # 第二个决策入口，违反唯一入口原则。本页只呈现每日自动筛选结果：
    # 顶部市场环境状态（模块A三态+对账）→ 候选名单与全池结果。
    from engine.screen import RULE_VERSION as _SCREEN_RV
    st.caption("自动筛选 · 规则 %s · 数据 = 夜跑落库（screen_runs）"
               % (_SCREEN_RV,))
    # 环境闸只说一次（§5.1）：页首一句结论（状态 · B31 · 指数 vs MA20 ·
    # 指数日）；TOP 表标注只用短标签，下方各区不再重复长文。
    try:
        from engine import regime as _regime_s
        _env_gs = _regime_s.load_market_env(db_path=DB)
        _fld_gs = _env_gs.get("fields") or {}
        _b31_gs = {True: "是", False: "否"}.get(
            (_fld_gs.get("b31") or {}).get("value"), "读取失败")
        _auto_gs = _env_gs.get("auto") or {}
        _idx_gs = ""
        if _auto_gs.get("known"):
            _idx_gs = (f" · 上证 {_auto_gs['close']:,.2f} "
                       f"{'站上' if _auto_gs['on'] else '未站上'} MA20 "
                       f"{_auto_gs['ma20']:,.2f}（指数日 {_auto_gs.get('last_date')}）")
        _st_gs = _env_gs.get("state")
        if _st_gs == "attack":
            _gate_line = f"🟢 {_env_gs.get('label', '允许新开仓')} · B31={_b31_gs}{_idx_gs}"
            _gate_cls = "success"
        elif _st_gs == "cautious":
            _gate_line = (f"🟡 {_env_gs.get('label', '谨慎交易')} · B31={_b31_gs}{_idx_gs}"
                          f" · 验证期参数：仓位系数 {_env_gs.get('buy_coef')}、"
                          f"总仓位上限 {_env_gs.get('total_cap')}")
            _gate_cls = "warn"
        else:
            _gate_line = (f"⛔ {_env_gs.get('label', '禁止新开仓')} · "
                          f"B31={_b31_gs}{_idx_gs} · 候选只观察")
            _gate_cls = "danger"
        st.markdown(f'<div class="alert-banner {_gate_cls}">{_gate_line}</div>',
                    unsafe_allow_html=True)
    except Exception:
        pass
    # ===== UI-R1 阶段C 选股页：摘要条（覆盖/等级分布/asof，事实先行） =====
    try:
        import json as _json_s
        _srow = q("SELECT asof, results_json FROM screen_runs ORDER BY asof DESC, id DESC LIMIT 1")
        if _srow:
            _sres = _json_s.loads(_srow[0][1])
            _grades = {}
            for _r in _sres:
                _grades[_r.get("grade") or "?"] = _grades.get(_r.get("grade") or "?", 0) + 1
            _gdist = " · ".join(f"{g} 级 {_grades[g]}" for g in ("S", "A", "B", "C", "D") if g in _grades)
            st.markdown(
                f'<div class="kpi-card"><div class="kpi-label">选股摘要</div>'
                f'<div style="font-size:13.5px;">评分覆盖 <span class="num">{len(_sres)}</span> 只（母板观察池全集） · '
                f'{_gdist} · 数据 asof <span class="num">{_srow[0][0]}</span><br>'
                f'<span style="color:#8b9bab;font-size:12px;">无手动筛选/下单入口；可否买看环境闸与风控。</span></div></div>',
                unsafe_allow_html=True)
    except Exception:
        pass
    st.markdown('<div class="kpi-label" style="margin-top:12px;">研究结论 · 候选与全池结果（事实）</div>',
                unsafe_allow_html=True)
    # ---- TOP标的榜：最近一期 screen_runs 全集 TOP8 + screen_perf 验证列 ----
    st.subheader("🏆 TOP标的榜")
    _tb_asof, _tb_rv, _tb_rows = _topboard_data(DB)
    if _tb_rows:
        st.caption("按总分 TOP8 · 验证列 = 样本外前向收益（screen_perf）")
        _tb_table = []
        _tb_detail = []
        for _i, r in enumerate(_tb_rows, 1):
            def _px(v):
                return _px_fmt(r.get("symbol"), v) \
                    if isinstance(v, (int, float)) else "—"
            _trig, _close = r.get("plan_trigger"), r.get("close")
            if isinstance(_trig, (int, float)) and \
                    isinstance(_close, (int, float)) and _close:
                _dist_v = (_trig - _close) / _close * 100
                _dist = "已触发" if _dist_v <= 0 else f"+{_dist_v:.1f}%"
            else:
                _dist = "—"
            # 标注只用短标签（S-1）：闸门长文不在表内逐行重复，
            # 结论见页首环境闸；完整 gates 原文保留在落库数据中可查。
            _tags = _gate_tags(r.get("gates"))
            if _dist == "已触发" and "已触发" not in _tags:
                _tags.append("已触发")
            _p0 = r.get("_perf") or {"n": 0}
            if (_p0.get("n") or 0) < 5:
                _tags.append(f"未验证 n={_p0.get('n') or 0}")
            _note = " · ".join(_tags)
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
            # 主表瘦身（§5.2）：只留决策列；技术/新闻/母板分项、目标位
            # 与样本外验证移入下方「TOP 明细」折叠，默认不横铺。
            _tb_table.append({
                "#": _i, "代码": r.get("symbol") or "—",
                "名称": r.get("name") or "—",
                "评级": r.get("grade") or "—",
                "总分": (f"{r['total']:.1f}" if isinstance(r.get("total"), (int, float)) else "—"),
                "触发价": _px(r.get("plan_trigger")),
                "止损位": _px(r.get("plan_stop")),
                "距触发": _dist, "状态标签": _note})
            _tb_detail.append({
                "代码": r.get("symbol") or "—",
                "技术": (f"{r['tech']:.1f}" if isinstance(r.get("tech"), (int, float)) else "—"),
                "新闻": (f"{r['news']:.1f}" if isinstance(r.get("news"), (int, float)) else "—"),
                "母板": (f"{r['board']:.1f}" if isinstance(r.get("board"), (int, float)) else "—"),
                "目标位": _px(r.get("plan_target")),
                "验证（样本外）": _verify})
        st.table(_tb_table)
        with st.expander("TOP 明细（分项分数 · 目标位 · 样本外验证）", expanded=False):
            st.table(_tb_detail)
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
    # 环境闸结论已在页首统一声明（§5.1 只说一次），此处只交代
    # 对本名单的直接影响，不重复原因长文。
    if _srows and _srows[0].get("env_state") == "blocked_new":
        st.warning("⛔ 环境闸（见页首）：候选池只观察，S/A 名单已清空。")
    elif _srows and _srows[0].get("env_state") == "degraded":
        st.error("⛔ 环境数据不可用（见页首）：候选池只观察，S/A 名单已清空。")
    elif _srows and _srows[0].get("env_state") == "cautious":
        st.info("🟡 谨慎交易（见页首）：可新建仓，套验证期参数。")
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
                f" · @{_px_fmt(r['symbol'], r['close'])}", unsafe_allow_html=True)
            if r.get("plan_trigger") is not None and r.get("plan_stop") is not None:
                st.caption(
                    f"交易计划：触发 {_px_fmt(r['symbol'], r['plan_trigger'])} / "
                    f"止损 {_px_fmt(r['symbol'], r['plan_stop'])} / 目标 {_px_fmt(r['symbol'], r['plan_target'])}"
                    "（目标位按2.5倍盈亏比计算）")
            else:
                st.caption("交易计划：母板未设（缺关键突破位/止损位）")
            st.caption(r["evidence"])
    elif _srows:
        _top = _srows[0]
        st.info(f"当前无 S/A 级标的。最高 {_top['grade']} 级："
                f"{_top['symbol']} {_top['name']} {_top['total']:.1f} 分"
                + (f"（{' · '.join(_gate_tags(_top['gates']))}）" if _top["gates"] else ""))
    # ---- 板块新闻面：X 博主证据账折算的板块情绪 ----
    st.markdown('<div class="kpi-label" style="margin-top:12px;">研究证据 · 新闻面（观察，不构成交易依据）</div>',
                unsafe_allow_html=True)
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
    st.markdown('<div class="kpi-label" style="margin-top:12px;">研究证据 · 技术形态（观察）</div>',
                unsafe_allow_html=True)
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
    st.subheader("订单管理")
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

        # ===== UI-R1 阶段C 交易页：执行状态栏（状态在先，明细在后） =====
        _n_ok = sum(1 for v in validated if v[4])
        _n_fail = len(validated) - _n_ok
        _tag_n = {"无效·无仓": 0, "已过期": 0, "已拦截": 0}
        for _v in validated:
            if _v[4]:
                continue
            if any("无持仓" in e for e in _v[5]):
                _tag_n["无效·无仓"] += 1
            elif any("已过期" in e for e in _v[5]):
                _tag_n["已过期"] += 1
            else:
                _tag_n["已拦截"] += 1
        try:
            from engine import regime as _regime_tr
            _env_tr = _regime_tr.load_market_env(db_path=DB)
            _env_chip = _r1_chip({"attack": "ok", "cautious": "wait"}.get(
                _env_tr.get("state"), "block"), _env_tr.get("label") or "环境未知")
        except Exception:
            _env_chip = _r1_chip("wait", "环境未知")
        _risk_txt = ("；".join(f"{k} {v}" for k, v in _tag_n.items() if v)
                     if _n_fail else "无拦截")
        # R1 去噪改动4：执行状态一行——环境门禁 + 可处理 N + 已拦截 N，
        # 拦截构成同行小字附注，不另起第二行、不复制全局阻断长文。
        st.markdown(
            f'<div class="kpi-card"><div class="kpi-label">执行状态</div>'
            f'<div style="font-size:13.5px;">环境：{_env_chip} · '
            f'可处理 <b>{_n_ok}</b> 条 · '
            f'已拦截 <b style="color:{"#f6465d" if _n_fail else "#2ebd85"};">{_n_fail}</b> 条'
            f'<span style="color:#8b9bab;font-size:12px;">{"（" + _risk_txt + "）" if _n_fail else ""}</span></div></div>',
            unsafe_allow_html=True)
        st.markdown('<div class="kpi-label" style="margin-top:10px;">可处理订单（校验通过 · 券商人工建单）</div>',
                    unsafe_allow_html=True)

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
                checked = st.checkbox(f"{nm} · {direction} · {_px_fmt(s, px)} · {d}",
                                      value=select_all, key=key)
                if checked:
                    selected.append((s, direction, px, nm))
            with top2:
                if selected:
                    if st.button(f"📋 复制 {len(selected)} 条", key="copy_selected"):
                        txt = "\n".join(f"{s} {d} 限价{_px_fmt(s, px)}" for s, d, px, _ in selected)
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
                    st.markdown(f'<span id="ord-{s}"></span>'
                                f'<div style="padding:8px 0;"><b>{nm}</b> '
                                f'<span style="color:{color};font-weight:bold;">{direction}</span> '
                                f'<span class="num">{_px_fmt(s, px)}</span> '
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


        st.markdown('<div class="kpi-label" style="margin-top:14px;">风控拦截（不可处理 · 原因与处理方式逐条标注）</div>',
                    unsafe_allow_html=True)
        failed = [v for v in validated if not v[4]]
        if failed:
            st.warning(f"⛔ {len(failed)} 条信号校验未通过，已拦截（不生成订单指引）")
            st.caption("校验未通过，不生成订单指引；「跳过/隔离」后不再计入待办。")
            for d, s, sd, px, ok, errs, warns in failed:
                # 无效/过期标签（§3.1）：无持仓 SELL 与过期信号一眼可辨，
                # 避免被反复当成可建单；拦截原因一行说完（首因+项数）。
                if any("无持仓" in e for e in errs):
                    _tag = "无效·无仓"
                elif any("已过期" in e for e in errs):
                    _tag = "已过期"
                else:
                    _tag = "已拦截"
                _reason = errs[0] if errs else ""
                if len(errs) > 2:
                    _reason += f"（共 {len(errs)} 项）"
                elif len(errs) == 2:
                    _reason += f"；{errs[1]}"
                _fc1, _fc2 = st.columns([3, 1])
                with _fc1:
                    st.markdown(f'<span id="blk-{s}"></span>'
                                f'<span style="background:rgba(246,70,93,.15);'
                                f'color:#f6465d;border-radius:4px;padding:1px 6px;'
                                f'font-size:12px;">{_tag}</span> '
                                f"**{sym_name(s)}** {sd} @{_px_fmt(s, px)} "
                                f'<span style="color:#666;font-size:12px;">{d}</span>',
                                unsafe_allow_html=True)
                    st.caption(_reason)
                with _fc2:
                    if st.button("⏭ 跳过/隔离", key=f"block_skip_{d}_{s}_{sd}"):
                        _ap_state(f"{d}_{s}_{sd}", "skipped", d, acct, s, sd)
                        if st.session_state.get(f"apstate_{d}_{s}_{sd}") == "已生效":
                            st.rerun()  # 立即重查：已跳过的信号当场从待办/列表消失
                        st.info(st.session_state.get(f"apstate_{d}_{s}_{sd}", "失败"))

    # 成交记录（独立区，与事件流分开；fills 同源只读）
    st.subheader("成交记录")
    _fills_tr = q("SELECT date, symbol, side, qty, price, pnl FROM fills WHERE account=? ORDER BY date DESC, id DESC LIMIT 15", (acct,))
    if _fills_tr:
        st.table([{"日期": d, "标的": sym_name(s),
                   "方向": "买入" if sd == "BUY" else "卖出",
                   "数量": f"{qty:g}", "成交价": _px_fmt(s, px),
                   "盈亏": (f"{pnl:+,.2f}" if pnl is not None else "—")}
                  for d, s, sd, qty, px, pnl in _fills_tr])
    else:
        st.write("暂无成交记录")

    # 事件流（§3.2：自工作台移入交易页）：日期 | 色点类型 | 一句话，
    # 默认近 15 条；类型色：信号灰、审批蓝、成交绿、拦截/阻断红。
    # 拦截事件读 risk_block_events 落库记录（V-3 只读展示，不改账本）。
    st.subheader("事件流")
    _EV_DOT = {"成交": "#2ebd85", "信号": "#8a8f9c",
               "审批": "#1890ff", "拦截": "#f6465d"}
    _ev = []
    for d, a, s, sd, qty, px in q(
            "SELECT date,account,symbol,side,qty,price FROM fills ORDER BY date DESC LIMIT 8"):
        _ev.append((d, "成交", f"{ACCT_CN.get(a, a)} {sym_name(s)} {sd} {qty:g} @ {_px_fmt(s, px)}"))
    for d, a, s, sd in q(
            "SELECT date,account,symbol,side FROM signals ORDER BY date DESC LIMIT 8"):
        _ev.append((d, "信号", f"{ACCT_CN.get(a, a)} {sym_name(s)} {sd}"))
    for d, a, s, sd, dc in q(
            "SELECT date,account,symbol,side,decision FROM approvals ORDER BY updated DESC LIMIT 8"):
        _dc_cn = "批准" if dc == "approved" else ("跳过" if dc == "skipped" else dc)
        _ev.append((d, "审批", f"{ACCT_CN.get(a, a)} {sym_name(s)} {sd} · {_dc_cn}"))
    try:
        for d, a, s, sd, reason in q(
                "SELECT COALESCE(trade_date, substr(ts,1,10)), account, symbol,"
                " side, reason FROM risk_block_events ORDER BY ts DESC LIMIT 8"):
            _ev.append((d or "—", "拦截",
                        f"{ACCT_CN.get(a, a)} {sym_name(s)} {sd} · {reason or ''}"))
    except Exception:
        pass
    _ev.sort(key=lambda e: e[0], reverse=True)
    if _ev:
        st.markdown("".join(
            f'<div style="display:flex;gap:10px;align-items:baseline;padding:5px 0;'
            f'border-bottom:1px solid #1e2532;">'
            f'<span style="color:#8a8f9c;font-size:12px;min-width:86px;">{d}</span>'
            f'<span style="color:{_EV_DOT[k]};font-size:12px;">●</span>'
            f'<span style="color:{_EV_DOT[k]};font-size:12px;font-weight:700;min-width:32px;">{k}</span>'
            f'<span style="font-size:13px;">{txt}</span></div>'
            for d, k, txt in _ev[:15]), unsafe_allow_html=True)
    else:
        st.write("暂无事件")

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
        st.write("暂无回执")

    # 行级深链（P3 实测项）：工作台「系统阻断」跳转时定位第一条
    # 被拦截行并闪烁描边；一次性消费，失败静默（页级跳转已达成）。
    _focus = st.session_state.pop("trade_focus", None)
    if _focus == "first_blocked":
        st.components.v1.html(
            "<script>"
            "const el = window.parent.document.querySelector('[id^=\"blk-\"]')"
            " || window.parent.document.querySelector('[id^=\"ord-\"]');"
            "if (el) { el.scrollIntoView({block: 'center'});"
            " el.style.outline = '2px solid #f6465d';"
            " setTimeout(() => { el.style.outline = ''; }, 3000); }"
            "</script>", height=0)

# ================================================================
# 💰 持仓与账本：持仓 + 成交 + 净值
# ================================================================
elif tab == "💰 账户":
    st.markdown('<div class="section-head">💰 持仓与账本</div>', unsafe_allow_html=True)

    # 账户切换（标签式）
    acct_choice = st.radio("账户", ["A股", "港股", "加密货币"], horizontal=True, key="acct_tab")
    acct_map = {"A股": "sim_a", "港股": "sim_hk", "加密货币": "sim_crypto"}
    acct = acct_map[acct_choice]

    _det = st.session_state.get("acct_detail")
    if _det and _det[0] == acct:
        _render_acct_detail(acct, _det[1])
    else:
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

        # ===== UI-R1 阶段C 账户页：顶部四项资产摘要 + 风险分布 =====
        _cur_s = {"sim_a": "¥", "sim_hk": "HK$", "sim_crypto": "$"}.get(acct, "")
        _sum = q("SELECT nav, pnl_day, cash, position_value FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1", (acct,))
        if _sum:
            _nv, _pd, _cash, _pv = _sum[0]
            _expo_a = (_pv / _nv * 100) if _nv else 0.0
            _cap_a = 100 if acct == "sim_crypto" else 70
            _sc1, _sc2, _sc3, _sc4 = st.columns(4)
            for _col, _lab, _val in (
                    (_sc1, "账户净值", f"{_cur_s}{_nv:,.2f}"),
                    (_sc2, "当日盈亏", f"{(_pd or 0):+,.2f}"),
                    (_sc3, "可用现金", f"{_cur_s}{(_cash or 0):,.2f}"),
                    (_sc4, "持仓敞口", f"{_expo_a:.1f}% / 上限 {_cap_a}%")):
                _col.markdown(f'<div class="kpi-card"><div class="kpi-label">{_lab}</div>'
                              f'<div class="kpi-value num" style="font-size:19px;">{_val}</div></div>',
                              unsafe_allow_html=True)
            _posr = q("SELECT symbol, market_value FROM positions WHERE account=? AND qty>0", (acct,))
            _tot_mv = sum(r[1] or 0 for r in _posr)
            _max_mv = max([r[1] or 0 for r in _posr], default=0)
            _conc_a = (_max_mv / _tot_mv * 100) if _tot_mv else 0.0
            _cov = sum(1 for r in _posr if stop_ref_of(r[0]))
            _nbr = len(_stop_breaches(acct))
            st.markdown(
                f'<div class="kpi-card"><div class="kpi-label">风险分布</div>'
                f'<div style="font-size:13.5px;">敞口 <span class="num">{_expo_a:.1f}%</span>（上限 {_cap_a}%） · '
                f'最大单标的集中度 <span class="num">{_conc_a:.1f}%</span>（占持仓市值） · '
                f'止损覆盖 <span class="num">{_cov}/{len(_posr)}</span> 只持仓有母板止损参考 · '
                f'破止损 <span class="num" style="color:{"#f6465d" if _nbr else "#2ebd85"};">{_nbr}</span> 只</div></div>',
                unsafe_allow_html=True)

        # 持仓（§4 定列）：代码 | 名称 | 数量 | 成本 | 现价 | 浮盈% |
        # 止损参考 | 状态；状态与工作台/验证页同源（_stop_state：破止损/
        # 接近止损（距参考 ≤5%）/正常），禁止三处数字不一致。
        st.subheader("当前持仓")
        pos = q("SELECT symbol,qty,avg_cost,last_price,market_value FROM positions WHERE account=? AND qty>0", (acct,))
        if pos:
            rows = []
            for s, qty, avg, last, mv in pos:
                upl_pct = (last - avg) / avg * 100 if last and avg else 0
                _stt, _sr = _stop_state(s, last)
                _stt_s = {"breach": "🔴 破止损", "near": "🟡 接近止损",
                          "ok": "正常", "none": "—"}.get(_stt, "—")
                rows.append({"代码": s[2:] if s.startswith("HK") else s,
                             "名称": SYMBOL_CN.get(s, ""),
                             "数量": f"{qty:g}", "成本": _px_fmt(s, avg),
                             "现价": _px_fmt(s, last),
                             "浮盈%": f"{upl_pct:+.1f}%",
                             "止损参考": _px_fmt(s, _sr) if _sr else "—",
                             "状态": _stt_s})
            st.caption("勾选持仓行首复选框，进入个股详情"
                       "（大图 · 成本/止损/计划线 · 成交记录）")
            # 点选行进入个股详情（批D-1）：选择状态被消费一次，
            # 返回后原选择仍在也不回弹；取消选择后守卫复位。
            _raw_order = [r[0] for r in pos]
            st.dataframe(rows, key="pos_df", hide_index=True,
                         on_select="rerun", selection_mode="single-row",
                         use_container_width=True)
            _sel = (st.session_state.get("pos_df") or {}
                    ).get("selection", {}).get("rows", [])
            if _sel:
                _ti = _sel[0]
                _tgt = _raw_order[_ti] if 0 <= _ti < len(_raw_order) \
                    else None
                if _tgt and st.session_state.get(
                        "_pos_consumed") != (acct, _tgt):
                    st.session_state["_pos_consumed"] = (acct, _tgt)
                    st.session_state["acct_detail"] = (acct, _tgt)
                    st.rerun()
            else:
                st.session_state["_pos_consumed"] = None

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

        # 净值曲线（UI批B 自工作台移入，归账户页职责）：纵轴为数据域
        # 自动缩放时必须明示范围，避免波动视觉误导（批C W-4 约束）。
        _nhist = q("SELECT date,nav FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date", (acct,))
        if _nhist:
            st.subheader("净值曲线")
            import pandas as pd
            _ndf = pd.DataFrame(_nhist, columns=["date", "nav"])
            _ndf["date"] = pd.to_datetime(_ndf["date"])
            _ndf = _ndf.set_index("date")
            _nmin, _nmax = float(_ndf["nav"].min()), float(_ndf["nav"].max())
            st.caption(f"纵轴数据域：{_nmin:,.2f} – {_nmax:,.2f}（自动缩放，非零起点）")
            st.line_chart(_ndf["nav"])

# ================================================================
# 🏦 机构专用席位连续净买监控（方案1 影子监控 · 只读）：龙虎榜机构专用
# 席位净买/连续天数；不生成信号、不建意图、不影响交易主链。
# ================================================================
elif tab == "🏦 机构影子":
    st.markdown('<div class="section-head">🏦 机构专用席位连续净买监控（影子 · 只读）</div>',
                unsafe_allow_html=True)
    st.caption("口径：龙虎榜「机构专用」匿名聚合席位，非机构家数；"
               "席位净买 ≠ 具名机构建仓。仅影子监控：不生成信号、"
               "不创建交易意图、不改变策略参数。数据源：东财龙虎榜（夜间抓取）。")
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
                # UI-R1 阶段C：数据可用性条（可信日/抓取时间/样本段）
                _lk = None
                if _latest_ok:
                    _lk = _con.execute(
                        "SELECT fetched_at, state FROM inst_lhb_fetch_days"
                        " WHERE date=?", (_latest_ok,)).fetchone()
                _seg_i = ("前向积累" if (_latest_ok and _latest_ok >= "2026-10-30")
                          else ("历史回填段" if _latest_ok else "—"))
                st.markdown(
                    f'<div class="kpi-card"><div class="kpi-label">数据可用性</div>'
                    f'<div style="font-size:13.5px;">最近可信交易日 '
                    f'<b class="num">{_latest_ok or "无"}</b> · 抓取时间 '
                    f'{(_lk[0] if _lk else "—")} · 数据状态 '
                    f'{("正常" if _lk and _lk[1] != "failed" else "异常")} · '
                    f'样本段：{_seg_i}（回填段与前向段分开登记，不混算）</div></div>',
                    unsafe_allow_html=True)
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
                            else "⚠ 不确定（序列含未登记日/抓取失败日）",
                        } for b in _board])
                        if any(not b["streak_certain"] for b in _board):
                            st.caption("⚠ 不确定：序列含未登记/失败日，天数可能被低估（I-1）。")
                    else:
                        st.write("当日无符合条件的标的。")
                    _day_rows = _con.execute(
                        "SELECT symbol,name,inst_buy,inst_sell,inst_net,"
                        "seat_buy_records,seat_sell_records,window"
                        " FROM inst_lhb_daily WHERE date=?"
                        " ORDER BY inst_net DESC", (_latest_ok,)).fetchall()
                    # 全表默认折叠（§6）：影子榜默认开，全表只供复算时展开。
                    with st.expander("当日聚合全表（%s · 共 %d 只 · 供复算核对）"
                                     % (_latest_ok, len(_day_rows)),
                                     expanded=False):
                        st.table([{
                            "代码": r[0], "名称": r[1] or "",
                            "买入(万)": f"{(r[2] or 0) / 10000:,.0f}",
                            "卖出(万)": f"{(r[3] or 0) / 10000:,.0f}",
                            "净买(万)": f"{(r[4] or 0) / 10000:,.0f}",
                            "买席位记录": r[5], "卖席位记录": r[6],
                            "窗口": "累计" if r[7] == "cum" else "一日",
                        } for r in _day_rows])
                        st.caption("窗口=累计：多日累计金额，与一日类不相加。")
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

    # ===== UI-R1 阶段C 验证页：顶部四块（健康/账本/门禁/运行证据） =====
    _HEALTH_TXT = {0: "正常", 1: "延迟/降级", 2: "断连/异常"}
    try:
        import json as _json_v
        _fp_v = q("SELECT COUNT(*), MAX(id) FROM fills")[0]
        _gate_st = {}
        try:
            with open(os.path.join(BASE, ".release_gate_state.json"), encoding="utf-8") as _gf:
                _gate_st = _json_v.load(_gf)
        except Exception:
            _gate_st = {}
        _ledger_ok = (bool(_gate_st) and _gate_st.get("fills_count") == _fp_v[0]
                      and _gate_st.get("max_id") == _fp_v[1])
        _gate_last = "无记录"
        try:
            with open(os.path.join(BASE, "reports", "release_gate_log.jsonl"), encoding="utf-8") as _lf:
                _ll = [ln for ln in _lf if ln.strip()]
            if _ll:
                _lj = _json_v.loads(_ll[-1])
                _gate_last = f"{_lj.get('event', '')} · {_lj.get('ts', '')}"
        except Exception:
            pass
        _dr_max = q("SELECT MAX(date) FROM daily_report")[0][0]
        _v1, _v2, _v3, _v4 = st.columns(4)
        _v1.markdown(f'<div class="kpi-card"><div class="kpi-label">数据健康</div>'
                     f'<div style="font-size:15px;">{_HEALTH_TXT.get(_hworst, "未知")}</div></div>',
                     unsafe_allow_html=True)
        _v2.markdown(f'<div class="kpi-card"><div class="kpi-label">账本核对</div>'
                     f'<div style="font-size:15px;">{"一致" if _ledger_ok else "待核验"} · fills {_fp_v[0]} 笔</div></div>',
                     unsafe_allow_html=True)
        _v3.markdown(f'<div class="kpi-card"><div class="kpi-label">发布门禁</div>'
                     f'<div style="font-size:15px;">最近：{_gate_last}</div></div>',
                     unsafe_allow_html=True)
        _v4.markdown(f'<div class="kpi-card"><div class="kpi-label">运行证据</div>'
                     f'<div style="font-size:15px;">日报至 {_dr_max or "—"}</div></div>',
                     unsafe_allow_html=True)
    except Exception:
        st.caption("验证页顶部状态读取失败（不影响下方明细）")

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
        if rt == 0:
            # 样本为 0：整表替换（§7）——不展示空的盈亏比/期望值
            # 卡片，一句说清状态；风控指标与破止损检查在下方照常。
            st.markdown('<div class="alert-banner warn">样本不足，'
                        '毕业统计未启用（当前 0 笔 round-trip，'
                        f'毕业线 {tg["sample_min"]}-{tg["sample_max"]} 笔）</div>',
                        unsafe_allow_html=True)
        else:
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
            # V-2（2026-10-31 批准实施）：仓位上限与执行端口径同源——
            # 读 strategy.yaml risk 段同一组键，账户映射与
            # engine.order_validate._position_cap 逐字一致
            # （sim_a/sim_hk 股票 70%，sim_crypto 加密 100%），
            # 不再写死 70% 对加密账户误报超限。
            _risk = CFG.get("risk") or {}
            if acct in ("sim_a", "sim_hk"):
                _cap_pct = float(_risk.get("max_stock_position_pct", 70))
            else:
                _cap_pct = float(_risk.get("max_crypto_position_pct", 100))
            ok = pos_ratio <= _cap_pct / 100.0
            st.markdown(f'<div class="kpi-card"><div class="kpi-label">仓位 {"✅" if ok else "⚠"}</div>'
                        f'<div class="kpi-value {"up" if ok else "down"}">{pos_ratio*100:.1f}%</div>'
                        f'<div class="kpi-label">上限 {_cap_pct:g}%</div></div>', unsafe_allow_html=True)
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
    for s, qty_, last_, _sr, _md in _stop_breaches(acct):
        st.error(f"🔴 {s} 现价 {_px_fmt(s, last_)} 已跌破母板止损参考 "
                 f"{_px_fmt(s, _sr)}，仍持有 {qty_:g}（数据日 {_md}）")
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
        st.caption(f"多重校验绩效（样本外）：S/A {_n} 条 · 满 5 日 {_m5} 条 · "
                   f"5 日平均相对收益 {_avg5 * 100:+.2f}% · 命中率 {_hit5 / _m5 * 100:.0f}%"
                   "（相对全池平均）")
    else:
        st.caption("多重校验绩效：样本积累中，暂无到期样本。")

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
        # 毕业硬线不在此处重复（页首标准行 + 各 KPI 卡硬线标签已呈现）
    except Exception as e:
        st.error(f"仪表盘读取失败：{e}")
    # ---- 验收报告（2026-10-31 新增）：判分/查收类报告直发本页 ----
    # 定时任务把报告写入 reports/public/*.md，随每日同步上公网，不走聊天推送。
    # ===== UI-R1 阶段C 验证页：五列检查表 + 异常证据时间线 =====
    st.subheader("系统检查表")
    try:
        _ck = []
        _fp2 = q("SELECT COUNT(*), MAX(id) FROM fills")[0]
        _ck.append({"检查项": "账本指纹", "标准": "与发布水位一致",
                    "当前值": f"fills {_fp2[0]} · max_id {_fp2[1]}",
                    "结论": "通过" if _ledger_ok else "待核验",
                    "证据来源": "sim.db / .release_gate_state.json"})
        _md2 = q("SELECT MAX(date) FROM market_data WHERE market IN ('a','hk')")[0][0]
        _dr2 = q("SELECT MAX(date) FROM daily_report")[0][0]
        _ck.append({"检查项": "行情新鲜度", "标准": "数据至最近日报日",
                    "当前值": f"行情 {_md2} · 日报 {_dr2}",
                    "结论": "通过" if _md2 and _dr2 and _md2 >= _dr2 else "待核验",
                    "证据来源": "market_data / daily_report"})
        _sc2 = q("SELECT MAX(asof) FROM screen_runs")[0][0]
        _ck.append({"检查项": "选股运行", "标准": "夜跑每期一跑",
                    "当前值": f"最近一期 {_sc2}",
                    "结论": "通过" if _sc2 else "待核验",
                    "证据来源": "screen_runs"})
        _il2 = q("SELECT MAX(date) FROM inst_lhb_fetch_days WHERE state != 'failed'")[0][0]
        _ck.append({"检查项": "机构抓取", "标准": "每交易日抓取（影子）",
                    "当前值": f"最近成功 {_il2}",
                    "结论": "通过" if _il2 else "待核验",
                    "证据来源": "inst_lhb_fetch_days"})
        _ck.append({"检查项": "数据健康", "标准": "四态为正常",
                    "当前值": _HEALTH_TXT.get(_hworst, "未知"),
                    "结论": "通过" if _hworst == 0 else "待核验",
                    "证据来源": "engine.data_health"})
        st.table(_ck)
    except Exception as _cke:
        st.caption(f"检查表生成失败：{_cke}")
    st.subheader("异常与拦截证据（只追加 · 可追溯）")
    try:
        _tl = q("SELECT COALESCE(trade_date, substr(ts,1,10)), symbol, side, rule, reason"
                " FROM risk_block_events ORDER BY ts DESC LIMIT 8")
        if _tl:
            st.markdown("".join(
                f'<div style="font-size:13px;padding:5px 0;border-bottom:1px solid #1e2532;">'
                f'<span style="color:#8b9bab;">{d}</span> · {s} {sd} · '
                f'<span style="color:#f6465d;">{rule}</span> · {reason or ""}</div>'
                for d, s, sd, rule, reason in _tl), unsafe_allow_html=True)
        else:
            st.write("暂无拦截记录")
    except Exception:
        st.write("拦截记录读取失败")

    st.subheader("📄 验收报告")
    try:
        import glob as _glob
        _reports = sorted(_glob.glob(os.path.join(
            BASE, "reports", "public", "*.md")), reverse=True)
        if not _reports:
            st.caption("暂无报告")
        for _rp in _reports:
            with open(_rp, encoding="utf-8") as _f:
                _content = _f.read()
            _first = _content.lstrip().splitlines()[0] if _content.strip() else ""
            _title = (_first.lstrip("# ").strip() if _first.startswith("#")
                      else os.path.splitext(os.path.basename(_rp))[0])
            with st.expander(_title, expanded=False):
                st.markdown(_content)
    except Exception as _re:
        st.caption(f"验收报告暂不可用：{_re}")

# ================================================================
# ⚙ 系统与审计：数据源 / 规则版本 / 指令日志
# ================================================================
