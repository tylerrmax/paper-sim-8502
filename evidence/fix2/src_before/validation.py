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


class ForwardRejectedError(Exception):
    """Forward 样本创建被时间规则拒绝（已在 candidate_history 留痕）。"""


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
    from market_first.db import ensure_fix1_schema
    ensure_fix1_schema(conn)


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
                  benchmark_price_source=None, data_version=None) -> int:
    """创建验证样本 -> sample_id（时间规则见模块 docstring）。

    - entry_date <= discovered_date（发现事件的上海日历日期）时，
      无论调用方传何 kind，一律强制 kind='backfill' 落库。
    - kind='forward' 时 entry/benchmark 不信任调用方入参：按 bars
      （入参或经 bars 层现取）推导 discovered_date 之后第一交易日
      及其收盘；无法推导则留痕拒绝（ForwardRejectedError）。
    - kind='backfill' 必须显式给 entry_date 与 benchmark_price。
    - UNIQUE(candidate_id, source_id, entry_date) 冲突转抛
      DuplicateSampleError。创建成功后置 validation_status。
    """
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
        else:
            if req_entry is None or benchmark_price is None:
                raise ValueError(
                    "backfill sample requires explicit entry_date and "
                    "benchmark_price")
            final_entry = req_entry
            final_bench = float(benchmark_price)
            bench_date = final_entry

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
            "bars_asof = ?, data_version = ? "
            "WHERE id = ?",
            (ret_t5, ret_t10, ret_t20, max_drawdown, maturity_status,
             missing_reason, _now_iso(), eval_version, bars_asof,
             data_version, sample_id))
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

# 规则注册表：只登记经用户确认的版本化规则。当前为空——阈值未
# 定版，任何规则不得在此自行登记生效。
RULE_REGISTRY: dict = {}


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


def judge_validation(pool, candidate_id, rule=None,
                     actor="validation") -> dict:
    """验证判定：汇总候选全部样本并按规则判定，写
    validation_decisions 一行，返回该行 dict。

    - rule=None（默认）：查 RULE_REGISTRY，无经确认规则时一律
      decision='not_evaluable'、rule_version=NO_RULE、
      basis 含 'no_approved_rule'，不流转状态。
    - 硬门槛前置：无（未排除的）forward 样本 -> 'not_passed'
      （backfill 不得替代 forward）；forward 样本未成熟或关键
      指标（ret_t20/max_drawdown）为 NULL -> 'not_passed'。
    - decision='passed' 时经 promotion 内部受控路径流转到
      validation_passed（要求当前状态为 validating）。
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

        if rule is None:
            decision = "not_evaluable"
            rule_version = NO_RULE
            basis = ("no_approved_rule: 验证数值门槛尚未经用户确认，"
                     "规则注册表为空；系统只能记录验证结果，"
                     "不得产生 validation_passed")
        elif not fwd:
            decision = "not_passed"
            rule_version = getattr(rule, "version", None)
            basis = ("no_forward_samples: 无未被排除的 forward 样本；"
                     "backfill 不得替代真实 forward 样本")
        elif metrics["forward_mature"] < len(fwd) or null_metric_ids:
            decision = "not_passed"
            rule_version = getattr(rule, "version", None)
            basis = ("immature_or_missing_metrics: 未成熟样本不得"
                     "判定通过，缺失指标保持 NULL 不按零值处理 "
                     "(immature=%d, null_metric_sample_ids=%s)"
                     % (metrics["forward_immature"], null_metric_ids))
        else:
            decision, basis = rule.evaluate(scope, metrics)
            if decision not in ("passed", "not_passed"):
                raise ValueError(
                    "rule %r returned invalid decision: %r"
                    % (rule_version, decision))
            rule_version = getattr(rule, "version", None)

        cur = conn.execute(
            "INSERT INTO validation_decisions "
            "(candidate_id, rule_version, sample_scope, metrics_json,"
            " decision, basis, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, rule_version,
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
                   "decision_id=%s" % (rule_version, decision_id))
    return row
