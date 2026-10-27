#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日线模拟管线：signals / positions / daily_report 全量重算（幂等）；
fills 追加制——历史成交一旦产生即为事实，切换暂停/手动等控制状态
不得删除历史成交，只决定新信号是否成交。
（2026-10-02 修复：此前 DELETE fills 会导致切手动模式时历史成交被清空。）

执行假设（声明，见 config/strategy.yaml）：
  - T+1 开盘执行：信号日收盘生成信号，次一交易日开盘价成交
    （BUY 按 open*(1+slippage)，SELL 按 open*(1-slippage)）；
  - 风险禁入（P0-1，禁入优先）：BUY 信号价/执行价跌破母板止损参考
    stop_ref 的一律不执行（buyback 不例外），拦截写入 blocked_signals；
  - 市场环境闸（模块A，母板真源三态，engine.regime）：判定只读母板
    字段（B31/实盘验证E14放行/B79，经 workbook_map 抽取）——允许进攻/
    谨慎交易（套母板验证期系数 0.5 与总仓位上限 0.3）/禁止新开仓；
    数据缺失或映射过期 fail-closed 降级为禁止新开仓。禁开态下换仓
    豁免（补齐①额度券制，engine.rotation）：仅主动信号卖出生成额
    度券（扣费后净回收计额、绑定来源成交、一券一买、5 交易日有
    效、防再生），BUY 在券面额内放行、超出拦截（均写入
    blocked_signals 留痕）；degraded 降级态禁用额度券；SELL 永不
    受环境闸；
  - BUY 仓位：信号日 ≥2026-10-26 用风险定仓 v2（数量=风险预算÷每股
    风险；每股风险=止损距离+每股往返成本（费率+滑点+最低收费摊算），
    止损距离=max(入场−母板stop_ref, 2×ATR)；再受总仓位/现金（含费）/
    风险簇预算 4%（同板块∪corr>0.8）/换仓额度封顶；止损无效、风险
    数据缺失、缩量后不足一手一律禁单留痕）；此前信号沿用旧口径
    （账户现金的 buy_frac，股票向下取整到 lot_size，加密到 min_qty）
    ——历史不可改写；
  - SELL 全平可用仓位；A股 T+1（engine.sim 已处理）；
  - A股涨停日不执行 BUY、跌停日不执行 SELL（简化）；
  - 同一日多标的信号按 symbol 字母序执行；
  - 无数据标的自动跳过（A股休市期间 sim_a 为空盘）。
