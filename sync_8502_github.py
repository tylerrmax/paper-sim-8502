#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
8502 独立交易终端 → GitHub 每日同步。
把本地有变更的部署文件推到 tylerrmax/paper-sim-8502（master），
供 Streamlit Cloud 拉取。无变更则静默（不产生空提交）。
Token 经 custom.github 连接器 surrogate 注入，本脚本不接触明文。
用法：python3 sync_8502_github.py [--dry-run]
      [--gate-check-only]        只跑发布门禁（release_gate.py），不推送
      [--force-bypass "原因"]    紧急绕过门禁（仅限用户明确下令；强制留痕）
      [--init-watermark]         以当前库初始化账本指纹水位线（部署时一次）
发布门禁：推送前依次跑冒烟/pytest/指纹水位线/账本一致性，任一不过打印
「发布门禁拒绝：<原因>」并以 exit 3 退出、零推送。设计见
~/workspace/release_gate/findings.md。
推送 API 带有限重试（P1）：仅连接类/5xx 等临时故障指数退避重试
（总尝试 ≤4 次），门禁拒绝与确定性错误不重试；PUT 结果未知时先核对
远端 blob sha 防重复推送。实现与分类口径见下方「P1 自动重试」节。
"""
import base64
import os
import subprocess
import sys

sys.path.insert(0, os.path.expanduser(
    "~/workspace/custom-connectors/custom.github/bin"))
from gh_api import api as _api_transport  # noqa: E402

REPO = "tylerrmax/paper-sim-8502"
DATA_REPO = "tylerrmax/paper-sim-8502-data"
# SYNC8502_SRC 仅供隔离测试/演练覆盖；生产恒为默认主仓库路径
SRC = os.environ.get("SYNC8502_SRC",
                     os.path.expanduser("~/workspace/paper-trading-sim"))

# ---------------------------------------------------------------------------
# P1 自动重试（第二层安全监控整改，2026-11-07；隔离开发，未经批准不进生产）
#
# 落点：gh_api.api 位于 custom.github 连接器（仓外共享基础设施，不在本
# 仓库版本控制内、多方复用），且重试策略（错误分类/退避/留痕/PUT 幂等
# 核对）是同步脚本自身的语义，故重试层落在 sync 的调用层：传输仍走
# gh_api.api（上文 _api_transport），本节 api() 在其上加有限重试，
# remote_sha / push_file 等既有调用点签名不变、自动获得重试。
# 发布门禁（release_gate）在任何 API 调用之前独立把关：门禁拒绝
# （GateRefusal / exit 3）发生在推送阶段之前，不经过本层；即使某异常
# 被本层遇到，GateRefusal 也归类为不可重试——重试在任何情况下都不能
# 绕过门禁。本节不提供、也不经过任何 --force-bypass 路径。
#
# 可重试（临时故障）：连接重置/连接错误/超时/不完整读等连接与协议类
# 异常；HTTP 500/502/503/504；HTTP 429（无速率限制终止语义时）。
# 不可重试（确定性错误）：HTTP 401/403/404/422 等；响应正文含
# "rate limit" 或响应头 X-RateLimit-Remaining: 0 的限流终止（GitHub
# 亦以 403 返回限流，同按此处理）；重定向拒绝等确定性 RuntimeError；
# 本地文件/序列化错误；其余未分类异常一律不可重试（fail-closed）。
# 退避：总尝试 ≤4 次，间隔 20s→60s→180s（加 ≤10% 随机抖动）。
# 幂等：PUT 发出后遇可重试错误时结果未知，先 GET 核对远端 blob sha，
# 已与目标一致则视为成功、绝不重复 PUT（见 _put_file_contents）；
# push_file 原有的「先比对后推送」语义保持不变。
# 留痕：只追加 JSONL（默认 SRC/reports/sync_retry_log.jsonl，路径可经
# _RETRY_LOG_PATH 注入）；每条含时间/方法/路径/文件/仓库/尝试序号/
# 错误类别/结果事件。写日志失败绝不影响同步本身。重试耗尽抛原异常，
# 现有 cron 失败告警通道照常工作。
# 测试注入点：_api_transport（假传输）、_sleep（假睡眠）、_RETRY_LOG_PATH。
# ---------------------------------------------------------------------------
import http.client as _http_client
import json as _json
import random as _random
import time as _time
import urllib.error as _urllib_error
from datetime import datetime as _datetime

_RETRY_DELAYS = (20.0, 60.0, 180.0)  # 3 次重试 + 首试 = 总尝试 ≤4 次
_RETRY_LOG_PATH = None  # None → 默认路径（SRC/reports/…）；测试可注入


def _sleep(seconds):
    _time.sleep(seconds)


def _retry_log_file():
    if _RETRY_LOG_PATH:
        return _RETRY_LOG_PATH
    return os.path.join(SRC, "reports", "sync_retry_log.jsonl")


def _log_retry_event(event, *, method=None, api_path=None, file=None,
                     repo=None, attempt=None, category=None, error=None,
                     delay_s=None, will_retry=None):
    rec = {"ts": _datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "event": event, "method": method, "api_path": api_path,
           "file": file, "repo": repo, "attempt": attempt,
           "category": category,
           "error": (str(error)[:300] if error is not None else None),
           "delay_s": delay_s, "will_retry": will_retry}
    try:
        path = _retry_log_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 留痕失败绝不阻断同步


def _http_error_body(exc):
    try:
        raw = exc.read()
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw or "")
    except Exception:
        return ""


def _classify_error(exc):
    """返回 (错误类别, 是否可重试)。未分类异常一律不可重试。"""
    if isinstance(exc, _urllib_error.HTTPError):
        # HTTPError 是 URLError 的子类，必须先于连接类判定。
        headers = getattr(exc, "headers", None) or getattr(exc, "hdrs", None)
        remaining = None
        try:
            remaining = headers.get("X-RateLimit-Remaining") if headers else None
        except Exception:
            remaining = None
        body = _http_error_body(exc) if exc.code in (403, 429) else ""
        if (body and "rate limit" in body.lower()) or remaining == "0":
            # 明确的速率限制终止语义：重试只会延长封禁，按确定性失败处理。
            return "rate_limited", False
        if exc.code in (500, 502, 503, 504):
            return "http_%d" % exc.code, True
        if exc.code == 429:
            return "http_429", True
        return "http_%d" % exc.code, False
    if isinstance(exc, (_urllib_error.URLError, ConnectionError, TimeoutError,
                        _http_client.HTTPException)):
        # 连接重置/连接失败/超时/不完整读/协议中断：临时故障。
        # （IncompleteRead、RemoteDisconnected 均落在此类或其子类。）
        return "connection", True
    return "non_retryable", False


def _backoff_delay(attempt):
    """attempt 为刚失败的尝试序号（1 起），返回下一次尝试前的等待秒数。"""
    base = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
    return base + _random.uniform(0, base * 0.1)


def _call_with_retry(method, api_path, data=None, *, file=None, repo=None):
    attempt = 0
    while True:
        attempt += 1
        try:
            result = _api_transport(method, api_path, data)
            if attempt > 1:
                _log_retry_event("retry_success", method=method,
                                 api_path=api_path, file=file, repo=repo,
                                 attempt=attempt)
            return result
        except Exception as exc:
            category, retryable = _classify_error(exc)
            if not retryable:
                _log_retry_event("attempt_failed", method=method,
                                 api_path=api_path, file=file, repo=repo,
                                 attempt=attempt, category=category,
                                 error=exc, will_retry=False)
                raise
            if attempt > len(_RETRY_DELAYS):
                _log_retry_event("retry_exhausted", method=method,
                                 api_path=api_path, file=file, repo=repo,
                                 attempt=attempt, category=category,
                                 error=exc)
                raise
            delay = _backoff_delay(attempt)
            _log_retry_event("attempt_failed", method=method,
                             api_path=api_path, file=file, repo=repo,
                             attempt=attempt, category=category, error=exc,
                             delay_s=round(delay, 2), will_retry=True)
            _sleep(delay)


def api(method, path, data=None):
    """gh_api.api 的有限重试包装；既有调用点签名与语义不变。"""
    return _call_with_retry(method, path, data)


def _remote_blob_sha(api_path):
    """经重试 GET 远端当前 blob sha；任何失败返回 None（不掩盖主错误）。"""
    try:
        r = api("GET", api_path)
        return r.get("sha") if isinstance(r, dict) else None
    except Exception:
        return None


def _put_file_contents(path, repo, data, expect_blob_sha):
    """PUT 单文件内容：有限重试 + 结果未知时的幂等核对。

    返回 (响应 dict, 是否经核对确认)。PUT 发出后若遇可重试错误，请求
    可能已在远端生效：再次 PUT 前先 GET 核对远端 blob sha，已等于目标
    sha 则视为成功、绝不重复 PUT；最后一次尝试失败后同样先核对再判
    耗尽。非可重试错误立即抛出，不核对、不重试。
    """
    api_path = f"/repos/{repo}/contents/{path}"
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = _api_transport("PUT", api_path, data)
            if attempt > 1:
                _log_retry_event("retry_success", method="PUT",
                                 api_path=api_path, file=path, repo=repo,
                                 attempt=attempt)
            return resp, False
        except Exception as exc:
            category, retryable = _classify_error(exc)
            if not retryable:
                _log_retry_event("attempt_failed", method="PUT",
                                 api_path=api_path, file=path, repo=repo,
                                 attempt=attempt, category=category,
                                 error=exc, will_retry=False)
                raise
            if expect_blob_sha and \
                    _remote_blob_sha(api_path) == expect_blob_sha:
                _log_retry_event("idempotent_reconciled", method="PUT",
                                 api_path=api_path, file=path, repo=repo,
                                 attempt=attempt, category=category,
                                 error=exc)
                return {"commit": {"sha": "reconciled"}}, True
            if attempt > len(_RETRY_DELAYS):
                _log_retry_event("retry_exhausted", method="PUT",
                                 api_path=api_path, file=path, repo=repo,
                                 attempt=attempt, category=category,
                                 error=exc)
                raise
            delay = _backoff_delay(attempt)
            _log_retry_event("attempt_failed", method="PUT",
                             api_path=api_path, file=path, repo=repo,
                             attempt=attempt, category=category, error=exc,
                             delay_s=round(delay, 2), will_retry=True)
            _sleep(delay)


# 公开仓库：代码（不含敏感数据）
FILES = [
    "app.py",
    ".streamlit/config.toml",
    "requirements.txt",
    "README.md",
    ".gitignore",
    "db/schema.sql",
    "data/__init__.py",
    "data/quotes.py",
    "engine/__init__.py",
    "engine/pipeline.py",
    "engine/signals.py",
    "engine/sim.py",
    "engine/execution.py",
    "engine/realtime.py",
    "engine/indicators.py",
    "engine/tv_charts.py",
    "engine/order_validate.py",
    "engine/screen.py",
    "engine/news_sentiment.py",
    "engine/risk_gate.py",
    "engine/regime.py",
    "engine/rotation.py",
    "engine/safety.py",
    "engine/data_health.py",
    "engine/page_freshness.py",
    "engine/inst_lhb.py",
    ".env.example",
    "config/sector_news.json",
    "config/workbook_map.json",
    "engine/lightweight-charts.standalone.js",
    "sync_snapshot.py",
    "run_daily.py",
    # 数据文件已公开（用户要求）
    "db/sim.db",
    "config/strategy.yaml",
    # 验收报告（2026-10-31）：验证页直发报告，随同步上公网
    "reports/public/*.md",
    # 关键测试公开复现（2026-10-31 专项整改 P2-02）：公开仓可
    # 独立运行安全补充/影子链/专项整改三套关键测试（独立测试
    # 库、运行命令与依赖版本见 README「关键测试复现」节）。
    "init_db.py",
    "tools/hash_baseline.py",
    "tools/nightly_run.sh",
    "tests/test_safety_supplements.py",
    "tests/test_inst_shadow_chain.py",
    "tests/test_audit_remediation.py",
    # 构建标识（P2-04）：同步时生成，云端页面只读展示运行版本
    "build_info.json",
]


def local_blob_sha(path):
    full = os.path.join(SRC, path)
    out = subprocess.run(["git", "hash-object", full], capture_output=True,
                         text=True, cwd=SRC)
    return out.stdout.strip() if out.returncode == 0 else None


def remote_sha(path, repo=None):
    repo = repo or REPO
    try:
        return api("GET", f"/repos/{repo}/contents/{path}").get("sha")
    except Exception:
        return None


def push_file(path, dry_run=False, repo=None):
    repo = repo or REPO
    full = os.path.join(SRC, path)
    if not os.path.exists(full):
        print(f"跳过（不存在）: {path}")
        return False
    # 回执隔离：推送 db/sim.db 前清空敏感表（真实券商回执永不公开）
    filtered = None
    if path == "db/sim.db":
        filtered = _filtered_db(full)
        full = filtered
    # 对过滤后的实际推送内容算 sha
    out = subprocess.run(["git", "hash-object", full], capture_output=True,
                         text=True, cwd=SRC)
    lsha = out.stdout.strip() if out.returncode == 0 else None
    rsha = remote_sha(path, repo)
    if lsha and lsha == rsha:
        return False  # 无变更
    if dry_run:
        print(f"[dry-run] 会推送: {path}")
        return True
    with open(full, "rb") as f:
        content = base64.b64encode(f.read()).decode()
    data = {"message": f"8502 每日同步：{path}",
            "content": content, "branch": "master"}
    if rsha:
        data["sha"] = rsha
    r, reconciled = _put_file_contents(path, repo, data, lsha)
    if reconciled:
        print(f"已推送: {path} -> 远端已生效（PUT 断线后核对确认，未重复 PUT）")
    else:
        print(f"已推送: {path} -> {r['commit']['sha'][:7]}")
    # 清理临时过滤文件
    if filtered:
        try:
            os.remove(filtered)
        except Exception:
            pass
    return True


def _filtered_db(src_path):
    """生成过滤版 DB：清空 broker_receipts 和 real_fills（真实券商数据永不推送到公开仓库）。
    返回临时文件路径。"""
    import sqlite3, tempfile, shutil
    tmp = tempfile.mktemp(suffix=".db")
    shutil.copy2(src_path, tmp)
    con = sqlite3.connect(tmp)
    for tbl in ("broker_receipts", "real_fills"):
        try:
            con.execute(f"DELETE FROM {tbl}")
        except Exception:
            pass  # 表不存在时跳过
    con.commit()
    con.close()
    return tmp


def _assert_fresh():
    """推送断言：daily_report 最大日期必须是最新的（今天或昨天，考虑周末），
    否则拒绝推送，避免云端展示过期数据。"""
    import sqlite3
    from datetime import datetime, timedelta
    db = os.path.join(SRC, "db", "sim.db")
    con = sqlite3.connect(db)
    row = con.execute("SELECT MAX(date) FROM daily_report").fetchone()
    con.close()
    if not row or not row[0]:
        raise RuntimeError("daily_report 为空，拒绝推送")
    max_date = datetime.strptime(row[0], "%Y-%m-%d").date()
    today = datetime.now().date()
    # 允许昨天（周末或跑批延迟）
    if (today - max_date).days > 2:
        raise RuntimeError(
            f"数据过期：daily_report 最大日期 {max_date}，距今 {(today - max_date).days} 天，拒绝推送")
    print(f"新鲜度检查通过：daily_report 截至 {max_date}")
    return True


def _arg_value(flag):
    """取 --flag 后一个参数值；无值返回空串；未出现返回 None。"""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    return None


def _print_gate_result(res):
    for c in res["checks"]:
        mark = "✅" if c["ok"] else "❌"
        print(f"发布门禁[{c['name']}] {mark} {c['detail']}")


def _failed_check(res):
    for c in reversed(res.get("checks") or []):
        if not c.get("ok"):
            return c.get("name", "unknown")
    return "unknown"


def main():
    import release_gate as gate

    dry_run = "--dry-run" in sys.argv
    db_path = os.path.join(SRC, "db", "sim.db")
    state_path = os.path.join(SRC, gate.STATE_NAME)

    if "--init-watermark" in sys.argv:
        st = gate.init_watermark(db_path, state_path)
        print(f"水位线已初始化：fills={st['fills_count']} "
              f"max_id={st['max_id']} hash={st['rolling_hash'][:16]}… "
              f"→ {state_path}")
        return

    if "--gate-check-only" in sys.argv:
        res = gate.run_gate(SRC, files=FILES)
        _print_gate_result(res)
        if not res["ok"]:
            gate.audit(SRC, {"event": "check_only_refuse",
                             "reason": res["reason"], "checks": res["checks"]})
            gate.log_refusal(
                SRC, "sync_8502_github.py",
                _failed_check(res), res["reason"],
                extra={"mode": "gate-check-only"})
            print(f"{gate.REJECT_LINE}：{res['reason']}")
            sys.exit(3)
        gate.audit(SRC, {"event": "check_only_pass",
                         "checks": res["checks"]})
        print("发布门禁检查通过（--gate-check-only：未推送、未推进水位线）")
        return

    bypass_reason = _arg_value("--force-bypass")
    if bypass_reason is not None:
        # 紧急绕过：仅限用户明确下令。强制留痕且醒目打印；水位线不推进。
        print(f"⚠️⚠️ {gate.BYPASS_LINE}（--force-bypass）：{bypass_reason}"
              f" —— 本次推送不经门禁校验，水位线不自动推进")
        gate.audit(SRC, {"event": "bypass", "reason": bypass_reason,
                         "dry_run": dry_run,
                         "skipped": ["smoke", "pytest", "fingerprint",
                                     "ledger"]})
    else:
        res = gate.run_gate(SRC, files=FILES)
        _print_gate_result(res)
        if not res["ok"]:
            gate.audit(SRC, {"event": "refuse", "reason": res["reason"],
                             "checks": res["checks"], "dry_run": dry_run})
            if not dry_run:
                # 正式运行被拒必须落持久拒绝日志（只追加，白名单之外）
                gate.log_refusal(
                    SRC, "sync_8502_github.py",
                    _failed_check(res), res["reason"])
            if dry_run:
                print(f"（dry-run 演练）{gate.REJECT_LINE}：{res['reason']}"
                      f" —— 正式运行将拒绝推送")
            else:
                print(f"{gate.REJECT_LINE}：{res['reason']}")
                sys.exit(3)
        else:
            gate.audit(SRC, {"event": "pass", "checks": res["checks"],
                             "dry_run": dry_run})

    if not dry_run:
        _assert_fresh()  # 推送前断言（保留原有新鲜度检查）
        # 构建标识（2026-10-31 整改 P2-04）：记录本次同步对应的
        # 本地提交，供云端页面只读展示实际运行版本。生成文件
        # 不入主仓（.gitignore 已排除）；生成失败只告警不阻断。
        try:
            import json as _bj
            from datetime import datetime as _dt
            _head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=SRC,
                capture_output=True, text=True).stdout.strip()
            with open(os.path.join(SRC, "build_info.json"), "w",
                      encoding="utf-8") as _bf:
                _bj.dump({"commit": _head,
                          "built_at": _dt.now().strftime(
                              "%Y-%m-%d %H:%M:%S"),
                          "source_repo": "paper-trading-sim (local master)"},
                         _bf, ensure_ascii=False)
            print("构建标识已生成：build_info.json commit=%s"
                  % _head[:12])
        except Exception as _be:  # noqa: BLE001
            print("WARN 构建标识生成失败（不阻断同步）：%s" % _be)
    changed = 0
    failures = []
    # 公开仓库：代码
    import glob as _glob
    _files = []
    for _pat in FILES:
        if "*" in _pat:
            _files.extend(sorted(
                os.path.relpath(p, SRC)
                for p in _glob.glob(os.path.join(SRC, _pat))))
        else:
            _files.append(_pat)
    for f in _files:
        try:
            if push_file(f, dry_run):
                changed += 1
        except Exception as e:
            failures.append(f)
            print(f"失败: {f}: {type(e).__name__}: {e}")
    # 私有仓库：敏感数据
    for f in ["config/strategy.yaml", "db/sim.db"]:
        try:
            if push_file(f, dry_run, repo=DATA_REPO):
                changed += 1
        except Exception as e:
            failures.append(f"(私有){f}")
            print(f"失败(私有): {f}: {type(e).__name__}: {e}")
    if failures:
        print(f"完成：{changed} 个文件有变更，{len(failures)} 个推送失败"
              f"（水位线不推进）：{failures}")
        sys.exit(1)
    print(f"完成：{changed} 个文件有变更" + ("（演练）" if dry_run else ""))
    if not dry_run and bypass_reason is None:
        st = gate.advance_watermark(db_path, state_path)
        print(f"水位线已推进：fills={st['fills_count']} "
              f"max_id={st['max_id']}")
    elif not dry_run:
        print("注意：绕过模式下水位线未推进，下次正常运行将重新校验账本。")


if __name__ == "__main__":
    main()
