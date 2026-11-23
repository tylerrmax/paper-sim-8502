"""Promotion & Approval：晋升状态机与人工审批。

状态机由显式转移表 TRANSITIONS 强制流转，任何表外转移都会
先在 candidate_history 留痕（field='promotion_transition_attempt'）
再抛 IllegalTransitionError，绝不静默放行。

验证通过 != 自动批准：进入 approved 的唯一入口是 decide()
（人工审批，approver/basis/versions 全量留痕于 approvals 表）。
"""
from __future__ import annotations

import json
from datetime import datetime

# 全部状态（顺序即生命周期顺序）
STATES = (
    "discovered",
    "pending_research",
    "researching",
    "validating",
    "validation_passed",
    "validation_failed",
    "pending_approval",
    "approved",
    "rejected",
)

# 显式转移表：当前状态 -> 允许的下一状态集合（终态为空集）
TRANSITIONS = {
    "discovered": frozenset({"pending_research"}),
    "pending_research": frozenset({"researching"}),
    "researching": frozenset({"validating"}),
    "validating": frozenset({"validation_passed", "validation_failed"}),
    "validation_failed": frozenset({"pending_research"}),
    "validation_passed": frozenset({"pending_approval"}),
    "pending_approval": frozenset({"approved", "rejected"}),
    "approved": frozenset(),
    "rejected": frozenset(),
}

DEFAULT_VERSIONS = {"promotion_engine": "mf-promo-1.0"}


