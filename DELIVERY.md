# Market-First 统一架构｜隔离交付报告

日期：2026-11-16。分支：`market-first/unified-arch`（基线 `1ce2370`），
worktree：`~/workspace/l2-remediation/market-first`。
权限边界：仅隔离分支设计/开发/测试/交付；**未合并、未推送、未部署、未启用调度、
未启用自动交易**。完成后停等独立审计与单独发布批准。

## 验收逐项（五项格式）

### 1. 架构完整性
- 检查对象：六大模块、数据模型、接口契约、状态流转、依赖关系。
- 实际证据：新包 `market_first/` 共 11 个模块文件 1790 行（models/db/discovery/
  pool/bars/research/validation/promotion/handoff/run_e2e/__init__）；数据模型与
  接口契约见 ARCHITECTURE.md §3；状态机为代码内显式 TRANSITIONS 表。
- 复核方法：逐文件审查 + 全量导入冒烟 + e2e 全程实跑。
- 结论：**已验证通过**。
- 剩余风险：接口为 0.1.0 首版，后续新增来源/因子时可能需要小幅演进（已按适配器
  形态预留）。

### 2. 主动发现（真实来源、不依赖母板 16 只）
- 检查对象：InstLhbAdapter 对 2026-11-13 的发现运行。
- 实际证据：e2e 实跑经**实时通路** `engine.inst_lhb.fetch_inst_daily` 取得当日
  37 只机构上榜聚合、过滤 inst_net>0 后 11 个发现事件（obtained_via=live 如实
  标注）；主标的 002623 亚玛顿（SZ，inst_net 31,318,542.80，发现价 26.25 =
  当日收盘）不在母板 17 只名单内。覆盖声明写死于代码并进入每条 evidence：
  龙虎榜口径≠全市场扫描。原始输出 `evidence/e2e_run.log`（118 行）。
- 复核方法：协调者本人复跑 run_e2e（exit=0，E2E RESULT: PASS）；stored 回退路径
  以 fixture 库单测 + 真实生产库 2026-10-16 只读实测（18 事件）双重验证。
- 结论：**已验证通过**。
- 剩余风险：实时通路依赖东财接口可用性；失败时回退 stored 已落库行（数据日期
  会滞后于当日，evidence 内如实标注 obtained_via 与 data_date）。

### 3. 统一候选池（归并正确、来源不丢失）
- 检查对象：CandidatePool.ingest 的 (market, symbol) 归并与事件追加。
- 实际证据：e2e 中同一标的经 live 与 stored 两路 ingest → candidate_id 同为 1、
  created 先 True 后 False、events_for=2 行且 source_id 分别为 inst_lhb_live /
  inst_lhb_stored；单测覆盖三次 ingest（含重复来源）事件行数=3 不覆盖、
  history 逐条留痕。
- 复核方法：pytest `test_market_first_discovery_pool.py` 6 项全过 + e2e 输出 §c。
- 结论：**已验证通过**。
- 剩余风险：无已知。

### 4. 研究与验证（样本可复核）
- 检查对象：ResearchEngine 分项报告；ValidationEngine 前向/回填样本。
- 实际证据：002623 研究报告 id=1：trend_pattern subtotal=9.0（pattern_points=4.0
  + trend_points=5.0，MA60 因 bars 仅 43 根不可用已如实标注）、inst_flow
  subtotal=8.0、total=17.0，含 engine/model 版本与 data_version（provenance）。
  前向样本 id=1（entry 2026-11-13，benchmark 26.25）：immature、四指标 None
  （未成熟如实，不记 0）。回填样本 id=2（000530 冰山冷热，entry 2026-10-21，
  benchmark 8.63，kind=backfill 与前向分开）：ret_t5=−0.1333、ret_t10=+0.0962、
  ret_t20=None（入场后 17 根 bar 不足 20）、max_drawdown=−0.2151，均为真实
  行情复算值。
- 复核方法：单测以已知合成序列手算对照（定值序列 ret_t5/t10/t20 全等 0.10、
  drawdown 两组 0.0 与 −0.1）全过；研究/验证子任务自检另有 0.10/0.20/0.30
  递增序列一组对照通过；缺失/未成熟/重复入账三类各有专门用例。
- 结论：**已验证通过**。
- 剩余风险：①validation 的 `delisted` 状态未实现检测（预留值，不伪造）；
  ②前向样本须随真实时间成熟，本次只能证明机制正确、不能证明样本已成熟；
  ③研究 bars 经 market_data 包取得，其个别标的量能单位警告（包自带
  DataQualityWarning）会如实出现在日志中，未做二次清洗。

### 5. 审批闭环（未审批不得进交易链）
- 检查对象：promotion 状态机与 handoff 拦截。
- 实际证据：e2e §g 三连——pending_approval 下 simulate_handoff 被拒
  （HandoffNotApprovedError）；transition 直达 approved 被拒且
  candidate_history 留痕 1 条；decide(approver='e2e_operator') 后交接成功，
  payload 追溯链 candidate=1 / discovery=[1,2] / research=[1] / validation=[1]
  / approval=1 / handoff=1 完整。§h 拒绝演示：601566 decide=rejected 后交接
  同样被拒。单测另覆盖 validation_passed≠自动批准、decide 非待审批态调用
  被拒且不落 approvals、终态无出边。
