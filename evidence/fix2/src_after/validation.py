#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market-First 验证层：前向/回填样本的创建、到期评估与晋升判定。

口径（冻结）：
- 样本以 (candidate_id, source_id, entry_date) 唯一，重复创建抛
  DuplicateSampleError，绝不覆盖既有样本。
- kind 只许 'forward'（发现时点真实前向）与 'backfill'（事后回填），
  两类严格区分、不得混算。
- 评估只用 entry_date 之后（严格大于）的 bars：ret_tN =
  after[N-1] 收盘 / benchmark_price - 1（N=5/10/20，数据可得才填）；
  max_drawdown 以 benchmark_price 为初始峰值，对 after[:20] 收盘
  序列算 min(close/running_peak - 1)。
- 成熟度：无 bars -> 'missing'（missing_reason='no_price_data'）；
  after 为空或不足 20 根 -> 'immature'；after >= 20 根 -> 'mature'。
- 缺失值一律 NULL，绝不写 0 充数。

Forward 时间规则（第一轮审计修复 P0-1，代码强制，写死）：
- 发现事件实际创建时间 = discovery_events.discovered_at，即数据
  实际可得时间。取其 Asia/Shanghai 日历日期为 discovered_date。
- Forward 的 entry_date 必须是 discovered_date 之后第一个交易日
  （以该标的 bars 序列中实际存在的交易日为准，不用自然日推算）；
  benchmark = entry_date 当日收盘（qfq 序列），benchmark_date =
  entry_date，benchmark_price_source 记录价格来源。
- entry_date <= discovered_date 的样本由代码强制 kind='backfill'，
  不信任调用方传入的 kind。
- Forward 无法确认下一交易日或其收盘价（该日尚未收盘、bars
  缺失）时拒绝创建：candidate_history 留痕（actor='validation'，
  reason 含 'forward_rejected: ...'）后抛 ForwardRejectedError，
  不得猜测、不得用后来取得的行情回填历史入场价。

验证通过门禁（P0-3）：validation_passed 只能由 judge_validation
按版本化规则产生并留 validation_decisions 记录；阈值未经用户
确认前规则注册表为空（NO_RULE），judge 一律 not_evaluable，
系统不得产生 validation_passed。未成熟样本、指标 NULL、仅有
backfill 样本三条硬门槛在判定函数前置拦截，与数值阈值分离。

Benchmark 收盘完整性（独立验收前补正 fix2-1，写死）：
- 不得仅凭 bars 中存在某日记录就认定该日收盘价已经形成。
  bar_close_confirmed(bar_date, fetched_at, provenance, market)
  为唯一判定入口：bar 日期严格早于取数日期（Asia/Shanghai）
  -> 已收盘确认；bar 日期等于取数日期 -> 仅当取数时刻已过该
  市场收盘确认时刻（A股/北交所/可转债 15:30 = 15:00 收盘 +
  30 分钟缓冲；港股 16:30）且 provenance 无 intraday 标记时
  才确认；bar 日期晚于取数日期 -> 数据异常，不确认。
  依据边界：market_data 包/service 不提供 bar 完整性或
  intraday 标记（检视 service.py/quality.py 无此字段，返回的
  Bar 仅含 OHLCV），故以时间规则为准，provenance 的
  intraday 标记为预留否决项（当前 provenance 串均不含该标记）。
- create_sample 的 forward/backfill 路径都必须以通过完整性
  确认的收盘作为 benchmark；不确认 -> 留痕拒绝
  （ForwardRejectedError，reason 含 benchmark_close_unconfirmed）。
- benchmark 一经记录不可变：evaluate_sample 若发现当前取到
  的 entry 日收盘与已记录 benchmark_price 不一致（行情源
  修订），不得静默覆盖——把首次发现的修订记入样本行
  benchmark_revision_json（旧值/新值/发现时间），每次发现
  另在 candidate_history 留痕，评估始终以原 benchmark 计算。

规则注册保护（fix2-3）：正式 judge 只接受 rule_id+version，
从 rule_registry 注册表（落库）加载并核对状态；规则对象
直传参数已删除。status != 'approved'（draft/test/disabled）
一律 not_evaluable；未经用户确认，本轮注册表不得出现
approved 规则，judge 恒 not_evaluable。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

