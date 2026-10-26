#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周度一致性简报回归（验证期收尾件二，2026-10-27）。

覆盖 tools/weekly_consistency.py 的三条核心契约：
1. 正常周：build_brief 输出六段齐全、无一致性异常，且抽查记录
   一行落入 consistency_checks.jsonl；
2. 篡改一期落库分数（技术分）后，复现抽查必须 FAIL 并点名标的；
3. 新闻账被改（news_sha256 与当前账不一致）时，新闻分/总分差异
   按「不可复现比对」标注，不得误判 FAIL。

fixture 库为全合成数据：market_data 仅含指数与两只标的的合成
日线，stored run 由现行 engine.screen.screen_all 真实生成，
保证抽查比对的是同一套评分代码的前后两次运行。
"""
import json
import os
import sqlite3
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.screen import _sha256_obj, screen_all  # noqa: E402
from tools.weekly_consistency import build_brief, reproduce_check  # noqa: E402
import release_gate  # noqa: E402

ASOF = "2026-10-23"
TODAY = date(2026, 10, 26)  # 周一，统计窗 10-20 ~ 10-26

ENTRIES = [{"date": "2026-10-22", "direction": 1, "evidence": "测试证据",
            "sector_key": "semi", "source": "Serenity", "strength": 3,
            "tags": []}]


def _bars(con, symbol, end, n=90, base=100.0):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    for i, d in enumerate(sorted(days)):
        c = base * (1 + 0.001 * i)
        con.execute(
            "INSERT INTO market_data VALUES(?,?,?,?,?,?,?)",
            (symbol, d.isoformat(), c, c * 1.01, c * 0.99, c, 1000000))


def _mk_db(tmp_path):
    db = str(tmp_path / "sim.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE market_data(symbol TEXT, date TEXT,"
                " open REAL, high REAL, low REAL, close REAL, volume REAL)")
    end = date(2026, 10, 23)
    _bars(con, "IDX000001", end, base=3000.0)
    _bars(con, "002446", end, base=10.0)
    _bars(con, "600150", end, base=30.0)
    con.execute("CREATE TABLE fills(id INTEGER PRIMARY KEY, date TEXT,"
                " account TEXT, symbol TEXT, side TEXT, qty REAL,"
                " price REAL, fee REAL, pnl REAL, strategy_version TEXT)")
    con.execute("INSERT INTO fills VALUES(1,'2026-10-09','sim_a','002446',"
                "'BUY',100,10.0,5.0,0,'v1.1-2026-10-26')")
    con.execute("INSERT INTO fills VALUES(2,'2026-10-12','sim_a','600150',"
                "'BUY',100,30.0,5.0,0,'v1.1-2026-10-26')")
    con.execute("CREATE TABLE daily_report(date TEXT, account TEXT,"
                " symbol TEXT, nav REAL)")
    for i in range(7):
        d = (date(2026, 10, 20) + timedelta(days=i)).isoformat()
        con.execute("INSERT INTO daily_report VALUES(?,'sim_a','TOTAL',"
                    "100000)", (d,))
    con.execute("CREATE TABLE graduation_exclusions(date TEXT,"
                " account TEXT, symbol TEXT, side TEXT)")
    con.execute("CREATE TABLE blocked_signals(date TEXT, signal_date TEXT,"
                " account TEXT, symbol TEXT, side TEXT, signal_price REAL,"
                " exec_price REAL, stop_ref REAL, reason TEXT,"
                " created_at TEXT)")
    con.execute("INSERT INTO blocked_signals VALUES('2026-10-24',"
                "'2026-10-23','sim_a','002185','BUY',16.19,16.19,16.30,"
                "'禁入闸','2026-10-24 09:12:00')")
    con.execute("CREATE TABLE blocked_perf(date TEXT, account TEXT,"
                " symbol TEXT, side TEXT, signal_date TEXT,"
                " base_price REAL, fwd5_date TEXT, fwd5_ret REAL,"
                " fwd20_date TEXT, fwd20_ret REAL, updated_at TEXT)")
    con.execute("INSERT INTO blocked_perf VALUES('2026-10-24','sim_a',"
                "'002185','BUY','2026-10-23',16.19,'2026-10-30',0.05,"
                "NULL,NULL,'x')")
    con.execute("INSERT INTO blocked_perf VALUES('2026-10-24','sim_a',"
                "'002446','BUY','2026-10-23',10.0,'2026-10-30',-0.01,"
                "NULL,NULL,'x')")
    con.execute("CREATE TABLE screen_perf(run_id INTEGER, asof TEXT,"
                " symbol TEXT, grade TEXT, total REAL, base_close REAL,"
                " capped INTEGER DEFAULT 0, gates TEXT, grp TEXT,"
                " fwd5_date TEXT, fwd5_ret REAL, fwd5_excess REAL,"
                " fwd20_date TEXT, fwd20_ret REAL, fwd20_excess REAL,"
                " updated_at TEXT)")
    con.execute("INSERT INTO screen_perf VALUES(1,'2026-10-16','S1','A',"
                "70,10,0,NULL,'sa','2026-10-23',0.03,NULL,NULL,NULL,NULL,'x')")
    con.execute("INSERT INTO screen_perf VALUES(1,'2026-10-16','S2','B',"
                "70,10,1,'[\"x\"]','shadow','2026-10-23',-0.02,NULL,NULL,"
                "NULL,NULL,'x')")
    con.execute("INSERT INTO screen_perf VALUES(1,'2026-10-16','S3','B',"
                "40,10,0,NULL,'other',NULL,NULL,NULL,NULL,NULL,NULL,'x')")
    con.commit()
    con.close()
    # 用现行评分代码真实生成一期落库（抽查的比对基准）
    screen_all(db_path=db, asof=ASOF, entries=ENTRIES, record=True)
    return db


def _mk_base(tmp_path, db):
    base = tmp_path / "base"
    (base / "reports").mkdir(parents=True)
    (base / "config").mkdir()
    (base / "config" / "sector_news.json").write_text(
        json.dumps({"entries": ENTRIES}, ensure_ascii=False),
        encoding="utf-8")
    release_gate.init_watermark(db, str(base / ".release_gate_state.json"))
    (base / "reports" / "release_gate_log.jsonl").write_text(
        '{"ts": "2026-10-26 17:30:00", "event": "pass", "path": "B"}\n'
        '{"ts": "2026-10-25 17:30:00", "event": "check_only_pass",'
        ' "path": "B"}\n', encoding="utf-8")
    return base


def test_brief_normal_week(tmp_path):
    db = _mk_db(tmp_path)
    base = _mk_base(tmp_path, db)
    brief = build_brief(db_path=db, base=str(base), today=TODAY)
    for marker in ("## 8502 周度一致性简报", "1. 运行可靠", "2. 账本完整",
                   "3. 样本积累", "4. 风险纪律", "5. 复现抽查", "6. 事故"):
        assert marker in brief, marker
    assert "日报覆盖 7/7 天" in brief
    assert "指纹水位线一致" in brief
    assert "影子样本已到期 sa 1 / shadow 1 / other 0" in brief
    assert "疑似误杀 1/2" in brief
    assert "禁入拦截新增 1 条" in brief
    assert "全部一致 ✅" in brief
    assert "本周无一致性异常" in brief
    lines = (base / "reports" / "consistency_checks.jsonl") \
        .read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["asof"] == ASOF and rec["result"] == "PASS"


def test_reproduce_detects_tampered_score(tmp_path):
    db = _mk_db(tmp_path)
    con = sqlite3.connect(db)
    rid, rj = con.execute(
        "SELECT id, results_json FROM screen_runs").fetchone()
    rows = json.loads(rj)
    rows[0]["tech"] = float(rows[0]["tech"]) + 5.0
    rows[0]["total"] = float(rows[0]["total"]) + 5.0
    con.execute("UPDATE screen_runs SET results_json=? WHERE id=?",
                (json.dumps(rows, ensure_ascii=False, sort_keys=True), rid))
    con.commit()
    con.close()
    txt, rec, anomaly = reproduce_check(
        db, str(tmp_path / "checks.jsonl"), ENTRIES, _sha256_obj(ENTRIES))
    assert rec["result"] == "FAIL"
    assert rec["diff_count"] >= 1
    assert rows[0]["symbol"] in txt
    assert anomaly is not None and "不一致" in anomaly


def test_reproduce_news_sha_change_not_fail(tmp_path):
    db = _mk_db(tmp_path)
    # 模拟新闻账被编辑：落库 sha 改掉且落库新闻分/总分随之而变；
    # 重跑用现行账，新闻分/总分必然不同——但 sha 不同 ⇒ 不可比对，
    # 技术/母板/封顶未动 ⇒ 不得判 FAIL。
    con = sqlite3.connect(db)
    rid, rj = con.execute(
        "SELECT id, results_json FROM screen_runs").fetchone()
    rows = json.loads(rj)
    for r in rows:
        r["news"] = float(r["news"]) + 7.0
        r["total"] = float(r["total"]) + 7.0
    con.execute(
        "UPDATE screen_runs SET results_json=?, news_sha256='deadbeef'"
        " WHERE id=?",
        (json.dumps(rows, ensure_ascii=False, sort_keys=True), rid))
    con.commit()
    con.close()
    txt, rec, anomaly = reproduce_check(
        db, str(tmp_path / "checks.jsonl"), ENTRIES, _sha256_obj(ENTRIES))
    assert rec["result"] == "PASS"
    assert anomaly is None
    assert "新闻输入已变更" in txt
    assert "不可复现比对" in txt
