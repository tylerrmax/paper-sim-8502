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
    created_at TEXT,
    ingest_mark_id INTEGER
);

CREATE TABLE IF NOT EXISTS ingest_channel_marks (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    mark TEXT NOT NULL UNIQUE,
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
    audit_history_id INTEGER,
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
    audit_history_id INTEGER,
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

CREATE TRIGGER IF NOT EXISTS trg_rt_rule_registry_approved_insert
BEFORE INSERT ON rule_registry
WHEN NEW.status = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'rule_registry approved requires approved_by (P0-3 db guard)')
    WHERE NEW.approved_by IS NULL OR trim(NEW.approved_by) = '';
    SELECT RAISE(ABORT, 'rule_registry approved requires approval_basis (P0-3 db guard)')
    WHERE NEW.approval_basis IS NULL OR trim(NEW.approval_basis) = '';
    SELECT RAISE(ABORT, 'rule_registry approved_at must be server-current within 120s (P0-3 db guard)')
    WHERE NEW.approved_at IS NULL
       OR abs(julianday(NEW.approved_at) - julianday('now')) * 86400.0 > 120.0;
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_rule_registry_approved_update
BEFORE UPDATE OF status ON rule_registry
WHEN NEW.status = 'approved' AND OLD.status <> 'approved'
BEGIN
    SELECT RAISE(ABORT, 'rule_registry approved requires approved_by (P0-3 db guard)')
    WHERE NEW.approved_by IS NULL OR trim(NEW.approved_by) = '';
    SELECT RAISE(ABORT, 'rule_registry approved requires approval_basis (P0-3 db guard)')
    WHERE NEW.approval_basis IS NULL OR trim(NEW.approval_basis) = '';
    SELECT RAISE(ABORT, 'rule_registry approved_at must be server-current within 120s (P0-3 db guard)')
    WHERE NEW.approved_at IS NULL
       OR abs(julianday(NEW.approved_at) - julianday('now')) * 86400.0 > 120.0;
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_registry_history_approved_insert
BEFORE INSERT ON registry_history
WHEN NEW.field = 'status' AND NEW.new_value = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'registry_history approved entry requires matching approved rule_registry row (P0-3 db guard)')
    WHERE NOT EXISTS (
        SELECT 1 FROM rule_registry
        WHERE rule_id = NEW.entity_id AND version = NEW.entity_version
          AND status = 'approved'
          AND approved_by IS NOT NULL AND trim(approved_by) <> ''
          AND approval_basis IS NOT NULL AND trim(approval_basis) <> '');
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_candidates_promotion_approved_insert
BEFORE INSERT ON candidates
WHEN NEW.promotion_status = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'candidates promotion to approved requires a valid approvals record (RT-1 db guard)')
    WHERE NOT EXISTS (
        SELECT 1 FROM approvals
        WHERE candidate_id = NEW.id AND decision = 'approved'
          AND approver IS NOT NULL AND trim(approver) <> ''
          AND basis IS NOT NULL AND trim(basis) <> '');
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_candidates_promotion_approved_update
BEFORE UPDATE OF promotion_status ON candidates
WHEN NEW.promotion_status = 'approved' AND OLD.promotion_status <> 'approved'
BEGIN
    SELECT RAISE(ABORT, 'candidates promotion to approved requires a valid approvals record (RT-1 db guard)')
    WHERE NOT EXISTS (
        SELECT 1 FROM approvals
        WHERE candidate_id = NEW.id AND decision = 'approved'
          AND approver IS NOT NULL AND trim(approver) <> ''
          AND basis IS NOT NULL AND trim(basis) <> '');
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_sample_exclusions_audit_insert
BEFORE INSERT ON sample_exclusions
BEGIN
    SELECT RAISE(ABORT, 'sample_exclusions requires non-empty reason (RT-2 db guard)')
    WHERE NEW.reason IS NULL OR trim(NEW.reason) = '';
    SELECT RAISE(ABORT, 'sample_exclusions requires non-empty decided_by (RT-2 db guard)')
    WHERE NEW.decided_by IS NULL OR trim(NEW.decided_by) = '';
    SELECT RAISE(ABORT, 'sample_exclusions requires a corresponding candidate_history audit row (RT-2 db guard)')
    WHERE NOT EXISTS (
        SELECT 1 FROM candidate_history h
        JOIN validation_samples s ON s.id = NEW.sample_id
        WHERE h.candidate_id = s.candidate_id
          AND h.field = 'sample_exclusion'
          AND h.new_value = NEW.excluded_from
          AND h.actor = NEW.decided_by
          AND h.reason = NEW.reason);
