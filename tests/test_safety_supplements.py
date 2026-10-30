#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""四项强制安全补充（2026-10-30）验收测试。

① 幂等/状态机 ② 拦截日志链 ③ 数据健康降级 ④ 配置隔离
+ 机构影子模块（口径=龙虎榜机构专用席位）。
"""
import json
import os
import re
import sqlite3
import threading

import pytest

from engine import data_health, inst_lhb, safety

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _con(tmp_path, name="t.db"):
    con = sqlite3.connect(str(tmp_path / name))
    safety.ensure_safety_tables(con)
    return con


def _submit(con, **kw):
    d = dict(account="sim_a", market="a", symbol="002446", side="BUY",
             qty=100, price=7.0, strategy_version="v1.1",
             signal_event_id="2026-10-29|sig1")
    d.update(kw)
    return safety.submit_intent(con, **d)


def _drive_to_released(con, key):
    safety.transition(con, key, safety.RISK_CHECK)
    safety.transition(con, key, safety.RELEASED)


# ------------------------------------------------------------------ ①

def test_same_intent_10x_creates_one(tmp_path):
    con = _con(tmp_path)
    rows = [_submit(con) for _ in range(10)]
    assert [c for _r, c in rows].count(True) == 1
    assert len({r["idempotency_key"] for r, _c in rows}) == 1
    assert con.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0] == 1


def test_distinct_signal_event_not_deduped(tmp_path):
    con = _con(tmp_path)
    _r1, c1 = _submit(con)
    _r2, c2 = _submit(con, signal_event_id="2026-10-29|sig2")
    assert c1 and c2
    assert con.execute("SELECT COUNT(*) FROM trade_intents").fetchone()[0] == 2


def test_full_flow_and_confirmed_resubmit_returns_original(tmp_path):
    con = _con(tmp_path)
    row, _ = _submit(con)
    key = row["idempotency_key"]
    _drive_to_released(con, key)
    safety.begin_submit(con, key)
    done = safety.transition(con, key, safety.CONFIRMED,
                             result_ref="fill#1")
    assert done["state"] == safety.CONFIRMED
    n_trans = con.execute(
        "SELECT COUNT(*) FROM intent_transitions").fetchone()[0]
    # 重复提交/重复请求确认：返回原结果，不产生新订单/新迁移
    again, created = _submit(con)
    assert not created and again["result_ref"] == "fill#1"
    assert safety.begin_submit(con, key)["state"] == safety.CONFIRMED
    assert con.execute(
        "SELECT COUNT(*) FROM intent_transitions").fetchone()[0] == n_trans


def test_illegal_transition_rejected(tmp_path):
    con = _con(tmp_path)
    row, _ = _submit(con)
    with pytest.raises(safety.IntentStateError):
        safety.transition(con, row["idempotency_key"], safety.CONFIRMED)


def test_concurrent_transition_only_one_wins(tmp_path):
    db = str(tmp_path / "cc.db")
    con0 = sqlite3.connect(db)
    safety.ensure_safety_tables(con0)
    row, _ = _submit(con0)
    key = row["idempotency_key"]
    safety.transition(con0, key, safety.RISK_CHECK)
    con0.commit()
    con0.close()
    results = []

    def worker():
        c = sqlite3.connect(db, timeout=10)
        try:
            safety.transition(c, key, safety.RELEASED)
            c.commit()
            results.append("ok")
        except Exception as e:  # noqa: BLE001
            results.append(type(e).__name__)
        finally:
            c.close()

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 1
    con = sqlite3.connect(db)
    assert safety.get_intent(con, key)["state"] == safety.RELEASED


def test_unknown_requires_reconcile_before_resubmit(tmp_path):
    con = _con(tmp_path)
    row, _ = _submit(con)
    key = row["idempotency_key"]
    _drive_to_released(con, key)
    safety.begin_submit(con, key)
    safety.mark_unknown(con, key)  # 模拟提交超时
    with pytest.raises(safety.IntentStateError):
        safety.begin_submit(con, key)  # 禁止盲目重发
    # 对账：下游未收到 → 回 released，才可再提交
    back = safety.reconcile_unknown(con, key, found_downstream=False)
    assert back["state"] == safety.RELEASED
    safety.begin_submit(con, key)
    safety.mark_unknown(con, key)
    # 对账：下游已成交 → confirmed
    done = safety.reconcile_unknown(con, key, found_downstream=True,
                                    result_ref="fill#9")
    assert done["state"] == safety.CONFIRMED and done["result_ref"] == "fill#9"


def test_sell_intent_not_subject_to_buy_env_gate(tmp_path):
    # SELL 意图全流程可走通；买入环境闸只在 BUY 判定（risk_gate 口径），
    # 安全层状态机不对 SELL 设环境闸分支——此处验证 SELL 可正常放行。
    con = _con(tmp_path)
    row, created = _submit(con, side="SELL", symbol="002446")
    assert created
    _drive_to_released(con, row["idempotency_key"])
    assert safety.get_intent(con, row["idempotency_key"])["state"] == safety.RELEASED


# ------------------------------------------------------------------ ②

def _log(con, **kw):
    d = dict(trade_date="2026-10-29", account="sim_a", symbol="002446",
             side="BUY", source="pipeline", strategy_version="v1.1",
             screen_passed=True, screen_score=64.0,
             rule_name="env_gate", reason="市场环境闸·禁止新开仓：B31=否",
             b31_state="否", index_close=3681.47, index_ma20=3764.55,
             data_ts="2026-10-29")
    d.update(kw)
    return safety.log_block_event(con, **d)


@pytest.fixture()
def anchor_tmp(tmp_path, monkeypatch):
    p = tmp_path / "head.json"
    cp = tmp_path / "checkpoint.json"
    monkeypatch.setattr(safety, "chain_head_path", lambda *a: str(p))
    monkeypatch.setattr(safety, "checkpoint_path", lambda *a: str(cp))
    return p


def test_block_events_persist_and_query(tmp_path, anchor_tmp):
    db = str(tmp_path / "b.db")
    con = sqlite3.connect(db)
    _log(con)
    _log(con, symbol="688305", rule_name="ban_entry_stop_ref",
         reason="跌破母板止损参考：688305 判定价 44.02 < stop_ref 52.50")
    con.commit()
    con.close()
    con2 = sqlite3.connect(db)  # 重启后仍可查
    rows = safety.query_block_events(con2, trade_date="2026-10-29")
    assert len(rows) == 2
    assert safety.query_block_events(con2, symbol="688305")[0]["rule_name"] \
        == "ban_entry_stop_ref"
    assert safety.query_block_events(
        con2, strategy_version="v1.1")[0]["index_ma20"] == 3764.55
    ok, _detail = safety.verify_block_chain(con2)
    assert ok


def test_block_chain_detects_tamper_and_delete(tmp_path, anchor_tmp):
    con = _con(tmp_path)
    _log(con)
    _log(con, symbol="600150")
    _log(con, symbol="002185")
    con.commit()
    ok, _ = safety.verify_block_chain(con)
    assert ok
    # 篡改中间一条的 reason
    con.execute("UPDATE risk_block_events SET reason='已改'"
                " WHERE symbol='600150'")
    ok, detail = safety.verify_block_chain(con)
    assert not ok and "篡改" in detail
    con.rollback()
    # 删除中间一条 → 链断裂
    con.execute("DELETE FROM risk_block_events WHERE symbol='600150'")
    ok, detail = safety.verify_block_chain(con)
    assert not ok


def test_block_chain_tail_delete_caught_by_anchor(tmp_path, anchor_tmp):
    con = _con(tmp_path)
    _log(con)
    _log(con, symbol="600150")
    ok, _ = safety.verify_block_chain(con)
    assert ok
    con.execute("DELETE FROM risk_block_events WHERE symbol='600150'")
    ok, detail = safety.verify_block_chain(con)
    assert not ok and "锚点" in detail


def test_block_chain_full_rewrite_caught_by_checkpoint(tmp_path, anchor_tmp):
    # 攻击者重写全部记录并重算一条自洽的假链：链内校验会通过，
    # 但链头与仓内锚点/仓外检查点不一致，必须被检出。
    import hashlib as _hl
    con = _con(tmp_path)
    _log(con)
    _log(con, symbol="600150")
    ok, _ = safety.verify_block_chain(con)
    assert ok
    rows = con.execute(
        "SELECT * FROM risk_block_events ORDER BY rowid").fetchall()
    cols = [d[0] for d in con.execute(
        "SELECT * FROM risk_block_events LIMIT 0").description]
    prev = safety.GENESIS
    for r in rows:
        f = dict(zip(cols, r))
        f["reason"] = "被改写的假理由"
        body = {k: f[k] for k in safety._CHAIN_COLS}
        h = _hl.sha256((prev + safety._canon(body)).encode()).hexdigest()
        con.execute("UPDATE risk_block_events SET reason=?, prev_hash=?,"
                    " row_hash=? WHERE event_id=?",
                    (f["reason"], prev, h, f["event_id"]))
        prev = h
    ok_internal, _ = safety.verify_block_chain(con, check_anchor=False)
    assert ok_internal  # 假链自洽——这正是必须有独立检查点的原因
    ok, detail = safety.verify_block_chain(con)
    assert not ok and "不符" in detail


def test_anchors_are_per_database(tmp_path):
    # 回归（2026-10-30 部署后复核发现）：锚点/检查点按库隔离，
    # 测试库的链头不得覆盖另一库的锚点。
    con_a = _con(tmp_path, "a.db")
    con_b = _con(tmp_path, "b.db")
    _log(con_a)
    _log(con_b, symbol="600150")
    _log(con_b, symbol="002185")
    assert safety.verify_block_chain(con_a)[0]
    assert safety.verify_block_chain(con_b)[0]
    assert safety.chain_head_path(safety._db_key(con_a)) != \
        safety.chain_head_path(safety._db_key(con_b))
    # 锚点被覆盖后的恢复：链内自洽 → reanchor 后双锚点校验恢复
    import json as _json
    p = safety.chain_head_path(safety._db_key(con_a))
    with open(p, "w", encoding="utf-8") as f:
        _json.dump({"head_hash": "bogus", "event_id": "x"}, f)
    assert not safety.verify_block_chain(con_a)[0]
    safety.reanchor(con_a)
    assert safety.verify_block_chain(con_a)[0]


def test_block_log_write_failure_is_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "BASE", str(tmp_path))
    con = sqlite3.connect(str(tmp_path / "x.db"))
    con.execute("CREATE TABLE risk_block_events(event_id TEXT PRIMARY KEY)")
    with pytest.raises(safety.RiskBlockLogError):
        _log(con)
    fallback = tmp_path / "reports" / "risk_block_log_failures.jsonl"
    assert fallback.exists() and "002446" in fallback.read_text()


def test_mirror_blocked_signals_idempotent(tmp_path, anchor_tmp):
    con = _con(tmp_path)
    con.execute("""CREATE TABLE IF NOT EXISTS blocked_signals(
        date TEXT, signal_date TEXT, account TEXT, symbol TEXT, side TEXT,
        signal_price REAL, exec_price REAL, stop_ref REAL, reason TEXT,
        created_at TEXT, PRIMARY KEY(date, account, symbol, side))""")
    con.execute("INSERT INTO blocked_signals VALUES"
                "('2026-10-29','2026-10-28','sim_a','002446','BUY',"
                "6.93,6.93,7.70,'跌破母板止损参考：002446 判定价 6.93"
                " < stop_ref 7.70','2026-10-29 17:12:00')")
    env = {"b31": False, "auto": {"close": 3681.47, "ma20": 3764.55,
                                  "last_date": "2026-10-29"}}
    assert safety.mirror_blocked_signals(
        con, strategy_version="v1.1", env=env) == 1
    assert safety.mirror_blocked_signals(
        con, strategy_version="v1.1", env=env) == 0  # 重放不重复记
    rows = safety.query_block_events(con, symbol="002446")
    assert rows[0]["rule_name"] == "ban_entry_stop_ref"
    assert rows[0]["b31_state"] == "否"
    ok, _ = safety.verify_block_chain(con)
    assert ok


# ------------------------------------------------------------------ ③

def test_health_classify_states():
    assert data_health.classify(fetch_ok=False)[0] == data_health.DISCONNECTED
    assert data_health.classify(payload_ok=False)[0] == data_health.ANOMALOUS
    assert data_health.classify(last_bar_date=None)[0] == data_health.ANOMALOUS
    s, _r = data_health.classify(last_bar_date="2026-10-20",
                                 ref_date="2026-10-29")
    assert s == data_health.DISCONNECTED  # 过期越线按断连级冻结
    s, _r = data_health.classify(last_bar_date="2026-10-27",
                                 ref_date="2026-10-29")
    assert s == data_health.DELAYED
    s, _r = data_health.classify(last_bar_date="2026-10-29",
                                 ref_date="2026-10-29")
    assert s == data_health.NORMAL


def test_health_buy_block_and_stale_label():
    state, _reason, blocked = data_health.symbol_buy_health(
        "688305", "2026-10-20", "2026-10-29")
    assert blocked and state == data_health.DISCONNECTED
    _s, _r, blocked = data_health.symbol_buy_health(
        "688305", "2026-10-29", "2026-10-29")
    assert not blocked
    lbl = data_health.label_stale(data_health.DISCONNECTED, "2026-10-20")
    assert "2026-10-20" in lbl and "失效" in lbl


def test_recovery_requires_revalidation():
    ok, state, _ = data_health.revalidate(
        fetch_ok=True, payload_ok=True, last_bar_date="2026-10-20",
        ref_date="2026-10-29", risk_inputs_ok=True)
    assert not ok  # 旧数据未刷新不得恢复
    ok, state, _ = data_health.revalidate(
        fetch_ok=True, payload_ok=True, last_bar_date="2026-10-29",
        ref_date="2026-10-29", risk_inputs_ok=False)
    assert not ok and state == data_health.ANOMALOUS
    ok, state, _ = data_health.revalidate(
        fetch_ok=True, payload_ok=True, last_bar_date="2026-10-29",
        ref_date="2026-10-29", risk_inputs_ok=True)
    assert ok and state == data_health.NORMAL


def test_queued_buys_expire_on_recovery_sells_untouched(tmp_path):
    con = _con(tmp_path)
    b, _ = _submit(con)
    s, _ = _submit(con, side="SELL", signal_event_id="2026-10-29|sell1")
    n = data_health.expire_queued_buys(con)
    assert n == 1
    assert safety.get_intent(con, b["idempotency_key"])["state"] \
        == safety.REJECTED
    assert safety.get_intent(con, s["idempotency_key"])["state"] \
        == safety.PENDING_REVIEW


# ------------------------------------------------------------------ ④

_SECRET_RE = re.compile(
    r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}"
    r"|AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def test_gitignore_covers_env_and_example_is_clean():
    gi = open(os.path.join(BASE, ".gitignore"), encoding="utf-8").read()
    assert re.search(r"(?m)^\.env$", gi)
    ex = open(os.path.join(BASE, ".env.example"), encoding="utf-8").read()
    assert not _SECRET_RE.search(ex)
    assert "PAPER_SIM_DB" in ex and "GITHUB_TOKEN" in ex


def test_business_config_has_no_secrets():
    for rel in ("config/strategy.yaml", "config/workbook_map.json",
                "config/sector_news.json"):
        txt = open(os.path.join(BASE, rel), encoding="utf-8").read()
        assert not _SECRET_RE.search(txt), rel


def test_default_mode_is_sim_safe():
    src = open(os.path.join(BASE, "init_db.py"), encoding="utf-8").read()
    assert "paused" in src and "manual" in src  # 新库默认暂停+手动


# ------------------------------------------------------------- 机构影子

def _fake_get_factory(buy_rows, sell_rows):
    def get(url):
        if "DAILYDETAILSBUY" in url:
            return {"result": {"pages": 1, "data": buy_rows}}
        if "DAILYDETAILSSELL" in url:
            return {"result": {"pages": 1, "data": sell_rows}}
        return {"result": {"pages": 1, "data": [
            {"SECURITY_CODE": "002446", "SECURITY_NAME_ABBR": "盛路通信"}]}}
    return get


def test_inst_fetch_aggregates_only_inst_seat():
    buys = [
        {"OPERATEDEPT_NAME": "机构专用", "SECURITY_CODE": "002446",
         "BUY": 5000, "SELL": 0, "NET": 5000},
        {"OPERATEDEPT_NAME": "招商证券南京庐山路", "SECURITY_CODE": "002446",
         "BUY": 99999, "SELL": 0, "NET": 99999},
    ]
    sells = [{"OPERATEDEPT_NAME": "机构专用", "SECURITY_CODE": "002446",
              "BUY": 0, "SELL": 1200, "NET": -1200}]
    rows, raw, state, _ = inst_lhb.fetch_inst_daily(
        "2026-10-29", get=_fake_get_factory(buys, sells))
    assert state == data_health.NORMAL
    assert len(rows) == 1 and rows[0]["inst_net"] == 3800
    assert rows[0]["name"] == "盛路通信"  # 名称来自汇总报表
    assert len(raw) == 3  # 原始记录全量保留（含非机构席位）


def test_inst_fetch_failure_is_disconnected():
    def boom(url):
        raise TimeoutError("timeout")
    rows, raw, state, _ = inst_lhb.fetch_inst_daily("2026-10-29", get=boom)
    assert rows == [] and raw == [] and state == data_health.DISCONNECTED


def test_inst_consecutive_days_and_shadow_board(tmp_path):
    con = sqlite3.connect(str(tmp_path / "i.db"))
    for d, net in (("2026-10-27", 100), ("2026-10-28", 200),
                    ("2026-10-29", 300)):
        inst_lhb.store_daily(con, d, [{"symbol": "002446", "name": "盛路通信",
                                       "inst_buy": net, "inst_sell": 0,
                                       "inst_net": net}])
    assert inst_lhb.consecutive_net_buy_days(con, "002446") == 3
    assert inst_lhb.consecutive_appear_days(con, "002446") == 3
    board = inst_lhb.shadow_board(con, min_net_buy_days=2)
    assert board and board[0]["symbol"] == "002446"
    assert board[0]["consec_appear_days"] == 3
    # 净卖日：净买天数归零，出现天数继续（两口径分开）
    inst_lhb.store_daily(con, "2026-10-30", [
        {"symbol": "002446", "name": "盛路通信", "inst_buy": 0,
         "inst_sell": 50, "inst_net": -50}])
    assert inst_lhb.consecutive_net_buy_days(con, "002446") == 0
    assert inst_lhb.consecutive_appear_days(con, "002446") == 4
    # 已抓取但该股无记录的交易日 → 两个口径都中断
    inst_lhb.store_daily(con, "2026-11-02", [
        {"symbol": "600150", "name": "中国船舶", "inst_buy": 10,
         "inst_sell": 0, "inst_net": 10}])
    assert inst_lhb.consecutive_appear_days(con, "002446") == 0


def test_inst_failed_day_is_not_absence(tmp_path):
    con = sqlite3.connect(str(tmp_path / "f.db"))
    row = {"symbol": "002446", "name": "盛路通信", "inst_buy": 100,
           "inst_sell": 0, "inst_net": 100}
    inst_lhb.store_daily(con, "2026-10-27", [row])
    inst_lhb.record_fetch_failure(con, "2026-10-28")  # 中间日抓取失败
    inst_lhb.store_daily(con, "2026-10-29", [row])
    det = inst_lhb.consecutive_detail(con, "002446")
    # 只数到失败日为止（1 天），不跨越失败日拼成 2 天不断言缺席
    assert det["net_buy_days"] == 1 and det["net_buy_certain"] is False
    # 最新日失败：以最近成功日为口径起点，此前连续不被清零
    inst_lhb.record_fetch_failure(con, "2026-10-30")
    det = inst_lhb.consecutive_detail(con, "002446")
    assert det["net_buy_days"] == 1 and det["net_buy_certain"] is False
    # ingest_day 失败路径：只登记失败日，不落当日数据
    def boom(url):
        raise TimeoutError("x")
    state, _ = inst_lhb.ingest_day(con, "2026-11-02", get=boom)
    assert state == data_health.DISCONNECTED
    assert con.execute("SELECT state FROM inst_lhb_fetch_days"
                       " WHERE date='2026-11-02'").fetchone()[0] == "failed"
    assert con.execute("SELECT COUNT(*) FROM inst_lhb_daily"
                       " WHERE date='2026-11-02'").fetchone()[0] == 0
