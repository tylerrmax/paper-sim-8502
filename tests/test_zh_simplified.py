#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简体中文一致性：8502 公网界面与代码注释统一使用简体中文（方便团队协作）。

判定方法：不在 GB2312 字符集内的汉字基本都是繁体字（GB2312 只收录简体常用字）。
用法： python3 tests/test_zh_simplified.py   （或 pytest）
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", "db", ".venv"}
SKIP_EXT = (".js", ".db", ".pyc")
# 允许出现的例外字（如专有名词），需要时在此登记
ALLOW = set()
# CJK 统一表意文字范围（用 chr() 组装，避免转义序列被编辑器展开）
_CJK = re.compile("[" + chr(0x4E00) + "-" + chr(0x9FFF) + "]")


def _bad_chars(text):
    out = {}
    for i, line in enumerate(text.split("\n"), 1):
        for ch in _CJK.findall(line):
            if ch in ALLOW:
                continue
            try:
                ch.encode("gb2312")
            except UnicodeEncodeError:
                out.setdefault(ch, []).append(i)
    return out


def scan_files():
    bad = {}
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(SKIP_EXT):
                continue
            p = os.path.join(root, f)
            try:
                text = open(p, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            b = _bad_chars(text)
            if b:
                bad[os.path.relpath(p, ROOT)] = {c: v[:3] for c, v in b.items()}
    return bad


def scan_db():
    """界面会直接显示数据库里的文本（信号类型、原因等），同样要求简体。"""
    path = os.path.join(ROOT, "db", "sim.db")
    if not os.path.exists(path):
        return {}
    con = sqlite3.connect(path)
    bad = {}
    for (t,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")
                if (r[2] or "").upper() in ("TEXT", "")]
        for col in cols:
            for (v,) in con.execute(f"SELECT DISTINCT {col} FROM {t} WHERE {col} IS NOT NULL LIMIT 5000"):
                if isinstance(v, str):
                    b = _bad_chars(v)
                    if b:
                        bad.setdefault(f"{t}.{col}", set()).update(b)
    con.close()
    return {k: "".join(sorted(v)) for k, v in bad.items()}


def test_source_files_simplified():
    bad = scan_files()
    assert not bad, f"发现繁体字：{bad}"


def test_database_text_simplified():
    bad = scan_db()
    assert not bad, f"数据库文本含繁体字：{bad}"


if __name__ == "__main__":
    b1, b2 = scan_files(), scan_db()
    print("✅ 源码/文档均为简体" if not b1 else f"❌ 源码含繁体字：{b1}")
    print("✅ 数据库文本均为简体" if not b2 else f"❌ 数据库含繁体字：{b2}")
    sys.exit(1 if (b1 or b2) else 0)
