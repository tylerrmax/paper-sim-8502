# P0-3｜P0-5 新事件 INSERT 时间锚（提交 47f1a55，部分实施）

已实施：trg_rt_discovery_events_created_at_insert_future
——直接 INSERT 的 created_at 晚于 DB 服务器时间 120 秒
以上 -> RAISE(ABORT)；既有 UPDATE 不可变保护保留。
测试 5 用例（未来一天/远未来拒绝、当前服务器时间/120 秒
容差内通过、UPDATE 保护复核），见 04 节 p0_3_tests.log。

未实施（如实登记）：NULL 拒绝与历史回填窗口与既有可信
时钟夹具存在真实冲突（固定字符串模拟历史入库、最老
2026-11-10 且年龄随墙钟无限增长；故意写 NULL 验证缺锚
拒绝的既有用例）。冲突全量与可选方案 A/B/C 见 03 节
DESIGN-V3-CORRECTION.md 与 00_GAPS G-06，待审计裁定，
本批不以时限性窗口冒充长期修复。