- 复核方法：pytest `test_market_first_promotion_handoff.py` 8 项全过 + e2e 输出。
- 结论：**已验证通过**。
- 剩余风险：审批人为字符串标识（本次为模拟交接定位），真实身份体系留待
  交易链真实接入阶段设计。

### 6. 端到端运行
- 检查对象：`market_first/run_e2e.py` 全流程。
- 实际证据：协调者复跑 exit=0、118 行输出、末行 `E2E RESULT: PASS`，
  全文存 `evidence/e2e_run.log`；覆盖发现→双来源入池→研究→前向样本→
  回填演示→待审批→未批交接被拒→非法转移被拒→人工批准→模拟交接成功→
  另一标的人工拒绝→交接被拒→生产只读自检全链。
- 复核方法：子任务首跑与协调者复跑两次结果一致。
- 结论：**已验证通过**。
- 剩余风险：e2e 的 validation_passed 流转发生在前向样本 immature 状态下——
  脚本内已明示该口径仅为流程演示、不代表达到任何晋升/毕业门槛；真实晋升
  判据（样本成熟度门槛）须在后续阶段单独定版，本次未擅自设定。

### 7. 生产安全（未改变生产交易链与数据库）
- 检查对象：生产 master、db/sim.db、生产三表（approvals/signals/trade_intents）。
- 实际证据：开工前/交付后两次只读采集逐项一致（HEAD、sim.db sha256
  `4b6e51…ee41`、fills 51/467、market_data 3509 行 max 2026-11-14、三表
  21/22/0），见 `evidence/production_baseline_before.txt` 与
  `production_baseline_after.txt`；e2e 运行中对生产库全部接触为 mode=ro，
  其 §i 自检与外部复测互相印证。本分支全部改动为新增文件 + .gitignore
  追加 3 行，未改任何既有代码文件（git diff --stat 对基线可查）。
- 复核方法：sha256 与行数双口径复测 + git 状态审查。
- 结论：**已验证通过**。
- 剩余风险：market_data 包在取数时会写其仓库外缓存 cache.db（生产管线同款
  行为，非生产仓与 sim.db），如实登记。

### 8. 回归测试
- 检查对象：新增测试 + 全量门禁口径 pytest（release_gate.default_pytest_cmd
  的 5 个 --ignore，worktree 内以生产 .venv 解释器运行）。
- 实际证据：新增 23 项全过（单独运行 23 passed in 0.43s，
  `evidence/new_tests_output.txt`）；全量 239 collected = 216 既有 + 23 新增，
  **238 passed, 1 failed in 253.63s**（`evidence/full_pytest_output.txt`）。
  唯一失败 `test_e2e_batch2.py::test_ledger_fingerprint_idempotent` 经纯净
  1ce2370 对照复跑证实为**既有失败**：该测试按生产活库 51 笔 fills 的基线
  JSON 比对仓库提交版 sim.db 快照（50 笔），与本分支改动无关
  （`evidence/baseline_failure_check.txt`）。未删改任何既有测试。
- 复核方法：纯净基线 worktree 单独复跑该用例，结果逐字一致。
- 结论：**存在故障**（仅指该既有基线口径失败；本分支新增部分与既有通过集
  零回归）。
- 剩余风险：该既有失败的根治（基线 JSON 与提交版 DB 快照口径对齐）属
  REPRODUCIBILITY/GOVERNANCE 议题，不在本分支授权内，未擅自处理。

## 已知问题与后续阶段

已知问题：
1. 既有测试基线口径失败 1 项（见 §8），非本分支引入。
2. validation `delisted` 检测未实现；promotion 判据（样本成熟门槛）未定版，
  当前状态机只强制流程与审批、不内嵌数值门槛。
3. discovery 目前仅 1 个真实来源（机构龙虎榜）；板块轮动/技术突破/基本面
  适配器未接入（接口已预留）。
4. research 目前 2 个模块（趋势形态、机构流向）；板块环境、量能资金、
  基本面因子未接入。
5. handoff 为模拟交接；与工作台待审批的真实写入适配未做（按授权边界）。

后续阶段及依赖（均须单独批准）：
- 阶段二（数据扩展）：新增发现适配器（板块轮动、技术突破扫描）与研究因子，
  依赖行情覆盖口径先定版。
- 阶段三（验证判据）：晋升数值门槛（成熟样本量、收益/回撤线）定版后写入
  promotion 判据层；前向样本随真实交易日自然成熟后复核。
- 阶段四（真实交接）：handoff 真实写入适配 + 身份/审批留痕强化，须与交易链
  批次流程合并审批，不得由本分支直接延伸。
- 发布：本分支合并/推送/部署/调度启用均未授权，停等独立审计与单独批准。