VALID_KINDS = ("forward", "backfill")
DEFAULT_EVAL_VERSION = "mf-val-1.0"

_SHANGHAI = timezone(timedelta(hours=8))


class DuplicateSampleError(Exception):
    """同 (candidate_id, source_id, entry_date) 样本已存在。"""


class ForwardRejectedError(ValueError):
    """样本创建被时间/完整性规则拒绝（已在 candidate_history 留痕）。

    fix2 起同时用于 backfill 路径的 benchmark 完整性拒绝，
    故继承 ValueError 以兼容既有按 ValueError 捕获的调用方。
    """


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _shanghai_date(iso_str) -> str:
    """ISO 时间戳 -> Asia/Shanghai 日历日期 'YYYY-MM-DD'。

    无时区的字符串按 Asia/Shanghai 本地时间解释；解析失败退回
    前 10 个字符（日期部分）。
    """
    s = str(iso_str)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return s[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_SHANGHAI)
    return dt.astimezone(_SHANGHAI).date().isoformat()


def _resolve_conn(pool):
    """从 CandidatePool 解析 sqlite 连接 -> (conn, owns_conn)。

    依次尝试：pool.conn/connection/_conn/db（Connection 实例）、
    pool.db_path/path/db_file/_db_path/db（路径，经 market_first.db.connect
    优先、sqlite3.connect 兜底）。解析不到抛 AttributeError。
    """
    for attr in ("conn", "connection", "_conn", "db"):
        c = getattr(pool, attr, None)
        if isinstance(c, sqlite3.Connection):
            return c, False
    for attr in ("db_path", "path", "db_file", "_db_path", "db"):
        p = getattr(pool, attr, None)
        if p:
            try:
                from market_first.db import connect as _mf_connect
                return _mf_connect(str(p)), True
            except Exception:  # noqa: BLE001
                return sqlite3.connect(str(p)), True
    raise AttributeError(
        "cannot resolve sqlite connection from pool %r: no conn/db_path"
        % (pool,))


def _row_dict(cur, row):
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _ensure(conn):
    from market_first.db import ensure_fix1_schema, ensure_fix2_schema
    ensure_fix1_schema(conn)
    ensure_fix2_schema(conn)


def _shanghai_dt(iso_str) -> datetime:
    """ISO 时间戳 -> 带 Asia/Shanghai 时区的 datetime。

    无时区字符串按 Asia/Shanghai 本地时间解释。
    """
    s = str(iso_str)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_SHANGHAI)
    return dt.astimezone(_SHANGHAI)


# 收盘确认时刻（Asia/Shanghai）：收盘时刻 + 30 分钟缓冲。
# A股/北交所/可转债 15:00 收盘 -> 15:30 确认；港股 16:00
# 收盘 -> 16:30 确认。未列市场按 A股规则（15:30）。
from datetime import time as _time  # noqa: E402

CLOSE_CONFIRM_TIME = {
    "SH": _time(15, 30),
    "SZ": _time(15, 30),
    "BJ": _time(15, 30),
    "CB": _time(15, 30),
    "HK": _time(16, 30),
}
DEFAULT_CLOSE_CONFIRM_TIME = _time(15, 30)


def bar_close_confirmed(bar_date, fetched_at, provenance=None,
                        market=None) -> bool:
    """bar 收盘完整性判定（fix2-1 唯一入口，规则见模块 docstring）。

    - bar_date 严格早于 fetched_at 的上海日历日期 -> True。
    - bar_date 晚于取数日期 -> False（数据异常）。
    - 同日：provenance 含 'intraday' 标记 -> False；取数时刻
      已过该市场收盘确认时刻（CLOSE_CONFIRM_TIME）-> True，
      否则 False。
    """
    bd = str(bar_date)[:10]
    fdt = _shanghai_dt(fetched_at)
    fd = fdt.date().isoformat()
    if bd < fd:
        return True
    if bd > fd:
        return False
    if provenance and "intraday" in str(provenance).lower():
        return False
    cutoff = CLOSE_CONFIRM_TIME.get(
        str(market or "").upper(), DEFAULT_CLOSE_CONFIRM_TIME)
    return fdt.time() >= cutoff


