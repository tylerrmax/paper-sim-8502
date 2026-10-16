#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第一轮真实交易压力测试（plumbing，可一键重跑）。
第二轮追加：7 并发指令竞态 / 8 control 状态机 / 9 新表一致性。

铁律：
- 全部使用测试库 /tmp/stress_sim.db（生产库的拷贝），绝不触碰生产库。
- 不改策略规则（RULE_VERSION / engine 信号逻辑 / 毕业标准），只验证管道健壮性。
- 不向生产库编造行情；A股冒烟测试只读接口、不入库。

用法：
    cd ~/workspace/paper-trading-sim && python3 tests/stress_test.py
"""
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

PROD_DB = os.path.join(BASE, "db", "sim.db")
TEST_DB = "/tmp/stress_sim.db"
TEST_SNAPSHOT = "/tmp/stress_snapshot.json"
TEST_REPORTS = "/tmp/stress_reports"

# ---- 安全护栏：测试内所有路径强制指向测试库 ----
assert TEST_DB != PROD_DB and "stress" in TEST_DB, "测试库路径异常，拒绝运行"
os.environ["PAPER_SIM_DB"] = TEST_DB
os.environ["PAPER_SIM_SNAPSHOT"] = TEST_SNAPSHOT
os.environ["PAPER_SIM_REPORTS"] = TEST_REPORTS

import yaml  # noqa: E402
from engine.pipeline import run as pipe_run  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(BASE, "config", "strategy.yaml"), encoding="utf-8"))

RESULTS = []


def fresh_db():
    """每项测试前用生产库拷贝重建测试库（隔离）。"""
    shutil.copyfile(PROD_DB, TEST_DB)
    os.makedirs(TEST_REPORTS, exist_ok=True)


def run_pipe_capture():
    """跑管线并捕获 stdout，返回 (asof, output)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asof = pipe_run(TEST_DB, CFG)
    return asof, buf.getvalue()


def dump_fills(db=TEST_DB):
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT date,account,symbol,side,qty,price,fee,pnl FROM fills"
        " ORDER BY date,account,symbol,side,id").fetchall()
    con.close()
    return rows


def count_signals(db=TEST_DB):
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    con.close()
    return n


def check(name, fn):
    try:
        detail = fn() or ""
        RESULTS.append((name, True, detail))
        print("[PASS] %s %s" % (name, detail))
    except AssertionError as e:
        RESULTS.append((name, False, str(e)))
        print("[FAIL] %s %s" % (name, e))
    except Exception as e:  # noqa: BLE001
        import traceback
        RESULTS.append((name, False, "异常 %r" % e))
        print("[FAIL] %s 异常 %r" % (name, e))
        traceback.print_exc()


