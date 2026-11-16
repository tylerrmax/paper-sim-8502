#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Market-First 验证层：前向/回填样本的创建与到期评估。

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
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

VALID_KINDS = ("forward", "backfill")
DEFAULT_EVAL_VERSION = "mf-val-1.0"


class DuplicateSampleError(Exception):
    """同 (candidate_id, source_id, entry_date) 样本已存在。"""


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def create_sample(pool, candidate_id, source_id, entry_date,
                  benchmark_price, kind) -> int:
    """创建验证样本 -> sample_id。

    kind 非法抛 ValueError；UNIQUE(candidate_id, source_id, entry_date)
    冲突（sqlite3.IntegrityError）转抛 DuplicateSampleError。
    创建成功后 pool.set_fields(validation_status='in_progress')。
    """
    if kind not in VALID_KINDS:
        raise ValueError("invalid sample kind: %r (allowed: %s)"
                         % (kind, "/".join(VALID_KINDS)))
    conn, owns = _resolve_conn(pool)
    try:
        try:
            cur = conn.execute(
                "INSERT INTO validation_samples "
                "(candidate_id, source_id, kind, entry_date, "
                " benchmark_price, maturity_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'immature', ?)",
                (candidate_id, source_id, kind, str(entry_date),
                 float(benchmark_price), _now_iso()))
            conn.commit()
        except sqlite3.IntegrityError as e:
            raise DuplicateSampleError(
                "duplicate validation sample: candidate_id=%r "
                "source_id=%r entry_date=%r (%s)"
                % (candidate_id, source_id, entry_date, e)) from e
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


def evaluate_sample(pool, sample_id, bars, eval_version=DEFAULT_EVAL_VERSION) -> dict:
    """评估样本并回写 -> 更新后的样本行 dict。

    after = [b for b in bars if b[0] > entry_date]（日期字符串比较，
    bars 日期须为 'YYYY-MM-DD'）。详见模块 docstring 的成熟度口径。
    """
    sample = get_sample(pool, sample_id)
    if sample is None:
        raise ValueError("validation sample not found: id=%r" % (sample_id,))
    entry_date = str(sample["entry_date"])
    benchmark = float(sample["benchmark_price"])
    bars = bars or []
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
        conn.execute(
            "UPDATE validation_samples SET ret_t5 = ?, ret_t10 = ?, "
            "ret_t20 = ?, max_drawdown = ?, maturity_status = ?, "
            "missing_reason = ?, evaluated_at = ?, eval_version = ? "
            "WHERE id = ?",
            (ret_t5, ret_t10, ret_t20, max_drawdown, maturity_status,
             missing_reason, _now_iso(), eval_version, sample_id))
        conn.commit()
    finally:
        if owns:
            conn.close()
    return get_sample(pool, sample_id)
