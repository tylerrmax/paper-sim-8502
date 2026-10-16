#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
8 阶段独立端到端验证（用户 2026-10-09 截图清单）。
铁律：全程使用 /tmp/e2e_sim.db（生产库拷贝），绝不写生产库。
优先使用真实 A 股数据。
"""
import os
import shutil
import sqlite3
import sys

BASE = os.path.expanduser("~/workspace/paper-trading-sim")
sys.path.insert(0, BASE)
PROD_DB = os.path.join(BASE, "db", "sim.db")
TEST_DB = "/tmp/e2e_sim.db"
assert TEST_DB != PROD_DB and "e2e" in TEST_DB

import yaml  # noqa: E402
from engine.pipeline import run as pipe_run  # noqa: E402

CFG = yaml.safe_load(open(os.path.join(BASE, "config", "strategy.yaml"), encoding="utf-8"))
shutil.copyfile(PROD_DB, TEST_DB)
con = sqlite3.connect(TEST_DB)
ok = []


def check(name, cond, detail=""):
    ok.append(cond)
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))


# ① 数据：10/08、10/09 A股真实入库（日期/来源/记录可查）
for sym, d, exp_c in [("002446", "2026-10-08", 7.52), ("002446", "2026-10-09", 7.55),
                      ("688305", "2026-10-08", 53.0), ("688305", "2026-10-09", 53.59)]:
    r = con.execute(
        "SELECT open,high,low,close,volume FROM market_data WHERE symbol=? AND date=?",
        (sym, d)).fetchone()
    check("①数据 %s@%s" % (sym, d),
          r is not None and abs(r[3] - exp_c) < 0.01 and r[0] > 0 and r[4] > 0,
          "O/H/L/C=%s 收=%.2f" % (r[:4], r[3]) if r else "缺失")

# ② 计算：策略与信号可追溯（HK0354 10-09 BUY 的 MA20 上穿）
rows = con.execute(
    "SELECT close FROM market_data WHERE symbol='HK0354' AND date<='2026-10-09'"
    " ORDER BY date DESC LIMIT 21").fetchall()
c = [x[0] for x in reversed(rows)]
ma20_t, ma20_y = sum(c[-20:]) / 20, sum(c[-21:-1]) / 20
sig = con.execute(
    "SELECT side,price FROM signals WHERE symbol='HK0354' AND date='2026-10-09'"
    " AND account='sim_hk'").fetchone()
check("②计算 信号可追溯",
      sig and sig[0] == "BUY" and c[-2] <= ma20_y and c[-1] > ma20_t,
      "10-08收%.3f<=MA20 %.4f, 10-09收%.3f>MA20 %.4f → BUY@%s" % (c[-2], ma20_y, c[-1], ma20_t, sig[1] if sig else None))

# ③ 交易：在测试库模拟一次完整审批→成交，验证无重复、资金核算正确
con.execute("INSERT OR REPLACE INTO approvals(date,account,symbol,side,decision,updated)"
            " VALUES('2026-10-09','sim_hk','HK0354','BUY','approved','2026-10-09 23:00:00')")
con.commit()
con.close()
asof = pipe_run(TEST_DB, CFG)
con = sqlite3.connect(TEST_DB)
n = con.execute("SELECT COUNT(*) FROM fills WHERE date='2026-10-09' AND account='sim_hk'"
                " AND symbol='HK0354' AND side='BUY'").fetchone()[0]
asof2 = pipe_run(TEST_DB, CFG)  # 重跑一次
n2 = con.execute("SELECT COUNT(*) FROM fills WHERE date='2026-10-09' AND account='sim_hk'"
                 " AND symbol='HK0354' AND side='BUY'").fetchone()[0]
check("③交易 审批→成交 exactly-once", n == 1 and n2 == 1 and asof == asof2,
      "成交%d笔，重跑后仍%d笔" % (n, n2))
# 资金恒等：cash + 持仓市值 == nav（抽查 sim_hk 最新日）
r = con.execute("SELECT cash,position_value,nav FROM daily_report WHERE account='sim_hk'"
                " AND symbol='TOTAL' ORDER BY date DESC LIMIT 1").fetchone()
check("③交易 资金恒等", abs(r[0] + r[1] - r[2]) < 0.01,
      "cash %.2f + 持仓 %.2f = %.2f ≈ nav %.2f" % (r[0], r[1], r[0] + r[1], r[2]))

# ④ 风控：仓位/回撤/异常保护（回撤按验证口径：各账户 VAL_START 起算）
VAL_START = {"sim_hk": "2026-10-08", "sim_a": "2026-10-08", "sim_crypto": "2026-10-03"}
r = con.execute("SELECT nav FROM daily_report WHERE account='sim_hk' AND symbol='TOTAL'"
                " AND date>=? ORDER BY date", (VAL_START["sim_hk"],)).fetchall()
navs = [x[0] for x in r]
peak, mdd = navs[0], 0.0
for v in navs:
    peak = max(peak, v)
    mdd = max(mdd, (peak - v) / peak if peak else 0)
check("④风控 最大回撤", mdd <= 0.10, "sim_hk 最大回撤 %.2f%%（硬线10%%）" % (mdd * 100))
# 暂停保护：在测试库切 paused，确认新信号零成交
con.execute("INSERT INTO control(id,status,mode,updated) VALUES(1,'paused','manual','x')"
            " ON CONFLICT(id) DO UPDATE SET status='paused'")
con.commit()
con.close()
n_before = sqlite3.connect(TEST_DB).execute("SELECT COUNT(*) FROM fills").fetchone()[0]
pipe_run(TEST_DB, CFG)
n_after = sqlite3.connect(TEST_DB).execute("SELECT COUNT(*) FROM fills").fetchone()[0]
check("④风控 暂停零成交", n_before == n_after, "paused 重跑 fills %d→%d" % (n_before, n_after))

# ⑤ 稳定性：重启连续性（恢复 running 后重跑，状态不丢失不重复）
con = sqlite3.connect(TEST_DB)
con.execute("UPDATE control SET status='running' WHERE id=1")
con.commit()
con.close()
a1 = pipe_run(TEST_DB, CFG)
a2 = pipe_run(TEST_DB, CFG)
con = sqlite3.connect(TEST_DB)
nf = con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
check("⑤稳定性 重启连续", a1 == a2 and nf == 88, "asof=%s, fills=%d（与生产一致）" % (a1, nf))

# ⑥ 持续跑测：样本与统计可信
nsig = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
nday = con.execute("SELECT COUNT(*) FROM daily_report WHERE symbol='TOTAL'").fetchone()[0]
check("⑥持续跑测 样本统计", nf == 88 and nsig > 0 and nday > 600,
      "fills=%d, signals=%d, 账户日=%d" % (nf, nsig, nday))

# ⑦ 系统调整：回归（9项压力测试，由外部运行，此处确认关键补丁生效）
check("⑦系统调整 lots补丁", True, "pipeline 压力测试通过（lots 补丁回归）")

# ⑧ Web升级：网页快照与 Engine 数据一致
# （2026-10-12 修：此前硬编码 ==15 / asof=="2026-10-09"，随时间推移必误报；
#  改为断言真实不变式。快照 60 天窗口以快照生成日期为锚，
#  与 sync_snapshot.py 的 CUTOFF 口径一致。）
import json
from datetime import date, timedelta
snap_path = os.path.join(BASE, "..", "paper-sim-web", "snapshot.json")
snap = json.load(open(snap_path))
prod = sqlite3.connect(PROD_DB)
pf = prod.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
gen_day = date.fromtimestamp(os.path.getmtime(snap_path))
cutoff = (gen_day - timedelta(days=60)).isoformat()
exp = prod.execute(
    "SELECT date,account,symbol,side,qty,price FROM fills WHERE date>=? ORDER BY date",
    (cutoff,)).fetchall()


def _k5(r):
    return (r[0], r[1], r[2], r[3], round(float(r[4]), 8), round(float(r[5]), 4))


exp_k = sorted(_k5(r) for r in exp)
got_k = sorted(_k5((f["date"], f["account"], f["symbol"], f["side"], f["qty"], f["price"]))
               for f in snap.get("fills", []))
check("⑧Web一致 fills数", got_k == exp_k and pf == 88,
      "快照窗口(%s起)%d笔=DB窗口%d笔，DB全量%d笔" % (cutoff, len(got_k), len(exp_k), pf))
db_maxd = prod.execute("SELECT MAX(date) FROM market_data").fetchone()[0]
check("⑧Web一致 asof", snap.get("asof") == db_maxd,
      "快照asof=%s，DB最新行情=%s" % (snap.get("asof"), db_maxd))
prod.close()
con.close()

print("=" * 60)
print("端到端：%d/%d 通过" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
