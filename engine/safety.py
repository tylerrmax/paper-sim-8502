#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安全层（2026-10-30 四项强制安全补充 ①② 的后端实现）。

① 交易意图幂等 + 状态机（trade_intents / intent_transitions）：
- 每个交易意图有稳定 idempotency_key = sha256(账户|市场|标的|方向|
  策略版本|信号事件标识)。同键重复提交只返回原意图与原状态，
  绝不新建第二条意图、绝不重复产生订单。
- 状态机：待审核 pending_review → 风控检查 risk_check → 已放行
  released → 提交中 submitting → 已确认 confirmed / 已拒绝
  rejected / 结果未知 unknown。迁移一律用原子条件更新
  （UPDATE ... WHERE key=? AND state=?），并发下只有一个迁移成功；
  状态更新与迁移记录写入同处函数自包含事务（atomic_unit，
  2026-10-31 整改 P1-02），不依赖调用方事务设置。
- submitting 超时/无回执 → unknown：禁止盲目重发；必须先
  reconcile_unknown() 对账（下游查到成交→confirmed；确认未收到→
  回到 released 才允许再次提交）。本系统下游为模拟撮合/人工回执，
  数据库去重只保证本系统内不重复，不宣称券商端端到端防重。
- SELL 意图在放行环节不走买入环境闸（check_env_gate 只拦 BUY），
  与 risk_gate 口径一致；卖出仍须上游按持仓核查规则校验。

② 风控拦截事件链（risk_block_events）：结构化、追加式、哈希链。
- 每条事件含 prev_hash/row_hash 链；篡改、删除（链断裂）、回退
  可由 verify_block_chain() 检出；链头锚点文件
  reports/risk_block_chain_head.json 防尾部删除/整体回退。
- 写入失败：先尝试把事件落到兜底文件
  reports/risk_block_log_failures.jsonl 留痕，再抛 RiskBlockLogError，
  绝不静默丢弃（调用方按故障处理，不得当作未拦截放行）。

本模块只负责意图与拦截事件的持久化，不做行情/策略判定。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import uuid
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULE_VERSION = "safety-v1.0-2026-10-30"
GENESIS = "GENESIS"

PENDING_REVIEW = "pending_review"
RISK_CHECK = "risk_check"
RELEASED = "released"
SUBMITTING = "submitting"
CONFIRMED = "confirmed"
REJECTED = "rejected"
UNKNOWN = "unknown"

TRANSITIONS = {
    PENDING_REVIEW: {RISK_CHECK, REJECTED},
    RISK_CHECK: {RELEASED, REJECTED},
    RELEASED: {SUBMITTING, REJECTED},
    SUBMITTING: {CONFIRMED, REJECTED, UNKNOWN},
    UNKNOWN: {CONFIRMED, RELEASED, REJECTED},  # 只能经对账离开
    CONFIRMED: set(),
    REJECTED: set(),
}


class IntentStateError(Exception):
    """非法状态迁移 / 并发迁移冲突 / 未对账即重发。"""


class RiskBlockLogError(Exception):
    """拦截事件写入失败（已尝试兜底留痕）。"""


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextlib.contextmanager
def atomic_unit(con):
    """函数自包含原子单元（2026-10-31 专项整改 P1-02）：保证一
    组写入要么整体提交、要么整体回滚，不依赖调用方的事务设置。
    - 连接不在事务中（含 autocommit，isolation_level=None）：
      BEGIN IMMEDIATE … COMMIT，异常 ROLLBACK；
    - 连接已在调用方事务中：SAVEPOINT … RELEASE，异常
      ROLLBACK TO SAVEPOINT——只回滚本单元，不替调用方提交或
      回滚其外层事务，避免嵌套事务/重复提交回归。
    主表状态 UPDATE 与 intent_transitions INSERT 必须同处一个
    atomic_unit 内：不得出现状态前进但迁移记录缺失，也不得
    出现迁移记录写入但状态未变。"""
    if con.in_transaction:
        sp = "sp_atomic_" + uuid.uuid4().hex[:12]
        con.execute("SAVEPOINT " + sp)
        try:
            yield
        except BaseException:
            con.execute("ROLLBACK TO " + sp)
            con.execute("RELEASE " + sp)
            raise
        else:
            con.execute("RELEASE " + sp)
    else:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            con.execute("ROLLBACK")
            raise
        else:
            con.execute("COMMIT")


