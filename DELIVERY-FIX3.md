# Market-First fix3 交付报告（效率升级指令 V1.0 首轮）

分支 market-first/unified-arch（隔离 worktree），基线 1a178c5（fix2 交付）。
提交链：643b91c（P0 修复+台账+快照工具+对抗测试库）→ c582659（审计包生成工具）→ 936e4c6（打包工具 sys.path 修复）→ 本证据提交。
审计包：`evidence/fix3/`（由 tools/build_audit_package.py 自动生成，STATUS=READY_FOR_AUDIT；包内 INDEX/commits/diff/前后源码/tests/database/production/security/risks/checksums 齐备）。
边界：未合并、未推送、未部署、未接入生产、未启用调度、未改交易逻辑。以下结论为开发方自检口径，最终结论以独立审计五项格式为准。

## 基建 1：问题台账 audit_issues.jsonl
- 检查对象：worktree 根 `audit_issues.jsonl`（12 项，字段含 issue_id/first_found_in/module/severity/description/repro/fix_commits/tests/status/audit_verdict/residual_risk）。
- 实际证据：包内 risks/audit_issues.jsonl 同版；状态分布 READY_FOR_AUDIT 10（fix1/fix2 六项 + fix3 四项）、OPEN 2（MF-PROMO-001、MF-HANDOFF-001，只登记不修）；CLOSED 0——开发方未自标任何 CLOSED。
- 复核方法：逐行 json.loads 解析；状态枚举仅四值。
- 结论：已验证通过
- 剩余风险：台账 rounds/审计耗时类指标待审计轮次积累后才有意义，本轮 metrics 只报可自证项。

## 基建 2：文件级快照机制（本轮窗口即启用）
- 检查对象：`market_first/snapshot_tool.py`（sqlite3 Backup API 一致性冻结 + sha256 + 逐表行数/内容摘要 + diff）；fix3 窗口 2026-11-17 12:02–12:13 CST，未跨 17:12/17:30 生产时段。
- 实际证据：包内 production/before|after（sim、cache 各一份文件本体+manifest）；sim.db 前后 sha 同为 22cd0901…、cache.db 同为 5ceccab2…，snapshot_diff.json 显示两库 changed_tables 均为空——窗口内生产文件零变化，无需归因划分。database/before|after 为规范库迁移前后：changed_tables 为空、仅新增两条触发器（见 P0-A）。
- 复核方法：打包工具对每份快照重算文件 sha 与 manifest 比对（不一致即 NOT_READY，本轮全等）；diff 由 snapshot_tool 独立于业务代码计算。
- 结论：已验证通过
- 剩余风险：快照为窗口两点对照，窗口之外的生产变化不在本证据范围（口径即如此）。

## 基建 3：审计包自动生成工具
- 检查对象：`tools/build_audit_package.py` + `tools/baseline_failures.txt`（已知基线失败清单，变更须明示——本轮新建，内容仅 test_ledger_fingerprint_idempotent 一项）。
- 实际证据：本轮包由该工具生成，STATUS.txt=READY_FOR_AUDIT、metrics.json 证据完整率 8/8、未登记失败 0；完整性门禁含必需文件、pytest 汇总行（防截断）、未登记失败比对、快照哈希、checksums 自检五类，任一不过只能 NOT_READY。
- 复核方法：工具运行原始输出与包内 STATUS/metrics/test_summary.json 互核；checksums.txt 覆盖除自身外全部文件。
- 结论：已验证通过
- 剩余风险：门禁规则为本轮首版，审计方如认为必需项清单需增减，下一轮按裁定调整工具（不影响本轮包内容真实性）。

## 基建 4：常驻对抗测试库
- 检查对象：`tests/test_adversarial_regression.py`（文件头含 A/B/C/D 全清单映射：新测与既有测试引用逐项标注）。
- 实际证据：新增实测项 A1（直接 SQL 改/删 benchmark）、A3（回填声称冒充 forward）、A5（日历五测）、C7（validation_passed 全称追溯）、D（隔离与快照自洽）共 10 项全过；B/C 其余项引用既有测试名、不重复造。
- 复核方法：专项日志 tests/special_pytest.log（79 passed = 既有 61 + 新增 18，含 rule_binding 8 项）。
- 结论：已验证通过
- 剩余风险：D1/D3 的生产口径靠窗口快照证据（非单元测试），已在文件头注明口径。

## P0-A-1：Benchmark 数据库层不可变（MF-BENCH-002）
- 检查对象：`market_first/db.py::ensure_fix3_schema` 新增触发器 trg_validation_samples_benchmark_immutable / trg_validation_samples_no_delete；规范库迁移。
- 实际证据：迁移前后规范库 validation_samples 全量 dump 逐行全等（2 行，样本 1 benchmark 仍 26.25）、sample_exclusions 全等（1 行）、rule_registry 仍 0 行；触发器在规范库实测直接 SQL UPDATE benchmark_price 与 DELETE 均 RAISE(ABORT) 且原值不变（交付前实测输出已验）；包内 database/snapshot_diff.json 显示迁移仅新增触发器、零表内容变化。
- 复核方法：对抗测试 A1 用独立 sqlite3 连接直改必须抛错且 evaluate 正常回写不受影响；security/audit_focus_benchmark_writes_fix3.txt 给出 benchmark 写入路径枚举（INSERT 仅 create_sample 一处）。
- 结论：已验证通过
- 剩余风险：触发器只拦值变更与 DELETE；同值 UPDATE 放行（无害）。规范库文件本身未入 git（.gitignore），其状态以包内文件级快照为证。