END;
"""


# V3 独立审计补正（2026-11-23）新增结构：
# - P0-3（P0-5 INSERT 时间锚，未来时间部分）：discovery_events
#   直接 INSERT 的 created_at 不得晚于 DB 服务器时间
#   120 秒（与 registry approved_at 窗口同口径）。NULL 与
#   历史回填窗口与既有可信时钟夹具存在真实冲突（夹具以
#   固定字符串模拟历史入库时刻、且有故意写 NULL 以验证
#   缺锚拒绝的用例），本批不实施，冲突全量与可选方案见
#   evidence 设计文档 DESIGN-V3-CORRECTION.md。
# - P0-1（RT-1 审批记录持续有效性）：approvals 行是某候选
#   approved 终态的有效依据时（decision='approved'、
#   approver/basis 非空、候选当前 promotion_status=
#   'approved'），禁止 DELETE 该行、禁止 UPDATE 其关键
#   字段（candidate_id/decision/approver/basis/decided_at）
#   使其失效或转移——封「事后抽掉依据」路径。
_V3C_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_rt_approvals_no_delete_live
BEFORE DELETE ON approvals
WHEN OLD.decision = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'approvals record is the live basis of an approved candidate: DELETE forbidden (RT-1 lifecycle guard)')
    WHERE OLD.approver IS NOT NULL AND trim(OLD.approver) <> ''
      AND OLD.basis IS NOT NULL AND trim(OLD.basis) <> ''
      AND EXISTS (
        SELECT 1 FROM candidates
        WHERE id = OLD.candidate_id
          AND promotion_status = 'approved');
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_discovery_events_created_at_insert_future
BEFORE INSERT ON discovery_events
WHEN NEW.created_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'discovery_events.created_at must not be later than server time + 120s (P0-5 insert guard)')
    WHERE julianday(NEW.created_at) - julianday('now')
        > 120.0 / 86400.0;
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_approvals_no_update_keyfields_live
BEFORE UPDATE OF candidate_id, decision, approver, basis,
    decided_at ON approvals
WHEN OLD.decision = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'approvals key fields of a live approval record are immutable (RT-1 lifecycle guard)')
    WHERE OLD.approver IS NOT NULL AND trim(OLD.approver) <> ''
      AND OLD.basis IS NOT NULL AND trim(OLD.basis) <> ''
      AND EXISTS (
        SELECT 1 FROM candidates
        WHERE id = OLD.candidate_id
          AND promotion_status = 'approved')
      AND (NEW.candidate_id IS NOT OLD.candidate_id
        OR NEW.decision IS NOT OLD.decision
        OR NEW.approver IS NOT OLD.approver
        OR NEW.basis IS NOT OLD.basis
        OR NEW.decided_at IS NOT OLD.decided_at);
END;

"""


# V3 独立审计补正 P0-2（RT-2 审计记录唯一绑定）：
# - sample_exclusions.audit_history_id 显式引用其审计记录
#   （candidate_history 行 id），UNIQUE 索引保证一对一；
# - trg_rt_sample_exclusions_audit_insert 修订版（ensure 内
#   先 DROP 旧同名触发器再以本定义重建）：在原字段非空与
#   逐字对应校验（保留为冗余校验）之上，增加绑定必备、
#   引用逐字对应、一对一三项检查；
# - 被任一排除引用的 candidate_history 行禁止 UPDATE /
#   DELETE（trg_rt_candidate_history_no_update_bound /
#   no_delete_bound）。
# 口径（P0-2 第 5 项修订）：本机制证明的是审计证据的
# 存在性、唯一性与不可变性——「不可变、一对一关联的审计
# 证据」；触发器无法证明审计行与排除行同出一事务或同一
# 操作者（同源性），不得再以「同事务」表述本闸的证明力。
_V3C2_TRIGGERS_SQL = """
CREATE TRIGGER trg_rt_sample_exclusions_audit_insert
BEFORE INSERT ON sample_exclusions
BEGIN
    SELECT RAISE(ABORT, 'sample_exclusions requires non-empty reason (RT-2 db guard)')
    WHERE NEW.reason IS NULL OR trim(NEW.reason) = '';
    SELECT RAISE(ABORT, 'sample_exclusions requires non-empty decided_by (RT-2 db guard)')
    WHERE NEW.decided_by IS NULL OR trim(NEW.decided_by) = '';
    SELECT RAISE(ABORT, 'sample_exclusions requires audit_history_id binding to an audit record (RT-2 unique binding)')
    WHERE NEW.audit_history_id IS NULL;
    SELECT RAISE(ABORT, 'sample_exclusions audit_history_id must reference the corresponding candidate_history audit row (RT-2 unique binding)')
    WHERE NOT EXISTS (
        SELECT 1 FROM candidate_history h
        JOIN validation_samples s ON s.id = NEW.sample_id
        WHERE h.id = NEW.audit_history_id
          AND h.candidate_id = s.candidate_id
          AND h.field = 'sample_exclusion'
          AND h.new_value = NEW.excluded_from
          AND h.actor = NEW.decided_by
          AND h.reason = NEW.reason);
    SELECT RAISE(ABORT, 'candidate_history audit row is already bound to another exclusion (RT-2 one-to-one binding)')
    WHERE EXISTS (
        SELECT 1 FROM sample_exclusions e
        WHERE e.audit_history_id = NEW.audit_history_id);
    SELECT RAISE(ABORT, 'sample_exclusions requires a corresponding candidate_history audit row (RT-2 db guard)')
    WHERE NOT EXISTS (
        SELECT 1 FROM candidate_history h
        JOIN validation_samples s ON s.id = NEW.sample_id
        WHERE h.candidate_id = s.candidate_id
          AND h.field = 'sample_exclusion'
          AND h.new_value = NEW.excluded_from
          AND h.actor = NEW.decided_by
          AND h.reason = NEW.reason);
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_candidate_history_no_update_bound
BEFORE UPDATE ON candidate_history
BEGIN
    SELECT RAISE(ABORT, 'candidate_history audit row bound to a sample exclusion is immutable (RT-2 binding guard)')
    WHERE EXISTS (
        SELECT 1 FROM sample_exclusions e
        WHERE e.audit_history_id = OLD.id);
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_candidate_history_no_delete_bound
BEFORE DELETE ON candidate_history
BEGIN
    SELECT RAISE(ABORT, 'candidate_history audit row bound to a sample exclusion cannot be deleted (RT-2 binding guard)')
    WHERE EXISTS (
        SELECT 1 FROM sample_exclusions e
        WHERE e.audit_history_id = OLD.id);
END;
"""


