#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周度一致性简报（《8502验证期运行纲领》§二证明指标 + §三每周节奏，
2026-10-27 验证期收尾件二）。

每周一由剪报 cron 调用，运行后向 stdout 打印一段紧凑的中文
Markdown 简报块，数字全部来自系统自身记录：

1. 运行可靠性：近 7 日夜跑日报覆盖天数（daily_report）、选股落库
   新增期数（screen_runs）、发布门禁审计的放行/拒绝/绕过计数
   （reports/release_gate_log.jsonl）。
2. 账本完整性：fills 指纹四项（笔数/Σ量/Σ额/Σpnl）与发布门禁
   水位线（.release_gate_state.json）对照——复用 release_gate 的
   滚动哈希核对，水位内历史行被改/删即不一致。
3. 样本积累：sim_a 现行策略版本完整 round-trip 数（复用
   sync_snapshot.build_graduation，只读）；影子三组（sa/shadow/
   other）已到期（fwd5 非空）样本数；blocked_perf 到期数与
   疑似误杀占比（fwd5>0）。
4. 风险纪律：近 7 日 blocked_signals 新增数；rotation_vouchers
   各状态计数（台账未建时如实标注）。
5. 决策可复现抽查：挑一期尚未抽查、且规则版本与现行
   engine.screen.RULE_VERSION 相同的历史落库期（规则版本不同的
   期不可比对，跳过并注明），把数据库拷到 /tmp 副本、以该 asof
   离线重跑 screen_all，与落库 results_json 逐标的比对技术分/
   母板分/封顶标记（及新闻可比时的新闻分/总分/评级）。
   新闻分不一致时先比对落库 news_sha256 与当前新闻账的 sha：
   不同 → 新闻输入已变更，该期新闻分/总分/评级按「不可复现
   比对」标注、不判 FAIL；相同还不一致才判 FAIL。
   抽查结果一行追加写入 reports/consistency_checks.jsonl
   （asof/结论/差异数），已查 asof 不重复抽。
6. 事故行：指纹漂移、抽查 FAIL 等异常逐条以「⚠️一致性异常：」
   开头列出；无异常明确写「本周无一致性异常」。

只读纪律：生产库一律以只读方式打开；抽查重跑只写 /tmp 副本
（用完即弃），绝不写生产库。本脚本唯一的落盘副作用是向
reports/consistency_checks.jsonl 追加一行抽查记录。

用法: python3 tools/weekly_consistency.py [--db PATH] [--base PATH]
      [--no-record]   # --no-record 时不写 consistency_checks.jsonl
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

DEFAULT_DB = os.path.join(BASE, "db", "sim.db")
WINDOW_DAYS = 7
_NUM_TOL = 1e-6

# screen_perf 组别推导（grp 为空的历史行）：与 tools/screen_perf.py
# 的 _group_of 同口径——封顶→shadow，未封顶 S/A→sa，其余→other。
_GRP_CASE = (
    "CASE WHEN grp IS NOT NULL AND grp<>'' THEN grp"
    " WHEN COALESCE(capped,0)<>0 THEN 'shadow'"
    " WHEN grade IN ('S','A') THEN 'sa' ELSE 'other' END")


def _ro(db_path):
    return sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)


def _fmt_num(v, dec=2):
    if v is None:
        return "—"
    s = ("%.*f" % (dec, v)).rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def _window(today):
    start = today - timedelta(days=WINDOW_DAYS - 1)
    return start, {(start + timedelta(days=i)).isoformat()
                   for i in range(WINDOW_DAYS)}


def _read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


