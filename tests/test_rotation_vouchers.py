#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换仓额度券专项测试（四项补齐① 乙，2026-10-27）。

覆盖任务单常驻回归清单：
- 券生命周期：主动信号卖出生成 → 禁开态用券成交（面额封顶、
  fills 定格券号）→ 用完作废、后续买入再拦；
- 止损卖出零生成：ATR 策略止损被动平仓不生成券；
- degraded 禁用：券已生成但降级态不可用、BUY 被拦且留痕；
- 一对一：同券第二笔买入被拒；不拼券（选券只返一张）；
- 净额计算含费：面额 = qty×成交价 − 卖出费（单元 + 集成双证）；
- 防再生：用券买入的仓位卖出不生成新券（含来源队列 FIFO/T+1
  单元用例）；
- 过期：卖出后第 5 个交易日可用、第 6 个不可用（单元 + 集成）；
- 频率上限：相邻两次用券间隔 <5 交易日被拒（单元）；
- 不豁免约束对券买入逐项生效：禁入闸优先于券（集成）；sizer 的
  2% 风险预算 / 簇 4% / 总仓位 / 现金封顶在 amount_cap 之外照常
  绑定（单元，amount_cap 只叠加不替代）；终局校验的额度项为断言
  式双保险（代码内），不在此单独造例（见 impl 报告口径说明）；
- 毕业统计不受券影响：券 round-trip 的 strategy_version 与普通
  成交同口径、买入腿可凭 fills.rotation_voucher_id 单独识别打标
  （毕业函数本身的版本隔离由 test_e2e_batch2 既有用例锁死）。