def _resolve_event(conn, candidate_id, source_id, discovery_event_id):
    """解析样本关联的发现事件 -> dict 或 None。

    显式 discovery_event_id 优先（须存在且属于该候选）；否则取该
    候选 source_id 相同的最近事件，再否则取最近事件；无事件返回
    None。
    """
    if discovery_event_id is not None:
        cur = conn.execute(
            "SELECT * FROM discovery_events WHERE id = ?",
            (discovery_event_id,))
        ev = _row_dict(cur, cur.fetchone())
        if ev is None:
            raise ValueError(
                "discovery_event not found: id=%r" % (discovery_event_id,))
        if ev["candidate_id"] != candidate_id:
            raise ValueError(
                "discovery_event %r belongs to candidate %r, not %r"
                % (discovery_event_id, ev["candidate_id"], candidate_id))
        return ev
    cur = conn.execute(
        "SELECT * FROM discovery_events WHERE candidate_id = ?"
        " ORDER BY discovered_at DESC, id DESC", (candidate_id,))
    rows = [_row_dict(cur, r) for r in cur.fetchall()]
    if not rows:
        return None
    for ev in rows:
        if ev.get("source_id") == source_id:
            return ev
    return rows[0]


def _derive_forward_entry(bars, discovered_date):
    """bars 中 discovered_date 之后第一根 -> (date_str, close) 或 None。"""
    for b in sorted(bars or [], key=lambda x: str(x[0])):
        d = str(b[0])[:10]
        if d > discovered_date:
            return d, float(b[4])
    return None


def _fetch_bars_for(pool, candidate_id, discovered_date):
    """经 market_first.bars 取该标的 discovered_date 起的 bars。

    返回 (bars, provenance)；取数失败返回 ([], 说明)。bars 层已与
    生产共享缓存隔离（use_cache=False + 自有 fetched_bars 表）。
    """
    cand = pool.get_by_id(candidate_id) or {}
    symbol = cand.get("symbol")
    if not symbol:
        return [], "unavailable: candidate has no symbol"
    try:
        from market_first import bars as bars_mod
        start = datetime.fromisoformat(discovered_date).date()
        end = start + timedelta(days=31)
        return bars_mod.get_bars(symbol, start.isoformat(),
                                 end.isoformat())
    except Exception as e:  # noqa: BLE001
        return [], "unavailable: %s: %s" % (type(e).__name__, e)


def _reject_forward(pool, candidate_id, detail):
    """Forward 拒绝统一路径：先在 candidate_history 留痕，再抛错。"""
    reason = "forward_rejected: %s" % detail
    pool.record_history(
        candidate_id,
        field="validation_sample",
        old=None,
        new=None,
        actor="validation",
        reason=reason,
    )
    raise ForwardRejectedError(reason)


