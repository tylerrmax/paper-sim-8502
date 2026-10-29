# 可验证 · 可追责 · 可恢复（系统完整性规范）

目标：证明系统能长期一致运行，而不是靠打补丁掩盖脆弱性。每一条都有可运行的检查，不靠口头保证。

| 维度 | 标准 | 如何验证 |
|---|---|---|
| 可验证 | 持仓 = fills 重放；现金 = 本金 + 成交现金流；NAV = 现金 + 持仓市值 | `python3 tools/check_consistency.py` |
| 可验证 | 行情入库前通过闸门（OHLC、重复、涨跌幅、成交量口径） | `engine/data_gate.py`；`check_consistency` 内含 |
| 可验证 | 非法写入被拒绝（side/qty/price/status/decision） | `db/migrations/003_integrity.sql` 触发器；`tests/test_integrity.py` |
| 可追责 | 每笔成交可追溯到当时的信号与策略版本 | `signal_log`（只增不改，记录状态迁移） |
| 可追责 | 审批、成交、启停控制留操作痕迹 | `audit_log`（只增不改）；`approvals.actor/reason` |
| 可追责 | 券商私有数据不进公开库、也不进审计 | 同步时清空并 VACUUM；无审计触发器 |
| 可恢复 | 库损坏时可只靠 fills 重建 positions/现金/净值并比对 | `python3 tools/rebuild_from_fills.py [--write 副本]` |
| 可恢复 | 迁移幂等、先备份、失败整体回滚 | `python3 tools/migrate.py` |
| 可恢复 | 同步为单次提交，不出现新程序 + 旧数据的混合状态 | `tools/sync_atomic.py` |
| 可恢复 | 部署清单覆盖全部被引用文件 | `python3 tools/sync_manifest.py` |

## 严重度
- error（阻止同步）：账本分叉/负持仓、现金重放不符、NAV 等式破坏、OHLC 非法、**成交量单位切换**、表缺失、完整性失败、行情过期。
- warn（记录但不阻止）：偶发成交量异常、历史成交无法追溯到信号（legacy）、无持仓的待执行卖出、个别标的陈旧、日报滞后。`--strict` 则 warn 也失败。

## 已知的、尚未修复的事实（2026-10-27 实测 master）
- A股 6 档标的成交量自 10-08 / 10-16 起整体放大约 80～150 倍（疑似手→股），历史与近期口径不一致 → 量能类指标不可用。需在数据入库处统一单位并回填。
- 46 笔历史成交（港股/加密）无法追溯到信号：signals 表每轮被重算覆盖。迁移后新成交可追溯，历史部分只能标记为 legacy。
- 无持仓的待执行 SELL：002472、002812、002639。

## 日常流程
1. 跑批后：`python3 tools/check_consistency.py`
2. 同步：`python3 tools/sync_atomic.py --dry-run`，确认后去掉 `--dry-run`
3. 每周：`python3 tools/rebuild_from_fills.py`（恢复演练）