"""
from __future__ import annotations

import fcntl
import math
import os
import sqlite3
from datetime import datetime, timedelta

from engine import risk_gate
from engine.rotation import RotationBook
from engine.sim import SimAccount, Lot, check_limit
from engine.signals import gen_signals, gen_buyback_signals

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "db", "sim.db")
REPORT_DIR = os.path.join(BASE, "reports")

# 验证期起点（分账户）：与 sync_snapshot.py 的 VAL_START 保持一致。
# 回填的历史 K 线仅用于指标预热（MA20 计算），此日期之前的信号禁止入库，
# 避免污染验证窗口、避免 manual 模式下积压历史待审批。
VAL_START = {"sim_crypto": "2026-10-03",
             "sim_a": "2026-10-08", "sim_hk": "2026-10-08"}


def _fee_for(account_id, side, cfg):
    """费率与最低收费全部读 config/strategy.yaml fees（P1-8 与
    order_validate 同源，禁止硬编码——此前港股 min_fee 硬编码 0 与
    yaml 的 50 打架）。min_fee 只用于 BUY（sim.buy）；SELL 按费率计。"""
    f = cfg["fees"]
    if account_id == "sim_a":
        return (f["a_buy"] if side == "BUY" else f["a_sell"]), float(f.get("a_min_fee", 5))
    if account_id == "sim_hk":
        return (f["hk_buy"] if side == "BUY" else f["hk_sell"]), float(f.get("hk_min_fee", 50))
    return f["crypto"], 0.0


def _replay_fill(sim, s, side, qty, price, fee, d):
    """历史成交重放进 sim 状态（零漂移，不重新过门禁）。
    调用方保证按日期顺序调用，以维持 T+1 等时间相关逻辑正确。"""
    if side == "BUY":
        sim.cash -= qty * price + fee
        sim.positions.setdefault(s, []).append(Lot(qty, price, 0.0, d))
    else:
        pos_lots = sim.positions.get(s, [])
        left = qty
        for lot in pos_lots:
            if left <= 1e-9:
                break
            if lot.qty <= 1e-9:
                continue
            if sim.t1 and not (lot.buy_date < d):
                continue
            take = min(lot.qty, left)
            lot.qty -= take
            left -= take
        # 只按实际卖出的数量记现金（qty-left），防止幻影现金
        sold = qty - left
        sim.cash += sold * price - fee
        sim.positions[s] = [l for l in pos_lots if l.qty > 1e-9]


def position_sizer(sim, aid, symbol, px, cfg, strategy, buy_frac, lots):
    """仓位计算函数（Position Sizer）：强制风控拦截。

    返回 (qty, block_reason)：
    - qty > 0：允许下单数量（已按 lot 取整）
    - qty == 0：被风控拦截，block_reason 说明原因
    拦截规则（来自 config/strategy.yaml 的 risk 段）：
    1. 总仓位上限：股票 70% / 加密 100%，超限直接拦截
    2. 单笔风险上限 2%：仅 buyback（固定止损 -5%）可精确计算，
       按 (entry - stop) * qty <= nav * 2% 反推最大数量
    """
    import math
    risk_cfg = cfg.get("risk", {})
    lot = float(lots.get(symbol, 1))

    nav_now = sim.cash + sum(
        l.qty * l.cost for lots_ in sim.positions.values() for l in lots_)
    if nav_now <= 0:
        return 0, "净值为零"

    # 拦截1：总仓位上限
    max_pos_pct = (risk_cfg.get("max_stock_position_pct", 70) / 100
                   if aid in ("sim_a", "sim_hk")
                   else risk_cfg.get("max_crypto_position_pct", 100) / 100)
    pos_val = sum(l.qty * l.cost
                  for lots_ in sim.positions.values() for l in lots_)
    est_cost = sim.cash * buy_frac
    if (pos_val + est_cost) / nav_now > max_pos_pct:
        return 0, f"仓位超限：{(pos_val + est_cost) / nav_now * 100:.1f}% > {max_pos_pct * 100:.0f}%"

    # 基础数量
    qty = math.floor(sim.cash * buy_frac / px / lot) * lot
    if qty <= 0:
        return 0, "现金不足"

    # 拦截2：单笔风险上限（buyback 固定止损 -5%）
    if strategy == "buyback":
        risk_pct = risk_cfg.get("single_trade_risk_pct", 2) / 100
        risk_per_share = px * 0.05
        max_qty_risk = math.floor(nav_now * risk_pct / risk_per_share / lot) * lot
        if max_qty_risk <= 0:
            return 0, f"单笔风险超限：2% 风险对应数量为 0"
        if qty > max_qty_risk:
            qty = max_qty_risk

    return qty, ""


# 仓位计算 v2 生效日（第 13 项）：按信号日期分流——此前信号保持旧
# 算法逐笔不变（历史不可改写），此后信号一律风险定仓。
SIZING_EFFECTIVE_DATE = "2026-10-26"


def _round_trip_cost_ps(px, buy_rate, buy_min, sell_rate, sell_min,
                        slippage, prelim_qty):
    """每股往返成本（模块B-2）：比例费（买入+卖出，yaml 费率）+ 双边滑点
    （yaml slippage 单边，往返 2×；与毕业标准“往返约 0.55%”同口径）
    + 最低收费按预估数量摊算到每股。prelim_qty<=0 时最低收费项记 0，
    由调用方先以纯比例成本估预数量再代入重算（一次迭代）。"""
    rate_ps = px * (buy_rate + sell_rate + 2 * slippage)
    min_ps = ((buy_min + sell_min) / prelim_qty) if prelim_qty and prelim_qty > 0 else 0.0
    return rate_ps + min_ps


def position_sizer_v2(ctx):
    """仓位计算 v2（模块B）：数量 = 单笔风险预算 ÷ 每股风险，再依次受
    总仓位上限 / 可用现金（含费）/ 风险簇预算 / 换仓额度 封顶，按 lot 取整。

    - 风险预算 = 净值 × risk_pct（yaml single_trade_risk_pct，R-02 2%）；
      谨慎态系数 env_buy_coef 由模块A状态机给出，此处只消费（乘预算）。
    - 每股风险 = 止损距离 + 每股往返成本（见 _round_trip_cost_ps）：
      预算约束的是“打到止损时含费用的真实亏损”，不是名义价差。
    - 止损距离 = max(入场 − 母板 stop_ref, atr_stop_mult × ATR)，由 ctx
      构造方按 as-of（信号日）数据算好传入（stop_dist/stop_price）。
    - 风险簇：与候选同 sector_key 或近 60 日收益相关系数 >0.8 的已有
      持仓（取并集），其存量未实现风险由构造方汇总为
      cluster_existing_risk；簇存量 + 本笔 ≤ 净值 × cluster_budget_pct
      （yaml sector_risk_budget_pct，4%），超出按比例缩量。
    - fail-closed 硬禁单（返回 qty=0 且 reason 注明类别）：风险数据
      缺失（data_ok=False / 净值缺失）、止损无效（止损价 ≥ 入场价，
      或 stop_ref 与 ATR 皆缺）、风险簇预算已满、缩量后不足最小交易
      单位（<1 手）——绝不为凑单强行下最小单位。
    ctx 字段：nav/cash/entry_px/lot/risk_pct/buy_fee_rate/buy_min_fee/
    sell_fee_rate/sell_min_fee/slippage/stop_dist/stop_price/max_pos_pct/
    pos_value/cluster_existing_risk/cluster_budget_pct/cluster_label/
    env_buy_coef/env_total_cap/amount_cap/data_ok/data_missing_reason。
    返回 (qty, block_reason)。"""
    if not ctx.get("data_ok", True):
        return 0, "风险数据缺失：%s" % ctx.get("data_missing_reason", "未知")
    nav = ctx.get("nav") or 0.0
    if nav <= 0:
        return 0, "风险数据缺失：账户净值缺失或为零"
    px = ctx["entry_px"]
    stop_price = ctx.get("stop_price")
    if stop_price is not None and stop_price >= px:
        return 0, "止损无效：止损价 %.4f ≥ 入场价 %.4f" % (stop_price, px)
    stop_dist = ctx.get("stop_dist")
    if not stop_dist or stop_dist <= 0:
        return 0, ("止损无效：母板止损与 ATR 止损均无效或缺失"
                   "（stop_ref=%s，ATR止损=%s），拒绝建单"
                   % (ctx.get("stop_ref_raw"), ctx.get("atr_stop_raw")))
    lot = float(ctx.get("lot") or 1)

    def _floor(q):
        return math.floor(q / lot) * lot if lot > 0 else q

    # 风险预算（谨慎态系数来自模块A，只消费）
    risk_budget = nav * float(ctx.get("risk_pct", 0.02))
    if ctx.get("env_buy_coef"):
        risk_budget *= float(ctx["env_buy_coef"])
    buy_rate = float(ctx.get("buy_fee_rate", 0.0))
    buy_min = float(ctx.get("buy_min_fee", 0.0))
    sell_rate = float(ctx.get("sell_fee_rate", 0.0))
    sell_min = float(ctx.get("sell_min_fee", 0.0))
    slippage = float(ctx.get("slippage", 0.0))
    # 每股风险：先以纯比例成本估预数量，再摊最低收费重算（一次迭代）
    psr = stop_dist + _round_trip_cost_ps(px, buy_rate, buy_min, sell_rate,
                                          sell_min, slippage, None)
    prelim = risk_budget / psr if psr > 0 else 0.0
    psr = stop_dist + _round_trip_cost_ps(px, buy_rate, buy_min, sell_rate,
                                          sell_min, slippage, prelim)
    qty = _floor(risk_budget / psr)
    binding = "风险预算"
    # 封顶 1：总仓位上限（市值口径；谨慎态上限由 A 给出，取更严者）
    max_pos_pct = float(ctx.get("max_pos_pct", 0.70))
    if ctx.get("env_total_cap") is not None:
        max_pos_pct = min(max_pos_pct, float(ctx["env_total_cap"]))
    room_val = max_pos_pct * nav - float(ctx.get("pos_value", 0.0))
    q_pos = _floor(room_val / px) if room_val > 0 else 0.0
    if q_pos < qty:
        qty, binding = q_pos, "总仓位上限"
    # 封顶 2：可用现金（含费用）：名义额 + 预估买入费（比例/最低取大）≤ 现金
    cash = float(ctx.get("cash", 0.0))
    q_cash_rate = cash / (px * (1 + buy_rate)) if px > 0 else 0.0
    q_cash_min = (cash - buy_min) / px if px > 0 else 0.0
    q_cash = _floor(min(q_cash_rate, q_cash_min))
    if q_cash < qty:
        qty, binding = q_cash, "可用现金（含费用）"
    # 封顶 3：风险簇预算（存量 + 本笔 ≤ 净值 4%）
    cluster_budget = nav * float(ctx.get("cluster_budget_pct", 0.04))
    cluster_existing = float(ctx.get("cluster_existing_risk", 0.0))
    room_risk = cluster_budget - cluster_existing
    if room_risk <= 0:
        return 0, ("风险簇预算已满：%s 存量未实现风险 %.2f ≥ 预算 %.2f"
                   "（净值 %.0f%%），本笔不再开仓"
                   % (ctx.get("cluster_label") or "同簇持仓", cluster_existing,
                      cluster_budget,
                      float(ctx.get("cluster_budget_pct", 0.04)) * 100))
    q_clu = _floor(room_risk / psr)
    if q_clu < qty:
        qty, binding = q_clu, "风险簇预算"
    # 封顶 4：换仓额度券面额（模块A禁开态放行上限，金额口径）
    if ctx.get("amount_cap") is not None:
        q_amt = _floor(float(ctx["amount_cap"]) / px)
        if q_amt < qty:
            qty, binding = q_amt, "换仓额度"
    qty = _floor(qty)
    if qty <= 0 or qty < lot:
        if binding == "换仓额度":
            return 0, ("环境闸·禁止新开仓：换仓额度券面额不足（面额 %.2f 元，"
                       "不足一手）" % float(ctx.get("amount_cap") or 0.0))
        return 0, ("缩量后不足最小交易单位（绑定约束：%s，缩量结果 %s < 1 手 %s），"
                   "不强行下单" % (binding, qty, lot))
    # 终局校验（模块B-6，事后断言）：对最终成交数量逐项验限——数量
    # 算完不是终点。任一不通过 → 拒绝建单（不许向上凑手超预算）。
    final_risk = qty * psr
    if final_risk > risk_budget + 1e-6:
        return 0, ("终局校验不通过：最终风险 %.2f > 风险预算 %.2f"
                   % (final_risk, risk_budget))
    if float(ctx.get("pos_value", 0.0)) + qty * px > max_pos_pct * nav + 1e-6:
        return 0, "终局校验不通过：成交后总仓位超上限"
    if cluster_existing + final_risk > cluster_budget + 1e-6:
        return 0, "终局校验不通过：成交后风险簇风险超预算"
    if qty * px + max(qty * px * buy_rate, buy_min) > cash + 1e-6:
        return 0, "终局校验不通过：名义额+费用超可用现金"
    if ctx.get("amount_cap") is not None and \
            qty * px > float(ctx["amount_cap"]) + 1e-6:
        return 0, "终局校验不通过：成交额超换仓额度券面额"
    return qty, ""


def _atr_at(atr_by_sym, symbol, on_date):
    """atr_by_sym[symbol] 中 ≤on_date 的最近一个 ATR 值（无则 None）。"""
    m = atr_by_sym.get(symbol) or {}
    if on_date in m:
        return m[on_date]
    earlier = [k for k in m if k <= on_date]
    return m[max(earlier)] if earlier else None


def _stop_dist_and_sector(aid, symbol, px, atr_v, cfg):
    """目标止损距离与板块键：止损距离 = max(入场 − 母板 stop_ref,
    atr_stop_mult × ATR)；stop_ref 缺失只用 ATR 项，两者皆缺 → None。"""
    atr_mult = cfg.get("risk", {}).get("atr_stop_mult", 2.0)
    dists = []
    sr = risk_gate.stop_ref_of(symbol)
    if sr is not None:
        dists.append(px - sr)
    if atr_v:
        dists.append(atr_mult * atr_v)
    stop_dist = max(dists) if dists else None
    return stop_dist, risk_gate.sector_key_of(symbol), sr


def _pearson(xs, ys):
    n = len(xs)
    if n < 30:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def _corr_asof(closes_a, closes_b, asof, n=60):
    """近 n 日收益相关系数（as-of：只用 ≤asof 的共同交易日收盘）。
    共同日 <31（收益 <30）→ None（数据不足，不臆测相关性）。"""
    dates = sorted(d for d in set(closes_a) & set(closes_b) if d <= asof)
    if len(dates) < 31:
        return None
    sel = dates[-(n + 1):]
    ra = [closes_a[sel[i]] / closes_a[sel[i - 1]] - 1 for i in range(1, len(sel))]
    rb = [closes_b[sel[i]] / closes_b[sel[i - 1]] - 1 for i in range(1, len(sel))]
    return _pearson(ra, rb)


def _sizer_v2_ctx(sim, aid, symbol, px, sig_d, cfg, atr_by_sym, closes_by_sym,
                  last_px, env=None):
    """从 pipeline 运行期状态组装 position_sizer_v2 的 ctx。
    全部风险取数按 as-of = 信号日 sig_d（只用信号日及之前的数据）：
    估值价、ATR、相关性均截断到 sig_d；入场价 px 为执行价（T+1 开盘）。"""
    risk_cfg = cfg.get("risk", {})
    atr_mult = risk_cfg.get("atr_stop_mult", 2.0)

    def _px_asof(sym):
        cp = closes_by_sym.get(sym, {})
        if sig_d in cp:
            return cp[sig_d]
        earlier = [k for k in cp if k <= sig_d]
        if earlier:
            return cp[max(earlier)]
        return last_px.get(sym)

    positions = []  # (symbol, qty, px_asof)
    pos_value = 0.0
    for sym2, ll in sim.positions.items():
        q2 = sum(l.qty for l in ll)
        if q2 <= 0:
            continue
        p2 = _px_asof(sym2)
        if p2 is None:
            p2 = sum(l.qty * l.cost for l in ll) / q2
        positions.append((sym2, q2, p2))
        pos_value += q2 * p2
    nav = sim.cash + pos_value

    # ---- fail-closed 数据完整性（模块B-4②） ----
    data_ok, data_reason = True, ""
    cand_closes = closes_by_sym.get(symbol, {})
    n_cand = sum(1 for d in cand_closes if d <= sig_d)
    if n_cand < 20:
        data_ok = False
        data_reason = "%s 信号日 %s 前 bars 仅 %d 根，不足算 ATR/相关性" % (
            symbol, sig_d, n_cand)
    if aid in ("sim_a", "sim_hk") and symbol not in risk_gate.load_symbol_meta():
        data_ok = False
        data_reason = "workbook_map 无 %s 的记录（母板真源缺失）" % symbol

    # ---- 双止损取严（模块B-3，as-of）：母板 stop_ref 与 ATR 策略止损
    # 先统一到价格方向（均为入场价下方的止损价），定仓按每股潜在亏损
    # 更大的一侧（两止损中更低者）取距离——任一止损实际触发时亏损都
    # 不超预算。任一侧止损 ≥ 入场价视为该侧无效；两侧皆无效/缺失 →
    # stop_price=None，sizer 拒绝建单。 ----
    atr_v = _atr_at(atr_by_sym, symbol, sig_d)
    sr = risk_gate.stop_ref_of(symbol)
    atr_stop = (px - atr_mult * atr_v) if atr_v else None
    cand_stops = []
    if sr is not None and sr < px:
        cand_stops.append(sr)
    if atr_stop is not None:
        cand_stops.append(atr_stop)
    stop_price = min(cand_stops) if cand_stops else None
    stop_dist = (px - stop_price) if stop_price is not None else None

    # ---- 风险簇：同 sector_key 或 corr>0.8（并集），存量未实现风险 ----
    target_sector = risk_gate.sector_key_of(symbol)
    cand_cp = closes_by_sym.get(symbol, {})
    cluster_risk = 0.0
    members = []
    for sym2, q2, p2 in positions:
        same_sector = bool(target_sector) and \
            risk_gate.sector_key_of(sym2) == target_sector
        corr = _corr_asof(cand_cp, closes_by_sym.get(sym2, {}), sig_d)
        if same_sector or (corr is not None and corr > 0.8):
            sr2 = risk_gate.stop_ref_of(sym2)
            if sr2 is not None:
                dist2 = p2 - sr2
            else:
                a2 = _atr_at(atr_by_sym, sym2, sig_d)
                dist2 = atr_mult * a2 if a2 else 0.0
            cluster_risk += q2 * max(dist2, 0.0)
            members.append(sym2)
    cluster_label = ("板块 %s" % target_sector) if target_sector else "相关性簇"
    if members:
        cluster_label += "（%s）" % "/".join(sorted(members))

    buy_rate, buy_min = _fee_for(aid, "BUY", cfg)
    sell_rate, sell_min = _fee_for(aid, "SELL", cfg)
    max_pos_pct = (risk_cfg.get("max_stock_position_pct", 70) / 100
                   if aid in ("sim_a", "sim_hk")
                   else risk_cfg.get("max_crypto_position_pct", 100) / 100)
    ctx = {"nav": nav, "cash": sim.cash, "entry_px": px,
           "lot": float(cfg.get("lots", {}).get(symbol, 1)),
           "risk_pct": risk_cfg.get("single_trade_risk_pct", 2) / 100,
           "buy_fee_rate": buy_rate, "buy_min_fee": buy_min,
           "sell_fee_rate": sell_rate, "sell_min_fee": sell_min,
           "slippage": cfg.get("fees", {}).get("slippage", 0.0),
           "max_pos_pct": max_pos_pct, "pos_value": pos_value,
           "stop_dist": stop_dist, "stop_price": stop_price,
           "stop_ref_raw": sr, "atr_stop_raw": atr_stop,
           "cluster_existing_risk": cluster_risk,
           "cluster_budget_pct": risk_cfg.get("sector_risk_budget_pct", 4) / 100,
           "cluster_label": cluster_label,
           "data_ok": data_ok, "data_missing_reason": data_reason}
    # 谨慎态参数由模块A状态机给出，此处只消费
    if env and env.get("state") == "cautious":
        ctx["env_buy_coef"] = env.get("buy_coef")
        ctx["env_total_cap"] = env.get("total_cap")
    return ctx


def suggest_buy_qty_from_db(db_path, aid, symbol, px, cfg):
    """从DB读取状态，计算建议买入数量（供app.py调用）。
    2026-10-18起：app的订单校验必须用此函数的结果，确保与实际下单数量一致。
    2026-10-26（模块A+B）：风险定仓 v2 + 环境态消费——本函数面向“当下
    新单”的指引：环境禁开/降级 → 0（不给新开仓指引）；谨慎态套 A 的
    系数/上限；pipeline 执行端另按信号日期分流（<2026-10-26 的信号仍走
    旧 position_sizer，历史不可改写）。"""
    import sqlite3
    from engine import regime as _regime
    env = _regime.load_market_env(map_path=cfg.get("env_map_path"))
    if not _regime.allows_new_position(env):
        return 0
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT nav, cash, position_value FROM daily_report "
            "WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1",
            (aid,)).fetchone()
        if not row or not row[0] or row[0] <= 0:
            return 0
        nav, cash, pos_val = row[0], row[1] or 0, row[2] or 0
        risk_cfg = cfg.get("risk", {})
        atr_mult = risk_cfg.get("atr_stop_mult", 2.0)
        today_s = __import__("datetime").date.today().isoformat()

        def _bars_of(sym):
            return con.execute(
                "SELECT date, high, low, close FROM market_data WHERE symbol=?"
                " ORDER BY date", (sym,)).fetchall()

        def _atr_of(sym):
            bars = _bars_of(sym)
            if len(bars) < 15:
                return None
            from engine.signals import atr as _atr
            vals = _atr([b[1] for b in bars], [b[2] for b in bars],
                        [b[3] for b in bars], 14)
            vals = [v for v in vals if v is not None]
            return vals[-1] if vals else None

        def _closes_of(sym):
            return {b[0]: b[3] for b in _bars_of(sym)}

        cand_bars = _bars_of(symbol)
        data_ok, data_reason = True, ""
        if len(cand_bars) < 20:
            data_ok = False
            data_reason = "%s bars 仅 %d 根，不足算 ATR/相关性" % (
                symbol, len(cand_bars))
        if aid in ("sim_a", "sim_hk") and \
                symbol not in risk_gate.load_symbol_meta():
            data_ok = False
            data_reason = "workbook_map 无 %s 的记录（母板真源缺失）" % symbol
        atr_v = _atr_of(symbol)
        sr = risk_gate.stop_ref_of(symbol)
        atr_stop = (px - atr_mult * atr_v) if atr_v else None
        cand_stops = []
        if sr is not None and sr < px:
            cand_stops.append(sr)
        if atr_stop is not None:
            cand_stops.append(atr_stop)
        stop_price = min(cand_stops) if cand_stops else None
        stop_dist = (px - stop_price) if stop_price is not None else None
        target_sector = risk_gate.sector_key_of(symbol)
        cand_cp = _closes_of(symbol)
        cluster_risk = 0.0
        members = []
        for sym2, q2, _avg, last2 in con.execute(
                "SELECT symbol, qty, avg_cost, last_price FROM positions"
                " WHERE account=? AND qty>0", (aid,)).fetchall():
            same_sector = bool(target_sector) and \
                risk_gate.sector_key_of(sym2) == target_sector
            corr = _corr_asof(cand_cp, _closes_of(sym2), today_s)
            if not (same_sector or (corr is not None and corr > 0.8)):
                continue
            sr2 = risk_gate.stop_ref_of(sym2)
            if sr2 is not None and last2 is not None:
                dist2 = last2 - sr2
            else:
                a2 = _atr_of(sym2)
                dist2 = atr_mult * a2 if a2 else 0.0
            cluster_risk += q2 * max(dist2, 0.0)
            members.append(sym2)
        cluster_label = ("板块 %s" % target_sector) if target_sector else "相关性簇"
        if members:
            cluster_label += "（%s）" % "/".join(sorted(members))
        buy_rate, buy_min = _fee_for(aid, "BUY", cfg)
        sell_rate, sell_min = _fee_for(aid, "SELL", cfg)
        max_pos_pct = (risk_cfg.get("max_stock_position_pct", 70) / 100
                       if aid in ("sim_a", "sim_hk")
                       else risk_cfg.get("max_crypto_position_pct", 100) / 100)
        ctx = {"nav": nav, "cash": cash, "entry_px": px,
               "lot": float(cfg.get("lots", {}).get(symbol, 1)),
               "risk_pct": risk_cfg.get("single_trade_risk_pct", 2) / 100,
               "buy_fee_rate": buy_rate, "buy_min_fee": buy_min,
               "sell_fee_rate": sell_rate, "sell_min_fee": sell_min,
               "slippage": cfg.get("fees", {}).get("slippage", 0.0),
               "max_pos_pct": max_pos_pct, "pos_value": pos_val,
               "stop_dist": stop_dist, "stop_price": stop_price,
               "stop_ref_raw": sr, "atr_stop_raw": atr_stop,
               "cluster_existing_risk": cluster_risk,
               "cluster_budget_pct": risk_cfg.get("sector_risk_budget_pct", 4) / 100,
               "cluster_label": cluster_label,
               "data_ok": data_ok, "data_missing_reason": data_reason}
        if env.get("state") == "cautious":
            ctx["env_buy_coef"] = env.get("buy_coef")
            ctx["env_total_cap"] = env.get("total_cap")
        qty, _reason = position_sizer_v2(ctx)
        return qty
    finally:
        con.close()


def run(db_path=DB, cfg=None):
    """日线管线入口（进程级串行）。

    并发安全：两个 run_daily / apply_commands / app 重算同时执行时，
    若不串行，后启动者的 existing_fills 快照会漏掉先启动者未提交的新成交，
    导致 fills 重复插入。fcntl 排他锁按库文件隔离（db_path + ".run.lock"），
    进程崩溃时锁随 fd 关闭自动释放，不留僵尸锁。
    同一进程内多线程调 run() 不在此锁保护范围（生产路径均为多进程）。
    """
    lock_path = db_path + ".run.lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return _run_locked(db_path, cfg)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _run_locked(db_path=DB, cfg=None):
    if cfg is None:
        import yaml
        cfg = yaml.safe_load(open(os.path.join(BASE, "config", "strategy.yaml"), encoding="utf-8"))
    # 启动自检（P1-6）：universe（可交易）与 display_only（仅展示）必须互斥，
    # 同一标的同时出现在两边 = 身份矛盾（如 600150 曾双栖），宁可停摆不带病跑批。
    _disp = {str(x).strip() for x in (cfg.get("display_only") or [])}
    _overlap = set()
    for _acct in cfg.get("accounts", []):
        _overlap |= {str(x).strip() for x in
                     cfg.get("universe", {}).get(_acct["id"], [])} & _disp
    assert not _overlap, ("strategy.yaml 配置矛盾：universe 与 display_only "
                          "重叠 %s" % sorted(_overlap))
    con = sqlite3.connect(db_path)
    # signals/positions/daily_report 全量重算；fills 追加制：历史成交是事实，
    # 控制状态（暂停/手动）只决定新信号是否成交，不得删除历史成交。
    # 换仓额度券台账（补齐①）：每轮按成交事实重建（重算产物）；
    # fills.rotation_voucher_id 为用券买入的定格引用（旧库幂等补列，
    # 不在发布门禁指纹列集内，不影响历史行哈希）。
    con.execute("""CREATE TABLE IF NOT EXISTS rotation_vouchers(
        voucher_id TEXT PRIMARY KEY, source_fill_id INTEGER,
        account TEXT, symbol TEXT, signal_date TEXT, exec_date TEXT,
        qty REAL, price REAL, fee REAL, amount REAL, status TEXT,
        used_fill_id INTEGER, used_date TEXT, used_symbol TEXT,
        created_at TEXT)""")
    if "rotation_voucher_id" not in {
            r[1] for r in con.execute("PRAGMA table_info(fills)")}:
        con.execute(
            "ALTER TABLE fills ADD COLUMN rotation_voucher_id INTEGER")
    for t in ("signals", "positions", "daily_report", "rotation_vouchers"):
        con.execute("DELETE FROM %s" % t)
    existing_fills = {}
    for r in con.execute(
            "SELECT id,date,account,symbol,side,qty,price,fee,signal_date,"
            "rotation_voucher_id FROM fills ORDER BY date,id"):
        existing_fills[(r[1], r[2], r[3], r[4])] = {
            "id": r[0], "qty": r[5], "price": r[6], "fee": r[7],
            "signal_date": r[8], "rotation_voucher_id": r[9]}
    # 网页版控制：paused 只记信号不成交；manual 需 approvals 表批准才成交
    row = con.execute("SELECT status, mode FROM control WHERE id=1").fetchone()
    status, mode = row if row else ("running", "auto")
    # fail-safe：control 被直接写坏（非法值）时不得 fail-open 当 running/auto；
    # 未知 status 一律视为 paused（不产生新成交），未知 mode 一律视为 manual（需审批）。
    if status not in ("running", "paused"):
        print("WARN control.status 非法值 %r，已 fail-safe 视为 paused" % (status,))
        status = "paused"
    if mode not in ("auto", "manual"):
        print("WARN control.mode 非法值 %r，已 fail-safe 视为 manual" % (mode,))
        mode = "manual"

    # 暂停期信号黑名单：暂停期间生成的信号永不自动补成交（恢复后只执行新信号）
    # 该表跨轮持久，不随 signals 重算删除
    con.execute("""CREATE TABLE IF NOT EXISTS pause_skipped_signals(
        date TEXT, account TEXT, symbol TEXT, side TEXT,
        PRIMARY KEY(date, account, symbol, side))""")
    # 风险禁入拦截记录（P0-1）：跨轮持久，不随 signals 重算删除；
    # date=执行日，signal_date=信号日，reason 含判定价与 stop_ref。
    con.execute("""CREATE TABLE IF NOT EXISTS blocked_signals(
        date TEXT, signal_date TEXT, account TEXT, symbol TEXT, side TEXT,
        signal_price REAL, exec_price REAL, stop_ref REAL, reason TEXT,
        created_at TEXT,
        PRIMARY KEY(date, account, symbol, side))""")
    pause_blacklist = set(
        r[:4] for r in con.execute(
            "SELECT date,account,symbol,side FROM pause_skipped_signals"))

    buy_frac = cfg.get("sizing", {}).get("buy_frac", 0.5)
    slippage = cfg["fees"].get("slippage", 0.0)
    lots = cfg.get("lots", {})
    boards = cfg.get("boards", {})
    ma_long = cfg["signals"]["ma_windows"][-1]

    # 市场环境态（模块A）：母板字段唯一真源（engine.regime 状态机），
    # 整轮跑批加载一次。cfg["env_map_path"] 仅供测试注入映射变体。
    from engine import regime as _regime
    env = _regime.load_market_env(map_path=cfg.get("env_map_path"), con=con)
    print("市场环境态：%s（%s）" % (env.get("label"),
                                   env.get("degraded_reason") or env.get("reason") or ""))
    # 换仓额度券（补齐①，草案口径，engine.rotation）：仅 blocked_new
    # 态消费；主动信号卖出生成、净回收计额、一对一、5 交易日有效、
    # degraded 禁用、防再生。回放内由 RotationBook 维护，轮末落
    # rotation_vouchers 表（重算产物），用券事实在 fills 定格。
    book = RotationBook()
    _acct_last = {}  # aid -> 本轮回放最后日期（券过期结算用）

    asof = None
    for acct in cfg["accounts"]:
        aid, ccy = acct["id"], acct["ccy"]
        symbols = cfg["universe"].get(aid, [])
        bars = {}
        for s in symbols:
            rows = con.execute(
                "SELECT date,open,high,low,close,volume FROM market_data"
                " WHERE symbol=? ORDER BY date", (s,)).fetchall()
            if rows:
                bars[s] = rows
        if not bars:
            continue  # 无数据（如休市中的 A 股）：空盘跳过
        # 管道健壮性：同一账户内各标的日历应一致；若某标的最新 bar 早于账户内
        # 最大日期，说明缺行情（接口失败/漏入库），WARN 留痕；缺失日的持仓估值
        # 沿用该标的上个收盘价（不编造新 bar、不把持仓归零）。
        last_by_sym = {s: blist[-1][0] for s, blist in bars.items()}
        max_last = max(last_by_sym.values())
        for s in sorted(last_by_sym):
            if last_by_sym[s] < max_last:
                print("WARN %s 最新行情 %s，早于账户 %s 内最新日期 %s；缺失日估值沿用上个收盘价"
                      % (s, last_by_sym[s], aid, max_last))
        sim = SimAccount(id=aid, ccy=ccy, cash=float(acct["principal"]),
                         t1=bool(cfg["rules"].get("a_t1")) and aid == "sim_a")
        # 预计算信号
        sig_by_date = {}
        closes_by_sym = {}
        # 策略选择：分账户（2026-10-16 用户选A），优先读 signals.per_account[aid]，回退到全局 strategy
        _sig_cfg = cfg.get("signals", {})
        strategy = _sig_cfg.get("per_account", {}).get(aid) or _sig_cfg.get("strategy", "ma20")
        from engine.signals import atr as calc_atr
        from engine.indicators import adx as calc_adx, market_regime
        atr_by_sym = {}  # symbol -> {date: atr_value}
        adx_by_sym = {}  # symbol -> {date: adx_value}（市场状态识别）
        atr_mult = cfg.get("risk", {}).get("atr_stop_mult", 2.0)
        # ADX 震荡市保护开关（config signals.adx_filter）
        adx_filter = _sig_cfg.get("adx_filter", True)
        for s, blist in bars.items():
            dates = [r[0] for r in blist]
            closes = [r[4] for r in blist]
            closes_by_sym[s] = dict(zip(dates, closes))
            # ATR 预计算（所有策略的策略止损用）
            highs = [r[2] for r in blist]
            lows = [r[3] for r in blist]
            atr_vals = calc_atr(highs, lows, closes, 14)
            atr_by_sym[s] = {d: v for d, v in zip(dates, atr_vals) if v is not None}
            # ADX 预计算（市场状态识别：ADX<20 震荡市暂停趋势策略）
            adx_vals = calc_adx(highs, lows, closes, 14)
            adx_by_sym[s] = {d: v for d, v in zip(dates, adx_vals) if v is not None}
            if strategy == "buyback":
                gen = gen_buyback_signals(dates, closes, highs)
            elif strategy == "disabled":
                gen = []  # 2026-10-18起停掉：数据不支持正期望，不再生成新信号
            else:
                gen = gen_signals(dates, closes, ma_long)
            for d, side, px in gen:
                # ADX 震荡市保护：ADX<20 时暂停趋势策略(MA)新信号，buyback不受影响
                # 2026-10-17 中期第一项：市场状态识别
                if adx_filter and strategy != "buyback" and side == "BUY":
                    adx_v = adx_by_sym.get(s, {}).get(d)
                    if adx_v is not None and market_regime(adx_v) == "range":
                        continue  # 震荡市跳过趋势策略买入信号
                sig_by_date.setdefault(d, []).append((s, side, px))
        prev_close = {}
        opens_by_sym = {}
        has_next_bar = {}  # (symbol, date) -> bool，是否有 T+1 的 bar
        for s, blist in bars.items():
            opens_by_sym[s] = {}
            for i, r in enumerate(blist):
                prev_close[(s, r[0])] = blist[i - 1][4] if i > 0 else None
                opens_by_sym[s][r[0]] = r[1]  # open
                has_next_bar[(s, r[0])] = (i + 1 < len(blist))

        dates = sorted(set(d for bl in bars.values() for d, *_ in bl))
        book.set_calendar(aid, dates)
        _acct_last[aid] = dates[-1]
        asof = dates[-1] if asof is None or dates[-1] > asof else asof
        prev_nav = float(acct["principal"])
        last_px = {}  # 标的 -> 上个已知收盘价；缺 bar 的日期沿用（不归零、不编造）
        pending = []  # T+1 待执行：[(sig_date, symbol, side, sig_px)]
        atr_stops = {}  # symbol -> stop_price（策略止损（ATR）价，持仓期间有效）
        for d in dates:
            seen = set()
            # ===== 策略止损（ATR）检查：持仓标的若跌破 ATR 止损，强制平仓 =====
            # 在 Phase 1 之前检查，确保止损优先于普通信号执行
            for s in list(sim.positions.keys()):
                if s not in atr_stops or sim.position_qty(s, d) <= 0:
                    continue
                # 用当日最低价判断是否触发止损（最保守）
                low = None
                for blist in bars.get(s, []):
                    if blist[0] == d:
                        low = blist[3]
                        break
                if low is not None and low < atr_stops[s]:
                    # 触发策略止损（ATR）：按当日开盘价（T+1 已过，直接执行）或收盘价成交
                    # 为简化，按收盘价+滑点成交（止损单为市价单）
                    close_px = closes_by_sym[s][d]
                    px = close_px * (1 - slippage)
                    fee_rate, min_fee = _fee_for(aid, "SELL", cfg)
                    try:
                        qty = sim.position_qty(s, d)
                        if qty > 0:
                            fee, pnl = sim.sell(s, qty, px, d, fee_rate)
                            con.execute(
                                "INSERT INTO fills(date,account,symbol,side,qty,price,fee,pnl,signal_date,strategy_version)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (d, aid, s, "SELL", qty, round(px, 6), round(fee, 4),
                                 round(pnl, 2), d, cfg.get("meta", {}).get("version", "unknown")))
                            # 被动强制平仓（ATR 策略止损）：不生成额度
                            # 券（草案第 8 条）；仅同步防再生来源队列。
                            book.note_sell(aid, s, qty, d, sim.t1)
                            del atr_stops[s]
                    except ValueError:
                        pass
            # ===== Phase 1：T+1 开盘执行 pending 信号 =====
            # 信号日 D 的信号，在 D+1 开盘价成交（贴近人工实盘：收盘看信号，次日开盘下单）
            still_pending = []
            for (sig_d, s, side, sig_px) in pending:
                _rot_voucher = None  # 本笔若用券成交，成交后据此定格标注
                # 该标的今日无 bar（如停牌），继续等待
                if d not in opens_by_sym.get(s, {}):
                    still_pending.append((sig_d, s, side, sig_px))
                    continue
                open_px = opens_by_sym[s][d]
                px = open_px * (1 + slippage) if side == "BUY" else open_px * (1 - slippage)
                # A股涨跌停保护（按执行日开盘价判断）
                if aid == "sim_a":
                    pc = prev_close.get((s, d))
                    lim = cfg["rules"]["limit_pct"].get(boards.get(s, "main"), 0.10)
                    hit = check_limit(open_px, pc, lim) if pc else None
                    if (side == "BUY" and hit == "up") or (side == "SELL" and hit == "down"):
                        continue
                fee_rate, min_fee = _fee_for(aid, side, cfg)
                # existing_fills 键为执行日期；兼容旧数据（信号日=执行日）
                key_new = (d, aid, s, side)
                key_old = (sig_d, aid, s, side)
                seen.add(key_new)
                if key_new in existing_fills:
                    _ef = existing_fills[key_new]
                    qty, price, fee = _ef["qty"], _ef["price"], _ef["fee"]
                    _replay_fill(sim, s, side, qty, price, fee, d)
                    if side == "BUY":
                        _vid = _ef["rotation_voucher_id"]
                        book.note_buy(aid, s, qty, d,
                                      is_voucher=_vid is not None)
                        if _vid is not None and not book.attach_replayed_use(
                                "RV-%d" % _vid, _ef["id"], d, s):
                            print("WARN 券 RV-%s 在重放中未找到来源"
                                  "（fills #%s 的用券引用无对应券）"
                                  % (_vid, _ef["id"]))
                    else:
                        # 历史信号卖出重放：与新成交同口径决定是否
                        # 生成额度券（信号日生效日 + 防再生两道门）
                        _touched = book.note_sell(aid, s, qty, d, sim.t1)
                        if _ef["signal_date"] and \
                                _ef["signal_date"] >= SIZING_EFFECTIVE_DATE \
                                and not _touched:
                            book.issue(aid, _ef["id"], s,
                                       _ef["signal_date"], d,
                                       qty, price, fee)
                    continue
                # 旧口径成交由底部"历史成交补重放"统一处理（按执行日期），此处不重复处理
                # 门禁（信号日维度）
                ap = con.execute(
                    "SELECT decision FROM approvals WHERE date=? AND account=?"
                    " AND symbol=? AND side=?", (sig_d, aid, s, side)).fetchone()
                ap = ap[0] if ap else None
                if ap == "skipped":
                    continue
                # 风险禁入闸（P0-1，用户拍板“禁入优先”）：信号照常生成，
                # 但 BUY 的信号价/执行价任一跌破母板止损参考 stop_ref 时
                # 执行层一律不成交（buyback 回落买入不例外，人工批准也不
                # 放行）；拦截写入 blocked_signals 并在日报列明原因。
                # 历史成交在上方 existing_fills 分支已重放，不受本闸追溯。
                if side == "BUY":
                    _blk, _breason, _sr = risk_gate.check_buy(s, px, sig_px)
                    if _blk:
                        con.execute(
                            "INSERT OR IGNORE INTO blocked_signals"
                            "(date,signal_date,account,symbol,side,"
                            "signal_price,exec_price,stop_ref,reason,created_at)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (d, sig_d, aid, s, side, sig_px, round(px, 6), _sr,
                             _breason,
                             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        continue
                    # 市场环境闸（模块A，与禁入闸同层）：仅对信号日
                    # ≥2026-10-26 的新信号生效（清单C 生效日分流；此前
                    # 信号走旧路径回放，逐笔与改造前一致）。attack/
                    # cautious 不拦（谨慎参数由 sizing 消费 env）；
                    # blocked_new 时 BUY 不执行——换仓豁免（额度券）
                    # 除外：本账户有可用额度券时放行本闸，券面额在
                    # sizing 作金额上限、成交后标已用，超出另行留痕；
                    # degraded 时额度券禁用（草案第 2 条）。SELL 永不
                    # 受本闸；历史成交已在上方重放，不受追溯。
                    _env_blk, _env_reason = (False, "")
                    if sig_d >= SIZING_EFFECTIVE_DATE:
                        _env_blk, _env_reason = risk_gate.check_env_gate(env)
                    if _env_blk:
                        if env.get("state") == _regime.DEGRADED:
                            con.execute(
                                "INSERT OR IGNORE INTO blocked_signals"
                                "(date,signal_date,account,symbol,side,"
                                "signal_price,exec_price,stop_ref,reason,created_at)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (d, sig_d, aid, s, side, sig_px, round(px, 6), _sr,
                                 _env_reason + "；安全降级态禁用换仓额度券",
                                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                            continue
                        _rot_voucher, _rot_why = book.pick(aid, d)
                        if _rot_voucher is None:
                            con.execute(
                                "INSERT OR IGNORE INTO blocked_signals"
                                "(date,signal_date,account,symbol,side,"
                                "signal_price,exec_price,stop_ref,reason,created_at)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (d, sig_d, aid, s, side, sig_px, round(px, 6), _sr,
                                 _env_reason + "；无可用换仓额度券（%s）" % _rot_why,
                                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                            continue
                if key_old in pause_blacklist:
                    continue  # 暂停期积压信号：恢复后不补成交
                if status == "paused":
                    con.execute(
                        "INSERT OR IGNORE INTO pause_skipped_signals(date,account,symbol,side)"
                        " VALUES(?,?,?,?)", (sig_d, aid, s, side))
                    pause_blacklist.add(key_old)
                    continue
                if mode == "manual" and ap != "approved":
                    # 手动模式未批准：保留 pending，等待批准后执行
                    still_pending.append((sig_d, s, side, sig_px))
                    continue
                try:
                    if side == "BUY":
                        if sig_d >= SIZING_EFFECTIVE_DATE:
                            # 风险定仓 v2（模块B）：按信号日期分流，
                            # 此前信号仍走旧 position_sizer（历史不可改写）
                            _v2ctx = _sizer_v2_ctx(
                                sim, aid, s, px, sig_d, cfg, atr_by_sym,
                                closes_by_sym, last_px, env=env)
                            if _rot_voucher is not None:
                                _v2ctx["amount_cap"] = _rot_voucher["amount"]
                            qty, block_reason = position_sizer_v2(_v2ctx)
                            if qty <= 0:
                                if block_reason:
                                    con.execute(
                                        "INSERT OR IGNORE INTO blocked_signals"
                                        "(date,signal_date,account,symbol,side,"
                                        "signal_price,exec_price,stop_ref,reason,created_at)"
                                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                                        (d, sig_d, aid, s, side, sig_px,
                                         round(px, 6), risk_gate.stop_ref_of(s),
                                         "仓位拦截：" + block_reason,
                                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                                continue
                            if _rot_voucher is not None:
                                # 额度券截断了计划数量：超出部分留痕
                                _planned, _pr = position_sizer_v2(
                                    {**_v2ctx, "amount_cap": None})
                                if _planned > qty:
                                    con.execute(
                                        "INSERT OR IGNORE INTO blocked_signals"
                                        "(date,signal_date,account,symbol,side,"
                                        "signal_price,exec_price,stop_ref,reason,created_at)"
                                        " VALUES(?,?,?,?,?,?,?,?,?,?)",
                                        (d, sig_d, aid, s, side, sig_px,
                                         round(px, 6), risk_gate.stop_ref_of(s),
                                         "环境闸·禁止新开仓：换仓额度券 %s 面额"
                                         "不足，超出部分已拦截（计划 %s 股，券内"
                                         "成交 %s 股，券面额 %.2f 元）"
                                         % (_rot_voucher["voucher_id"],
                                            _planned, qty,
                                            _rot_voucher["amount"]),
                                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        else:
                            qty, block_reason = position_sizer(
                                sim, aid, s, px, cfg, strategy, buy_frac, lots)
                            if qty <= 0:
                                continue
                            # legacy 路径到此为止：不套环境态/换仓额度
                            # （清单C：此前信号按旧路径逐笔回放）
                        fee = sim.buy(s, qty, px, d, fee_rate, min_fee)
                        pnl = 0.0
                        # 设置策略止损（ATR）：entry - atr_mult * ATR(信号日)
                        atr_v = atr_by_sym.get(s, {}).get(sig_d)
                        if atr_v:
                            atr_stops[s] = px - atr_mult * atr_v
                    else:
                        qty = sim.position_qty(s, d)
                        if qty <= 0:
                            continue
                        fee, pnl = sim.sell(s, qty, px, d, fee_rate)
                except ValueError:
                    continue
                _cur = con.execute(
                    "INSERT INTO fills(date,account,symbol,side,qty,price,fee,pnl,signal_date,strategy_version,rotation_voucher_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (d, aid, s, side, qty, round(px, 6), round(fee, 4), round(pnl, 2), sig_d,
                     cfg.get("meta", {}).get("version", "unknown"),
                     _rot_voucher["source_fill_id"]
                     if (side == "BUY" and _rot_voucher is not None)
                     else None))
                _fill_id = _cur.lastrowid
                if side == "BUY":
                    book.note_buy(aid, s, qty, d,
                                  is_voucher=_rot_voucher is not None)
                    if _rot_voucher is not None:
                        book.mark_used(_rot_voucher, _fill_id, d, s)
                else:
                    # 主动信号卖出成交（Phase 1 信号执行路径）→ 生成
                    # 额度券；卖出触及券资助仓位时不生成（防再生）。
                    _touched = book.note_sell(aid, s, qty, d, sim.t1)
                    if sig_d >= SIZING_EFFECTIVE_DATE and not _touched:
                        book.issue(aid, _fill_id, s, sig_d, d,
                                   qty, round(px, 6), round(fee, 4))
            pending = still_pending

            # ===== Phase 2：从今日收盘生成新信号，加入 pending =====
            for s, side, sig_px in sorted(sig_by_date.get(d, [])):
                # 验证期闸（仅 sim_a）
                if aid == "sim_a" and d < VAL_START.get(aid, "2026-10-08"):
                    continue
                # 动作类型 + 止损价 + 策略
                act = "开仓" if side == "BUY" else "信号平仓"
                stop_px = None
                if side == "BUY":
                    # 策略止损（ATR）：entry - 2*ATR
                    atr_v = atr_by_sym.get(s, {}).get(d)
                    if atr_v:
                        stop_px = round(sig_px - 2 * atr_v, 2)
                # 策略中文名
                strat_cn = {"buyback": "接回策略", "ma20": "MA20趋势"}.get(strategy, strategy)
                # 信号过期：超过7天未执行的信号标记为已过期（2026-10-18起）
                sig_date = datetime.strptime(d, "%Y-%m-%d").date()
                today = datetime.now().date()
                days_old = (today - sig_date).days
                sig_status = "已过期" if days_old > 7 else "待执行"
                con.execute(
                    "INSERT INTO signals(date,account,symbol,side,price,action_type,status,stop_price,strategy)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (d, aid, s, side, sig_px, act, sig_status, stop_px, strat_cn))
                # 有 T+1 bar 才加入待执行；否则信号保留，待数据到达后执行
                if has_next_bar.get((s, d)):
                    pending.append((d, s, side, sig_px))
                # 无 T+1 bar（如最新日期）：信号已入库，pending 待下次跑批
            # 历史成交补重放：bar 缺失导致信号未重新生成时（如行情漏入库），
            # 该日期的历史成交仍必须重放，否则持仓会被静默丢弃。
            for (fd, faid, fs, fside), _ef in existing_fills.items():
                if faid != aid or fd != d or (fd, faid, fs, fside) in seen:
                    continue
                qty, price, fee = _ef["qty"], _ef["price"], _ef["fee"]
                _replay_fill(sim, fs, fside, qty, price, fee, fd)
                if fside == "BUY":
                    _vid = _ef["rotation_voucher_id"]
                    book.note_buy(aid, fs, qty, fd,
                                  is_voucher=_vid is not None)
                    if _vid is not None and not book.attach_replayed_use(
                            "RV-%d" % _vid, _ef["id"], fd, fs):
                        print("WARN 券 RV-%s 在补重放中未找到来源"
                              "（fills #%s）" % (_vid, _ef["id"]))
                else:
                    _touched = book.note_sell(aid, fs, qty, fd, sim.t1)
                    # 补重放的卖出无 pending 上下文：以 signals 表有无
                    # 对应信号行判类别（查无对应信号行 → 保守不生成）
                    _sd = _ef["signal_date"]
                    if _sd and _sd >= SIZING_EFFECTIVE_DATE \
                            and not _touched:
                        _has_sig = con.execute(
                            "SELECT 1 FROM signals WHERE date=? AND"
                            " account=? AND symbol=? AND side='SELL'",
                            (_sd, aid, fs)).fetchone()
                        if _has_sig:
                            book.issue(aid, _ef["id"], fs, _sd, fd,
                                       qty, price, fee)
            # 当日收盘净值（缺 bar 的标的沿用上个收盘价估值，已在上方 WARN 留痕）
            pos_val, cash = 0.0, sim.cash
            for s in bars:
                if d in closes_by_sym[s]:
                    last_px[s] = closes_by_sym[s][d]
                if s in last_px:
                    pos_val += sim.market_value(s, last_px[s])
            nav = cash + pos_val
            pnl_day = nav - prev_nav
            ret_total = (nav - acct["principal"]) / acct["principal"]
            con.execute(
                "INSERT INTO daily_report(date,account,symbol,nav,cash,position_value,pnl_day,ret_total)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (d, aid, "TOTAL", round(nav, 2), round(cash, 2),
                 round(pos_val, 2), round(pnl_day, 2), round(ret_total, 6)))
            prev_nav = nav
        # 期末持仓快照（只记有仓位的）
        for s, blist in bars.items():
            q = sim.position_qty(s)
            if q <= 0:
                continue
            last = blist[-1][4]
            lots_s = sim.positions.get(s, [])
            avg = sum(l.qty * l.cost for l in lots_s) / q if q > 0 else 0.0
            con.execute(
                "INSERT INTO positions(account,symbol,qty,avg_cost,last_price,market_value)"
                " VALUES(?,?,?,?,?,?)",
                (aid, s, q, round(avg, 6), last, round(q * last, 2)))
    # 换仓额度券落表（重算产物：轮始已清空，此处按回放事实全量写入，
    # 供日报逐笔列示与事后审计；用券事实同时在 fills 定格引用）
    book.settle(_acct_last)
    _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for v in book.vouchers:
        con.execute(
            "INSERT INTO rotation_vouchers(voucher_id,source_fill_id,"
            "account,symbol,signal_date,exec_date,qty,price,fee,amount,"
            "status,used_fill_id,used_date,used_symbol,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (v["voucher_id"], v["source_fill_id"], v["account"],
             v["symbol"], v["signal_date"], v["exec_date"], v["qty"],
             v["price"], v["fee"], v["amount"], v["status"],
             v["used_fill_id"], v["used_date"], v["used_symbol"], _now))
    con.commit()
    con.close()
    return asof


def write_report(db_path=DB, cfg=None, asof=None):
    """生成 Markdown 日报到 reports/，返回文件路径。"""
    if cfg is None:
        import yaml
        cfg = yaml.safe_load(open(os.path.join(BASE, "config", "strategy.yaml"), encoding="utf-8"))
    from engine import regime as _regime_wr
    _env_wr = _regime_wr.load_market_env(
        map_path=cfg.get("env_map_path"), db_path=db_path)
    _env_label = _env_wr.get("label", "?")
    _auto_wr = _env_wr.get("auto") or {}
    if _auto_wr.get("known"):
        _recon_wr = (
            "自动计算 上证 %.2f vs MA20 %.2f（指数日 %s）→ %s；母板B31=%s；"
            "对账%s；环境态=%s" % (
                _auto_wr["close"], _auto_wr["ma20"], _auto_wr.get("last_date"),
                "站上20日线" if _auto_wr["on"] else "未站上20日线",
                "是" if _env_wr.get("b31") else "否",
                {True: "一致", False: "不一致"}.get(
                    _env_wr.get("reconciled"), "不可用"),
                _env_label))
    else:
        _recon_wr = "自动计算不可用（指数数据缺失）；环境态=%s" % _env_label
    if _env_wr.get("state") == "degraded":
        _recon_wr += "；降级原因：%s" % (_env_wr.get("degraded_reason") or "")
    # 测试时可覆盖日报输出目录，避免测试跑批覆盖生产日报
    report_dir = os.environ.get("PAPER_SIM_REPORTS", REPORT_DIR)
    os.makedirs(report_dir, exist_ok=True)
    con = sqlite3.connect(db_path)
    if asof is None:
        r = con.execute("SELECT MAX(date) FROM daily_report").fetchone()
        asof = r[0] if r and r[0] else "nodata"
    L = ["# 日报 %s" % asof, ""]
    for acct in cfg["accounts"]:
        aid = acct["id"]
        row = con.execute(
            "SELECT nav,cash,position_value,pnl_day,ret_total FROM daily_report"
            " WHERE date=? AND account=? AND symbol='TOTAL'", (asof, aid)).fetchone()
        # 各市场日历不同（如加密 UTC 日K在跑批时未收盘）：用账户自身最新日期，
        # 避免“本期无数据”误报；滞后时在标题注明截至日期。
        sec_date = asof
        if not row:
            r2 = con.execute(
                "SELECT date,nav,cash,position_value,pnl_day,ret_total FROM daily_report"
                " WHERE account=? AND symbol='TOTAL' ORDER BY date DESC LIMIT 1",
                (aid,)).fetchone()
            if not r2:
                L += ["## %s（%s）— 本期无数据" % (aid, acct["ccy"]), ""]
                continue
            sec_date = r2[0]
            nav, cash, pv, pnl_day, ret = r2[1], r2[2], r2[3], r2[4], r2[5]
        else:
            nav, cash, pv, pnl_day, ret = row
        title = "## %s（%s）" % (aid, acct["ccy"])
        if sec_date != asof:
            title += " · 截至 %s" % sec_date
        L += [title,
              "- 初始资金 %s → 总资产 %s（累计 %+.2f%%）" % (
                  f"{acct['principal']:,.0f}", f"{nav:,.2f}", ret * 100),
              "- 现金 %s / 持仓市值 %s / 当日盈亏 %+.2f" % (
                  f"{cash:,.2f}", f"{pv:,.2f}", pnl_day),
              "", "持仓："]
        unit = "枚" if aid == "sim_crypto" else "股"
        for s, q, avg, last, mv in con.execute(
                "SELECT symbol,qty,avg_cost,last_price,market_value FROM positions"
                " WHERE account=? AND qty>0", (aid,)):
            L.append("- %s：%s %s @均价 %s，现价 %s，市值 %s" % (s, f"{q:,.4f}".rstrip("0").rstrip("."),
                                                              unit, avg, last, f"{mv:,.2f}"))
        L += ["", "今日信号与成交："]
        n = 0
        for d, s, side, px in con.execute(
                "SELECT date,symbol,side,price FROM signals WHERE date=? AND account=?"
                " ORDER BY symbol", (asof, aid)):
            f = con.execute(
                "SELECT qty,price,fee,pnl FROM fills WHERE date=? AND account=?"
                " AND symbol=? AND side=?", (d, aid, s, side)).fetchone()
            if f:
                L.append("- %s %s %s %s @%s（费 %s，盈亏 %+.2f）" % (s, side, f[0], unit, f[1], f[2], f[3]))
            else:
                L.append("- %s %s 信号 @%s（未成交）" % (s, side, px))
            n += 1
        if n == 0:
            L.append("- 无")
        L.append("")
    # 风险禁入拦截（P0-1）：当日被禁入闸拦截的 BUY，逐条列明原因
    try:
        blk = con.execute(
            "SELECT account,symbol,signal_date,signal_price,stop_ref,reason"
            " FROM blocked_signals WHERE date=? ORDER BY account,symbol",
            (asof,)).fetchall()
    except Exception:
        blk = []
    if blk:
        L += ["## 风险禁入拦截（禁入优先）", ""]
        for a_, s_, sd_, sp_, sr_, rs_ in blk:
            L.append("- %s %s BUY（信号日 %s，信号价 %s，母板止损 %s）：%s"
                     % (a_, s_, sd_, sp_, sr_, rs_))
        L.append("")
    # 换仓额度券（补齐①，草案第 9 条）：全部券逐笔列示（生成来源/
    # 面额/状态/使用去向），不只总述；表为本轮重算产物。
    try:
        vchs = con.execute(
            "SELECT voucher_id,account,symbol,signal_date,exec_date,"
            "amount,status,used_symbol,used_date FROM rotation_vouchers"
            " ORDER BY exec_date, source_fill_id").fetchall()
    except Exception:
        vchs = []
    if vchs:
        _st = {"active": "有效", "used": "已用", "expired": "已过期"}
        L += ["## 换仓额度券（禁开态豁免·一对一）", ""]
        for vid, va, vs, vsd, ved, vamt, vst, vus, vud in vchs:
            line = "- %s %s：来源卖出 %s（信号日 %s、成交日 %s），面额 %.2f 元，状态 %s" \
                   % (vid, va, vs, vsd, ved, vamt, _st.get(vst, vst))
            if vst == "used":
                line += "，用于 %s 买入（%s）" % (vus, vud)
            L.append(line)
        L.append("")
    L += ["---", "口径声明：",
          "- 数据：%s；%s；%s" % (cfg["data_sources"]["a_hk"],
                                 cfg["data_sources"]["crypto"], cfg["data_sources"]["fx"]),
          "- 执行：T+1 开盘执行，滑点 %s%%；BUY 仓位：信号日≥2026-10-26 按风险定仓（风险预算=净值×2%%，每股风险=止损距离+往返成本；总仓位/现金（含费）/风险簇预算4%%/换仓额度封顶），此前信号沿用旧口径（现金 %s%%）；SELL 全平；费用见配置" % (
              cfg["fees"]["slippage"] * 100, cfg.get("sizing", {}).get("buy_frac", 0.5) * 100),
          "- 环境闸（模块A）：禁止新开仓时 BUY 不执行（换仓额度券内除外：主动卖出生成、扣费后净回收计额、一券一买、5 交易日有效、降级态禁用，逐笔见上额度券段），拦截逐条见上（blocked_signals）",
          "- 环境对账（模块A·每日对账）：%s" % _recon_wr,
          "- 规则版本：%s（DRAFT）" % cfg["meta"]["version"]]
    path = os.path.join(report_dir, "日报-%s.md" % asof)
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    con.close()
    return path


if __name__ == "__main__":
    asof = run()
    print(write_report(asof=asof))
