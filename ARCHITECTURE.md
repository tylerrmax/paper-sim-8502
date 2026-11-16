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
- **硬约束**：研究报告只有证据与分数；落库前递归扫描禁止键
  （side/action/order/buy/sell/instruction/qty/position_size），命中抛错——
  研究层在代码上无法产出交易指令。

### 3.4 validation（`market_first/validation.py`）

- 表 `validation_samples`：UNIQUE(candidate_id, source_id, entry_date) 防同键重复
  统计（冲突抛 `DuplicateSampleError`）；kind ∈ {forward, backfill} 严格区分，
  前向=发现后真实等待成熟，回填=历史时点演示/评估，两者永不混算。
- `evaluate_sample(pool, sample_id, bars)` 可复算：基准价为发现时点收盘；
  ret_tN = 入场后第 N 个交易日收盘/基准−1（N=5/10/20）；max_drawdown 以基准价为
  初始峰值对入场后 20 日收盘序列计算。成熟状态：≥20 个交易日 `mature`；
  不足 `immature`（可得的近端指标照实填、远端留 NULL）；无价格数据 `missing`
  且 missing_reason 单列。**缺失一律 NULL，绝不记 0。**
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
`market_data` 表，provenance 字符串如实标注来源。注：market_data 包自身带缓存
（其 cache.db 位于仓库外 `~/workspace/market_data/`，是生产管线同款行为）。

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
