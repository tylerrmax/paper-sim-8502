#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟盘数据层：A股/港股（腾讯）+ 加密（Kraken 主 / CoinGecko 备）+ 汇率
- 复用母板更新脚本已验证的腾讯接口模式，独立封装，不碰冻结文件。
- 网络双模式：直连优先（用户 Mac），失败自动走系统代理（本机经 egress 代理）。
- 加密说明：币安公开 API 在本机出口被地域限制，改用 Kraken BTC/USD 日K
  （与币安 BTC/USDT 存在细微价差，模拟盘级别可接受，报告中声明）。
"""
import json
import ssl
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _make_opener():
    """网络模式（显式、可解释）：
    - 默认：用系统 opener，自动读 http_proxy/https_proxy 环境变量。
      本机经 egress 代理出站；用户 Mac 无代理变量即直连。
    - SIM_DIRECT=1：强制直连，跳过一切代理（Mac 上 VPN 残留代理时用，
      与母板更新脚本同策略）。
    启动时打印实际模式，满足"每个数字能说出来源"——网络路径也一样。"""
    import os
    if os.environ.get("SIM_DIRECT") == "1":
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=_ctx()),
        )
        print("网络模式: 强制直连 (SIM_DIRECT=1)")
    else:
        opener = urllib.request.build_opener()
        px = urllib.request.getproxies()
        print("网络模式: 系统代理 %s" % ("直连" if not px else px.get("https", px.get("http", "?"))[:40]))
    return opener


_OPENER = _make_opener()


def _read(url, timeout=25):
    err = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with _OPENER.open(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - 网络抖动则重试
            err = e
            time.sleep(2)
    raise RuntimeError("接口失败 %s: %s" % (url.split("?")[0], err))


def _get_json(url, tries=2):
    err = None
    for _ in range(tries):
        try:
            return json.loads(_read(url).decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(2)
    raise RuntimeError("接口失败 %s: %s" % (url.split("?")[0], err))


def to_tx_code(code):
    """母板代码 -> 腾讯接口代码，返回 (tx_code, market)，market: 'a' 或 'hk'"""
    c = str(code).strip().upper()
    if c.startswith("HK"):
        return "hk" + c[2:].zfill(5), "hk"
    if c.startswith(("6", "5")):
        return "sh" + c, "a"
    if c.startswith("9"):
        return "bj" + c, "a"
    return "sz" + c, "a"


def _fetch_tencent(code, start):
    """腾讯日K（主源）。"""
    tx, market = to_tx_code(code)
    key = "qfqday" if market == "a" else "day"
    fq = "qfq" if market == "a" else "bfq"
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=%s,day,%s,2100-01-01,640,%s" % (tx, start.isoformat(), fq))
    d = _get_json(url)
    days = d["data"][tx].get(key) or []
    out = []
    for x in days:
        # 腾讯格式: [日期, 开, 收, 高, 低, 量]
        dt = datetime.strptime(x[0], "%Y-%m-%d").date()
        if dt >= start:
            out.append((dt, float(x[1]), float(x[3]), float(x[4]), float(x[2]), float(x[5])))
    return out


def _fetch_sina_a(code, start):
    """新浪 A 股日K（备用源，前复权与否不影响日级信号；仅在腾讯返回空/异常时用）。
    接口：quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"""
    tx, market = to_tx_code(code)
    if market != "a":
        return []
    # 新浪 scale=240 为日K；datalen 取足够覆盖 start 至今
    days_needed = max((date.today() - start).days + 30, 60)
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
           "?symbol=%s&scale=240&ma=no&datalen=%d" % (tx, min(days_needed, 1023)))
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    d = json.load(urllib.request.urlopen(req, timeout=20))
    out = []
    for x in d:
        dt = datetime.strptime(x["day"], "%Y-%m-%d").date()
        if dt >= start:
            out.append((dt, float(x["open"]), float(x["high"]),
                        float(x["low"]), float(x["close"]), float(x["volume"])))
    return out


def _latest_trading_day(now=None):
    """最近一个股票交易日（date），与 paper-sim-web/lib/freshness.py 同口径：
    周末回退到周五；工作日 17 点前（跑批前）用前一天，避免把盘中
    未收盘的 forming bar 当成已收盘 bar 补进来。节假日无表，
    此时主/备都无新 bar，补缺自然无操作。"""
    now = now or datetime.now()
    d = now.date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    if now.date().weekday() < 5 and now.hour < 17:
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d


def _fill_gap_from_backup(code, start, rows):
    """主源返回非空但末 bar 滞后于最近交易日时，用备用源补齐缺失日期。

    背景：腾讯 qfq 对个别标的（如 688305）会稳定滞后一个交易日但返回
    非空——“空/异常才切备用”触发不了，2026-10-12/13 连续两天只能
    WARN 沿用旧收盘。策略：只补主源缺失且 <= 最近交易日的日期；
    重叠日期保留主源（qfq 口径优先，与母板同源）。
    """
    _, market = to_tx_code(code)
    if market != "a" or not rows:
        return rows
    expected = _latest_trading_day()
    if rows[-1][0] >= expected:
        return rows
    try:
        backup = _fetch_sina_a(code, start)
    except Exception as e:  # noqa: BLE001
        print("WARN %s 备用源补缺异常 %s，沿用主源" % (code, e))
        return rows
    have = {r[0] for r in rows}
    extra = [r for r in backup if r[0] not in have and r[0] <= expected]
    if extra:
        print("INFO %s 主源末 bar %s 滞后于 %s，备用源补 %d 根（%s~%s）"
              % (code, rows[-1][0], expected, len(extra), extra[0][0], extra[-1][0]))
        rows = sorted(rows + extra, key=lambda r: r[0])
    else:
        print("WARN %s 主源末 bar %s 滞后，备用源无可补数据" % (code, rows[-1][0]))
    return rows


def fetch_kline(code, start):
    """A股/港股日K。code: 002446 / HK9660；start: date。
    返回 [(date, open, high, low, close, volume), ...]，日期升序。
    腾讯主、新浪备（A股）：腾讯返回空或异常时整体切换；腾讯返回非空
    但末 bar 滞后于最近交易日时，用新浪补齐缺失日期（主源口径优先）。"""
    try:
        rows = _fetch_tencent(code, start)
        if rows:
            return _fill_gap_from_backup(code, start, rows)
        print("WARN %s 腾讯返回 0 行，切换新浪备用源" % code)
    except Exception as e:  # noqa: BLE001
        print("WARN %s 腾讯异常 %s，切换新浪备用源" % (code, e))
    rows = _fetch_sina_a(code, start)
    if not rows:
        print("WARN %s 新浪备用源也返回 0 行" % code)
    return rows


def fetch_index_kline(start, tx_code="sh000001"):
    """指数日K（市场环境闸专用，如上证指数 sh000001）。
    腾讯主源与个股同接口：fqkline/get + bfq（指数无复权概念，数据在
    "day" 键，格式同个股 [日期,开,收,高,低,量]）；腾讯失败/为空时
    切新浪 CN_MarketData（sh000001）备用。返回 [(date,open,high,low,
    close,volume), ...] 日期升序。"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=%s,day,%s,2100-01-01,640,bfq" % (tx_code, start.isoformat()))
    try:
        d = _get_json(url)
        days = d["data"][tx_code].get("day") or []
        out = []
        for x in days:
            dt = datetime.strptime(x[0], "%Y-%m-%d").date()
            if dt >= start:
                out.append((dt, float(x[1]), float(x[3]), float(x[4]),
                            float(x[2]), float(x[5])))
        if out:
            return sorted(out)
        print("WARN 指数 %s 腾讯返回 0 行，切换新浪备用源" % tx_code)
    except Exception as e:  # noqa: BLE001
        print("WARN 指数 %s 腾讯异常 %s，切换新浪备用源" % (tx_code, e))
    days_needed = max((date.today() - start).days + 30, 60)
    url = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
           "?symbol=%s&scale=240&ma=no&datalen=%d" % (tx_code, min(days_needed, 1023)))
    d = json.loads(_read(url).decode("utf-8"))
    out = []
    for x in d:
        dt = datetime.strptime(x["day"], "%Y-%m-%d").date()
        if dt >= start:
            out.append((dt, float(x["open"]), float(x["high"]),
                        float(x["low"]), float(x["close"]), float(x["volume"])))
    return sorted(out)


