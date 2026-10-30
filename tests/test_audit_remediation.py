# -*- coding: utf-8 -*-
"""专项整改测试（2026-10-31 任务书：机构影子 + 交易安全机制）。

纪律：全部测试只用 tmp_path 独立测试库，绝不指向生产库
db/sim.db；不依赖本地路径、私有密钥或网络。

P1-01 _consec 交易日历差集校验（跨未登记交易日不得拼接）；
P1-02 transition 函数自包含事务原子性（autocommit 故障注入 +
      多进程并发实测）；
P1-03 expire_queued_buys 迁移日志必须记录真实 from_state。

运行（仓库根目录）：
  .venv/bin/python -m pytest tests/test_audit_remediation.py -q
"""
import multiprocessing as mp
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine import data_health, inst_lhb, safety  # noqa: E402


# ================================================================ P1-01

def _inst_con(tmp_path, calendar=None, name="i.db"):
    """calendar=None → 不建 market_data（交易日历缺失场景）；
    calendar=[日期...] → 以 market_data 的 A 股日期为交易日历。"""
    con = sqlite3.connect(str(tmp_path / name))
    if calendar is not None:
        con.execute("CREATE TABLE market_data(symbol TEXT, date TEXT,"
                    " open REAL, high REAL, low REAL, close REAL,"
                    " volume REAL, market TEXT)")
        for d in calendar:
            con.execute("INSERT INTO market_data VALUES"
                        "('002446',?,1,1,1,1,1,'a')", (d,))
        con.commit()
    return con


def _buy_row(net=100):
    return {"symbol": "002446", "name": "盛路通信", "inst_buy": net,
            "inst_sell": 0, "inst_net": net}


def test_consec_normal_three_days_with_calendar(tmp_path):
    days = ["2026-10-27", "2026-10-28", "2026-10-29"]
    con = _inst_con(tmp_path, calendar=days)
    for d in days:
        inst_lhb.store_daily(con, d, [_buy_row()])
    det = inst_lhb.consecutive_detail(con, "002446")
    assert det["net_buy_days"] == 3 and det["appear_days"] == 3
    assert det["net_buy_certain"] is True and det["appear_certain"] is True
    assert det["missing_dates"] == [] and det["calendar_ok"] is True


def test_consec_failed_middle_truncates(tmp_path):
    days = ["2026-10-27", "2026-10-28", "2026-10-29"]
    con = _inst_con(tmp_path, calendar=days)
    inst_lhb.store_daily(con, "2026-10-27", [_buy_row()])
    inst_lhb.record_fetch_failure(con, "2026-10-28", "接口超时")
    inst_lhb.store_daily(con, "2026-10-29", [_buy_row()])
    det = inst_lhb.consecutive_detail(con, "002446")
    assert det["net_buy_days"] == 1 and det["net_buy_certain"] is False


def test_consec_unregistered_middle_must_not_splice(tmp_path):
    # 2026-10-28 是交易日（在日历内）但完全没有抓取登记：
    # 旧算法直接跳过它把 27/29 拼成 2 天且标确定——必须被堵住。
    days = ["2026-10-27", "2026-10-28", "2026-10-29"]
    con = _inst_con(tmp_path, calendar=days)
    inst_lhb.store_daily(con, "2026-10-27", [_buy_row()])
    inst_lhb.store_daily(con, "2026-10-29", [_buy_row()])
    det = inst_lhb.consecutive_detail(con, "002446")
    assert det["net_buy_days"] == 1
    assert det["net_buy_certain"] is False
    assert det["missing_dates"] == ["2026-10-28"]


def test_consec_weekend_does_not_break_streak(tmp_path):
    # 周五 2026-10-23 与周一 2026-10-26 之间是周末（非交易日，
    # 不在日历内），不得影响交易日连续性。
    days = ["2026-10-23", "2026-10-26"]
    con = _inst_con(tmp_path, calendar=days)
    for d in days:
        inst_lhb.store_daily(con, d, [_buy_row()])
    det = inst_lhb.consecutive_detail(con, "002446")
    assert det["net_buy_days"] == 2 and det["net_buy_certain"] is True


def test_consec_calendar_missing_not_certain(tmp_path):
    con = _inst_con(tmp_path, calendar=None)  # 无 market_data 表
    for d in ("2026-10-27", "2026-10-28", "2026-10-29"):
        inst_lhb.store_daily(con, d, [_buy_row()])
    det = inst_lhb.consecutive_detail(con, "002446")
    assert det["calendar_ok"] is False
    assert det["net_buy_certain"] is False and det["appear_certain"] is False


