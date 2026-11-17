# Market-First fix2 交付：fix1 独立验收前补正（三项）

- 分支：`market-first/unified-arch`（隔离 worktree，未合并、未推送、
  未部署、未启用调度、未接入生产）
- 基线：`13ff9e5`（fix1 证据提交）；代码提交：`7e4e7e3`；证据提交：
  `c7a9533` + `82e6898`。完整 diff（相对 13ff9e5）：`full_diff.patch`
  （覆盖代码/测试/文档/证据全部变更，仅不含 patch 文件自身；其中
  代码+测试+ARCHITECTURE 部分为 10 文件、+1086/−108）；关键文件
  修改前后源码：`src_before/`（13ff9e5 版）与 `src_after/`
  （7e4e7e3 版）并列存放 validation/research/promotion/db 四文件。
- 本文件为开发方交付与自检记录，**不构成独立验收结论**；结论词仅用
  「已验证通过 / 存在故障 / 缺乏证据 / 不适用」。

---

## 补正 1：Forward benchmark 收盘完整性确认

- **① 检查对象**：`validation.create_sample` 的 forward/backfill 两条
  建样路径、`evaluate_sample` 的 benchmark 使用、样本 1 与 exclusions
  记录的迁移安全性。
- **② 实际证据**：
  - 新增唯一判定入口 `bar_close_confirmed(bar_date, fetched_at,
    provenance=None, market=None)`（validation.py）：bar 日期严格早于
    取数上海日期 → 确认；同日 → 取数时刻 ≥ 收盘确认时刻（常量
    `CLOSE_CONFIRM_TIME`：SH/SZ/BJ/CB 15:30 = 15:00 收盘 + 30 分钟
    缓冲，HK 16:30）且 provenance 无 intraday 标记才确认；未来日 →
    不确认。检视 `~/workspace/market_data` 包：`service.py` 返回纯
    OHLCV Bar 元组、`quality.py` 仅有陈旧/量能告警，**无 bar 完整性
    或 intraday 标记**，故按指令以时间规则为准（依据边界如实记录）。
  - forward 推导出的 entry bar 未确认 → `_reject_forward` 留痕拒绝，
    reason 含 `benchmark_close_unconfirmed`；backfill 同日盘中 entry
    同样留痕拒绝（`ForwardRejectedError` 改为 `ValueError` 子类兼容
    既有捕获）。
  - benchmark 不可变：`evaluate_sample` 发现 entry 日收盘与已记
    benchmark 不一致时，只写样本行 `benchmark_revision_json`
    （COALESCE 只保留首次：旧值/新值/发现时间）+ candidate_history
    留痕，评估继续用原 benchmark（迁移新增列
    `benchmark_revision_json TEXT`，幂等 ALTER）。
  - 新增测试 7 项（tests/test_market_first_benchmark_integrity.py）：
    判定单元（历史日/未来日/盘中 10:30/15:30 整/收盘后 16:00/
    intraday 否决/港股 16:30）、盘中不完整 bar 拒绝+留痕、收盘后
    当日 bar 通过、历史补取通过、backfill 当日盘中拒绝、行情源
    修订记录不覆盖、对抗测试（见验收重点专节 1）。全过。
  - 样本 1 迁移前后对比（canonical_migration_and_registry_proof.txt）：
    迁移前快照 sample1_before.json 与迁移后原列逐字段全等 = True，
    exclusions 行全等 = True，新列为 NULL，未被触碰。
  - 独立核对（check_benchmark_independent.py，不依赖 create_sample，
    时间规则在脚本内独立重写）：规范库 2 样本 benchmark 与 entry 日
    收盘逐一相等（002623 26.25、000530 8.63）、created_at 时刻确认
    成立 → OVERALL OK；e2e 演示库同样 OK。
  - e2e 实跑 exit 0 / E2E RESULT: PASS（e2e_run.log）：judge 停在
    validating（not_evaluable/NO_RULE），未产生 validation_passed。
