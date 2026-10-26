# -*- coding: utf-8 -*-
"""
发布门禁（release_gate.py + sync_8502_github.py 门禁接线）回归测试。

覆盖任务单七项：
(i)   基线状态 → 全量门禁通过（桩 runner + 真实检查指纹/账本）
(ii)  篡改一条历史 fills 行 → 指纹校验拒绝
(iii) 删除一行（笔数回退）→ 拒绝
(iv)  合法新增一笔成交且 daily_report 有对应日期 → 通过且水位线推进
(v)   新增成交但无日报佐证 → 拒绝
(vi)  pytest 人为失败 → 门禁拒绝且 sync 主流程零推送
(vii) --force-bypass → 放行推送但审计日志落痕、水位线不推进
另含：门禁拒绝专属日志（只追加、字段齐全）的回归断言。

**本文件是常驻回归测试**（部署在仓库 tests/ 内，门禁自身的 pytest 步骤
会跑到它）；(ii)(iii)(v) 三类破坏性场景 + (iv) 正对照为验收定版口径，
后续任何门禁改动都必须重跑本文件并保持全过。

全部跑在临时 fixture DB / 桩上，不碰生产远端。
"""
import importlib.util
import json
import os
import sqlite3
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import release_gate as gate  # noqa: E402

SYNC_PATH = os.path.join(ROOT, "sync_8502_github.py")

FILLS_SCHEMA = """
CREATE TABLE fills (
    id INTEGER PRIMARY KEY, date TEXT, account TEXT, symbol TEXT,
    side TEXT, qty REAL, price REAL, fee REAL, pnl REAL,
    signal_date TEXT, strategy_version TEXT);
CREATE TABLE daily_report (
    date TEXT, account TEXT, symbol TEXT, nav REAL, cash REAL,
    position_value REAL, pnl_day REAL, ret_total REAL);
CREATE TABLE positions (account TEXT, symbol TEXT, qty REAL);
"""

BASE_FILLS = [
    # id, date, account, symbol, side, qty, price, fee, pnl
    (1, "2026-10-20", "sim_a", "600150", "BUY", 100.0, 10.0, 1.0, 0.0),
    (2, "2026-10-20", "sim_a", "002472", "BUY", 200.0, 20.0, 1.0, 0.0),
    (3, "2026-10-21", "sim_a", "600150", "SELL", 100.0, 11.0, 1.0, 100.0),
    (5, "2026-10-21", "sim_hk", "HK0354", "BUY", 1000.0, 5.0, 2.0, 0.0),
    (8, "2026-10-22", "sim_a", "600150", "BUY", 50.0, 12.0, 1.0, 0.0),
]
BASE_POSITIONS = [("sim_a", "600150", 50.0), ("sim_a", "002472", 200.0),
                  ("sim_hk", "HK0354", 1000.0)]
BASE_DR_DATES = ["2026-10-20", "2026-10-21", "2026-10-22"]


