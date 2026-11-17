#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Forward benchmark 独立核对（不依赖 validation.create_sample）：
直读指定库 validation_samples 全部样本，逐一：
1. 经 market_first.bars 取 bars（use_cache=False 研发通路），
   核对 benchmark_price == entry 日收盘（容差 1e-9）；
2. 以本脚本内独立重写的时间规则（不调用 bar_close_confirmed）
   复核：以样本 created_at 为取数时刻，entry 日 bar 在该时刻
   是否已收盘确认（历史样本应全部确认）；
3. 打印 benchmark_revision_json 现状（应为 NULL 或含修订记录）。
只读：样本库以 mode=ro 打开，bars 层只写研发自有 fetched_bars。
"""
import json
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, "/home/hatch/workspace/l2-remediation/market-first")
from market_first.bars import get_bars  # noqa: E402

SH = "+08:00"


def confirmed_inline(bar_date, fetched_at, market=None):
    """独立重写的收盘确认规则（与 fix2-1 口径一致）：
    bar 日 < 取数日 -> True；> -> False；同日须取数时刻 >=
    收盘确认时刻（HK 16:30，其余 15:30）。"""
    fdt = datetime.fromisoformat(str(fetched_at))
    fd = fdt.date().isoformat()
    bd = str(bar_date)[:10]
    if bd < fd:
        return True
    if bd > fd:
        return False
    cutoff = "16:30" if str(market or "").upper() == "HK" else "15:30"
    return fdt.strftime("%H:%M") >= cutoff


SCRATCH_BARS_DB = "/tmp/fix2_bars_cache.db"


def main(db_path):
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    conn.row_factory = sqlite3.Row
    samples = conn.execute(
        "SELECT s.*, c.market, c.symbol FROM validation_samples s"
        " JOIN candidates c ON c.id = s.candidate_id"
        " ORDER BY s.id").fetchall()
    print("DB=%s samples=%d" % (db_path, len(samples)))
    all_ok = True
    for s in samples:
        # bars 经研发通路现取（use_cache=False；研发缓存写
        # /tmp 暂存库，不触碰被核对库与生产缓存）
        rows, _prov = get_bars(s["symbol"], s["entry_date"],
                               "2026-12-31", db_path=SCRATCH_BARS_DB)
        close_on_entry = None
        for b in rows:
            if str(b[0])[:10] == s["entry_date"]:
                close_on_entry = float(b[4])
                break
        price_ok = (close_on_entry is not None
                    and abs(close_on_entry - s["benchmark_price"]) < 1e-9)
        conf_ok = confirmed_inline(s["entry_date"], s["created_at"],
                                   s["market"])
        rev = s["benchmark_revision_json"] \
            if "benchmark_revision_json" in s.keys() else "<no column>"
        ok = price_ok and conf_ok
        all_ok = all_ok and ok
        print("sample id=%s %s %s kind=%s entry=%s benchmark=%s "
              "close_on_entry=%s price_match=%s "
              "confirmed_at_created=%s revision=%s -> %s"
              % (s["id"], s["market"], s["symbol"], s["kind"],
                 s["entry_date"], s["benchmark_price"], close_on_entry,
                 price_ok, conf_ok,
                 (rev[:60] + "...") if isinstance(rev, str) and rev
                 else rev,
                 "OK" if ok else "FAIL"))
    print("OVERALL:", "OK" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
