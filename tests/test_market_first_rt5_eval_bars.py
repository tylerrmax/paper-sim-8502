# -*- coding: utf-8 -*-
"""RT-5 对抗测试（2026-11-23 红队集中修复第 7 项）。

问题：evaluate_sample 无条件接受调用方 bars 计算 forward
收益——伪造 bars 可直接制造 ret_t20 / 成熟样本，穿透
forward_stats 与 judge。

修复：forward 样本评估前先过 _verify_eval_bars——标的取
样本 candidate 的库内 symbol，调用方 bars 必须与系统自有
fetched_bars（bars 取数层经 store_bars/get_bars 落库）逐 bar
一致（日期存在、OHLCV 差 <= 1e-6）；不可信/无法验证时写
maturity_status='not_evaluable'、missing_reason=
'bars_untrusted: ...'、收益全 NULL，不产生有效 forward
结果（与既有 missing 口径一致的等价拒绝）。data_version
只留痕不作依据。

夹具迁移登记：既有 9 个文件的 forward 评估 helper 已在
evaluate 前经 bars_mod.store_bars 受控通道落库（逐项见
交付说明），断言零改动。backfill 样本不进 forward_stats，
不在本项校验口径（范围对照有用例固化）。
"""
from datetime import date, timedelta

import pytest

from market_first import bars as bars_mod
from market_first import validation
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool


def _gen_bars(start=date(2026, 11, 13), days=40, step=0.1):
    out = []
    d = start
    i = 0
    while d <= start + timedelta(days=days):
        if d.weekday() < 5:
            c = 10.0 + step * i
            out.append((d.isoformat(), c, c, c, c, 1000.0))
            i += 1
        d += timedelta(days=1)
    return out


def _forward_sample(tmp_path, symbol="603893"):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol=symbol, name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    pool.clock = lambda: ev.discovered_at
    cid, _ = pool.ingest(ev)
    ev_row = pool.events_for(cid)[0]
    bars = _gen_bars()
    sid = validation.create_sample(
        pool, cid, ev_row["source_id"], kind="forward",
        discovery_event_id=ev_row["id"], bars=bars,
        fetched_at="2026-11-17T16:00:00+08:00")
    return pool, cid, sid, bars


def test_forged_bar_values_not_evaluable(tmp_path):
    """伪造对抗：fetched 中落真实 bars，评估时把收盘整体
    抬高伪造收益 -> not_evaluable、收益全 NULL。"""
    pool, cid, sid, bars = _forward_sample(tmp_path)
    bars_mod.store_bars("603893", bars, "test-fixture", pool.db_path)
    forged = [(d, o, h, l, c * 1.5, v) for (d, o, h, l, c, v) in bars]
    out = validation.evaluate_sample(pool, sid, forged)
    assert out["maturity_status"] == "not_evaluable"
    assert out["missing_reason"].startswith("bars_untrusted")
    assert out["ret_t5"] is None and out["ret_t20"] is None
    assert out["max_drawdown"] is None


def test_unverifiable_without_fetched_bars(tmp_path):
    """无法验证：fetched_bars 无任何落库 -> 同样拒绝，
    不得凭调用方自报 bars 生成结果。"""
    pool, cid, sid, bars = _forward_sample(tmp_path)
    out = validation.evaluate_sample(pool, sid, bars)
    assert out["maturity_status"] == "not_evaluable"
    assert "no fetched_bars" in out["missing_reason"]


def test_bar_date_not_in_fetched_rejected(tmp_path):
    """完整性：评估 bars 含 fetched 中不存在的日期 ->
    not_evaluable。"""
    pool, cid, sid, bars = _forward_sample(tmp_path)
    bars_mod.store_bars("603893", bars, "test-fixture", pool.db_path)
    extra = bars + [("2027-01-04", 99.0, 99.0, 99.0, 99.0, 1000.0)]
    out = validation.evaluate_sample(pool, sid, extra)
    assert out["maturity_status"] == "not_evaluable"
    assert "not in fetched_bars" in out["missing_reason"]


def test_bars_seeded_under_wrong_symbol_rejected(tmp_path):
    """标的核对：bars 落在别的 symbol 名下，本标的无据 ->
    not_evaluable（标的以 candidate 库内记录为准）。"""
    pool, cid, sid, bars = _forward_sample(tmp_path)
    bars_mod.store_bars("999999", bars, "test-fixture", pool.db_path)
    out = validation.evaluate_sample(pool, sid, bars)
    assert out["maturity_status"] == "not_evaluable"


def test_verified_bars_positive_control(tmp_path):
    """正常路径：经受控通道落库的 bars 评估 -> mature，
    ret_t20 按落库行情计算。"""
    pool, cid, sid, bars = _forward_sample(tmp_path)
    bars_mod.store_bars("603893", bars, "test-fixture", pool.db_path)
    out = validation.evaluate_sample(pool, sid, bars)
    assert out["maturity_status"] == "mature"
    assert out["ret_t20"] is not None and out["ret_t20"] > 0


def test_backfill_sample_outside_rt5_scope(tmp_path):
    """范围对照：backfill 样本不进 forward_stats，不在本项
    校验口径——无 fetched 落库时仍按既有口径评估。"""
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol="603893", name="测试标的",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    pool.clock = lambda: ev.discovered_at
    cid, _ = pool.ingest(ev)
    bars = _gen_bars()
    sid = validation.create_sample(
        pool, cid, ev.source_id, kind="backfill",
        entry_date="2026-11-16", benchmark_price=10.0, bars=bars)
    out = validation.evaluate_sample(pool, sid, bars)
    assert out["maturity_status"] == "mature"
