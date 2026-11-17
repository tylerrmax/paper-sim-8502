#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件级快照工具（效率升级指令 V1.0 第四节）：SQLite 一致性冻结。

用 sqlite3 Backup API（Connection.backup）对目标库做一致性副本，
输出副本文件 + sha256 + 表清单（表名/行数/每表内容摘要）。内容
摘要 = 对该表全部行按 rowid 顺序的 repr 串流做 sha256，可独立
复算。diff 子命令对比两份 manifest，逐表列出变化。

用法：
  python -m market_first.snapshot_tool snapshot <db> <out_dir> [--label L]
  python -m market_first.snapshot_tool diff <manifest_a> <manifest_b>

快照只读源库（以只读 URI 打开 + backup），绝不写源库。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _table_digest(conn, table: str) -> str:
    h = hashlib.sha256()
    cur = conn.execute('SELECT * FROM "%s" ORDER BY rowid' % table)
    for row in cur:
        h.update(repr(tuple(row)).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def snapshot(db_path: str, out_dir: str, label: str = "") -> dict:
    src_path = Path(db_path).resolve()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / src_path.name
    src = sqlite3.connect("file:%s?mode=ro" % src_path, uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    conn = sqlite3.connect("file:%s?mode=ro" % dest, uri=True)
    try:
        tables = {}
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for (name,) in rows:
            cnt = conn.execute(
                'SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
            tables[name] = {
                "row_count": cnt,
                "content_sha256": _table_digest(conn, name),
            }
        triggers = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
            " ORDER BY name")]
    finally:
        conn.close()
    manifest = {
        "label": label,
        "source_path": str(src_path),
        "snapshot_file": dest.name,
        "taken_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "file_sha256": _sha256_file(dest),
        "file_size": dest.stat().st_size,
        "tables": tables,
        "triggers": triggers,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def diff_manifests(a: dict, b: dict) -> dict:
    """对比两份 manifest -> 逐表差异（行数/摘要）与文件级差异。"""
    tables = {}
    names = sorted(set(a["tables"]) | set(b["tables"]))
    for name in names:
        ta = a["tables"].get(name)
        tb = b["tables"].get(name)
        if ta == tb:
            tables[name] = {"changed": False}
        else:
            tables[name] = {
                "changed": True,
                "before": ta,
                "after": tb,
            }
    return {
        "file_sha256_before": a["file_sha256"],
        "file_sha256_after": b["file_sha256"],
        "file_changed": a["file_sha256"] != b["file_sha256"],
        "triggers_before": a.get("triggers", []),
        "triggers_after": b.get("triggers", []),
        "tables": tables,
        "changed_tables": [n for n, v in tables.items() if v["changed"]],
    }


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "snapshot":
        label = ""
        args = argv[1:]
        if "--label" in args:
            i = args.index("--label")
            label = args[i + 1]
            args = args[:i] + args[i + 2:]
        manifest = snapshot(args[0], args[1], label=label)
        print(json.dumps({"file_sha256": manifest["file_sha256"],
                          "tables": len(manifest["tables"]),
                          "taken_at": manifest["taken_at"]},
                         ensure_ascii=False))
        return 0
    if cmd == "diff":
        a = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        b = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        print(json.dumps(diff_manifests(a, b), ensure_ascii=False, indent=1))
        return 0
    print("unknown command: %s" % cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
