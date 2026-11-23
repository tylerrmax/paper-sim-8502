原问题：RT-2 入口层已强制（exclude_sample reason/decided_by 非空 + 同事务审计行），但直写 SQL `INSERT INTO sample_exclusions` 无 DB 层拦截，可无审计留痕地改变 forward_stats 样本构成。
修复提交：59b2eb7（trg_rt_sample_exclusions_audit_insert：每条排除 INSERT 须 reason/decided_by 非空且同事务已有逐字对应的 candidate_history 审计行；exclude_sample 写序配合改为先审计行后排除行，同事务单次 commit，集合/字段/返回值不变）。
对抗测试：test_market_first_rt2_db_guard.py 5 用例（正常路径/底层正对照/无审计行拒绝/空白字段拒绝/审计行不一致拒绝+回滚留痕）。
设计：03 节 DESIGN-BOTTOM-LAYER.md 闸 2。
