# DELIVERY：P0-3 完整落地——方案 A 受控通道标记（2026-11-23）

基线：unified-arch `61b1d98`（代码冻结对象 610128d）。
隔离分支：`market-first/p0-3-channel-a-20261123`
（worktree `~/workspace/l2-remediation/p0-3-channel-a`）。
授权：用户 2026-11-23 15:13「全部批准、继续进入下一阶段」
——V4 GAPS G-06 三方案中定案方案 A（受控通道标记，唯一
与时间无关的长期修复），不再等审计裁定。

## ① 检查对象

V4 GAPS G-06 登记的 P0-3 未实施部分：discovery_events
直接 INSERT 的 created_at 为 NULL 或历史回填时无 DB 层
校验。V3 补正批已实施未来时间拒绝（>120 秒）；NULL 与
回填因与可信时钟夹具的结构性冲突（夹具固定字符串历史
时刻年龄随墙钟无限增长；缺锚用例前提需库内存在 NULL
行；触发器无法区分夹具写入与直写写入）未实施。本批以
方案 A 收口：按写序而非时间值区分合法写入与直写。

## ② 实际证据

**修复提交**：`c0a062c`（独立提交）

- schema（db.ensure_redteam_schema 幂等迁移 + SCHEMA_SQL
  同步）：新表 `ingest_channel_marks(id, candidate_id,
  mark UNIQUE, created_at)`；discovery_events 新增
  `ingest_mark_id` 列与一对一 UNIQUE 索引（存量行 NULL
  不受约束）。
- 触发器 `trg_rt_discovery_events_insert_channel`：
  无有效标记的 INSERT，created_at 为 NULL / 不可解析 /
  早于服务器时间 120 秒以上 -> RAISE(ABORT)；声称标记
  但不存在、跨候选或已被消费 -> RAISE(ABORT)；带有效
  标记的 INSERT 不受时间值限制。未来时间拒绝沿既有
  insert_future 触发器（对全部 INSERT 生效）保留。
- 标记不可变：`trg_rt_ingest_channel_marks_no_update`
  （append-only）、`trg_rt_ingest_channel_marks_
  no_delete_bound`（被引用禁删；未引用可删，范围边界
  如实登记）。
- pool.ingest 受控通道：同事务先落标记行（mark=uuid4、
  标记行 created_at 为真实服务器时间），事件行以
  ingest_mark_id 绑定，末尾单次 commit 不变。
- 设计文档：evidence/p0-3-channel-a-20261123/design/
  DESIGN-P0-3-CHANNEL-A.md（含标记存储选型理由：专用
  表 + ID 绑定与 P0-2 的 audit_history_id 模式同构，
  不污染 candidate_history 审计语义）。

**新增对抗测试 12 用例**（tests/test_market_first_
p0_5_channel_marks.py，分组 -v 日志逐用例名/结果/退出
码见 evidence 日志 p0_3_group_tests.log，组总 40
passed、exit 0）：

| 用例 | 结果 |
|---|---|
| test_direct_insert_null_created_at_without_mark_blocked | PASSED |
| test_direct_insert_backfilled_created_at_without_mark_blocked | PASSED |
| test_direct_insert_unparseable_created_at_without_mark_blocked | PASSED |
| test_direct_insert_recent_past_within_tolerance_passes | PASSED |
| test_channel_ingest_binds_mark_one_to_one | PASSED |
| test_channel_ingest_null_created_at_premise_constructible | PASSED |
| test_consumed_mark_reuse_blocked | PASSED |
| test_dangling_mark_reference_blocked | PASSED |
| test_cross_candidate_mark_reference_blocked | PASSED |
| test_forged_mark_full_set_bypass_measured | PASSED（实测放行，见 ⑤） |
| test_bound_mark_update_and_delete_blocked | PASSED |
| test_unbound_mark_delete_allowed_scope_boundary | PASSED |

未来时间直写拒绝由既有 test_market_first_p0_5_insert_
anchor.py 覆盖（本批引用不重复），该文件本批全过。