# ---------------------------------------------------------------- §1
def sec_reliability(con, reports_dir, win):
    start_s, days = win
    n_days = 0
    try:
        n_days = con.execute(
            "SELECT COUNT(DISTINCT date) FROM daily_report"
            " WHERE date>=?", (start_s,)).fetchone()[0]
    except sqlite3.OperationalError:
        n_days = 0
    n_runs = 0
    try:
        n_runs = sum(
            1 for (ra,) in con.execute("SELECT run_at FROM screen_runs")
            if str(ra)[:10] in days)
    except sqlite3.OperationalError:
        n_runs = 0
    log = _read_jsonl(os.path.join(reports_dir, "release_gate_log.jsonl"))
    n_pass = n_refuse = n_bypass = 0
    for e in log:
        if str(e.get("ts", ""))[:10] not in days:
            continue
        ev = e.get("event")
        if ev in ("pass", "check_only_pass"):
            n_pass += 1
        elif ev in ("refuse", "check_only_refuse"):
            n_refuse += 1
        elif ev == "bypass":
            n_bypass += 1
    return ("1. 运行可靠：近 7 日夜跑日报覆盖 %d/%d 天；选股落库新增 "
            "%d 期；发布门禁放行 %d 次、拒绝 %d 次、绕过 %d 次。"
            % (n_days, WINDOW_DAYS, n_runs, n_pass, n_refuse, n_bypass))


# ---------------------------------------------------------------- §2
def sec_ledger(db_path, base):
    """返回 (简报行, 异常文本或 None)。"""
    import release_gate
    con = _ro(db_path)
    try:
        cnt, sqty, samt, spnl = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(qty),0),"
            " COALESCE(SUM(qty*price),0), COALESCE(SUM(pnl),0)"
            " FROM fills").fetchone()
    finally:
        con.close()
    head = ("2. 账本完整：fills %d 笔（Σ量 %s / Σ额 %s / Σpnl %s）；"
            % (cnt, _fmt_num(sqty, 5), _fmt_num(samt), _fmt_num(spnl)))
    state = release_gate.load_state(
        os.path.join(base, release_gate.STATE_NAME))
    if state is None:
        msg = "指纹水位线未初始化，账本完整性无法核对"
        return head + msg + "。", msg
    try:
        n_new = release_gate.check_fingerprint(db_path, state)
    except release_gate.GateRefusal as e:
        msg = "指纹水位线不一致：%s" % e
        return head + msg + "。", msg
    return (head + "指纹水位线一致（水位 %d 笔 / max_id %s，历史行哈希"
            "未变，本期新增 %d 笔均有日报佐证）。"
            % (state["fills_count"], state["max_id"], n_new)), None


# ---------------------------------------------------------------- §3
def sec_samples(con):
    from sync_snapshot import build_graduation, CFG
    ver = CFG.get("meta", {}).get("version", "unknown")
    n_rt = build_graduation(con)["accounts"].get("sim_a", {}).get(
        "round_trips", 0)
    grp_n = {"sa": 0, "shadow": 0, "other": 0}
    try:
        for g, n in con.execute(
                "SELECT %s, COUNT(*) FROM screen_perf"
                " WHERE fwd5_ret IS NOT NULL GROUP BY 1" % _GRP_CASE):
            grp_n[g] = n
    except sqlite3.OperationalError:
        pass
    n_blk, n_pos = 0, 0
    try:
        n_blk, n_pos = con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN fwd5_ret>0 THEN 1 ELSE 0 END)"
            " FROM blocked_perf WHERE fwd5_ret IS NOT NULL").fetchone()
        n_pos = n_pos or 0
    except sqlite3.OperationalError:
        pass
    sus = ("疑似误杀 %d/%d（%s）" % (
        n_pos, n_blk,
        ("%.1f%%" % (100.0 * n_pos / n_blk)) if n_blk else "—"))
    return ("3. 样本积累：sim_a 现行版本（%s）完整 round-trip %d 笔；"
            "影子样本已到期 sa %d / shadow %d / other %d；拦截样本"
            "已到期 %d 条，%s。"
            % (ver, n_rt, grp_n["sa"], grp_n["shadow"], grp_n["other"],
               n_blk, sus))


# ---------------------------------------------------------------- §4
def sec_risk(con, start_s):
    try:
        n_blk = con.execute(
            "SELECT COUNT(*) FROM blocked_signals WHERE date>=?",
            (start_s,)).fetchone()[0]
    except sqlite3.OperationalError:
        n_blk = 0
    has_v = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name='rotation_vouchers'").fetchone()[0]
    if has_v:
        counts = dict(con.execute(
            "SELECT status, COUNT(*) FROM rotation_vouchers"
            " GROUP BY status").fetchall())
        vtxt = "额度券 " + " / ".join(
            "%s %d" % (k, counts[k]) for k in sorted(counts)) \
            if counts else "额度券台账为空（尚无券事件）"
    else:
        vtxt = "额度券台账未建（尚无券事件）"
    return ("4. 风险纪律：近 7 日禁入拦截新增 %d 条；%s。" % (n_blk, vtxt))


