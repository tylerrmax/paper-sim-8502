# -*- coding: utf-8 -*-
"""Market-First：发现适配器（stored 路径）与统一候选池归并测试。

只用本地 fixture 库，不触网、不读生产库。
"""
import sqlite3

import pytest

from market_first.discovery import InstLhbAdapter, classify_market
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool


def _fixture_sim_db(path):
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE inst_lhb_daily (date TEXT, symbol TEXT, name TEXT,"
        " inst_buy REAL, inst_sell REAL, inst_net REAL,"
        " seat_buy_records INTEGER, seat_sell_records INTEGER,"
        " window TEXT, trade_id TEXT, fetched_at TEXT)")
    rows = [
        ("2026-11-13", "603893", "瑞芯微", 200.0, 50.0, 150.0, 2, 1,
         "1d", "T001", "2026-11-13 18:00:00"),
        ("2026-11-13", "000892", "欢瑞世纪", 10.0, 90.0, -80.0, 1, 2,
         "1d", "T002", "2026-11-13 18:00:00"),
        ("2026-11-13", "123291", "佐力转债", 300.0, 0.0, 300.0, 1, 0,
         "1d", "T003", "2026-11-13 18:00:00"),
    ]
    con.executemany("INSERT INTO inst_lhb_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
    con.commit()
    con.close()
    return str(path)


def test_classify_market():
    assert classify_market("601566") == "SH"
    assert classify_market("002623") == "SZ"
    assert classify_market("301697") == "SZ"
    assert classify_market("920021") == "BJ"
    assert classify_market("123291") == "CB"
    assert classify_market("HK9660") == "HK"


def test_adapter_stored_filters_and_declares_coverage(tmp_path):
    sim = _fixture_sim_db(tmp_path / "sim_fixture.db")
    adapter = InstLhbAdapter(sim_db_path=sim, prefer="stored")
    events = adapter.discover("2026-11-13")
    # inst_net<0 的 000892 必须被触发条件过滤
    symbols = {e.symbol for e in events}
    assert symbols == {"603893", "123291"}
    assert adapter.last_obtained_via == "stored"
    ev = next(e for e in events if e.symbol == "603893")
    assert ev.source_id == "inst_lhb_stored"
    assert ev.market == "SH"
    assert "inst_net>0" in ev.trigger_condition
    # 覆盖声明必须如实：不得把龙虎榜口径称作全市场扫描
    assert "不得称全市场扫描" in adapter.COVERAGE
    assert ev.evidence["obtained_via"] == "stored"
    assert ev.evidence["data_date"] == "2026-11-13"
    assert ev.evidence["inst_net"] == 150.0


def test_adapter_empty_date_returns_empty(tmp_path):
    sim = _fixture_sim_db(tmp_path / "sim_fixture.db")
    adapter = InstLhbAdapter(sim_db_path=sim, prefer="stored")
    assert adapter.discover("2026-01-01") == []


def _event(source_id="inst_lhb_stored", symbol="603893", market="SH"):
    return DiscoveryEvent(
        market=market, symbol=symbol, name="瑞芯微", source_id=source_id,
        discovered_at="2026-11-16T09:00:00+08:00",
        trigger_condition="龙虎榜机构专用席位净买入>0（inst_net>0）",
        price_at_discovery=88.5, price_data_time="2026-11-13",
        evidence={"inst_net": 150.0, "data_date": "2026-11-13"})


def test_pool_merge_dedup_keeps_all_sources(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    cid1, created1 = pool.ingest(_event("inst_lhb_live"))
    cid2, created2 = pool.ingest(_event("inst_lhb_stored"))
    cid3, created3 = pool.ingest(_event("inst_lhb_live"))
    assert created1 is True and created2 is False and created3 is False
    assert cid1 == cid2 == cid3
    assert len(pool.list_candidates()) == 1
    events = pool.events_for(cid1)
    assert len(events) == 3  # 多次/多来源发现全部保留，不覆盖
    assert [e["source_id"] for e in events] == [
        "inst_lhb_live", "inst_lhb_stored", "inst_lhb_live"]
    hist = pool.history_for(cid1)
    assert sum(1 for h in hist if h["field"] == "discovery_event") == 3
    cand = pool.get("SH", "603893")
    assert cand["id"] == cid1
    assert cand["promotion_status"] == "discovered"
    assert cand["research_status"] == "pending"


def test_pool_distinct_symbols_distinct_candidates(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    a, _ = pool.ingest(_event(symbol="603893", market="SH"))
    b, _ = pool.ingest(_event(symbol="000530", market="SZ"))
    assert a != b
    assert len(pool.list_candidates()) == 2


def test_pool_set_fields_whitelist_and_history(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    cid, _ = pool.ingest(_event())
    pool.set_fields(cid, actor="tester", research_status="done",
                    reason="unit test")
    cand = pool.get_by_id(cid)
    assert cand["research_status"] == "done"
    hist = [h for h in pool.history_for(cid)
            if h["field"] == "research_status"]
    assert len(hist) == 1 and hist[0]["old_value"] == "pending"
    assert hist[0]["new_value"] == "done"
    with pytest.raises(ValueError):
        pool.set_fields(cid, actor="tester", bogus_field="x")
