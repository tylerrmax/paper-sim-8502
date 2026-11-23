# DELIVERY：judge P0 三项补正 + Market-First 集成（2026-11-23）

基线：fix3 代码最终提交 `936e4c6`。集成分支
`market-first/judge-p0-integration-20261123`（独立 worktree
`~/workspace/l2-remediation/judge-p0-integration`），三项各自独立提交；
旧 judge-p0fix 分支（8cede22）只作实现参考，未整体合并/cherry-pick。
证据包：`evidence/judge-p0-integration-20261123/`（INDEX.md 为索引）。

## P0-1 规则版本绑定（提交 b2badaa）

- ①检查对象：register_rule_implementation / register_rule /
  judge_validation 的 (rule_id, version) 绑定链。
- ②实际证据：fix3 基线已含 P0-B 精确绑定——实现按
  (rule_id, version) 登记、条目 impl_version 恒取实现自报版本
  （显式 version 参数只能改变登记键、覆盖不了 impl_version）、
  judge 双边核对 impl_key/impl_version。本轮未改生产代码，
  按审计对抗形态新增常驻测试
  tests/test_market_first_p0_1_version_binding.py（4 用例）。
- ③复核方法：v1 实现经显式 version='v2' 冒充登记 → judge v2
  IMPL_MISMATCH 拒绝；注册表为 v2 伪造 impl_version='v1' →
  NO_IMPL 拒绝；只有 v2 实现时 judge v1 → NO_IMPL 拒绝；
  版本精确一致正对照 → passed。
- ④结论：通过（4/4 对抗用例全过；判定侧拒绝语义与审计口径
  一致——不匹配一律 not_evaluable 并留痕）。
- ⑤剩余风险：register_rule_implementation 允许把实现登记到
  与自报版本不同的键下（既有 fix3 测试
  test_impl_mismatch_impl_impersonates_version 依赖此行为构造
  IMPL_MISMATCH 场景），其危害由 judge 双边核对兜底；若审计
  要求登记期即拒绝，须另行裁定该既有测试的存废。

## P0-3 批准信息（提交 7d2ec5a）

- ①检查对象：register_rule / set_rule_status 的 approved 流转；
  registry_history 留痕；rule_registry 批准字段。
- ②实际证据：基线缺口——无 registry_history 表；approved 登记
  只要求 approved_by、approved_at 可回填；状态转 approved 零
  要求。本轮新增：db.py SCHEMA_SQL + ensure_p0int_schema
  （registry_history 表 + append-only 触发器 +
  rule_registry.approval_basis 列，init_db/_ensure 双挂钩）；
  两条 approved 路径强制 actor/reason/approval_basis，缺一在
  写库前 ValueError；同事务写 approved_by/approved_at（恒
  服务器时间、调用方回填不生效）/approval_basis/留痕；
  registry_history() 读取口。对抗测试
  tests/test_market_first_p0_3_approval_info.py（7 用例）。
- ③复核方法：无批准人/无 reason/无依据/空白依据四种缺失均
  拒绝且回滚（状态不变、批准字段 NULL、留痕不增）；伪造
  approved_at=2020 年 → 落库为服务器时间；齐备时状态+批准
  字段+留痕原子落字；history UPDATE/DELETE 被触发器拒绝；
  非 approved 流转（disabled）保持既有兼容。
- ④结论：通过（7/7 对抗用例全过；受影响既有测试文件 49 个
  用例全过）。
- ⑤剩余风险：本基线（fix3 线）无 source_policy_registry
  （该机制只在 judge 裁定线、未并入 fix3），source 侧无登记
  对象，批准强制只作用于 rule_registry；若日后并入 source
  policy，须按同一口径补强制与留痕。既有测试适配 5 文件
  （仅 helper/调用点补批准参数，断言零改动，diff 可查）。

## P0-5 Forward 时间锚（提交 bbdbe95）

