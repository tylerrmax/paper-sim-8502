"""Market-First 独立 SQLite 存储：建表与连接。

新库默认路径为本包目录下的 market_first.db，与生产 sim.db 隔离。
SCHEMA_SQL 一次建全（含研究/验证/审批/交接模块的表）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent / "market_first.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    market TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    first_seen_at TEXT,
    last_seen_at TEXT,
    research_status TEXT DEFAULT 'pending',
    validation_status TEXT DEFAULT 'none',
    promotion_status TEXT DEFAULT 'discovered',
    risk_status TEXT DEFAULT 'normal',
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(market, symbol)
);

CREATE TABLE IF NOT EXISTS discovery_events (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    source_id TEXT,
    market TEXT,
    symbol TEXT,
    discovered_at TEXT,
    trigger_condition TEXT,
    price_at_discovery REAL,
    price_data_time TEXT,
    evidence_json TEXT DEFAULT '{}',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS candidate_history (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    actor TEXT,
    reason TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS research_reports (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    engine_version TEXT,
    computed_at TEXT,
    data_asof TEXT,
    data_version TEXT,
    factors_json TEXT,
    total_score REAL,
    evidence_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS validation_samples (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    source_id TEXT,
    kind TEXT CHECK (kind IN ('forward', 'backfill')),
    entry_date TEXT,
    benchmark_price REAL,
    ret_t5 REAL,
    ret_t10 REAL,
    ret_t20 REAL,
    max_drawdown REAL,
    maturity_status TEXT DEFAULT 'immature',
    missing_reason TEXT,
    evaluated_at TEXT,
    eval_version TEXT,
    created_at TEXT,
    discovery_event_id INTEGER REFERENCES discovery_events(id),
    bars_asof TEXT,
    benchmark_date TEXT,
    benchmark_price_source TEXT,
    data_version TEXT,
    benchmark_revision_json TEXT,
    UNIQUE(candidate_id, source_id, entry_date)
);

CREATE TABLE IF NOT EXISTS rule_registry (
    rule_id TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'approved', 'disabled', 'test')),
    approved_by TEXT,
    approved_at TEXT,
    approval_basis TEXT,
    params_json TEXT DEFAULT '{}',
    created_at TEXT,
    impl_key TEXT,
    impl_version TEXT,
    PRIMARY KEY (rule_id, version)
);

CREATE TABLE IF NOT EXISTS registry_history (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_version TEXT,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    actor TEXT,
    reason TEXT,
    basis TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS sample_exclusions (
    id INTEGER PRIMARY KEY,
    sample_id INTEGER NOT NULL REFERENCES validation_samples(id),
    excluded_from TEXT NOT NULL,
    reason TEXT,
    decided_by TEXT,
    created_at TEXT,
    UNIQUE(sample_id, excluded_from)
);

CREATE TABLE IF NOT EXISTS validation_decisions (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    rule_version TEXT,
    sample_scope TEXT,
    metrics_json TEXT DEFAULT '{}',
    decision TEXT,
    basis TEXT,
    created_at TEXT,
    rule_id TEXT
);

CREATE TABLE IF NOT EXISTS fetched_bars (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    source TEXT,
    fetched_at TEXT,
    PRIMARY KEY (symbol, date, source)
);

CREATE TABLE IF NOT EXISTS fetched_coverage (
    symbol TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT,
    source TEXT,
    fetched_at TEXT,
    PRIMARY KEY (symbol, start, end, source)
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    decision TEXT CHECK (decision IN ('approved', 'rejected')),
    approver TEXT,
    decided_at TEXT,
    basis TEXT,
    versions_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    approval_id INTEGER REFERENCES approvals(id),
    payload_json TEXT,
    status TEXT DEFAULT 'simulated',
    created_at TEXT
);
"""


