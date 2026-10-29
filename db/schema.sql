-- 模拟盘 SQLite 表结构（v2，与 engine/pipeline.py 对齐）
-- market: a = A股 / hk = 港股 / crypto = 加密

CREATE TABLE IF NOT EXISTS market_data (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,          -- YYYY-MM-DD
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume REAL NOT NULL,
    market TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS signals (
    date    TEXT NOT NULL,         -- 信号日期
    account TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    side    TEXT NOT NULL,         -- BUY / SELL
    price   REAL NOT NULL,         -- 信号参考价（当日收盘）
    action_type TEXT DEFAULT '开仓',  -- 开仓 / 止盈 / 止损 / 信号平仓（pipeline 写入）
    status  TEXT DEFAULT '待执行',    -- 待执行 / 已成交 / 已过期
    stop_price REAL,               -- 止损价（ATR 或策略止损）
    strategy TEXT,                 -- 生成该信号的策略名（版本隔离用）
    adx_value REAL,                -- 信号日 ADX 值
    range_protected INTEGER DEFAULT 0,  -- 震荡保护标记
    volume_confirmed INTEGER DEFAULT 0, -- 量能确认标记
    atr_multiple REAL,             -- ATR 倍数
    PRIMARY KEY (date, account, symbol, side)
);

CREATE TABLE IF NOT EXISTS fills (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    date    TEXT NOT NULL,
    account TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    side    TEXT NOT NULL,         -- BUY / SELL
    qty     REAL NOT NULL,
    price   REAL NOT NULL,         -- 实际成交价（含滑点）
    fee     REAL NOT NULL,
    pnl     REAL DEFAULT 0,        -- SELL 时的实现盈亏
    signal_date TEXT,              -- 信号日期（T+1 撮合：date=执行日）
    strategy_version TEXT,         -- 策略版本（毕业样本按版本隔离）
    rotation_voucher_id INTEGER    -- 用券买入时 = 来源卖出成交 fills.id（额度券 RV-<id>）；旧库由 pipeline 幂等补列
);

CREATE TABLE IF NOT EXISTS positions (
    account      TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    qty          REAL NOT NULL,
    avg_cost     REAL NOT NULL,
    last_price   REAL NOT NULL,
    market_value REAL NOT NULL,
    PRIMARY KEY (account, symbol)
);

CREATE TABLE IF NOT EXISTS daily_report (
    date           TEXT NOT NULL,
    account        TEXT NOT NULL,
    symbol         TEXT NOT NULL,   -- 'TOTAL' 为账户级汇总
    nav            REAL NOT NULL,   -- 净值（本币）
    cash           REAL NOT NULL,
    position_value REAL NOT NULL,
    pnl_day        REAL NOT NULL,
    ret_total      REAL NOT NULL,   -- 相对初始资金累计收益率
    PRIMARY KEY (date, account, symbol)
);

CREATE TABLE IF NOT EXISTS fx_rate (
    date TEXT PRIMARY KEY,         -- YYYY-MM-DD
    hkdcny REAL,                   -- 新浪 HKDCNY（港元兑人民币）
    usdcny_est REAL                -- = hkdcny × 7.8（联系汇率估算，声明值）
);

-- 网页版控制（v3）：开始/暂停 + 自动/手动
CREATE TABLE IF NOT EXISTS control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'running',  -- running / paused
    mode TEXT NOT NULL DEFAULT 'auto',       -- auto / manual
    updated TEXT
);

-- 指令 exactly-once 台账：已处理的网页指令 id，防重放导致重复成交
CREATE TABLE IF NOT EXISTS processed_commands (
    id TEXT PRIMARY KEY,
    applied_at TEXT
);

-- 手动模式的信号审批
CREATE TABLE IF NOT EXISTS approvals (
    date TEXT NOT NULL,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,            -- BUY / SELL
    decision TEXT NOT NULL,        -- approved / skipped
    updated TEXT,
    PRIMARY KEY (date, account, symbol, side)
);

-- 实盘成交登记（用户券商手工下单的实际成交，由截图录入）：
-- 与 fills（模拟成交）按 (signal_date, account, symbol, side) 关联，
-- 用于统计执行滑点 = (real_price - sim_price) / sim_price。
CREATE TABLE IF NOT EXISTS real_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date TEXT NOT NULL,     -- 对应的模拟信号日期
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,            -- BUY / SELL
    real_date TEXT NOT NULL,       -- 券商实际成交日期
    real_price REAL NOT NULL,      -- 券商实际成交价
    real_qty REAL NOT NULL,        -- 券商实际数量
    note TEXT,                     -- 备注
    created TEXT                   -- 录入时间
);

-- 券商回执（人工条件单在券商成交后录入，回执独立对账）：
-- 与 fills（模拟成交）独立，不直接关联，用于人工核对执行一致性。
CREATE TABLE IF NOT EXISTS broker_receipts (
    date TEXT NOT NULL,            -- 券商成交日期 YYYY-MM-DD
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,            -- BUY / SELL
    price REAL NOT NULL,           -- 券商实际成交价
    qty REAL NOT NULL,             -- 券商实际数量
    created_at TEXT,               -- 录入时间
    PRIMARY KEY (date, account, symbol, side)
);