- **③ 复核方法**：新增单元/集成测试 + 独立脚本直读库复算 + 迁移前后
  dump 程序化对比 + grep 写入路径枚举（验收重点专节 1）。
- **④ 结论**：已验证通过（开发方自检，待独立验收）。
- **⑤ 剩余风险**：收盘确认以时间规则为准，源端在 15:30 后再修订
  当日 bar 的情形规则本身无法感知，靠 benchmark_revision_detected
  事后留痕兜底；provenance 的 intraday 否决为预留项（现行 provenance
  串均不含该标记，实际判定只走时间规则）。

## 补正 2：去重退化改 uncertain 保留（三态 mf-dedup-2.0）

- **① 检查对象**：`research.dedup_inst_events` 与 `InstFlowModule`
  的聚合与研究报告 evidence 披露。
- **② 实际证据**：
  - `dedup_inst_events` 改三态并返回 `(kept, dropped, uncertain_ids)`，
    规则版本 `DEDUP_VERSION = "mf-dedup-2.0"`：有 trade_id 且键
    (market, symbol, data_date, trade_id) 相同 → confirmed 只计一次；
    任一侧缺 trade_id → 一律不自动合并，全元组相同（或与有 id 事件
    全元组相同）标 uncertain、全部保留计入；其余 independent。
  - 研究报告 evidence 完整披露：`raw_event_ids`、
    `confirmed_duplicate_dropped_ids`（另保留 `dropped_duplicate_ids`
    别名）、`uncertain_event_ids`、`uncertain_events`（逐条
    {id, inst_net, dedup_status}）、`uncertain_inst_net_total`、
    `uncertain_note`（明示「身份不确定、未合并」）、`dedup_key`、
    `dedup_version`；factors 增 `uncertain_events_count`。
  - 含 uncertain 事件的研究报告 JSON 原文：
    `audit_focus_uncertain_report.json`（构造场景经真实 ResearchEngine
    产出：confirmed 对只计一次 100 + uncertain 对双计 40×2，
    total_inst_net = 180，uncertain_inst_net_total = 80）。
  - 独立复算（recompute_dedup_v2.py，不导入 research，独立实现三态
    口径）：演示库五项逐项 MATCH（kept/dropped/uncertain/total/
    version）；e2e 库候选 1（002623 真实双路事件）五项 MATCH
    （total 31,318,542.80 只计一次），候选 2/3 无研究报告记 SKIP。
  - 测试：fix1 的缺 id 用例按新口径重写（见「既有测试改动明示」3），
    新增三态直测 1 项；fix1 的 confirmed 用例（同 trade_id 只计
    一次、不同 trade_id/不同日期分别保留、池内原始事件不动）
    原样通过。
- **③ 复核方法**：独立复算脚本双库对照 + 报告 JSON 原文逐字核查 +
  测试断言报告 evidence 字段完整。
- **④ 结论**：已验证通过（开发方自检，待独立验收）。
- **⑤ 剩余风险**：uncertain 按「全部计入」是补正指令定的保守口径：
  若两条实为同一事件，净额将高估——报告已单列 uncertain 影响额
  （uncertain_inst_net_total）供人工判断，未静默处理；有 id 与无 id
  事件全元组相同时无 id 侧标 uncertain（有 id 侧照常计入），此为
  口径选择，已写入 DEDUP_KEY_SPEC。

## 补正 3：RULE_REGISTRY 注册保护

- **① 检查对象**：`validation.judge_validation` 的规则来源、
  `rule_registry` 落库表、`promotion._apply_validation_passed`
  受控流转、历史判定留痕。