def test_consec_calendar_stale_not_certain(tmp_path):
    # 日历只到 10-28（过期/不可信），抓取登记却有 10-29：
    # 日历无法覆盖校验区间，不得给出确定结果。
    con = _inst_con(tmp_path, calendar=["2026-10-27", "2026-10-28"])
    for d in ("2026-10-27", "2026-10-28", "2026-10-29"):
        inst_lhb.store_daily(con, d, [_buy_row()])
    det = inst_lhb.consecutive_detail(con, "002446")
    assert det["calendar_ok"] is False
    assert det["net_buy_certain"] is False


def test_consec_recovers_only_from_reconfirmed_segment(tmp_path):
    days = ["2026-10-26", "2026-10-27", "2026-10-28",
            "2026-10-29", "2026-10-30"]
    con = _inst_con(tmp_path, calendar=days)
    for d in ("2026-10-26", "2026-10-27", "2026-10-29", "2026-10-30"):
        inst_lhb.store_daily(con, d, [_buy_row()])  # 10-28 未登记
    det = inst_lhb.consecutive_detail(con, "002446")
    # 只能从缺口之后重新确认的区间计数（29、30 两天），不拼成 4 天
    assert det["net_buy_days"] == 2 and det["net_buy_certain"] is False
    assert det["missing_dates"] == ["2026-10-28"]


def test_consec_historical_gap_not_spliced(tmp_path):
    days = ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27"]
    con = _inst_con(tmp_path, calendar=days)
    for d in ("2026-10-22", "2026-10-26", "2026-10-27"):
        inst_lhb.store_daily(con, d, [_buy_row()])  # 10-23 未登记
    det = inst_lhb.consecutive_detail(con, "002446")
    assert det["net_buy_days"] == 2 and det["net_buy_certain"] is False
    assert det["missing_dates"] == ["2026-10-23"]


# ================================================================ P1-02

def _safety_con(tmp_path, name="s.db", autocommit=False):
    kw = {"isolation_level": None} if autocommit else {}
    con = sqlite3.connect(str(tmp_path / name), **kw)
    safety.ensure_safety_tables(con)
    return con


def _submit(con, **kw):
    d = dict(account="sim_a", market="a", symbol="002446", side="BUY",
             qty=100, price=7.0, strategy_version="v1.1",
             signal_event_id="2026-10-29|sig1")
    d.update(kw)
    return safety.submit_intent(con, **d)


def _transitions(con, key):
    return con.execute(
        "SELECT from_state, to_state FROM intent_transitions"
        " WHERE idempotency_key=? ORDER BY id", (key,)).fetchall()


def _assert_consistent(con, key):
    """主表状态必须等于最后一条迁移记录的 to_state。"""
    state = safety.get_intent(con, key)["state"]
    trs = _transitions(con, key)
    assert trs and trs[-1][1] == state


def test_transition_autocommit_normal_persists_both(tmp_path):
    con = _safety_con(tmp_path, autocommit=True)
    row, _ = _submit(con)
    key = row["idempotency_key"]
    safety.transition(con, key, safety.RISK_CHECK)
    con.close()  # autocommit 连接不做任何调用方 commit 也必须落盘
    con2 = sqlite3.connect(str(tmp_path / "s.db"))
    assert safety.get_intent(con2, key)["state"] == safety.RISK_CHECK
    _assert_consistent(con2, key)


def test_transition_autocommit_insert_fault_rolls_back(tmp_path):
    con = _safety_con(tmp_path, autocommit=True)
    row, _ = _submit(con)
    key = row["idempotency_key"]
    n0 = len(_transitions(con, key))
    con.execute(
        "CREATE TRIGGER fail_ins BEFORE INSERT ON intent_transitions"
        " WHEN NEW.to_state='risk_check'"
        " BEGIN SELECT RAISE(ABORT,'injected insert failure'); END")
    with pytest.raises(Exception):
        safety.transition(con, key, safety.RISK_CHECK)
    # 故障后：主表不得前进，迁移日志不得新增（同一连接立即核验，
    # 不依赖调用方 rollback）
    assert safety.get_intent(con, key)["state"] == safety.PENDING_REVIEW
    assert len(_transitions(con, key)) == n0
    con.execute("DROP TRIGGER fail_ins")
    safety.transition(con, key, safety.RISK_CHECK)  # 故障消除后可正常迁移
    _assert_consistent(con, key)


