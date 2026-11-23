# 设计文档：P0-3 数据库审批保护 / P0-5 created_at 可信性 / RT-5 bars 校验

批次：红队缺陷集中修复 V1.0（2026-11-23），基线 c490424。
本文件在第 5/6/7 项实施**之前**定稿，随证据包交付；实施与
本设计不一致处以交付说明的偏差登记为准。

## 一、第 5 项 P0-3 数据库审批保护

### 现状（自红队探针 A1/A2 实证）
- `rule_registry` 无触发器：直写
  `UPDATE rule_registry SET status='approved', approved_by='伪造',
  approved_at='2020-01-01', approval_basis='伪造'` 成功。
- `registry_history` 只有 UPDATE/DELETE 防护触发器：伪造
  INSERT 一条 approved 流转记录成功。
- 应用层（register_rule / set_rule_status）已有强制：approved
  须 approved_by + reason + approval_basis 非空、approved_at
  恒服务器时间、同事务写 history。

### 设计：DB 层触发器（受控写入机制）
在 `market_first/db.py` 新增 `ensure_redteam_schema(conn)`，
幂等安装下列触发器，并挂入 `init_db` 与 `validation._ensure`
（与既有 `ensure_p0int_schema` 同模式，保证新旧库一致）：

1. `trg_rt_rule_registry_approved_insert`（BEFORE INSERT ON
   rule_registry，WHEN NEW.status='approved'）：要求
   trim(NEW.approved_by) 非空、trim(NEW.approval_basis) 非空，
   且 NEW.approved_at 与 DB 服务器时间 `julianday('now')` 之
   差 ≤ 120 秒（绝对值）。任一不满足 RAISE(ABORT)。
2. `trg_rt_rule_registry_approved_update`（BEFORE UPDATE OF
   status ON rule_registry，WHEN NEW.status='approved' AND
   OLD.status<>'approved'）：同上校验（只管「进入 approved」
   的流转；已 approved 行的其他字段更新不拦）。
3. `trg_rt_registry_history_approved_insert`（BEFORE INSERT ON
   registry_history，WHEN NEW.field='status' AND
   NEW.new_value='approved'）：交叉核对——同库 rule_registry
   中 (rule_id=NEW.entity_id, version=NEW.entity_version) 行
   必须已为 approved 且 approved_by/approval_basis 非空，
   否则 RAISE(ABORT)。
   - 可行性：应用路径恒「先写 registry、后写 history、同
     事务」，触发器在 INSERT 时点可见同事务未提交的 registry
     行（SQLite 同连接可见），天然通过。
   - 既有 history UPDATE/DELETE 防护触发器保持不变。

### 测试替代方案（夹具处置）
- 全仓实查：tests/ 与 market_first/ 之外无直写 rule_registry /
  registry_history 的代码（grep 证据见交付说明）；既有测试
  夹具全部走应用层 register_rule / set_rule_status，approved_at
  恒为真实服务器时间，天然满足 120 秒窗口，无需受控测试通道。
- 若个别夹具确需构造历史批准行：只许走应用层 + 本设计第 6 项
  的可信时钟夹具思路（在 validation 层注入），禁止测试直写
  SQL 绕过触发器；本批实测无此需要，如出现则停下登记。

### 诚实残余（设计界限，如实登记）
- 同一主体若用一条 SQL 把 registry 的 status/approved_by/
  approval_basis/approved_at 四个字段**按当前时间一致伪造**，
  触发器无法与应用层合法写入区分——DB 触发器防的是「缺项/
  回填时间/无对应注册行」的伪造，不能防「全套一致伪造」。
  全套伪造仍会留下与应用写入不可区分的痕迹，这是 SQLite
  单文件、无独立审计主体的固有界限；进一步防护（独立审计
  库/签名）不在本批范围，登记为待审计裁定项。

## 二、第 6 项 P0-5 created_at 可信性

### 现状
- `pool.ingest` 以服务器 `_now_iso()` 落 discovery_events
  .created_at（生产路径可信）；但表无触发器，直写 UPDATE 可
  回填/前移 created_at（自红队 B1/B2），应用层锚校验被绕过。
