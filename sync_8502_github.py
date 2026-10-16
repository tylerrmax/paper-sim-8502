#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
8502 独立交易终端 → GitHub 每日同步。
把本地有变更的部署文件推到 tylerrmax/paper-sim-8502（master），
供 Streamlit Cloud 拉取。无变更则静默（不产生空提交）。
Token 经 custom.github 连接器 surrogate 注入，本脚本不接触明文。
用法：python3 sync_8502_github.py [--dry-run]
"""
import base64
import os
import subprocess
import sys

sys.path.insert(0, os.path.expanduser(
    "~/workspace/custom-connectors/custom.github/bin"))
from gh_api import api  # noqa: E402

REPO = "tylerrmax/paper-sim-8502"
SRC = os.path.expanduser("~/workspace/paper-trading-sim")

# 云端部署需要的全部文件（app.py 只读这些）
FILES = [
    "app.py",
    "requirements.txt",
    "README.md",
    ".gitignore",
    "config/strategy.yaml",
    "db/sim.db",
    "db/schema.sql",
    "data/__init__.py",
    "data/quotes.py",
    "engine/__init__.py",
    "engine/pipeline.py",
    "engine/signals.py",
    "engine/sim.py",
    "engine/execution.py",
    "engine/batch_test.py",
    "engine/realtime.py",
    "engine/tv_charts.py",
    "engine/lightweight-charts.standalone.js",
    "sync_snapshot.py",
    "run_daily.py",
]


def local_blob_sha(path):
    full = os.path.join(SRC, path)
    out = subprocess.run(["git", "hash-object", full], capture_output=True,
                         text=True, cwd=SRC)
    return out.stdout.strip() if out.returncode == 0 else None


def remote_sha(path):
    try:
        return api("GET", f"/repos/{REPO}/contents/{path}").get("sha")
    except Exception:
        return None


def push_file(path, dry_run=False):
    full = os.path.join(SRC, path)
    if not os.path.exists(full):
        print(f"跳过（不存在）: {path}")
        return False
    lsha, rsha = local_blob_sha(path), remote_sha(path)
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
    r = api("PUT", f"/repos/{REPO}/contents/{path}", data)
    print(f"已推送: {path} -> {r['commit']['sha'][:7]}")
    return True


def main():
    dry_run = "--dry-run" in sys.argv
    changed = 0
    for f in FILES:
        try:
            if push_file(f, dry_run):
                changed += 1
        except Exception as e:
            print(f"失败: {f}: {type(e).__name__}: {e}")
    print(f"完成：{changed} 个文件有变更" + ("（演练）" if dry_run else ""))


if __name__ == "__main__":
    main()
