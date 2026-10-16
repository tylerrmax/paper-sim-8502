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
    # 回执隔离：推送 db/sim.db 前清空敏感表（真实券商回执永不公开）
    filtered = None
    if path == "db/sim.db":
        filtered = _filtered_db(full)
        full = filtered
    # 对过滤后的实际推送内容算 sha
    out = subprocess.run(["git", "hash-object", full], capture_output=True,
                         text=True, cwd=SRC)
    lsha = out.stdout.strip() if out.returncode == 0 else None
    rsha = remote_sha(path)
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


def main():
    dry_run = "--dry-run" in sys.argv
    if not dry_run:
        _assert_fresh()  # 推送前断言
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
