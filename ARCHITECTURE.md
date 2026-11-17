# 8502 Market-First 统一架构说明

版本：mf-arch-0.1.0（隔离分支 `market-first/unified-arch`，基线 `1ce2370`）
状态：最小端到端可运行版本已建成并实测；**未合并、未推送、未部署、未启用任何调度**。

## 1. 现状架构盘点（建设前）

- 发现入口单一：选股/信号链以母板 16 只（`config/strategy.yaml` 的 universe 9 只 +
  display_only 7 只）为全集，没有全市场主动发现层。
- 机构因子只有影子观察面：`engine/inst_lhb.py` 已具备全市场龙虎榜机构席位抓取
  （`fetch_inst_daily`）与落库（`inst_lhb_daily`），但其结果只进影子榜单展示，
  没有候选池、没有前向样本账、没有晋升通道（用户 2026-11-16 指出此为断头路）。
- 研究能力分散：`engine/screen.py` 的评分（v1.6）与形态因子只服务于母板 16 只，
  结果不落独立研究报告账，无法跨来源复用与追溯。
- 验证账本与交易账本同库：毕业统计在生产 `db/sim.db` 内按策略版本隔离，
  没有「发现时点→前向表现」的统一前向样本机制。
- 交易链（工作台待审批→信号/待建单→成交→持仓→复盘）本身完整且有门禁，
  本次建设**不改动**它，只在其上游新建主干并以模拟交接对接。

## 2. 目标架构

```
① Market Discovery ──DiscoveryEvent──▶ ② Unified Candidate Pool
   (可插拔适配器)                        (market,symbol) 归并、多来源事件追加
                                              │
                                              ▼
⑤ Promotion & Approval ◀──状态机── ③ Research Engine ──▶ ④ Validation Engine
   (人工审批唯一入口)                  (分项证据+分数)        (前向/回填样本)
        │
        ▼
⑥ Trading Handoff（本次仅模拟交接：写新库 handoffs，status=simulated）
        ┄┄┄ 未来经单独批准后 ┄┄┄▶ 现有安全交易链（生产 approvals/signals/账本）
```

每个阶段保留状态、证据、版本与来源；未通过验证或审批不得越级进入交易链
（由状态机转移表 + handoff 代码级拦截双重强制，并有测试覆盖）。

## 3. 六模块数据模型与接口契约

新系统使用**独立 SQLite 库** `market_first/market_first.db`（已加入 .gitignore），
与生产 `db/sim.db` 完全隔离；对生产库只有只读（URI `mode=ro`）读取。

### 3.1 discovery（`market_first/discovery.py`）

- 适配器统一输出 `DiscoveryEvent`（`models.py`）：market、symbol、name、source_id、
  discovered_at、trigger_condition、price_at_discovery、price_data_time、evidence(dict)。
- `InstLhbAdapter`：首个真实来源。优先实时调用既有 `engine.inst_lhb.fetch_inst_daily`
  （source_id=`inst_lhb_live`）；实时不可用时回退只读生产库已落库的 `inst_lhb_daily`
  真实行（source_id=`inst_lhb_stored`），evidence 内 `obtained_via` 如实标注。
  触发条件：`inst_net>0`（机构专用席位净买入为正）。
- **覆盖声明（写死在代码 COVERAGE 常量并进入每条 evidence）**：覆盖范围为当日触发
  龙虎榜披露的股票，不是沪深京全部上市公司，**不得称全市场扫描**。
- `classify_market(symbol)`：6→SH、0/3→SZ、92/83/87/43→BJ、11/12/13→CB（可转债）、HK→HK。
- 新增来源只需实现同一 `discover(date_str, price_fn=None)` 形态并注册 source_id，
  候选池与下游零改动。

### 3.2 pool（`market_first/pool.py` + `db.py`）

表：`candidates`（UNIQUE(market, symbol)；first/last_seen_at；research_status、
validation_status、promotion_status、risk_status 四个状态面）、`discovery_events`
（追加制，一次发现一行，多来源历史不覆盖）、`candidate_history`（append-only
状态变更史）。接口：`ingest(event)->(candidate_id, created)`、`get/get_by_id/
list_candidates/events_for/history_for`、`set_fields`（五字段白名单，逐字段写史）、
`record_history`。候选池独立于母板 16 只；母板名单仅在 `models.MOTHERBOARD_SYMBOLS`
中作为「非母板」判定参照，不构成发现范围限制。

