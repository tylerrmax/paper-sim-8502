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
    pnl     REAL DEFAULT 0         -- SELL 时的实现盈亏
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