安全：写操作只在 tmp fixture 库；生产库只读。运行:
.venv/bin/python -m pytest tests/test_rotation_vouchers.py -q
"""
import os
import sqlite3
import sys
from datetime import date, timedelta

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tests"))

from test_regime_sizing_20261026 import (  # noqa: E402
    _bars_from_closes, _cfg, _fresh_db, _idx_bars, _insert, _write_map)
from engine.rotation import RotationBook, net_proceeds  # noqa: E402
from engine.pipeline import position_sizer_v2, run as pipeline_run  # noqa: E402

START = date(2026, 10, 1)
SYM = "002446"  # 母板 stop_ref 7.70，序列价格全程在其上方（除禁入用例）


def _d(i):
    return (START + timedelta(days=i)).isoformat()


# 主链 closes（下标=自 2026-10-01 起天数）：
# i25 回落触发策略 BUY（被环境闸拦）；预置持仓；i28 涨 +15% 策略
# SELL（i29 执行→生券）；i30 回落 BUY（i31 执行→用券成交）；
# i32 涨 +15% SELL（i33 执行→卖券仓位，防再生不生券）；
# i34 回落 BUY（i35 执行→无券被拦）。
MAIN_CLOSES = ([10.0] * 25 + [8.9, 8.9, 8.9, 10.3, 10.3, 9.2, 9.2,
                             10.6, 10.6, 9.4, 9.4])


def _fixture(tmp_path, state="blocked", closes=None, idx_n=40):
    db = _fresh_db(tmp_path)
    _insert(db, SYM, _bars_from_closes(closes or MAIN_CLOSES, start=START))
    _insert(db, "IDX000001", _idx_bars(idx_n, on=False), market="index")
    # 预置持仓：执行日 2026-10-28 买入 1000 股（历史成交事实，走重放建仓）
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO fills(date,account,symbol,side,qty,price,fee,pnl,"
        "signal_date,strategy_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("2026-10-28", "sim_a", SYM, "BUY", 1000.0, 9.0, 5.0, 0.0,
         "2026-10-27", "v1.1-2026-10-26"))
    con.commit()
    con.close()
    return db, _cfg([SYM], _write_map(tmp_path, state))


def _q(db, sql, args=()):
    con = sqlite3.connect(db)
    rows = con.execute(sql, args).fetchall()
    con.close()
    return rows


# ---------------- RotationBook 单元 ----------------

def _book_with_cal(n=20):
    b = RotationBook()
    b.set_calendar("sim_a", [_d(i) for i in range(n)])
    return b


def test_net_proceeds_deducts_fee():
    assert net_proceeds(1000, 10.2794, 12.8493) == pytest.approx(10266.5507)


def test_voucher_window_boundary():
    b = _book_with_cal()
    v = b.issue("sim_a", 101, SYM, _d(0), _d(0), 100.0, 10.0, 5.0)
    got, _ = b.pick("sim_a", _d(5))     # 第 5 个交易日：仍可用
    assert got is v
    got, why = b.pick("sim_a", _d(6))   # 第 6 个交易日：过期
    assert got is None and "过期" in why


def test_voucher_one_to_one_and_no_stacking():
    b = _book_with_cal()
    v1 = b.issue("sim_a", 101, SYM, _d(0), _d(0), 100.0, 10.0, 5.0)
    v2 = b.issue("sim_a", 102, "002639", _d(0), _d(0), 100.0, 10.0, 5.0)
    got, _ = b.pick("sim_a", _d(1))
    assert got is v1  # 只返一张（先到期先用），不拼 v1+v2
    b.mark_used(v1, 201, _d(1), SYM)
    got, _ = b.pick("sim_a", _d(6))  # v1 已用；v2 在第 6 日已过期
    assert got is None
    # 同券第二笔：已 used 的券永不再被选出
    b2 = _book_with_cal()
    w = b2.issue("sim_a", 103, SYM, _d(0), _d(0), 100.0, 10.0, 5.0)
    b2.mark_used(w, 202, _d(1), SYM)
    got, why = b2.pick("sim_a", _d(2))
    assert got is None and "无有效额度券" in why


def test_voucher_frequency_cap():
    b = _book_with_cal()
    v1 = b.issue("sim_a", 101, SYM, _d(0), _d(0), 100.0, 10.0, 5.0)
    b.issue("sim_a", 102, SYM, _d(5), _d(5), 100.0, 10.0, 5.0)
    got, _ = b.pick("sim_a", _d(5))
    assert got is v1
    b.mark_used(v1, 201, _d(5), SYM)
    got, why = b.pick("sim_a", _d(8))   # 距上次用券 3 个交易日 <5
    assert got is None and "频率上限" in why
    got, _ = b.pick("sim_a", _d(10))    # 间隔恰 5 → 可用（第二张券窗口第 5 日）
    assert got is not None


def test_anti_regeneration_fifo_queue():
    b = RotationBook()
    b.note_buy("sim_a", SYM, 1000, _d(0), is_voucher=False)
    b.note_buy("sim_a", SYM, 500, _d(1), is_voucher=True)
    # 先卖 800：只消耗普通 lot → 不触及券仓位
    assert b.note_sell("sim_a", SYM, 800, _d(2)) is False
    # 再卖 300：溢出到券 lot（剩普通 200 + 券 100）→ 触及
    assert b.note_sell("sim_a", SYM, 300, _d(3)) is True


def test_anti_regeneration_t1_skip():
    b = RotationBook()
    b.note_buy("sim_a", SYM, 500, _d(2), is_voucher=True)
    # T+1：当日买入的 lot 当日不可卖 → 不扣减、不触及
    assert b.note_sell("sim_a", SYM, 500, _d(2), t1=True) is False
    # 次日可卖 → 触及券仓位
    assert b.note_sell("sim_a", SYM, 500, _d(3), t1=True) is True


def test_settle_marks_expired():
    b = _book_with_cal()
    v = b.issue("sim_a", 101, SYM, _d(0), _d(0), 100.0, 10.0, 5.0)
    b.settle({"sim_a": _d(5)})
    assert v["status"] == "active"
    b.settle({"sim_a": _d(6)})
    assert v["status"] == "expired"


# ---------------- sizer：券买入其余约束照常绑定 ----------------

def _ctx(**kw):
    base = {"nav": 1000000.0, "cash": 1000000.0, "entry_px": 10.0,
            "lot": 100, "risk_pct": 0.02, "buy_fee_rate": 0.00025,
            "buy_min_fee": 5.0, "sell_fee_rate": 0.00125,
            "sell_min_fee": 5.0, "slippage": 0.002,
            "max_pos_pct": 0.70, "pos_value": 0.0,
            "stop_dist": 1.0, "stop_price": 9.0,
            "cluster_existing_risk": 0.0, "cluster_budget_pct": 0.04,
            "cluster_label": "测试簇", "data_ok": True,
            "data_missing_reason": ""}
    base.update(kw)
    return base


def test_sizer_other_caps_still_bind_with_voucher_cap():
    plain, _ = position_sizer_v2(_ctx())
    # 面额极大时与无券一致（2% 风险预算绑定，券不放大权限）
    with_cap, _ = position_sizer_v2(_ctx(amount_cap=1e12))
    assert with_cap == plain
    # 面额绑定时名义额 ≤ 面额
    capped, _ = position_sizer_v2(_ctx(amount_cap=5000.0))
    assert 0 < capped * 10.0 <= 5000.0 + 1e-6
    # 簇预算已满：有券也禁单
    q, r = position_sizer_v2(_ctx(amount_cap=1e6,
                                  cluster_existing_risk=40000.0))
    assert q == 0 and "风险簇预算已满" in r
    # 现金不足一手：有券也禁单
    q, r = position_sizer_v2(_ctx(amount_cap=1e6, cash=500.0))
    assert q == 0
    # 总仓位已满：有券也禁单
    q, r = position_sizer_v2(_ctx(amount_cap=1e6, pos_value=700000.0))
    assert q == 0


# ---------------- pipeline 集成：主链 ----------------

def test_voucher_full_lifecycle(tmp_path):
    db, cfg = _fixture(tmp_path)
    pipeline_run(db, cfg)
    fills = _q(db, "SELECT id,date,symbol,side,qty,price,fee,"
                   "rotation_voucher_id,strategy_version FROM fills"
                   " ORDER BY id")
    sells = [f for f in fills if f[3] == "SELL"]
    assert len(sells) == 2, fills  # 券来源卖出 + 券仓位卖出（防再生笔）
    s1 = sells[0]
    assert s1[1] == "2026-10-30" and s1[4] == 1000.0
    net = s1[4] * s1[5] - s1[6]
    vouchers = _q(db, "SELECT voucher_id,source_fill_id,amount,status,"
                      "used_fill_id,used_symbol FROM rotation_vouchers")
    assert len(vouchers) == 1, vouchers  # 防再生：第二笔 SELL 未生券
    vid, src_id, amount, status, used_fill_id, used_symbol = vouchers[0]
    assert vid == "RV-%d" % s1[0] and src_id == s1[0]
    assert abs(amount - net) < 1e-6, (amount, net)  # 净回收计额（含费）
    assert status == "used" and used_symbol == SYM
    vbuys = [f for f in fills if f[3] == "BUY" and f[1] == "2026-11-01"]
    assert len(vbuys) == 1, fills
    vb = vbuys[0]
    assert vb[4] * vb[5] <= amount + 1e-6       # 名义额 ≤ 券面额
    assert vb[7] == s1[0]                        # fills 定格券号引用
    assert used_fill_id == vb[0]
    assert vb[8] == "v1.1-2026-10-26"            # 毕业版本口径与普通成交一致
    # 一对一用尽：2026-11-05 的回落 BUY 无券可用被拦
    assert not [f for f in fills if f[3] == "BUY" and f[1] == "2026-11-05"]
    blk = _q(db, "SELECT reason FROM blocked_signals"
                 " WHERE date='2026-11-05' AND side='BUY'")
    assert blk and "无可用换仓额度券" in blk[0][0], blk


def test_voucher_degraded_disabled(tmp_path):
    import copy
    import json as _json
    db, cfg = _fixture(tmp_path)
    # 造 degraded：删映射 gate_b79 字段（env fail-closed 降级）
    from test_regime_sizing_20261026 import _real_map
    m = copy.deepcopy(_real_map())
    del m["gate_b79"]
    p = tmp_path / "map_degraded.json"
    p.write_text(_json.dumps(m, ensure_ascii=False), encoding="utf-8")
    cfg["env_map_path"] = str(p)
    pipeline_run(db, cfg)
    # SELL 照常成交、券照常生成（卖出侧不受环境闸）
    sells = _q(db, "SELECT COUNT(*) FROM fills WHERE side='SELL'")
    assert sells[0][0] == 1
    vouchers = _q(db, "SELECT status,used_fill_id FROM rotation_vouchers")
    assert len(vouchers) == 1 and vouchers[0][1] is None  # 未被使用
    assert vouchers[0][0] == "expired"  # 至回放末已过 5 交易日窗口
    # 券在手的买回日（2026-11-01）BUY 被降级禁用拦截
    blk = _q(db, "SELECT reason FROM blocked_signals"
                 " WHERE date='2026-11-01' AND side='BUY'")
    assert blk and "安全降级态禁用换仓额度券" in blk[0][0], blk
    assert not _q(db, "SELECT 1 FROM fills WHERE side='BUY'"
                      " AND date='2026-11-01'")


def test_voucher_expired_by_execution_day(tmp_path):
    # SELL 后价格在 9.3 徘徊（不触发回落买入），第 9 天才触发 BUY
    closes = ([10.0] * 25 + [8.9, 8.9, 8.9, 10.3, 10.3]
              + [9.3] * 7 + [9.2, 9.2])
    db, cfg = _fixture(tmp_path, closes=closes, idx_n=45)
    pipeline_run(db, cfg)
    vouchers = _q(db, "SELECT status,used_fill_id FROM rotation_vouchers")
    assert len(vouchers) == 1 and vouchers[0][0] == "expired"
    assert vouchers[0][1] is None
    assert not _q(db, "SELECT 1 FROM fills WHERE side='BUY'"
                      " AND date>='2026-11-01'")
    blk = _q(db, "SELECT reason FROM blocked_signals WHERE side='BUY'"
                 " ORDER BY date DESC LIMIT 1")
    assert blk and "过期" in blk[0][0], blk


def test_atr_stop_sell_issues_no_voucher(tmp_path):
    # 允许进攻：策略 BUY 建仓后价格跌破 ATR 策略止损 → Phase 0 被动
    # 平仓。断言成交存在、券表为空（被动卖出零生成）。
    db = _fresh_db(tmp_path)
    closes = [10.0] * 25 + [8.9, 8.9, 8.2, 8.2]
    _insert(db, SYM, _bars_from_closes(closes, start=START))
    _insert(db, "IDX000001", _idx_bars(29, on=True), market="index")
    pipeline_run(db, _cfg([SYM], _write_map(tmp_path, "attack")))
    sells = _q(db, "SELECT date,qty FROM fills WHERE side='SELL'")
    assert len(sells) == 1 and sells[0][0] == "2026-10-28", sells
    assert _q(db, "SELECT COUNT(*) FROM rotation_vouchers")[0][0] == 0


def test_ban_entry_gate_precedes_voucher(tmp_path):
    # 券已生成，但目标 BUY 跌破母板止损参考 → 禁入闸先于环境闸/券
    # 拦截（券不豁免禁入），券不被消耗。
    closes = ([10.0] * 25 + [8.9, 8.9, 8.9, 10.3, 10.3, 7.5, 7.5])
    db, cfg = _fixture(tmp_path, closes=closes, idx_n=35)
    pipeline_run(db, cfg)
    assert not _q(db, "SELECT 1 FROM fills WHERE side='BUY'"
                      " AND date='2026-11-01'")
    blk = _q(db, "SELECT reason FROM blocked_signals"
                 " WHERE date='2026-11-01' AND side='BUY'")
    assert blk and "跌破母板止损参考" in blk[0][0], blk
    vouchers = _q(db, "SELECT status,used_fill_id FROM rotation_vouchers")
    assert len(vouchers) == 1 and vouchers[0][1] is None