## P0-A-2：Forward 时间门禁信任路径（MF-FWD-002）
- 检查对象：`validation.create_sample` 的时间锚来源。
- 实际证据：锚改为 max(声称 discovered_at 日期, 库内服务器侧 ingested_at=discovery_events.created_at 日期)，不一致时 candidate_history 留痕 field='discovery_time_anchor'（security/audit_focus_anchor_fix3.txt 枚举两侧落字点：pool.ingest 以服务器时间写 created_at，事件内容不可回填）；对抗测试 A3 双路实证：声称 2026-11-10 发现、今日入库的事件，(a) 声称日后 entry 被强制 backfill，(b) 按声称推导 forward 被留痕拒绝。
- 复核方法：A3 测试 + 既有 forward_time/benchmark_integrity 全套回归（61→79 专项全过）。
- 既有测试改动明示（断言零改动）：5 个既有测试文件的事件构造 helper 各加一段对齐——把事件行 created_at 对齐到模拟发现时刻，模拟生产不变量「发现即入库」（适配器 discovered_at=抓取时刻 ≈ ingest 落字时刻）；test_market_first_rule_registry 的 _register 改为登记与版本自洽的实现（P0-B 新口径下 v2 判定必须有 v2 实现，原 setup 正是错配形态），其版本变更测试另对第二个候选做同样对齐。全部断言未删改、未弱化、未跳过。
- 结论：已验证通过
- 剩余风险：若生产未来出现事件延迟入库，forward entry 保守顺延（宁晚不冒充），样本量影响见 risks.md。

## P0-A-3：交易日历与收盘确认（MF-BENCH-003）
- 检查对象：新增 `market_first/calendar.py`（2025–2026 A股/北交所休市表、港股休市与半日市表，分市场时区 Asia/Shanghai / Asia/Hong_Kong，文件头注明来源与边界）；`bar_close_confirmed` 改为日历优先、未知回退 fix2 规则。
- 实际证据：对抗测试 A5 五项：国庆/清明休市 bar 不确认、周末 bar 不确认、港股半日市（2026-09-25 中秋当日）11:00 不确认/12:30 确认、跨市场差异日（2025-04-18 港股休市 A股交易；2026-09-25 A股休市港股半日市）双向断言、2027 未覆盖年份回退时间规则。
- 复核方法：A5 测试 + fix2 的 benchmark_integrity 时间规则测试全套回归通过（日历路径与旧规则在既有场景下结论一致）。
- 结论：已验证通过
- 剩余风险：日历为人工整理的公开口径，交易所临时休市（如台风）无法感知；2027 年起需续表，未续时自动回退时间规则（兜底口径已注明）。

## P0-B：Registry 精确绑定（MF-REG-002）
- 检查对象：`validation.py` 实现注册表与 judge；`rule_registry` 新增 impl_key/impl_version 列；`validation_decisions` 新增 rule_id 列（ensure_fix3_schema 幂等迁移，规范库历史 0 行无影响）。
- 实际证据：新增 tests/test_market_first_rule_binding.py 8 项全过——注册表 impl 绑定落字、登记 v2/实现只有 v1 -> NO_IMPL、实现 v2 冒充 v1 -> IMPL_MISMATCH、注册表 impl_key 不符 -> IMPL_MISMATCH、精确绑定可 passed 且判定记录同时含 rule_id 与 rule_version、历史判定复现（v1 停用+v2 登记后 reproduce_decision 仍按 v1 实现复现 passed 且原记录逐字不变）、not_evaluable 记录复现不适用、实现注销后复现报 NO_IMPL。security/audit_focus_judge_entries_fix3.txt 枚举：validation_decisions 的 INSERT 仅 judge 一处、_apply_validation_passed 唯一生产调用点在 judge。
- 复核方法：binding 测试 + fix2 rule_registry 10 项与 validation_gate 测试回归全过；规范库查询 rule_registry 0 行、approved 0 行（迁移后复查）。
- 结论：已验证通过
- 剩余风险：实现注册表为进程内登记，正式规则的登记纪律（谁可登记 approved）随门槛定版时一并裁定；本轮规范库仍无 approved 规则、judge 恒 not_evaluable。

## 取证升级与全量测试（如实保留基线失败）
- 检查对象：全量 pytest（门禁口径，5 个 --ignore 同 release_gate.default_pytest_cmd）。
- 实际证据：tests/full_pytest.log——**294 passed / 1 failed，249.70s**；唯一失败 test_ledger_fingerprint_idempotent 为既有基线失败（纯净 1ce2370 复跑同失败，见 fix2 证据），已登记于 tools/baseline_failures.txt，未改写为通过、未删改该测试。专项 79 passed（special_pytest.log）；E2E exit 0、E2E RESULT: PASS（e2e.log；判定停在 validating、not_evaluable/NO_RULE）。生产只读复核：仓 HEAD 1ce2370、sim.db fills 51/max_id 467（e2e 只读输出与窗口快照一致）。
- 复核方法：打包工具解析全量日志汇总行与 FAILED 项并与基线清单比对，未登记失败为 0。
- 结论：已验证通过（按「基线失败如实保留」口径；全量不得表述为全过）
- 剩余风险：全量耗时 249.70s 接近 300s 门禁上限（既有观察项，未调阈值）。

## 台账状态汇总
READY_FOR_AUDIT 10 项（MF-FWD-001/002、MF-DEDUP-001/002、MF-BENCH-001/002/003、MF-REG-001/002、MF-CACHE-001）；OPEN 2 项（MF-PROMO-001、MF-HANDOFF-001，本轮按裁定只登记）；CLOSED 0 项（待独立审计置位）。

停止，等待独立审计。不合并、不推送、不部署、不启用调度。
