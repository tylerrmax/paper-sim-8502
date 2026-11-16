# Market-First 第一轮审计问题修复（fix1）交付报告

基线：`638cc180e0980b418790eb183914a43a1d526c59`（分支
`market-first/unified-arch`，原隔离 worktree、原独立数据库）。
性质：定点修复，不重建架构、不新增发现源、不扩展研究因子；
未合并、未推送、未部署、未接入生产。**本报告为开发方交付，
结论待独立审计复核，不以本报告代替独立验收。**

修改文件清单（完整 diff 见提交记录与 `evidence/fix1/`）：
- 代码：`market_first/validation.py`、`market_first/promotion.py`、
  `market_first/research.py`、`market_first/bars.py`、
  `market_first/db.py`、`market_first/run_e2e.py`
- 既有测试改动：`tests/test_market_first_promotion_handoff.py`
  ——仅 walk 路径改走 judge 合法路径（P0-3 强制新规矩使
  transition 直达 validation_passed 成为非法），全部原断言
  保留未弱化；其余既有测试文件未动。
- 新增测试：`tests/test_market_first_forward_time.py`（8）、
  `tests/test_market_first_inst_dedup.py`（5）、
  `tests/test_market_first_validation_gate.py`（6）、
  `tests/test_market_first_cache_isolation.py`（1），共 20 项。
- 文档：`ARCHITECTURE.md`（口径更新）、`THRESHOLD_PROPOSAL.md`
  （门槛方案，未定版）、本报告。
- 证据：`evidence/fix1/`（基线、窗口、e2e、复算、测试原始输出）。

---

## P0-1 Forward 时间规则

- 检查对象：`validation.create_sample / evaluate_sample`、
  规范库样本 1、新增测试 8 项、e2e 演示 1/2。
- 实际证据：entry 推导 = discovered_at 的 Asia/Shanghai 日历
  日期之后第一个 bars 交易日，benchmark = 该日收盘并落
  `benchmark_date / benchmark_price_source / discovery_event_id`；
  评估回写 `bars_asof / data_version`（schema 迁移幂等 ALTER，
  只加可空列）。样本 1：迁移与排除登记前后原始列逐字段全等
  （`evidence/fix1/sample1_before_row.txt`、
  `sample1_after_row_and_exclusion.txt`）；`sample_exclusions`
  已登记 excluded_from='forward_stats'（reason 注明审计裁定），
  `forward_stats` 排除后 total=0、excluded_sample_ids=[1]。
  e2e 实跑：规范 forward 请求因下一交易日不可确认被拒并在
  candidate_history 留 `forward_rejected:` 痕；以历史 entry
  请求 forward 被强制改类 backfill 落库（`e2e_run.log` §e）。
- 复核方法：新增单测覆盖盘后/盘中/周末发现、强制改类、历史
  补取、价格缺失拒绝+留痕、无发现事件拒绝、排除后统计与
  原行不变；e2e 在演示库实跑（规范库不触碰）。
- 结论：已验证通过（待独立审计复核）。
- 剩余风险：forward 推导依赖调用方传入或 bars 层现取的 bars
  完整性；若行情源对下一交易日给出未收盘的不完整 bar，
  benchmark 可能取到盘中价——当前以「bars 中存在该日 bar」
  为可确认标准，未另设收盘确认标志。

## P0-2 机构事件重复计数

- 检查对象：`research.dedup_inst_events` 与 InstFlowModule
  聚合、规范/演示库真实双路事件、新增测试 5 项。
- 实际证据：去重键 =(market, symbol, data_date, trade_id)，
  trade_id 缺失退化为全元组全等；候选池事件全量保留。
  真实数据（002623 亚玛顿，trade_id=100444236 的 live/stored
  双路事件）：修复前 total_inst_net 双计 62,637,085.6，修复后
  31,318,542.80 只计一次（e2e §d 输出 raw=[1,2]、
  deduped=[1]、dropped=[2]）。研究报告 evidence 落
  raw/deduped/dropped id 与 dedup_key 明细。
- 复核方法：独立复算脚本 `evidence/fix1/recompute_dedup.py`
  （不导入 research.py，直读库按审计口径手工去重汇总）与
  报告逐候选比对：MATCH（`recompute_dedup_output.txt`）；
  单测覆盖双路同事件、不同日期、不同 trade_id、缺 id 退化、
  重跑可复算。
- 结论：已验证通过（待独立审计复核）。
- 剩余风险：trade_id 由上游龙虎榜数据提供，若上游对同一
  底层事件给出不同 trade_id，双路仍会各计一次（口径上视为
  独立事件，与审计口径一致）；全元组退化键依赖 evidence
  字段齐全，字段缺失的事件按 None 元组比较，可能把字段
  同缺的两条不同事件误判为同一——当前真实数据字段齐全，
  未触发。

## P0-3 验证通过门禁与门槛未定版

- 检查对象：`promotion.transition`、`validation.
  judge_validation`、`validation_decisions` 表、新增测试 6 项、
  `THRESHOLD_PROPOSAL.md`。
