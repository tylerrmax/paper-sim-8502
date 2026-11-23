原问题：RT-1 入口层已强制（decide approver/basis 非空 + handoff 下游核验），但直写 SQL `UPDATE candidates SET promotion_status='approved'` 无 DB 层拦截，不符合「正式入口和底层边界同时成立」（用户 2026-11-23 13:24 重申）。
修复提交：f273d5b（trg_rt_candidates_promotion_approved_insert/_update：进入 approved 须有 decision='approved' 且 approver/basis 非空的 approvals 行，否则 RAISE(ABORT)，INSERT/UPDATE 双路径）。
对抗测试：test_market_first_rt1_db_guard.py 5 用例（正常路径/底层正对照/直写 UPDATE 拒绝/直写 INSERT 拒绝/空字段 approvals 不放行+回滚留痕）。
设计：03 节 DESIGN-BOTTOM-LAYER.md 闸 1。
