#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三重校验引擎测试：各层规则、合并优先级、风险卡算术、输入校验、无输入时零通过。
用法： python3 tests/test_screener.py （或 pytest）"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from engine import screener as S  # noqa: E402

TH = dict(S.DEFAULTS)
ASOF = "2026-10-23"          # 周五
TRACKED = {"002446", "688305", "HK9660", "BTCUSDT"}


def _t(**kw):
    t = {"date": ASOF, "bars": 80, "px": 10.0, "ma20": 9.5, "ma60": 9.0, "align": 1, "macd_hist": 0.1,
         "rsi": 55, "adx": 25, "pctb": 0.6, "atr": 0.3, "atr_pct": 3.0, "r1": 1.0, "r5": 3.0,
         "r20": 8.0, "dd20": -1.0}
    t.update(kw)
    return t


def _ctx(**kw):
    c = {"t": _t(), "stale_days": 0, "display_only": False, "strategy": "buyback",
         "buy_ok": True, "buy_errs": [], "suggest_qty": 300, "signal": None, "limit_pct": 0.10}
    c.update(kw)
    return c


def _brief(**kw):
    it = {"date": "2026-10-23", "source": "A", "target": "002446", "direction": "利好",
          "event_type": "公告", "fact": "中标", "evidence": ""}
    it.update(kw)
    return it


def _binfo(items, status="ok"):
    return {"status": status, "items": items, "errors": []}


def _sec(**kw):
    s = {"name": "商业航天", "aliases": [], "members": ["002446"], "rank_pct": 85.0,
         "ret_5d": 3.0, "ret_20d": 6.0, "breadth": 0.7}
    s.update(kw)
    return s


def _sinfo(secs, status="ok"):
    return {"status": status, "sectors": secs, "errors": []}


# ---------------- 第一层
def test_quant_pass_and_soft_fail():
    assert S.layer_quant(_ctx(), TH)["status"] == "PASS"
    q = S.layer_quant(_ctx(t=_t(rsi=78)), TH)
    assert q["status"] == "FAIL" and not q["hard_fail"]
    q = S.layer_quant(_ctx(t=_t(r5=20)), TH)
    assert q["status"] == "FAIL" and "5日涨幅过大" in q["tags"]
    q = S.layer_quant(_ctx(t=_t(macd_hist=-0.1, align=-1, px=9.0)), TH)
    assert q["status"] == "FAIL" and q["score"] < 1


def test_quant_hard_gates():
    for kw in (dict(display_only=True), dict(strategy="disabled"), dict(buy_ok=False, suggest_qty=0),
               dict(suggest_qty=0), dict(stale_days=5), dict(t=_t(bars=54)),
               dict(signal={"status": "已过期", "side": "BUY"}),
               dict(signal={"status": "待执行", "side": "SELL"}),
               dict(t=_t(r1=9.9))):
        q = S.layer_quant(_ctx(**kw), TH)
        assert q["status"] == "FAIL" and q["hard_fail"], kw
    assert S.layer_quant(_ctx(t=None), TH)["hard_fail"]


# ---------------- 第二层
def test_news_rules():
    ok = lambda items: S.layer_news("002446", [], _binfo(items), ASOF, TH)["status"]
    assert ok([_brief(evidence="巨潮公告")]) == "PASS"                       # 有佐证单源可过
    assert ok([_brief(), _brief(source="B")]) == "PASS"                     # 两个独立来源
    assert ok([_brief()]) == "FAIL"                                          # 单源无佐证
    assert ok([]) == "FAIL"                                                  # 无利好
    assert ok([_brief(evidence="公告"), _brief(direction="利空", fact="减持", evidence="公告")]) == "VETO"
    assert ok([_brief(evidence="公告"), _brief(direction="利空", fact="传闻", source="C")]) == "WAIT"
    assert ok([_brief(date="2026-10-15", evidence="公告")]) == "FAIL"        # 超出有效期
    assert ok([_brief(target="商业航天", evidence="公告")]) == "FAIL"        # 未传入板块名时不命中
    r = S.layer_news("002446", ["商业航天"], _binfo([_brief(target="商业航天", evidence="公告")]), ASOF, TH)
    assert r["status"] == "PASS"                                             # 主题简报命中成分股


def test_news_missing_inputs_wait():
    for st in ("missing", "stale", "invalid"):
        assert S.layer_news("002446", [], {"status": st, "items": [], "errors": []}, ASOF, TH)["status"] == "WAIT"


# ---------------- 第三层
def test_sector_rules():
    f = lambda s, t=None: S.layer_sector("002446", t or _t(), _sinfo([s]), TH)["status"]
    assert f(_sec()) == "PASS"
    assert f(_sec(rank_pct=60)) == "FAIL"
    assert f(_sec(ret_5d=-0.5)) == "FAIL"
    assert f(_sec(breadth=0.3)) == "FAIL"
    assert f(_sec(breadth=None)) == "PASS"                                   # 无广度字段不卡
    assert f(_sec(ret_20d=20.0)) == "FAIL"                                   # 个股掉队
    assert S.layer_sector("688305", _t(), _sinfo([_sec()]), TH)["status"] == "WAIT"   # 未映射
    for st in ("missing", "stale", "invalid"):
        assert S.layer_sector("002446", _t(), _sinfo([], st), TH)["status"] == "WAIT"


