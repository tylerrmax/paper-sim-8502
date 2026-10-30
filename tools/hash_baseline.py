#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
表级哈希基线 · 序列化规范 V1（2026-10-31 专项整改 P2-07 固定）。

规范版本：hash-spec-v1，生效时间：2026-10-31。
本规范由 2026-10-30 样本登记基线的实际计算方法逆向复原并逐字
验证（inst_lhb_daily / inst_lhb_raw / inst_lhb_fetch_days 三表
哈希与登记值全等），自生效起为唯一口径；历史基线即按本规范
计算，不存在静默替换。后续任何变更必须升版本号（V2…）并保留
V1 复算能力，不得改写历史基线。

序列化规则（V1，全文）：
- 表名：由调用方指定；列集合 = SELECT * 的表定义列序
  （PRAGMA table_info 顺序），不含列名行（无表头）；
- 行排序：按表主键升序（调用方以 --order-by 显式给出，与
  基线登记一致：daily=date,symbol；raw=date,kind,seq；
  fetch_days=date；账本表可用 rowid 并在登记中注明）；
- 字段分隔符：单个半角竖线 "|"；行终止符：单个换行 "\n"
  （每行含末行都以 "\n" 结尾，等价于逐行拼接后 sha256）；
- NULL 表示：空字符串 ""；
- 数值格式：Python str()（整数十进制；浮点为最短往返表示，
  如 5000.0 → "5000.0"，-4323.26 → "-4323.26"）；
- 日期时间格式：库内存储的原始文本（YYYY-MM-DD /
  YYYY-MM-DD HH:MM:SS），不做时区转换；
- 文本：原始 UTF-8 文本，不转义、不裁剪；
- 编码：UTF-8；算法：SHA256（hex 小写）。

已知限制（V1 如实登记）：文本字段若含 "|" 或 "\n" 会产生
拼接歧义——当前三表与账本字段经核查无此情形；未来若出现，
须升 V2（转义方案）并重建基线，不得在 V1 内悄悄改规则。

用法（只读打开数据库，绝不写库）：
  .venv/bin/python tools/hash_baseline.py --db db/sim.db \
      --table inst_lhb_daily --order-by "date, symbol"
输出一行 JSON：{"table":…, "rows":…, "sha256":…, "spec":"hash-spec-v1"}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3

SPEC_VERSION = "hash-spec-v1"


def serialize_value(v):
    """V1 值序列化：NULL→空字符串，其余 str()。"""
    return "" if v is None else str(v)


def table_hash(con, table, order_by):
    """按 V1 规范计算表哈希，返回 (rows, sha256hex)。"""
    rows = con.execute(
        "SELECT * FROM %s ORDER BY %s" % (table, order_by)).fetchall()
    buf = "".join(
        "|".join(serialize_value(v) for v in r) + "\n" for r in rows)
    return len(rows), hashlib.sha256(buf.encode("utf-8")).hexdigest()


def main():
    ap = argparse.ArgumentParser(description="表级哈希基线（hash-spec-v1）")
    ap.add_argument("--db", required=True)
    ap.add_argument("--table", required=True)
    ap.add_argument("--order-by", required=True)
    a = ap.parse_args()
    con = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    n, h = table_hash(con, a.table, a.order_by)
    print(json.dumps({"table": a.table, "rows": n, "sha256": h,
                      "spec": SPEC_VERSION}, ensure_ascii=False))


if __name__ == "__main__":
    main()
