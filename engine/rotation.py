#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
换仓额度券台账（四项补齐①，2026-10-27；《换仓豁免风险边界（草案）》落地）。

替代旧口径「账户历史卖出毛额无期限资金池」（engine.pipeline 旧
rotation_quota）：旧池无配对、无时效、按毛额、被动止损也解锁、
degraded 态同样豁免、无账本标注。额度券口径逐条对齐草案：

- 生成（草案第 8 条）：只有**主动信号卖出**成交生成券；ATR 策略
  止损等被动强制平仓不生成。类别由成交驱动路径机械判定（见
  pipeline 的调用点）：信号执行路径 = 主动；ATR 跌破平仓路径 = 被动。
  信号日缺失、或（补重放时）signals 表查无对应信号行 → 保守不生成。
- 面额（草案第 5 条）：来源卖出成交的**扣费后净回收额**
  = qty × 成交价 − 卖出费用（成交价已含滑点，即账本实际回收口径）。
- 配对与时效（草案第 3、4 条）：一券绑定一笔来源卖出成交
  （voucher_id = "RV-<来源 fills.id>"），至多资助一笔买入、用完
  作废、不拼券；自卖出成交日起 5 个交易日内有效，逾期自动失效。
- 频率（草案第 7 条）：同一账户相邻两次用券的交易日间隔须 ≥5
  （即任意滚动 5 个交易日窗口内最多完成 1 对换仓）。
- 防再生（草案第 7 条）：用券买入的仓位，其后卖出**不生成新券**。
  判定靠本模块维护的买入来源队列（与 sim 持仓 Lot 同序 FIFO、
  同 T+1 口径扣减）：一笔卖出只要触及任一券资助 lot，整笔不生成。
- 启用态（草案第 2 条）：券只在环境闸 blocked_new 态被 pipeline
  消费；degraded 态禁用由 pipeline 在调用 pick 前拦截，本模块
  不感知环境态（只管券自身的可用性）。