### 3.3 research（`market_first/research.py`）

- 模块接口：`compute(candidate, bars, context) -> {factors, subtotal, evidence}`，
  可插拔增减；引擎 `ResearchEngine.run(candidate_id, bars, data_asof, data_version)`
  逐模块计算后落 `research_reports` 一行（engine_version、computed_at、data_asof、
  data_version、factors_json 分项、total_score、evidence_json）。
- 已实现模块：`trend_pattern`（mf-trend-1.0，复用 `engine/screen.py:pattern_factors`
  的形态因子分 + 本模块 MA 趋势分，规则写入 docstring；bars<30 时 subtotal=None
  不充数）、`inst_flow`（mf-inst-1.0，基于候选的机构发现事件证据计分）。
- **机构事件去重（fix1/P0-2，fix2-2 改三态 mf-dedup-2.0）**：inst_flow
  聚合前经 `dedup_inst_events` 分类——有 trade_id 且键 (market, symbol,
  data_date, trade_id) 相同 = confirmed duplicate，只贡献一次净额；
  trade_id 缺失时**一律不自动合并**：全元组相同标
  `dedup_status='uncertain'`，全部保留计入，并在研究报告 evidence 中
  完整披露（uncertain_event_ids、逐条 inst_net、合计影响额、明示
  说明）；其余为 independent 分别保留。禁止按 symbol 单独去重。
  候选池原始事件全量保留不动；evidence 记录 raw/deduped/
  confirmed_dropped/uncertain 明细与 dedup_version，保证可复算。
- **硬约束**：研究报告只有证据与分数；落库前递归扫描禁止键
  （side/action/order/buy/sell/instruction/qty/position_size），命中抛错——
  研究层在代码上无法产出交易指令。

### 3.4 validation（`market_first/validation.py`）

- 表 `validation_samples`：UNIQUE(candidate_id, source_id, entry_date) 防同键重复
  统计（冲突抛 `DuplicateSampleError`）；kind ∈ {forward, backfill} 严格区分，
  前向=发现后真实等待成熟，回填=历史时点演示/评估，两者永不混算。
- **Forward 时间规则（fix1/P0-1，代码强制）**：发现事件实际创建时间
  `discovery_events.discovered_at` 即数据实际可得时间；forward 的 entry 必须是
  其 Asia/Shanghai 日历日期之后第一个交易日（以 bars 实际存在的交易日为准），
  benchmark = entry 日收盘。entry ≤ 发现创建日期的样本由代码强制改类
  `backfill`，不信任调用方 kind；无法确认下一交易日或其收盘价时拒绝创建并在
  candidate_history 留痕（`forward_rejected: ...`）后抛 `ForwardRejectedError`。
  样本行记录 `discovery_event_id / benchmark_date / benchmark_price_source`，
  评估回写 `bars_asof / data_version`（另保留 evaluated_at / eval_version）。
- `evaluate_sample(pool, sample_id, bars)` 可复算：ret_tN = 入场后第 N 个交易日
  收盘/基准−1（N=5/10/20）；max_drawdown 以基准价为初始峰值对入场后 20 日收盘
  序列计算。成熟状态：≥20 个交易日 `mature`；不足 `immature`（可得的近端指标
  照实填、远端留 NULL）；无价格数据 `missing` 且 missing_reason 单列。
  **缺失一律 NULL，绝不记 0。**
- 审计排除表 `sample_exclusions`：被裁定样本（如原样本 1）只登记排除、
  原行不改，`forward_stats` 与 judge 判定均排除之。
- **验证通过门禁（fix1/P0-3）**：`validation_passed` 只能由
  `judge_validation()` 按版本化规则产生并写 `validation_decisions` 记录；
  阈值未定版，规则注册表为空时 judge 一律 `not_evaluable`
  （no_approved_rule）。门槛方案见 `THRESHOLD_PROPOSAL.md`（待用户确认）。
