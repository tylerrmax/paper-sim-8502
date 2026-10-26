#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发布门禁（路径 B：sync_8502_github.py 每日 17:30 同步）。

设计依据：~/workspace/release_gate/findings.md（2026-10-27 核查定版）。
门禁在推送任何文件之前依次执行，任一不过即拒绝（零推送）：
  (a) 语法/import 冒烟：FILES 中全部 .py 用 compile() 做语法检查（不落盘），
      并在子进程中 import app 与 engine/data 全部模块；
  (b) pytest 全套（排除脚本型长跑文件 e2e_8stage.py / stress_test.py），
      超时上限 300 秒；
  (c) 账本指纹水位线：水位线文件 .release_gate_state.json 记录上次放行时
      fills 笔数与按 id 顺序的滚动哈希。本次要求：水位内历史行数与哈希
      不变（防篡改/防回滚）、笔数只增不减、新增行的日期必须都能在
      daily_report 中找到对应日期（夜跑佐证，无源之水拒绝）；
  (d) tools/check_ledger_consistency.py：positions == fills 汇总。

水位线只在门禁全过且推送全部成功后由 sync 脚本调用 advance_watermark()
推进；--force-bypass 绕过时不推进（防洗白），只写审计日志。
每次门禁判定（通过/拒绝/绕过）都追加写 reports/release_gate_log.jsonl。
"""
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

REJECT_LINE = "发布门禁拒绝"
BYPASS_LINE = "发布门禁已被绕过"
STATE_NAME = ".release_gate_state.json"
AUDIT_LOG_REL = os.path.join("reports", "release_gate_log.jsonl")
# 门禁拒绝专属日志（只追加）：每次拒绝落一行 {ts, script, path, check, reason}。
# 位于 reports/ 下，不在 sync_8502_github.py 的 FILES 推送白名单内，
# 也不会被路径 A 的 git add -A 扫到（不在 paper-sim-web 仓库内）。
REFUSAL_LOG_REL = os.path.join("reports", "release_gate_refusals.jsonl")
HASH_SQL = ("SELECT id, date, account, symbol, side, qty, price, fee, pnl "
            "FROM fills ORDER BY id")
IMPORT_MODULES = [
    "app",
    "engine.pipeline", "engine.signals", "engine.sim", "engine.execution",
    "engine.realtime", "engine.indicators", "engine.tv_charts",
    "engine.order_validate", "engine.screen", "engine.news_sentiment",
    "engine.risk_gate", "engine.regime", "engine.rotation",
    "data.quotes", "sync_snapshot", "run_daily",
]
PYTEST_TIMEOUT = 300
SMOKE_TIMEOUT = 180


class GateRefusal(Exception):
    """门禁拒绝：message 即拒绝原因（会出现在「发布门禁拒绝：<原因>」行）。"""


# ---------------------------------------------------------------- 纹/水位线
def rolling_hash(rows):
    """对按 id 顺序的 fills 行做链式 sha256：任一行被改/删/重排都会改变结果。"""
    h = hashlib.sha256()
    for r in rows:
        h.update(repr(tuple(r)).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _fetch_fills(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return con.execute(HASH_SQL).fetchall()
    finally:
        con.close()


def compute_watermark(db_path):
    rows = _fetch_fills(db_path)
    return {
        "version": 1,
        "fills_count": len(rows),
        "max_id": rows[-1][0] if rows else 0,
        "rolling_hash": rolling_hash(rows),
    }


def load_state(state_path):
    if not os.path.exists(state_path):
        return None
    with open(state_path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state_path, state):
    state = dict(state)
    state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path)
    return state


def init_watermark(db_path, state_path):
    """以当前库实查值初始化水位线（部署时执行一次）。"""
    return save_state(state_path, compute_watermark(db_path))


def advance_watermark(db_path, state_path):
    """全部门禁通过且推送成功后推进水位线到当前库状态。"""
    return save_state(state_path, compute_watermark(db_path))


def check_fingerprint(db_path, state):
    """水位线核对。通过返回新增行数；不过抛 GateRefusal。"""
    if state is None:
        raise GateRefusal(
            f"水位线未初始化（缺 {STATE_NAME}），先执行 --init-watermark")
    rows = _fetch_fills(db_path)
    wm_id = state["max_id"]
    wm_rows = [r for r in rows if r[0] <= wm_id]
    if len(wm_rows) != state["fills_count"]:
        raise GateRefusal(
            f"水位内历史行数不符：水位 {state['fills_count']} 行（id≤{wm_id}），"
            f"实查 {len(wm_rows)} 行——历史成交被删改或账本回滚")
    if rolling_hash(wm_rows) != state["rolling_hash"]:
        raise GateRefusal(
            f"水位内历史行滚动哈希不一致（id≤{wm_id}）——历史成交被篡改")
    new_rows = [r for r in rows if r[0] > wm_id]
    if new_rows:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            dr_dates = {r[0] for r in con.execute(
                "SELECT DISTINCT date FROM daily_report")}
        finally:
            con.close()
        bad = sorted({r[1] for r in new_rows} - dr_dates)
        if bad:
            raise GateRefusal(
                f"新增 {len(new_rows)} 笔成交中，日期 {bad} 在 daily_report "
                f"中无对应日期——无夜跑佐证的新增成交，拒绝放行")
    return len(new_rows)


def check_ledger(db_path, src):
    """复用现成 tools/check_ledger_consistency.py 的 check()（只读）。"""
    tool = os.path.join(src, "tools", "check_ledger_consistency.py")
    spec = importlib.util.spec_from_file_location(
        "check_ledger_consistency", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    errs = mod.check(db_path)
    if errs:
        raise GateRefusal("账本一致性检查失败：" + "；".join(errs))
    return "positions == fills 汇总"


# ------------------------------------------------------------------- 子检查
def run_smoke(src, files):
    """(a) 语法/import 冒烟。返回明细字符串；不过抛 GateRefusal。"""
    bad = []
    n_py = 0
    for rel in files:
        if not rel.endswith(".py"):
            continue
        full = os.path.join(src, rel)
        if not os.path.exists(full):
            continue
        n_py += 1
        try:
            with open(full, encoding="utf-8") as f:
                compile(f.read(), rel, "exec")
        except SyntaxError as e:
            bad.append(f"{rel}: {e}")
    if bad:
        raise GateRefusal("语法检查失败：" + "；".join(bad))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    code = ("import importlib, sys\n"
            "for m in sys.argv[1:]:\n"
            "    importlib.import_module(m)\n")
    try:
        r = subprocess.run(
            [sys.executable, "-c", code, *IMPORT_MODULES],
            cwd=src, capture_output=True, text=True,
            timeout=SMOKE_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise GateRefusal(f"import 冒烟超时（>{SMOKE_TIMEOUT}s）")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        raise GateRefusal(
            "import 冒烟失败：" + (tail[-1] if tail else "未知错误"))
    return f"{n_py} 个 .py 语法 OK，{len(IMPORT_MODULES)} 个模块 import OK"


def default_pytest_cmd(src):
    venv_py = os.path.join(src, ".venv", "bin", "python")
    py = venv_py if os.path.exists(venv_py) else sys.executable
    # 排除脚本型文件：它们在收集期即执行并 sys.exit，会打断整套 pytest
    # （e2e_8stage / stress_test 为长跑脚本；test_gapfill / test_ui_regression
    #  / test_pause_resume 为脚本型自检，各自有独立运行方式，不经 pytest）。
    return [py, "-m", "pytest", "tests/", "-q",
            "--ignore=tests/e2e_8stage.py", "--ignore=tests/stress_test.py",
            "--ignore=tests/test_gapfill.py",
            "--ignore=tests/test_ui_regression.py",
            "--ignore=tests/test_pause_resume.py",
            "-p", "no:cacheprovider"]


def run_pytest(src, cmd=None, timeout=PYTEST_TIMEOUT):
    """(b) pytest 全套。返回明细字符串；不过抛 GateRefusal。"""
    cmd = cmd or default_pytest_cmd(src)
    try:
        r = subprocess.run(cmd, cwd=src, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GateRefusal(f"pytest 超时（>{timeout}s），按未通过处理")
    out = (r.stdout or "") + (r.stderr or "")
    tail = [ln for ln in out.strip().splitlines() if ln.strip()]
    summary = tail[-1] if tail else ""
    if r.returncode != 0:
        failed = [ln for ln in tail if "FAILED" in ln or "ERROR" in ln][:5]
        raise GateRefusal(
            f"pytest 未通过（exit {r.returncode}）：{summary}"
            + ("；" + "；".join(failed) if failed else ""))
    return f"pytest 通过：{summary}"


# --------------------------------------------------------------------- 审计
def audit(src, entry):
    """把一次门禁判定追加写入审计日志（jsonl）。写失败不阻断主流程。"""
    entry = dict(entry)
    entry.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    entry.setdefault("path", "B")
    try:
        path = os.path.join(src, AUDIT_LOG_REL)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return entry


def log_refusal(src, script, check, reason, path="B", extra=None):
    """门禁拒绝专属日志（持久、只追加）：时间、被拒脚本、路径、失败检查项、
    原因摘要。文件在 reports/ 下，不在任何同步推送白名单内、不会被同步覆盖。
    写失败不阻断拒绝本身（拒绝已由 exit 3 保证），但会额外打印告警行。"""
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "script": script,
        "path": path,
        "check": check,
        "reason": reason,
    }
    if extra:
        entry.update(extra)
    try:
        fp = os.path.join(src, REFUSAL_LOG_REL)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"警告：门禁拒绝日志写入失败：{type(e).__name__}: {e}")
    return entry


# --------------------------------------------------------------------- 主门
def run_gate(src, db_path=None, state_path=None, files=None,
             smoke_runner=None, pytest_runner=None):
    """
    依次执行 (a) 冒烟 → (b) pytest → (c) 指纹水位线 → (d) 账本一致性，
    首个失败即停。返回 {"ok": bool, "reason": str|None, "checks": [...]}。
    smoke_runner / pytest_runner 可注入（测试用），签名 (src) -> 明细字符串，
    失败时抛 GateRefusal。
    """
    db_path = db_path or os.path.join(src, "db", "sim.db")
    state_path = state_path or os.path.join(src, STATE_NAME)
    checks = []

    def step(name, fn):
        try:
            detail = fn()
        except GateRefusal as e:
            checks.append({"name": name, "ok": False, "detail": str(e)})
            return False
        except Exception as e:  # 检查自身异常一律按拒绝处理（fail-closed）
            checks.append({"name": name, "ok": False,
                           "detail": f"检查异常：{type(e).__name__}: {e}"})
            return False
        checks.append({"name": name, "ok": True, "detail": detail})
        return True

    ok = step("smoke", lambda: (smoke_runner or (lambda s: run_smoke(s, files or [])))(src))
    if ok:
        ok = step("pytest", lambda: (pytest_runner or run_pytest)(src))
    if ok:
        def _fp():
            state = load_state(state_path)
            n = check_fingerprint(db_path, state)
            return (f"水位线 OK（水位 {state['fills_count']} 笔 / max_id "
                    f"{state['max_id']}，本次新增 {n} 笔）")
        ok = step("fingerprint", _fp)
    if ok:
        ok = step("ledger", lambda: check_ledger(db_path, src))
    reason = None
    if not ok:
        last = checks[-1]
        reason = f"{last['name']}：{last['detail']}"
    return {"ok": ok, "reason": reason, "checks": checks}
