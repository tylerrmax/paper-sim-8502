#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：暂停→恢复后，不得自动补成交暂停期间积压的旧信号。
只允许符合有效期和执行条件的新信号成交。
"""
import os
import shutil
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

from engine.pipeline import run
import yaml

def load_cfg():
    with open(os.path.join(BASE, "config", "strategy.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)

TEST_DB = "/tmp/test_pause_resume.db"

def fills_count(db):
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    con.close()
    return n

def set_status(db, status):
    con = sqlite3.connect(db)
    con.execute("UPDATE control SET status=? WHERE id=1", (status,))
    con.commit()
    con.close()

def main():
    # 用生产库结构建测试库（不碰生产库）
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)
    shutil.copy("db/sim.db", TEST_DB)
    cfg = load_cfg()

    # 基线：running 模式跑一轮
    set_status(TEST_DB, "running")
    run(TEST_DB, cfg)
    base_fills = fills_count(TEST_DB)
    print(f"基线 fills: {base_fills}")

    # 暂停：跑一轮，应零新增成交，暂停黑名单应有记录
    set_status(TEST_DB, "paused")
    run(TEST_DB, cfg)
    paused_fills = fills_count(TEST_DB)
    assert paused_fills == base_fills, f"暂停期不应有新成交: {base_fills} -> {paused_fills}"
    con = sqlite3.connect(TEST_DB)
    blacklist_n = con.execute("SELECT COUNT(*) FROM pause_skipped_signals").fetchone()[0]
    con.close()
    print(f"暂停期 fills 零增长 ✅, 黑名单 {blacklist_n} 条")

    # 恢复 running：再跑一轮，fills 必须零增长（不补成交积压旧信号）
    set_status(TEST_DB, "running")
    run(TEST_DB, cfg)
    resumed_fills = fills_count(TEST_DB)
    assert resumed_fills == paused_fills, \
        f"恢复后不得补成交旧信号: {paused_fills} -> {resumed_fills}"
    print(f"恢复后 fills 零增长 ✅（{paused_fills} -> {resumed_fills}）")

    # 清理
    os.remove(TEST_DB)
    print("暂停恢复回归测试通过")

if __name__ == "__main__":
    main()
