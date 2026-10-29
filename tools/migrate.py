#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按序应用 db/migrations/NNN_*.sql（幂等；先备份；失败整体回滚并保留备份）。
  python3 tools/migrate.py [db路径] [--dry-run]
ALTER TABLE ADD COLUMN 遇到"列已存在"视为已应用。"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _statements(sql):
    """按触发器 BEGIN..END; 感知的方式拆分语句。"""
    buf, out, depth = "", [], 0
    for line in sql.splitlines():
        s = line.strip()
        if s.startswith("--") and not buf:
            continue
        buf += line + "\n"
        up = s.upper()
        if re.search(r"\bBEGIN\s*$", up) or up.endswith(" BEGIN"):
            depth += 1
        if depth and (up == "END;" or up.endswith(" END;") or up.endswith("; END;")):
            depth -= 1
        if depth == 0 and s.endswith(";"):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def migrate(db_path, dry_run=False, mig_dir=None):
    mig_dir = mig_dir or os.path.join(ROOT, "db", "migrations")
    files = sorted(f for f in os.listdir(mig_dir) if re.match(r"\d{3}_.*\.sql$", f))
    con = sqlite3.connect(db_path)
    con.isolation_level = None
    con.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
    done = {r[0] for r in con.execute("SELECT version FROM schema_version")}
    todo = [f for f in files if int(f[:3]) not in done]
    if not todo:
        con.close()
        return []
    if dry_run:
        con.close()
        return todo
    bak = f"{db_path}.bak-{datetime.now():%Y%m%d%H%M%S}"
    shutil.copy2(db_path, bak)
    applied = []
    try:
        con.execute("BEGIN")
        for f in todo:
            for st in _statements(open(os.path.join(mig_dir, f), encoding="utf-8").read()):
                try:
                    con.execute(st)
                except sqlite3.OperationalError as e:
                    if "duplicate column name" in str(e):
                        continue
                    raise
            con.execute("INSERT INTO schema_version VALUES (?,?,?)",
                        (int(f[:3]), f, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            applied.append(f)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.close()
        print(f"迁移失败，已回滚；备份保留于 {bak}", file=sys.stderr)
        raise
    con.close()
    return applied


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    db = args[0] if args else os.path.join(ROOT, "db", "sim.db")
    r = migrate(db, "--dry-run" in sys.argv)
    print(("待应用：" if "--dry-run" in sys.argv else "已应用：") + (", ".join(r) if r else "无（已是最新）"))
