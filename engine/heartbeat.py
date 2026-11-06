#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时行情心跳监控（第二层安全监控 P3）。

设计原则：
- 只追加（JSONL）、零交易依赖：本模块不被任何交易/信号/风控路径调用，
  唯一写入方是 engine/realtime.py 的展示取数埋点；心跳异常只供告警，
  绝不触发或阻断交易。
- HTTP 200 不等于行情有效：分四阶段记录——request（请求发起）、
  response（拿到原始响应）、valid（响应可解析且至少 1 个标的含有效
  价格、源时间戳不荒谬）、persisted（有效报价交付调用方）。
- 源时间戳合理界：不晚于 now+5 分钟、不早于 now-24 小时。盘中报价
  时间戳为当日时间，24 小时界可容忍跨日/时区偏差，又能拦住明显错误
  的时间（如 2099 年、1970 年）；无可用源时间戳的源（如 Binance
  ticker 的文本时间）以接收时间近似，并在校验结果中注明。

异常判定（status）：
- cn_hk（腾讯 A股/港股）：交易日（周一至周五）09:30–15:05 CST 窗口内，
  距最近 valid 事件（当日窗口开始后）超过 10 分钟 → 异常；窗口外不报。
  注：未接交易日历，节假日可能误报——本模块只告警，误报代价可接受
  （见 EVIDENCE 剩余风险）。
- crypto（Binance，24/7）：超过 30 分钟无 valid → 异常；从未有 valid
  亦为异常（没有心跳即视为异常，符合心跳监控语义）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    CST = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - zoneinfo 缺失时的兜底
    CST = timezone(timedelta(hours=8))

STAGES = ("request", "response", "valid", "persisted")
SOURCES = ("cn_hk", "crypto")
SOURCE_LABELS = {"cn_hk": "腾讯 A股/港股", "crypto": "Binance 加密"}

ENV_PATH = "PAPER_SIM_HEARTBEAT_PATH"

# A/H 交易窗口（CST）与无有效行情容忍时长
_CN_HK_OPEN = (9, 30)
_CN_HK_CLOSE = (15, 5)
_CN_HK_TOLERANCE = timedelta(minutes=10)
_CRYPTO_TOLERANCE = timedelta(minutes=30)

# 源时间戳合理界
_SRC_TS_FUTURE = timedelta(minutes=5)
_SRC_TS_PAST = timedelta(hours=24)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_path():
    """心跳日志路径：环境变量覆盖 > 仓库 reports/heartbeat.jsonl。"""
    env = os.environ.get(ENV_PATH)
    if env:
        return env
    return os.path.join(_repo_root(), "reports", "heartbeat.jsonl")


def _resolve(path):
    return path if path else default_path()


def _now():
    return datetime.now(CST)


def _as_cst(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=CST)
    return dt.astimezone(CST)


def _iso(dt):
    return _as_cst(dt).isoformat(timespec="seconds")


def record(source, stage, ok, detail=None, ts=None, path=None):
    """追加一条心跳事件。IO 失败会抛异常——调用方（realtime 埋点）
    必须 try/except 吞掉，绝不能影响取数主流程。"""
    if stage not in STAGES:
        raise ValueError(f"unknown heartbeat stage: {stage!r}")
    when = _as_cst(ts) if ts is not None else _now()
    evt = {
        "ts": _iso(when),
        "source": str(source),
        "stage": stage,
        "ok": bool(ok),
        "detail": None if detail is None else str(detail),
    }
    target = _resolve(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(evt, ensure_ascii=False) + "\n")
    return evt


def read_events(path=None):
    """读全部事件（容忍坏行，逐行跳过）。文件不存在返回空列表。"""
    target = _resolve(path)
    if not os.path.exists(target):
        return []
    out = []
    with open(target, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
                evt["_dt"] = _as_cst(datetime.fromisoformat(evt["ts"]))
                out.append(evt)
            except Exception:
                continue
    return out


def _parse_src_ts(value):
    """解析源时间戳字符串，失败返回 None。"""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=CST)
        except ValueError:
            continue
    return None