def test_transition_default_conn_insert_fault_rolls_back(tmp_path):
    con = _safety_con(tmp_path)  # 默认隔离级别（调用方事务模式）
    row, _ = _submit(con)
    key = row["idempotency_key"]
    con.commit()
    n0 = len(_transitions(con, key))
    con.execute(
        "CREATE TRIGGER fail_ins BEFORE INSERT ON intent_transitions"
        " WHEN NEW.to_state='risk_check'"
        " BEGIN SELECT RAISE(ABORT,'injected insert failure'); END")
    con.commit()
    with pytest.raises(Exception):
        safety.transition(con, key, safety.RISK_CHECK)
    # 不做调用方 rollback 也必须一致：函数自身已回滚其原子单元
    assert safety.get_intent(con, key)["state"] == safety.PENDING_REVIEW
    assert len(_transitions(con, key)) == n0


def test_transition_update_fault_writes_no_log(tmp_path):
    con = _safety_con(tmp_path, autocommit=True)
    row, _ = _submit(con)
    key = row["idempotency_key"]
    n0 = len(_transitions(con, key))
    con.execute(
        "CREATE TRIGGER fail_upd BEFORE UPDATE ON trade_intents"
        " BEGIN SELECT RAISE(ABORT,'injected update failure'); END")
    with pytest.raises(Exception):
        safety.transition(con, key, safety.RISK_CHECK)
    assert safety.get_intent(con, key)["state"] == safety.PENDING_REVIEW
    assert len(_transitions(con, key)) == n0


def test_illegal_transition_changes_nothing(tmp_path):
    con = _safety_con(tmp_path, autocommit=True)
    row, _ = _submit(con)
    key = row["idempotency_key"]
    n0 = len(_transitions(con, key))
    with pytest.raises(safety.IntentStateError):
        safety.transition(con, key, safety.CONFIRMED)
    assert safety.get_intent(con, key)["state"] == safety.PENDING_REVIEW
    assert len(_transitions(con, key)) == n0


def test_duplicate_submit_single_creation_record(tmp_path):
    con = _safety_con(tmp_path, autocommit=True)
    row, _ = _submit(con)
    key = row["idempotency_key"]
    for _ in range(3):
        _again, created = _submit(con)
        assert created is False
    assert con.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0] == 1
    assert len(_transitions(con, key)) == 1  # 只有创建记录


def _mp_transition_worker(db_path, key, queue):
    try:
        c = sqlite3.connect(db_path, timeout=30)
        try:
            safety.transition(c, key, safety.RELEASED)
            if not c.in_transaction:
                pass
            else:
                c.commit()
            queue.put("ok")
        except Exception as e:  # noqa: BLE001
            try:
                c.rollback()
            except Exception:  # noqa: BLE001
                pass
            queue.put(type(e).__name__)
        finally:
            c.close()
    except Exception as e:  # noqa: BLE001
        queue.put("connect:" + type(e).__name__)


def test_transition_multiprocess_single_winner(tmp_path):
    db = str(tmp_path / "mp.db")
    con = sqlite3.connect(db)
    safety.ensure_safety_tables(con)
    row, _ = _submit(con)
    key = row["idempotency_key"]
    safety.transition(con, key, safety.RISK_CHECK)
    con.commit()
    con.close()
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    ps = [ctx.Process(target=_mp_transition_worker, args=(db, key, q))
          for _ in range(2)]
    for p in ps:
        p.start()
    results = sorted(q.get(timeout=60) for _ in ps)
    for p in ps:
        p.join(30)
    assert results.count("ok") == 1, results  # 只有合法一方成功
    con = sqlite3.connect(db)
    assert safety.get_intent(con, key)["state"] == safety.RELEASED
    step = [t for t in _transitions(con, key)
            if t == (safety.RISK_CHECK, safety.RELEASED)]
    assert len(step) == 1  # 该步迁移只落一条记录
    _assert_consistent(con, key)