def create_sample(pool, candidate_id, source_id, entry_date=None,
                  benchmark_price=None, kind="backfill",
                  discovery_event_id=None, bars=None,
                  benchmark_price_source=None, data_version=None,
                  fetched_at=None) -> int:
    """创建验证样本 -> sample_id（时间规则见模块 docstring）。

    - entry_date <= discovered_date（发现事件的上海日历日期）时，
      无论调用方传何 kind，一律强制 kind='backfill' 落库。
    - kind='forward' 时 entry/benchmark 不信任调用方入参：按 bars
      （入参或经 bars 层现取）推导 discovered_date 之后第一交易日
      及其收盘；无法推导则留痕拒绝（ForwardRejectedError）。
    - kind='backfill' 必须显式给 entry_date 与 benchmark_price。
    - fix2-1：forward/backfill 的 benchmark 都必须通过
      bar_close_confirmed 完整性确认（以 fetched_at 为取数时刻，
      缺省为当前时刻）；不确认则留痕拒绝，reason 含
      'benchmark_close_unconfirmed'。
    - UNIQUE(candidate_id, source_id, entry_date) 冲突转抛
      DuplicateSampleError。创建成功后置 validation_status。
    """
    fetched_at = fetched_at or _now_iso()
    if kind not in VALID_KINDS:
        raise ValueError("invalid sample kind: %r (allowed: %s)"
                         % (kind, "/".join(VALID_KINDS)))
    conn, owns = _resolve_conn(pool)
    try:
        _ensure(conn)
        event = _resolve_event(conn, candidate_id, source_id,
                               discovery_event_id)
        discovered_date = (_shanghai_date(event["discovered_at"])
                           if event else None)
        ev_id = event["id"] if event else None

        req_entry = str(entry_date)[:10] if entry_date else None
        final_kind = kind
        if (discovered_date is not None and req_entry is not None
                and req_entry <= discovered_date):
            # 入场不晚于数据实际可得日 -> 事后回填，代码强制改类
            final_kind = "backfill"

        bench_source = benchmark_price_source
        if final_kind == "forward":
            if event is None:
                _reject_forward(
                    pool, candidate_id,
                    "no discovery event linked; forward requires a "
                    "discovery event as its availability anchor")
            bars_use = bars
            prov = None
            if bars_use is None:
                bars_use, prov = _fetch_bars_for(
                    pool, candidate_id, discovered_date)
            derived = _derive_forward_entry(bars_use, discovered_date)
            if derived is None:
                _reject_forward(
                    pool, candidate_id,
                    "next_trading_day_unconfirmed: no bar after "
                    "discovered_date=%s (bars=%d, provenance=%s); "
                    "refusing to guess an entry point"
                    % (discovered_date, len(bars_use or []), prov))
            final_entry, final_bench = derived
            bench_date = final_entry
            if bench_source is None:
                bench_source = prov or "provided_bars close (qfq)"
            # fix2-1：推导出的 entry 日 bar 必须通过收盘完整性
            # 确认才可作 benchmark（不得仅凭 bar 存在认定收盘）。
            cand = pool.get_by_id(candidate_id) or {}
            if not bar_close_confirmed(
                    final_entry, fetched_at,
                    provenance=bench_source,
                    market=cand.get("market")):
                _reject_forward(
                    pool, candidate_id,
                    "benchmark_close_unconfirmed: entry bar %s not "
                    "confirmed closed at fetched_at=%s "
                    "(provenance=%s); refusing to use an incomplete "
                    "bar as benchmark"
                    % (final_entry, fetched_at, bench_source))
        else:
            if req_entry is None or benchmark_price is None:
                raise ValueError(
                    "backfill sample requires explicit entry_date and "
                    "benchmark_price")
            final_entry = req_entry
            final_bench = float(benchmark_price)
            bench_date = final_entry
            # fix2-1：backfill 同受完整性约束（历史日期严格早于
            # 取数日天然确认；entry 为取数当日且未过收盘确认
            # 时刻、或晚于取数日，一律留痕拒绝）。
            cand = pool.get_by_id(candidate_id) or {}
            if not bar_close_confirmed(
                    final_entry, fetched_at,
                    provenance=bench_source,
                    market=cand.get("market")):
                reason = (
                    "backfill_rejected: benchmark_close_unconfirmed:"
                    " entry_date=%s not confirmed closed at "
                    "fetched_at=%s" % (final_entry, fetched_at))
                pool.record_history(
                    candidate_id, field="validation_sample",
                    old=None, new=None, actor="validation",
                    reason=reason)
                raise ForwardRejectedError(reason)

        try:
            cur = conn.execute(
                "INSERT INTO validation_samples "
                "(candidate_id, source_id, kind, entry_date, "
                " benchmark_price, maturity_status, created_at, "
                " discovery_event_id, benchmark_date, "
                " benchmark_price_source, data_version) "
                "VALUES (?, ?, ?, ?, ?, 'immature', ?, ?, ?, ?, ?)",
                (candidate_id, source_id, final_kind, final_entry,
                 float(final_bench), _now_iso(), ev_id, bench_date,
                 bench_source, data_version))
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise DuplicateSampleError(
                "duplicate validation sample: candidate_id=%r "
                "source_id=%r entry_date=%r (%s)"
                % (candidate_id, source_id, final_entry, e)) from e
        sample_id = cur.lastrowid
    finally:
        if owns:
            conn.close()
    pool.set_fields(candidate_id, actor="validation",
                    validation_status="in_progress")
    return sample_id