- **Benchmark 收盘完整性（fix2-1）**：benchmark 只能取通过
  `bar_close_confirmed()` 确认的收盘——bar 日期严格早于取数日天然确认；
  当日 bar 须取数时刻已过收盘确认时刻（A股/北交所/可转债 15:30、港股
  16:30）且 provenance 无 intraday 标记；未来日 bar 不确认。不确认则
  留痕拒绝（reason 含 `benchmark_close_unconfirmed`）。market_data 包
  不提供 bar 完整性标记（仅 OHLCV），故以时间规则为准。benchmark 一经
  记录不可变：评估时发现 entry 日收盘被行情源修订，只记
  `benchmark_revision_json` + candidate_history，绝不覆盖原值。
- **规则注册保护（fix2-3）**：正式 judge 只接受 `rule_id+rule_version`
  并从落库表 `rule_registry` 加载，规则对象直传参数已删除；
  status != 'approved'（draft/test/disabled）一律 `not_evaluable`；
  注册须留 approved_by/approved_at，注册表变更不改写历史判定。
  promotion 受控流转 `_apply_validation_passed` 必须携带并核验真实
  passed 判定记录（decision_id），绕过 judge 的直接调用被拒并留痕。
- **Forward 时间锚信任路径（fix3/MF-FWD-002）**：`create_sample` 的
  发现时间锚 = max(调用方声称的 `discovered_at` 日期, 库内服务器侧
  `discovery_events.created_at` 的日期)。`discovered_at` 由调用方经
  DiscoveryEvent 传入、可回填伪造；`created_at` 由 `pool.ingest`
  入库时以服务器时间落字，事件内容无法回填。两者不一致时在
  candidate_history 留痕（field='discovery_time_anchor'）。回填声称
  不能把 backfill 变成 forward（对抗测试 A3）。
- **Benchmark 数据库层不可变（fix3/MF-BENCH-002）**：触发器
  `trg_validation_samples_benchmark_immutable` /
  `trg_validation_samples_no_delete`（`ensure_fix3_schema` 幂等
  安装）：benchmark 三字段值变更的 UPDATE 与样本行 DELETE 在
  数据库层 RAISE(ABORT)，直接 SQL 亦不可改（对抗测试 A1）。
  evaluate 回写不触碰 benchmark 三列，不受影响。
- **收盘确认日历化（fix3/MF-BENCH-003）**：`bar_close_confirmed`
  优先查 `market_first/calendar.py`（2025–2026 休市/半日市表，
  来源为国务院与交易所公开安排；分市场时区 Asia/Shanghai /
  Asia/Hong_Kong；港股半日市确认时刻 12:30）：非交易日 bar 一律
  无效。日历未知（市场未知/年份未覆盖）回退 fix2 固定时刻规则
  并注明为兜底、非日历确认。
- **Registry 精确绑定（fix3/MF-REG-002）**：代码侧实现按
  (rule_id, version) 登记并自报 impl_key/impl_version；
  `rule_registry` 落库条目同样保存 impl_key/impl_version。
  judge 双边核对：实现缺失 -> `NO_IMPL`、绑定错配 ->
  `IMPL_MISMATCH`，均 `not_evaluable` 留痕。
  `validation_decisions` 同时保存 `rule_id` 与 `rule_version`；
  `reproduce_decision()` 按记录版本取实现复现历史判定（只读，
  注册表变更后仍可复现，原记录不改写）。
- 预留 `delisted` 状态值但本次未实现退市检测（见剩余风险），不伪造。

### 3.5 promotion（`market_first/promotion.py`）

状态机（代码内显式 TRANSITIONS 表强制）：

```
discovered → pending_research → researching → validating
validating → validation_passed / validation_failed
validation_failed → pending_research（回炉）
validation_passed → pending_approval → approved / rejected（终态）
```

- `transition()` 只走转移表；非法转移先在 candidate_history 留痕
  （field='promotion_transition_attempt'）再抛 `IllegalTransitionError`。
- **approved/rejected 只能经 `decide()` 人工审批进入**：公开 transition() 对这两个
  终态一律拒绝（留痕+抛错），decide() 内部走私有路径并同事务写 `approvals`
  （审批人、时间、对象、依据 basis、versions_json）。即：验证通过≠自动批准，
  代码上不存在绕过人工审批的路径。
- **validation_passed 只能经验证引擎判定进入（fix1/P0-3）**：公开 transition()
  对 validation_passed 同样一律拒绝（留痕+抛错）；唯一入口是
  `validation.judge_validation` 判定 passed 后调用内部
  `_apply_validation_passed`（要求当前状态为 validating）。