def test_consistency_after_mixed_ops_with_fault(tmp_path):
    con = _safety_con(tmp_path, autocommit=True)
    r1, _ = _submit(con)
    r2, _ = _submit(con, signal_event_id="2026-10-29|sig2")
    k1, k2 = r1["idempotency_key"], r2["idempotency_key"]
    safety.transition(con, k1, safety.RISK_CHECK)
    con.execute(
        "CREATE TRIGGER fail_rel BEFORE INSERT ON intent_transitions"
        " WHEN NEW.to_state='released'"
        " BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(Exception):
        safety.transition(con, k1, safety.RELEASED)
    safety.transition(con, k2, safety.RISK_CHECK)
    _assert_consistent(con, k1)
    _assert_consistent(con, k2)
    assert safety.get_intent(con, k1)["state"] == safety.RISK_CHECK


# ============================================================ P2-05/07

def _load_hash_tool():
    import importlib.util
    p = os.path.join(os.path.dirname(__file__), "..",
                     "tools", "hash_baseline.py")
    spec = importlib.util.spec_from_file_location("hash_baseline", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hash_spec_v1_known_vector(tmp_path):
    # 固定测试向量（期望值由规范文本独立计算并冻结）：
    # 行 ("x",1.5,None)、("y",2.0,"z") 按主键 a 升序序列化为
    # "x|1.5|\ny|2.0|z\n"，sha256 见下。
    hb = _load_hash_tool()
    con = sqlite3.connect(str(tmp_path / "h.db"))
    con.execute("CREATE TABLE t(a TEXT PRIMARY KEY, b REAL, c TEXT)")
    con.execute("INSERT INTO t VALUES('y', 2.0, 'z')")
    con.execute("INSERT INTO t VALUES('x', 1.5, NULL)")
    n, h = hb.table_hash(con, "t", "a")
    assert n == 2
    assert h == "971491c9637d9336c949609acc616146419767d3" \
                "6874c6c3827175c5bf564dd6"
    # 空表哈希 = sha256("") 已知常量
    con.execute("CREATE TABLE e(a TEXT PRIMARY KEY)")
    n, h = hb.table_hash(con, "e", "a")
    assert n == 0
    assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991" \
                "b7852b855"


_FORBIDDEN = ("signals", "approvals", "fills",
              "trade_intents", "intent_transitions")
_ALLOWED_WRITES = {"inst_lhb_daily", "inst_lhb_raw",
                   "inst_lhb_fetch_days"}


def _fake_inst_get(url):
    if "DAILYDETAILSBUY" in url:
        return {"result": {"pages": 1, "data": [
            {"OPERATEDEPT_NAME": "机构专用", "SECURITY_CODE": "002446",
             "BUY": 5000, "SELL": 0, "NET": 5000, "TRADE_ID": "T1",
             "EXPLANATION": "日涨幅偏离值达到7%的前5只证券"}]}}
    if "DAILYDETAILSSELL" in url:
        return {"result": {"pages": 1, "data": []}}
    return {"result": {"pages": 1, "data": [
        {"SECURITY_CODE": "002446", "SECURITY_NAME_ABBR": "盛路通信"}]}}


def test_inst_runtime_writes_only_shadow_tables(tmp_path):
    # P2-05：运行时 SQL 追踪（测试环境）——机构模块运行全程
    # 不得对五张核心账本表发生任何 INSERT/UPDATE/DELETE，
    # 且其指纹（hash-spec-v1）运行前后不变。
    import re as _re
    hb = _load_hash_tool()
    con = sqlite3.connect(str(tmp_path / "iso.db"))
    for t in _FORBIDDEN:
        con.execute("CREATE TABLE %s(id INTEGER PRIMARY KEY, v TEXT)" % t)
        con.execute("INSERT INTO %s VALUES(1, 'sentinel')" % t)
    con.execute("CREATE TABLE market_data(symbol TEXT, date TEXT,"
                " open REAL, high REAL, low REAL, close REAL,"
                " volume REAL, market TEXT)")
    con.execute("INSERT INTO market_data VALUES"
                "('002446','2026-10-29',1,1,1,1,1,'a')")
    con.commit()
    before = {t: hb.table_hash(con, t, "rowid") for t in _FORBIDDEN}
    traced = []
    con.set_trace_callback(traced.append)
    state, _reason = inst_lhb.ingest_day(con, "2026-10-29",
                                         get=_fake_inst_get)
    inst_lhb.consecutive_detail(con, "002446")
    inst_lhb.shadow_board(con, min_net_buy_days=1)
    con.set_trace_callback(None)
    assert state == "normal"
    write_res = [
        _re.compile(r"^\s*(?:INSERT|REPLACE)\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)",
                    _re.IGNORECASE),
        _re.compile(r"^\s*UPDATE\s+(?:OR\s+\w+\s+)?(\w+)", _re.IGNORECASE),
        _re.compile(r"^\s*DELETE\s+FROM\s+(\w+)", _re.IGNORECASE),
    ]
    targets = set()
    for stmt in traced:
        for write_re in write_res:
            m = write_re.match(stmt or "")
            if m:
                targets.add(m.group(1).lower())
                break
    assert not (targets & set(_FORBIDDEN)), targets
    assert targets <= _ALLOWED_WRITES, targets
    after = {t: hb.table_hash(con, t, "rowid") for t in _FORBIDDEN}
    assert before == after


def test_sell_path_independent_of_buy_freeze(tmp_path):
    # P2-05 附带：BUY 冻结（断连）语义不得阻断 SELL 路径——
    # 安全层 SELL 意图在 buy_blocked=True 下仍可完整走通
    # 状态机并确认，expire 也不得触碰 SELL（已由 P1-03 覆盖）。
    con = _safety_con(tmp_path, autocommit=True)
    assert data_health.buy_blocked(data_health.DISCONNECTED) is True
    s, _ = _submit(con, side="SELL", signal_event_id="iso|sell")
    key = s["idempotency_key"]
    _drive(con, key, safety.RISK_CHECK, safety.RELEASED,
           safety.SUBMITTING, safety.CONFIRMED)
    assert safety.get_intent(con, key)["state"] == safety.CONFIRMED


# ================================================================ P1-03

def _drive(con, key, *states):
    for s in states:
        safety.transition(con, key, s)


def _expire_records(con, key):
    return [(f, t) for f, t in _transitions(con, key)
            if t == safety.REJECTED]


def test_expire_records_real_from_states(tmp_path):
    con = _safety_con(tmp_path, autocommit=True)
    keys = {}
    r, _ = _submit(con, signal_event_id="e|p")
    keys[safety.PENDING_REVIEW] = r["idempotency_key"]
    r, _ = _submit(con, signal_event_id="e|r")
    _drive(con, r["idempotency_key"], safety.RISK_CHECK)
    keys[safety.RISK_CHECK] = r["idempotency_key"]
    r, _ = _submit(con, signal_event_id="e|rel")
    _drive(con, r["idempotency_key"], safety.RISK_CHECK, safety.RELEASED)
    keys[safety.RELEASED] = r["idempotency_key"]
    n = data_health.expire_queued_buys(con, reason="断连积压作废")
    assert n == 3
    for state, key in keys.items():
        recs = _expire_records(con, key)
        # from_state 必须是真实来源状态，不得是字面量 "queued"
        assert recs == [(state, safety.REJECTED)], (state, recs)
        assert safety.get_intent(con, key)["state"] == safety.REJECTED
        _assert_consistent(con, key)


def test_expire_terminal_and_repeat_untouched(tmp_path):
    con = _safety_con(tmp_path, autocommit=True)
    r, _ = _submit(con, signal_event_id="e|done")
    key = r["idempotency_key"]
    _drive(con, key, safety.RISK_CHECK, safety.RELEASED,
           safety.SUBMITTING, safety.CONFIRMED)
    n_tr0 = len(_transitions(con, key))
    assert data_health.expire_queued_buys(con) == 0
    assert safety.get_intent(con, key)["state"] == safety.CONFIRMED
    assert len(_transitions(con, key)) == n_tr0
    # 已作废的不重复处理
    r2, _ = _submit(con, signal_event_id="e|once")
    k2 = r2["idempotency_key"]
    assert data_health.expire_queued_buys(con) == 1
    assert data_health.expire_queued_buys(con) == 0
    assert len(_expire_records(con, k2)) == 1


def test_expire_sell_untouched(tmp_path):
    con = _safety_con(tmp_path, autocommit=True)
    s, _ = _submit(con, side="SELL", signal_event_id="e|sell")
    b, _ = _submit(con, side="BUY", signal_event_id="e|buy")
    assert data_health.expire_queued_buys(con) == 1
    assert safety.get_intent(con, s["idempotency_key"])["state"] \
        == safety.PENDING_REVIEW
    assert _expire_records(con, s["idempotency_key"]) == []


def test_expire_skips_submitting_no_false_record(tmp_path):
    # 状态已被并发推进到 submitting（不在可作废集合）时：
    # 不得作废、不得留下错误来源记录。
    con = _safety_con(tmp_path, autocommit=True)
    r, _ = _submit(con, signal_event_id="e|sub")
    key = r["idempotency_key"]
    _drive(con, key, safety.RISK_CHECK, safety.RELEASED,
           safety.SUBMITTING)
    assert data_health.expire_queued_buys(con) == 0
    assert safety.get_intent(con, key)["state"] == safety.SUBMITTING
    assert _expire_records(con, key) == []


def test_expire_fault_rolls_back(tmp_path):
    con = _safety_con(tmp_path, autocommit=True)
    r, _ = _submit(con, signal_event_id="e|fault")
    key = r["idempotency_key"]
    _drive(con, key, safety.RISK_CHECK)
    n0 = len(_transitions(con, key))
    con.execute(
        "CREATE TRIGGER fail_rej BEFORE INSERT ON intent_transitions"
        " WHEN NEW.to_state='rejected'"
        " BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(Exception):
        data_health.expire_queued_buys(con)
    assert safety.get_intent(con, key)["state"] == safety.RISK_CHECK
    assert len(_transitions(con, key)) == n0