本模块是纯逻辑（不读写的 DB/文件）：pipeline 在全量回放中维护
RotationBook 实例，轮末把全部券状态落 rotation_vouchers 表
（每轮按成交事实重建，与 positions/daily_report 同属重算产物，
保证审计表与成交事实严格一致、可复现）。
"""
from __future__ import annotations

import bisect

WINDOW_TRADING_DAYS = 5   # 券有效期（交易日，卖出成交日为第 0 日）
FREQ_MIN_GAP_TRADING_DAYS = 5  # 相邻两次用券的最小交易日间隔


def net_proceeds(qty, price, fee):
    """券面额 = 卖出净回收额（成交额 − 卖出费用）。"""
    return round(qty * price - fee, 4)


class RotationBook:
    """单轮回放内的额度券台账（按账户隔离，天然不跨账户/市场/币种）。"""

    def __init__(self):
        self._calendars = {}   # aid -> [date,...] 升序（该账户交易日）
        self.vouchers = []     # 全部券（dict），按生成顺序
        self._by_id = {}
        self._lots = {}        # (aid, symbol) -> [[qty_left, is_voucher, buy_date]]

    # ---------------- 交易日历 ----------------
    def set_calendar(self, aid, dates):
        self._calendars[aid] = sorted(set(dates))

    def tdays(self, aid, d_from, d_to):
        """两日期在该账户交易日历上的间隔（交易日数）。
        日期不在日历上时以 bisect 序位计（成交日必在日历上；
        asof 等边界日期可能不在）。d_to < d_from 时返回负数。"""
        cal = self._calendars.get(aid) or []
        i = bisect.bisect_left(cal, d_from)
        j = bisect.bisect_left(cal, d_to)
        return j - i

    # ---------------- 生成 ----------------
    def issue(self, aid, source_fill_id, symbol, signal_date, exec_date,
              qty, price, fee):
        """一笔主动信号卖出成交 → 生成一张额度券。返回券 dict。"""
        v = {
            "voucher_id": "RV-%d" % source_fill_id,
            "source_fill_id": source_fill_id,
            "account": aid,
            "symbol": symbol,
            "signal_date": signal_date,
            "exec_date": exec_date,
            "qty": qty,
            "price": price,
            "fee": fee,
            "amount": net_proceeds(qty, price, fee),
            "status": "active",
            "used_fill_id": None,
            "used_date": None,
            "used_symbol": None,
        }
        self.vouchers.append(v)
        self._by_id[v["voucher_id"]] = v
        return v

    def get(self, voucher_id):
        return self._by_id.get(voucher_id)

    # ---------------- 可用性 ----------------
    def _expired(self, v, on_date):
        return self.tdays(v["account"], v["exec_date"], on_date) \
            > WINDOW_TRADING_DAYS

    def pick(self, aid, on_date):
        """为 on_date 的一笔禁开态 BUY 选券。返回 (voucher, reason)：
        选到券时 reason=""；无可用券时 voucher=None、reason 说明
        （无券 / 全部已过期 / 频率上限）。只返回一张——不拼券。"""
        mine = [v for v in self.vouchers
                if v["account"] == aid and v["status"] == "active"]
        if not mine:
            return None, "本账户无有效额度券（未先主动卖出）"
        live = [v for v in mine if not self._expired(v, on_date)]
        if not live:
            return None, "额度券已全部过期（卖出成交后超过 %d 个交易日）" \
                % WINDOW_TRADING_DAYS
        # 频率上限：与本账户上一次用券日的交易日间隔须 ≥ FREQ_MIN_GAP
        used_dates = [v["used_date"] for v in self.vouchers
                      if v["account"] == aid and v["status"] == "used"
                      and v["used_date"]]
        if used_dates:
            last_used = max(used_dates)
            gap = self.tdays(aid, last_used, on_date)
            if gap < FREQ_MIN_GAP_TRADING_DAYS:
                return None, ("换仓频率上限：上次用券 %s 距今 %d 个交易日"
                              "（须 ≥%d）" % (last_used, gap,
                                             FREQ_MIN_GAP_TRADING_DAYS))
        live.sort(key=lambda v: (v["exec_date"], v["source_fill_id"]))
        return live[0], ""

    def mark_used(self, voucher, used_fill_id, used_date, used_symbol):
        voucher["status"] = "used"
        voucher["used_fill_id"] = used_fill_id
        voucher["used_date"] = used_date
        voucher["used_symbol"] = used_symbol

    def attach_replayed_use(self, voucher_id, used_fill_id, used_date,
                            used_symbol):
        """历史成交重放：fills 行已带 rotation_voucher_id 定格的用券
        事实 → 把对应券标为已用。找不到券返回 False（数据异常，
        调用方 WARN 留痕，不崩）。"""
        v = self._by_id.get(voucher_id)
        if v is None:
            return False
        self.mark_used(v, used_fill_id, used_date, used_symbol)
        return True

    # ---------------- 防再生：买入来源队列 ----------------
    def note_buy(self, aid, symbol, qty, buy_date, is_voucher):
        """记录一笔买入成交事实（与 sim 持仓 Lot 同序追加）。"""
        if qty <= 0:
            return
        self._lots.setdefault((aid, symbol), []).append(
            [float(qty), bool(is_voucher), buy_date])

    def note_sell(self, aid, symbol, qty, day, t1=True):
        """记录一笔卖出成交事实，按 FIFO + T+1 口径从来源队列扣减
        （与 engine.sim.sell / pipeline._replay_fill 的 Lot 消耗顺序
        对齐：T+1 账户跳过 buy_date >= day 的 lot）。返回 True 表示
        本笔卖出触及了券资助买入的仓位 → 不得生成新券（防再生）。"""
        q = self._lots.get((aid, symbol)) or []
        left = float(qty)
        touched = False
        for lot in q:
            if left <= 1e-9:
                break
            if lot[0] <= 1e-9:
                continue
            if t1 and not (lot[2] < day):
                continue
            take = min(lot[0], left)
            lot[0] -= take
            left -= take
            if lot[1] and take > 1e-9:
                touched = True
        return touched

    # ---------------- 轮末结算 ----------------
    def settle(self, asof_by_account):
        """按各账户日历把已过有效期且未使用的券标 expired。
        asof_by_account: {aid: 该账户回放的最后日期}。"""
        for v in self.vouchers:
            if v["status"] != "active":
                continue
            asof = asof_by_account.get(v["account"])
            if asof and self._expired(v, asof):
                v["status"] = "expired"
        return self.vouchers