# ---------------------------------------------------------------- test 1
def test_1_missing_quote():
    """缺失行情：删掉持仓标的最近一个交易日 bar，管线必须 fail-safe：
    不崩溃、不编造 bar、WARN 留痕、缺失日持仓估值沿用上个收盘价（不归零）。
    注：选用多标的账户（sim_hk）的持仓标的，这样删除后该账户在缺失日仍有
    行情行，可验证“沿用上个收盘价估值”。"""
    fresh_db()
    con = sqlite3.connect(TEST_DB)
    sym = acct = None
    for s, a in con.execute("SELECT symbol, account FROM positions WHERE qty>0"):
        if len(CFG["universe"].get(a, [])) > 1:
            sym, acct = s, a
            break
    assert sym, "没有多标的账户的持仓，无法测试估值沿用"
    d_max = con.execute(
        "SELECT MAX(date) FROM market_data WHERE symbol=?", (sym,)).fetchone()[0]
    close_prev = con.execute(
        "SELECT close FROM market_data WHERE symbol=? AND date<? ORDER BY date DESC LIMIT 1",
        (sym, d_max)).fetchone()[0]
    con.close()

    asof0, _ = run_pipe_capture()  # 基线
    assert asof0, "基线管线无 asof"

    con = sqlite3.connect(TEST_DB)
    con.execute("DELETE FROM market_data WHERE symbol=? AND date=?", (sym, d_max))
    con.commit()
    con.close()

    asof, out = run_pipe_capture()  # 缺行情后重跑
    con = sqlite3.connect(TEST_DB)
    gone = con.execute(
        "SELECT COUNT(*) FROM market_data WHERE symbol=? AND date=?", (sym, d_max)
    ).fetchone()[0]
    # 该账户在缺失日仍有行情行（来自同账户另一标的），其持仓估值应沿用上个收盘价
    pv = con.execute(
        "SELECT position_value FROM daily_report WHERE account=? AND date=? AND symbol='TOTAL'",
        (acct, d_max)).fetchone()[0]

    assert gone == 0, "管线编造了缺失的 bar (%s,%s)" % (sym, d_max)
    assert "WARN" in out and sym in out, "缺行情无 WARN 留痕，输出=%r" % out[-300:]
    # 各持仓估值应沿用其各自最新已知收盘价：被删标的用上个收盘价，
    # 其余标的用当日收盘价；总量不归零、不编造。
    # 注：positions 表是管线重建的期末快照，此处用其校验总量构成。
    holds = con.execute(
        "SELECT symbol, qty FROM positions WHERE account=? AND qty>0", (acct,)).fetchall()
    assert holds, "持仓丢失：%s 的持仓在缺行情重跑后消失" % sym
    expected_pv = 0.0
    for psym, pqty in holds:
        if psym == sym:
            expected_pv += pqty * close_prev
        else:
            c = con.execute(
                "SELECT close FROM market_data WHERE symbol=? ORDER BY date DESC LIMIT 1",
                (psym,)).fetchone()[0]
            expected_pv += pqty * c
    expected_pv = round(expected_pv, 2)
    con.close()
    assert abs(pv - expected_pv) <= 0.01, \
        "估值未沿用上个收盘价：position_value=%s，期望≈%s" % (pv, expected_pv)
    assert pv > 0, "持仓估值被归零"
    return ("删 %s@%s：WARN 留痕，持仓未丢失，当日估值沿用各标的最新已知收盘价（总量 %s)" %
            (sym, d_max, expected_pv))


# ---------------------------------------------------------------- test 2
def test_2_idempotent_rerun():
    """重复跑批幂等：同一数据跑两次管线，signals/fills 不得翻倍、无重复键。"""
    fresh_db()
    pipe_run(TEST_DB, CFG)
    fa, sa = dump_fills(), count_signals()
    pipe_run(TEST_DB, CFG)
    fb, sb = dump_fills(), count_signals()
    assert fa == fb, "两次跑批 fills 不一致（%d→%d 行）" % (len(fa), len(fb))
    assert sa == sb, "两次跑批 signals 不一致（%d→%d 行）" % (sa, sb)
    keys = [(r[0], r[1], r[2], r[3]) for r in fb]
    assert len(keys) == len(set(keys)), "fills 出现重复 (date,account,symbol,side) 键"
    return "fills=%d 行、signals=%d 行，两次完全一致" % (len(fb), sb)


# ---------------------------------------------------------------- test 3
def test_3_cash_identity():
    """资金恒等式：所有账户所有日期 cash + 持仓市值 == NAV（容差 0.01）；
    positions 表市值 == qty×last_price（容差 0.01）。"""
    fresh_db()
    pipe_run(TEST_DB, CFG)
    con = sqlite3.connect(TEST_DB)
    bad, maxdiff = [], 0.0
    for acct, d, nav, cash, pv in con.execute(
            "SELECT account,date,nav,cash,position_value FROM daily_report WHERE symbol='TOTAL'"):
        diff = abs(nav - (cash + pv))
        maxdiff = max(maxdiff, diff)
        if diff > 0.01 + 1e-9:
            bad.append((acct, d, nav, cash, pv, round(diff, 4)))
    for acct, sym, qty, last, mv in con.execute(
            "SELECT account,symbol,qty,last_price,market_value FROM positions"):
        if abs(mv - round(qty * last, 2)) > 0.01 + 1e-9:
            bad.append(("positions", acct, sym, mv, round(qty * last, 2)))
    n = con.execute(
        "SELECT COUNT(*) FROM daily_report WHERE symbol='TOTAL'").fetchone()[0]
    con.close()
    assert not bad, "恒等式破坏 %d 处：%r" % (len(bad), bad[:3])
    return "检查 %d 个账户日，最大偏差 %s" % (n, round(maxdiff, 4))


