# -*- coding: utf-8 -*-
"""Market-First P0-4：研发缓存与生产共享缓存隔离。

断言：bars.get_bars 调用前后生产共享 cache.db 的 SHA256 不变；
取到的 bars 写入研发自有 fetched_bars 表；同日重跑命中自有
缓存（provenance 可区分）。
"""
import hashlib
import sqlite3

from market_first import bars as bars_mod

PROD_CACHE = "/home/hatch/workspace/market_data/cache.db"


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def test_get_bars_never_touches_prod_shared_cache(tmp_path):
    db = str(tmp_path / "mf.db")
    before = _sha(PROD_CACHE)
    # 002446 在生产 sim.db market_data 有回退数据，主源亦可达，
    # 任一路径都不得读写生产共享 cache.db
    bars, prov = bars_mod.get_bars("002446", "2026-10-01",
                                   "2026-11-13", db_path=db)
    assert _sha(PROD_CACHE) == before
    assert bars, "002446 应有 bars（主源或 sim.db 回退）"
    # 研发自有缓存已写入
    conn = sqlite3.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM fetched_bars WHERE symbol='002446'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n > 0
    # 同日重跑命中自有缓存，结果一致且仍不触生产缓存
    bars2, prov2 = bars_mod.get_bars("002446", "2026-10-01",
                                     "2026-11-13", db_path=db)
    assert prov2 == bars_mod.PROV_OWN_CACHE
    assert bars2 == bars
    assert _sha(PROD_CACHE) == before