def get_sample(pool, sample_id) -> dict:
    """按 id 读样本行 -> dict；不存在返回 None。"""
    conn, owns = _resolve_conn(pool)
    try:
        cur = conn.execute(
            "SELECT * FROM validation_samples WHERE id = ?", (sample_id,))
        return _row_dict(cur, cur.fetchone())
    finally:
        if owns:
            conn.close()


def evaluate_sample(pool, sample_id, bars, eval_version=DEFAULT_EVAL_VERSION,
                    data_version=None) -> dict:
    """评估样本并回写 -> 更新后的样本行 dict。

    after = [b for b in bars if b[0] > entry_date]（日期字符串比较，
    bars 日期须为 'YYYY-MM-DD'）。成熟度口径见模块 docstring。
    回写同时记录 bars_asof（本次评估所用 bars 的最大日期）与
    data_version（评估数据版本/来源标识，入参传入）。
    """
    sample = get_sample(pool, sample_id)
    if sample is None:
        raise ValueError("validation sample not found: id=%r" % (sample_id,))
    entry_date = str(sample["entry_date"])
    benchmark = float(sample["benchmark_price"])
    bars = bars or []
    bars_asof = max((str(b[0])[:10] for b in bars), default=None)
    after = [b for b in bars if str(b[0]) > entry_date]

    # fix2-1：benchmark 不可变。当前 bars 中 entry 日收盘与已
    # 记录 benchmark 不一致 = 行情源事后修订：只记录、绝不
    # 覆盖 benchmark，评估继续以原 benchmark 计算。
    revision = None
    for b in bars:
        if str(b[0])[:10] == entry_date:
            observed_close = float(b[4])
            if abs(observed_close - benchmark) > 1e-9:
                revision = {
                    "detected": True,
                    "entry_date": entry_date,
                    "recorded_benchmark": benchmark,
                    "observed_close": observed_close,
                    "detected_at": _now_iso(),
                    "eval_version": eval_version,
                }
            break
    if revision is not None:
        pool.record_history(
            sample["candidate_id"], field="benchmark_revision",
            old=benchmark, new=revision["observed_close"],
            actor="validation",
            reason="benchmark_revision_detected: entry=%s "
                   "recorded=%s observed=%s; benchmark immutable, "
                   "evaluation keeps the recorded value"
                   % (entry_date, benchmark,
                      revision["observed_close"]))

    ret_t5 = ret_t10 = ret_t20 = None
    max_drawdown = None
    missing_reason = None

    if not bars:
        maturity_status = "missing"
        missing_reason = "no_price_data"
    elif not after:
        maturity_status = "immature"
    else:
        closes = [float(b[4]) for b in after]
        if len(after) >= 5:
            ret_t5 = closes[4] / benchmark - 1.0
        if len(after) >= 10:
            ret_t10 = closes[9] / benchmark - 1.0
        if len(after) >= 20:
            ret_t20 = closes[19] / benchmark - 1.0
        peak = benchmark
        worst = None
        for c in closes[:20]:
            if c > peak:
                peak = c
            dd = c / peak - 1.0
            worst = dd if worst is None else min(worst, dd)
        max_drawdown = worst
        maturity_status = "mature" if len(after) >= 20 else "immature"

    conn, owns = _resolve_conn(pool)
    try:
        _ensure(conn)
        conn.execute(
            "UPDATE validation_samples SET ret_t5 = ?, ret_t10 = ?, "
            "ret_t20 = ?, max_drawdown = ?, maturity_status = ?, "
            "missing_reason = ?, evaluated_at = ?, eval_version = ?, "
            "bars_asof = ?, data_version = ?, "
            "benchmark_revision_json = COALESCE("
            "benchmark_revision_json, ?) "
            "WHERE id = ?",
            (ret_t5, ret_t10, ret_t20, max_drawdown, maturity_status,
             missing_reason, _now_iso(), eval_version, bars_asof,
             data_version,
             json.dumps(revision, ensure_ascii=False)
             if revision is not None else None,
             sample_id))
        conn.commit()
    finally:
        if owns:
            conn.close()
    return get_sample(pool, sample_id)


# ---------------------------------------------------------------------------
# 样本排除（审计裁定）与 Forward 统计
# ---------------------------------------------------------------------------