# ---------------------------------------------------------------- test 4
def _apply_once(cmds):
    env = dict(os.environ)
    p = subprocess.run([sys.executable, os.path.join(BASE, "apply_commands.py")],
                       input=json.dumps(cmds), capture_output=True, text=True,
                       env=env, cwd=BASE, timeout=300)
    assert p.returncode == 0, "apply_commands 退出码 %d，stderr=%s" % (
        p.returncode, p.stderr[-500:])
    line = [ln for ln in p.stdout.splitlines() if ln.strip()][-1]
    return json.loads(line)


def test_4_exactly_once_commands():
    """指令 exactly-once：同一批指令跑两次，第二次 applied_ids 为空、
    duplicate_ids 列出重复 id，fills 不得重复，台账有记录。"""
    fresh_db()
    con = sqlite3.connect(TEST_DB)
    # 找一个真实存在、尚未成交的信号（不限 60 天窗口；引擎的审批门禁本身不限窗口）
    sig = con.execute(
        "SELECT s.date,s.account,s.symbol,s.side FROM signals s"
        " LEFT JOIN fills f ON f.date=s.date AND f.account=s.account"
        " AND f.symbol=s.symbol AND f.side=s.side"
        " WHERE f.date IS NULL"
        " ORDER BY s.date DESC LIMIT 1").fetchone()
    con.close()
    cmds = [{"id": "stress-setmode-1", "kind": "set_mode",
             "payload_json": json.dumps({"mode": "manual"})}]
    if sig:
        d, a, s, side = sig
        cmds.append({"id": "stress-approve-1", "kind": "approve",
                     "payload_json": json.dumps(
                         {"date": d, "account": a, "symbol": s, "side": side})})
    ids = [c["id"] for c in cmds]

    out1 = _apply_once(cmds)
    assert set(out1["applied_ids"]) == set(ids), "首次 applied_ids=%r" % out1
    assert out1["duplicate_ids"] == [], "首次不应有重复"
    fills1 = dump_fills()
    # 批准是否真实产生了一笔成交（有持仓的 SELL 才会成交；记录实际路径）
    fill_created = False
    if sig:
        d, a, s, side = sig
        con = sqlite3.connect(TEST_DB)
        fill_created = con.execute(
            "SELECT COUNT(*) FROM fills WHERE date=? AND account=? AND symbol=? AND side=?",
            (d, a, s, side)).fetchone()[0] > 0
        con.close()

    out2 = _apply_once(cmds)
    assert out2["applied_ids"] == [], "重放 applied_ids 应为空，实际=%r" % out2
    assert set(out2["duplicate_ids"]) == set(ids), "重放 duplicate_ids=%r" % out2
    fills2 = dump_fills()
    assert fills1 == fills2, "重放导致 fills 变化（%d→%d）" % (len(fills1), len(fills2))
    keys = [(r[0], r[1], r[2], r[3]) for r in fills2]
    assert len(keys) == len(set(keys)), "fills 出现重复键"

    con = sqlite3.connect(TEST_DB)
    logged = {r[0] for r in con.execute("SELECT id FROM processed_commands")}
    con.close()
    assert set(ids) <= logged, "processed_commands 台账缺失 %r" % (set(ids) - logged)
    return ("2 条指令（approve %s%s），重放零重复成交，台账齐全" %
            (sig[2] if sig else "-", " 成交1笔" if fill_created else " 未成交"))