def make_idempotency_key(account, market, symbol, side, strategy_version,
                         signal_event_id):
    raw = "|".join(str(x) for x in (
        account, market, symbol, side, strategy_version, signal_event_id))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_safety_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS trade_intents(
        idempotency_key TEXT PRIMARY KEY,
        account TEXT, market TEXT, symbol TEXT, side TEXT,
        qty REAL, price REAL,
        strategy_version TEXT, signal_event_id TEXT,
        state TEXT NOT NULL, result_ref TEXT, note TEXT,
        created_at TEXT, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS intent_transitions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT, from_state TEXT, to_state TEXT,
        note TEXT, at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS risk_block_events(
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
        prev_hash TEXT, row_hash TEXT)""")


# ---------------------------------------------------------------- ① 意图

def submit_intent(con, *, account, market, symbol, side, qty=None,
                  price=None, strategy_version, signal_event_id,
                  note=""):
    """提交交易意图。同 idempotency_key 重复到达 → 返回原意图
    (created=False)，不新建、不改状态。返回 (row_dict, created)。"""
    ensure_safety_tables(con)
    key = make_idempotency_key(account, market, symbol, side,
                               strategy_version, signal_event_id)
    with atomic_unit(con):
        cur = con.execute(
            "INSERT OR IGNORE INTO trade_intents"
            "(idempotency_key,account,market,symbol,side,qty,price,"
            "strategy_version,signal_event_id,state,note,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, account, market, symbol, side, qty, price,
             strategy_version, signal_event_id, PENDING_REVIEW, note,
             _now(), _now()))
        created = cur.rowcount == 1
        if created:
            con.execute(
                "INSERT INTO intent_transitions"
                "(idempotency_key,from_state,to_state,note,at)"
                " VALUES(?,?,?,?,?)",
                (key, None, PENDING_REVIEW, note or "意图创建", _now()))
    row = con.execute(
        "SELECT * FROM trade_intents WHERE idempotency_key=?",
        (key,)).fetchone()
    cols = [d[0] for d in con.execute(
        "SELECT * FROM trade_intents LIMIT 0").description]
    return dict(zip(cols, row)), created


def get_intent(con, key):
    row = con.execute(
        "SELECT * FROM trade_intents WHERE idempotency_key=?",
        (key,)).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in con.execute(
        "SELECT * FROM trade_intents LIMIT 0").description]
    return dict(zip(cols, row))


def transition(con, key, to_state, note="", result_ref=None):
    """原子条件迁移：只有当前状态合法且未被并发改走时成功。
    状态 UPDATE 与迁移 INSERT 同处一个 atomic_unit（P1-02）：
    任一失败整体回滚，autocommit 连接下同样成立；条件 UPDATE
    的 WHERE state=当前状态 + rowcount 校验保留，防并发覆盖。"""
    ensure_safety_tables(con)
    cur_row = get_intent(con, key)
    if cur_row is None:
        raise IntentStateError("意图不存在：%s" % key)
    frm = cur_row["state"]
    if to_state not in TRANSITIONS.get(frm, set()):
        raise IntentStateError(
            "非法迁移 %s → %s（key=%s）" % (frm, to_state, key))
    with atomic_unit(con):
        cur = con.execute(
            "UPDATE trade_intents SET state=?, updated_at=?,"
            " result_ref=COALESCE(?,result_ref), note=?"
            " WHERE idempotency_key=? AND state=?",
            (to_state, _now(), result_ref, note or cur_row["note"],
             key, frm))
        if cur.rowcount != 1:
            raise IntentStateError(
                "并发迁移冲突：%s 已不在 %s 状态" % (key, frm))
        con.execute(
            "INSERT INTO intent_transitions"
            "(idempotency_key,from_state,to_state,note,at)"
            " VALUES(?,?,?,?,?)",
            (key, frm, to_state, note, _now()))
    return get_intent(con, key)


def mark_unknown(con, key, note="提交超时，下游结果未知"):
    return transition(con, key, UNKNOWN, note=note)


def reconcile_unknown(con, key, *, found_downstream, result_ref=None,
                      note=""):
    """结果未知必须先对账：查到下游已接收/成交 → confirmed；
    确认下游未收到 → 回 released（之后才允许再次提交）。"""
    row = get_intent(con, key)
    if row is None or row["state"] != UNKNOWN:
        raise IntentStateError("只有 unknown 状态可对账：%s" % key)
    if found_downstream:
        return transition(con, key, CONFIRMED,
                          note=note or "对账确认：下游已成交",
                          result_ref=result_ref)
    return transition(con, key, RELEASED,
                      note=note or "对账确认：下游未收到，可重新提交")


def begin_submit(con, key, note=""):
    """进入提交中。unknown 状态直接拒绝——必须先对账，禁止盲目重发；
    confirmed 状态返回原结果（重复请求不产生新订单）。"""
    row = get_intent(con, key)
    if row is None:
        raise IntentStateError("意图不存在：%s" % key)
    if row["state"] == UNKNOWN:
        raise IntentStateError(
            "结果未知，禁止盲目重发：先 reconcile_unknown 对账（%s）" % key)
    if row["state"] == CONFIRMED:
        return row
    return transition(con, key, SUBMITTING, note=note or "提交中")


# ------------------------------------------------------- ② 拦截事件链

def _canon(fields):
    return json.dumps(fields, ensure_ascii=False, sort_keys=True,
                      default=str)


def _db_key(con):
    """锚点必须绑定具体数据库：按连接的库文件绝对路径派生键，
    防止测试库/副本库的链头覆盖生产库锚点（2026-10-30 部署后
    复核实测发现的缺陷：门禁 pytest 的管线跑批曾把测试链链头
    写进生产锚点，导致生产校验误报不符）。"""
    try:
        rows = con.execute("PRAGMA database_list").fetchall()
        path = rows[0][2] if rows else ""
    except Exception:  # noqa: BLE001
        path = ""
    if not path:
        return "memory"
    return hashlib.sha256(
        os.path.abspath(path).encode("utf-8")).hexdigest()[:12]


def chain_head_path(db_key=None):
    name = "risk_block_chain_head.json"
    if db_key:
        name = "risk_block_chain_head_%s.json" % db_key
    return os.path.join(BASE, "reports", name)


def checkpoint_path(db_key=None):
    """链头检查点（独立于仓库与推送白名单）：环境变量
    PAPER_SIM_CHAIN_CHECKPOINT 可覆盖；默认在目标工作区
    hidden_files（不在 git 仓内、不随同步推送）。与仓内锚点
    双写双验：攻击者只改库、或连仓内锚点一起改，仍会被仓外
    检查点发现；仓与检查点同机整体替换不在检测范围内
    （如实披露，需异机检查点才可防）。"""
    p = os.environ.get("PAPER_SIM_CHAIN_CHECKPOINT")
    if p:
        return p
    name = "risk_block_chain_checkpoint.json"
    if db_key:
        name = "risk_block_chain_checkpoint_%s.json" % db_key
    return os.path.expanduser(
        "~/workspace/goals/paper-trading-simulator/hidden_files/" + name)


def _write_head_anchor(con, row_hash, event_id):
    payload = {"head_hash": row_hash, "event_id": event_id, "at": _now()}
    key = _db_key(con)
    for p in (chain_head_path(key), checkpoint_path(key)):
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except OSError:
            pass  # 写失败不掩盖主写入结果；verify 会报告锚点/检查点缺失


def log_block_event(con, *, trade_date, account=None, market=None,
                    symbol, side="BUY", source="", institution_evidence=None,
                    strategy_version=None, screen_passed=None,
                    screen_score=None, rule_name, rule_version=RULE_VERSION,
                    reason, b31_state=None, index_close=None,
                    index_ma20=None, data_ts=None, request_id=None,
                    idempotency_key=None, order_id=None,
                    disposition="blocked", event_id=None, ts=None):
    """追加一条风控拦截事件（哈希链）。失败 → 兜底留痕 + 抛错。"""
    ensure_safety_tables(con)
    fields = {
        "event_id": event_id or uuid.uuid4().hex,
        "ts": ts or _now(),
        "trade_date": trade_date, "account": account, "market": market,
        "symbol": symbol, "side": side, "source": source,
        "institution_evidence": _canon(institution_evidence)
        if institution_evidence is not None else None,
        "strategy_version": strategy_version,
        "screen_passed": (None if screen_passed is None
                          else int(bool(screen_passed))),
        "screen_score": screen_score,
        "rule_name": rule_name, "rule_version": rule_version,
        "reason": reason, "b31_state": b31_state,
        "index_close": index_close, "index_ma20": index_ma20,
        "data_ts": data_ts, "request_id": request_id,
        "idempotency_key": idempotency_key, "order_id": order_id,
        "disposition": disposition,
        "review_status": "pending", "review_at": None,
    }
    try:
        last = con.execute(
            "SELECT row_hash FROM risk_block_events"
            " ORDER BY rowid DESC LIMIT 1").fetchone()
        prev = last[0] if last else GENESIS
        row_hash = hashlib.sha256(
            (prev + _canon(fields)).encode("utf-8")).hexdigest()
        cols = list(fields.keys()) + ["prev_hash", "row_hash"]
        con.execute(
            "INSERT INTO risk_block_events(%s) VALUES(%s)"
            % (",".join(cols), ",".join("?" * len(cols))),
            [fields[c] for c in fields] + [prev, row_hash])
    except Exception as e:  # noqa: BLE001 - 必须留痕并响亮失败
        try:
            p = os.path.join(BASE, "reports",
                             "risk_block_log_failures.jsonl")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(_canon({"failed_at": _now(), "error": str(e),
                                "event": fields}) + "\n")
        except OSError:
            pass
        raise RiskBlockLogError("拦截事件写入失败：%s" % e) from e
    _write_head_anchor(con, row_hash, fields["event_id"])
    return fields["event_id"]


_CHAIN_COLS = ["event_id", "ts", "trade_date", "account", "market",
               "symbol", "side", "source", "institution_evidence",
               "strategy_version", "screen_passed", "screen_score",
               "rule_name", "rule_version", "reason", "b31_state",
               "index_close", "index_ma20", "data_ts", "request_id",
               "idempotency_key", "order_id", "disposition",
               "review_status", "review_at"]


def verify_block_chain(con, check_anchor=True):
    """校验哈希链完整性。返回 (ok, detail)。
    能检出：内容篡改、中间删除、顺序回退；check_anchor 时另验链头
    锚点（防尾部删除/整体回退）。"""
    ensure_safety_tables(con)
    rows = con.execute(
        "SELECT %s,prev_hash,row_hash FROM risk_block_events"
        " ORDER BY rowid" % ",".join(_CHAIN_COLS)).fetchall()
    prev = GENESIS
    last_hash = None
    for r in rows:
        fields = dict(zip(_CHAIN_COLS, r[:len(_CHAIN_COLS)]))
        stored_prev, stored_hash = r[len(_CHAIN_COLS)], r[len(_CHAIN_COLS) + 1]
        if stored_prev != prev:
            return False, "链断裂于事件 %s（prev_hash 不衔接）" % fields["event_id"]
        expect = hashlib.sha256(
            (prev + _canon(fields)).encode("utf-8")).hexdigest()
        if expect != stored_hash:
            return False, "事件 %s 内容被篡改（row_hash 不符）" % fields["event_id"]
        prev = stored_hash
        last_hash = stored_hash
    if check_anchor and last_hash is not None:
        _key = _db_key(con)
        for label, p in (("仓内锚点", chain_head_path(_key)),
                         ("仓外检查点", checkpoint_path(_key))):
            try:
                with open(p, encoding="utf-8") as f:
                    anchor = json.load(f)
            except (OSError, ValueError):
                return False, "%s缺失/不可读（疑似尾部回退或%s丢失）" % (
                    label, label)
            if anchor.get("head_hash") != last_hash:
                return False, "链头与%s不符（疑似尾部删除/回退/全链重写）" % label
    return True, "链完整，共 %d 条" % len(rows)


def _rule_name_of(reason):
    r = reason or ""
    if r.startswith("跌破母板止损参考"):
        return "ban_entry_stop_ref"
    if "安全降级" in r:
        return "env_gate_degraded"
    if r.startswith("市场环境闸") or "环境闸" in r:
        return "env_gate"
    if r.startswith("仓位拦截"):
        return "position_sizer_v2"
    if "行情过期" in r or "无行情数据" in r:
        return "data_freshness"
    return "risk_gate_other"


def mirror_blocked_signals(con, *, strategy_version=None, env=None,
                           source="pipeline"):
    """把 blocked_signals 的行镜像为结构化哈希链事件（幂等：
    event_id 由 (date,account,symbol,side,signal_date) 确定性
    生成，已镜像的行跳过）。env 提供本轮 B31/指数证据。
    返回新镜像条数。"""
    ensure_safety_tables(con)
    auto = (env or {}).get("auto") or {}
    b31 = None
    if env:
        b31 = "是" if env.get("b31") else "否"
    n = 0
    rows = con.execute(
        "SELECT date,signal_date,account,symbol,side,signal_price,"
        "exec_price,stop_ref,reason FROM blocked_signals").fetchall()
    for (d, sig_d, aid, s, side, sig_px, exec_px, _sr, reason) in rows:
        eid = hashlib.sha256(
            ("blocked|%s|%s|%s|%s|%s" % (d, sig_d, aid, s, side)
             ).encode("utf-8")).hexdigest()
        if con.execute("SELECT 1 FROM risk_block_events WHERE event_id=?",
                       (eid,)).fetchone():
            continue
        log_block_event(
            con, trade_date=d, account=aid, symbol=s, side=side,
            source=source, strategy_version=strategy_version,
            rule_name=_rule_name_of(reason), reason=reason,
            b31_state=b31, index_close=auto.get("close"),
            index_ma20=auto.get("ma20"), data_ts=auto.get("last_date"),
            idempotency_key=make_idempotency_key(
                aid, "sim", s, side, strategy_version or "",
                sig_d or ""),
            event_id=eid)
        n += 1
    return n


def reanchor(con):
    """锚点/检查点丢失或被无关跑批覆盖后的恢复动作：先做链内
    校验（哈希链本身必须自洽），通过后把当前链头重写到本库
    专属的双锚点。链内校验不过时拒绝重锚（不给篡改背书）。"""
    ok, detail = verify_block_chain(con, check_anchor=False)
    if not ok:
        raise RiskBlockLogError("链内校验未过，拒绝重锚：%s" % detail)
    row = con.execute(
        "SELECT event_id, row_hash FROM risk_block_events"
        " ORDER BY rowid DESC LIMIT 1").fetchone()
    if row:
        _write_head_anchor(con, row[1], row[0])
    return True, "已重锚（%s）" % detail


def query_block_events(con, *, trade_date=None, symbol=None,
                       strategy_version=None, rule_name=None):
    ensure_safety_tables(con)
    q = "SELECT * FROM risk_block_events WHERE 1=1"
    args = []
    for col, val in (("trade_date", trade_date), ("symbol", symbol),
                     ("strategy_version", strategy_version),
                     ("rule_name", rule_name)):
        if val is not None:
            q += " AND %s=?" % col
            args.append(val)
    q += " ORDER BY rowid"
    cur = con.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
