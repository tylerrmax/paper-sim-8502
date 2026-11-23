# 交付说明：红队缺陷集中修复 V1.0（2026-11-23）

指令依据：用户《红队缺陷集中修复指令 V1.0》（2026-11-23 11:38）+
10:58 合并推送授权 + 11:19 自红队要求 + 11:29 执行风险明示同意 +
11:40 推送合并授权（合并推送归主会话执行，本交付止于合并后 HEAD）。
基线：unified-arch `c490424` → 合并后 HEAD **`0d93eb8`**（--no-ff
合并提交，零冲突）。生产库 sim.db 开工/收工/合并后 SHA256 均为
`c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f`，
逐字节一致，生产零影响。

## 八项修复（各自独立提交）

| # | 项 | 提交 | 要点 |
|---|---|---|---|
| 1 | RT-1 晋升批准强制+入口全枚举 | 5c08010 + 75b4b88 | decide approved 须 approver+basis 非空（写库前拒绝、整事务回滚）；普查实证 transition 直达/_apply_validation_passed 伪造 decision_id/set_fields 直改均被拒，decide 为唯一入口 |
| 2 | RT-2 排除来源强制+原子性 | 405ecfc + ebd7717 | reason/decided_by 非空强制；排除行与 candidate_history 审计行同事务原子写入，失败显式 rollback、forward_stats 构成不变（实证） |
| 3 | RT-3 机构因子时间锚 | 417d324 | InstFlowModule 最新事件改以服务器入库 created_at 排序（id 序兜底），discovered_at 只留痕 |
| 4 | P0-1 登记期拒绝 | cdce393 | register_rule_implementation 拒绝显式 version ≠ 实现自报版本（含无自报版本单方指定）；judge 双边核对不变 |
| 5 | P0-3 数据库审批保护 | acf54d9 | 先设计后实施（evidence/design/DESIGN-P0-3-P0-5-RT-5.md）；三触发器：registry 进入 approved 须批准字段非空+approved_at 与 DB 时间差 ≤120s；history approved 记录须有状态一致的 registry 行 |
| 6 | P0-5 created_at 可信性 | 1818018 | 触发器禁 created_at 落字后 UPDATE 篡改；CandidatePool 可信时钟夹具（生产缺省真实服务器时间） |
| 7 | RT-5 evaluate bars 校验 | 8b16cd5 | forward 样本 bars 须与自有 fetched_bars 逐 bar 一致（标的取库内 symbol）；不可信写 not_evaluable+bars_untrusted 留痕、收益全 NULL；bars.store_bars 为受控落库通道 |
| 8 | MF-PROMO-001 set_fields 封口 | bd731e2 | set_fields 对 promotion_status='approved' 无条件拒绝；状态机改走 pool._transition_promotion_status 专用通道；handoff 核验行为不变 |

## 五类测试与回归对照

- 每项均含：正常路径 / 缺参拒绝 / 直绕或 DB 污染对抗 / 失败回滚
  及留痕 / 既有回归（新增测试文件 8 个、新增用例 43）。
- 纯基线全量（c490424）：311 passed / 1 failed（日志
  logs/baseline_full_pytest.log）。
- 修复后全量（同口径命令，300 秒门槛未动）：**354 passed /
  1 failed**，唯一失败仍为既有基线失败
  test_ledger_fingerprint_idempotent，**新增失败为零**
  （logs/item6_full_regression_v2.log，250.26s）。
- 专项：Market-First 全部+对抗回归 139 passed
  （logs/item3_market_first_all_v2.log；构成 = tests/ 下全部
  test_market_first_*.py + test_adversarial_regression.py，经 -k
  "market_first or adversarial" 选取）；G3/G5 排除统计组 25
  passed（forward_time + validation_gate + validation + rt2）；
  晋升/交接/边界组 77 passed（promotion_handoff + p0_1 x2 +
  p0_3 x2 + p0_5 x2 + rule_binding + rule_registry + rt1 x2 +
  mf_promo_001 + rt5）。
- 合并后复跑 Market-First 全部：139 passed
  （logs/postmerge_market_first_all.log）。

## 既有测试构造迁移登记（断言零改动）

1. **P0-1 冒充构造（2 处，用户指令原文口径）**：
   test_market_first_rule_binding.py::
   test_impl_mismatch_impl_impersonates_version、
   test_market_first_p0_1_version_binding.py::
   test_v1_impl_cannot_impersonate_v2_via_explicit_version——
   原 API 冒充登记构造改为直写 RULE_IMPLEMENTATIONS 污染，
   judge 拒绝断言与覆盖意图逐字保留。
2. **P0-5 created_at 直写迁移（15 处）**：旧
   `UPDATE discovery_events SET created_at` 全部改为
   CandidatePool 时钟夹具（ingest 前设 pool.clock）：文件与
   处数——adversarial_regression x2、benchmark_integrity、
   forward_time、p0_1_version_binding、p0_5_time_anchor
   （ingested_at 参数）、promotion_handoff、rule_binding、
   rule_registry x2、validation_gate、rt1_decide_approval、
   rt1_entry_census、rt2_exclude_sample、
   mf_promo_001_set_fields。
3. **RT-5 fetched_bars 落库迁移（11 处/9 文件）**：forward
   评估 helper 在 evaluate 前加一行 bars_mod.store_bars 经
   受控通道落库：adversarial_regression x2、
   mf_promo_001_set_fields、p0_1_version_binding、
   promotion_handoff、rt1_decide_approval、rt1_entry_census、
   rule_binding、rule_registry、validation_gate x2（含
   bars[:8] 子集评估处）。
4. 第 5 项夹具处置：全仓实查无直写 rule_registry /
   registry_history 的测试夹具（grep 证据：tests/ 与
   market_first/ 内除 validation.py/db.py 外零命中），既有
   夹具全走应用层、天然通过触发器，无需受控通道。

## 台账（audit_issues.jsonl，19 行）

READY_FOR_AUDIT 16 / OPEN 3 / CLOSED 0（CLOSED 只待独立审计）。
新增：MF-PROMO-002（RT-1）、MF-VAL-001（RT-2）、MF-RES-001
（RT-3）、MF-VAL-002（RT-5）均 READY；MF-RES-002（RT-4）、
MF-PROMO-003（RT-6）按用户裁定新增 OPEN 单独评估；
MF-PROMO-001 由 OPEN 转 READY（第 8 项）；MF-REG-002 /
MF-REG-003 / MF-FWD-002 追加本批修复提交与残余说明；
MF-HANDOFF-001 保持 OPEN 不动。

## 剩余风险（如实登记）

1. DB 直写固有界限：同主体全套一致伪造 registry 批准字段
   （当前时间+齐全字段）、直写 INSERT 伪造 discovery_events
   新行、直写污染 fetched_bars——在 SQLite 单文件、无独立
   审计主体下与合法写入不可区分；本批触发器已封「缺项/
   回填时间/无对应行/落字后篡改」四类，余下待独立审计裁定
   是否需要更强机制（设计文档第一节/第二节已明示）。
2. RT-4/RT-6 已登记 OPEN 单独评估；source_policy_registry
   缺口（MF-REG-003 残余）保持 OPEN。
3. 既有基线失败 test_ledger_fingerprint_idempotent 未在本批
   范围（与上批口径一致）。
4. 本批只合并入隔离线 unified-arch，未推送、未部署生产
   master；推送由主会话按 11:40 授权执行，生产部署仍须
   独立审计关闭 P0 后另行批准。
