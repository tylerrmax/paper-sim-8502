# -*- coding: utf-8 -*-
"""机构影子链补齐验收测试（2026-10-30 方案2：自动抓取 + 只读视图）。

覆盖验收项：重复运行不重复累计（收敛）、失败原因落库且成功日
不被降级、ingest_recent 目标日选择（最近 A 股交易日）、空数据
守卫（缺失不当零）、失败场景隔离（只登记失败日）。"""
import sqlite3
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine import inst_lhb  # noqa: E402


def _con(tmp_path, name="t.db"):
    con = sqlite3.connect(str(tmp_path / name))
    con.execute("CREATE TABLE market_data(symbol TEXT, date TEXT,"
                " open REAL, high REAL, low REAL, close REAL,"
                " volume REAL, market TEXT)")
    return con


def _seed_a_days(con, dates):
    for d in dates:
        con.execute("INSERT INTO market_data VALUES"
                    "('002446',?,1,1,1,1,1,'a')", (d,))
    con.commit()


def _good_get(url):
    if "DAILYDETAILSBUY" in url:
        return {"result": {"pages": 1, "data": [
            {"OPERATEDEPT_NAME": "机构专用", "SECURITY_CODE": "002446",
             "BUY": 5000, "SELL": 0, "NET": 5000, "TRADE_ID": "T1",
             "EXPLANATION": "日涨幅偏离值达到7%的前5只证券"}]}}
    if "DAILYDETAILSSELL" in url:
        return {"result": {"pages": 1, "data": []}}
    return {"result": {"pages": 1, "data": [
        {"SECURITY_CODE": "002446", "SECURITY_NAME_ABBR": "盛路通信"}]}}


def _empty_get(url):
    return {"result": {"pages": 1, "data": []}}


def _boom_get(url):
    raise TimeoutError("timeout")


def test_store_daily_repeat_converges(tmp_path):
    con = _con(tmp_path)
    rows2 = [{"symbol": "002446", "name": "盛路通信", "inst_buy": 100,
              "inst_sell": 0, "inst_net": 100},
             {"symbol": "600150", "name": "中国船舶", "inst_buy": 50,
              "inst_sell": 0, "inst_net": 50}]
    raw3 = [{"_kind": "buy", "SECURITY_CODE": "002446",
             "OPERATEDEPT_NAME": "机构专用", "BUY": 1, "SELL": 0,
             "NET": 1}] * 3
    inst_lhb.store_daily(con, "2026-10-29", rows2, raw3)
    # 同日重复抓取且数据源修订为更少条数：以最新一次为准，不累计
    inst_lhb.store_daily(con, "2026-10-29", rows2[:1], raw3[:1])
    assert con.execute("SELECT COUNT(*) FROM inst_lhb_daily"
                       " WHERE date='2026-10-29'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM inst_lhb_raw"
                       " WHERE date='2026-10-29'").fetchone()[0] == 1
    assert con.execute("SELECT agg_rows,raw_rows FROM"
                       " inst_lhb_fetch_days WHERE date='2026-10-29'"
                       ).fetchone() == (1, 1)


def test_failure_reason_stored_and_success_not_downgraded(tmp_path):
    con = _con(tmp_path)
    inst_lhb.record_fetch_failure(con, "2026-10-28", "接口超时")
    row = con.execute("SELECT state,reason FROM inst_lhb_fetch_days"
                      " WHERE date='2026-10-28'").fetchone()
    assert row[0] == "failed" and "接口超时" in (row[1] or "")
    inst_lhb.store_daily(con, "2026-10-29", [
        {"symbol": "002446", "name": "盛路通信", "inst_buy": 1,
         "inst_sell": 0, "inst_net": 1}])
    inst_lhb.record_fetch_failure(con, "2026-10-29", "迟到的失败")
    assert con.execute("SELECT state FROM inst_lhb_fetch_days"
                       " WHERE date='2026-10-29'"
                       ).fetchone()[0] == "normal"


def test_ingest_recent_targets_and_empty_guard(tmp_path):
    con = _con(tmp_path)
    _seed_a_days(con, ["2026-10-27", "2026-10-28", "2026-10-29"])
    out = inst_lhb.ingest_recent(con, get=_good_get, days=2)
    # 只抓最近两个 A 股交易日
    assert [d for d, _, _ in out] == ["2026-10-28", "2026-10-29"]
    assert all(s == "normal" for _, s, _ in out)
    assert inst_lhb.latest_success_date(con) == "2026-10-29"
    # 空数据守卫（已有好数据的日子）：不覆盖、不降级
    out = inst_lhb.ingest_recent(con, get=_empty_get, days=1)
    assert out[0][1] == "failed" and "空数据" in out[0][2]
    assert con.execute("SELECT COUNT(*) FROM inst_lhb_daily"
                       " WHERE date='2026-10-29'").fetchone()[0] == 1
    assert con.execute("SELECT state FROM inst_lhb_fetch_days"
                       " WHERE date='2026-10-29'"
                       ).fetchone()[0] == "normal"
    # 空数据守卫（新日子）：按失败登记、带原因、不落零数据
    _seed_a_days(con, ["2026-10-30"])
    out = inst_lhb.ingest_recent(con, get=_empty_get, days=1)
    assert out[0][0] == "2026-10-30" and out[0][1] == "failed"
    st = inst_lhb.recent_fetch_status(con, limit=1)[0]
    assert st["date"] == "2026-10-30" and st["state"] == "failed"
    assert "空数据" in st["reason"]
    assert con.execute("SELECT COUNT(*) FROM inst_lhb_daily"
                       " WHERE date='2026-10-30'").fetchone()[0] == 0


def test_ingest_recent_failure_isolated(tmp_path):
    con = _con(tmp_path)
    _seed_a_days(con, ["2026-10-29"])
    out = inst_lhb.ingest_recent(con, get=_boom_get, days=2)
    assert out and out[0][1] != "normal"
    assert con.execute("SELECT COUNT(*) FROM inst_lhb_daily"
                       ).fetchone()[0] == 0
    st = inst_lhb.recent_fetch_status(con, limit=1)[0]
    assert st["state"] == "failed" and st["reason"]


def test_ingest_recent_no_a_share_days(tmp_path):
    con = _con(tmp_path)
    assert inst_lhb.ingest_recent(con, get=_good_get, days=2) == []
    assert inst_lhb.recent_fetch_status(con) == []
