# V3 独立审计补正交付（2026-11-23）

基线：unified-arch `28e730f`（代码冻结对象 bf2135b）。
分支：`market-first/v3-audit-correction-20261123`。
设计：`evidence/v3-audit-correction-20261123/design/DESIGN-V3-CORRECTION.md`。

## ① 检查对象

V3 冻结包独立审计三项补正：P0-1 RT-1 审批记录持续有效性、
P0-2 RT-2 审计记录唯一绑定（含口径修订）、P0-3 P0-5 新
事件 INSERT 时间锚。禁区（生产库/交易逻辑/B31/仓位/策略/
同步链）未触碰。

## ② 实际证据

**三项独立提交**：

| 项 | 提交 | 内容 | 新增测试 |
|---|---|---|---|
| P0-1 | `88cb4c8` | trg_rt_approvals_no_delete_live / no_update_keyfields_live：候选 approved 时其有效 approvals 记录禁删、禁改关键字段（candidate_id/decision/approver/basis/decided_at） | 12 用例（删除/整表删除/六种改写/转移/范围边界/decide 正对照+落字后受保护） |
| P0-2 | `2927750` | sample_exclusions.audit_history_id 列（ensure ALTER + SCHEMA_SQL）+ UNIQUE 索引；修订版 audit_insert 触发器（绑定必备/引用逐字对应/一对一，原字段校验保留为冗余）；被引用审计行禁改删两触发器；exclude_sample 落绑定 id | 11 用例（绑定正对照/无绑定拒绝/旧行复用/跨样本复用/事后删除/事后修改×4/未引用行边界） |
| P0-3 | `47f1a55`（部分实施） | trg_rt_discovery_events_created_at_insert_future：INSERT created_at 晚于服务器时间 120 秒拒绝；UPDATE 保护保留 | 5 用例（未来一天/远未来拒绝、当前时间/容差内通过、UPDATE 复核） |

**回归对照**（同命令同环境，原始日志在
`evidence/v3-audit-correction-20261123/logs/`）：
命令 `pytest tests/ -q --ignore=tests/test_gapfill.py
--ignore=tests/test_ui_regression.py`——
基线 28e730f：368 passed / 7 failed（exit 1）；
修复后：**396 passed / 7 failed**（+28 恰为新增用例）；
失败集逐项 diff **完全相同**（6 个 stress_test 环境性
失败 + 既有基线失败 test_ledger_fingerprint_idempotent），
**新增失败为零**。分组日志：p0_1_tests.log（26 passed，
含既有 rt1 三文件）、p0_2_tests.log（33 passed）、
p0_3_tests.log（28 passed），均为 -v 逐用例输出含退出码。

**口径修订（P0-2 第 5 项）落点**：设计文档（新增
DESIGN-V3-CORRECTION + DESIGN-BOTTOM-LAYER 补记）、
db.py 触发器消息与 ensure docstring、validation
.exclude_sample docstring、rt2_db_guard 测试 docstring、
台账 MF-VAL-001 残余（追加修订声明）、V4 GAPS。
统一表述为「不可变、一对一关联的审计证据」，并明示
同源性（同一事务/同一操作者）不在 DB 层证明范围内。

**既有测试构造迁移登记（断言零改动）**：仅一处——
tests/test_market_first_rt2_db_guard.py 直写辅助函数
改为显式传 audit_history_id（_direct_insert_audit
返回行 id、_direct_insert_exclusion 增参，两个用例的
调用点改传 id）；全部断言、预期与用例集合一字未动。
另：本批新文件 test_market_first_rt2_audit_binding.py
初版第二样本 entry_date 与 forward 样本相撞（唯一键），
已修正为 2026-11-17——属本批新测试自身构造修正，非
既有测试改动。

**台账**：MF-PROMO-002 / MF-VAL-001 / MF-FWD-002 追加
本批提交、测试与残余；19 行 READY 16 / OPEN 3 /
CLOSED 0，RT-1/RT-2 状态保持 READY_FOR_AUDIT，未自标
CLOSED。

**生产隔离**：生产库 sim.db SHA256 开工/收工两点均为
c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f。

## ③ 复核方法

每项五类测试（正常/缺参或无绑定拒绝/直写污染对抗/
回滚留痕/既有回归）；全量同命令双边对照 + 失败集程序
化 diff；触发器经 init_db 实际安装后由对抗测试以直写
SQL 验证；台账 JSON 逐行解析计数校验。

## ④ 结论

P0-1、P0-2 已完整落地并通过全量对照；P0-3 **部分实施**
（未来时间已封；NULL 与历史回填窗口因与既有夹具的真实
冲突未实施，见 ⑤），不伪报全过。本批状态
READY_FOR_AUDIT（V4 冻结包为准）。

## ⑤ 剩余风险

1. **P0-3 未实施部分（待审计裁定）**：NULL 拒绝与
   test_missing_ingest_time_rejects_forward 的被测前提
   （库内存在 NULL created_at 事件行）直接冲突；历史
   回填窗口与固定字符串时钟夹具结构性冲突（夹具时刻
   年龄随墙钟无限增长，任何有限窗口终将无代码变更地
   击穿夹具；且 DB 层无法区分夹具写入与直写写入）。
   可选方案 A 受控通道标记 / B 宽限窗口（400 天级，
   明示时限）/ C 维持现状（INSERT 只封未来 + UPDATE
   不可变 + validation 层锚校验），详见设计文档。
2. 全套一致伪造固有界限（先改出 approved 再删记录、
   先整套伪造审计行再写排除）在本批两闸下形态更新，
   已写入 V4 GAPS G-05；handoff 下游核验为第二道防线。
3. approvals.versions_json 未纳入关键字段保护（RT-6
   已登记 OPEN 单独评估，不在本批范围）。
4. 存量排除行（本线无生产存量）audit_history_id 为
   NULL，不受一对一约束追溯影响；新写入强制绑定。
