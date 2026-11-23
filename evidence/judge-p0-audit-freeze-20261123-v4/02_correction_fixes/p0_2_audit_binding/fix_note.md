# P0-2｜RT-2 审计记录唯一绑定（提交 2927750）

缺口：字段逐字相等判断下同一审计行可被多条排除复用，
被引用审计行事后可改可删。

修复：
- sample_exclusions.audit_history_id 列（ensure_redteam_schema
  幂等 ALTER + SCHEMA_SQL 同步）+ UNIQUE 索引
  idx_sample_exclusions_audit_history_id；
- 修订版 trg_rt_sample_exclusions_audit_insert（ensure 内
  DROP 旧同名后重建）：原字段非空/逐字对应校验保留为冗余，
  新增绑定必备、引用逐字对应、一对一三项检查；
- trg_rt_candidate_history_no_update_bound /
  no_delete_bound：被任一排除引用的审计行禁改禁删；
- validation.exclude_sample 落字写入绑定 id（写序不变）。

口径修订（第 5 项）：「同事务」表述统一修订为「不可变、
一对一关联的审计证据」，同源性不在 DB 层证明范围内；
落点见 03 节设计文档与 DESIGN-BOTTOM-LAYER 补记。

测试：test_market_first_rt2_audit_binding.py 11 用例
（绑定正对照/无绑定拒绝/旧行复用/跨样本复用/事后删除/
事后修改×4/未引用行边界）；既有 rt2_db_guard 直写辅助
迁移为显式传绑定 id（构造迁移、断言零改动，登记见
DELIVERY）。逐项结果见 04 节 p0_2_tests.log。
