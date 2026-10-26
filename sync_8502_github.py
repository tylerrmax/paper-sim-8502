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
"""
import base64
import os
import subprocess
import sys

sys.path.insert(0, os.path.expanduser(
    "~/workspace/custom-connectors/custom.github/bin"))
from gh_api import api  # noqa: E402

REPO = "tylerrmax/paper-sim-8502"
DATA_REPO = "tylerrmax/paper-sim-8502-data"
# SYNC8502_SRC 仅供隔离测试/演练覆盖；生产恒为默认主仓库路径
SRC = os.environ.get("SYNC8502_SRC",
                     os.path.expanduser("~/workspace/paper-trading-sim"))

# 公开仓库：代码（不含敏感数据）
FILES = [
    "app.py",
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
    "config/sector_news.json",
    "config/workbook_map.json",
    "engine/lightweight-charts.standalone.js",
    "sync_snapshot.py",
    "run_daily.py",
    # 数据文件已公开（用户要求）
    "db/sim.db",
    "config/strategy.yaml",
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
    r = api("PUT", f"/repos/{repo}/contents/{path}", data)
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
    changed = 0
    failures = []
    # 公开仓库：代码
    for f in FILES:
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
