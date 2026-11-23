# V3 独立审计补正设计（2026-11-23，基线 28e730f）

对象：V3 冻结包独立审计提出的三项补正——RT-1 审批记录持续
有效性（P0-1）、RT-2 审计记录唯一绑定（P0-2）、P0-5 新事件
时间锚 INSERT 校验（P0-3）。机制沿用既有 ensure 体系
（db.ensure_redteam_schema 幂等安装 `trg_rt_*` 触发器；
validation._ensure 与 db.init_db 均调用之）。

## P0-1｜approvals 生命周期保护（RT-1）

缺口：既有闸只校验「进入 approved 的瞬间」存在有效 approvals
行；事后直写 DELETE 该行、或 UPDATE 其 candidate_id /
decision / approver / basis / decided_at 使其失效或转移，
候选可继续停留在无有效审批依据的 approved 状态。

设计（两道新触发器，db.py `_V3C_TRIGGERS_SQL`）：

- `trg_rt_approvals_no_delete_live`（BEFORE DELETE ON
  approvals）：被删行本身是有效批准（decision='approved'、
  approver/basis 非空）且其候选当前 promotion_status=
  'approved' 时 RAISE(ABORT)。候选未处于 approved、或行
  本身非有效批准（如 rejected 记录、字段残缺行）不在此闸
  范围——它们不是任何 approved 终态的依据。
- `trg_rt_approvals_no_update_keyfields_live`（BEFORE UPDATE
  OF candidate_id, decision, approver, basis, decided_at ON
  approvals）：旧行是有效批准且候选当前 approved 时，五个
  关键字段任一实际变化（IS NOT 比较）即 RAISE(ABORT)。
  versions_json 不在保护列内（RT-6 已登记 OPEN 单独评估，
  不在本批范围）。

正常路径：promotion.decide 只 INSERT approvals、从不
UPDATE/DELETE，天然通过；既有测试中删除 approvals 的场景
（rt1_db_guard 在候选仍为 discovered 时清理、stress_test
操作的是生产 sim.db 模式的另一张 approvals 表、无本组
触发器）均不受影响（全量回归双边对照实证）。

边界（诚实登记）：先把 candidates.promotion_status 直写
改出 approved、再删 approvals、再伪造新 approvals 行改回
approved——属「全套一致伪造」固有界限（改回时仍须有字段
齐全的 approvals 行，handoff 下游核验仍在）；单步的删/改
失效路径本批已封。

## P0-2｜sample_exclusions 审计记录唯一绑定（RT-2）

缺口：既有闸以字段逐字相等判断审计行对应，同一审计行可
被多条排除反复引用（旧行复用/跨样本复用在字段恰好相同时
不可区分），且被引用的审计行事后可改可删。

设计：

- schema：sample_exclusions 新增 `audit_history_id INTEGER`
  列（ensure_redteam_schema 内 PRAGMA 检查后 ALTER；
  SCHEMA_SQL 同步加列供新库）；`CREATE UNIQUE INDEX
  IF NOT EXISTS idx_sample_exclusions_audit_history_id
  ON sample_exclusions(audit_history_id)`（SQLite UNIQUE
  对 NULL 不约束，存量/历史行 NULL 不受影响）。
- 写入：validation.exclude_sample 写序不变（先审计行后
  排除行、同连接单次 commit），排除行落字时带上审计行
  的 lastrowid 作为 audit_history_id。
- 触发器（修订 trg_rt_sample_exclusions_audit_insert 并
  新增两道）：
  - 绑定必备：audit_history_id 为 NULL -> 拒绝；
  - 引用有效：所引 candidate_history 行必须存在且与排除
    行逐字对应（candidate_id 随样本、field='sample_exclusion'、
    new_value=excluded_from、actor=decided_by、reason=reason）
    ——原字段对应校验保留为冗余校验；
  - 一对一：所引审计行已被另一条排除引用 -> 拒绝
    （触发器显式检查 + UNIQUE 索引兜底）；
  - `trg_rt_candidate_history_no_update_bound` /
    `trg_rt_candidate_history_no_delete_bound`：被任一
    排除引用的 candidate_history 行禁止 UPDATE/DELETE。
    未被引用的历史行不在此闸范围（rt2 既有测试删除未被
    引用的不匹配审计行场景不受影响，回归实证）。

- 既有测试构造迁移（断言零改动，逐项登记于交付文档）：
  tests/test_market_first_rt2_db_guard.py 的直写辅助函数
  改为显式传 audit_history_id（_direct_insert_audit 返回
  行 id、_direct_insert_exclusion 增参），各用例断言与
  预期（拒绝/通过、行数、统计构成）一字未动。

### 口径修订（P0-2 第 5 项，强制执行）