_KRAKEN_PAIR = {"BTCUSDT": "XBTUSD", "ETHUSDT": "ETHUSD"}


def _fetch_kraken(symbol, start):
    pair = _KRAKEN_PAIR.get(symbol.upper(), symbol.upper())
    url = "https://api.kraken.com/0/public/OHLC?pair=%s&interval=1440" % pair
    d = _get_json(url)
    if d.get("error"):
        raise RuntimeError("Kraken: %s" % d["error"])
    result = d["result"]
    key = next(k for k in result.keys() if k != "last")
    out = []
    today_utc = datetime.now(timezone.utc).date()
    for c in result[key]:
        # [time, open, high, low, close, vwap, volume, count]
        dt = datetime.fromtimestamp(int(c[0]), tz=timezone.utc).date()
        if dt >= start and dt < today_utc:  # 只收完整日K（当日UTC未收盘，剔除防重绘）
            out.append((dt, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[6])))
    return out


def _fetch_coingecko(symbol, start):
    coin = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum"}.get(symbol.upper(), "bitcoin")
    days = max((date.today() - start).days + 2, 2)
    url = ("https://api.coingecko.com/api/v3/coins/%s/market_chart"
           "?vs_currency=usd&days=%d" % (coin, min(days, 365)))
    d = _get_json(url)
    # prices: [[ms, price], ...] 按小时/天，聚合为日K（简化：取每日首尾）
    by_day = {}
    for ms, p in d["prices"]:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
        by_day.setdefault(dt, []).append(p)
    out = []
    today_utc = datetime.now(timezone.utc).date()
    for dt in sorted(by_day):
        if dt >= start and dt < today_utc:  # 只收完整日K（防重绘）
            ps = by_day[dt]
            out.append((dt, ps[0], max(ps), min(ps), ps[-1], 0.0))
    return out


