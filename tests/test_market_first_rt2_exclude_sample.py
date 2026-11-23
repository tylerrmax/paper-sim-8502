# -*- coding: utf-8 -*-
"""RT-2 对抗测试（2026-11-23 自红队补正）：exclude_sample 来源强制。

自红队口径（冻结包 07_SELF_REDTEAM RT-2）：exclude_sample 的
reason / decided_by 形参虽必填，此前不校验非空，空字符串可
成功写入 sample_exclusions——排除记录直接改变 forward_stats
与 judge 的样本构成，空来源使「谁、为何排除」不可追溯，且
与 P0-3 已确立的「空白即无依据」标准自相矛盾。修复后：两
字段必须非空（空白/纯空格视为无），缺失在写库前抛
ValueError，排除不落字。

对抗形态：
- 空 reason / 空白 reason -> 拒绝且不落字；
- 空 decided_by / 空白 decided_by -> 拒绝且不落字；
- 正对照：信息齐备 -> 排除落字且进入 excluded_sample_ids。
"""
import pytest

from market_first.models import DiscoveryEvent
from market_first.pool import CandidatePool
from market_first.validation import (create_sample, exclude_sample,
                                     excluded_sample_ids, get_sample)

BARS = [
    ("2026-11-12", 25.0, 25.5, 24.5, 25.2, 1000.0),
    ("2026-11-13", 26.0, 26.5, 25.8, 26.25, 1000.0),
    ("2026-11-16", 26.8, 27.2, 26.6, 27.00, 1000.0),
    ("2026-11-17", 27.1, 27.4, 26.9, 27.30, 1000.0),
]


def _pool_with_sample(tmp_path):
    pool = CandidatePool(db_path=str(tmp_path / "mf.db"))
    ev = DiscoveryEvent(
        market="SZ", symbol="002623", name="亚玛顿",
        source_id="inst_lhb_live",
        discovered_at="2026-11-13T16:30:00+08:00",
        trigger_condition="inst_net>0", evidence={"inst_net": 100.0})
    cid, _ = pool.ingest(ev)
    pool.conn.execute(
        "UPDATE discovery_events SET created_at=?"
        " WHERE candidate_id=?", (ev.discovered_at, cid))
    pool.conn.commit()
    ev_id = pool.events_for(cid)[0]["id"]
    sid = create_sample(pool, cid, "inst_lhb_live", kind="forward",
                        discovery_event_id=ev_id, bars=BARS)
    return pool, sid


def _exclusion_rows(pool):
    return pool.conn.execute(
        "SELECT COUNT(*) c FROM sample_exclusions").fetchone()["c"]


def test_exclude_requires_reason(tmp_path):
    pool, sid = _pool_with_sample(tmp_path)
    with pytest.raises(ValueError):
        exclude_sample(pool, sid, "forward_stats", "",
                       decided_by="independent_audit")
    with pytest.raises(ValueError):
        exclude_sample(pool, sid, "forward_stats", "   ",
                       decided_by="independent_audit")
    assert _exclusion_rows(pool) == 0
    assert excluded_sample_ids(pool) == set()
    assert get_sample(pool, sid) is not None  # 样本行本身不动


def test_exclude_requires_decided_by(tmp_path):
    pool, sid = _pool_with_sample(tmp_path)
    with pytest.raises(ValueError):
        exclude_sample(pool, sid, "forward_stats", "审计裁定排除",
                       decided_by="")
    with pytest.raises(ValueError):
        exclude_sample(pool, sid, "forward_stats", "审计裁定排除",
                       decided_by="  ")
    assert _exclusion_rows(pool) == 0
    assert excluded_sample_ids(pool) == set()


def test_exclude_with_full_info_positive_control(tmp_path):
    pool, sid = _pool_with_sample(tmp_path)
    xid = exclude_sample(pool, sid, "forward_stats", "审计裁定排除",
                         decided_by="independent_audit")
    assert xid >= 1
    assert excluded_sample_ids(pool) == {sid}
    row = pool.conn.execute(
        "SELECT * FROM sample_exclusions WHERE id=?",
        (xid,)).fetchone()
    assert row["reason"] == "审计裁定排除"
    assert row["decided_by"] == "independent_audit"


# ---------------------------------------------------------------------------
# RT-2 强化（2026-11-23 红队集中修复第 2 项强化）：原子性
# 排除行与 candidate_history 审计行同事务写入；失败整体回滚，
# forward_stats 构成不变（以 excluded_sample_ids 为构成口径实证）。
# ---------------------------------------------------------------------------

def _audit_rows(pool):
    return pool.conn.execute(
        "SELECT COUNT(*) c FROM candidate_history"
        " WHERE field='sample_exclusion'").fetchone()["c"]


def test_exclude_writes_audit_history_atomically(tmp_path):
    """成功路径：排除行与审计行同时存在，审计行带
    actor=decided_by / reason / excluded_from。"""
    pool, sid = _pool_with_sample(tmp_path)
    exclude_sample(pool, sid, "forward_stats", "审计裁定排除",
                   decided_by="independent_audit")
    assert _exclusion_rows(pool) == 1
    assert _audit_rows(pool) == 1
    row = pool.conn.execute(
        "SELECT * FROM candidate_history"
        " WHERE field='sample_exclusion'").fetchone()
    assert row["actor"] == "independent_audit"
    assert row["reason"] == "审计裁定排除"
    assert row["new_value"] == "forward_stats"


def test_failed_exclude_rolls_back_both_writes(tmp_path):
    """失败回滚实证：重复排除（UNIQUE 冲突）在写审计行前失败
    ——但即使冲突发生在第二写，也必须整体回滚：排除行仍为
    1、审计行仍为 1、forward_stats 构成不变，且连接不在悬挂
    事务中（后续正常排除其他口径可成功）。"""
    pool, sid = _pool_with_sample(tmp_path)
    exclude_sample(pool, sid, "forward_stats", "审计裁定排除",
                   decided_by="independent_audit")
    stats_before = excluded_sample_ids(pool)
    with pytest.raises(ValueError):
        exclude_sample(pool, sid, "forward_stats", "重复排除",
                       decided_by="independent_audit")
    assert _exclusion_rows(pool) == 1
    assert _audit_rows(pool) == 1
    assert excluded_sample_ids(pool) == stats_before == {sid}
    # 回滚后连接可用：另一口径排除成功，两行同步增长
    exclude_sample(pool, sid, "judge_stats", "法官口径排除",
                   decided_by="independent_audit")
    assert _exclusion_rows(pool) == 2
    assert _audit_rows(pool) == 2
    assert excluded_sample_ids(pool, "judge_stats") == {sid}
