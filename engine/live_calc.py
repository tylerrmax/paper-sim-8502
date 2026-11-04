#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公网实时计算层（2026-11-04）：不依赖 streamlit，便于单测。

只用于"展示"，不参与信号生成 / 风控闸门 / pipeline / 验证 KPI
（这些一律用 market_data 官方收盘价，口径见 engine/live_quotes.py 文件头）。

三件事：
1. 双时间戳：行情实时时间（交易所最近一笔）与账本快照时间（build_info.json
   的 built_at，即本地跑批库推到公网的时刻）分开算、分开显示，延迟一眼可见。
2. 技术指标实时计算：账本日K（官方收盘，到最近一次同步为止）+ 公网直拉的最新
   日K（含盘中未收盘的当日 K 线）合并后再算指标；直拉失败则回退纯账本并标注。
3. 成交量单位校验：直拉日K 与账本在重叠交易日的成交量比值必须≈1；
   落在 ×100 / ×0.01 附近判为"手/股"单位错位并按账本口径换算，
   其余偏差判为不一致，直拉成交量不予采用。指标本身只用价格，
   成交量只影响图表与量能类标签，所以校验失败不会污染 MACD/RSI 等。
"""
from __future__ import annotations

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")
_UA = {"User-Agent": "Mozilla/5.0"}
FETCH_TIMEOUT = 5  # 公网页面不能被慢接口拖住：单标的 5 秒、不重试

MARKET_CN = {"a": "A股", "hk": "港股", "crypto": "加密"}


# ====================================================================
# 一、时间戳
# ====================================================================
def market_of(symbol):
    s = str(symbol).upper()
    if s.endswith("USDT"):
        return "crypto"
    if s.startswith("HK"):
        return "hk"
    return "a"


def parse_ts(s):
    """'2026-11-04 14:25:03' / '2026-11-04 14:25' / '2026-11-04' → 北京时间 aware datetime；
    无法解析（如旧版的 'Binance实时'）返回 None，调用方必须能处理 None。"""
    if not s:
        return None
    s = str(s).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    return None


def quote_time_summary(quotes):
    """行情实时时间：{symbol: {"time": ...}} → {"newest": dt|None, "per_market": {m: dt}}。
    只认能解析的时间戳；解析不了的不参与（宁缺勿造）。"""
    per = {}
    for sym, d in (quotes or {}).items():
        dt = parse_ts((d or {}).get("time"))
        if dt is None:
            continue
        m = market_of(sym)
        if m not in per or dt > per[m]:
            per[m] = dt
    newest = max(per.values()) if per else None
    return {"newest": newest, "per_market": per}


def ledger_snapshot_time(base_dir):
    """账本快照时间 = build_info.json 的 built_at（同步脚本推送时写入）。
    不用 sim.db 的文件 mtime：云端 mtime 是容器检出时刻，会把"部署时间"
    误当成"数据时间"（2026-11-04 截图里的「同步于 06:13」即此类）。
    返回 (dt|None, 来源说明)。"""
    import os
    p = os.path.join(base_dir, "build_info.json")
    try:
        with open(p, encoding="utf-8") as f:
            info = json.load(f)
        dt = parse_ts(info.get("built_at"))
        if dt is not None:
            return dt, "build_info.json"
    except (OSError, ValueError):
        pass
    return None, "未标记"


def human_lag(later, earlier):
    """later - earlier 的中文时长，如 '2天13小时' / '5小时20分' / '12分钟'；
    earlier 晚于 later 返回 ''（不制造负延迟）。"""
    if later is None or earlier is None:
        return ""
    sec = int((later - earlier).total_seconds())
    if sec <= 0:
        return ""
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}天{h}小时" if h else f"{d}天"
    if h:
        return f"{h}小时{m}分" if m else f"{h}小时"
    return f"{max(m, 1)}分钟"


def lag_level(later, earlier):
    """延迟等级：ok(<6小时) / warn(<3天) / danger(≥3天) / unknown。"""
    if later is None or earlier is None:
        return "unknown"
    sec = (later - earlier).total_seconds()
    if sec < 6 * 3600:
        return "ok"
    if sec < 3 * 86400:
        return "warn"
    return "danger"


# ====================================================================
# 二、直拉日K（腾讯 / Kraken），解析与单位口径与 data/quotes.py 一致
# ====================================================================
def _tx_code(symbol):
    c = str(symbol).strip().upper()
    if c.startswith("HK"):
        return "hk" + c[2:].zfill(5), "hk"
    if c.startswith(("6", "5")):
        return "sh" + c, "a"
    if c.startswith("9"):
        return "bj" + c, "a"
    return "sz" + c, "a"


def parse_tencent_kline(days, symbol):
    """腾讯日K → [(date_str, o, h, l, c, vol_in_shares)] 升序。
    腾讯格式 [日期, 开, 收, 高, 低, 量]。
    量单位口径（与 data/quotes.py::_fetch_tencent 同，2026-11-01 双源实测）：
    主板/创业板为"手" → ×100 换算成股；科创板(688)与港股原始即股。"""
    market = market_of(symbol)
    star = str(symbol).strip().upper().startswith("688")
    out = []
    for x in days or []:
        try:
            vol = float(x[5])
            if market == "a" and not star:
                vol *= 100
            out.append((str(x[0]), float(x[1]), float(x[3]), float(x[4]),
                        float(x[2]), vol))
        except (ValueError, IndexError, TypeError):
            continue
    out.sort(key=lambda r: r[0])
    return out


def parse_kraken_ohlc(payload):
    """Kraken OHLC → [(date_str, o, h, l, c, vol)] 升序，包含当日未收盘 K（日期按 UTC）。"""
    res = payload.get("result") or {}
    key = next((k for k in res if k != "last"), None)
    out = []
    for c in (res.get(key) or []) if key else []:
        try:
            dt = datetime.fromtimestamp(int(c[0]), tz=timezone.utc).strftime("%Y-%m-%d")
            out.append((dt, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[6])))
        except (ValueError, IndexError, TypeError):
            continue
    out.sort(key=lambda r: r[0])
    return out


def _http_json(url):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_fresh_bars(symbol, n=150, getter=None):
    """直拉最新日K（含当日未收盘 K 线）。失败返回 []，绝不抛异常。
    getter(url)->dict 可注入（单测）。A 股前复权、港股不复权，与账本同口径。"""
    getter = getter or _http_json
    try:
        m = market_of(symbol)
        if m == "crypto":
            pair = {"BTCUSDT": "XBTUSD", "ETHUSDT": "ETHUSD"}.get(symbol.upper(), symbol.upper())
            return parse_kraken_ohlc(getter(
                f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440"))
        tx, mk = _tx_code(symbol)
        key, fq = ("qfqday", "qfq") if mk == "a" else ("day", "bfq")
        start = (datetime.now(CN_TZ) - timedelta(days=n * 2)).strftime("%Y-%m-%d")
        d = getter("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                   f"?param={tx},day,{start},2100-01-01,{n},{fq}")
        return parse_tencent_kline(d["data"][tx].get(key) or [], symbol)
    except Exception:  # noqa: BLE001 - 展示层：任何失败都回退账本，不影响页面
        return []


def fetch_fresh_bars_many(symbols, getter=None, workers=8):
    symbols = list(symbols)
    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as ex:
        return dict(zip(symbols, ex.map(lambda s: fetch_fresh_bars(s, getter=getter), symbols)))


# ====================================================================
# 三、成交量单位校验 + 合并
# ====================================================================
def check_volume_units(db_bars, fresh_bars, today=None, min_overlap=2, tail=5):
    """比较重叠交易日的成交量（排除 today 的未收盘 K）。
    返回 {"status", "ratio", "factor", "n"}：
      ok           比值≈1（±15%），可直接采用
      unit_x100    直拉量≈账本×100（手 vs 股）→ factor=0.01 把直拉量换回账本口径
      unit_x0.01   直拉量≈账本×0.01 → factor=100
      inconsistent 其余偏差：直拉成交量不予采用
      unverified   重叠样本不足：成交量不予采用（宁缺勿造）"""
    db = {r[0]: r[5] for r in db_bars if r[5] and r[5] > 0}
    ratios = []
    for r in fresh_bars:
        if today and r[0] >= today:
            continue
        if r[0] in db and r[5] and r[5] > 0:
            ratios.append((r[0], r[5] / db[r[0]]))
    ratios = [v for _, v in sorted(ratios)[-tail:]]
    if len(ratios) < min_overlap:
        return {"status": "unverified", "ratio": None, "factor": None, "n": len(ratios)}
    ratios.sort()
    med = ratios[len(ratios) // 2]
    if abs(med - 1) <= 0.15:
        return {"status": "ok", "ratio": med, "factor": 1.0, "n": len(ratios)}
    if 85 <= med <= 118:
        return {"status": "unit_x100", "ratio": med, "factor": 0.01, "n": len(ratios)}
    if 1 / 118 <= med <= 1 / 85:
        return {"status": "unit_x0.01", "ratio": med, "factor": 100.0, "n": len(ratios)}
    return {"status": "inconsistent", "ratio": med, "factor": None, "n": len(ratios)}


VOLUME_STATUS_CN = {
    "ok": "成交量单位一致",
    "unit_x100": "成交量单位错位（手/股），已按账本口径换算",
    "unit_x0.01": "成交量单位错位（股/手），已按账本口径换算",
    "inconsistent": "成交量与账本不一致，直拉成交量未采用",
    "unverified": "成交量重叠样本不足，直拉成交量未采用",
}


def check_price_consistency(db_bars, fresh_bars, today=None, tol=0.01, tail=5):
    """重叠交易日收盘价应与账本一致（±1%）。复权口径变动、接口串标的等会让两段
    价格接不上，此时拼接会制造假的跳空，指标失真——判不一致则不拼接直拉数据。
    重叠不足时返回 True（不因缺样本而否决，成交量那边另有 unverified 处理）。"""
    db = {r[0]: r[4] for r in db_bars if r[4]}
    diffs = []
    for r in fresh_bars:
        if today and r[0] >= today:
            continue
        if r[0] in db and r[4]:
            diffs.append((r[0], abs(r[4] / db[r[0]] - 1)))
    diffs = [v for _, v in sorted(diffs)[-tail:]]
    if not diffs:
        return True
    diffs.sort()
    return diffs[len(diffs) // 2] <= tol


def merge_bars(db_bars, fresh_bars, today=None):
    """合并：账本日期内以账本（官方收盘）为准；账本之后的新日期取直拉。
    成交量先过单位校验。返回 (bars, info)，bars 升序
    [(date,o,h,l,c,vol)]；info = {"source", "asof", "provisional", "volume"}。
    直拉为空 → 纯账本，source='ledger'。"""
    db_bars = sorted(db_bars, key=lambda r: r[0])
    last_db = db_bars[-1][0] if db_bars else ""
    vcheck = check_volume_units(db_bars, fresh_bars, today=today)
    if not fresh_bars:
        return list(db_bars), {"source": "ledger", "asof": last_db,
                               "provisional": False, "volume": vcheck}
    if not check_price_consistency(db_bars, fresh_bars, today=today):
        return list(db_bars), {"source": "ledger", "asof": last_db,
                               "provisional": False, "volume": vcheck,
                               "note": "直拉收盘价与账本在重叠日不一致，未拼接"}
    factor = vcheck["factor"]
    new = []
    for r in fresh_bars:
        if r[0] <= last_db:
            continue
        vol = r[5] * factor if factor else 0.0  # 不可信的成交量置 0，不伪造
        new.append((r[0], r[1], r[2], r[3], r[4], vol))
    bars = list(db_bars) + new
    asof = bars[-1][0] if bars else ""
    return bars, {"source": "live" if new else "ledger", "asof": asof,
                  "provisional": bool(new and today and new[-1][0] >= today),
                  "volume": vcheck}


# ====================================================================
# 四、技术标签（逻辑自 app.py::render_ticker._tech_signals 原样迁出，纯函数）
# ====================================================================
def tech_tags(bars):
    """bars 升序 [(date,o,h,l,c,vol)]。不足 30 根返回 '-'。
    标签与迁出前完全一致：超买/超卖、MACD多/空、趋势、均线多/空、破上轨/破下轨。"""
    from engine.indicators import rsi, macd, bollinger, adx, market_regime, ma_align
    if len(bars) < 30:
        return "-"
    closes = [r[4] for r in bars]
    highs = [r[2] for r in bars]
    lows = [r[3] for r in bars]
    tags = []
    rsi_v = rsi(closes)[-1]
    if rsi_v and rsi_v > 70:
        tags.append("超买")
    elif rsi_v and rsi_v < 30:
        tags.append("超卖")
    _, _, hist = macd(closes)
    tags.append("MACD多" if (hist[-1] and hist[-1] > 0) else "MACD空")
    if market_regime(adx(highs, lows, closes)[-1]) == "trend":
        tags.append("趋势")
    align = ma_align(closes)
    if align > 0.5:
        tags.append("均线多")
    elif align < -0.5:
        tags.append("均线空")
    upper, _, lower = bollinger(closes)
    if closes[-1] > upper[-1]:
        tags.append("破上轨")
    if closes[-1] < lower[-1]:
        tags.append("破下轨")
    return " ".join(tags)


def live_tech_view(db_bars_by_sym, fresh_by_sym, today=None):
    """{symbol: {"tags", "source", "asof", "provisional", "volume"}}。"""
    out = {}
    for sym, db_bars in db_bars_by_sym.items():
        bars, info = merge_bars(db_bars, fresh_by_sym.get(sym) or [], today=today)
        info["tags"] = tech_tags(bars)
        out[sym] = info
    return out
