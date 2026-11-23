# 8502 JUDGE-P0 审计冻结包 V5（P0-3 方案 A 完整落地）

- 冻结对象：unified-arch HEAD `2cf2234`（P0-3 方案 A 合并提交，代码冻结对象；其后的组装提交仅新增本包目录并回填 DELIVERY 合并节）
- 基线：`61b1d9868d11dd943f0943e9809b4867a8aafdf0`（V4 冻结 HEAD）
- **V5 增量 = P0-3 完整落地**：方案 A 受控通道标记（修复提交 `c0a062c`、交付提交 `b307477`、--no-ff 合并提交 `2cf2234`，零冲突）；discovery_events 直接 INSERT 的 NULL/历史回填/不可解析 created_at 已由通道标记闸收口，未来时间拒绝保留；合并后复跑 Market-First 全组 189 passed（V4 同口径 177 + 新增 12）
- 内容：01 源码与提交（链/diff_61b1d98_to_2cf2234/bundle+verify，bundle 以 c490424 为前置）/ 02 P0-3 方案 A 修复（fix_note + 测试源码）/ 03 设计文档（DESIGN-P0-3-CHANNEL-A + DESIGN-V3-CORRECTION 冲突分析原文）/ 04 回归与隔离（基线/修复双边全量日志 + 分组 -v 日志 + 合并后复跑 + 失败集对照 + 生产 SHA 记录）/ 05 台账（V5 时点快照）/ 06 DELIVERY-P0-3-CHANNEL-A 五项交账
- V1/V2/V3/V4 包原样保留，本包为增量覆盖 P0-3 完整落地后的新冻结
- 整包状态 NOT_READY：缺项见 00_GAPS（G-01/G-02/G-03 证据形式项沿前；G-06 已按方案 A 实施、更新为待独立审计复核）