- **② 实际证据**：
  - 新增落库表 `rule_registry`（rule_id, version, status ∈
    {draft, approved, disabled, test}, approved_by, approved_at,
    params_json, created_at，PK (rule_id, version)），`register_rule /
    set_rule_status / get_registered_rule` 三个治理函数；approved
    登记强制要求 approved_by。
  - judge 签名改为 `judge_validation(pool, candidate_id, rule_id=None,
    rule_version=None, actor=...)`，**rule 对象直传参数已删除**
    （传入触发 TypeError）；判定链：未登记/版本不符 →
    `rule_not_registered`；status≠approved → `rule_not_approved`；
    无代码实现 → `rule_implementation_missing`；均记
    not_evaluable 并写 validation_decisions 留痕。硬门槛（无
    forward/未成熟/NULL）仍在数值判定之前。
  - `_apply_validation_passed` 加固：必须携带 decision_id 并在函数内
    核验该 validation_decisions 记录真实存在、属该候选且
    decision='passed'，核验不过先留痕再抛 IllegalTransitionError。
  - 新增测试 10 项（tests/test_market_first_rule_registry.py）：未登记
    规则、伪造版本、直传对象 TypeError、缺 version、draft/test 状态
    拒绝、测试库 approved 正常通过、停用后 not_evaluable 且历史
    passed 行逐字段不变、版本变更 v1→v2 旧记录仍标 v1、受控流转
    直调（decision_id=None 与伪造 id）双路被拒+留痕、新库注册表
    为空。全过。
  - **规范库查询证明**（canonical_migration_and_registry_proof.txt）：
    `rule_registry` 总行数 0、approved 行数 0——本轮未登记任何
    approved 规则；规范库 validation_decisions 仍 0 行，未产生
    validation_passed。
- **③ 复核方法**：绕过测试组 + grep 入口枚举（验收重点专节 3）+
  规范库实查。
- **④ 结论**：已验证通过（开发方自检，待独立验收）。
- **⑤ 剩余风险**：`RULE_IMPLEMENTATIONS` 为进程内字典，与落库表
  是双轨（登记 approved 但无实现时 judge 安全地返回
  not_evaluable）；规则登记的治理纪律（谁可登记 approved）靠流程
  约束而非代码权限，本轮按指令以「规范库零 approved 行」实证
  收口。THRESHOLD_PROPOSAL 三案仍待用户确认，未确认前正式判定
  恒 not_evaluable。

---

## 验收重点专节（三条审查口径逐条对应）

1. **Benchmark 真不可变**：证据 `audit_focus_benchmark_writes.txt`
   ——grep 枚举证明 benchmark_price 的写入只有 create_sample 的
   INSERT 一处（validation.py:378-382）；唯一 UPDATE 语句的 SET
   列表不含 benchmark_price；evaluate_sample 只读；重复 create
   抛 DuplicateSampleError；迁移仅 ALTER ADD COLUMN；judge 不写
   validation_samples。对抗测试
   `test_benchmark_adversarial_immutable`：建样后以修订 bar 评估 +
   同键重复创建 + judge 三路施压，benchmark_price 与建样值全等。
2. **uncertain 报告完整披露**：证据 `audit_focus_uncertain_report.json`
   ——真实 ResearchEngine 产出的研究报告原文，evidence.dedup 内可
   直接读到 uncertain_event_ids=[3,4]、逐条 inst_net=40.0、
   uncertain_inst_net_total=80.0、「身份不确定、未合并」明示说明，
   及 confirmed dropped=[2]、total=180 的完整勾稽；测试
   `test_missing_trade_id_uncertain_kept_and_counted` 对报告
   evidence 的 uncertain 字段完整性逐项断言。
3. **RULE_REGISTRY 绕过面枚举**：证据 `audit_focus_judge_entries.txt`
   ——grep 枚举证明 validation_decisions 的 INSERT 仅 judge 一处
   （validation.py:830），进入 validation_passed 的唯一链为
   judge→_apply_validation_passed（唯一生产调用点
   validation.py:849）；受控流转已加 decision_id 核验，直调被拒
   并留痕；旧 rule= 直传仅剩一处负测试（断言 TypeError）；测试
   注册域（pytest tmp 库 + 进程内实现字典）与正式注册域
   （规范库落库表，实测 0 行）隔离方式与绕过测试逐条对应表均在
   该文件内。

## 既有测试改动明示（按新规则更新路径，断言未弱化）