- ①检查对象：create_sample 的发现时间锚与 forward 入场推导。
- ②实际证据：基线 fix3 口径为锚 = max(声称 discovered_at,
  服务器 ingested_at)，且事件行缺 created_at 时退回声称值。
  本轮收紧：锚 = discovery_events.created_at 的上海日期
  单一锚，discovered_at 不再参与锚计算（不一致仍以
  discovery_time_anchor 留痕）；缺 created_at 时 forward 以
  trusted_ingest_time_missing 留痕拒绝。对抗测试
  tests/test_market_first_p0_5_time_anchor.py（6 用例）。
- ③复核方法：2026-09-15 声称发现、2026-11-20 才入库的真实
  对抗三形态——显式 9 月 entry 请求 forward → 强制 backfill
  且不进 forward_stats；只有历史 bars → 拒绝且不落样本；
  bars 齐备 → entry 锚定入库后第一个交易日（11-19 对
  11-18 入库样本）；另：缺入库时间拒绝 forward（backfill
  不受影响）、声称晚于入库不延长锚、同日入库正对照。
- ④结论：通过（6/6 对抗用例全过；既有 forward_time 8 用例
  不变通过）。
- ⑤剩余风险：锚取 created_at 的上海日历日期（日粒度，与
  既有口径一致）；同一日内「先入库、声称当日稍早发现」的
  情形按同日锚处理，盘中粒度不进一步区分（bar 收盘完整性
  门禁另行兜底）。

## 回归（六项，原始日志见证据包 logs/）

1. 三项 P0 正反对抗：17 passed，exit=0。
2. fix3 原有专项（rule_binding/benchmark_integrity/
   adversarial_regression/forward_time）：33 passed，exit=0。
3. Market-First 全部（集成分支）：86 passed，exit=0。
4. G3/G5 排除统计（forward_stats 排除口径）：20 passed，exit=0。
5. 晋升/交接/统计边界：30 passed，exit=0。
6. 全量回归 + 基线对照：纯基线 936e4c6 为 294 passed /
   1 failed（271.26s）；集成后 311 passed / 1 failed
   （269.21s）。失败集逐项相同 =
   {test_e2e_batch2.py::test_ledger_fingerprint_idempotent}
   （既有基线失败），新增失败为零；+17 恰为新对抗用例数。
   未删测试、未降断言、未调 300 秒门槛。

## 合并结果

- 合并方式：merge commit（--no-ff），在 market-first
  worktree 内将集成分支并入 `market-first/unified-arch`。
- 合并前检查：market-first worktree 已跟踪文件干净（两份
  reports/e2e-batch2/*.json 为测试运行副产物、已 checkout
  恢复）；未跟踪的 evidence/.staging/ 与
  reports/heartbeat.jsonl 为既存副产物，未被合并触及、
  原样保留。
- 冲突与解决：零冲突（ort 策略一次完成）。合并带入 10 个
  文件（market_first/db.py、validation.py、3 份新测试、
  5 份适配测试），与 78009cf 的 fix3 交付文件无重叠。
- 合并后 HEAD：`086b23e48ec45f044feed9bc833b974f3c436e9c`
  （合并提交；其后另有本交付提交——台账更新 + 证据 +
  本文档，不含任何 .py 代码变更）。
- 合并后重跑：unified-arch@086b23e 上 Market-First 全部
  测试 86 passed，exit=0（logs/postmerge_market_first_all.log）。
- 台账：audit_issues.jsonl 更新——MF-REG-002、MF-FWD-002
  追加集成提交与测试引用（状态保持 READY_FOR_AUDIT）；
  新增 MF-REG-003（P0-3 批准信息）READY_FOR_AUDIT。
  CLOSED 未自标，待独立审计置位。
- 推送：按用户指令由主会话用凭证执行，本交付不含推送；
  推送目标 = unified-arch 最终 HEAD（见交付提交 SHA）。

## 数据库隔离

生产库 `~/workspace/paper-trading-sim/db/sim.db` 开工/收工
SHA256 均为
`c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f`，
逐字节一致；全部测试用 tmp 临时库，未导入生产数据
（详见证据包 db_isolation.md）。未改生产代码/交易逻辑/
B31/发布门禁，未用 force-bypass，未启用调度。