# ---------------------------------------------------------------- §5
def _numdiff(a, b):
    if a is None or b is None:
        return (a is None) != (b is None)
    try:
        return abs(float(a) - float(b)) > _NUM_TOL
    except (TypeError, ValueError):
        return a != b


def _checked_asofs(checks_log):
    return {e.get("asof") for e in _read_jsonl(checks_log)
            if e.get("asof") and e.get("result") in ("PASS", "FAIL")}


def reproduce_check(db_path, checks_log, entries, news_sha_now):
    """离线复现抽查。返回 (简报行, 记录 dict, 异常文本或 None)。

    只读生产库；重跑落在 /tmp 副本上（record=False），用完即弃。
    """
    from engine.screen import RULE_VERSION, screen_all
    con = _ro(db_path)
    try:
        runs = con.execute(
            "SELECT id, asof, rule_version, news_sha256, results_json"
            " FROM screen_runs ORDER BY asof, id").fetchall()
    except sqlite3.OperationalError:
        runs = []
    finally:
        con.close()
    latest = {}
    for r in runs:
        latest[r[1]] = r  # 同 asof 取最新一期（与 screen_perf 口径一致）
    checked = _checked_asofs(checks_log)
    pending = [a for a in sorted(latest) if a not in checked]
    cands = [a for a in pending if latest[a][2] == RULE_VERSION]
    if not cands:
        if pending:
            vers = "、".join(sorted({latest[a][2] for a in pending}))
            reason = ("未抽查的落库期规则版本为 %s，现行 %s，版本不同"
                      "不可比对" % (vers, RULE_VERSION))
        elif latest:
            reason = "已落库各期均已抽查"
        else:
            reason = "尚无选股落库期"
        rec = {"asof": None, "result": "NO_ELIGIBLE", "diff_count": 0,
               "note": reason}
        return ("5. 复现抽查：无可抽查历史期（%s）。" % reason), rec, None
    asof = cands[0]
    run_id, _a, _v, stored_sha, results_json = latest[asof]
    try:
        stored_rows = json.loads(results_json)
    except (TypeError, ValueError):
        stored_rows = []
    stored_by = {r.get("symbol"): r for r in stored_rows}
    fd, tmp = tempfile.mkstemp(prefix="weekly_repro_", suffix=".db",
                               dir="/tmp")
    os.close(fd)
    try:
        shutil.copyfile(db_path, tmp)
        new_rows = screen_all(db_path=tmp, asof=asof, entries=entries,
                              record=False)
    except Exception as e:  # 重跑本身失败 = 系统异常，响亮记事故
        msg = "复现抽查 asof %s 重跑失败：%s: %s" % (
            asof, type(e).__name__, e)
        rec = {"asof": asof, "run_id": run_id, "result": "ERROR",
               "diff_count": 0, "note": msg}
        return "5. 复现抽查：%s。" % msg, rec, msg
    finally:
        for p in (tmp, tmp + "-wal", tmp + "-shm"):
            try:
                os.remove(p)
            except OSError:
                pass
    new_by = {r.get("symbol"): r for r in new_rows}
    sha_same = bool(stored_sha) and stored_sha == news_sha_now
    diffs, news_affected = [], []
    for sym in sorted(set(stored_by) | set(new_by)):
        s, n = stored_by.get(sym), new_by.get(sym)
        if s is None or n is None:
            diffs.append("%s 存在性（落库%s / 重跑%s）"
                         % (sym, "有" if s else "无", "有" if n else "无"))
            continue
        for f, label in (("tech", "技术分"), ("board", "母板分")):
            if _numdiff(s.get(f), n.get(f)):
                diffs.append("%s %s 落库 %s vs 重跑 %s"
                             % (sym, label, s.get(f), n.get(f)))
        if bool(s.get("capped")) != bool(n.get("capped")):
            diffs.append("%s 封顶标记 落库 %s vs 重跑 %s"
                         % (sym, s.get("capped"), n.get("capped")))
        news_bad = (_numdiff(s.get("news"), n.get("news"))
                    or _numdiff(s.get("total"), n.get("total"))
                    or s.get("grade") != n.get("grade"))
        if sha_same:
            if _numdiff(s.get("news"), n.get("news")):
                diffs.append("%s 新闻分 落库 %s vs 重跑 %s"
                             % (sym, s.get("news"), n.get("news")))
            if _numdiff(s.get("total"), n.get("total")):
                diffs.append("%s 总分 落库 %s vs 重跑 %s"
                             % (sym, s.get("total"), n.get("total")))
            if s.get("grade") != n.get("grade"):
                diffs.append("%s 评级 落库 %s vs 重跑 %s"
                             % (sym, s.get("grade"), n.get("grade")))
        elif news_bad:
            news_affected.append(sym)
    note = None
    if not sha_same:
        note = ("新闻输入已变更（落库 news_sha256 与当前新闻账 sha "
                "不一致），该期新闻分不可复现比对（非系统不一致）")
        if news_affected:
            note += "；受影响标的 %d 只：%s" % (
                len(news_affected), "、".join(news_affected))
    if diffs:
        msg = ("复现抽查 asof %s（run #%d）不一致：%s"
               % (asof, run_id, "；".join(diffs)))
        rec = {"asof": asof, "run_id": run_id, "result": "FAIL",
               "diff_count": len(diffs),
               "diff_symbols": sorted({d.split(" ")[0] for d in diffs}),
               "note": note}
        return "5. %s。" % msg, rec, msg
    txt = ("5. 复现抽查：asof %s（run #%d，%s）逐标的比对 %d 只，"
           "技术/母板/总分/评级/封顶全部一致 ✅"
           % (asof, run_id, RULE_VERSION, len(new_by)))
    if note:
        txt += "（注：%s）" % note
    rec = {"asof": asof, "run_id": run_id, "result": "PASS",
           "diff_count": 0, "note": note}
    return txt + "。", rec, None


