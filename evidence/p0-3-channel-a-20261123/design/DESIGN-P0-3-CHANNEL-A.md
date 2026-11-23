# P0-3 完整落地设计：方案 A 受控通道标记（2026-11-23，基线 61b1d98）

对象：V4 GAPS G-06 登记的 P0-3 未实施部分——discovery_events
直接 INSERT 的 created_at 为 NULL 或历史回填时无 DB 层校验。
V3 补正批只实施了未来时间拒绝（>120 秒），NULL 与回填窗口
因与可信时钟夹具的结构性冲突未实施（冲突全量见
evidence/v3-audit-correction-20261123/design/
DESIGN-V3-CORRECTION.md）。用户 2026-11-23 15:13 定案采用
方案 A（受控通道标记）：唯一与时间无关的长期修复。

## 冲突回顾（方案 A 为何成立）

- 时钟夹具经 pool.ingest 以固定字符串写 created_at（最老
  2026-11-10），其年龄随墙钟无限增长——任何以服务器当前
  时间为参照的有限回填窗口终将击穿夹具；
- test_missing_ingest_time_rejects_forward 的被测前提是库内
  存在 created_at=NULL 的事件行——DB 层无条件拒 NULL 会使
  前提不可构造；
- DB 触发器无法区分「夹具经 ingest 写入」与「直写 SQL
  写入」——两者在 DB 层是同一条 INSERT。

方案 A 的解法：不按时间值区分，按**写序**区分。合法写入
（ingest，含时钟夹具路径）先落一条通道标记、再落事件行并
以 ID 绑定标记，同事务完成；触发器只对「无有效标记」的
INSERT 启用时间校验。夹具的历史时刻与 NULL 时刻都经
ingest 通道落字、带有效标记，不受时间窗口影响，与墙钟
无关、无到期日。

## 机制设计（沿 P0-2 绑定模式）

标记存储选用专用表而非 DESIGN-V3-CORRECTION 方案 A 草案
的 candidate_history 行：candidate_history 是候选字段变更
审计日志（ingest 本已每事件写一行 discovery_event 记录），
混入通道工件会污染其语义；且「未被引用」判定本就需要在
事件行上落绑定 ID。专用表 + ID 绑定与 P0-2 的
sample_exclusions.audit_history_id 模式完全同构，口径一致。

schema（ensure_redteam_schema 幂等迁移，SCHEMA_SQL 同步）：

- 新表 `ingest_channel_marks(id, candidate_id, mark,
  created_at)`：mark 为 ingest 生成的 uuid4 hex、UNIQUE；
  created_at 由 ingest 以**真实服务器时间**落字（不用夹具
  时钟）——标记证明的是「该事件于真实时刻 T 经受控写序
  入库」，事件行 created_at 承载入库时刻值（生产为服务器
  时间、测试为夹具模拟时刻），两者分工明确。
- discovery_events 新增列 `ingest_mark_id INTEGER`
  （存量行 NULL，不受追溯约束）；UNIQUE 索引
  idx_discovery_events_ingest_mark_id 兜底一对一（SQLite
  UNIQUE 不约束 NULL）。

ingest 写序（pool.ingest，单事务、末尾单次 commit 不变）：

  候选 upsert -> INSERT 标记行 -> INSERT 事件行（带
  ingest_mark_id）-> INSERT candidate_history 行 -> commit。

触发器（db.py `_P03A_TRIGGERS_SQL`，ensure 幂等安装）：

- `trg_rt_discovery_events_insert_channel`（BEFORE INSERT
  ON discovery_events）：
  1. 声称标记（ingest_mark_id 非 NULL）但标记无效——不存
     在、属其他候选、或已被另一事件行消费——一律拒绝；
  2. 无有效标记时启用时间校验：created_at 为 NULL、不可
     解析（julianday 为 NULL）、或早于 DB 服务器时间
     120 秒以上（与 registry approved_at、INSERT 未来
     时间窗口同口径）——一律拒绝；
  3. 带有效标记的 INSERT 不受时间值限制（历史时刻、NULL
     均可），这正是夹具与缺锚前提的合法通道。
- 未来时间拒绝：既有
  trg_rt_discovery_events_created_at_insert_future 原样
  保留（对全部 INSERT 生效，含带标记行——夹具从不写未来
  时刻，普查已证）。
- `trg_rt_ingest_channel_marks_no_update`：标记行禁止
  UPDATE（append-only 通道证据）。
- `trg_rt_ingest_channel_marks_no_delete_bound`：已被事件
  行引用的标记行禁止 DELETE；未被引用的标记行可删（它
  尚未绑定任何事件、不构成任何事件的通道证据，删除不
  改变任何已落字事件的可验证性）——闸的范围边界如实
  登记。

## 证明力与边界（照 P0-2 修订口径，不夸大）

本机制证明的是：每条带非当前时刻（或 NULL）created_at
的事件行，都绑定一条**存在、属同一候选、一对一、绑定后
不可改删**的通道标记，即证据的**存在性、唯一性与不可变
性**——事件行「经受控写序落字」的形态成立。它**不证明
调用方身份**：直写者自造一条标记行、再按其 ID 写回填
事件行，DB 层无法区分（对抗测试
test_forged_mark_full_set_bypass_measured 实测放行并如实
登记）。这与 RT-1/RT-2 的「全套一致伪造」同属 SQLite
单文件模型固有界限；伪造的标记行本身即成为不可删除的
落字证据，审计抽查可据此追溯。handoff 下游核验与审计
抽查仍为第二道防线。

## 夹具迁移登记（断言零改动）

- 唯一生产改动点：pool.ingest 写序（加标记行、事件行带
  绑定 ID）。全仓 25 处 pool.clock 夹具赋值（19 个测试
  文件）均经 ingest 落字，通道内化后自动带标记——测试
  调用点零改动、用例集合不变、断言零改动。
- test_missing_ingest_time_rejects_forward：构造不变——
  clock 返回 None 时 ingest 经通道落 NULL created_at
  事件行（带标记），前提继续成立；新增
  test_channel_ingest_null_created_at_premise_constructible
  作为该前提可构造性的直接证据。
- 既有测试文件仅两处**文档**更新（无逻辑/断言改动）：
  test_market_first_p0_5_insert_anchor.py 与
  test_market_first_p0_5_created_at_guard.py 的模块
  docstring 中「未实施/残余待裁定」段落更新为方案 A
  落地后的实际状态，避免文档与行为脱节（与 P0-2 批
  修订 rt2_db_guard docstring 的先例同口径）。
