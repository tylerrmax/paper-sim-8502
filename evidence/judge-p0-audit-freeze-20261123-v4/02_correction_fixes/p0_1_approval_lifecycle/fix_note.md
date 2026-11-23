# P0-1｜RT-1 审批记录持续有效性（提交 88cb4c8）

缺口：进入 approved 瞬间的存在性校验已封，但事后直写 DELETE
有效 approvals 行、或 UPDATE 其 candidate_id/decision/
approver/basis 使其失效/转移，无拦截。

修复（market_first/db.py，_V3C_TRIGGERS_SQL）：
- trg_rt_approvals_no_delete_live：被删行是有效批准
  （decision='approved'、approver/basis 非空）且候选当前
  promotion_status='approved' 时 DELETE -> RAISE(ABORT)；
- trg_rt_approvals_no_update_keyfields_live：同等条件下
  candidate_id/decision/approver/basis/decided_at 任一
  实际变化 -> RAISE(ABORT)。
versions_json 不在保护列内（RT-6 已登记 OPEN）。

测试：test_market_first_rt1_approval_lifecycle.py 12 用例
（删除/整表删除/六种关键字段改写/跨候选转移/范围边界/
decide 正常路径 + 落字后即受保护），逐项结果见 04 节
p0_1_tests.log（同组既有 rt1 三文件 14 用例同跑全过）。
