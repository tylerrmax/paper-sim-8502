# 自红队报告：同类模式全量排查（冻结对象 c490424）

方法：按三类模式逐类枚举 market_first/ 全部模块的入口
（grep 全量 + 逐函数人工核对），每项给出判定；存疑与确认项用
临时库探针实测（探针在本目录：probe_redteam.py、
probe_research_data_asof.py，原始输出 probe_results.txt、
probe_research_data_asof_results.txt；另引用 03/04 节探针）。
本报告只记录、不修复；确认的新问题不进 audit_issues.jsonl
（台账原样收录于 06_ledger/），由主会话汇总后决定修复批次。

全局注：本库无认证层，actor/approver/decided_by 等身份字段全仓
均为调用方自报字符串——这是单人本地系统的既定信任模型。红队
口径只查「特权动作是否强制必填且非空、时刻是否服务器生成、
声称值是否作资格/统计依据」，不把自报性本身逐项立项。

## 模式① 关键值可被调用方覆盖（枚举 8 入口）

| # | 入口 | 可覆盖点 | 判定 |
|---|---|---|---|
| 1-1 | validation.register_rule_implementation(version=) | 登记键 | 安全（判定面）：impl_version 恒自报值、judge 双边核对；登记期错配可构造属已知残余，第 02 节成文待裁定 |
| 1-2 | validation.register_rule(approved_by/approved_at/…) | 批准字段 | 安全：approved 路径全强制非空、approved_at 服务器覆盖（第 03 节） |
| 1-3 | validation.create_sample(entry_date/kind/benchmark_price) | 入场与资格 | 安全（P0-5 后）：显式 entry 受锚分类、forward benchmark 由 bars 派生且 DB 不可变 |
| 1-4 | validation.evaluate_sample(bars/eval_version/data_version) | 评估输入 | **存疑 RT-5**（探针 C6） |
| 1-5 | research.ResearchEngine.run(data_asof/data_version) | 报告元数据 | **存疑 RT-4**（补充探针） |
| 1-6 | pool.ingest(event, actor) | 事件声称值 | 安全（锚面）：created_at 服务器生成；声称值去向见模式③ |
| 1-7 | validation.judge_validation(rule_id) | 判定对象 | 安全：精确绑定 + 判定记录核验（MF-REG-001/002 口径） |
| 1-8 | promotion.decide(versions=) | 版本留痕 | **存疑 RT-6 附带**：versions dict 调用方可任意传入并落 approvals.versions_json；属留痕字段、非判定依据，但与版本绑定口径并置时值得注意 |

## 模式② 状态流转无来源要求（枚举 10 入口）

| # | 入口 | 来源要求 | 判定 |
|---|---|---|---|
| 2-1 | register_rule →approved | approved_by+reason+basis 全强制 | 安全 |
| 2-2 | set_rule_status →approved | actor+reason+basis 全强制、同事务留痕 | 安全 |
| 2-3 | set_rule_status →非 approved | actor/reason 有缺省值、恒写留痕 | 安全（docstring 明示的兼容口径，非特权流转） |
| 2-4 | promotion.decide | approver/basis 均可空 | **确认问题 RT-1**（探针 C1/C2） |
| 2-5 | promotion.transition（普通流转） | actor 必填、reason 可空 | **存疑 RT-6**（探针 C8）；特权态 approved/validation_passed 被状态机单独封死、直达拒绝 |
| 2-6 | promotion._apply_validation_passed | decision_id 须与 DB passed 记录核验 | 安全（正面样本） |
| 2-7 | pool.set_fields（四类 status 字段） | 仅自报 actor、reason 可空 | **确认问题（已登记 MF-PROMO-001，OPEN）**（探针 C3 附新鲜证据与影响面收窄） |
| 2-8 | validation.exclude_sample | reason/decided_by 必填形参但可空字符串 | **确认问题 RT-2**（探针 C4） |
| 2-9 | ResearchEngine.run → research_status | actor 硬编码 'research'、计算后落字 | 安全 |
| 2-10 | handoff.build_payload/simulate_handoff | 无状态写入口；simulate 以 approvals 表为闸 | 安全 |

## 模式③ 时间信任（枚举 8 入口）