class IllegalTransitionError(Exception):
    """非法状态转移（已在 candidate_history 留痕后抛出）。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _reject(pool, cid, current, to_state, actor, detail):
    """非法转移统一路径：先留痕，再抛错。"""
    reason = "ILLEGAL TRANSITION REJECTED: %s" % detail
    pool.record_history(
        cid,
        field="promotion_transition_attempt",
        old=current,
        new=to_state,
        actor=actor,
        reason=reason,
    )
    raise IllegalTransitionError(reason)


# 审批终态：转移表上 pending_approval 可达，但只能经 decide()
# 人工审批进入；公开 transition() 直达一律按非法转移处理。
DECISION_STATES = frozenset({"approved", "rejected"})

# 验证通过态：转移表上 validating 可达，但只能由验证引擎的
# judge_validation 按版本化规则判定产生；公开 transition() 直达
# 一律按非法转移处理（第一轮审计修复 P0-3）。
VALIDATION_GATE_STATES = frozenset({"validation_passed"})


def _apply_transition(pool, cid, current, to_state, actor, reason=""):
    """表内流转的实际执行（调用方已完成合法性判定）。

    经 pool 的状态机专用写通道 _transition_promotion_status
    落字；普通调用方经 set_fields 直改
    promotion_status='approved' 被 pool 层无条件拒绝
    （MF-PROMO-001）。
    """
    pool._transition_promotion_status(
        cid, actor=actor, reason=reason if reason else None,
        new_status=to_state)
    return to_state


def transition(pool, cid, to_state, actor, reason=""):
    """按转移表流转 promotion_status。

    合法：经 pool.set_fields 写字段（逐字段记 history）。
    非法：先 record_history 留痕，再抛 IllegalTransitionError。
    approved / rejected 虽在转移表中（pending_approval 可达），
    但只能经 decide() 进入，transition() 直达按非法处理——
    验证通过 != 自动批准，除 decide 外没有任何函数能进入 approved。
    返回流转后的状态字符串。
    """
    cand = pool.get_by_id(cid)
    if cand is None:
        raise ValueError("候选不存在：id=%s" % (cid,))
    current = cand.get("promotion_status")
    if to_state in DECISION_STATES:
        _reject(
            pool, cid, current, to_state, actor,
            "%s -> %s 属人工审批终态，只能经 decide() 审批进入，"
            "transition() 直达被拒" % (current, to_state),
        )
    if to_state in VALIDATION_GATE_STATES:
        _reject(
            pool, cid, current, to_state, actor,
            "%s -> %s 属验证引擎判定态，只能由 "
            "validation.judge_validation 按版本化规则产生，"
            "transition() 直达被拒" % (current, to_state),
        )
    allowed = TRANSITIONS.get(current, frozenset())
    if to_state not in allowed:
        _reject(
            pool, cid, current, to_state, actor,
            "%s -> %s 不在允许转移内（当前状态允许：%s）"
            % (current, to_state,
               ",".join(sorted(allowed)) if allowed else "无，终态"),
        )
    return _apply_transition(pool, cid, current, to_state, actor, reason)


def _apply_validation_passed(pool, cid, actor, reason="",
                             decision_id=None):
    """内部受控入口：validating -> validation_passed。

    唯一合法调用方是 validation.judge_validation 在判定
    decision='passed' 后调用（fix2-3 加固）：必须携带
    decision_id，且库内须存在该 id、属于该候选、decision=
    'passed' 的 validation_decisions 记录；核验不过先在
    candidate_history 留痕再抛 IllegalTransitionError——任何
    模块绕过 judge 直接调用本函数都无法完成流转。当前状态
    不是 validating 时同样拒绝。不经公开 transition()（其对
    validation_passed 直达一律拒绝）。
    """
    cand = pool.get_by_id(cid)
    if cand is None:
        raise ValueError("候选不存在：id=%s" % (cid,))
    current = cand.get("promotion_status")
    verified = False
    if decision_id is not None:
        row = pool.conn.execute(
            "SELECT candidate_id, decision FROM validation_decisions"
            " WHERE id = ?", (decision_id,)).fetchone()
        verified = (row is not None
                    and row["candidate_id"] == cid
                    and row["decision"] == "passed")
    if not verified:
        _reject(
            pool, cid, current, "validation_passed", actor,
            "受控流转核验失败：decision_id=%s 无对应的 "
            "validation_decisions passed 记录，绕过 "
            "judge_validation 的直接调用被拒" % (decision_id,))
    if current != "validating":
        raise IllegalTransitionError(
            "validation_passed 只能从 validating 经 judge_validation "
            "判定流转，当前状态为 %s" % (current,))
    return _apply_transition(pool, cid, current, "validation_passed",
                             actor, reason)


def decide(pool, cid, approver, decision, basis="", versions=None):
    """人工审批：唯一能进入 approved / rejected 终态的入口。

    decision 只接受 'approved' / 'rejected'；当前状态必须为
    pending_approval，否则走非法路径（留痕 + IllegalTransitionError）。
    同一事务内写 approvals 一行并流转到对应终态，返回 approval_id。

    RT-1 批准信息强制（2026-11-23 自红队补正）：decision 为
    'approved' 时必须显式给出非空 approver 与非空 basis
    （批准依据；空白/纯空格视为无）——与 rule 侧 P0-3 的
    set_rule_status approved 分支同口径。任一项缺失在写库前
    抛 ValueError，approvals 不落行、状态不变（整事务回滚）。
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(
            "decision 只接受 'approved' / 'rejected'，收到：%r" % (decision,))
    cand = pool.get_by_id(cid)
    if cand is None:
        raise ValueError("候选不存在：id=%s" % (cid,))
    current = cand.get("promotion_status")
    if current != "pending_approval":
        _reject(
            pool, cid, current, decision, approver,
            "decide(decision=%s) 要求当前状态为 pending_approval，"
            "实际为 %s" % (decision, current),
        )
    if decision == "approved":
        # RT-1：批准必须带批准人与批准依据，缺一在写库前拒绝。
        if not (approver and str(approver).strip()):
            raise ValueError(
                "approving a candidate requires approver")
        if not (basis and str(basis).strip()):
            raise ValueError(
                "approving a candidate requires basis（批准依据）")
    versions_payload = versions if versions is not None else dict(DEFAULT_VERSIONS)
    conn = pool.conn
    try:
        cur = conn.execute(
            "INSERT INTO approvals (candidate_id, decision, approver,"
            " decided_at, basis, versions_json) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, decision, approver, _now_iso(), basis,
             json.dumps(versions_payload, ensure_ascii=False)),
        )
        approval_id = cur.lastrowid
        # 不单独 commit：与随后的状态流转同事务提交
        # （pool.set_fields 内部 commit 一并落盘两处写入）。
        # 审批终态只能经本函数进入，故此处走内部 _apply_transition，
        # 不经公开 transition()（其对 approved/rejected 直达一律拒绝）。
        _apply_transition(
            pool, cid, current, decision, actor=approver,
            reason="人工审批 decide=%s approver=%s approval_id=%s"
                   % (decision, approver, approval_id),
        )
    except Exception:
        conn.rollback()
        raise
    return approval_id