def exclude_sample(pool, sample_id, excluded_from, reason,
                   decided_by) -> int:
    """为样本写一条排除记录（如审计裁定不进 forward 统计）。

    只新增 sample_exclusions 行，绝不改动样本行本身。重复排除
    同一 (sample_id, excluded_from) 抛 ValueError。
    """
    conn, owns = _resolve_conn(pool)
    try:
        _ensure(conn)
        if get_sample(pool, sample_id) is None:
            raise ValueError("validation sample not found: id=%r"
                             % (sample_id,))
        try:
            cur = conn.execute(
                "INSERT INTO sample_exclusions "
                "(sample_id, excluded_from, reason, decided_by, "
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (sample_id, excluded_from, reason, decided_by,
                 _now_iso()))
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise ValueError(
                "sample %r already excluded from %r (%s)"
                % (sample_id, excluded_from, e)) from e
        return cur.lastrowid
    finally:
        if owns:
            conn.close()


def excluded_sample_ids(pool, excluded_from="forward_stats") -> set:
    """被指定口径排除的样本 id 集合。"""
    conn, owns = _resolve_conn(pool)
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT sample_id FROM sample_exclusions"
            " WHERE excluded_from = ?", (excluded_from,)).fetchall()
        return {r[0] for r in rows}
    finally:
        if owns:
            conn.close()


def _samples_for(pool, candidate_id=None):
    conn, owns = _resolve_conn(pool)
    try:
        _ensure(conn)
        sql = "SELECT * FROM validation_samples"
        params = ()
        if candidate_id is not None:
            sql += " WHERE candidate_id = ?"
            params = (candidate_id,)
        sql += " ORDER BY id ASC"
        cur = conn.execute(sql, params)
        return [_row_dict(cur, r) for r in cur.fetchall()]
    finally:
        if owns:
            conn.close()


def forward_stats(pool, candidate_id=None) -> dict:
    """Forward 统计口径：仅 kind='forward' 且未被 forward_stats
    排除的样本。被排除样本单独列出，绝不混入统计。"""
    excluded = excluded_sample_ids(pool, "forward_stats")
    samples = _samples_for(pool, candidate_id)
    fwd = [s for s in samples
           if s["kind"] == "forward" and s["id"] not in excluded]
    return {
        "sample_ids": [s["id"] for s in fwd],
        "total": len(fwd),
        "mature": sum(1 for s in fwd
                      if s["maturity_status"] == "mature"),
        "immature": sum(1 for s in fwd
                        if s["maturity_status"] == "immature"),
        "missing": sum(1 for s in fwd
                       if s["maturity_status"] == "missing"),
        "excluded_sample_ids": sorted(
            s["id"] for s in samples
            if s["kind"] == "forward" and s["id"] in excluded),
    }


# ---------------------------------------------------------------------------
# 验证通过判定（P0-3 门禁）：validation_passed 的唯一产生路径
# ---------------------------------------------------------------------------

NO_RULE = "NO_RULE"

RULE_STATUSES = ("draft", "approved", "disabled", "test")

# 代码侧规则实现注册表：rule_id -> ValidationRule 实例。正式
# judge 只按 rule_id 从这里取实现，且必须同时满足 rule_registry
# 落库表中存在同 rule_id 的 status='approved' 版本行。本轮不
# 预置任何实现（数值门槛未经用户确认）。
RULE_IMPLEMENTATIONS: dict = {}


class ValidationRule:
    """版本化验证规则接口（数值阈值由用户确认后另行实现登记）。

    evaluate(scope, metrics) -> (decision, basis)，
    decision 只许 'passed' / 'not_passed'。硬门槛（无 forward
    样本 / 未成熟 / 指标 NULL）由 judge_validation 前置拦截，
    不进入规则的数值判定。
    """

    version = "abstract"

    def evaluate(self, scope, metrics):  # pragma: no cover - 接口
        raise NotImplementedError


def register_rule_implementation(rule_id, rule):
    """登记代码侧规则实现（rule 须提供 evaluate(scope, metrics)）。

    只登记实现不等于批准：正式判定还要求 rule_registry 表中
    有同 rule_id 的 approved 版本行（见 register_rule）。
    """
    if not hasattr(rule, "evaluate"):
        raise TypeError(
            "rule implementation must provide evaluate(scope, metrics)")
    RULE_IMPLEMENTATIONS[str(rule_id)] = rule
    return str(rule_id)


