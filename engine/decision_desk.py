#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""8502 决策台：只读读取引擎数据库，提供透明的仓位风险试算，不提交订单。"""
from __future__ import annotations

import math


def calculate_position_size(
    equity: float,
    cash: float,
    position_value: float,
    price: float,
    stop_price: float,
    risk_pct: float,
    lot_size: float = 100,
    max_position_pct: float = 70,
) -> dict:
    """按单笔风险、现金、总仓位上限三重约束计算理论数量；不含费用和滑点。"""
    values = (equity, cash, position_value, price, stop_price, risk_pct, lot_size, max_position_pct)
    try:
        if not all(math.isfinite(float(v)) for v in values):
            return {"valid": False, "reason": "输入包含非有限数值"}
    except (TypeError, ValueError):
        return {"valid": False, "reason": "输入必须为数字"}
    if equity <= 0 or cash < 0 or price <= 0 or stop_price <= 0 or lot_size <= 0:
        return {"valid": False, "reason": "账户、价格或交易单位无效"}
    if stop_price >= price:
        return {"valid": False, "reason": "止损价必须低于参考价格"}
    if not 0 < risk_pct <= 100 or not 0 < max_position_pct <= 100:
        return {"valid": False, "reason": "风险比例或仓位上限超出有效范围"}

    distance = price - stop_price
    risk_budget = equity * risk_pct / 100.0
    risk_qty = math.floor((risk_budget / distance) / lot_size + 1e-12) * lot_size
    cash_qty = math.floor((cash / price) / lot_size + 1e-12) * lot_size
    exposure_room = max(equity * max_position_pct / 100.0 - max(position_value, 0), 0)
    exposure_qty = math.floor((exposure_room / price) / lot_size + 1e-12) * lot_size
    qty = max(0.0, min(risk_qty, cash_qty, exposure_qty))
    notional = qty * price
    estimated_loss = qty * distance
    return {
        "valid": True,
        "reason": "",
        "risk_budget": risk_budget,
        "stop_distance": distance,
        "risk_qty": risk_qty,
        "cash_qty": cash_qty,
        "exposure_qty": exposure_qty,
        "quantity": qty,
        "notional": notional,
        "estimated_loss": estimated_loss,
        "risk_used_pct": estimated_loss / equity * 100.0,
        "exposure_after_pct": (max(position_value, 0) + notional) / equity * 100.0,
    }


def _atr(bars, period=14):
    """以最近真实波幅均值作为止损输入参考，不改动引擎规则。bars: date,o,h,l,c,v。"""
    true_ranges = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1][4]
        high, low = bars[i][2], bars[i][3]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not true_ranges:
        return None
    window = true_ranges[-period:]
    return sum(window) / len(window)


def _signal_columns(q):
    try:
        return {str(row[1]) for row in q("PRAGMA table_info(signals)")}
    except Exception:
        return set()