# P0-3 完整落地（2026-11-23，方案 A 受控通道标记，用户定案）：
# - ingest_channel_marks 为受控通道标记表：pool.ingest 在与
#   事件行同一事务内先落标记行（mark=uuid4、created_at 为
#   真实服务器时间），事件行以 ingest_mark_id 绑定之；
# - trg_rt_discovery_events_insert_channel：无有效标记的
#   INSERT 必须 created_at 为服务器当前时间（±120 秒口径
#   的过去侧；未来侧由既有 insert_future 触发器管辖）——
#   NULL、不可解析、历史回填一律拒绝；声称标记但标记不
#   存在/跨候选/已被消费，一律拒绝；带有效标记的 INSERT
#   不受时间值限制（时钟夹具的历史时刻与 NULL 缺锚前提
#   经通道合法落字，与墙钟无关）；
# - 标记行 append-only：禁止 UPDATE；被事件行引用后禁止
#   DELETE（未被引用者可删，不构成任何事件的通道证据）。
# 口径（照 P0-2 修订）：本机制证明标记的存在性、唯一性与
# 不可变性——事件行「经受控写序落字」的形态；不证明调用
# 方身份（直写自造标记整套伪造属固有界限，见设计文档
# DESIGN-P0-3-CHANNEL-A.md 与对抗实测）。
_P03A_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS ingest_channel_marks (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    mark TEXT NOT NULL UNIQUE,
    created_at TEXT
);
"""

_P03A_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_rt_discovery_events_insert_channel
BEFORE INSERT ON discovery_events
BEGIN
    SELECT RAISE(ABORT, 'discovery_events ingest_mark_id must reference a valid unconsumed ingest channel mark of the same candidate (P0-5 channel guard)')
    WHERE NEW.ingest_mark_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM ingest_channel_marks m
        WHERE m.id = NEW.ingest_mark_id
          AND m.candidate_id = NEW.candidate_id
          AND NOT EXISTS (
            SELECT 1 FROM discovery_events e
            WHERE e.ingest_mark_id = m.id));
    SELECT RAISE(ABORT, 'discovery_events created_at must be server-current (within 120s) or carry a valid ingest channel mark (P0-5 channel guard)')
    WHERE NOT EXISTS (
        SELECT 1 FROM ingest_channel_marks m
        WHERE m.id = NEW.ingest_mark_id
          AND m.candidate_id = NEW.candidate_id
          AND NOT EXISTS (
            SELECT 1 FROM discovery_events e
            WHERE e.ingest_mark_id = m.id))
      AND (NEW.created_at IS NULL
        OR julianday(NEW.created_at) IS NULL
        OR (julianday('now') - julianday(NEW.created_at))
            * 86400.0 > 120.0);
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_ingest_channel_marks_no_update
BEFORE UPDATE ON ingest_channel_marks
BEGIN
    SELECT RAISE(ABORT, 'ingest_channel_marks are append-only channel evidence: UPDATE forbidden (P0-5 channel guard)');
END;

CREATE TRIGGER IF NOT EXISTS trg_rt_ingest_channel_marks_no_delete_bound
BEFORE DELETE ON ingest_channel_marks
BEGIN
    SELECT RAISE(ABORT, 'ingest_channel_marks bound to a discovery event cannot be deleted (P0-5 channel guard)')
    WHERE EXISTS (
        SELECT 1 FROM discovery_events e
        WHERE e.ingest_mark_id = OLD.id);
END;
"""