**夹具迁移登记（断言零改动）**：

- 唯一生产改动点为 pool.ingest 写序；全仓 25 处
  pool.clock 夹具赋值（19 个测试文件）均经 ingest 落
  字，通道内化后自动带标记——测试调用点零改动、用例
  集合不变、断言零改动。
- test_missing_ingest_time_rejects_forward 构造不变：
  clock 返回 None 时 ingest 经通道落 NULL created_at
  事件行（带标记），前提继续成立，本批全量实测通过；
  新增 test_channel_ingest_null_created_at_premise_
  constructible 为前提可构造性的直接证据。
- 既有测试文件仅两处文档更新（无逻辑/断言改动）：
  test_market_first_p0_5_insert_anchor.py 与
  test_market_first_p0_5_created_at_guard.py 的模块
  docstring 残余段落更新为方案 A 落地后的实际状态。

**回归对照**（同命令同环境：`pytest tests/ -q
--ignore=tests/test_gapfill.py --ignore=tests/
test_ui_regression.py`，解释器为 paper-trading-sim
.venv 的 pytest 9.1.1）：

- 基线（纯 61b1d98）：396 passed / 7 failed，exit 1；
- 修复后：408 passed / 7 failed（+12 恰为新增用例），
  exit 1；失败集与基线逐项 diff **完全相同、新增失败
  为零**（基线失败 = 6 个 stress_test 环境性失败 +
  既有基线失败 test_ledger_fingerprint_idempotent，
  环境性失败单独登记、不伪报 PASS）。

**合并与复跑**：交付提交 `b307477`（fix 分支）后
--no-ff 零冲突合并入 unified-arch，合并提交
`2cf2234`；合并后按登记口径复跑 Market-First 全组
（tests/test_market_first_*.py +
tests/test_adversarial_regression.py）：**189 passed**
（V4 同口径 177 + 新增 12），exit 0，日志见 V5 包
04 节。

**生产隔离**：生产库 sim.db SHA256 开工/收工两点均为
c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f，
逐字节一致；全程隔离临时库。

**台账**：MF-FWD-002 追加本批提交与残余更新，状态保持
READY_FOR_AUDIT，未自标 CLOSED。

## ③ 复核方法

修复前后同命令同环境全量对照 + 失败集程序化 diff；
新增用例分组 -v 逐项留名/结果/退出码；触发器经 init_db
实际安装后以直写 SQL 对抗验证（含伪造实测）；合并后按
V2 起登记口径独立复跑 Market-First 全组；冻结包 ZIP
完整性 + SHA256SUMS 复算 + bundle verify 三重校验；
台账 JSON 逐行解析计数。

## ④ 结论

P0-3 完整落地：discovery_events 直接 INSERT 的 NULL、
历史回填、不可解析 created_at 已由受控通道标记闸收口，
未来时间拒绝保留；时钟夹具与缺锚前提经通道合法落字、
与墙钟无关，无时限性窗口。本批范围内已验证通过，状态
READY_FOR_AUDIT，送审对象为 V5 冻结包（NOT_READY 仅剩
证据形式项，见 V5 GAPS）。

## ⑤ 剩余风险

1. **整套一致伪造（实测）**：直写者自造标记行再按其 ID
   写回填事件行，DB 层放行（test_forged_mark_full_set_
   bypass_measured 实测）——标记机制证明「经受控写序」
   的形态（存在性/唯一性/不可变性），不证明调用方身
   份，与 RT-1/RT-2 同级固有界限，待独立审计裁定；伪
   造标记落字后即被绑定锁定（禁改禁删），行为本身留
   下不可删除的证据行；handoff 下游核验与审计抽查为
   第二道防线。
2. 无标记直写的 120 秒当前时间窗口内落字放行（正对
   照实证）：该窗口内的 created_at 与服务器当前时间
   不可区分，本就是「服务器当前时间」口径本身，非回
   填通道。
3. 既有基线失败 test_ledger_fingerprint_idempotent 与
   6 个 stress_test 环境性失败不在本批范围（双边同在、
   如实登记）。