def render_decision_desk(q, cfg, accounts, account_names, symbol_names,
                         strategy_of, strategy_desc, quote_state, db_path):
    """显示来自 8502 既有数据库/策略配置的只读决策工作区。"""
    import pandas as pd
    import streamlit as st
    from engine.indicators import rsi, macd, adx as calc_adx, market_regime

    st.markdown('<div class="section-head">🧭 决策台</div>', unsafe_allow_html=True)
    st.caption("读取 8502 历史行情、虚拟账户、持仓和引擎信号。这里只复核交易计划，不写订单、不改策略、不连接券商。")

    left, right = st.columns([1, 2])
    with left:
        account = st.selectbox("交易账户", accounts,
                               format_func=lambda x: account_names.get(x, x), key="decision_account")
    market = {"sim_a": "a", "sim_hk": "hk", "sim_crypto": "crypto"}.get(account)
    symbol_rows = q("SELECT DISTINCT symbol FROM market_data WHERE market=? ORDER BY symbol", (market,))
    symbols = [str(row[0]) for row in symbol_rows if row and row[0]]
    universe = (cfg.get("universe") or {}).get(account, [])
    configured = {str(s).zfill(6) if str(s).isdigit() and account == "sim_a" else str(s) for s in universe}
    matched = [s for s in symbols if s in configured]
    if matched:
        symbols = matched
    if not symbols:
        st.warning(f"{account_names.get(account, account)}账户在数据库中没有可分析的行情标的。")
        return
    with right:
        symbol = st.selectbox("标的（数据库行情）", symbols,
                              format_func=lambda s: f"{s}  {symbol_names.get(s, '')}".strip(),
                              key="decision_symbol")

    bars = q("SELECT date,open,high,low,close,volume FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 90",
             (symbol,))
    bars = list(reversed(bars))
    if not bars:
        st.warning("该标的暂无有效历史行情，已停止计算。")
        return

    latest_date, _, _, _, last_close, _ = bars[-1]
    latest_date = str(latest_date)
    state, _ = quote_state("", "", latest_date)
    state_text = {"live": "实时", "close": "最近收盘", "stale": "已过期", "unknown": "未知"}.get(state, state)
    strategy = strategy_of(account)
    strategy_text = "策略停用" if strategy in ("disabled", None, "") else strategy_desc(strategy)
    currency = {"sim_a": "CNY", "sim_hk": "HKD", "sim_crypto": "USDT"}.get(account, "")
    st.caption(f"数据日期：{latest_date} · 数据状态：{state_text} · 账户策略：{strategy_text}")

    equity_row = q("SELECT nav,cash,position_value FROM daily_report WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1",
                   (account,))
    equity, cash, position_value = equity_row[0] if equity_row else (0, 0, 0)
    equity, cash, position_value = float(equity or 0), float(cash or 0), float(position_value or 0)
    pos_rows = q("SELECT qty,avg_cost,market_value FROM positions WHERE account=? AND symbol=? AND qty>0",
                 (account, symbol))
    qty_held, avg_cost, market_value = pos_rows[0] if pos_rows else (0, 0, 0)
    qty_held, avg_cost, market_value = float(qty_held or 0), float(avg_cost or 0), float(market_value or 0)

    cols = st.columns(4)
    cols[0].metric("最新收盘", f"{float(last_close):,.4f}")
    cols[1].metric("账户净值", f"{equity:,.2f} {currency}" if equity else "暂无账户快照")
    cols[2].metric("可用现金", f"{cash:,.2f} {currency}" if equity else "—")
    cols[3].metric("当前持仓", f"{qty_held:,.4f}" if qty_held % 1 else f"{qty_held:,.0f}")

    frame = pd.DataFrame(bars, columns=["日期", "开盘", "最高", "最低", "收盘", "成交量"])
    st.line_chart(frame.set_index("日期")["收盘"], height=220)
    closes = [float(row[4]) for row in bars]
    highs = [float(row[2]) for row in bars]
    lows = [float(row[3]) for row in bars]
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    rsi_values = rsi(closes)
    _, _, macd_hist = macd(closes)
    adx_values = calc_adx(highs, lows, closes)
    cols = st.columns(4)
    cols[0].metric("MA20", f"{ma20:.4f}" if ma20 is not None else "样本不足")
    cols[1].metric("RSI(14)", f"{rsi_values[-1]:.1f}" if rsi_values and rsi_values[-1] is not None else "样本不足")
    cols[2].metric("MACD", "多头" if macd_hist and macd_hist[-1] > 0 else "空头/未确认")
    cols[3].metric("市场状态", market_regime(adx_values[-1]) if adx_values else "未知")
    st.caption("技术指标仅整理已有行情，不会触发新的交易策略。")

    cols_available = _signal_columns(q)
    fields = ["date", "side", "price"]
    fields.append("action_type" if "action_type" in cols_available else "NULL AS action_type")
    fields.append("stop_price" if "stop_price" in cols_available else "NULL AS stop_price")
    signal = q(f"SELECT {', '.join(fields)} FROM signals WHERE account=? AND symbol=? ORDER BY date DESC LIMIT 1",
               (account, symbol))
    if signal:
        sig_date, sig_side, sig_price, sig_action, sig_stop = signal[0]
        st.write(f"**最近引擎信号**：{sig_date} · {sig_side} · 参考价 {float(sig_price):,.4f}"
                 + (f" · 动作 {sig_action}" if sig_action else "")
                 + (f" · 止损 {float(sig_stop):,.4f}" if sig_stop else ""))
    else:
        sig_date, sig_side, sig_price, sig_action, sig_stop = (None, None, None, None, None)
        st.info("该账户/标的暂无引擎信号。研究结论不等于引擎信号。")

    if equity <= 0:
        st.warning("账户净值快照缺失或无效。停止仓位计算，请先检查账户账本。")
        return

    lot_cfg = cfg.get("lots", {}) or {}
    lot_size = float(lot_cfg.get(symbol, 1e-5 if account == "sim_crypto" else 100))
    risk_cfg = cfg.get("risk", {}) or {}
    max_pct = float(risk_cfg.get("max_crypto_position_pct" if account == "sim_crypto" else "max_stock_position_pct",
                                 100 if account == "sim_crypto" else 70))
    risk_default = min(max(float(risk_cfg.get("single_trade_risk_pct", 1.0)), 0.25), 5.0)
    atr_value = _atr(bars)
    atr_mult = float(risk_cfg.get("atr_stop_mult", 2.0))
    fallback_stop = float(last_close) - atr_value * atr_mult if atr_value and float(last_close) > atr_value * atr_mult else float(last_close) * 0.95
    stop_default = float(sig_stop) if sig_side == "BUY" and sig_stop and 0 < float(sig_stop) < float(last_close) else fallback_stop
    stop_default = max(stop_default, float(last_close) * 0.01)
    step = max(float(last_close) * 0.001, 0.000001)

    st.markdown("#### 交易计划试算")
    c1, c2, c3 = st.columns(3)
    with c1:
        risk_pct = st.number_input("单笔风险上限（净值 %）", min_value=0.25, max_value=5.0,
                                   value=risk_default, step=0.25, format="%.2f", key="decision_risk_pct")
    with c2:
        stop_price = st.number_input("计划止损价", min_value=0.000001, max_value=float(last_close) * 1.5,
                                     value=float(stop_default), step=step, format="%.6f", key=f"decision_stop_{account}_{symbol}")
    with c3:
        target_default = float(last_close) + max(float(last_close) - float(stop_default), step) * 1.8
        target_price = st.number_input("第一目标价", min_value=0.000001, max_value=max(float(last_close) * 5, target_default * 2),
                                       value=float(target_default), step=step, format="%.6f", key=f"decision_target_{account}_{symbol}")

    risk = calculate_position_size(equity, cash, position_value, float(last_close), float(stop_price),
                                   float(risk_pct), lot_size, max_pct)
    rr = (float(target_price) - float(last_close)) / (float(last_close) - float(stop_price)) if float(stop_price) < float(last_close) else None
    r1, r2, r3, r4 = st.columns(4)
    r1.metric("单笔风险预算", f"{risk.get('risk_budget', 0):,.2f} {currency}")
    r2.metric("理论数量上限", f"{risk.get('quantity', 0):,.6g}" if risk.get("valid") else "—")
    r3.metric("参考金额", f"{risk.get('notional', 0):,.2f} {currency}" if risk.get("valid") else "—")
    r4.metric("潜在收益风险比", f"{rr:.2f}:1" if rr is not None else "止损价无效")
    if not risk.get("valid"):
        st.warning(risk.get("reason", "仓位试算无效"))
    elif risk["quantity"] <= 0:
        st.warning("在当前现金、止损距离和组合仓位限制下，理论可用数量为 0。")
    else:
        st.caption(f"按止损反推数量：风险预算 {risk['risk_budget']:,.2f} {currency}；估算止损风险 {risk['estimated_loss']:,.2f} {currency}；计划后仓位 {risk['exposure_after_pct']:.1f}%（上限 {max_pct:.0f}%）。")
    st.caption("理论试算未计手续费、滑点、跳空损失或流动性影响，不是可执行订单数量。")

    display_only = {str(s).zfill(6) if str(s).isdigit() else str(s) for s in (cfg.get("display_only") or [])}
    is_display_only = symbol in display_only
    action = st.selectbox("本轮动作", ["观望", "买入候选", "持有跟踪", "卖出/减仓"], key="decision_action")
    c1, c2, c3 = st.columns(3)
    with c1:
        checked_data = st.checkbox("已核实行情日期与价格", key="decision_checked_data")
    with c2:
        checked_stop = st.checkbox("已确认止损和第一目标", key="decision_checked_stop")
    with c3:
        checked_risk = st.checkbox("已核对现金与组合风险", key="decision_checked_risk")
    if st.button("生成决策复核", key="decision_review"):
        checks_ok = checked_data and checked_stop and checked_risk
        enough_bars = len(bars) >= 30
        fresh = state in ("live", "close")
        if action == "观望":
            result = ("观望是有效决策。等待引擎信号或更清晰的价量结构；本轮不生成订单指引。", "info")
        elif action == "持有跟踪":
            result = ("按既有策略检查持仓成本、止损触发条件与下一次复核时间；本页不改变持仓或风控状态。", "info")
        elif action == "卖出/减仓":
            if qty_held <= 0:
                result = ("账户当前没有该标的持仓；不生成卖出指引。", "warning")
            elif not fresh or not enough_bars or not checks_ok:
                result = ("未通过行情新鲜度、历史样本或人工检查项；失败关闭，不生成订单指引。", "warning")
            else:
                result = ("可进入人工卖出复核。仍需遵守原有风控、T+1 和券商可成交价；本操作不会写入订单。", "success")
        else:
            blockers = []
            if not fresh:
                blockers.append(f"行情状态为{state_text}")
            if not enough_bars:
                blockers.append(f"历史样本仅{len(bars)}根，少于30根")
            if not checks_ok:
                blockers.append("下单前检查项未全部确认")
            if strategy in ("disabled", None, ""):
                blockers.append("该账户策略当前停用")
            if is_display_only:
                blockers.append("该标的配置为 display_only")
            if not risk.get("valid") or risk.get("quantity", 0) <= 0:
                blockers.append("仓位风险试算未通过")
            if rr is None or rr < 1.8:
                blockers.append("第一目标收益风险比低于1.8:1")
            if blockers:
                result = ("暂不通过：" + "；".join(blockers) + "。", "warning")
            elif sig_side != "BUY":
                result = ("仅为研究候选：最近引擎信号不是 BUY。不得把手工分析视为引擎入场信号；本页不生成订单。", "warning")
            else:
                result = ("基础检查通过，可进入人工复核。仍需通过现有订单校验并核对组合风险；此页面不提交订单。", "success")
        message, level = result
        getattr(st, {"success": "success", "warning": "warning", "info": "info"}[level])(message)

    with st.expander("规则与数据说明", expanded=False):
        st.write("行情、账户、持仓与引擎信号只读自当前 8502 数据库。")
        st.write("账户策略、交易单位、标的限制和风险上限读取 config/strategy.yaml。")
        st.write("决策复核不会写入 SQLite、不生成券商订单，也不改变系统开始/暂停状态。")