### 3.6 handoff（`market_first/handoff.py`）

- `build_payload()` 按现有工作台待审批形态组装：{date, account(None 占位),
  symbol, market, side('BUY' 意图占位，intent_placeholder=True), decision,
  source_trace{candidate_id, discovery_event_ids, research_report_ids,
  validation_sample_ids, approval_id}, status='simulated', note}。
- `simulate_handoff()` 代码级拦截：promotion_status≠approved 或无 approved 审批行
  → 抛 `HandoffNotApprovedError`；成功只写新库 `handoffs` 表且 status 恒为
  'simulated'。**绝不写生产 approvals/signals/trade_intents**（e2e 只读核对了
  生产三表行数不变）。

### 3.7 行情通路（`market_first/bars.py`）

`get_bars(symbol, start, end) -> (bars, provenance)`：优先复用生产同款
`market_data` 包（腾讯/新浪，`get_bars_as_tuples`），失败回退只读生产 sim.db
`market_data` 表，provenance 字符串如实标注来源。
**缓存隔离（fix1/P0-4）**：调行情包恒传 `use_cache=False`，绝不读写生产共享
`~/workspace/market_data/cache.db`（不修改行情包任何文件/默认配置）；研发自有
缓存为 market_first 库内 `fetched_bars` + `fetched_coverage` 表，取数成功写入
自有表、同日重跑可命中自有缓存（provenance='market_first fetched_bars
cache'）。机构发现的 live 抓取（`engine.inst_lhb.fetch_inst_daily`）只走 HTTP
抓龙虎榜报表，不经行情包缓存路径。

## 4. 复用与迁移清单

| 复用项 | 方式 | 冲突与最小迁移 |
|---|---|---|
| `engine/inst_lhb.fetch_inst_daily` | discovery 实时路径只读导入调用 | 无冲突；导入失败自动回退 stored 路径并留痕 |
| 生产 `inst_lhb_daily` 已落库行 | discovery stored 路径 ro 读取 | 无迁移；行数/日期在证据中标注 |
| `engine/screen.pattern_factors` | research trend 模块只读导入调用 | 其 points 口径为「待验证因子」，在报告中按原口径呈现，不改其定义 |
| `market_data` 包 `get_bars_as_tuples` | bars.py 主通路（与 run_daily 同款） | 该包在仓库外、无 commit 锚定（既有 R4 缺口），本次只读使用并标注 provenance |
| 生产 `approvals`/`trade_intents` 字段形态 | handoff payload 映射目标（仅形态） | 不写生产表；真实写入适配留待单独批准后的阶段 |
| 既有交易链/门禁/账本 | 完全不改 | 无迁移 |

未复用：机构影子样本账不直接充当统一验证引擎（按指令要求先审查）——其数据经
discovery 适配器以事件形式重新入池、由新 validation 引擎独立建样本，口径自洽。

## 5. 生产安全证明（基线前后对照）

基线采集于开工前（2026-11-16，只读），交付时同法复测，结果见 DELIVERY.md 与
`evidence/production_baseline_after.txt`：

| 项目 | 开工前 | 交付复测 |
|---|---|---|
| 生产 master HEAD | 1ce237026c0ff7a0871f22bd2d7d4872dbc0d6a9 | 1ce237026c0ff7a0871f22bd2d7d4872dbc0d6a9（一致） |
| db/sim.db sha256 | 4b6e512022abe5a836b8077cbb6cad93e105afaba5ebb081b0ca41fab703ee41 | 4b6e512022abe5a836b8077cbb6cad93e105afaba5ebb081b0ca41fab703ee41（逐字一致） |
| fills 行数 / max_id | 51 / 467 | 51 / 467（一致） |
| market_data 最大日期 / 行数 | 2026-11-14 / 3509 | 2026-11-14 / 3509（一致） |
| 生产 approvals / signals / trade_intents 行数 | 21 / 22 / 0（e2e 内只读） | 21 / 22 / 0（一致） |

注：sim.db 为活库，17:12 夜跑等生产调度会在其正常节律内写入；对照口径为
「本分支工作未对其产生任何写入」（全部接触均为 mode=ro 只读连接，e2e 前后
sha256 逐字一致已在子任务实测中验证一次，交付时再复测）。