def check_quotes(quotes, now=None, src_times=None):
    """有效性校验：返回 (ok, reason)。

    quotes: realtime 返回的 {symbol: {"price":..., "time":...}}。
    src_times: 可选的源时间戳 datetime 列表（优先于 quotes 内的文本
    时间，如 Binance closeTime）。无任何可解析源时间戳时以接收时间
    近似（reason 中注明），只校验价格。
    """
    now = _as_cst(now) if now is not None else _now()
    if not quotes:
        return False, "无任何含有效价格的标的"
    priced = [s for s, q in quotes.items()
              if isinstance(q, dict) and (q.get("price") or 0) > 0]
    if not priced:
        return False, "无任何含有效价格的标的"
    times = []
    if src_times:
        times.extend(_as_cst(t) for t in src_times if t is not None)
    for sym in priced:
        t = _parse_src_ts(quotes[sym].get("time"))
        if t is not None:
            times.append(t)
    if not times:
        return True, "价格有效；源无可用时间戳，以接收时间近似"
    for t in times:
        if t > now + _SRC_TS_FUTURE:
            return False, f"源时间戳在未来：{t.isoformat(timespec='seconds')}"
        if t < now - _SRC_TS_PAST:
            return False, f"源时间戳过旧：{t.isoformat(timespec='seconds')}"
    return True, "价格与源时间戳均有效"


def _cn_hk_anomaly(now, last_valid):
    """返回 (anomaly, reason)。now/last_valid 均为 CST datetime。"""
    if now.weekday() >= 5:
        return False, "周末休市，窗口外不判定"
    start = now.replace(hour=_CN_HK_OPEN[0], minute=_CN_HK_OPEN[1],
                        second=0, microsecond=0)
    end = now.replace(hour=_CN_HK_CLOSE[0], minute=_CN_HK_CLOSE[1],
                      second=0, microsecond=0)
    if now < start or now > end:
        return False, "不在交易窗口（09:30–15:05），不判定"
    ref = start
    if last_valid is not None and last_valid >= start:
        ref = last_valid
    gap = now - ref
    if gap > _CN_HK_TOLERANCE:
        return True, (f"交易窗口内已 {int(gap.total_seconds() // 60)} 分钟"
                      f"无有效行情（容忍 {_CN_HK_TOLERANCE.seconds // 60} 分钟）")
    return False, "交易窗口内有效行情新鲜"


def _crypto_anomaly(now, last_valid):
    if last_valid is None:
        return True, "从未记录到有效行情"
    gap = now - last_valid
    if gap > _CRYPTO_TOLERANCE:
        return True, (f"已 {int(gap.total_seconds() // 60)} 分钟无有效行情"
                      f"（容忍 {_CRYPTO_TOLERANCE.seconds // 60} 分钟）")
    return False, "有效行情新鲜"


def status(now=None, path=None):
    """汇总各源最近各阶段时间与异常判定（只读，不写文件）。"""
    now = _as_cst(now) if now is not None else _now()
    events = read_events(path)
    result = {"generated_at": _iso(now), "sources": {}}
    for source in SOURCES:
        last = {stage: None for stage in STAGES}
        last_valid = None
        for evt in events:
            if evt.get("source") != source or evt.get("stage") not in last:
                continue
            if not evt.get("ok"):
                continue
            dt = evt["_dt"]
            if last[evt["stage"]] is None or dt > last[evt["stage"]]:
                last[evt["stage"]] = dt
            if evt["stage"] == "valid":
                last_valid = dt
        if source == "cn_hk":
            anomaly, reason = _cn_hk_anomaly(now, last_valid)
        else:
            anomaly, reason = _crypto_anomaly(now, last_valid)
        age_min = None
        if last_valid is not None:
            age_min = round((now - last_valid).total_seconds() / 60.0, 1)
        result["sources"][source] = {
            "label": SOURCE_LABELS[source],
            "last": {k: (_iso(v) if v is not None else None)
                     for k, v in last.items()},
            "last_valid": _iso(last_valid) if last_valid else None,
            "valid_age_min": age_min,
            "anomaly": anomaly,
            "reason": reason,
        }
    result["anomaly"] = any(s["anomaly"] for s in result["sources"].values())
    return result
