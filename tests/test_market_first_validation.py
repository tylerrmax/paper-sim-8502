# -*- coding: utf-8 -*-
"""Market-First：验证引擎测试——可复算、去重、缺失不记 0、前向/回填区分。"""
import pytest

from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.validation import (DuplicateSampleError, create_sample,
                                     evaluate_sample, get_sample)

ENTRY = "2026-01-05"


def _pool(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    return pool, cid


def _bars(after_closes, entry_close=10.0):
    bars = [(ENTRY, 10.0, 10.0, 10.0, entry_close, 1000.0)]
    for i, c in enumerate(after_closes):
        bars.append(("2026-01-%02d" % (6 + i), c, c, c, c, 1000.0))
    return bars


def test_evaluate_mature_exact_values(tmp_path):
    pool, cid = _pool(tmp_path)
    sid = create_sample(pool, cid, "inst_lhb_stored", ENTRY, 10.0, "forward")
    out = evaluate_sample(pool, sid, _bars([11.0] * 20))
    assert out["maturity_status"] == "mature"
    assert out["ret_t5"] == pytest.approx(0.1)
    assert out["ret_t10"] == pytest.approx(0.1)
    assert out["ret_t20"] == pytest.approx(0.1)
    assert out["max_drawdown"] == pytest.approx(0.0)
    assert out["eval_version"] == "mf-val-1.0"
    assert out["evaluated_at"]


def test_evaluate_drawdown_uses_benchmark_peak(tmp_path):
    pool, cid = _pool(tmp_path)
    sid = create_sample(pool, cid, "inst_lhb_stored", ENTRY, 10.0, "backfill")
    out = evaluate_sample(pool, sid, _bars([9.0] + [11.0] * 19))
    assert out["max_drawdown"] == pytest.approx(-0.1)
    assert out["kind"] == "backfill"


def test_evaluate_immature_partial_horizons(tmp_path):
    pool, cid = _pool(tmp_path)
    sid = create_sample(pool, cid, "inst_lhb_stored", ENTRY, 10.0, "forward")
    out = evaluate_sample(pool, sid, _bars([11.0] * 7))
    assert out["maturity_status"] == "immature"
    assert out["ret_t5"] == pytest.approx(0.1)
    assert out["ret_t10"] is None  # 未成熟的远期指标留空，不记 0
    assert out["ret_t20"] is None


def test_evaluate_missing_never_zero(tmp_path):
    pool, cid = _pool(tmp_path)
    sid = create_sample(pool, cid, "inst_lhb_stored", ENTRY, 10.0, "forward")
    out = evaluate_sample(pool, sid, [])
    assert out["maturity_status"] == "missing"
    assert out["missing_reason"] == "no_price_data"
    for k in ("ret_t5", "ret_t10", "ret_t20", "max_drawdown"):
        assert out[k] is None


def test_duplicate_sample_rejected(tmp_path):
    pool, cid = _pool(tmp_path)
    create_sample(pool, cid, "inst_lhb_stored", ENTRY, 10.0, "forward")
    with pytest.raises(DuplicateSampleError):
        create_sample(pool, cid, "inst_lhb_stored", ENTRY, 10.0, "forward")
    # 不同来源的同日样本是独立样本（防的是同键重复统计）
    sid2 = create_sample(pool, cid, "inst_lhb_live", ENTRY, 10.0, "forward")
    assert sid2 >= 1
    assert get_sample(pool, sid2)["source_id"] == "inst_lhb_live"


def test_create_sample_invalid_kind(tmp_path):
    pool, cid = _pool(tmp_path)
    with pytest.raises(ValueError):
        create_sample(pool, cid, "inst_lhb_stored", ENTRY, 10.0, "retro")