def fetch_crypto(symbol="BTCUSDT", start=None):
    """加密日K：Kraken 主，CoinGecko 备。返回 [(date, o,h,l,c, vol), ...] 升序。"""
    start = start or (date.today() - timedelta(days=365))
    try:
        return _fetch_kraken(symbol, start)
    except Exception as e:  # noqa: BLE001
        print("Kraken 失败，切 CoinGecko: %s" % e)
        return _fetch_coingecko(symbol, start)


def fetch_hkdcny():
    """港元兑人民币（新浪 hq.sinajs.cn/list=HKDCNY，需 Referer）。
    返回 (hkdcny, usdcny_est)，usdcny_est = hkdcny × 7.8（联系汇率估算，声明值）。
    失败返回 (None, None)。"""
    url = "https://hq.sinajs.cn/list=HKDCNY"
    try:
        req = urllib.request.Request(url, headers={**UA, "Referer": "https://finance.sina.com.cn"})
        with _OPENER.open(req, timeout=20) as r:
            txt = r.read().decode("gbk", "ignore")
        # var hq_str_HKDCNY="00:47:33,0.8544,...,港元人民币,2026-10-02";
        seg = txt.split('"')[1].split(",")
        v = float(seg[1])
        if 0.5 < v < 1.5:
            return v, round(v * 7.8, 4)
    except Exception:
        pass
    return None, None


if __name__ == "__main__":
    start = date.today() - timedelta(days=10)
    for code in ["HK9660", "HK0354"]:
        rows = fetch_kline(code, start)
        print(code, "rows=%d" % len(rows),
              "last=%s close=%s" % (rows[-1][0], rows[-1][4]) if rows else "EMPTY")
    rows = fetch_crypto("BTCUSDT", start)
    print("BTCUSDT(via Kraken)", "rows=%d" % len(rows),
          "last=%s close=%s" % (rows[-1][0], rows[-1][4]) if rows else "EMPTY")
    hkdcny, usdcny = fetch_hkdcny()
    print("HKDCNY=%s USDCNY(est)=%s" % (hkdcny, usdcny))
