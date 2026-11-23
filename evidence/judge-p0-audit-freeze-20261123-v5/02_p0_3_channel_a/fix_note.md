# P0-3｜P0-5 新事件 INSERT 时间锚完整落地（提交 c0a062c，方案 A 受控通道标记）

V3 补正批已实施：未来时间拒绝（trg_rt_discovery_events_
created_at_insert_future，>120 秒）。未实施的 NULL 拒绝
与历史回填拦截本批按用户定案的方案 A 落地：

- ingest_channel_marks 通道标记表 + discovery_events.
  ingest_mark_id 一对一绑定（UNIQUE 索引兜底）；
  pool.ingest 同事务先落标记行（真实服务器时间落字）
  再落事件行并绑定；
- trg_rt_discovery_events_insert_channel：无有效标记的
  INSERT，created_at 为 NULL / 不可解析 / 早于服务器
  时间 120 秒以上 -> RAISE(ABORT)；声称标记但不存在、
  跨候选或已被消费 -> RAISE(ABORT)；带有效标记者不受
  时间值限制（时钟夹具历史时刻与 NULL 缺锚前提合法
  落字，与墙钟无关、无时限窗口）；
- 标记行禁 UPDATE、被引用后禁 DELETE（未引用可删，
  范围边界如实登记）。

新增测试 12 用例（无标记 NULL/回填/不可解析拒绝、窗口
内正对照、通道一对一绑定、NULL 前提经通道可构造、标
记复用/悬空/跨候选拒绝、整套伪造实测、已绑定标记不可
变、未绑定标记删除范围边界），见 04 节
p0_3_group_tests.log（组总 40 passed）。未来时间拒绝由
既有 test_market_first_p0_5_insert_anchor.py 覆盖（随
附于本节），本批引用不重复。

证明力边界：标记证明「经受控写序落字」的形态（存在
性/唯一性/不可变性），不证明调用方身份——直写自造标
记整套一致伪造实测放行（test_forged_mark_full_set_
bypass_measured），属固有界限，待审计裁定。设计全量
见 03 节 DESIGN-P0-3-CHANNEL-A.md。
