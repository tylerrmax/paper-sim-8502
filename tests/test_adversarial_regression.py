# -*- coding: utf-8 -*-
"""常驻对抗测试库（效率升级指令 V1.0 第三节）：历史审计发现
转化为长期回归测试，不得因某轮修复完成而删除。

覆盖映射（已有测试已覆盖的项只标注对应测试名、不重复造）：
A. Benchmark 与 Forward
  A1 直接 SQL 改历史 Benchmark 必须失败 -> 本文件（fix3 新增）
  A2 建样后行情修订不能覆盖原 Benchmark ->
     test_market_first_benchmark_integrity.py::
     test_source_revision_recorded_benchmark_unchanged /
     test_benchmark_adversarial_immutable
  A3 延迟创建/回填声称不得冒充 forward -> 本文件（fix3 新增，
     MF-FWD-002 信任路径）
  A4 当日未收盘 bar 不得成为 Benchmark ->
     test_market_first_benchmark_integrity.py::
     test_forward_intraday_incomplete_bar_rejected /
     test_backfill_today_entry_intraday_rejected
  A5 节假日/半日市/跨市场日历 -> 本文件（fix3 新增，
     MF-BENCH-003）
  A6 重复建样不得产生重复样本 ->
     test_market_first_validation.py::
     test_duplicate_sample_rejected
B. 去重与机构资金流（test_market_first_inst_dedup.py）
  B1 同 trade_id 只计一次 -> test_live_stored_same_event_counted_once
  B2 不同 trade_id 真实事件不能合并 ->
     test_different_trade_id_same_day_both_kept /
     test_different_dates_both_kept
  B3 无 trade_id 不得自动合并 ->
     test_missing_trade_id_uncertain_kept_and_counted /
     test_dedup_function_three_states
  B4 uncertain 报告逐项披露 ->
     test_missing_trade_id_uncertain_kept_and_counted +
     evidence/fix2/audit_focus_uncertain_report.json
  B5 确定/不确定净额分列核算 -> test_dedup_function_three_states
  B6 报告汇总可独立复算 -> test_dedup_report_reproducible
C. 规则注册与判定
  C1 rule_id+version 精确匹配 / C2 版本-实现绑定 ->
     test_market_first_rule_binding.py（fix3 新增）
  C3 未登记/伪造版本 -> test_market_first_rule_registry.py::
     test_unregistered_rule_rejected_and_traced /
     test_forged_version_rejected / test_missing_version_rejected
  C4 draft/test/disabled 不得通过 ->
     test_market_first_rule_registry.py::
     test_draft_and_test_status_rules_rejected /
     test_disabled_rule_stops_passing_history_kept
  C5 无 approved 必 not_evaluable ->
     test_market_first_validation_gate.py::
     test_judge_no_rule_not_evaluable
  C6 伪造 decision ID / 直调受控流转必须拒绝 ->
     test_market_first_rule_registry.py::
     test_apply_validation_passed_direct_call_rejected
  C7 历史判定不被注册表变化覆盖、validation_passed 必有
     可追溯判定记录 -> 本文件（fix3 新增全称检查）+
     test_market_first_rule_binding.py::
     test_reproduce_decision_after_version_change_and_disable
D. 生产隔离
  D1 隔离测试不改生产 sim.db -> 窗口文件级快照对照
     （evidence/fix3/production/，非单元测试口径）
  D2 不污染共享 cache.db ->
     test_market_first_cache_isolation.py::
     test_get_bars_never_touches_prod_shared_cache +
     evidence/fix3/production/ 文件级快照对照
  D3 关键业务表未经授权变化 -> 同 D1 快照逐表摘要对照
  D4 快照可独立复核 -> 本文件（snapshot_tool 清单自洽）
  D5 发布门禁拒绝留持久日志 -> test_release_gate.py
"""
import sqlite3
from datetime import date, timedelta

import pytest

from market_first import calendar as cal
from market_first import validation
from market_first.db import DEFAULT_DB_PATH
from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.promotion import transition

BARS = [
    ("2026-11-12", 25.0, 25.5, 24.5, 25.2, 1000.0),
    ("2026-11-13", 26.0, 26.5, 25.8, 26.25, 1000.0),
    ("2026-11-16", 26.8, 27.2, 26.6, 27.00, 1000.0),
    ("2026-11-17", 27.1, 27.4, 26.9, 27.30, 1000.0),
]