def ensure_redteam_schema(conn) -> None:
    """幂等安装红队集中修复（2026-11-23）的 DB 层保护触发器。

    - P0-5：discovery_events.created_at 落字后不可 UPDATE
      篡改（回填/前移/后移全拒）；生成仍由 pool.ingest 以
      服务器时间（或测试注入的可信时钟）完成。
    - P0-3：rule_registry 进入 approved（INSERT 或 status
      UPDATE 流转）必须 approved_by/approval_basis 非空且
      approved_at 与 DB 服务器时间差 ≤120 秒；registry_history
      的 approved 流转记录必须有状态一致的 registry 行对应
      （应用路径恒先写 registry 后写 history、同事务，天然
      通过）。设计与诚实残余见 evidence 设计文档
      DESIGN-P0-3-P0-5-RT-5.md。
    - RT-1 底层闸（2026-11-23 补强批）：candidates 进入
      promotion_status='approved'（INSERT 或 status UPDATE
      流转）必须有 decision='approved' 且 approver/basis
      均非空的 approvals 行对应（decide 恒先写 approvals
      后流转、同事务，天然通过）。
    - RT-2 底层闸（2026-11-23 补强批，V3 补正 P0-2 修订）：
      sample_exclusions 每条 INSERT 必须 reason/decided_by
      非空，并以 audit_history_id 显式绑定一条逐字对应的
      candidate_history 审计行（一对一、绑定后审计行不可
      改删）——不可变、一对一关联的审计证据；触发器不
      证明审计行与排除行同出一事务（见 _V3C2 注释）。
      exclude_sample 恒先写审计行后写排除行并落绑定 id，
      天然通过。设计见 evidence 设计补记
      DESIGN-BOTTOM-LAYER.md 与 DESIGN-V3-CORRECTION.md。
    - V3 审计补正（2026-11-23）：approvals 生命周期保护
      （P0-1）与 discovery_events INSERT 未来时间拒绝
      （P0-3）触发器，见 _V3C_TRIGGERS_SQL 注释。
    - P0-3 完整落地（2026-11-23，方案 A 受控通道标记）：
      ingest_channel_marks 表 + discovery_events.
      ingest_mark_id 绑定列与一对一唯一索引 +
      insert_channel 触发器——无有效通道标记的 INSERT，
      created_at 为 NULL/不可解析/早于服务器时间 120 秒
      以上一律拒绝；带有效标记者不受时间值限制。ingest
      恒先落标记行再落事件行并绑定、同事务，天然通过。
      本闸证明标记的存在性/唯一性/不可变性（经受控写序
      落字的形态），不证明调用方身份，见 _P03A 注释。

    触发器只新增、不动既有数据。
    """
    conn.executescript(_REDTEAM_TRIGGERS_SQL)
    # P0-2 schema 迁移：audit_history_id 列 + 一对一唯一索引
    # （存量行为 NULL，SQLite UNIQUE 不约束 NULL）。
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(sample_exclusions)")}
    if cols and "audit_history_id" not in cols:
        conn.execute(
            "ALTER TABLE sample_exclusions"
            " ADD COLUMN audit_history_id INTEGER")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS"
        " idx_sample_exclusions_audit_history_id"
        " ON sample_exclusions(audit_history_id)")
    # P0-2 修订版 audit_insert 触发器替换旧同名定义。
    conn.execute(
        "DROP TRIGGER IF EXISTS trg_rt_sample_exclusions_audit_insert")
    conn.executescript(_V3C2_TRIGGERS_SQL)
    conn.executescript(_V3C_TRIGGERS_SQL)
    # P0-3 方案 A 迁移：通道标记表 + 事件行绑定列 + 一对一
    # 唯一索引（存量行 ingest_mark_id 为 NULL，SQLite
    # UNIQUE 不约束 NULL）+ 通道触发器。
    conn.executescript(_P03A_TABLES_SQL)
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(discovery_events)")}
    if cols and "ingest_mark_id" not in cols:
        conn.execute(
            "ALTER TABLE discovery_events"
            " ADD COLUMN ingest_mark_id INTEGER")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS"
        " idx_discovery_events_ingest_mark_id"
        " ON discovery_events(ingest_mark_id)")
    conn.executescript(_P03A_TRIGGERS_SQL)
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
