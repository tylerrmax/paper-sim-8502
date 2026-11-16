# -*- coding: utf-8 -*-
"""Market-First P0-2：机构事件研究层去重。

同一底层机构事件经 live/stored 双路入账只计一次净额；
不同日期、不同底层事件分别保留；禁止按 symbol 单独去重；
候选池原始事件全量保留不动。
"""
import json

from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.research import InstFlowModule, ResearchEngine

BASE_EVIDENCE = {
    "inst_buy": 200.0, "inst_sell": 100.0, "inst_net": 100.0,
    "seat_buy_records": 2, "seat_sell_records": 1,
    "trade_id": "T001", "data_date": "2026-11-13",
}


def _ingest(pool, source_id, evidence, discovered_at="2026-11-16T09:00:00+08:00",
            symbol="002623"):
    ev = DiscoveryEvent(
        market="SZ", symbol=symbol, name="亚玛顿",
        source_id=source_id, discovered_at=discovered_at,
        trigger_condition="inst_net>0", evidence=evidence)
    cid, _ = pool.ingest(ev)
    return cid


def _inst_factors(pool, cid):
    engine = ResearchEngine(pool, modules=[InstFlowModule()])
    rid = engine.run(cid, [], data_asof="2026-11-16",
                     data_version="unit-test")
    row = pool.conn.execute(
        "SELECT factors_json, evidence_json FROM research_reports"
        " WHERE id=?", (rid,)).fetchone()
    factors = json.loads(row["factors_json"])["inst_flow"]["factors"]
    evidence = json.loads(row["evidence_json"])["modules"]["inst_flow"]
    return factors, evidence


def test_live_stored_same_event_counted_once(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    cid = _ingest(pool, "inst_lhb_live", dict(BASE_EVIDENCE))
    _ingest(pool, "inst_lhb_stored", dict(BASE_EVIDENCE),
            discovered_at="2026-11-16T09:05:00+08:00")
    # 候选池原始事件全量保留（去重只发生在研究聚合层）
    assert len(pool.events_for(cid)) == 2
    factors, evidence = _inst_factors(pool, cid)
    assert factors["events_count"] == 1
    assert factors["raw_events_count"] == 2
    assert factors["total_inst_net"] == 100.0  # 只计一次净额
    dedup = evidence["dedup"]
    assert len(dedup["deduped_event_ids"]) == 1
    assert len(dedup["dropped_duplicate_ids"]) == 1
    assert set(dedup["raw_event_ids"]) == set(
        dedup["deduped_event_ids"]) | set(dedup["dropped_duplicate_ids"])


def test_different_dates_both_kept(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    cid = _ingest(pool, "inst_lhb_live", dict(BASE_EVIDENCE))
    ev2 = dict(BASE_EVIDENCE, trade_id="T002",
               data_date="2026-11-16", inst_net=50.0)
    _ingest(pool, "inst_lhb_live", ev2,
            discovered_at="2026-11-16T18:00:00+08:00")
    factors, _ = _inst_factors(pool, cid)
    assert factors["events_count"] == 2
    assert factors["total_inst_net"] == 150.0


def test_different_trade_id_same_day_both_kept(tmp_path):
    # 同标的同日不同底层事件（trade_id 不同）是独立真实事件
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    cid = _ingest(pool, "inst_lhb_live", dict(BASE_EVIDENCE))
    ev2 = dict(BASE_EVIDENCE, trade_id="T999", inst_net=30.0)
    _ingest(pool, "inst_lhb_stored", ev2,
            discovered_at="2026-11-16T09:05:00+08:00")
    factors, _ = _inst_factors(pool, cid)
    assert factors["events_count"] == 2
    assert factors["total_inst_net"] == 130.0


def test_missing_trade_id_falls_back_to_full_tuple(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    no_id = {k: v for k, v in BASE_EVIDENCE.items() if k != "trade_id"}
    cid = _ingest(pool, "inst_lhb_live", dict(no_id))
    # 全元组相同（仅来源不同）-> 同一事件，去重
    _ingest(pool, "inst_lhb_stored", dict(no_id),
            discovered_at="2026-11-16T09:05:00+08:00")
    # 元组不同（inst_buy 不同）-> 独立事件，保留
    diff = dict(no_id, inst_buy=999.0, inst_net=899.0)
    _ingest(pool, "inst_lhb_stored", diff,
            discovered_at="2026-11-16T09:06:00+08:00")
    factors, evidence = _inst_factors(pool, cid)
    assert factors["events_count"] == 2
    assert factors["total_inst_net"] == 100.0 + 899.0
    assert len(evidence["dedup"]["dropped_duplicate_ids"]) == 1


def test_dedup_report_reproducible(tmp_path):
    # 去重前后研究报告可复算：同输入重跑，factors 全等
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    cid = _ingest(pool, "inst_lhb_live", dict(BASE_EVIDENCE))
    _ingest(pool, "inst_lhb_stored", dict(BASE_EVIDENCE),
            discovered_at="2026-11-16T09:05:00+08:00")
    f1, e1 = _inst_factors(pool, cid)
    f2, e2 = _inst_factors(pool, cid)
    assert f1 == f2
    assert e1["dedup"] == e2["dedup"]
    assert e1["total_inst_net"] == e2["total_inst_net"] == 100.0
