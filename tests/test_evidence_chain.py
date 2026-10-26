#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证据链补齐专项测试（四项补齐① 甲，2026-10-27）。

覆盖：
- 甲1 新闻入账工具 tools/news_ingest.py：合法入账 + 回执留痕；
  非法条目矩阵逐项整批拒绝且账文件逐字不变；完全重复拒、同日
  同板块同源异证据允许（现存账形态）；market 条目 scope 规则；
  --check 只校验不写账。
- 甲2 母板映射核对 tools/check_workbook_map.py：真实封版母板与
  现行 workbook_map.json 推导一致（ok）；篡改一格 → diff 检出且
  现行文件不被改动（不自动覆盖）。
- 甲3 分项分数落库：screen_perf 新增 tech/news/board_score 三列，
  回填值与 screen_runs.results_json 逐项一致；回填不触碰
  screen_runs / 成交表（只加存储）；旧表缺列幂等补列；
  screen_all 同输入回放总分/评级/闸门/分项逐项不变（确定性对照）。

安全：写操作只在 tmp 副本；真实文件只读。运行:
.venv/bin/python -m pytest tests/test_evidence_chain.py -q
"""
import json
import os
import shutil
import sqlite3
import sys
from datetime import date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))

import news_ingest  # noqa: E402
import check_workbook_map  # noqa: E402

REAL_MAP = os.path.join(BASE, "config", "workbook_map.json")
REAL_XLSX = os.path.expanduser(
    "~/workspace/your_files/交易母版V8.2.1-Final.xlsx")
TODAY = date(2026, 10, 27)

GOOD = {"date": "2026-10-27", "sector_key": "semi", "direction": 1,
        "strength": 2, "source": "Serenity", "evidence": "测试证据甲",
        "tags": ["memory"]}


def _setup_book(tmp_path, entries=()):
    news = tmp_path / "sector_news.json"
    news.write_text(json.dumps({"entries": list(entries), "note": "t"},
                               ensure_ascii=False, indent=2,
                               sort_keys=True), encoding="utf-8")
    return str(news), str(tmp_path / "receipts.jsonl")


def _ingest(tmp_path, batch, news, receipt, **kw):
    return news_ingest.ingest(batch, news_path=news, map_path=REAL_MAP,
                              receipt_path=receipt, today=TODAY, **kw)


# ---------------- 甲1：新闻入账 ----------------

def test_news_ingest_happy_and_receipt(tmp_path):
    news, receipt = _setup_book(tmp_path)
    res = _ingest(tmp_path, [GOOD], news, receipt)
    assert res["ok"] and res["added"] == 1 and res["total"] == 1
    assert res["latest_date"] == "2026-10-27"
    book = json.loads(open(news, encoding="utf-8").read())
    assert book["entries"][0]["evidence"] == "测试证据甲"
    assert book["note"] == "t"  # 原 note 保留
    lines = open(receipt, encoding="utf-8").read().strip().split("\n")
    rec = json.loads(lines[-1])
    assert rec["status"] == "ok" and rec["added"] == 1
    assert rec["sources"] == ["Serenity"] and rec["entries_sha256"]


def test_news_ingest_check_only_writes_nothing(tmp_path):
    news, receipt = _setup_book(tmp_path)
    before = open(news, "rb").read()
    res = _ingest(tmp_path, [GOOD], news, receipt, check_only=True)
    assert res["ok"]
    assert open(news, "rb").read() == before


def test_news_ingest_rejects_bad_entries(tmp_path):
    news, receipt = _setup_book(tmp_path)
    before = open(news, "rb").read()
    bad_variants = [
        dict(GOOD, sector_key="no_such_sector"),     # 白名单外
        dict(GOOD, direction=0),                      # direction 非 ±1
        dict(GOOD, direction=True),                   # bool 不算整数
        dict(GOOD, strength=0),                       # strength 越界
        dict(GOOD, strength=4),
        dict(GOOD, date="2026-10-28"),                # 晚于当日
        dict(GOOD, date="2026-13-01"),                # 非真实日期
        dict(GOOD, source="Bloomberg"),               # 来源枚举外
        dict(GOOD, evidence="  "),                   # 证据为空
        dict(GOOD, tags="memory"),                    # tags 非数组
        dict(GOOD, scope="sector"),                   # scope 非法值
        dict(GOOD, sector_key="market"),              # market 缺 scope
    ]
    for i, bad in enumerate(bad_variants):
        res = _ingest(tmp_path, [bad], news, receipt)
        assert not res["ok"], (i, bad, res)
        assert res["errors"], (i, bad)
    assert open(news, "rb").read() == before  # 整批拒绝，账文件逐字不动
    recs = [json.loads(x) for x in
            open(receipt, encoding="utf-8").read().strip().split("\n")]
    assert all(r["status"] == "rejected" for r in recs)


def test_news_ingest_dedup_rules(tmp_path):
    news, receipt = _setup_book(tmp_path, entries=[GOOD])
    # 与账内完全重复 → 拒
    res = _ingest(tmp_path, [dict(GOOD)], news, receipt)
    assert not res["ok"] and "完全重复" in res["errors"][0]
    # 同日同板块同源、证据不同 → 允许（现存账即有此形态）
    res = _ingest(tmp_path, [dict(GOOD, evidence="另一条不同证据")],
                  news, receipt)
    assert res["ok"] and res["total"] == 2
    # 批内两条完全重复 → 整批拒
    e = dict(GOOD, evidence="第三条证据")
    res = _ingest(tmp_path, [e, dict(e)], news, receipt)
    assert not res["ok"]


def test_news_ingest_market_and_secondary_sectors(tmp_path):
    news, receipt = _setup_book(tmp_path)
    mkt = {"date": "2026-10-27", "sector_key": "market", "direction": -1,
           "strength": 1, "source": "Serenity", "scope": "market_risk",
           "evidence": "市场级去杠杆风险提示"}
    assert _ingest(tmp_path, [mkt], news, receipt)["ok"]
    # semi_equip 是新闻侧独有板块（Danny 历史条目口径），在白名单内
    equip = {"date": "2026-10-27", "sector_key": "semi_equip",
             "direction": -1, "strength": 2, "source": "Musk",
             "evidence": "设备板块测试证据"}
    assert _ingest(tmp_path, [equip], news, receipt)["ok"]


# ---------------- 甲2：母板映射核对 ----------------

def test_workbook_map_check_consistent_with_sealed_master():
    ok, diffs, summary = check_workbook_map.run(
        xlsx=REAL_XLSX, map_path=REAL_MAP)
    assert ok, diffs[:5]
    assert summary["symbols"] == 16 and summary["sectors"] == 11


def test_workbook_map_check_detects_tamper_without_overwrite(tmp_path):
    m = json.loads(open(REAL_MAP, encoding="utf-8").read())
    m["symbols"]["002446"]["stop_ref"] = 1.23
    p = tmp_path / "tampered_map.json"
    p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    before = p.read_bytes()
    ok, diffs, _ = check_workbook_map.run(xlsx=REAL_XLSX,
                                          map_path=str(p))
    assert not ok
    assert any("symbols.002446.stop_ref" in d for d in diffs), diffs[:5]
    assert p.read_bytes() == before  # 核对绝不自动覆盖现行 json


# ---------------- 甲3：分项分数落库 ----------------

def _perf_fixture_db(tmp_path, with_old_perf=False):
    db = str(tmp_path / "perf.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE market_data(symbol TEXT, date TEXT,"
                " open REAL, high REAL, low REAL, close REAL,"
                " volume REAL, market TEXT)")
    closes = [10.0 + i * 0.1 for i in range(30)]
    from datetime import timedelta
    rows = [("002446", (date(2026, 9, 28) + timedelta(days=i)).isoformat(),
             c, c, c, c, 1000.0, "a") for i, c in enumerate(closes)]
    con.executemany("INSERT INTO market_data VALUES(?,?,?,?,?,?,?,?)",
                    rows)
    results = [{"symbol": "002446", "grade": "A", "total": 37.75,
                "close": 10.0, "tech": 20.5, "news": 10.25, "board": 7.0,
                "capped": False, "gates": []}]
    con.execute("CREATE TABLE screen_runs(id INTEGER PRIMARY KEY,"
                " run_at TEXT, asof TEXT, rule_version TEXT,"
                " news_sha256 TEXT, map_sha256 TEXT, results_json TEXT)")
    con.execute("INSERT INTO screen_runs VALUES(1,'2026-10-25 16:05',"
                "'2026-10-25','screen-v1.6-test','n','m',?)",
                (json.dumps(results, ensure_ascii=False),))
    con.execute("CREATE TABLE fills(id INTEGER PRIMARY KEY, date TEXT)")
    if with_old_perf:
        # 旧版 screen_perf（无分项三列）→ 验证幂等补列
        con.execute("CREATE TABLE screen_perf(run_id INTEGER NOT NULL,"
                    " asof TEXT NOT NULL, symbol TEXT NOT NULL,"
                    " grade TEXT, total REAL, base_close REAL,"
                    " capped INTEGER DEFAULT 0, gates TEXT, grp TEXT,"
                    " fwd5_date TEXT, fwd5_ret REAL, fwd5_excess REAL,"
                    " fwd20_date TEXT, fwd20_ret REAL, fwd20_excess REAL,"
                    " updated_at TEXT, PRIMARY KEY(run_id, symbol))")
    con.commit()
    con.close()
    return db


def _run_perf(db):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "screen_perf_tool", os.path.join(BASE, "tools", "screen_perf.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run(db)


def test_screen_perf_component_scores_stored(tmp_path):
    db = _perf_fixture_db(tmp_path)
    runs_before = sqlite3.connect(db).execute(
        "SELECT results_json FROM screen_runs").fetchall()
    _run_perf(db)
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT tech_score, news_score, board_score, grade, total"
        " FROM screen_perf WHERE symbol='002446'").fetchone()
    runs_after = con.execute(
        "SELECT results_json FROM screen_runs").fetchall()
    fills_n = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
    con.close()
    assert row == (20.5, 10.25, 7.0, "A", 37.75), row
    assert runs_before == runs_after  # 只读 screen_runs，不改一行
    assert fills_n == 0               # 不触碰成交表


def test_screen_perf_component_scores_migrate_old_table(tmp_path):
    db = _perf_fixture_db(tmp_path, with_old_perf=True)
    _run_perf(db)
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(screen_perf)")}
    row = con.execute(
        "SELECT tech_score, news_score, board_score FROM screen_perf"
        ).fetchone()
    con.close()
    assert {"tech_score", "news_score", "board_score"} <= cols
    assert row == (20.5, 10.25, 7.0)


def test_screen_all_replay_deterministic(tmp_path):
    """甲3 对照：同一输入回放 screen_all 两次，总分/评级/闸门/三项
    分项逐项全等（加存储不得改变评分口径）。生产库副本只读数据，
    record=False 不落库。"""
    from engine.screen import screen_all
    db = str(tmp_path / "prod_copy.db")
    shutil.copy2(os.path.join(BASE, "db", "sim.db"), db)
    runs = []
    for _ in range(2):
        rows = screen_all(db, "2026-10-26", record=False)
        runs.append([(r["symbol"], r["total"], r["grade"],
                      tuple(r["gates"]), r["tech"], r["news"], r["board"])
                     for r in rows])
    assert runs[0] == runs[1]
    assert len(runs[0]) == 16
