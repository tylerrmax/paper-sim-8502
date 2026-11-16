# -*- coding: utf-8 -*-
"""Market-First：研究引擎测试——分项报告、版本留痕、绝不产交易指令。"""
import json
from datetime import date, timedelta

import pytest

from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.research import (ENGINE_VERSION, FORBIDDEN_KEYS,
                                   ResearchEngine, TrendPatternModule,
                                   assert_no_instruction_keys)


def _bars(n=65, start_close=10.0, step=0.1):
    base = date(2026, 1, 1)
    out = []
    for i in range(n):
        c = start_close + step * i
        out.append(((base + timedelta(days=i)).isoformat(),
                    c - 0.05, c + 0.1, c - 0.1, c, 1000.0 + i))
    return out


def _all_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(str(k).lower())
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            keys |= _all_keys(v)
    return keys


def test_assert_no_instruction_keys():
    assert_no_instruction_keys({"factors": {"ma20": 1.0}, "note": "x"})
    with pytest.raises(ValueError):
        assert_no_instruction_keys({"factors": {"side": "BUY"}})
    with pytest.raises(ValueError):
        assert_no_instruction_keys({"a": [{"order": 1}]})


def test_trend_module_insufficient_bars_not_zero(tmp_path):
    mod = TrendPatternModule()
    out = mod.compute({}, _bars(10), {})
    assert out["subtotal"] is None  # 数据不足记 None，不记 0 充数
    assert out["factors"]["available"] is False


def test_research_engine_report_breakdown_and_no_instructions(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SH", symbol="603893", name="瑞芯微",
        source_id="inst_lhb_stored",
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 5000.0})
    cid, _ = pool.ingest(ev)
    engine = ResearchEngine(pool)
    rid = engine.run(cid, _bars(65), data_asof="2026-04-10",
                     data_version="unit-test-bars")
    assert rid >= 1
    row = pool.conn.execute(
        "SELECT * FROM research_reports WHERE id=?", (rid,)).fetchone()
    assert row["engine_version"] == ENGINE_VERSION
    assert row["data_asof"] == "2026-04-10"
    assert row["data_version"] == "unit-test-bars"
    factors = json.loads(row["factors_json"])
    assert set(factors) == {"trend_pattern", "inst_flow"}
    assert factors["trend_pattern"]["model_version"] == "mf-trend-1.0"
    assert factors["inst_flow"]["model_version"] == "mf-inst-1.0"
    assert factors["inst_flow"]["subtotal"] == 8.0  # latest inst_net>0
    assert factors["trend_pattern"]["subtotal"] is not None
    assert row["total_score"] == pytest.approx(
        factors["trend_pattern"]["subtotal"] + 8.0)
    # 研究报告全量扫描：不得出现任何交易指令字段
    evidence = json.loads(row["evidence_json"])
    keys = _all_keys(factors) | _all_keys(evidence)
    assert not (keys & FORBIDDEN_KEYS)
    cand = pool.get_by_id(cid)
    assert cand["research_status"] == "done"