# ---------------------------------------------------------------- 组装
def build_brief(db_path=DEFAULT_DB, base=BASE, today=None, record=True):
    today = today or date.today()
    start, days = _window(today)
    start_s = start.isoformat()
    reports_dir = os.path.join(base, "reports")
    checks_log = os.path.join(reports_dir, "consistency_checks.jsonl")
    anomalies = []
    con = _ro(db_path)
    try:
        l1 = sec_reliability(con, reports_dir, (start_s, days))
        l3 = sec_samples(con)
        l4 = sec_risk(con, start_s)
    finally:
        con.close()
    l2, anom2 = sec_ledger(db_path, base)
    if anom2:
        anomalies.append(anom2)
    from engine.news_sentiment import load_news
    from engine.screen import _sha256_obj
    entries = load_news(os.path.join(base, "config", "sector_news.json"))
    l5, rec, anom5 = reproduce_check(db_path, checks_log, entries,
                                     _sha256_obj(entries))
    if anom5:
        anomalies.append(anom5)
    if record:
        rec = dict(rec)
        rec["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(reports_dir, exist_ok=True)
        with open(checks_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if anomalies:
        l6 = "\n".join("⚠️一致性异常：%s" % a for a in anomalies)
        l6 = "6. 事故：\n" + l6
    else:
        l6 = "6. 事故：本周无一致性异常。"
    header = "## 8502 周度一致性简报（%s，统计窗 %s ~ %s）" % (
        today.isoformat(), start_s, today.isoformat())
    return "\n".join([header, l1, l2, l3, l4, l5, l6])


def main():
    ap = argparse.ArgumentParser(description="8502 周度一致性简报")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--no-record", action="store_true",
                    help="不向 consistency_checks.jsonl 追加抽查记录")
    args = ap.parse_args()
    print(build_brief(db_path=args.db, base=args.base,
                      record=not args.no_record))


if __name__ == "__main__":
    main()