| # | 入口 | 声称时间的用途 | 判定 |
|---|---|---|---|
| 3-1 | validation.create_sample | 锚已改服务器 created_at（P0-5） | 安全 |
| 3-2 | research.InstFlowModule.compute | 以声称 discovered_at 选「最新」事件定因子符号 | **确认问题 RT-3**（探针 C5） |
| 3-3 | research.dedup_inst_events | data_date（evidence/price_data_time/discovered_at[:10]）参与去重键 | 口径说明：mf-dedup-2.0 已定版规则、data_date 为数据日期语义、三态全披露；不立项，供审计判断 |
| 3-4 | candidates.first_seen_at/last_seen_at | =discovered_at，但全仓无读取方 | 安全（纯留痕） |
| 3-5 | bars 日期（create/evaluate） | 行情日期语义；create 路径有 bar_close_confirmed 与 benchmark 不可变兜底 | 安全（evaluate 面并入 RT-5） |
| 3-6 | discovery.InstLhbAdapter | 产生 discovered_at 声称值（数据源语义本身） | 安全（下游锚已改服务器时间） |
| 3-7 | research.TrendPatternModule | bars 日期做形态计算（行情语义） | 安全 |
| 3-8 | run_e2e.py / snapshot_tool.py | 消费 forward_stats 作展示 | 安全 |

---

## 确认的新问题（3 项，只报不修）

### RT-1 promotion.decide 批准信息无强制（P0-3 同类，晋升审批层）
- 现象：`promotion.decide(pool, cid, approver, decision, basis="")`
  （promotion.py:164）对 approver 与 basis 均无非空校验：basis
  缺省即空、approver 传空字符串亦可完成 approved 终态流转并落
  approvals 行。
- 复现（探针 C1/C2，probe_results.txt）：候选置 pending_approval
  后，decide(approver='probe-approver', decision='approved') 不传
  basis → 成功，approvals.basis=''；decide(approver='',
  decision='approved', basis='probe basis') → 成功，
  approvals.approver=''。
- 证据：probe_redteam.py C1/C2 段 + probe_results.txt 对应行。
- 影响面：候选晋升的人工批准（进入 approved 的唯一合法入口）
  可无批准依据、无批准人落字——与 rule 侧 P0-3 的强制口径不
  一致；approvals 表是 handoff 闸与审计追溯的依据，空字段直接
  削弱追溯。decided_at 为服务器时间、状态机前置状态校验
  （pending_approval）仍在，故影响限于来源信息面。
- 建议修复口径：decide 内对 approver、basis 做 strip 非空校验
  （与 set_rule_status approved 分支同口径），缺失抛 ValueError
  且整事务回滚；补对抗测试（空 approver / 空 basis / 空白 basis）。

### RT-2 validation.exclude_sample 来源字段可空（P0-3 同类，统计排除面）
- 现象：`exclude_sample(pool, sample_id, excluded_from, reason,
  decided_by)`（validation.py:674）形参虽必填，但不校验非空：
  reason=''、decided_by='' 可成功写入 sample_exclusions。
- 复现（探针 C4）：对 forward 样本调用
  exclude_sample(..., reason='', decided_by='') → 成功，落库行
  两字段均为空字符串。
- 证据：probe_redteam.py C4 段 + probe_results.txt。
- 影响面：排除记录直接改变 forward_stats 与 judge 的样本构成
  （forward_consumers.md），空来源使「谁、为何排除」不可追溯；
  且与本仓 P0-3 已确立的「空白即无依据」标准自相矛盾（rule 侧
  空白 basis 被拒绝、样本排除侧被接受）。
- 建议修复口径：exclude_sample 内对 reason、decided_by 做 strip
  非空校验，缺失抛 ValueError；补对抗测试（空/空白两字段）。

### RT-3 research.InstFlowModule 以声称时间选最新事件（P0-5 同类，研究层）
- 现象：compute 以 `when = str(ev.discovered_at or ev.created_at)`
  （research.py:335–336）对事件排序取「最新」，subtotal 符号由
  最新事件的 inst_net 正负决定（±8.0）。discovered_at 是来源
  声称值：把任一事件的声称日期改到未来，即可使其恒为「最新」。