- 实际证据：transition() 对 validation_passed 直达与
  approved/rejected 同级拒绝（留痕+抛错）；judge 写判定记录
  （rule_version、sample_scope、metrics、decision、basis）；
  无规则时一律 not_evaluable / NO_RULE；硬门槛前置拦截
  （无 forward、未成熟、指标 NULL、仅 backfill 均不可通过，
  被排除样本不计入）。e2e §g/§h 实跑：judge 输出
  not_evaluable、状态停在 validating、decide 与 handoff 均被拒。
  门槛未定版：注册表为空，未把任何候选阈值写进生效代码；
  方案文档给出 A（机构门同款）/B（毕业标准映射）/C（分级门）
  三案与影响分析——当前成熟 forward 样本 = 0，三案均无可
  判定样本；最早可判定时间约 21 个交易日后（样本累积速率
  敏感，详见文档）。
- 复核方法：单测覆盖直达拒绝、无规则不通过、backfill-only
  不通过、未成熟不通过、成熟+显式测试规则通过并流转、排除
  样本不计入；e2e 实跑复核。
- 结论：门禁机制已验证通过（待独立审计复核）；门槛数值
  本身缺乏证据——未定版，待用户确认，本文不宣称具备正式
  晋升通过能力。
- 剩余风险：judge 的 `rule` 参数为接口预留，调用方显式传入
  未登记规则对象即可触发数值判定（测试即如此）；生产路径
  （e2e/未来调度）均以 rule=None 调用。规则登记纪律需在
  门槛确认时一并定版（建议：只有写入 RULE_REGISTRY 的
  version 才允许被生产调用）。

## P0-4 研发缓存隔离

- 检查对象：`market_first/bars.py`、生产共享
  `~/workspace/market_data/cache.db`、研发自有 fetched_bars。
- 实际证据：bars 调行情包恒传 `use_cache=False`（已核
  service.py：该开关下既不读也不写其 cache.db）；未改行情
  包任何文件。取数写入自有 `fetched_bars`/`fetched_coverage`
  （演示库实测 155 行 / 11 标的），同日重跑命中自有缓存且
  provenance 可区分。受控窗口（e2e + 全部 market_first
  测试）前后生产 cache.db SHA256 同为
  `c2f718aa023093147c3bc3b076d641aac9f60095fd8461ff85ac59a6
  0e0f45f4`，与开工基线一致；全量 pytest 后复测仍不变
  （`evidence/fix1/cache_window_before/after.txt`、
  `baseline_start/end.txt`）。机构 live 抓取
  `fetch_inst_daily` 只走 HTTP 抓龙虎榜报表（另读生产库
  market_data 表作交易日历，经传入连接），不经行情包缓存
  路径。
- 复核方法：断言测试在 get_bars 调用前后实时测生产缓存
  SHA 并断言不变、自有表行数增长、二次命中自有缓存；
  批次级窗口对照复核。
- 结论：已验证通过（待独立审计复核）。
- 剩余风险：自有缓存覆盖判定以 fetched_coverage 登记的
  请求区间为准；若行情源事后修订历史 bar，自有缓存不会
  自动失效（与生产缓存同类问题），需人工清表重取。

## 测试与生产对照（汇总）

- 检查对象：新增测试、全量 pytest、纯净基线、生产基线。
- 实际证据：market_first 测试 43 项全过（原 23 + 新 20）；
  全量门禁口径（5 个 --ignore）259 collected、**258 passed /
  1 failed**（246.27s，`full_pytest_output.txt`）。唯一失败
  `test_e2e_batch2.py::test_ledger_fingerprint_idempotent`
  为既有基线失败：已在纯净 1ce2370 独立 worktree 复跑同
  一失败（1 failed in 0.43s，`baseline_failure_rerun.txt`），
  与 round-1 的 `baseline_failure_proof.log` 一致，未隐藏、
  未删改。全量运行再生的 `reports/e2e-batch2/*.json` 产物
  已恢复至提交版，不计入本批 diff。
- 生产对照：master HEAD 仍 `1ce2370…`；sim.db SHA256 批次
  前后同为 `e1646ee9…`，fills 51 / max_id 467 不变；
  生产 cache.db SHA256 全程 `c2f718aa…` 不变。
- 结论：存在故障（仅既有基线 1 项，非本批引入）；本批新增
  与 market_first 全部测试已验证通过（待独立审计复核）。
- 剩余风险：既有基线失败的根因（基线 JSON 按活库 51 笔 vs
  提交版 DB 快照 50 笔口径差）不在本批范围，留待基线治理
  批次处理。

## 完成条件核对

- Forward 时间规则已由代码强制执行 ✔
- 原样本完整保留且不进入 Forward 统计 ✔
- 机构事件重复计数已消除（研究聚合层） ✔
- 未成熟样本不能进入验证通过状态 ✔
- 研发缓存与生产缓存完全隔离 ✔
- 新增测试全部通过 ✔（20/20，market_first 合计 43/43）
- 全量测试结果及既有失败如实披露 ✔（258/1，失败为既有基线）
- 没有生产业务数据和交易逻辑变更 ✔（基线前后对照为据）

Promotion 存储层保护与 BUY placeholder 改名按指令保留为
后续独立加固任务，本轮未动。