def _pool_with_sample(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id="inst_lhb_live",
        discovered_at="2026-11-13T16:30:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    pool.clock = lambda: ev.discovered_at  # P0-5 可信时钟夹具迁移（2026-11-23）：ingest 落字时刻即模拟入库时刻，替代旧直写 UPDATE created_at 手法；断言未改动。
    cid, _ = pool.ingest(ev)
    ev_row = pool.events_for(cid)[0]
    sid = validation.create_sample(
        pool, cid, ev_row["source_id"], kind="forward",
        discovery_event_id=ev_row["id"], bars=BARS,
        fetched_at="2026-11-16T16:00:00+08:00")
    return pool, cid, sid


# ---------------------------------------------------------------------------
# A1：直接 SQL 修改/删除历史 Benchmark 必须失败（MF-BENCH-002）
# ---------------------------------------------------------------------------

def test_a1_direct_sql_benchmark_update_delete_forbidden(tmp_path):
    pool, _cid, sid = _pool_with_sample(tmp_path)
    before = validation.get_sample(pool, sid)
    raw = sqlite3.connect(str(tmp_path / "mf.db"))
    try:
        with pytest.raises(sqlite3.Error):
            raw.execute(
                "UPDATE validation_samples SET benchmark_price=0.01"
                " WHERE id=?", (sid,))
        raw.rollback()
        with pytest.raises(sqlite3.Error):
            raw.execute(
                "UPDATE validation_samples SET benchmark_date="
                "'2020-01-01' WHERE id=?", (sid,))
        raw.rollback()
        with pytest.raises(sqlite3.Error):
            raw.execute(
                "DELETE FROM validation_samples WHERE id=?", (sid,))
        raw.rollback()
    finally:
        raw.close()
    after = validation.get_sample(pool, sid)
    assert after["benchmark_price"] == before["benchmark_price"]
    assert after["benchmark_date"] == before["benchmark_date"]
    # 正常评估回写不受触发器影响（不触碰 benchmark 三列）
    validation.evaluate_sample(pool, sid, BARS)
    assert validation.get_sample(pool, sid)["benchmark_price"] \
        == before["benchmark_price"]


# ---------------------------------------------------------------------------
# A3：回填声称的 discovered_at 不能把 backfill 变 forward
# （MF-FWD-002：时间锚以库内服务器侧 ingested_at 为准）
# ---------------------------------------------------------------------------

def test_a3_backdated_discovery_claim_cannot_fake_forward(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id="inst_lhb_live",
        # 声称 2026-11-10 已发现，但事件今天才入库
        # （created_at 为服务器侧当前时间，不做对齐）
        discovered_at="2026-11-10T16:30:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    ev_row = pool.events_for(cid)[0]

    # (a) 声称发现日之后、入库日之前的 entry 不得是 forward
    sid = validation.create_sample(
        pool, cid, ev_row["source_id"], kind="forward",
        entry_date="2026-11-13", benchmark_price=26.25,
        discovery_event_id=ev_row["id"], bars=BARS,
        fetched_at="2026-11-16T16:00:00+08:00")
    assert validation.get_sample(pool, sid)["kind"] == "backfill"

    # (b) 按声称时间推导 forward entry 同样不可能：锚为入库日，
    # 其后无 bar -> 留痕拒绝
    with pytest.raises(validation.ForwardRejectedError):
        validation.create_sample(
            pool, cid, ev_row["source_id"], kind="forward",
            discovery_event_id=ev_row["id"], bars=BARS)
    hist = pool.history_for(cid)
    assert any(h["field"] == "discovery_time_anchor"
               for h in hist)
    assert any("forward_rejected" in (h["reason"] or "")
               for h in hist)


# ---------------------------------------------------------------------------
# A5：交易日历与收盘确认（MF-BENCH-003）
# ---------------------------------------------------------------------------

def test_a5_holiday_bar_not_confirmed():
    # 2026-10-01 国庆休市：该日 bar 即使日期早于取数日也无效
    assert cal.is_trading_day("SZ", "2026-10-01") is False
    assert validation.bar_close_confirmed(
        "2026-10-01", "2026-10-09T10:00:00+08:00",
        market="SZ") is False
    # 2025 清明 04-04 休市
    assert validation.bar_close_confirmed(
        "2025-04-04", "2025-04-07T10:00:00+08:00",
        market="SH") is False


def test_a5_weekend_bar_not_confirmed():
    # 2026-11-14 周六
    assert cal.is_trading_day("SZ", "2026-11-14") is False
    assert validation.bar_close_confirmed(
        "2026-11-14", "2026-11-16T10:00:00+08:00",
        market="SZ") is False


def test_a5_hk_half_day_confirm_window():
    # 2026-09-25 中秋节当日港股半日市（收市 12:00，确认 12:30）
    assert cal.is_trading_day("HK", "2026-09-25") is True
    assert cal.is_half_day("HK", "2026-09-25") is True
    assert validation.bar_close_confirmed(
        "2026-09-25", "2026-09-25T11:00:00+08:00",
        market="HK") is False
    assert validation.bar_close_confirmed(
        "2026-09-25", "2026-09-25T12:30:00+08:00",
        market="HK") is True


def test_a5_cross_market_calendar_difference():
    # 2025-04-18 港股耶稣受难日休市，A股正常交易
    assert cal.is_trading_day("HK", "2025-04-18") is False
    assert cal.is_trading_day("SZ", "2025-04-18") is True
    assert validation.bar_close_confirmed(
        "2025-04-18", "2025-04-21T10:00:00+08:00",
        market="HK") is False
    assert validation.bar_close_confirmed(
        "2025-04-18", "2025-04-21T10:00:00+08:00",
        market="SZ") is True
    # 2026-09-25 A股中秋休市，港股半日市交易
    assert validation.bar_close_confirmed(
        "2026-09-25", "2026-09-28T10:00:00+08:00",
        market="SZ") is False
    assert validation.bar_close_confirmed(
        "2026-09-25", "2026-09-28T10:00:00+08:00",
        market="HK") is True


def test_a5_unknown_year_falls_back_to_time_rule():
    # 日历未覆盖年份（2027）：回退 fix2 时间规则（早于取数日确认）
    assert cal.is_trading_day("SZ", "2027-01-04") is None
    assert validation.bar_close_confirmed(
        "2027-01-04", "2027-01-05T09:00:00+08:00",
        market="SZ") is True


# ---------------------------------------------------------------------------
# C7：validation_passed 必有可追溯 passed 判定记录（全称检查）
# ---------------------------------------------------------------------------

class _PassRule:
    version = "v1"

    def evaluate(self, scope, metrics):
        return "passed", "adversarial C7 rule"


def _bars_long(start=date(2026, 11, 13), end=date(2026, 12, 20)):
    out = []
    d = start
    i = 0
    while d <= end:
        if d.weekday() < 5:
            c = 10.0 + 0.1 * i
            out.append((d.isoformat(), c, c, c, c, 1000.0))
            i += 1
        d += timedelta(days=1)
    return out


def test_c7_every_validation_passed_has_passed_decision(tmp_path):
    pool, cid, _sid = _pool_with_sample(tmp_path)
    for state in ("pending_research", "researching", "validating"):
        transition(pool, cid, state, actor="tester")
    bars = _bars_long()
    ev = pool.events_for(cid)[0]
    sid2 = None
    # _pool_with_sample 的样本只有 4 根 bar 不成熟；另建成熟样本
    # 于第二个候选上完成 passed 流转
    ev2 = DiscoveryEvent(
        market="SZ", symbol="000530", name="冰山冷热",
        source_id="inst_lhb_live",
        discovered_at="2026-11-13T16:30:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 50.0})
    pool.clock = lambda: ev2.discovered_at  # P0-5 可信时钟夹具迁移（2026-11-23）：ingest 落字时刻即模拟入库时刻，替代旧直写 UPDATE created_at 手法；断言未改动。
    cid2, _ = pool.ingest(ev2)
    for state in ("pending_research", "researching", "validating"):
        transition(pool, cid2, state, actor="tester")
    ev2_row = pool.events_for(cid2)[0]
    sid2 = validation.create_sample(
        pool, cid2, ev2_row["source_id"], kind="forward",
        discovery_event_id=ev2_row["id"], bars=bars)
    validation.evaluate_sample(pool, sid2, bars)
    rid = "adv_c7_rule"
    try:
        validation.register_rule_implementation(rid, _PassRule())
        validation.register_rule(
            pool, rid, "v1", status="approved",
            approved_by="unit-test",
            # P0-3 集成（2026-11-23）：补齐批准信息；断言未改动。
            actor="unit-test", reason="adversarial test approval",
            approval_basis="test-only approval (tmp db)")
        out = validation.judge_validation(
            pool, cid2, rule_id=rid, rule_version="v1")
        assert out["decision"] == "passed"
    finally:
        validation.RULE_IMPLEMENTATIONS.pop((rid, "v1"), None)
    assert pool.get_by_id(cid2)["promotion_status"] \
        == "validation_passed"
    # 全称检查：库内任何 validation_passed 候选都必须存在
    # 对应 passed 判定记录，无孤儿状态
    orphans = pool.conn.execute(
        "SELECT c.id FROM candidates c"
        " WHERE c.promotion_status = 'validation_passed'"
        " AND NOT EXISTS (SELECT 1 FROM validation_decisions d"
        "  WHERE d.candidate_id = c.id"
        "  AND d.decision = 'passed')").fetchall()
    assert orphans == []
    # 判定记录同时保存 rule_id 与 rule_version（可追溯）
    row = pool.conn.execute(
        "SELECT rule_id, rule_version FROM validation_decisions"
        " WHERE candidate_id = ? AND decision = 'passed'",
        (cid2,)).fetchone()
    assert row["rule_id"] == rid
    assert row["rule_version"] == "v1"


# ---------------------------------------------------------------------------
# D：隔离与快照自洽
# ---------------------------------------------------------------------------

def test_d_default_db_isolated_from_production():
    assert DEFAULT_DB_PATH.endswith("market_first/market_first.db")
    assert "paper-trading-sim" not in DEFAULT_DB_PATH


def test_d_snapshot_tool_manifest_self_consistent(tmp_path):
    from market_first.snapshot_tool import diff_manifests, snapshot
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'a')")
    conn.commit()
    conn.close()
    m1 = snapshot(str(src), str(tmp_path / "s1"), label="t1")
    m2 = snapshot(str(src), str(tmp_path / "s2"), label="t2")
    assert m1["file_sha256"] == m2["file_sha256"]
    assert m1["tables"]["t"]["row_count"] == 1
    d = diff_manifests(m1, m2)
    assert d["file_changed"] is False
    assert d["changed_tables"] == []