-- 毕业样本剔除（策略错配等原因不计入毕业统计，但历史保留）：
-- 如 2026-10-15 sim_hk HK9660 SELL（错策略成交）已标记剔除。
CREATE TABLE IF NOT EXISTS graduation_exclusions (
    date TEXT NOT NULL,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    reason TEXT,                   -- 剔除原因
    excluded_at TEXT,              -- 标记时间
    PRIMARY KEY (date, account, symbol, side)
);

-- 暂停期信号黑名单：暂停期间生成的信号记入此表，恢复自动模式后永不补成交。
-- 跨轮持久，不随 signals 全量重算删除。
CREATE TABLE IF NOT EXISTS pause_skipped_signals (
    date TEXT NOT NULL,
    account TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,            -- BUY / SELL
    PRIMARY KEY (date, account, symbol, side)
);

-- 换仓额度券台账（四项补齐①，2026-10-27）：禁开态换仓豁免的审计表。
-- 每轮由 pipeline 按成交事实全量重建（重算产物，与 positions 同性质）：
-- 主动信号卖出生成一张券（voucher_id = 'RV-' || 来源卖出 fills.id），
-- 面额 = 扣费后净回收额；一券至多资助一笔买入（used_* 回填买入成交），
-- 5 个交易日未用自动 expired；用券事实同时在 fills.rotation_voucher_id 定格。
CREATE TABLE IF NOT EXISTS rotation_vouchers (
    voucher_id TEXT PRIMARY KEY,   -- RV-<source_fill_id>
    source_fill_id INTEGER,        -- 来源卖出成交 fills.id
    account TEXT,
    symbol TEXT,                   -- 来源卖出的标的
    signal_date TEXT,              -- 来源卖出的信号日
    exec_date TEXT,                -- 来源卖出的成交（执行）日，有效期起算
    qty REAL, price REAL, fee REAL,-- 来源卖出成交的数量/价/费（账本口径）
    amount REAL,                   -- 面额 = qty*price - fee（净回收）
    status TEXT,                   -- active / used / expired
    used_fill_id INTEGER,          -- 被资助买入的 fills.id（used 时）
    used_date TEXT,
    used_symbol TEXT,
    created_at TEXT                -- 本轮重建时间
);

-- 安全补充（2026-10-30）：交易意图幂等与状态机（engine/safety.py）
CREATE TABLE IF NOT EXISTS trade_intents (
    idempotency_key TEXT PRIMARY KEY,  -- sha256(账户|市场|标的|方向|策略版本|信号事件标识)
    account TEXT, market TEXT, symbol TEXT, side TEXT,
    qty REAL, price REAL,
    strategy_version TEXT, signal_event_id TEXT,
    state TEXT NOT NULL,               -- pending_review/risk_check/released/submitting/confirmed/rejected/unknown
    result_ref TEXT, note TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS intent_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT, from_state TEXT, to_state TEXT,
    note TEXT, at TEXT
);

-- 安全补充（2026-10-30）：风控拦截结构化事件（追加式哈希链，engine/safety.py）
CREATE TABLE IF NOT EXISTS risk_block_events (
    event_id TEXT PRIMARY KEY,
    ts TEXT, trade_date TEXT, account TEXT, market TEXT,
    symbol TEXT, side TEXT, source TEXT,
    institution_evidence TEXT, strategy_version TEXT,
    screen_passed INTEGER, screen_score REAL,
    rule_name TEXT, rule_version TEXT, reason TEXT,
    b31_state TEXT, index_close REAL, index_ma20 REAL,
    data_ts TEXT, request_id TEXT, idempotency_key TEXT,
    order_id TEXT, disposition TEXT,
    review_status TEXT DEFAULT 'pending', review_at TEXT,
    prev_hash TEXT, row_hash TEXT
);

-- 机构集体建仓影子监控（2026-10-30，口径=龙虎榜机构专用席位，engine/inst_lhb.py）
CREATE TABLE IF NOT EXISTS inst_lhb_daily (
    date TEXT, symbol TEXT, name TEXT,
    inst_buy REAL, inst_sell REAL, inst_net REAL,
    seat_buy_records INTEGER, seat_sell_records INTEGER,
    window TEXT, trade_id TEXT,
    fetched_at TEXT,
    PRIMARY KEY (date, symbol)
);
CREATE TABLE IF NOT EXISTS inst_lhb_raw (
    date TEXT, kind TEXT, seq INTEGER, symbol TEXT, seat_name TEXT,
    trade_id TEXT, explanation TEXT,
    buy REAL, sell REAL, net REAL,
    PRIMARY KEY (date, kind, seq)
);
CREATE TABLE IF NOT EXISTS inst_lhb_fetch_days (
    date TEXT PRIMARY KEY, agg_rows INTEGER, raw_rows INTEGER,
    state TEXT, fetched_at TEXT, reason TEXT
);
