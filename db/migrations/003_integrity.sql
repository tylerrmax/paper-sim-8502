-- 003 完整性硬化（幂等）：schema 版本、只增不改的信号日志与审计日志、写入约束触发器、行情隔离表。
-- 应用：python3 tools/migrate.py db/sim.db        （自动先备份）
-- 约束用触发器实现：SQLite 无法给既有表追加 CHECK，触发器对老库同样生效。

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
CREATE TABLE IF NOT EXISTS meta_kv (k TEXT PRIMARY KEY, v);

-- 信号日志：signals 被每轮重算覆盖，这里按"状态迁移"逐条留底，成交可回溯到当时的信号与策略版本
CREATE TABLE IF NOT EXISTS signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, account TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    price REAL, action_type TEXT, stop_price REAL,
    strategy TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
    logged_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (date, account, symbol, side, price, status, strategy));

CREATE TRIGGER IF NOT EXISTS trg_signals_log_ins AFTER INSERT ON signals BEGIN
    INSERT OR IGNORE INTO signal_log(date,account,symbol,side,price,action_type,stop_price,strategy,status)
    VALUES (NEW.date,NEW.account,NEW.symbol,NEW.side,NEW.price,NEW.action_type,NEW.stop_price,
            COALESCE(NEW.strategy,''),COALESCE(NEW.status,''));
END;
CREATE TRIGGER IF NOT EXISTS trg_signals_log_upd AFTER UPDATE OF status ON signals BEGIN
    INSERT OR IGNORE INTO signal_log(date,account,symbol,side,price,action_type,stop_price,strategy,status)
    VALUES (NEW.date,NEW.account,NEW.symbol,NEW.side,NEW.price,NEW.action_type,NEW.stop_price,
            COALESCE(NEW.strategy,''),COALESCE(NEW.status,''));
END;
INSERT OR IGNORE INTO signal_log(date,account,symbol,side,price,action_type,stop_price,strategy,status)
    SELECT date,account,symbol,side,price,action_type,stop_price,COALESCE(strategy,''),COALESCE(status,'') FROM signals;
-- 迁移时刻的最大成交 id：此后新成交必须能在 signal_log 找到信号（之前的算 legacy）
INSERT OR IGNORE INTO meta_kv(k,v) SELECT 'signal_log_since_fill_id', COALESCE(MAX(id),0) FROM fills;

-- 审计日志：只增不改
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT (datetime('now','localtime')),
    actor TEXT, action TEXT NOT NULL, target TEXT, detail TEXT);
CREATE TRIGGER IF NOT EXISTS trg_audit_no_update BEFORE UPDATE ON audit_log BEGIN
    SELECT RAISE(ABORT,'audit_log 只增不改'); END;
CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete BEFORE DELETE ON audit_log BEGIN
    SELECT RAISE(ABORT,'audit_log 只增不改'); END;
CREATE TRIGGER IF NOT EXISTS trg_signal_log_no_update BEFORE UPDATE ON signal_log BEGIN
    SELECT RAISE(ABORT,'signal_log 只增不改'); END;
CREATE TRIGGER IF NOT EXISTS trg_signal_log_no_delete BEFORE DELETE ON signal_log BEGIN
    SELECT RAISE(ABORT,'signal_log 只增不改'); END;

-- 审批：补操作人与理由（老代码不传这两列时 actor 默认 web-ui）
ALTER TABLE approvals ADD COLUMN actor TEXT DEFAULT 'web-ui';
ALTER TABLE approvals ADD COLUMN reason TEXT;
CREATE TRIGGER IF NOT EXISTS trg_audit_approvals AFTER INSERT ON approvals BEGIN
    INSERT INTO audit_log(actor,action,target,detail)
    VALUES (COALESCE(NEW.actor,'web-ui'),'approval',NEW.account||'/'||NEW.symbol||'/'||NEW.side||'/'||NEW.date,
            NEW.decision||COALESCE('；'||NEW.reason,''));
END;
-- 注：不为 broker_receipts / real_fills 建审计触发器——真实券商数据不得进入会公开推送的表。
CREATE TRIGGER IF NOT EXISTS trg_audit_fills AFTER INSERT ON fills BEGIN
    INSERT INTO audit_log(actor,action,target,detail)
    VALUES ('pipeline','fill',NEW.account||'/'||NEW.symbol||'/'||NEW.side||'/'||NEW.date,
            'qty='||NEW.qty||' price='||NEW.price||' fee='||NEW.fee||' signal_date='||COALESCE(NEW.signal_date,'')||
            ' ver='||COALESCE(NEW.strategy_version,''));
END;
CREATE TRIGGER IF NOT EXISTS trg_audit_control AFTER UPDATE ON control BEGIN
    INSERT INTO audit_log(actor,action,target,detail)
    VALUES ('web-ui','control','control',COALESCE(OLD.status,'')||'/'||COALESCE(OLD.mode,'')||' -> '||
            COALESCE(NEW.status,'')||'/'||COALESCE(NEW.mode,''));
END;

-- 写入约束（非法值直接拒绝，而不是悄悄入库）
CREATE TRIGGER IF NOT EXISTS trg_chk_fills BEFORE INSERT ON fills
WHEN NEW.side NOT IN ('BUY','SELL') OR NEW.qty <= 0 OR NEW.price <= 0 OR NEW.fee < 0 BEGIN
    SELECT RAISE(ABORT,'fills 非法：side/qty/price/fee'); END;
CREATE TRIGGER IF NOT EXISTS trg_chk_signals BEFORE INSERT ON signals
WHEN NEW.side NOT IN ('BUY','SELL') OR NEW.price <= 0
     OR (NEW.status IS NOT NULL AND NEW.status NOT IN ('待执行','已成交','已过期')) BEGIN
    SELECT RAISE(ABORT,'signals 非法：side/price/status'); END;
CREATE TRIGGER IF NOT EXISTS trg_chk_approvals BEFORE INSERT ON approvals
WHEN NEW.side NOT IN ('BUY','SELL') OR NEW.decision NOT IN ('approved','skipped') BEGIN
    SELECT RAISE(ABORT,'approvals 非法：side/decision'); END;
CREATE TRIGGER IF NOT EXISTS trg_chk_positions BEFORE INSERT ON positions
WHEN NEW.qty < 0 OR NEW.avg_cost < 0 BEGIN
    SELECT RAISE(ABORT,'positions 非法：qty/avg_cost'); END;
-- 注：行情不加触发器——单条坏K线会令整批入库回滚；由 engine/data_gate.py 在入库前隔离处理。

-- 行情隔离登记（入库闸门 engine/data_gate.py 使用）
CREATE TABLE IF NOT EXISTS data_quarantine (
    symbol TEXT NOT NULL, date TEXT NOT NULL, rule TEXT NOT NULL,
    detail TEXT, detected_at TEXT, PRIMARY KEY (symbol, date, rule));
