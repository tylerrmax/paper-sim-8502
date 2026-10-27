#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""8502 → GitHub 单次原子同步（替代 sync_8502_github.py 的"逐文件逐提交"）。

为什么：逐文件提交会让公网在同步中途出现"新 app.py + 旧 sim.db"的混合状态。
本脚本把所有变更放进【一个提交】（Git Data API：blobs → tree → commit → 更新 ref），
并在推送前强制检查：
  1. 部署清单完整（import 闭包里的文件都在清单内）
  2. 一致性检查无 error（tools/check_consistency.py，对"过滤后将要推送的 db"跑）
  3. daily_report 新鲜度
  4. 真实券商数据（broker_receipts / real_fills）清空后才推送
任一不过 → 不推送，退出码 1。

用法（在 muse 本机，沿用同样的 gh_api 连接器）：
  python3 tools/sync_atomic.py --dry-run     # 只检查并列出将提交的文件
  python3 tools/sync_atomic.py               # 正式推送
  python3 tools/sync_atomic.py --allow-warn  # 默认即允许 warn；--strict 则 warn 也拒绝
"""
import base64
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

REPO = "tylerrmax/paper-sim-8502"
BRANCH = "master"
SRC = os.environ.get("SIM_SRC", os.path.expanduser("~/workspace/paper-trading-sim"))
PRIVATE_TABLES = ("broker_receipts", "real_fills")


def filtered_db(src_path):
    tmp = tempfile.mktemp(suffix=".db")
    shutil.copy2(src_path, tmp)
    con = sqlite3.connect(tmp)
    for t in PRIVATE_TABLES:
        try:
            con.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass
    con.commit()
    con.execute("VACUUM")            # 抹掉已删除记录的残留页，防止从文件里恢复券商数据
    con.close()
    return tmp


def private_rows(db_path):
    con = sqlite3.connect(db_path)
    n = 0
    for t in PRIVATE_TABLES:
        try:
            n += con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            pass
    con.close()
    return n


def preflight(files, src=SRC, strict=False):
    """返回 (ok, problems, db_to_push)。files 为清单；不触网。"""
    import check_consistency
    import sync_manifest
    problems = []
    miss = [f for f in sync_manifest.required_files() if f not in set(files)]
    if miss:
        problems.append("部署清单缺少被引用的文件：" + ", ".join(miss))
    db_src = os.path.join(src, "db", "sim.db")
    pushed_db = filtered_db(db_src)
    if private_rows(pushed_db):
        problems.append("过滤后的 db 仍含券商私有数据，拒绝推送")
    res = check_consistency.run_checks(pushed_db, os.path.join(src, "config", "strategy.yaml"))
    for r in res:
        if r["severity"] == "error" or (strict and r["severity"] == "warn"):
            problems.append(f"[{r['code']}] {r['msg']}")
    row = sqlite3.connect(pushed_db).execute("SELECT MAX(date) FROM daily_report").fetchone()
    if not row or not row[0]:
        problems.append("daily_report 为空")
    elif (datetime.now().date() - datetime.strptime(row[0], "%Y-%m-%d").date()).days > 2:
        problems.append(f"数据过期：daily_report 截至 {row[0]}")
    summary = f"check: error={sum(r['severity']=='error' for r in res)} warn={sum(r['severity']=='warn' for r in res)}"
    return (not problems), problems, pushed_db, summary


def _git_blob_sha(path):
    import hashlib
    data = open(path, "rb").read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def atomic_push(api, files, src, pushed_db, summary, dry_run=False):
    """api(method, path, data=None) -> dict，与 gh_api.api 同签名。返回新提交 sha（无变更返回 None）。"""
    head = api("GET", f"/repos/{REPO}/git/ref/heads/{BRANCH}")["object"]["sha"]
    base_tree = api("GET", f"/repos/{REPO}/git/commits/{head}")["tree"]["sha"]
    remote = {e["path"]: e["sha"] for e in
              api("GET", f"/repos/{REPO}/git/trees/{base_tree}?recursive=1")["tree"] if e["type"] == "blob"}
    changed, entries = [], []
    for f in files:
        path = pushed_db if f == "db/sim.db" else os.path.join(src, f)
        if not os.path.exists(path):
            continue
        if _git_blob_sha(path) == remote.get(f):
            continue
        changed.append(f)
        if dry_run:
            continue
        blob = api("POST", f"/repos/{REPO}/git/blobs",
                   {"content": base64.b64encode(open(path, "rb").read()).decode(), "encoding": "base64"})
        entries.append({"path": f, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    if not changed:
        return None, []
    if dry_run:
        return "dry-run", changed
    tree = api("POST", f"/repos/{REPO}/git/trees", {"base_tree": base_tree, "tree": entries})
    msg = f"8502 同步 {datetime.now():%Y-%m-%d %H:%M}：{len(changed)} 个文件（单次提交）\n\n{summary}\n" + "\n".join(f"- {c}" for c in changed)
    commit = api("POST", f"/repos/{REPO}/git/commits", {"message": msg, "tree": tree["sha"], "parents": [head]})
    api("PATCH", f"/repos/{REPO}/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": False})
    return commit["sha"], changed


def main(argv):
    dry = "--dry-run" in argv
    sys.path.insert(0, os.path.expanduser("~/workspace/custom-connectors/custom.github/bin"))
    import sync_manifest
    files = sync_manifest.required_files() if "--auto-list" in argv else \
        sync_manifest.read_files_list(os.path.join(SRC, "sync_8502_github.py"))
    ok, problems, pushed_db, summary = preflight(files, SRC, strict="--strict" in argv)
    print(summary)
    if not ok:
        print("❌ 推送前检查未通过，已拒绝：")
        for p in problems:
            print("  -", p)
        return 1
    from gh_api import api  # noqa: E402
    sha, changed = atomic_push(api, files, SRC, pushed_db, summary, dry)
    try:
        os.remove(pushed_db)
    except OSError:
        pass
    print("无变更" if sha is None else f"{'[dry-run] 将提交' if dry else '已提交 ' + sha[:7]}：{len(changed)} 个文件")
    for c in changed:
        print("  ", c)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
