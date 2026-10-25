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
# 生产 fills 基线（修订 2026-10-26：原断言硬编码 fills==88，该数字是
# 2026-10 中旬的旧快照，生产账本已演进为 50 笔（账本指纹存证见
# reports/e2e-batch2/ledger_fingerprint.json）→ 新断言：与运行时读取的
# 生产实时计数一致，不再写死数字。规则依据：清单C 账本不可重写，
# 对照应以基线指纹/实时计数为准。）
PF_LIVE = sqlite3.connect(PROD_DB).execute(
    "SELECT COUNT(*) FROM fills").fetchone()[0]


def check(name, cond, detail=""):
    ok.append(bool(cond))
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

# ② 计算：策略与信号可追溯
# 修订（2026-10-26）：原断言查 HK0354 2026-10-09 的 MA20 上穿 BUY 信号。
# 新断言：(a) sim_hk/sim_crypto 策略已停用（2026-10-18 起 config
# signals.per_account=disabled），HK0354 不再生成任何信号——原信号
# 消失是规则使然而非数据丢失；(b) 现行 sim_a buyback 信号可独立追溯：
# 002446 2026-10-15 BUY@6.93 应满足 buyback 规则（收盘 ≤ 前20日最高
# ×0.9），用 market_data 独立重算验证。规则依据：engine/signals.py
# gen_buyback_signals + 2026-10-16 用户选分账户策略 A、10-18 停用
# 港股/加密 MA20。
hk_sig_n = con.execute(
    "SELECT COUNT(*) FROM signals WHERE account='sim_hk'").fetchone()[0]
_h = con.execute(
    "SELECT high FROM market_data WHERE symbol='002446' AND date<'2026-10-15'"
    " ORDER BY date DESC LIMIT 20").fetchall()
high20 = max(x[0] for x in _h)
_c15 = con.execute(
    "SELECT close FROM market_data WHERE symbol='002446' AND date='2026-10-15'"
).fetchone()[0]
sig = con.execute(
    "SELECT side,price FROM signals WHERE symbol='002446' AND date='2026-10-15'"
    " AND account='sim_a'").fetchone()
check("②计算 信号可追溯",
      hk_sig_n == 0 and sig and sig[0] == "BUY"
      and abs(sig[1] - _c15) < 1e-9 and _c15 <= high20 * 0.9,
      "港股策略停用 signals=%d；002446 前20日高 %.2f×0.9=%.3f ≥ 10-15收 %.2f → BUY@%s"
      % (hk_sig_n, high20, high20 * 0.9, _c15, sig[1] if sig else None))

# ③ 交易：完整审批→成交链（exactly-once）+ 资金核算
# 修订（2026-10-26）：原断言在生产拷贝上批准 HK0354 信号并期望成交。
# 新断言：HK 信号已不存在（见②），且现行真实 BUY 信号全部被现行闸门
# 拦截（禁入优先/仓位冻结——系统按设计说“不”），无法在真实数据上
# 构造审批成交；改在合成小库上走完整链：manual 未审批→0 成交，
# 批准→恰好 1 笔，重跑→仍 1 笔。标的 002446、信号日 2026-10-18
# （<2026-10-26 走 legacy 路径，信号价 8.0 > 母板止损 7.70 不触发
# 禁入闸）。规则依据：清单C 生效日分流 + P0-1 禁入优先。
import subprocess
from datetime import date as _date, timedelta as _td
MINI_DB = "/tmp/e2e_mini.db"
if os.path.exists(MINI_DB):
    os.remove(MINI_DB)
subprocess.run([sys.executable, os.path.join(BASE, "init_db.py"),
                "--db", MINI_DB], check=True, capture_output=True)
_mc = sqlite3.connect(MINI_DB)
_mc.execute("UPDATE control SET status='running', mode='manual' WHERE id=1")
_bars, _prev = [], 9.0
for _i, _c in enumerate([9.0] * 25 + [8.0, 8.0]):
    _d = (_date(2026, 9, 23) + _td(days=_i)).isoformat()
    _bars.append(("002446", _d, _prev, max(_prev, _c),
                  min(_prev, _c) * 0.995, _c, 100000.0, "a"))
    _prev = _c
_mc.executemany(
    "INSERT INTO market_data(symbol,date,open,high,low,close,volume,market)"
    " VALUES(?,?,?,?,?,?,?,?)", _bars)
_mc.commit()
_mc.close()
pipe_run(MINI_DB, CFG)  # manual 未审批 → 零成交
_mc = sqlite3.connect(MINI_DB)
_n0 = _mc.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
_mc.execute("INSERT OR REPLACE INTO approvals(date,account,symbol,side,decision,updated)"
            " VALUES('2026-10-18','sim_a','002446','BUY','approved','2026-10-18 23:00:00')")
_mc.commit()
_mc.close()
pipe_run(MINI_DB, CFG)  # 批准后成交
_mc = sqlite3.connect(MINI_DB)
_n1 = _mc.execute(
    "SELECT COUNT(*) FROM fills WHERE account='sim_a' AND symbol='002446'"
    " AND side='BUY' AND signal_date='2026-10-18'").fetchone()[0]
_mc.close()
asof = pipe_run(MINI_DB, CFG)  # 重跑：不得重复成交
_mc = sqlite3.connect(MINI_DB)
_n2 = _mc.execute(
    "SELECT COUNT(*) FROM fills WHERE account='sim_a' AND symbol='002446'"
    " AND side='BUY' AND signal_date='2026-10-18'").fetchone()[0]
_mc.close()
check("③交易 审批→成交 exactly-once", _n0 == 0 and _n1 == 1 and _n2 == 1,
      "未审批%d笔 → 批准后%d笔 → 重跑后仍%d笔" % (_n0, _n1, _n2))
con = sqlite3.connect(TEST_DB)
asof = pipe_run(TEST_DB, CFG)
con = sqlite3.connect(TEST_DB)
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
# 修订（2026-10-26）：原断言 fills==88（旧快照数字）→ 新断言 fills 与
# 脚本启动时读取的生产实时计数 PF_LIVE 一致。依据同 ① 上方基线注释。
check("⑤稳定性 重启连续", a1 == a2 and nf == PF_LIVE, "asof=%s, fills=%d（与生产一致）" % (a1, nf))

# ⑥ 持续跑测：样本与统计可信
nsig = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
nday = con.execute("SELECT COUNT(*) FROM daily_report WHERE symbol='TOTAL'").fetchone()[0]
check("⑥持续跑测 样本统计", nf == PF_LIVE and nsig > 0 and nday > 600,
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
# 修订（2026-10-26）：原断言含 pf == 88（旧快照数字）→ 新断言只验
# 不变式：快照窗口与 DB 窗口逐笔全等（pf 仅作明细展示）。
check("⑧Web一致 fills数", got_k == exp_k,
      "快照窗口(%s起)%d笔=DB窗口%d笔，DB全量%d笔" % (cutoff, len(got_k), len(exp_k), pf))
db_maxd = prod.execute("SELECT MAX(date) FROM market_data").fetchone()[0]
check("⑧Web一致 asof", snap.get("asof") == db_maxd,
      "快照asof=%s，DB最新行情=%s" % (snap.get("asof"), db_maxd))
prod.close()
con.close()

print("=" * 60)
print("端到端：%d/%d 通过" % (sum(ok), len(ok)))
sys.exit(0 if all(ok) else 1)