- 复现（探针 C5）：真实事件（声称 2026-11-10、inst_net +500）
  与伪造事件（声称 2027-01-05、inst_net −900、入库时间更晚亦
  不影响）→ subtotal = −8.0；把伪造事件声称日期改到
  2026-11-09 → subtotal 回到 +8.0。因子符号完全由声称日期控制。
- 证据：probe_redteam.py C5 段 + probe_results.txt。
- 影响面：限于研究层因子分（research_reports 的 inst_flow
  subtotal 与 total_score、research_status 流程的证据面）；
  不进入 forward_stats、judge 判定、晋升状态机或 handoff 闸。
  但按「声称时间只许留痕、不许作依据」的定版口径，这是同类
  模式在研究层的未收口实例。
- 建议修复口径：排序键改用服务器入库时间 created_at（缺失时
  以 id 序兜底），discovered_at 只留痕；补对抗测试（未来声称
  日期不得改变最新事件选择）。

## 已登记问题的新鲜实证（1 项，非新增）

### MF-PROMO-001 pool.set_fields 通用写原语（台账 OPEN）
- 探针 C3：set_fields(actor='probe', promotion_status='approved')
  直写成功，候选状态即刻为 approved，绕过 promotion 状态机与
  decide 的全部前置校验（与台账原描述一致，本轮附可复现证据）。
- 影响面收窄事实（本轮新查）：handoff.simulate_handoff 不认
  promotion_status 字段、只认 approvals 表——字段翻转后交接仍
  被 HandoffNotApprovedError 拒绝。故该原语的现行影响面是
  「状态字段本身与按 promotion_status 过滤/展示的消费方」，
  未穿透到交接闸。此收窄不改变其 OPEN 定性（存储层无封锁的
  事实不变），供主会话排修复批次时参考。

## 存疑项（3 项，附探针、请审计裁定）

### RT-4 ResearchEngine.run 的 data_asof / data_version 由调用方自报
- 补充探针实测：run(cid, [], '2020-01-01', 'probe-version') →
  research_reports 原样落 data_asof='2020-01-01'、
  data_version='probe-version'（computed_at 为服务器时间，
  见 probe_research_data_asof_results.txt）。
- 定性存疑：两字段是报告元数据、不作资格/统计依据；但若未来
  有消费方按 data_asof 判断报告新鲜度，声称值即可误导。建议
  口径：data_asof 由 bars 实际最大日期推导、data_version 取
  引擎登记值，调用方值只留痕。

### RT-5 evaluate_sample 的 bars 输入面
- 探针 C6：对 forward 样本传入全量伪造 bars（entry 后价格 ×2）
  → 样本被回写 maturity_status='mature'、max_drawdown=0.0、
  bars_asof='2026-12-18'。函数自身不校验 bars 来源/provenance。
- 定性存疑：bars 是该函数的声明数据输入，生产路径经
  market_first.bars 取数（自有 fetched_bars 表 + use_cache
  隔离）并在 create 路径有 bar_close_confirmed；且 benchmark
  不可变触发器保证入场基准不被评估覆盖、修订只留痕。但评估
  结果（成熟度/收益）进入 forward_stats 聚合，函数层零校验
  与 P0-5 收口后的 create 面相比明显较弱，是否补 provenance
  校验（如要求 bars 与 fetched_bars 对账）请审计裁定。

### RT-6 普通流转 reason 可空 + decide 的 versions 自报
- 探针 C8：promotion.transition 不带 reason 流转成功，
  candidate_history 的 reason 落 NULL（actor 恒记录）。非特权
  流转的 reason 缺失只影响追溯完整性，不影响状态合法性
  （转移表强制、非法流转留痕后抛错）。
- decide 的 versions 参数可任意自报落 approvals.versions_json
  （模式① 1-8）。两者同属「留痕完整性」面，合并登记为低优先
  存疑，建议口径：reason 在流转时缺省回填状态机版本标识、
  versions 缺省值之外的值须来自登记表——是否实施请审计裁定。

## 探针清单（本节 + 引用节）

- 本节：C1、C2、C3、C4、C5、C6、C8 共 7 项 + data_asof 补充
  1 项 = 8 项（probe_redteam.py / probe_research_data_asof.py）。
- 引用：03 节 A1–A4（4 项）、04 节 B1–B2（2 项），同为本轮
  临时库实测，结论在各节 probe_conclusions.md。