def register_rule(pool, rule_id, version, status="draft", params=None,
                  approved_by=None, approved_at=None) -> int:
    """向 rule_registry 落库表登记一个规则版本 -> row id。

    status 只许 draft/approved/disabled/test；status='approved'
    必须带 approved_by（批准留痕）。重复 (rule_id, version)
    抛 ValueError。注册只新增行，绝不改写既有判定记录；
    未经用户确认的规则不得以 approved 登记到正式注册表。
    """
    if status not in RULE_STATUSES:
        raise ValueError("invalid rule status: %r (allowed: %s)"
                         % (status, "/".join(RULE_STATUSES)))
    if status == "approved" and not approved_by:
        raise ValueError(
            "approved rule registration requires approved_by")
    conn, owns = _resolve_conn(pool)
    try:
        _ensure(conn)
        try:
            cur = conn.execute(
                "INSERT INTO rule_registry "
                "(rule_id, version, status, approved_by, approved_at,"
                " params_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(rule_id), str(version), status, approved_by,
                 approved_at,
                 json.dumps(params or {}, ensure_ascii=False),
                 _now_iso()))
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise ValueError(
                "rule already registered: %s@%s (%s)"
                % (rule_id, version, e)) from e
        return cur.lastrowid
    finally:
        if owns:
            conn.close()


def set_rule_status(pool, rule_id, version, status) -> None:
    """变更已登记规则版本的状态（如 approved -> disabled）。

    只影响之后的判定；既有 validation_decisions 记录（含其
    rule_version 与 metrics）不随注册表变更而改写。
    """
    if status not in RULE_STATUSES:
        raise ValueError("invalid rule status: %r (allowed: %s)"
                         % (status, "/".join(RULE_STATUSES)))
    conn, owns = _resolve_conn(pool)
    try:
        _ensure(conn)
        cur = conn.execute(
            "UPDATE rule_registry SET status = ?"
            " WHERE rule_id = ? AND version = ?",
            (status, str(rule_id), str(version)))
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError("rule not registered: %s@%s"
                             % (rule_id, version))
    finally:
        if owns:
            conn.close()


def get_registered_rule(pool, rule_id, version):
    """读 rule_registry 中 (rule_id, version) 一行 -> dict/None。"""
    conn, owns = _resolve_conn(pool)
    try:
        _ensure(conn)
        cur = conn.execute(
            "SELECT * FROM rule_registry"
            " WHERE rule_id = ? AND version = ?",
            (str(rule_id), str(version)))
        return _row_dict(cur, cur.fetchone())
    finally:
        if owns:
            conn.close()