# ---------------------------------------------------------------- test 5
def test_5_snapshot_atomicity():
    """快照读写并发：300 次并发写 + 持续读，零 torn read；
    再用真实 sync_snapshot.py 跑 8 次做集成验证。"""
    from sync_snapshot import atomic_write_json  # noqa: E402
    path = "/tmp/stress_snap.json"
    if os.path.exists(path):
        os.remove(path)
    stop, errors = threading.Event(), []
    reads = [0]

    def writer():
        try:
            for i in range(300):
                atomic_write_json(path, {"asof": "2026-10-05", "i": i,
                                         "final": i == 299, "blob": "x" * 20000,
                                         "accounts": [{"account": "sim_hk"}]})
        except Exception as e:  # noqa: BLE001
            errors.append(("writer", repr(e)))
        finally:
            stop.set()

    def reader():
        try:
            while not stop.is_set():
                try:
                    with open(path, encoding="utf-8") as f:
                        d = json.load(f)
                except FileNotFoundError:
                    continue
                assert d.get("asof") == "2026-10-05" and "blob" in d and "i" in d
                reads[0] += 1
        except Exception as e:  # noqa: BLE001
            errors.append(("reader", repr(e)))

    ts = [threading.Thread(target=writer), threading.Thread(target=reader),
          threading.Thread(target=reader)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=120)
    assert not errors, "并发读写出错：%r" % errors[:2]
    assert reads[0] > 50, "有效读取次数过少：%d" % reads[0]

    # 集成：真实脚本并发写，持续读
    path2 = "/tmp/stress_snap2.json"
    env = dict(os.environ)
    env["PAPER_SIM_SNAPSHOT"] = path2
    stop2, errors2, reads2 = threading.Event(), [], [0]

    def writer2():
        try:
            for _ in range(8):
                subprocess.run([sys.executable, os.path.join(BASE, "sync_snapshot.py")],
                               env=env, cwd=BASE, capture_output=True,
                               check=True, timeout=300)
        except Exception as e:  # noqa: BLE001
            errors2.append(("writer", repr(e)))
        finally:
            stop2.set()

    def reader2():
        try:
            while not stop2.is_set():
                try:
                    with open(path2, encoding="utf-8") as f:
                        d = json.load(f)
                except FileNotFoundError:
                    continue
                assert isinstance(d.get("accounts"), list) and d.get("asof")
                reads2[0] += 1
        except Exception as e:  # noqa: BLE001
            errors2.append(("reader", repr(e)))

    ts = [threading.Thread(target=writer2), threading.Thread(target=reader2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=600)
    assert not errors2, "集成并发出错：%r" % errors2[:2]
    assert reads2[0] > 0, "集成读取次数为 0"
    return "机制 300 写/%d 读零 torn；集成 8 次脚本/%d 读零 torn" % (reads[0], reads2[0])


# ---------------------------------------------------------------- test 6
def test_6_ashare_dryrun():
    """10-08 A股管道冒烟：只读拉取 002446/688305 前复权日K，不入库；
    要求有足够历史（>100 行）且最新日期 ≥ 2026-09-30（节前最后交易日）。"""
    from data.quotes import fetch_kline  # noqa: E402
    from datetime import date, timedelta  # noqa: E402
    start = date.today() - timedelta(days=400)
    info = []
    for code in ["002446", "688305"]:
        rows = fetch_kline(code, start)  # 只读，不写库
        assert rows, "%s 拉取为空" % code
        assert len(rows) > 100, "%s 仅 %d 行，历史不足" % (code, len(rows))
        last_d, close = rows[-1][0], rows[-1][4]
        assert str(last_d) >= "2026-09-30", "%s 最新日期 %s 早于节前最后交易日" % (code, last_d)
        info.append("%s %d行 最新%s 收%s" % (code, len(rows), last_d, close))
    return "；".join(info)


# ---------------------------------------------------------------- test 7
def _apply_concurrent(cmds):
    """真并发：两个 apply_commands 进程同时处理同一批指令。
    用线程同时 communicate，保证两进程的 stdin 几乎同时送达、
    pipeline 阶段真实重叠（顺序 communicate 会退化成串行，
    后进程直接看到前进程的台账，测不到竞态）。返回 (out1, out2)。"""
    env = dict(os.environ)
    data = json.dumps(cmds).encode("utf-8")
    ps = [subprocess.Popen([sys.executable, os.path.join(BASE, "apply_commands.py")],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, cwd=BASE)
          for _ in range(2)]
    results = [None, None]

    def run_one(i, p):
        try:
            stdout, stderr = p.communicate(data, timeout=300)
            line = [ln for ln in stdout.decode().splitlines() if ln.strip()][-1]
            results[i] = (p.returncode, json.loads(line), stderr.decode()[-500:])
        except Exception as e:  # noqa: BLE001
            results[i] = (None, None, "communicate 异常 %r" % e)

    ts = [threading.Thread(target=run_one, args=(i, p)) for i, p in enumerate(ps)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=320)
    outs = []
    for i, (rc, out, err) in enumerate(results):
        assert rc == 0, "并发进程%d异常，退出码 %r，stderr=%s" % (i, rc, err)
        assert isinstance(out, dict) and "applied_ids" in out and "duplicate_ids" in out, \
            "并发进程%d输出缺字段：%r" % (i, out)
        outs.append(out)
    return outs[0], outs[1]


def test_7_concurrent_apply():
    """并发指令竞态：两个 apply_commands 进程同时处理同一批指令。
    构造可成交场景：清空 fills → manual 跑出 signals → 双进程并发批准最早的 BUY。
    断言：fills 无 (date,account,symbol,side) 重复键；
    processed_commands 每 id 恰一行；两进程都正常输出 JSON。
    注：两进程各自都算“应用了”指令（幂等应用），各自 applied_ids 都含 id；
    关键不变量是成交不重复、台账不重复行——崩溃恢复语义（先重算、后记账）保持不变。"""
    dup_evidence = None
    for attempt in range(6):
        fresh_db()
        _set_control("running", "manual")
        con = sqlite3.connect(TEST_DB)
        con.execute("DELETE FROM fills")
        con.commit()
        con.close()
        pipe_run(TEST_DB, CFG)  # 生成 signals；manual 无审批 → 零成交
        con = sqlite3.connect(TEST_DB)
        buy = con.execute(
            "SELECT date,account,symbol,side FROM signals WHERE side='BUY'"
            " ORDER BY date LIMIT 1").fetchone()
        con.close()
        assert buy, "无 BUY 信号可审批"
        d, a, s, side = buy
        cmds = [{"id": "stress7-approve-%d" % attempt, "kind": "approve",
                 "payload_json": json.dumps(
                     {"date": d, "account": a, "symbol": s, "side": side})}]
        out1, out2 = _apply_concurrent(cmds)
        fills = dump_fills()
        keys = [(r[0], r[1], r[2], r[3]) for r in fills]
        counts = {}
        for k in keys:
            counts[k] = counts.get(k, 0) + 1
        dups = [k for k, c in counts.items() if c > 1]
        if dups:
            dup_evidence = (attempt, dups[:3], len(fills))
            break
        # 至少应真实产生这笔成交（否则测试空转，竞态无从观察）
        hit = [k for k in keys if k == (d, a, s, side)]
        assert len(hit) == 1, "第 %d 轮：批准的 BUY 未成交或重复（%d 笔）" % (attempt, len(hit))
        con = sqlite3.connect(TEST_DB)
        over = con.execute(
            "SELECT id, COUNT(*) c FROM processed_commands GROUP BY id HAVING c>1"
        ).fetchall()
        con.close()
        assert not over, "台账出现重复 id 行：%r" % over[:3]
    assert dup_evidence is None, "第 %d 轮并发出现重复成交键：%r（fills=%d）" % dup_evidence
    return "6 轮双进程真并发：fills 零重复键，台账每 id 恰一行"


# ---------------------------------------------------------------- test 8
def _set_control(status, mode):
    con = sqlite3.connect(TEST_DB)
    con.execute("INSERT INTO control(id,status,mode,updated) VALUES(1,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET status=excluded.status,"
                " mode=excluded.mode, updated=excluded.updated",
                (status, mode, "2026-10-06 00:00:00"))
    con.commit()
    con.close()


def _run_fresh(status, mode):
    """fresh_db → 设 control → 清空 fills 与 approvals → 跑管线 → 返回 (n_fills, n_signals)。
    注：生产库拷贝可能带有真实审批记录；本测试验证“无审批”语义，必须先清空。"""
    fresh_db()
    _set_control(status, mode)
    con = sqlite3.connect(TEST_DB)
    con.execute("DELETE FROM fills")
    con.execute("DELETE FROM approvals")
    con.commit()
    con.close()
    pipe_run(TEST_DB, CFG)
    con = sqlite3.connect(TEST_DB)
    nf = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    ns = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    con.close()
    return nf, ns


def test_8_control_state_machine():
    """control 状态机（engine/pipeline.py::run 新信号门禁）：
                    ┌─────────────┐
                    │  新信号到达  │
                    └──────┬──────┘
                           ▼
              decision == 'skipped'? ──yes──▶ 永不成交（任何模式）
                           │no
                           ▼
              status == 'paused'? ──yes──▶ 只记信号，不成交
                           │no
                           ▼
              mode == 'manual' and ──yes──▶ 只记信号，不成交
              decision != 'approved'?
                           │no
                           ▼
                        成交（INSERT fills）
    fail-safe 规则：status/mode 非法值不得 fail-open（当 running/auto），
    必须落到最保守语义（paused / manual）。
    另验证：paused 下审批 → 不成交；恢复 running → 审批生效成交。"""
    nf, ns = _run_fresh("running", "auto")
    assert nf > 0, "running+auto 应产生新成交，实际 fills=%d" % nf
    n_auto = nf

    nf, ns = _run_fresh("running", "manual")
    assert nf == 0, "running+manual 无审批应零成交，实际 %d" % nf
    assert ns > 0, "running+manual 应仍记录信号"

    nf, ns = _run_fresh("paused", "auto")
    assert nf == 0, "paused+auto 应零成交，实际 %d" % nf
    assert ns > 0, "paused 应仍记录信号"

    nf, ns = _run_fresh("paused", "manual")
    assert nf == 0 and ns > 0, "paused+manual：信号照记、零成交"

    # manual + 审批 → 被批准的信号成交（取最早的 BUY，保证现金充足可成交）
    fresh_db()
    _set_control("running", "manual")
    pipe_run(TEST_DB, CFG)
    con = sqlite3.connect(TEST_DB)
    buy = con.execute(
        "SELECT date,account,symbol,side FROM signals WHERE side='BUY'"
        " ORDER BY date LIMIT 1").fetchone()
    con.close()
    assert buy, "无 BUY 信号可审批"
    d, a, s, side = buy
    con = sqlite3.connect(TEST_DB)
    con.execute("INSERT OR REPLACE INTO approvals(date,account,symbol,side,decision,updated)"
                " VALUES(?,?,?,?,?,?)",
                (d, a, s, side, "approved", "2026-10-06 00:00:00"))
    con.commit()
    con.close()
    pipe_run(TEST_DB, CFG)
    con = sqlite3.connect(TEST_DB)
    hit = con.execute(
        "SELECT COUNT(*) FROM fills WHERE date=? AND account=? AND symbol=? AND side=?",
        (d, a, s, side)).fetchone()[0]
    con.close()
    assert hit == 1, "审批后应恰好成交 1 笔，实际 %d" % hit

    # paused 下审批 → 不成交；恢复 running → 审批生效成交（转移正确）
    # 注：先清空 fills，否则历史成交会污染“零成交”断言
    fresh_db()
    _set_control("paused", "manual")
    con = sqlite3.connect(TEST_DB)
    con.execute("DELETE FROM fills")
    con.execute("INSERT OR REPLACE INTO approvals(date,account,symbol,side,decision,updated)"
                " VALUES(?,?,?,?,?,?)",
                (d, a, s, side, "approved", "2026-10-06 00:00:00"))
    con.commit()
    con.close()
    pipe_run(TEST_DB, CFG)
    con = sqlite3.connect(TEST_DB)
    nf1 = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    con.close()
    assert nf1 == 0, "paused 下审批也不应成交，实际 %d" % nf1
    _set_control("running", "manual")
    pipe_run(TEST_DB, CFG)
    con = sqlite3.connect(TEST_DB)
    nf2 = con.execute(
        "SELECT COUNT(*) FROM fills WHERE date=? AND account=? AND symbol=? AND side=?",
        (d, a, s, side)).fetchone()[0]
    con.close()
    assert nf2 == 1, "恢复 running 后审批应成交，实际 %d" % nf2

    # 非法值 fail-safe：未知 status/mode 不得 fail-open 当 running/auto 用
    nf, _ = _run_fresh("bogus", "auto")
    assert nf == 0, "非法 status='bogus' 被当成 running（fail-open），成交 %d 笔" % nf
    nf, _ = _run_fresh("running", "weird")
    assert nf == 0, "非法 mode='weird' 被当成 auto（fail-open），成交 %d 笔" % nf

    return ("running+auto 成交%d；running+manual/paused×auto/manual 零成交且信号照记；"
            "审批→成交、暂停→恢复转移正确；非法值 fail-safe" % n_auto)


# ---------------------------------------------------------------- test 9
def _norm_ddl(sql):
    s = sql.strip().rstrip(";")
    s = re.sub(r"\s*\(\s*", "(", s)
    s = re.sub(r"\s*\)", ")", s)
    s = re.sub(r"\s*,\s*", ",", s)
    s = re.sub(r"\s+", " ", s)
    return s.upper()


def _table_info(db, table):
    con = sqlite3.connect(db)
    ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    cols = con.execute("PRAGMA table_info(%s)" % table).fetchall()
    con.close()
    assert ddl, "%s 表不存在" % table
    return ddl[0], [(c[1], c[2], c[5]) for c in cols]  # (列名, 类型, 是否主键)


def test_9_table_consistency():
    """新表一致性：processed_commands 在各建表入口语义一致。
    入口① db/schema.sql（经 run_daily.init_db，支持 PAPER_SIM_DB 指向测试库）；
    入口② apply_commands.py 自包含 DDL。
    验证：两入口建出的列定义/主键一致（归一化 DDL + PRAGMA 比对）；
    缺表旧库（当前生产库即第一轮之前的旧库形态）+ apply_commands → 表被补建（迁移路径）。"""
    # 入口①：init_db 建测试库（PAPER_SIM_DB 已指向测试路径；
    # 注意：必须在 import 之前设置环境变量，因为 run_daily.DB 在模块加载时绑定）
    db1 = "/tmp/stress_initdb.db"
    if os.path.exists(db1):
        os.remove(db1)
    os.environ["PAPER_SIM_DB"] = db1
    from run_daily import init_db  # noqa: E402
    init_db().close()
    ddl1, info1 = _table_info(db1, "processed_commands")

    # 入口②：缺表旧库 + apply_commands（set_mode 触发自包含 DDL）
    # 注：用 DROP 显式构造“缺表旧库”，不再依赖生产库恰好是旧形态
    #（2026-10-09 起生产库已有 processed_commands 表）。
    db2 = "/tmp/stress_legacy.db"
    if os.path.exists(db2):
        os.remove(db2)
    shutil.copyfile(PROD_DB, db2)
    con = sqlite3.connect(db2)
    con.execute("DROP TABLE IF EXISTS processed_commands")
    con.commit()
    missing = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name='processed_commands'").fetchone()[0]
    con.close()
    assert missing == 0, "前置条件破坏：旧库构造失败，processed_commands 表仍在"
    env = dict(os.environ)
    env["PAPER_SIM_DB"] = db2
    cmds = [{"id": "stress9-setmode", "kind": "set_mode",
             "payload_json": json.dumps({"mode": "manual"})}]
    p = subprocess.run([sys.executable, os.path.join(BASE, "apply_commands.py")],
                       input=json.dumps(cmds), capture_output=True, text=True,
                       env=env, cwd=BASE, timeout=300)
    assert p.returncode == 0, "apply_commands 退出码 %d，stderr=%s" % (
        p.returncode, p.stderr[-500:])
    ddl2, info2 = _table_info(db2, "processed_commands")

    assert _norm_ddl(ddl1) == _norm_ddl(ddl2), \
        "两入口 DDL 不一致：\n%s\n%s" % (ddl1, ddl2)
    assert info1 == info2, "两入口列定义不一致：%r vs %r" % (info1, info2)
    os.environ["PAPER_SIM_DB"] = TEST_DB  # 恢复，避免影响后续测试
    return "schema.sql 与自包含 DDL 列定义/主键一致；缺表旧库迁移补建成功"


# ---------------------------------------------------------------- test 10
def test_10_datasource_fallback():
    """数据源熔断：腾讯主源异常/空窗口时，自动切换新浪备用源（A股），
    数据不断档。模拟两种故障：抛异常、返回空列表。"""
    from data import quotes  # noqa: E402
    from datetime import date  # noqa: E402
    orig = quotes._fetch_tencent
    try:
        # 故障1：腾讯抛异常
        quotes._fetch_tencent = lambda c, s: (_ for _ in ()).throw(
            RuntimeError("模拟腾讯故障"))
        rows = quotes.fetch_kline("688305", date(2026, 10, 8))
        assert rows and rows[-1][0] >= date(2026, 10, 8), "新浪备用源未接管（异常场景）"
        # 故障2：腾讯返回空窗口（10-08/10-09 的 688305 真实故障形态）
        quotes._fetch_tencent = lambda c, s: []
        rows2 = quotes.fetch_kline("002446", date(2026, 10, 9))
        assert rows2 and rows2[-1][0] >= date(2026, 10, 9), "新浪备用源未接管（空窗口场景）"
    finally:
        quotes._fetch_tencent = orig
    # 恢复后主源正常
    rows3 = quotes.fetch_kline("002446", date(2026, 10, 9))
    assert rows3, "主源恢复后无数据"
    return "腾讯异常/空窗口 → 新浪接管（688305 %d行/002446 %d行），主源恢复正常" % (
        len(rows), len(rows2))


def main():
    print("生产库: %s" % PROD_DB)
    print("测试库: %s（每项测试前重建）" % TEST_DB)
    print("=" * 70)
    check("1 缺失行情 fail-safe", test_1_missing_quote)
    check("2 重复跑批幂等", test_2_idempotent_rerun)
    check("3 资金恒等式", test_3_cash_identity)
    check("4 指令 exactly-once", test_4_exactly_once_commands)
    check("5 快照读写并发", test_5_snapshot_atomicity)
    check("6 A股管道冒烟", test_6_ashare_dryrun)
    check("7 并发指令竞态", test_7_concurrent_apply)
    check("8 control 状态机", test_8_control_state_machine)
    check("9 新表一致性", test_9_table_consistency)
    check("10 数据源熔断", test_10_datasource_fallback)
    print("=" * 70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("结果：%d/%d 通过" % (passed, len(RESULTS)))
    for name, ok, detail in RESULTS:
        if not ok:
            print("  失败项：%s —— %s" % (name, detail))
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
