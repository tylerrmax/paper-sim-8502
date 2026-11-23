# 交付报告：红队修复 V1.0「底层边界」补齐（RT-1 / RT-2 两道 DB 闸）

批次：加速补强批次（2026-11-23），基线 unified-arch `a84fb4a`（V2 冻结 HEAD，代码冻结对象 `dd70483`）。
依据：用户 2026-11-23 13:24 重申最终验收原则——关键约束必须在正式入口和底层边界同时成立，不能只靠上层函数自觉遵守；并要求进度加速。
执行中断与恢复：本批第一执行者在 V3 冻结组装前被系统 execution-risk 安全通知停工（两项修复、对抗测试、全量双边对照、台账修改均已完成，未提交/未合并/未冻结）；主会话向用户明示后，用户于 2026-11-23 13:46 明确回复「同意，完成剩余收尾」（范围覆盖本批提交、合并、复跑测试、复制仓库文件组装审计包），本报告为恢复执行后的收尾交付。

## ① 检查对象

基线 `a84fb4a` 上两条只有入口层强制的红队线：

- **RT-1 晋升（MF-PROMO-002）**：promotion.decide 入口强制 approver/basis 非空 + handoff 下游独立核验，但直写 SQL `UPDATE candidates SET promotion_status='approved'` 无 DB 层拦截。
- **RT-2 排除（MF-VAL-001）**：validation.exclude_sample 入口强制 reason/decided_by 非空且与审计行同事务写入，但直写 SQL `INSERT INTO sample_exclusions` 无 DB 层拦截（该表原有触发器只有 benchmark 不可变与禁删）。

设计先行：`evidence/redteam-bottom-layer-20261123/design/DESIGN-BOTTOM-LAYER.md`（沿用既有 `ensure_redteam_schema` / `trg_rt_*` 机制，含实施前全仓普查：直写 sample_exclusions 全仓仅 exclude_sample 一处；无夹具直写 promotion_status='approved'）。

## ② 实际证据

**两项修复提交**（隔离分支 `market-first/redteam-bottom-layer-20261123`，各自独立提交）：

| 项 | 提交 | 内容 | 新增对抗测试 |
|---|---|---|---|
| RT-1 底层闸 | `f273d5b` | db.py 新增 `trg_rt_candidates_promotion_approved_insert` / `_update`：进入 approved 必须有 decision='approved' 且 approver/basis 均非空（trim 后）的 approvals 行，否则 RAISE(ABORT)；INSERT 与 UPDATE 两路径全覆盖。decide 写序经核查为「先 INSERT approvals、后流转、同事务」，天然通过，promotion.py 零改动 | `tests/test_market_first_rt1_db_guard.py` 5 用例（正常路径/decide 底层正对照/直写 UPDATE 拒绝/直写 INSERT 拒绝/空字段 approvals 不放行+回滚留痕） |
| RT-2 底层闸 | `59b2eb7` | db.py 新增 `trg_rt_sample_exclusions_audit_insert`：每条排除 INSERT 必须 reason/decided_by 非空，且同事务已有逐字对应的 candidate_history 审计行（field='sample_exclusion'、new_value=excluded_from、actor=decided_by、reason=reason）；validation.exclude_sample 写序配合改为先审计行后排除行（同事务单次 commit，两行集合/字段/返回值不变，RT-2 原子性语义不变） | `tests/test_market_first_rt2_db_guard.py` 5 用例（正常路径/底层正对照/无审计行拒绝/空白字段拒绝/审计行不一致拒绝+回滚留痕） |

**回归对照**（原始日志在 `evidence/redteam-bottom-layer-20261123/logs/`，同命令双边实测：`pytest tests/ -q --ignore=tests/test_gapfill.py --ignore=tests/test_ui_regression.py`，两 ignore 文件为脚本型/环境型收集期失败，与上一批口径差异已在日志中可查）：

- pristine 基线 `a84fb4a`：358 passed / 7 failed（245.60s）
- 修复分支：**368 passed / 7 failed**（247.17s），+10 恰为新增用例
- 两轮 FAILED 清单程序化 diff **逐项完全相同**（6 个 stress_test.py 环境性失败 + 既有基线失败 `test_ledger_fingerprint_idempotent`），**新增失败为零**；300 秒门槛未动
- 快速组实测：新 10 用例 + 相关既有 31 = 41 passed
- 触发器安装经 init_db 实测枚举：`trg_rt_*` 共 7 条（原 4 + 新 3）

注：上一批任务书基线「354 passed/1 failed」为较窄口径，本批以同命令双边实测为准，差异如实登记。

**台账**（audit_issues.jsonl，19 行：READY 16 / OPEN 3 / CLOSED 0，无自标 CLOSED）：MF-PROMO-002 追加 fix_commit `f273d5b` 与测试 `test_market_first_rt1_db_guard.py`、MF-VAL-001 追加 fix_commit `59b2eb7` 与测试 `test_market_first_rt2_db_guard.py`，两条 residual_risk 均已更新底层闸证据与固有界限说明；其余条目未动（MF-HANDOFF-001、MF-RES-002=RT-4、MF-PROMO-003=RT-6 保持 OPEN）。

**生产隔离**：生产库 sim.db SHA256 开工实测 `c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f`，与基准一致；全程只用隔离临时库。收工 SHA 与合并后复跑结果记录在 V3 冻结包 `01_source_and_commits/merge_and_head_check_record.md`。

## ③ 复核方法

每项五类测试（正常路径/底层正对照/直写 UPDATE 拒绝/直写 INSERT 拒绝/字段缺项与不一致拒绝+回滚留痕）；触发器经 init_db 枚举实证；全量回归同命令双边对照 + 失败集程序化 diff；台账 JSON 逐行解析校验；合并后复跑 Market-First 相关组（结果入 V3 包 04 节）。

## ④ 结论

两道底层闸的代码、测试与回归证据齐备：RT-1/RT-2 的关键约束现已在正式入口与 DB 底层边界同时成立。本批范围内**已验证通过**（以本批自测与复核证据为准；独立审计关闭仍待审计方置 CLOSED，台账状态保持 READY_FOR_AUDIT）。

## ⑤ 剩余风险

1. **DB 直写固有界限（待审计裁定，设计补记与 V3 GAPS 已明示）**：同一主体全套一致伪造（先伪造齐全 approvals 行再改状态、先伪造逐字对应 candidate_history 审计行再写排除）在 SQLite 单文件、无独立审计主体模型下与合法写入不可区分；本批已封「无对应记录/字段缺项」的绕过，全套伪造靠 handoff 下游核验与审计抽查作第二道防线。
2. RT-4（MF-RES-002）/ RT-6（MF-PROMO-003）已登记 OPEN 单独评估；source_policy_registry 缺口保持 OPEN；既有基线失败 test_ledger_fingerprint_idempotent 不在本批范围。
3. 本批只到隔离线 unified-arch 合并，未部署生产 master——生产部署仍须独立审计关闭三项 P0 后另行批准。