1. `tests/test_market_first_validation_gate.py`：新增
   `_register_test_rule/_judge_with_test_rule` 辅助（测试库内登记
   approved 行 + 代码侧登记实现），4 处 judge 调用由
   `rule=_Rule()` 直传改注册表路径；全部原断言逐字保留（含
   `rule_version == "test-rule-0"`、basis 关键词、状态断言）。
2. `tests/test_market_first_promotion_handoff.py`：`_judge_pass` 同改
   注册表路径；`create_sample` 显式传
   `fetched_at="2026-11-17T16:00:00+08:00"`——该用例为模拟时钟
   （事件 2026-11-16 发现、forward entry 推导为 2026-11-17），
   fix2-1 完整性规则要求取数时刻与样本时间线一致，此为显式化
   模拟取数时刻，非放宽规则；原断言保留。
3. `tests/test_market_first_inst_dedup.py`：
   `test_missing_trade_id_falls_back_to_full_tuple` 按补正 2 新口径
   重写为 `test_missing_trade_id_uncertain_kept_and_counted`——
   预期由「去重为 2 条、净额 999、dropped 1 条」改为「3 条全
   保留、净额 1099（100+100+899）、confirmed dropped 0 条、
   uncertain 2 条」，并新增报告披露字段断言。此为指令口径变更
   （禁止缺 id 自动合并），非弱化；同文件其余 fix1 用例未改。
4. 其余既有测试（forward_time 8、validation round-1、
   discovery_pool、research、cache_isolation 等）未改动、原样通过。

## 测试与基线数字（原始日志见同目录）

- market_first 专项：**61 passed**（new_tests_output.txt；fix1 时
  43 项，本轮新增 18：benchmark_integrity 7 + rule_registry 10 +
  dedup 三态直测 1，另重写 1）。
- 全量 pytest（门禁口径五 ignore）：**277 collected，276 passed，
  1 failed，263.24s**（full_pytest_output.txt）。唯一失败
  `test_e2e_batch2.py::test_ledger_fingerprint_idempotent` 为既有
  基线失败：纯净 1ce2370 临时 worktree 复跑同失败
  （baseline_failure_rerun.txt，0.76s 单测复现），与本批无关。
- e2e：exit 0，E2E RESULT: PASS（e2e_run.log / e2e_run.stderr.log）。
- 生产与缓存前后对照（baseline_start.txt / baseline_end.txt /
  cache_window_before.txt / cache_window_after.txt）：
  - 生产 master HEAD：`1ce2370` → `1ce2370`（未变）；
  - 生产 sim.db sha256：`e1646ee9…` → `e1646ee9…`（未变），
    fills 51 / max_id 467（未变）；
  - 生产 cache.db sha256（受控窗口：专项测试正式跑、e2e、独立
    脚本与全量 pytest 均在窗口内）：`5b10f251…` → `5b10f251…`
    （未变；窗口起点值与 fix1 的 c2f718aa 不同，系生产自身夜间
    任务在两批之间写入，隔离口径为受控窗口前后对照）。
- 规范库：新增 rule_registry 表（0 行）+ benchmark_revision_json
  列（样本行均为 NULL）；样本 1 与 exclusions 原样未动（程序化
  对比 = True）。

## 未完成 / 风险项汇总

- 数值门槛仍未定版：THRESHOLD_PROPOSAL 三案待用户确认；未确认前
  judge 对任何候选恒 not_evaluable，系统不产生 validation_passed
  （本轮实证：规范库 decisions 0 行、e2e 停在 validating）。
- Promotion 存储层下沉保护（pool.set_fields 字段级封锁 + DB 层
  约束）与 handoff 的 BUY placeholder 改名，按 2026-11-16 独立
  复核裁定属**下一批必要加固**，本轮按指令未扩围。
- 全量 pytest 263.24s 接近 300s 门禁上限（既有观察项，本批未
  调阈值、未降标准）；后续若继续增长需另行处置。
- 本轮全部工作仅在隔离分支 `market-first/unified-arch`，停等
  独立审计；未合并、未推送、未部署、未启用调度。
