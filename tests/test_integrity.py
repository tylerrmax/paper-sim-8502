#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""完整性工具测试：入库闸门、一致性检查、迁移与触发器、重建演练、部署清单、原子同步（假 API）。
用法： python3 tests/test_integrity.py （或 pytest）"""
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tools")):
    sys.path.insert(0, p)
from engine import data_gate as G  # noqa: E402
import check_consistency as CC      # noqa: E402
import migrate as MG                # noqa: E402
import rebuild_from_fills as RB     # noqa: E402
import sync_manifest as SM          # noqa: E402
import sync_atomic as SA            # noqa: E402

CFG = """accounts:
  - {id: sim_a, ccy: CNY, principal: 100000}
"""


def _dates(n, end=None):
    end = end or datetime.now().date()
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return out[::-1]


def _bars(n=30, vol=1000.0, price=10.0):
    return [(d, price, price + 0.2, price - 0.2, price + 0.1, vol) for d in _dates(n)]


def make_src(tmp, with_fill=True, nav_ok=True, volume_shift=False):
    """合成一个完整的 SRC 目录：schema + config + 数据。"""
    os.makedirs(os.path.join(tmp, "db"))
    os.makedirs(os.path.join(tmp, "config"))
    open(os.path.join(tmp, "config", "strategy.yaml"), "w", encoding="utf-8").write(CFG)
    con = sqlite3.connect(os.path.join(tmp, "db", "sim.db"))
    con.executescript(open(os.path.join(ROOT, "db", "schema.sql"), encoding="utf-8").read())
    bars = _bars(30)
    if volume_shift:
        bars = bars[:20] + [(d, o, h, l, c, v * 100) for d, o, h, l, c, v in bars[20:]]
    con.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?)",
                    [("000001", b[0], b[1], b[2], b[3], b[4], b[5], "a") for b in bars])
    last = bars[-1][0]
    cash, qty = 100000.0, 0.0
    if with_fill:
        d0 = bars[-3][0]
        con.execute("INSERT INTO fills(date,account,symbol,side,qty,price,fee,pnl,signal_date) VALUES (?,?,?,?,?,?,?,?,?)",
                    (d0, "sim_a", "000001", "BUY", 1000, 10.0, 5.0, 0, bars[-4][0]))
        con.execute("INSERT INTO signals(date,account,symbol,side,price,status) VALUES (?,?,?,?,?,?)",
                    (bars[-4][0], "sim_a", "000001", "BUY", 10.0, "已成交"))
        cash, qty = 100000.0 - 10005.0, 1000.0
        con.execute("INSERT INTO positions VALUES ('sim_a','000001',?,?,?,?)", (qty, 10.005, 10.1, qty * 10.1))
    mv = qty * 10.1
    con.execute("INSERT INTO daily_report VALUES (?,?,?,?,?,?,?,?)",
                (last, "sim_a", "TOTAL", cash + mv + (0 if nav_ok else 500), cash, mv, 0, 0))
    con.commit()
    con.close()
    return tmp


# ---------------- 入库闸门
def test_gate_bad_bars_and_dupes():
    b = _bars(10)
    bad = b + [(b[-1][0], 10, 10, 10, 10, 1)]                         # 重复日期
    assert any(i["rule"] == "duplicate_date" for i in G.check_bars("X", bad))
    ohlc = b[:-1] + [(b[-1][0], 10, 9, 11, 10, 100)]                  # high<low
    assert any(i["rule"] == "ohlc_inconsistent" and i["severity"] == "error" for i in G.check_bars("X", ohlc))
    neg = b[:-1] + [(b[-1][0], 10, 11, 9, 10, -5)]
    assert any(i["rule"] == "bad_value" for i in G.check_bars("X", neg))


def test_gate_isolated_outlier_vs_unit_shift():
    b = _bars(30, vol=1000)
    one = b[:25] + [(b[25][0],) + b[25][1:5] + (200000,)] + b[26:]
    r = {i["rule"] for i in G.check_bars("X", one)}
    assert r == {"volume_outlier"}, r                                 # 偶发：只告警
    shift = b[:20] + [(d, o, h, l, c, v * 100) for d, o, h, l, c, v in b[20:]]
    iss = G.check_bars("X", shift)
    assert any(i["rule"] == "volume_unit_shift" and i["severity"] == "error" for i in iss)
    assert iss[0]["date"] == b[20][0] or any(i["date"] == b[20][0] for i in iss)
    assert not G.check_bars("X", b)                                   # 干净数据零告警


def test_gate_price_jump():
    b = _bars(10)
    j = b[:-1] + [(b[-1][0], 14, 14.2, 13.8, 14.0, 1000)]
    assert any(i["rule"] == "price_jump" for i in G.check_bars("X", j, "a"))


# ---------------- 一致性检查
def test_consistency_clean_and_failures():
    tmp = tempfile.mkdtemp()
    try:
        make_src(tmp)
        db, cfg = os.path.join(tmp, "db", "sim.db"), os.path.join(tmp, "config", "strategy.yaml")
        res = CC.run_checks(db, cfg)
        assert not [r for r in res if r["severity"] == "error"], res
        c = sqlite3.connect(db)
        c.execute("UPDATE positions SET qty=900")                      # 分叉
        c.commit()
        assert any(r["code"] == "ledger_fork" for r in CC.run_checks(db, cfg))
        c.execute("UPDATE positions SET qty=1000")
        c.execute("UPDATE daily_report SET cash=cash+100")             # 现金重放不符 + NAV 等式破坏
        c.commit()
        codes = {r["code"] for r in CC.run_checks(db, cfg)}
        assert {"cash_replay", "nav_equation"} <= codes
        c.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_consistency_flags_unit_shift_and_orphan_sell():
    tmp = tempfile.mkdtemp()
    try:
        make_src(tmp, volume_shift=True)
        db, cfg = os.path.join(tmp, "db", "sim.db"), os.path.join(tmp, "config", "strategy.yaml")
        c = sqlite3.connect(db)
        c.execute("INSERT INTO signals(date,account,symbol,side,price,status) VALUES ('2026-10-20','sim_a','999999','SELL',5,'待执行')")
        c.commit()
        codes = {r["code"] for r in CC.run_checks(db, cfg)}
        assert "volume_unit_shift" in codes and "orphan_sell" in codes
        assert CC.main([db, "--config", cfg]) == 1                      # error → 退出码 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 迁移与触发器
def test_migration_idempotent_and_triggers():
    tmp = tempfile.mkdtemp()
    try:
        make_src(tmp)
        db = os.path.join(tmp, "db", "sim.db")
        assert MG.migrate(db) == ["003_integrity.sql"]
        assert MG.migrate(db) == []                                     # 幂等
        c = sqlite3.connect(db)
        assert c.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0] == 1
        # 非法写入被拒
        for sql in ("INSERT INTO fills(date,account,symbol,side,qty,price,fee) VALUES ('d','a','s','HOLD',1,1,0)",
                    "INSERT INTO fills(date,account,symbol,side,qty,price,fee) VALUES ('d','a','s','BUY',0,1,0)",
                    "INSERT INTO signals(date,account,symbol,side,price,status) VALUES ('d','a','s','BUY',1,'乱写')",
                    "INSERT INTO approvals(date,account,symbol,side,decision,updated) VALUES ('d','a','s','BUY','maybe','t')"):
            try:
                c.execute(sql)
                raise AssertionError("应被拒绝：" + sql)
            except sqlite3.DatabaseError:
                pass
        # 审批 → 审计日志；审计/信号日志只增不改
        c.execute("INSERT INTO approvals(date,account,symbol,side,decision,updated) VALUES ('2026-10-27','sim_a','000001','BUY','approved','t')")
        assert c.execute("SELECT action FROM audit_log").fetchall() == [("approval",)] or \
            ("approval",) in c.execute("SELECT action FROM audit_log").fetchall()
        for sql in ("UPDATE audit_log SET actor='x'", "DELETE FROM audit_log", "DELETE FROM signal_log", "UPDATE signal_log SET price=1"):
            try:
                c.execute(sql)
                raise AssertionError("应被拒绝：" + sql)
            except sqlite3.DatabaseError:
                pass
        # 新信号自动留底；状态迁移也留底
        c.execute("INSERT INTO signals(date,account,symbol,side,price,status) VALUES ('2026-10-27','sim_a','000001','SELL',11,'待执行')")
        c.execute("UPDATE signals SET status='已过期' WHERE symbol='000001' AND side='SELL'")
        assert c.execute("SELECT COUNT(*) FROM signal_log WHERE side='SELL'").fetchone()[0] == 2
        # 券商私有表不进审计
        c.execute("INSERT INTO broker_receipts VALUES ('2026-10-27','sim_a','000001','BUY',10,100,'t')")
        assert not c.execute("SELECT 1 FROM audit_log WHERE action='broker_receipt'").fetchall()
        c.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_migration_rollback_on_failure():
    tmp = tempfile.mkdtemp()
    try:
        make_src(tmp)
        db = os.path.join(tmp, "db", "sim.db")
        md = os.path.join(tmp, "mig")
        os.makedirs(md)
        open(os.path.join(md, "001_bad.sql"), "w").write("CREATE TABLE t1(a);\nINSERT INTO no_such_table VALUES (1);\n")
        try:
            MG.migrate(db, mig_dir=md)
            raise AssertionError("应抛错")
        except sqlite3.OperationalError:
            pass
        c = sqlite3.connect(db)
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='t1'").fetchall()   # 已回滚
        assert any(f.startswith("sim.db.bak-") for f in os.listdir(os.path.join(tmp, "db")))   # 备份保留
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 重建演练
def test_rebuild_matches_and_detects_tamper():
    import yaml
    tmp = tempfile.mkdtemp()
    try:
        make_src(tmp)
        c = sqlite3.connect(os.path.join(tmp, "db", "sim.db"))
        cfg = yaml.safe_load(CFG)
        pos, accts, diffs = RB.compare(c, cfg)
        assert not diffs and abs(accts["sim_a"]["cash"] - 89995.0) < 1e-6
        c.execute("UPDATE positions SET qty=1")
        _, _, diffs = RB.compare(c, cfg)
        assert diffs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 部署清单
def test_manifest_finds_missing_modules():
    req = SM.required_files()
    assert "app.py" in req and "engine/order_validate.py" in req and "db/migrations/003_integrity.sql" in req
    assert not any(f.endswith(".example.json") for f in req)
    miss = SM.missing_from(["app.py", "engine/pipeline.py"])
    assert "engine/order_validate.py" in miss
    assert SM.missing_from(req) == []


# ---------------- 原子同步（假 GitHub API）
class FakeAPI:
    def __init__(self, remote=None):
        self.calls, self.remote = [], remote or {}

    def __call__(self, method, path, data=None):
        self.calls.append((method, path))
        if path.endswith("/git/ref/heads/master"):
            return {"object": {"sha": "H0"}}
        if "/git/commits/H0" in path:
            return {"tree": {"sha": "T0"}}
        if "/git/trees/T0" in path:
            return {"tree": [{"path": p, "sha": s, "type": "blob"} for p, s in self.remote.items()]}
        if path.endswith("/git/blobs"):
            return {"sha": "B%d" % len(self.calls)}
        if path.endswith("/git/trees"):
            self.tree_entries = data["tree"]
            return {"sha": "T1"}
        if path.endswith("/git/commits"):
            self.parents = data["parents"]
            self.msg = data["message"]
            return {"sha": "C1"}
        if method == "PATCH":
            self.ref_update = data
            return {}
        raise AssertionError(path)


def test_atomic_push_is_single_commit_and_noop_when_unchanged():
    tmp = tempfile.mkdtemp()
    try:
        make_src(tmp)
        files = ["config/strategy.yaml", "db/sim.db"]
        pushed = SA.filtered_db(os.path.join(tmp, "db", "sim.db"))
        api = FakeAPI()
        sha, changed = SA.atomic_push(api, files, tmp, pushed, "check: ok")
        assert sha == "C1" and sorted(changed) == sorted(files)
        assert sum(1 for m, p in api.calls if p.endswith("/git/commits") and m == "POST") == 1   # 单次提交
        assert api.parents == ["H0"] and api.ref_update["force"] is False
        # 远端已一致 → 无变更、不创建提交
        remote = {"config/strategy.yaml": SA._git_blob_sha(os.path.join(tmp, "config", "strategy.yaml")),
                  "db/sim.db": SA._git_blob_sha(pushed)}
        api2 = FakeAPI(remote)
        assert SA.atomic_push(api2, files, tmp, pushed, "x") == (None, [])
        assert not any(p.endswith("/git/commits") for _, p in api2.calls)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_preflight_blocks_unsafe_pushes():
    tmp = tempfile.mkdtemp()
    try:
        make_src(tmp)
        full = SM.required_files()
        ok, problems, db, _ = SA.preflight(full, tmp)
        assert ok, problems
        ok, problems, *_ = SA.preflight(["db/sim.db"], tmp)               # 清单不全
        assert not ok and any("部署清单" in p for p in problems)
        c = sqlite3.connect(os.path.join(tmp, "db", "sim.db"))             # 券商私有数据必须被过滤
        c.execute("INSERT INTO broker_receipts VALUES ('2026-10-27','sim_a','000001','BUY',10,100,'t')")
        c.execute("UPDATE positions SET qty=5")                            # 账本分叉 → 拒绝
        c.commit()
        ok, problems, pushed, _ = SA.preflight(full, tmp)
        assert not ok and any("ledger_fork" in p for p in problems)
        assert SA.private_rows(pushed) == 0
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
                import traceback
                fails += 1
                print("❌", name, type(e).__name__, e)
                traceback.print_exc()
    sys.exit(1 if fails else 0)