旧表述「同事务已有逐字对应审计行」「同事务原子写入」自
本批起修订为：**不可变、一对一关联的审计证据**。理由：
SQLite 触发器在排除行 INSERT 时点只能观察已落字的行，
无法证明审计行与排除行由同一事务、同一调用方写入——
「同事务身份」不是触发器能证明的命题。本批机制实际
证明的是：①每条排除绑定一条明确的审计记录（ID 引用）；
②该审计记录与排除逐字对应且只能被引用一次；③绑定后
审计记录不可改、不可删。即证据的**存在性、唯一性与
不可变性**成立；证据的**同源性（同一事务/同一操作者）**
不成立、无法由 DB 层证明——先整套伪造审计行再按其内容
写排除行，仍属全套一致伪造固有界限，待审计裁定。
修订落点：本设计文档、db.py 触发器消息与
ensure_redteam_schema docstring、validation.exclude_sample
docstring、DESIGN-BOTTOM-LAYER.md 补记、V4 GAPS、台账
MF-VAL-001 残余风险。

## P0-3｜discovery_events INSERT 时间锚：部分实施 + 冲突报告

先行普查（可信测试时钟夹具实际写入的 created_at 全量）：

- 时钟夹具经 pool.ingest 写入的 created_at 取值：
  2026-11-10T16:30、2026-11-13T16:30/16:35、2026-11-16
  T09:05、2026-11-17T09:05、2026-11-18T09:00、2026-11-20
  T09:00/18:00（均为固定字符串，模拟历史入库时刻），
  以及 **None**（test_market_first_p0_5_time_anchor.py::
  test_missing_ingest_time_rejects_forward 故意写 NULL
  created_at，验证「无可信锚 -> forward 在 validation 层
  被拒」这一断言本身）。
- 全仓无任何夹具写入未来 created_at（最大值 2026-11-20
  < 冻结日 2026-11-23）；全仓无测试直写 INSERT
  discovery_events（唯一写入口 pool.ingest）。

冲突判定：

- **未来时间**：与全部夹具无冲突 -> 本批实施。新增
  `trg_rt_discovery_events_created_at_insert_future`：
  BEFORE INSERT，created_at 非 NULL 且晚于 DB 服务器
  时间 120 秒以上（与 P0-3 registry approved_at 窗口
  同口径）-> RAISE(ABORT)。
- **NULL**：与 test_missing_ingest_time_rejects_forward
  存在真实冲突——该用例的被测前提就是「库内存在
  created_at 为 NULL 的事件行」，DB 层拒绝 NULL 会使
  前提不可构造（ingest 当场 IntegrityError），断言无从
  验证。且 NULL 锚在 validation 层已被处理（缺锚 ->
  ForwardRejectedError + trusted_ingest_time_missing
  留痕），NULL 行不可能充当 forward 锚，风险方向是
  「不可用」而非「可伪造」。-> 本批**不实施**，待审计
  裁定（可选方案见下）。
- **历史回填窗口**：与夹具存在结构性冲突——夹具以固定
  字符串模拟历史入库时刻（最老 2026-11-10，距冻结日
  13 天），其「年龄」随墙钟无限增长；任何以
  julianday('now') 为参照的有限窗口 W，都会在墙钟越过
  夹具日期 + W 时无代码变更地击穿既有测试。窗口须
  ≥13 天才在冻结日成立，而审计攻击面正是天级回填
  （如 9 月声称、11 月入库约 69 天）——能拦住攻击的
  窗口（<69 天）至多维持数周即击穿夹具，能长期容纳
  夹具的窗口（→∞）拦不住攻击。且 DB 触发器无法区分
  「应用层可信时钟夹具写入」与「直写 SQL 写入」——
  两者在 DB 层是同一条 INSERT。-> 本批**不实施窗口**，
  不以时限性窗口冒充长期修复，待审计裁定。

可选方案（交审计/用户裁定，本批不擅自执行）：

- 方案 A（受控通道标记）：ingest 写序改为先写带唯一
  标记的 candidate_history 行、再写事件行，INSERT 触发
  器要求同候选存在未被引用的标记行——直写无标记一律
  拒绝（与时间无关、无时限问题）；代价是全套一致伪造
  仍可绕过（与 RT-1/RT-2 同级界限），且改变 ingest
  写序与多处夹具构造。
- 方案 B（宽限窗口）：接受 400 天级窗口作为「荒谬回填」
  下限（拒 2025 年以前的 created_at），夹具安全期约
  一年，文档明示时限与到期处理；拦不住天级攻击。
- 方案 C（维持现状）：INSERT 侧只封未来（本批已做）+
  既有 UPDATE 不可变 + validation 层锚校验（缺锚拒绝、
  锚后入场）构成实际防线；直写 INSERT 历史回填登记为
  已知界限（V3 GAPS G-05 已载），由审计抽查兜底。

保留：既有 UPDATE 不可变触发器
trg_rt_discovery_events_created_at_no_update 原样不动。