def judge_validation(pool, candidate_id, rule_id=None,
                     rule_version=None, actor="validation") -> dict:
    """验证判定：汇总候选全部样本并按注册规则判定，写
    validation_decisions 一行，返回该行 dict。

    fix2-3 注册保护：正式判定只接受 rule_id+rule_version 并
    从 rule_registry 注册表加载，规则对象直传参数已删除。
    - rule_id=None（默认）：decision='not_evaluable'、
      rule_version=NO_RULE、basis 含 'no_approved_rule'。
    - 未登记 / 版本不符 / status != 'approved'（draft/test/
      disabled）/ 代码侧无实现：一律 'not_evaluable' 并在
      basis 注明原因，判定留痕，不流转状态。
    - 已登记 approved 规则：硬门槛前置——无（未排除的）
      forward 样本 -> 'not_passed'（backfill 不得替代）；
      forward 未成熟或关键指标（ret_t20/max_drawdown）为
      NULL -> 'not_passed'；其后才进规则的数值判定。
    - decision='passed' 时经 promotion 内部受控路径（须携带
      本次判定记录 id 供其核验）流转到 validation_passed。
    """
    conn, owns = _resolve_conn(pool)
    try:
        _ensure(conn)
        samples = _samples_for(pool, candidate_id)
        excluded = excluded_sample_ids(pool, "forward_stats")
        fwd = [s for s in samples
               if s["kind"] == "forward" and s["id"] not in excluded]
        bf = [s for s in samples if s["kind"] == "backfill"]
        scope = {
            "forward_sample_ids": [s["id"] for s in fwd],
            "backfill_sample_ids": [s["id"] for s in bf],
            "excluded_sample_ids": sorted(
                s["id"] for s in samples if s["id"] in excluded),
        }
        null_metric_ids = [
            s["id"] for s in fwd
            if s["ret_t20"] is None or s["max_drawdown"] is None]
        metrics = {
            "forward_count": len(fwd),
            "forward_mature": sum(
                1 for s in fwd if s["maturity_status"] == "mature"),
            "forward_immature": sum(
                1 for s in fwd if s["maturity_status"] == "immature"),
            "forward_missing": sum(
                1 for s in fwd if s["maturity_status"] == "missing"),
            "backfill_count": len(bf),
            "forward_null_metric_sample_ids": null_metric_ids,
        }

        impl = None
        if rule_id is None:
            decision = "not_evaluable"
            recorded_version = NO_RULE
            basis = ("no_approved_rule: 验证数值门槛尚未经用户确认，"
                     "注册表无 approved 规则；系统只能记录验证"
                     "结果，不得产生 validation_passed")
        elif rule_version is None:
            decision = "not_evaluable"
            recorded_version = NO_RULE
            basis = ("rule_version_required: 正式判定必须显式给出"
                     " rule_id 与 version（请求 rule_id=%s 未带"
                     " version），拒绝按未登记规则判定" % (rule_id,))
        else:
            reg = get_registered_rule(pool, rule_id, rule_version)
            if reg is None:
                decision = "not_evaluable"
                recorded_version = NO_RULE
                basis = ("rule_not_registered: rule_id=%s "
                         "version=%s 未在 rule_registry 登记，"
                         "正式判定拒绝" % (rule_id, rule_version))
            elif reg["status"] != "approved":
                decision = "not_evaluable"
                recorded_version = NO_RULE
                basis = ("rule_not_approved: rule_id=%s version=%s "
                         "status=%s；draft/test/disabled 规则不得"
                         "用于正式判定"
                         % (rule_id, rule_version, reg["status"]))
            else:
                impl = RULE_IMPLEMENTATIONS.get(str(rule_id))
                if impl is None:
                    decision = "not_evaluable"
                    recorded_version = NO_RULE
                    basis = ("rule_implementation_missing: "
                             "rule_id=%s 已登记 approved 但代码侧"
                             "无实现，无法执行数值判定" % (rule_id,))
                else:
                    recorded_version = rule_version
                    if not fwd:
                        decision = "not_passed"
                        basis = ("no_forward_samples: 无未被排除的"
                                 " forward 样本；backfill 不得替代"
                                 "真实 forward 样本")
                    elif (metrics["forward_mature"] < len(fwd)
                            or null_metric_ids):
                        decision = "not_passed"
                        basis = ("immature_or_missing_metrics: "
                                 "未成熟样本不得判定通过，缺失指标"
                                 "保持 NULL 不按零值处理 "
                                 "(immature=%d, "
                                 "null_metric_sample_ids=%s)"
                                 % (metrics["forward_immature"],
                                    null_metric_ids))
                    else:
                        decision, basis = impl.evaluate(scope, metrics)
                        if decision not in ("passed", "not_passed"):
                            raise ValueError(
                                "rule %s@%s returned invalid "
                                "decision: %r"
                                % (rule_id, rule_version, decision))

        cur = conn.execute(
            "INSERT INTO validation_decisions "
            "(candidate_id, rule_version, sample_scope, metrics_json,"
            " decision, basis, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, recorded_version,
             json.dumps(scope, ensure_ascii=False),
             json.dumps(metrics, ensure_ascii=False),
             decision, basis, _now_iso()))
        conn.commit()
        decision_id = cur.lastrowid
        cur = conn.execute(
            "SELECT * FROM validation_decisions WHERE id = ?",
            (decision_id,))
        row = _row_dict(cur, cur.fetchone())
    finally:
        if owns:
            conn.close()

    if decision == "passed":
        from market_first import promotion
        promotion._apply_validation_passed(
            pool, candidate_id, actor=actor,
            reason="judge_validation passed: rule_version=%s "
                   "decision_id=%s" % (recorded_version, decision_id),
            decision_id=decision_id)
    return row