def make_db(path, fills=BASE_FILLS, positions=BASE_POSITIONS,
            dr_dates=BASE_DR_DATES):
    con = sqlite3.connect(path)
    con.executescript(FILLS_SCHEMA)
    con.executemany(
        "INSERT INTO fills (id,date,account,symbol,side,qty,price,fee,pnl,"
        "signal_date,strategy_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [f + (f[1], "v1.1-2026-10-26") for f in fills])
    con.executemany("INSERT INTO positions VALUES (?,?,?)", positions)
    con.executemany(
        "INSERT INTO daily_report (date,account,symbol) VALUES (?,'sim_a',"
        "'TOTAL')", [(d,) for d in dr_dates])
    con.commit()
    con.close()
    return str(path)


def ok_runner(src):
    return "stub ok"


@pytest.fixture()
def fixture_db(tmp_path):
    return make_db(tmp_path / "sim.db")


# ------------------------------------------------------------ (i) 基线通过
def test_baseline_gate_passes(fixture_db, tmp_path):
    state_path = str(tmp_path / gate.STATE_NAME)
    gate.init_watermark(fixture_db, state_path)
    res = gate.run_gate(
        ROOT, db_path=fixture_db, state_path=state_path,
        smoke_runner=ok_runner, pytest_runner=ok_runner)
    assert res["ok"], res
    assert [c["name"] for c in res["checks"]] == [
        "smoke", "pytest", "fingerprint", "ledger"]


def test_real_db_copy_matches_deployed_watermark(tmp_path):
    """部署后持续自检：生产库副本 + 已部署水位线 → 指纹与账本均应通过。
    水位线文件不存在（未部署）时跳过。"""
    real_db = os.path.join(ROOT, "db", "sim.db")
    state_path = os.path.join(ROOT, gate.STATE_NAME)
    if not (os.path.exists(real_db) and os.path.exists(state_path)):
        pytest.skip("水位线未部署")
    import shutil
    db_copy = str(tmp_path / "sim.db")
    shutil.copy2(real_db, db_copy)
    state = gate.load_state(state_path)
    n = gate.check_fingerprint(db_copy, state)
    assert n >= 0
    assert gate.check_ledger(db_copy, ROOT)


# ------------------------------------------------------- (ii) 篡改历史行
def test_tampered_history_row_refused(fixture_db, tmp_path):
    state_path = str(tmp_path / gate.STATE_NAME)
    state = gate.init_watermark(fixture_db, state_path)
    con = sqlite3.connect(fixture_db)
    con.execute("UPDATE fills SET price = price + 1 WHERE id = 1")
    con.commit()
    con.close()
    with pytest.raises(gate.GateRefusal) as ei:
        gate.check_fingerprint(fixture_db, state)
    assert "哈希" in str(ei.value)


# ------------------------------------------------------- (iii) 删行/回退
def test_deleted_row_refused(fixture_db, tmp_path):
    state_path = str(tmp_path / gate.STATE_NAME)
    state = gate.init_watermark(fixture_db, state_path)
    con = sqlite3.connect(fixture_db)
    con.execute("DELETE FROM fills WHERE id = 2")
    con.commit()
    con.close()
    with pytest.raises(gate.GateRefusal) as ei:
        gate.check_fingerprint(fixture_db, state)
    assert "行数" in str(ei.value)


# ------------------------------------------- (iv) 合法新增 → 通过 + 推进
def test_legal_append_passes_and_watermark_advances(fixture_db, tmp_path):
    state_path = str(tmp_path / gate.STATE_NAME)
    state = gate.init_watermark(fixture_db, state_path)
    con = sqlite3.connect(fixture_db)
    con.execute(
        "INSERT INTO fills (id,date,account,symbol,side,qty,price,fee,pnl,"
        "signal_date,strategy_version) VALUES (9,'2026-10-23','sim_a',"
        "'600150','BUY',10.0,12.5,1.0,0.0,'2026-10-23','v1.1-2026-10-26')")
    con.execute(
        "INSERT INTO daily_report (date,account,symbol) VALUES "
        "('2026-10-23','sim_a','TOTAL')")
    con.execute("UPDATE positions SET qty = 60 WHERE account='sim_a' "
                "AND symbol='600150'")
    con.commit()
    con.close()
    assert gate.check_fingerprint(fixture_db, state) == 1
    new_state = gate.advance_watermark(fixture_db, state_path)
    assert new_state["fills_count"] == state["fills_count"] + 1
    assert new_state["max_id"] == 9
    assert gate.check_fingerprint(fixture_db, new_state) == 0


# ------------------------------------------- (v) 新增无日报佐证 → 拒绝
def test_append_without_daily_report_refused(fixture_db, tmp_path):
    state_path = str(tmp_path / gate.STATE_NAME)
    state = gate.init_watermark(fixture_db, state_path)
    con = sqlite3.connect(fixture_db)
    con.execute(
        "INSERT INTO fills (id,date,account,symbol,side,qty,price,fee,pnl,"
        "signal_date,strategy_version) VALUES (9,'2026-10-24','sim_a',"
        "'600150','BUY',10.0,12.5,1.0,0.0,'2026-10-24','v1.1-2026-10-26')")
    con.commit()
    con.close()
    with pytest.raises(gate.GateRefusal) as ei:
        gate.check_fingerprint(fixture_db, state)
    assert "daily_report" in str(ei.value)


# ------------------------------------------------- (vi) pytest 失败零推送
def test_gate_stops_at_pytest_failure(fixture_db, tmp_path):
    state_path = str(tmp_path / gate.STATE_NAME)
    gate.init_watermark(fixture_db, state_path)

    def bad_pytest(src):
        raise gate.GateRefusal("pytest 未通过（exit 1）：1 failed")

    res = gate.run_gate(
        ROOT, db_path=fixture_db, state_path=state_path,
        smoke_runner=ok_runner, pytest_runner=bad_pytest)
    assert not res["ok"]
    assert [c["name"] for c in res["checks"]] == ["smoke", "pytest"]
    assert "pytest" in res["reason"]


def test_run_pytest_real_runner(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    cmd = [sys.executable, "-m", "pytest", "tests/", "-q",
           "-p", "no:cacheprovider"]
    assert "pytest 通过" in gate.run_pytest(str(tmp_path), cmd=cmd)
    (tmp_path / "tests" / "test_bad.py").write_text(
        "def test_bad():\n    assert False\n", encoding="utf-8")
    with pytest.raises(gate.GateRefusal):
        gate.run_pytest(str(tmp_path), cmd=cmd)


def _load_sync(monkeypatch, tmp_path, gate_stub):
    """以桩 gh_api / release_gate 载入 sync 脚本（不触发真实推送）。"""
    monkeypatch.setitem(sys.modules, "gh_api",
                        types.SimpleNamespace(api=lambda *a, **k: {}))
    monkeypatch.setitem(sys.modules, "release_gate", gate_stub)
    monkeypatch.setenv("SYNC8502_SRC", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "sync_8502_github_under_test", SYNC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gate_stub(res, audit_sink):
    return types.SimpleNamespace(
        STATE_NAME=gate.STATE_NAME,
        REJECT_LINE=gate.REJECT_LINE,
        BYPASS_LINE=gate.BYPASS_LINE,
        run_gate=lambda src, files=None: res,
        audit=lambda src, entry: audit_sink.append(entry),
        log_refusal=lambda src, script, check, reason, path="B",
        extra=None: audit_sink.append(
            {"event": "refusal_log", "script": script, "check": check,
             "reason": reason, "path": path, "extra": extra}),
        init_watermark=lambda db, sp: {},
        advance_watermark=lambda db, sp: audit_sink.append(
            {"event": "advance"}) or {"fills_count": 0, "max_id": 0},
    )


def test_main_refuses_and_pushes_nothing_on_pytest_failure(
        monkeypatch, tmp_path):
    res = {"ok": False,
           "reason": "pytest：pytest 未通过（exit 1）：1 failed",
           "checks": [{"name": "smoke", "ok": True, "detail": "ok"},
                      {"name": "pytest", "ok": False,
                       "detail": "pytest 未通过（exit 1）：1 failed"}]}
    audits = []
    mod = _load_sync(monkeypatch, tmp_path, _gate_stub(res, audits))
    pushed = []
    monkeypatch.setattr(mod, "push_file",
                        lambda *a, **k: pushed.append(a) or False)
    monkeypatch.setattr(mod, "_assert_fresh", lambda: True)
    monkeypatch.setattr(sys, "argv", ["sync_8502_github.py"])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 3
    assert pushed == []
    assert audits and audits[0]["event"] == "refuse"
    # 拒绝必须同时落持久拒绝日志（桩替身记录了这次调用）
    refusals = [a for a in audits if a.get("event") == "refusal_log"]
    assert len(refusals) == 1
    assert refusals[0]["script"] == "sync_8502_github.py"
    assert refusals[0]["check"] == "pytest"
    assert "pytest" in refusals[0]["reason"]


# ------------------------------------------- (vii) bypass 放行 + 留痕
def test_main_bypass_pushes_but_audits_and_keeps_watermark(
        monkeypatch, tmp_path):
    res = {"ok": False, "reason": "pytest：未通过", "checks": []}
    audits = []
    mod = _load_sync(monkeypatch, tmp_path, _gate_stub(res, audits))
    pushed = []
    monkeypatch.setattr(mod, "push_file",
                        lambda *a, **k: pushed.append(a) or False)
    monkeypatch.setattr(mod, "_assert_fresh", lambda: True)
    monkeypatch.setattr(
        sys, "argv", ["sync_8502_github.py", "--force-bypass", "测试原因"])
    mod.main()  # 不应抛 SystemExit
    assert pushed, "bypass 时应执行推送"
    assert audits[0]["event"] == "bypass"
    assert audits[0]["reason"] == "测试原因"
    assert not any(a.get("event") == "advance" for a in audits), \
        "bypass 时水位线不得推进"


def test_gate_check_only_exit_codes(monkeypatch, tmp_path):
    audits = []
    ok_res = {"ok": True, "reason": None,
              "checks": [{"name": "smoke", "ok": True, "detail": "ok"}]}
    mod = _load_sync(monkeypatch, tmp_path, _gate_stub(ok_res, audits))
    pushed = []
    monkeypatch.setattr(mod, "push_file",
                        lambda *a, **k: pushed.append(a) or False)
    monkeypatch.setattr(sys, "argv",
                        ["sync_8502_github.py", "--gate-check-only"])
    mod.main()
    assert pushed == []

    bad_res = {"ok": False, "reason": "fingerprint：哈希不一致",
               "checks": [{"name": "fingerprint", "ok": False,
                           "detail": "哈希不一致"}]}
    mod2 = _load_sync(monkeypatch, tmp_path, _gate_stub(bad_res, audits))
    monkeypatch.setattr(mod2, "push_file",
                        lambda *a, **k: pushed.append(a) or False)
    with pytest.raises(SystemExit) as ei:
        mod2.main()
    assert ei.value.code == 3
    assert pushed == []


def test_smoke_detects_syntax_error(tmp_path):
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(gate.GateRefusal) as ei:
        gate.run_smoke(str(tmp_path), ["bad.py"])
    assert "语法" in str(ei.value)


def test_audit_log_written(tmp_path):
    gate.audit(str(tmp_path), {"event": "pass"})
    log = tmp_path / "reports" / "release_gate_log.jsonl"
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[-1])["event"] == "pass"


# ------------------------------------- 拒绝专属日志：持久、只追加、字段齐全
def test_refusal_log_append_only_with_fields(tmp_path):
    src = str(tmp_path)
    gate.log_refusal(src, "sync_8502_github.py", "fingerprint",
                     "水位内历史行滚动哈希不一致（id≤466）——历史成交被篡改")
    fp = tmp_path / "reports" / "release_gate_refusals.jsonl"
    assert fp.exists()
    first = fp.read_text(encoding="utf-8")
    gate.log_refusal(src, "tools/git_sync.py", "snapshot_fills",
                     "快照 fills 笔数回退", path="A")
    lines = fp.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2, "只许追加，不许覆盖"
    assert lines[0] == first.strip()
    e1, e2 = (json.loads(x) for x in lines)
    for e in (e1, e2):
        for key in ("ts", "script", "path", "check", "reason"):
            assert e.get(key), f"拒绝日志缺字段 {key}: {e}"
    assert e1["check"] == "fingerprint" and e1["path"] == "B"
    assert e2["script"] == "tools/git_sync.py" and e2["path"] == "A"
    # 存放位置必须在推送白名单之外：sync 的 FILES 不得包含该文件
    sync_src = open(SYNC_PATH, encoding="utf-8").read()
    assert "release_gate_refusals" not in sync_src.split("FILES = [")[1] \
        .split("]")[0]
