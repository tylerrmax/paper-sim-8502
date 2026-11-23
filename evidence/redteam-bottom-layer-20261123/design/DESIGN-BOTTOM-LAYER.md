# 设计补记：红队修复 V1.0「底层边界」补齐（晋升 / 排除两道 DB 闸）

批次：加速补强批次（2026-11-23），基线 unified-arch `a84fb4a`。
依据：用户 2026-11-23 13:24 重申最终验收原则——关键约束必须在
正式入口和底层边界同时成立，不能只靠上层函数自觉遵守。

## 背景与缺口

红队 V1.0 八项中，第 5 项（rule_registry 审批）与第 6 项
（discovery_events.created_at）已在 DB 层成立（`trg_rt_*` 触发器，
见 DESIGN-P0-3-P0-5-RT-5.md）。但晋升与排除两条线只有入口层：

- RT-1：promotion.decide 入口强制 approver/basis 非空，handoff
  下游再独立核验 approvals 记录；但直写 SQL
  `UPDATE candidates SET promotion_status='approved'` 无 DB 层
  拦截，可绕过入口直接伪造晋升终态。
- RT-2：exclude_sample 入口强制 reason/decided_by 非空且与审计
  行同事务写入；但直写 SQL `INSERT INTO sample_exclusions`
  无 DB 层拦截，可无审计留痕地改变 forward_stats 样本构成。

## 设计（沿用 ensure_redteam_schema / trg_rt_* 同一机制）

新触发器追加进 `market_first/db.py` 的 `_REDTEAM_TRIGGERS_SQL`，
由既有 `ensure_redteam_schema` 幂等安装（init_db 与
validation._ensure 两处挂载点不变，新旧库一致）：

### 闸 1：晋升底层闸（RT-1 底层化）

- `trg_rt_candidates_promotion_approved_insert`：BEFORE INSERT
  ON candidates，WHEN NEW.promotion_status='approved'。
- `trg_rt_candidates_promotion_approved_update`：BEFORE UPDATE
  OF promotion_status ON candidates，WHEN
  NEW.promotion_status='approved' AND OLD<>'approved'
  （只管「进入 approved」的流转，已 approved 行的其他更新
  不拦，与 rule_registry 闸同口径）。
- 两者均要求：同库 EXISTS approvals 行，candidate_id=NEW.id、
  decision='approved'、approver 非空（trim 后）、basis 非空
  （trim 后）；不满足 RAISE(ABORT)。
- 应用路径天然通过：promotion.decide 在同一事务内**先**写
  approvals 行、**后**经 _transition_promotion_status 更新
  candidates（现有写序，无需调整）；SQLite 同连接可见同事务
  未提交行，与 P0-3 registry_history 闸同机制。

### 闸 2：排除底层闸（RT-2 底层化）

- `trg_rt_sample_exclusions_audit_insert`：BEFORE INSERT ON
  sample_exclusions，无 WHEN（每条排除都管）：
  1. NEW.reason / NEW.decided_by 必须非空（trim 后）；
  2. 必须 EXISTS 对应审计行：candidate_history 中
     candidate_id = 该样本（validation_samples 查得）的
     candidate_id、field='sample_exclusion'、
     new_value=NEW.excluded_from、actor=NEW.decided_by、
     reason=NEW.reason（审计行与排除行的决定人/理由逐字
     对应，防「随便留一行痕再排除」）。
- 写序调整：validation.exclude_sample 改为同事务内**先**写
  candidate_history 审计行、**后**写 sample_exclusions 行
  （两行集合、字段、返回值与异常口径不变；原 RT-2 原子性
  语义——同事务、单次 commit、失败整体 rollback——不变）。

## 全仓普查（实施前实查）

- 直写 INSERT INTO sample_exclusions 的代码全仓仅
  validation.exclude_sample 一处（evidence 历史快照除外）。
- 无测试夹具直写 candidates.promotion_status='approved'
  的 SQL；set_fields 路径由 pool 层先行拒绝（MF-PROMO-001），
  不与 DB 闸冲突。
- candidates 的 INSERT 只有 pool.ingest（状态取默认
  'discovered'），INSERT 闸不影响正常入库。

## 诚实残余（与 V1.0 同口径）

- 同一主体若按合法形态**全套一致伪造**（先伪造 approvals
  行再改状态；先伪造 candidate_history 审计行再写排除行），
  DB 触发器无法与应用层合法写入区分——触发器防的是「无对
  应记录/字段缺项」的绕过。全套伪造在 SQLite 单文件、无独
  立审计主体的模型下属固有界限，handoff 下游核验与审计抽
  查仍是第二道防线；本界限登记为待审计裁定项，不在本批
  扩大范围。