def _resolve(db_path: str | None) -> str:
    return str(db_path) if db_path is not None else DEFAULT_DB_PATH


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """打开连接：row_factory=sqlite3.Row、外键开启。"""
    path = _resolve(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# 第一轮审计修复（fix1）新增列：旧库靠 ALTER 幂等补齐，新库由
# SCHEMA_SQL 直接建全。ALTER 只加可空列，不改动既有行数据。
_FIX1_SAMPLE_COLUMNS = {
    "discovery_event_id": "INTEGER",
    "bars_asof": "TEXT",
    "benchmark_date": "TEXT",
    "benchmark_price_source": "TEXT",
    "data_version": "TEXT",
}

_FIX1_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS sample_exclusions (
    id INTEGER PRIMARY KEY,
    sample_id INTEGER NOT NULL REFERENCES validation_samples(id),
    excluded_from TEXT NOT NULL,
    reason TEXT,
    decided_by TEXT,
    created_at TEXT,
    UNIQUE(sample_id, excluded_from)
);

CREATE TABLE IF NOT EXISTS validation_decisions (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    rule_version TEXT,
    sample_scope TEXT,
    metrics_json TEXT DEFAULT '{}',
    decision TEXT,
    basis TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS fetched_bars (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    source TEXT,
    fetched_at TEXT,
    PRIMARY KEY (symbol, date, source)
);

CREATE TABLE IF NOT EXISTS fetched_coverage (
    symbol TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT,
    source TEXT,
    fetched_at TEXT,
    PRIMARY KEY (symbol, start, end, source)
);
"""


def ensure_fix1_schema(conn) -> None:
    """幂等补齐 fix1 结构：validation_samples 新列 + 三张新表。

    对既有行只新增 NULL 列，绝不 UPDATE/DELETE 既有数据。
    """
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(validation_samples)")}
    if cols:
        for name, typ in _FIX1_SAMPLE_COLUMNS.items():
            if name not in cols:
                conn.execute(
                    "ALTER TABLE validation_samples ADD COLUMN %s %s"
                    % (name, typ))
    conn.executescript(_FIX1_TABLES_SQL)
    conn.commit()


# 独立验收前补正（fix2）新增结构：validation_samples 的
# benchmark 修订记录列 + rule_registry 规则注册表。同样只加
# 可空列/新表，绝不 UPDATE/DELETE 既有数据。
_FIX2_SAMPLE_COLUMNS = {
    "benchmark_revision_json": "TEXT",
}

_FIX2_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS rule_registry (
    rule_id TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'approved', 'disabled', 'test')),
    approved_by TEXT,
    approved_at TEXT,
    params_json TEXT DEFAULT '{}',
    created_at TEXT,
    PRIMARY KEY (rule_id, version)
);
"""


def ensure_fix2_schema(conn) -> None:
    """幂等补齐 fix2 结构：benchmark_revision_json 列 +
    rule_registry 表。对既有行只新增 NULL 列。"""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(validation_samples)")}
    if cols:
        for name, typ in _FIX2_SAMPLE_COLUMNS.items():
            if name not in cols:
                conn.execute(
                    "ALTER TABLE validation_samples ADD COLUMN %s %s"
                    % (name, typ))
    conn.executescript(_FIX2_TABLES_SQL)
    conn.commit()


# 效率升级 fix3 新增结构（P0-A/P0-B）：
# - validation_samples 数据库层不可变触发器：benchmark 三字段
#   （benchmark_price/benchmark_date/benchmark_price_source）在
#   行已存在后被 UPDATE 改动 -> RAISE(ABORT)；样本行 DELETE
#   同样禁止（历史样本不可删）。触发器只拦「值发生变化」的
#   UPDATE，evaluate_sample 的评估回写不触碰这三列、不受影响。
# - rule_registry 增加 impl_key/impl_version（实现绑定，P0-B）。
# - validation_decisions 增加 rule_id 列（与 rule_version 并存）。
# 迁移幂等：列用 PRAGMA 检查后 ALTER，触发器 IF NOT EXISTS。
_FIX3_REGISTRY_COLUMNS = {
    "impl_key": "TEXT",
    "impl_version": "TEXT",
}

_FIX3_DECISION_COLUMNS = {
    "rule_id": "TEXT",
}

_FIX3_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_validation_samples_benchmark_immutable
BEFORE UPDATE OF benchmark_price, benchmark_date,
    benchmark_price_source ON validation_samples
BEGIN
    SELECT RAISE(ABORT,
        'benchmark immutable: benchmark fields cannot be changed after creation')
    WHERE NEW.benchmark_price IS NOT OLD.benchmark_price
       OR NEW.benchmark_date IS NOT OLD.benchmark_date
       OR NEW.benchmark_price_source IS NOT OLD.benchmark_price_source;
END;

CREATE TRIGGER IF NOT EXISTS trg_validation_samples_no_delete
BEFORE DELETE ON validation_samples
BEGIN
    SELECT RAISE(ABORT,
        'validation_samples are historical records: DELETE forbidden');
END;
"""


def ensure_fix3_schema(conn) -> None:
    """幂等补齐 fix3 结构：不可变触发器 + 注册表实现绑定列 +
    判定记录 rule_id 列。对既有行只新增 NULL 列/触发器，
    绝不 UPDATE/DELETE 既有数据。"""
    for table, columns in (("rule_registry", _FIX3_REGISTRY_COLUMNS),
                           ("validation_decisions",
                            _FIX3_DECISION_COLUMNS)):
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(%s)" % table)}
        if cols:
            for name, typ in columns.items():
                if name not in cols:
                    conn.execute(
                        "ALTER TABLE %s ADD COLUMN %s %s"
                        % (table, name, typ))
    conn.executescript(_FIX3_TRIGGERS_SQL)
    conn.commit()


# judge P0 集成（2026-11-23，judge-p0-integration）新增结构，
# 服务 P0-3 批准信息强制：
# - rule_registry 增加 approval_basis 列（批准依据落字）；
# - registry_history append-only 留痕表：规则登记与每次状态
#   变更同事务写入（entity/field/old/new/actor/reason/basis/
#   时间）；表自身带触发器禁止 UPDATE/DELETE（RAISE 消息为
#   单行字符串——SQLite RAISE 内不拼接相邻字面量）。
# 迁移幂等：列用 PRAGMA 检查后 ALTER，表/触发器 IF NOT EXISTS。
_P0INT_REGISTRY_COLUMNS = {
    "approval_basis": "TEXT",
}

_P0INT_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS registry_history (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_version TEXT,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    actor TEXT,
    reason TEXT,
    basis TEXT,
    created_at TEXT
);
"""

_P0INT_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_p0int_registry_history_no_update
BEFORE UPDATE ON registry_history
BEGIN
    SELECT RAISE(ABORT, 'registry_history is append-only: UPDATE forbidden');
END;

CREATE TRIGGER IF NOT EXISTS trg_p0int_registry_history_no_delete
BEFORE DELETE ON registry_history
BEGIN
    SELECT RAISE(ABORT, 'registry_history is append-only: DELETE forbidden');
END;
"""


def ensure_p0int_schema(conn) -> None:
    """幂等补齐 P0 集成结构：approval_basis 列 + registry_history
    表与 append-only 触发器。对既有行只新增 NULL 列，绝不
    UPDATE/DELETE 既有数据。"""
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(rule_registry)")}
    if cols:
        for name, typ in _P0INT_REGISTRY_COLUMNS.items():
            if name not in cols:
                conn.execute(
                    "ALTER TABLE rule_registry ADD COLUMN %s %s"
                    % (name, typ))
    conn.executescript(_P0INT_TABLES_SQL)
    conn.executescript(_P0INT_TRIGGERS_SQL)
    conn.commit()


_REDTEAM_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_rt_discovery_events_created_at_no_update
BEFORE UPDATE OF created_at ON discovery_events
WHEN NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'discovery_events.created_at is server-stamped at ingest and immutable (P0-5)');
END;
"""


def ensure_redteam_schema(conn) -> None:
    """幂等安装红队集中修复（2026-11-23）的 DB 层保护触发器。

    - P0-5：discovery_events.created_at 落字后不可 UPDATE
      篡改（回填/前移/后移全拒）；生成仍由 pool.ingest 以
      服务器时间（或测试注入的可信时钟）完成。
    触发器只新增、不动既有数据。
    """
    conn.executescript(_REDTEAM_TRIGGERS_SQL)
    conn.commit()


def init_db(db_path: str | None = None) -> str:
    """建表（幂等）并返回实际库路径。"""
    path = _resolve(db_path)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        ensure_fix1_schema(conn)
        ensure_fix2_schema(conn)
        ensure_fix3_schema(conn)
        ensure_p0int_schema(conn)
        ensure_redteam_schema(conn)
    finally:
        conn.close()
    return path
