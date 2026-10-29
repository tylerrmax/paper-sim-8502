#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据流健康与安全降级（2026-10-30 四项强制安全补充 ③ 的后端实现）。

四态（由真实请求结果 + 数据时间戳共同判定，不凭页面能打开）：
- NORMAL 正常：请求成功，数据未过期；
- DELAYED 延迟：连接正常，但数据滞后超过预期、未达冻结线；
- DISCONNECTED 断连：接口超时/连接失败/健康检查失败/数据过期越线；
- ANOMALOUS 异常：空响应、格式错误、关键字段缺失、校验不通过。

后端强制口径（前端指示只是同步显示）：
- BUY（含跟单）：DISCONNECTED / ANOMALOUS 一律冻结；日线口径下
  单标的滞后 > FRESH_LIMIT_DAYS 天按过期处理（与 risk_gate.
  check_fresh 同一阈值），冻结该标的新买入。
- SELL / 止损等风险退出：不走本模块的买入冻结，按独立安全规则
  （持仓核查 + 策略止损）处理，不因数据故障被一刀切冻结，
  也不因此被无条件放行。
- 恢复：必须先重新校验时间戳/字段完整性/风控输入（revalidate）
  才恢复；断连期间积压的旧 BUY 意图不自动补发——
  expire_queued_buys() 将其置为 rejected 并留痕，需新信号重新生成。
- 旧缓存只可作诊断展示，必须带数据时间戳与失效状态标注
  （label_stale），不得伪装成最新数据。
"""
from __future__ import annotations

from datetime import datetime

NORMAL = "normal"
DELAYED = "delayed"
DISCONNECTED = "disconnected"
ANOMALOUS = "anomalous"

LABELS = {NORMAL: "正常", DELAYED: "延迟",
          DISCONNECTED: "断连", ANOMALOUS: "异常"}

# 日线数据源的滞后期望：T+1 源（如 Kraken 日线）gap<=1 正常；
# 2..FRESH_LIMIT 为延迟；超过 FRESH_LIMIT 视为过期（冻结该标的 BUY）。
FRESH_LIMIT_DAYS = 4


def _to_date(v):
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def classify(*, fetch_ok=True, fetch_error=None, payload_ok=True,
             last_bar_date=None, ref_date=None):
    """由一次真实取数结果判定健康态。返回 (state, reason)。"""
    if not fetch_ok:
        return DISCONNECTED, "接口失败：%s" % (fetch_error or "超时/连接失败")
    if not payload_ok:
        return ANOMALOUS, "数据格式错误/关键字段缺失/校验不通过"
    if last_bar_date is None:
        return ANOMALOUS, "空响应：无行情数据"
    if ref_date is not None:
        gap = (_to_date(ref_date) - _to_date(last_bar_date)).days
        if gap > FRESH_LIMIT_DAYS:
            return DISCONNECTED, ("数据过期：最新 %s 距参照 %s 已 %d 天"
                                  "（>%d 天冻结线）"
                                  % (last_bar_date, ref_date, gap,
                                     FRESH_LIMIT_DAYS))
        if gap >= 2:
            return DELAYED, ("数据延迟：最新 %s 距参照 %s %d 天"
                             % (last_bar_date, ref_date, gap))
    return NORMAL, "数据源可用，数据未过期"


def buy_blocked(state):
    """后端买入冻结判定（SELL 不调用本函数）。"""
    return state in (DISCONNECTED, ANOMALOUS)


def symbol_buy_health(symbol, last_bar_date, ref_date):
    """单标的新鲜度 → (state, reason, blocked)。接入执行层：
    滞后越线时后端拒绝该标的新 BUY，而不是只 WARN 沿用旧价。"""
    state, reason = classify(last_bar_date=last_bar_date,
                              ref_date=ref_date)
    return state, reason, buy_blocked(state)


def label_stale(state, data_ts):
    """诊断展示旧数据时必须带的标注。"""
    if state == NORMAL:
        return "数据时间 %s" % data_ts
    return "⚠ 数据已失效（%s）：数据时间 %s，不得作为最新数据使用" % (
        LABELS.get(state, state), data_ts)


def revalidate(*, fetch_ok, payload_ok, last_bar_date, ref_date,
               risk_inputs_ok):
    """恢复前重校验：时间戳、字段完整性、风控输入三者全过才恢复。"""
    state, reason = classify(fetch_ok=fetch_ok, payload_ok=payload_ok,
                             last_bar_date=last_bar_date,
                             ref_date=ref_date)
    if state != NORMAL:
        return False, state, "恢复校验未过：%s" % reason
    if not risk_inputs_ok:
        return False, ANOMALOUS, "恢复校验未过：风控输入不完整"
    return True, NORMAL, "恢复校验通过"


def expire_queued_buys(con, *, reason="数据断连期间积压，恢复后不自动补发"):
    """把仍在待审核/风控检查/已放行状态的 BUY 意图全部作废留痕。
    返回作废条数。SELL 意图不受影响。"""
    from engine import safety
    safety.ensure_safety_tables(con)
    rows = con.execute(
        "SELECT idempotency_key FROM trade_intents"
        " WHERE side='BUY' AND state IN (?,?,?)",
        (safety.PENDING_REVIEW, safety.RISK_CHECK,
         safety.RELEASED)).fetchall()
    n = 0
    for (key,) in rows:
        cur = con.execute(
            "UPDATE trade_intents SET state=?, updated_at=?"
            " WHERE idempotency_key=? AND state IN (?,?,?)",
            (safety.REJECTED, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             key, safety.PENDING_REVIEW, safety.RISK_CHECK,
             safety.RELEASED))
        if cur.rowcount == 1:
            con.execute(
                "INSERT INTO intent_transitions"
                "(idempotency_key,from_state,to_state,note,at)"
                " VALUES(?,?,?,?,?)",
                (key, "queued", safety.REJECTED, reason,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            n += 1
    return n
