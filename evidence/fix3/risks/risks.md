# fix3 未解决问题与剩余风险

## 未关闭问题（台账状态以 audit_issues.jsonl 为准）
- MF-PROMO-001（P1，OPEN）：promotion 存储层下沉保护（pool.set_fields 字段级封锁）按独立审计裁定列为下一批必要加固，本轮不修、只登记。
- MF-HANDOFF-001（P1，OPEN）：handoff 的 BUY placeholder 语义待后续改名处理，本轮不修、只登记。
- fix1/fix2 已交付项（MF-FWD-001、MF-DEDUP-001、MF-DEDUP-002、MF-BENCH-001、MF-REG-001、MF-CACHE-001）与 fix3 四项（MF-FWD-002、MF-BENCH-002、MF-BENCH-003、MF-REG-002）均为 READY_FOR_AUDIT；CLOSED 只能由独立审计置位，开发方未自标。

## 剩余风险
- 晋升数值门槛（THRESHOLD_PROPOSAL 三案）仍待用户确认：未确认前规范库 rule_registry 无 approved 行、judge 恒 not_evaluable，系统无正式晋升通过能力（设计内结果，非故障）。
- 交易日历覆盖 2025–2026 年公开休市/半日市口径：表外年份回退 fix2 时间规则（兜底、非日历确认）；日历表为人工整理口径，若交易所临时调整休市安排（如台风半日市、临时休市）日历无法感知，收盘确认仍以「bar 日期严格早于取数日」为主判据、风险有限。
- 收盘确认的 provenance intraday 否决仍为预留项（行情包不提供完整性标记）；源端在确认时刻后修订当日 bar 靠 benchmark_revision_json 留痕兜底，benchmark 本身由触发器保证不被改写。
- forward 时间锚取 max(claimed, ingested)：若生产适配器未来出现「事件延迟入库」（抓取后隔日才 ingest），forward entry 将顺延至入库日之后——这是保守方向（宁可晚、不可冒充），但会减少可用 forward 样本，需在审计时知悉。
- 全量 pytest 耗时接近 300 秒门禁上限（既有观察项）：本轮未调阈值、未降标准；全量 1 项既有基线失败（test_ledger_fingerprint_idempotent）如实保留，纯净基线复跑同失败（见 fix2 证据 baseline_failure_rerun.txt，本轮失败项与之相同、已由 build_audit_package 与 tools/baseline_failures.txt 比对登记）。
- 生产窗口说明：fix3 快照窗口为 2026-11-17 12:02 CST 起，未跨越 17:12/17:30 生产任务时段；窗口内生产自身 15 分钟级同步任务若写入 sim.db/cache.db，其变化在 snapshot_diff 中单独可见、不得归因于隔离开发（归因见 DELIVERY-FIX3.md）。
- 工作区已知残留（reports/e2e-batch2 两份测试产物修改、未跟踪 heartbeat.jsonl）未纳入本轮任何提交。
