#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market-First 统一架构端到端演示脚本（隔离环境专用）。

流程：发现（机构龙虎榜 live 优先）-> 双来源归并 -> 研究
（含机构事件去重）-> 验证（Forward 时间规则 + 回填）->
验证通过门禁 judge（门槛未定版，not_evaluable）-> 直达/审批/
交接拒绝演示，另含生产安全自检。

运行：python -m market_first.run_e2e（worktree 根目录）
或直接执行本文件。只写演示库 market_first_e2e.db，规范库
market_first.db（含审计保留样本）不触碰；生产 sim.db
全程 sqlite3 URI mode=ro 只读，绝不写入。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)  # worktree 根目录
for _p in (_ROOT, "/home/hatch/workspace"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from market_first import bars as bars_mod  # noqa: E402
from market_first import db as mf_db  # noqa: E402
from market_first import handoff, promotion, validation  # noqa: E402
from market_first.discovery import InstLhbAdapter  # noqa: E402
from market_first.models import MOTHERBOARD_SYMBOLS, PROD_SIM_DB  # noqa: E402
from market_first.pool import CandidatePool  # noqa: E402
from market_first.research import ResearchEngine  # noqa: E402

DISCOVERY_DATE = "2026-11-13"
EVAL_END = "2026-11-16"
RESEARCH_START = "2026-05-01"
BACKFILL_CUTOFF = "2026-10-23"
ACTOR = "e2e"

_price_cache: dict = {}


def _section(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def price_fn(symbol, date_str):
    """适配器价格回调：用 bars.get_bars 取该日收盘价。

    返回 (close, date_str)；无该日 bar 返回 (None, None)（留空，
    不编造价格）。结果按 (symbol, date) 缓存，避免重复取数。
    """
    key = (symbol, date_str)
    if key in _price_cache:
        return _price_cache[key]
    result = (None, None)
    day_bars, _prov = bars_mod.get_bars(symbol, date_str, date_str)
    for b in day_bars:
        if b[0] == date_str:
            result = (float(b[4]), date_str)
            break
    _price_cache[key] = result
    return result


def close_on(symbol, date_str):
    return price_fn(symbol, date_str)[0]


def _fmt(v):
    return "None" if v is None else v


def _prod_ro():
    return sqlite3.connect("file:%s?mode=ro" % PROD_SIM_DB, uri=True)


def _earlier_inst_row(symbol):
    """生产库（ro）查某标的在 BACKFILL_CUTOFF 或更早、inst_net>0
    的最近一行 -> (date, inst_net) 或 None。"""
    conn = _prod_ro()
    try:
        return conn.execute(
            "SELECT date, inst_net FROM inst_lhb_daily"
            " WHERE symbol=? AND date<=? AND inst_net>0"
            " ORDER BY date DESC LIMIT 1",
            (symbol, BACKFILL_CUTOFF),
        ).fetchone()
    finally:
        conn.close()


def _backfill_demo(pool, cid, symbol):
    """对已入池候选做回填样本演示；无更早记录/无基准价返回 False。"""
    row = _earlier_inst_row(symbol)
    if row is None:
        return False
    entry_date, inst_net = row
    bench = close_on(symbol, entry_date)
    if bench is None:
        print("  %s %s 有机构上榜记录（inst_net=%s），但 bars 无该日"
              "收盘价，基准价缺失，不编造、跳过"
              % (symbol, entry_date, inst_net))
        return False
    sid = validation.create_sample(
        pool, candidate_id=cid, source_id="inst_lhb_stored",
        entry_date=entry_date, benchmark_price=bench, kind="backfill")
    eval_bars, prov = bars_mod.get_bars(symbol, entry_date, EVAL_END)
    sample = validation.evaluate_sample(pool, sid, eval_bars)
    print("  回填样本 id=%s candidate_id=%s symbol=%s kind=backfill"
          " entry_date=%s benchmark=%s（bars 收盘，provenance=%s）"
          % (sid, cid, symbol, entry_date, bench, prov))
    print("  maturity_status=%s ret_t5=%s ret_t10=%s ret_t20=%s"
          " max_drawdown=%s missing_reason=%s"
          % (_fmt(sample["maturity_status"]), _fmt(sample["ret_t5"]),
             _fmt(sample["ret_t10"]), _fmt(sample["ret_t20"]),
             _fmt(sample["max_drawdown"]),
             _fmt(sample["missing_reason"])))
    return True


def main():
    # ---- a. 演示库初始化（删旧重建，保证可复跑） ------------------
    # 修复轮起 e2e 只用演示库 market_first_e2e.db：规范库
    # market_first.db 含独立审计裁定保留的原始样本（样本 1），
    # e2e 绝不触碰、绝不重建规范库。
    _section("a. 初始化 Market-First 演示库")
    db_path = os.path.join(_HERE, "market_first_e2e.db")
    if os.path.exists(db_path):
        os.remove(db_path)
        print("已删除旧演示库：%s" % db_path)
    mf_db.init_db(db_path)
    pool = CandidatePool(db_path)
    bars_mod.configure(db_path)
    print("演示 DB 路径：%s（与生产 sim.db 隔离）" % db_path)
    print("规范库 %s 本次不触碰（审计保留样本所在库）"
          % mf_db.DEFAULT_DB_PATH)

    # ---- b. 发现：InstLhbAdapter live 优先 -----------------------
    _section("b. 市场发现 InstLhbAdapter(prefer='live') %s" % DISCOVERY_DATE)
    adapter = InstLhbAdapter(sim_db_path=PROD_SIM_DB, prefer="live")
    events = adapter.discover(DISCOVERY_DATE, price_fn=price_fn)
    via = adapter.last_obtained_via
    print("事件数（inst_net>0）：%d" % len(events))
    print("obtained_via：%s" % via)
    print("运行说明：%s" % adapter.last_run_note)
    print("覆盖声明：%s" % InstLhbAdapter.COVERAGE)
    if via != "live":
        print("如实说明：live 实时抓取不可用，已回退 stored"
              "（生产库已落库行），不以 stored 冒充 live。")
    assert events, "发现事件为空，e2e 无法继续"
    for e in events:
        print("  %s %s %s inst_net=%s price_at_discovery=%s source=%s"
              % (e.symbol, e.market, e.name,
                 e.evidence.get("inst_net"), _fmt(e.price_at_discovery),
                 e.source_id))

    # ---- c. 主标的选择 + 双来源归并 --------------------------------
    _section("c. 主标的选择与双来源归并（live + stored 同一标的）")
    eligible = [e for e in events
                if e.symbol not in MOTHERBOARD_SYMBOLS
                and e.market in ("SH", "SZ")]
    assert eligible, "当日事件中无非母板 SH/SZ 标的"
    main_event = max(eligible,
                     key=lambda e: float(e.evidence.get("inst_net") or 0))
    print("选中主标的：%s %s %s inst_net=%s"
          % (main_event.symbol, main_event.market, main_event.name,
             main_event.evidence.get("inst_net")))
    print("理由：当日事件中 symbol 不在母板 %d 只、market ∈ {SH, SZ}、"
          "inst_net 最大者" % len(MOTHERBOARD_SYMBOLS))
    cid_main, created = pool.ingest(main_event, actor=ACTOR)
    print("ingest #1（%s）-> candidate_id=%s created=%s，候选总数=%d"
          % (main_event.source_id, cid_main, created,
             len(pool.list_candidates())))
    n_candidates = len(pool.list_candidates())

    stored_adapter = InstLhbAdapter(sim_db_path=PROD_SIM_DB, prefer="stored")
    stored_events = stored_adapter.discover(DISCOVERY_DATE, price_fn=price_fn)
    stored_main = next(
        e for e in stored_events
        if e.symbol == main_event.symbol and e.market == main_event.market)
    cid_again, created_again = pool.ingest(stored_main, actor=ACTOR)
    ev_rows = pool.events_for(cid_main)
    src_ids = [r["source_id"] for r in ev_rows]
    print("ingest #2（%s）-> candidate_id=%s created=%s，候选总数=%d"
          % (stored_main.source_id, cid_again, created_again,
             len(pool.list_candidates())))
    assert cid_again == cid_main and created_again is False
    assert len(pool.list_candidates()) == n_candidates, "归并失败：候选数增加"
    if len(set(src_ids)) >= 2 and len(ev_rows) == 2:
        print("双来源归并证明：同一 candidate_id=%s 下 events_for=%d 行，"
              "来源分别为 %s（不同来源历史并存、未覆盖）"
              % (cid_main, len(ev_rows), src_ids))
    else:
        print("如实说明：live 不可用，两次 ingest 来源同为 %s；"
              "改以同一事件再 ingest 一次验证归并不丢历史。"
              % (src_ids,))
        cid_third, created_third = pool.ingest(stored_main, actor=ACTOR)
        ev_rows = pool.events_for(cid_main)
        assert cid_third == cid_main and created_third is False
        assert len(pool.list_candidates()) == n_candidates
        print("第三次 ingest 同一事件后：candidate_id 不变=%s，候选总数"
              "仍为 %d，events_for=%d 行（历史追加、不覆盖）"
              % (cid_third, len(pool.list_candidates()), len(ev_rows)))

    # ---- d. 研究 ---------------------------------------------------
    _section("d. 晋升流转 -> researching + ResearchEngine 研究")
    promotion.transition(pool, cid_main, "pending_research",
                         actor=ACTOR, reason="e2e：发现完成，进入待研究")
    promotion.transition(pool, cid_main, "researching",
                         actor=ACTOR, reason="e2e：开始研究")
    res_bars, res_prov = bars_mod.get_bars(
        main_event.symbol, RESEARCH_START, EVAL_END)
    data_asof = res_bars[-1][0] if res_bars else DISCOVERY_DATE
    report_id = ResearchEngine(pool).run(
        cid_main, res_bars, data_asof=data_asof, data_version=res_prov)
    row = pool.conn.execute(
        "SELECT factors_json, total_score, evidence_json"
        " FROM research_reports WHERE id=?", (report_id,)).fetchone()
    factors = json.loads(row["factors_json"])
    ev_modules = json.loads(row["evidence_json"]).get("modules", {})
    print("research_report id=%s bars=%d 根（%s，provenance=%s）"
          % (report_id, len(res_bars),
             "%s..%s" % (res_bars[0][0], res_bars[-1][0]) if res_bars
             else "无", res_prov))
    for mod_id, mod in factors.items():
        print("  模块 %s（model_version=%s）subtotal=%s factors=%s"
              % (mod_id, mod.get("model_version"), _fmt(mod.get("subtotal")),
                 json.dumps(mod.get("factors"), ensure_ascii=False)))
    print("total_score=%s" % _fmt(row["total_score"]))
    inst_ev = ev_modules.get("inst_flow") or {}
    dedup = inst_ev.get("dedup") or {}
    print("机构事件去重（P0-2）：raw_event_ids=%s deduped_event_ids=%s"
          " dropped_duplicate_ids=%s total_inst_net=%s（同一底层事件"
          "只计一次净额）"
          % (dedup.get("raw_event_ids"), dedup.get("deduped_event_ids"),
             dedup.get("dropped_duplicate_ids"),
             _fmt(inst_ev.get("total_inst_net"))))

    # ---- e. 验证：Forward 时间规则（P0-1）--------------------------
    _section("e. 晋升流转 -> validating + Forward 时间规则演示")
    promotion.transition(pool, cid_main, "validating",
                         actor=ACTOR, reason="e2e：研究完成，进入验证")
    main_ev_row = next(
        r for r in pool.events_for(cid_main)
        if r["source_id"] == main_event.source_id)
    print("发现事件 id=%s discovered_at=%s（数据实际可得时间 = "
          "事件创建时间，运行时刻）；数据所属日期=%s"
          % (main_ev_row["id"], main_ev_row["discovered_at"],
             DISCOVERY_DATE))
    fwd_bars, fwd_prov = bars_mod.get_bars(
        main_event.symbol, DISCOVERY_DATE, EVAL_END)
    print("演示 1：规范 forward 请求（不指定 entry，由代码推导发现"
          "创建之后第一个交易日）：现有 bars 截止 %s、无发现创建"
          "之后的 bar、下一交易日收盘尚不可确认，应被拒绝并留痕"
          % (fwd_bars[-1][0] if fwd_bars else "无"))
    try:
        validation.create_sample(
            pool, candidate_id=cid_main,
            source_id=main_event.source_id,
            kind="forward",
            discovery_event_id=main_ev_row["id"], bars=fwd_bars)
        raise AssertionError("Forward 时间规则失效：无下一交易日被放行")
    except validation.ForwardRejectedError as exc:
        print("Forward 创建被拒（预期）：%s" % exc)
    rej_hist = [h for h in pool.history_for(cid_main)
                if "forward_rejected:" in (h["reason"] or "")]
    print("拒绝留痕：candidate_history 中 forward_rejected 共 %d 条"
          % len(rej_hist))
    assert rej_hist, "Forward 拒绝未在 candidate_history 留痕"
    print("演示 2：以历史 entry=%s 请求 kind='forward'（即原样本 1 "
          "的错误形态）：按 P0-1 由代码强制改类 backfill，不信任"
          "调用方 kind" % DISCOVERY_DATE)
    forced_id = validation.create_sample(
        pool, candidate_id=cid_main, source_id=main_event.source_id,
        entry_date=DISCOVERY_DATE,
        benchmark_price=main_event.price_at_discovery,
        kind="forward",
        discovery_event_id=main_ev_row["id"], bars=fwd_bars)
    forced = validation.get_sample(pool, forced_id)
    print("落库样本 id=%s kind=%s（请求 forward -> 强制 backfill）"
          " entry_date=%s benchmark=%s benchmark_date=%s"
          % (forced_id, forced["kind"], forced["entry_date"],
             forced["benchmark_price"], forced["benchmark_date"]))
    assert forced["kind"] == "backfill"
    forced_eval = validation.evaluate_sample(
        pool, forced_id, fwd_bars, data_version=fwd_prov)
    print("评估回写：bars_asof=%s data_version=%s maturity_status=%s"
          % (forced_eval["bars_asof"], forced_eval["data_version"],
             forced_eval["maturity_status"]))
    print("forward 统计（排除审计裁定样本后）：%s"
          % validation.forward_stats(pool, cid_main))

    # ---- f. 验证：回填样本演示 --------------------------------------
    _section("f. 回填样本演示（backfill，与前向严格区分）")
    backfill_symbol = None
    if _earlier_inst_row(main_event.symbol) is not None:
        if _backfill_demo(pool, cid_main, main_event.symbol):
            backfill_symbol = main_event.symbol
    else:
        print("主标的无更早机构上榜记录，回填演示缺失（不编造）："
              "%s 在 %s 或更早无 inst_net>0 的 inst_lhb_daily 行"
              % (main_event.symbol, BACKFILL_CUTOFF))
        others = sorted(
            (e for e in events
             if e.symbol != main_event.symbol
             and e.symbol not in MOTHERBOARD_SYMBOLS
             and e.market in ("SH", "SZ")),
            key=lambda e: -float(e.evidence.get("inst_net") or 0))
        for e in others:
            if _earlier_inst_row(e.symbol) is None:
                continue
            cid_other, _c = pool.ingest(e, actor=ACTOR)
            print("改用当日另一只非母板标的 %s %s（candidate_id=%s）"
                  "做回填演示：" % (e.symbol, e.name, cid_other))
            if _backfill_demo(pool, cid_other, e.symbol):
                backfill_symbol = e.symbol
                break
        if backfill_symbol is None:
            print("当日事件中无其他标的可做回填演示，"
                  "回填演示缺失（不编造）")

    # ---- g. 验证通过门禁（P0-3）------------------------------------
    _section("g. 验证通过门禁：judge 判定与直达拒绝")
    print("门槛状态：验证数值门槛尚未经用户确认（见 "
          "THRESHOLD_PROPOSAL.md），规则注册表为空；judge 应输出 "
          "not_evaluable、状态停在 validating，系统不得产生 "
          "validation_passed。")
    try:
        promotion.transition(pool, cid_main, "validation_passed",
                             actor=ACTOR,
                             reason="e2e：试图绕过验证引擎直达通过态")
        raise AssertionError("validation_passed 直达未被拒，门禁失效")
    except promotion.IllegalTransitionError as exc:
        print("transition(validating -> validation_passed) 被拒"
              "（预期）：%s" % exc)
    decision = validation.judge_validation(pool, cid_main)
    print("judge_validation -> decision=%s rule_version=%s"
          % (decision["decision"], decision["rule_version"]))
    print("  sample_scope=%s" % decision["sample_scope"])
    print("  metrics=%s" % decision["metrics_json"])
    print("  basis=%s" % decision["basis"])
    assert decision["decision"] == "not_evaluable"
    status_now = pool.get_by_id(cid_main)["promotion_status"]
    print("判定后 promotion_status=%s（停在 validating，未产生 "
          "validation_passed）" % status_now)
    assert status_now == "validating"
    try:
        promotion.decide(pool, cid_main, approver="e2e_operator",
                         decision="approved",
                         basis="e2e：未过验证门禁试图审批")
        raise AssertionError("未达 pending_approval 的 decide 未被拒")
    except promotion.IllegalTransitionError as exc:
        print("decide 被拒（预期，状态非 pending_approval）：%s" % exc)
    try:
        handoff.simulate_handoff(pool, cid_main)
        raise AssertionError("未批准交接未被拒，交接门禁失效")
    except handoff.HandoffNotApprovedError as exc:
        print("未批准交接被拒（预期）：%s" % exc)
    attempts = [h for h in pool.history_for(cid_main)
                if h["field"] == "promotion_transition_attempt"]
    print("非法转移留痕：candidate_history 中 promotion_transition_"
          "attempt 共 %d 条，最近一条 reason=%s"
          % (len(attempts), attempts[-1]["reason"] if attempts else "无"))
    assert attempts, "非法转移未留痕"

    # ---- h. 第二只标的同样停在验证门禁 --------------------------------
    _section("h. 第二只标的：流转至 validating 后同样被门禁拦下")
    used = {main_event.symbol, backfill_symbol}
    rej_event = next(
        e for e in sorted(events,
                          key=lambda e: -float(e.evidence.get("inst_net")
                                               or 0))
        if e.symbol not in used
        and e.symbol not in MOTHERBOARD_SYMBOLS
        and e.market in ("SH", "SZ"))
    cid_rej, _c = pool.ingest(rej_event, actor=ACTOR)
    print("演示标的：%s %s %s（candidate_id=%s）"
          % (rej_event.symbol, rej_event.market, rej_event.name, cid_rej))
    for to_state in ("pending_research", "researching", "validating"):
        promotion.transition(pool, cid_rej, to_state, actor=ACTOR,
                             reason="e2e 门禁演示：逐步合法流转")
    decision2 = validation.judge_validation(pool, cid_rej)
    print("judge_validation -> decision=%s，promotion_status=%s"
          % (decision2["decision"],
             pool.get_by_id(cid_rej)["promotion_status"]))
    assert decision2["decision"] == "not_evaluable"
    try:
        handoff.simulate_handoff(pool, cid_rej)
        raise AssertionError("未批准候选的交接未被拒，门禁失效")
    except handoff.HandoffNotApprovedError as exc:
        print("未批准候选交接被拒（预期）：%s" % exc)

    # ---- i. 生产安全自检（只读） ---------------------------------------
    _section("i. 生产安全自检（生产 sim.db 只读）")
    conn = _prod_ro()
    try:
        fills_count, fills_max = conn.execute(
            "SELECT COUNT(*), MAX(id) FROM fills").fetchone()
        sig_count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        appr_count = conn.execute(
            "SELECT COUNT(*) FROM approvals").fetchone()[0]
        intent_count = conn.execute(
            "SELECT COUNT(*) FROM trade_intents").fetchone()[0]
    finally:
        conn.close()
    print("生产 sim.db（mode=ro 只读）：fills=%s（max_id=%s） signals=%s"
          " approvals=%s trade_intents=%s"
          % (fills_count, fills_max, sig_count, appr_count, intent_count))
    print("说明：本脚本对生产库仅以 sqlite3 URI mode=ro 读取，"
          "未写入生产任何表；起止不变的基线对照由交付方复测。")
    handoffs = pool.conn.execute(
        "SELECT id, candidate_id, approval_id, status FROM handoffs"
        " ORDER BY id").fetchall()
    print("新库 handoffs 共 %d 行（全部 status='simulated'）：%s"
          % (len(handoffs), [dict(h) for h in handoffs]))
    assert all(h["status"] == "simulated" for h in handoffs)

    pool.close()
    print("\nE2E RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