- 大量测试 helper 在 ingest 后直写 UPDATE created_at 对齐
  discovered_at（模拟「发现即入库」），共 13 处、9+2 个文件。

### 设计
1. **可信测试时钟夹具**：`CandidatePool.__init__` 新增
   `clock=None` 参数；ingest 落字时刻取
   `self.clock() if self.clock else _now_iso()`。生产不传
   clock 即真实服务器时间，行为不变。测试在 ingest 前设置
   `pool.clock = lambda: ev.discovered_at`（或构造时传入），
   以应用层路径产生「模拟入库时刻」，断言零改动。
2. **写入保护触发器**：`ensure_redteam_schema` 安装
   `trg_rt_discovery_events_created_at_no_update`（BEFORE
   UPDATE OF created_at ON discovery_events WHEN
   NEW.created_at IS NOT OLD.created_at → RAISE(ABORT)）：
   created_at 一经落字不可修改（回填/前移/后移全拒）。
3. **测试迁移**：13 处直写 UPDATE 逐处改为 clock 夹具，
   逐项登记（文件/用例/原手法/新手）入交付说明；断言零改动。

### 诚实残余
- INSERT 时点伪造 created_at（直写 INSERT 一条带任意
  created_at 的事件行）触发器不拦：INSERT 是新事件的唯一
  入口，合法 ingest 与伪造 INSERT 在 DB 层不可区分（同
  第 5 项界限）。锚的可信性因此依赖「事件行只能经 ingest
  产生」的应用纪律 + 本触发器保证「落字后不可篡改」。
  如实登记为待审计裁定项。

## 三、第 7 项 RT-5 evaluate_sample bars 校验

### 现状
- `evaluate_sample(pool, sample_id, bars)` 无条件接受调用方
  bars 计算 ret_t5/t10/t20 与 max_drawdown 并回写；伪造 bars
  可直接制造 forward 收益（自红队 RT-5 存疑项，用户定案
  本批修复）。

### 设计
1. **可信来源**：系统自有行情落库表 `fetched_bars`（由
   market_first.bars 取数层写入，同库）。校验 = 调用方 bars
   必须与 fetched_bars 中该标的的记录**逐 bar 一致**（日期
   存在且 OHLCV 浮点差 ≤ 1e-6）。
2. **校验范围**：样本 kind='forward' 且 bars 非空时执行
   （backfill 样本不进 forward_stats，不在本项口径；
   bars 为空走既有 missing 路径）。校验点在 benchmark 修订
   检测与收益计算**之前**。
3. **标的核对**：标的取样本 candidate 的 symbol（库内记录，
   非调用方声称）；fetched_bars 无该标的任何行 → 不可验证。
4. **失败口径**（与既有门禁一致的等价拒绝）：不抛异常中断，
   写 maturity_status='not_evaluable'、missing_reason=
   'bars_untrusted: <具体原因>'、ret/max_drawdown 全 NULL、
   bars_asof/data_version 照常留痕，返回样本。不可信行情
   不产生任何有效 forward 结果（judge 的成熟样本口径只认
   maturity_status='mature'，forward_stats 同）。
5. **数据版本**：调用方 data_version 只留痕不作依据；版本
   依据 = fetched_bars 的 source/fetched_at 落库记录本身
   （与全仓「声称值只留痕」口径一致）。
6. **受控测试通道**：bars 层新增公开函数
   `store_bars(symbol, bars, source, db_path)`（内部即既有
   `_own_cache_write`，get_bars 取数成功时本就经此落库）；
   测试夹具在 evaluate 前经此通道把行情落入 fetched_bars，
   模拟生产取数路径。受影响测试逐文件登记（helper 内一处
   落库，断言零改动）。

### 残余
- fetched_bars 本身被直写 SQL 污染时校验被绕过——同
  第 5/6 项的 DB 直写固有界限，登记待裁定；应用层调用方
  （本项威胁模型）无法绕过。