# ---------------- 合并
def test_combine_priority():
    qp = {"status": "PASS", "hard_fail": False, "reasons": ["ok"]}
    qh = {"status": "FAIL", "hard_fail": True, "reasons": ["仓位上限"]}
    qs = {"status": "FAIL", "hard_fail": False, "reasons": ["RSI超买"]}
    P, V, W, F = ({"status": x, "reasons": [x]} for x in ("PASS", "VETO", "WAIT", "FAIL"))
    assert S.combine(qp, P, P)[0] == "PASS"                                  # 三层全过才 PASS
    assert S.combine(qh, V, P)[0] == "FAIL"                                  # 硬门槛优先于否决
    assert S.combine(qp, V, P)[0] == "VETO"
    assert S.combine(qs, V, P)[0] == "VETO"                                  # 否决优先于技术软失败
    assert S.combine(qs, P, P)[0] == "FAIL"
    assert S.combine(qp, F, W)[0] == "FAIL"
    assert S.combine(qp, W, P)[0] == "WAIT"
    assert S.combine(qp, P, W)[0] == "WAIT"
    for a in (P, W, F, V):
        for b in (P, W, F, V):
            if not (a is P and b is P):
                assert S.combine(qp, a, b)[0] != "PASS"                      # 任一层非 PASS 绝不通过


# ---------------- 风险卡
def test_risk_card_math():
    t = _t(px=10.0, atr=0.5)
    rc = S.risk_card(t, 100000, 5000, 100, {"atr_stop_mult": 2.0, "single_trade_risk_pct": 1.0})
    assert abs(rc["stop"] - 9.0) < 1e-9
    assert rc["qty"] == 1000 and abs(rc["risk_amt"] - 1000) < 1e-6      # 100000×1% / 1元每股
    rc = S.risk_card(t, 100000, 300, 100, {"atr_stop_mult": 2.0, "single_trade_risk_pct": 1.0})
    assert rc["qty"] == 300                                                  # 系统建议更小则取系统
    assert S.risk_card(_t(atr=None), 100000, 300, 100, {}) is None
    assert S.risk_card(t, 0, 300, 100, {}) is None


# ---------------- 输入校验
def test_validate_and_bdays():
    items, errs = S.validate_briefs({"asof": ASOF, "items": [_brief(), {"x": 1}, _brief(direction="看多")]}, TRACKED)
    assert len(items) == 1 and len(errs) == 2
    assert S.validate_briefs({"items": []}, TRACKED)[0] is None
    secs, errs = S.validate_sectors({"asof": ASOF, "sectors": [
        {"name": "A", "rank_pct": 90, "ret_5d": 1, "members": ["2446", "000001"]},
        {"name": "B", "rank_pct": 150, "ret_5d": 1}]}, TRACKED)
    assert len(secs) == 1 and secs[0]["members"] == ["002446"] and len(errs) == 1
    assert S.bdays_between("2026-10-23", "2026-10-26") == 1                  # 周五→周一
    assert S.bdays_between("2026-10-26", "2026-10-23") == -1


def test_load_inputs_states():
    tmp = tempfile.mkdtemp()
    try:
        assert S.load_inputs(tmp, TRACKED, ASOF)["briefs"]["status"] == "missing"
        d = os.path.join(tmp, "data", "screen_inputs")
        os.makedirs(d)
        open(os.path.join(d, "briefs.json"), "w").write("{坏")
        assert S.load_inputs(tmp, TRACKED, ASOF)["briefs"]["status"] == "invalid"
        json.dump({"asof": "2026-10-01", "items": []}, open(os.path.join(d, "briefs.json"), "w"))
        assert S.load_inputs(tmp, TRACKED, ASOF)["briefs"]["status"] == "stale"
        json.dump({"asof": ASOF, "items": []}, open(os.path.join(d, "briefs.json"), "w"))
        assert S.load_inputs(tmp, TRACKED, ASOF)["briefs"]["status"] == "ok"
        # 示例文件绝不会被当真实输入加载
        shutil.copy(os.path.join(d, "briefs.json"), os.path.join(d, "briefs.example.json"))
        os.remove(os.path.join(d, "briefs.json"))
        assert S.load_inputs(tmp, TRACKED, ASOF)["briefs"]["status"] == "missing"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 端到端：无外部输入时零通过
def test_run_without_inputs_has_no_pass():
    import yaml
    tmp = tempfile.mkdtemp()
    try:
        shutil.copytree(os.path.join(ROOT, "db"), os.path.join(tmp, "db"))
        cfg = yaml.safe_load(open(os.path.join(ROOT, "config", "strategy.yaml"), encoding="utf-8"))
        res = S.run(os.path.join(tmp, "db", "sim.db"), cfg, tmp)
        assert res["rows"] and res["summary"].get("PASS", 0) == 0
        assert res["inputs"]["briefs"]["status"] == "missing"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("✅", name)
            except Exception as e:
                fails += 1
                print("❌", name, type(e).__name__, e)
    sys.exit(1 if fails else 0)
