# 8502 JUDGE-P0 审计冻结包 V4（V3 独立审计补正）

- 冻结对象：unified-arch HEAD `610128d`（V3 补正合并提交，代码冻结对象；其后的组装提交仅新增本包目录）
- 基线：`28e730fc24c2ab9edc9c989d4bff630374cd3bad`（V3 冻结 HEAD）
- **V4 增量 = V3 独立审计三项补正**：P0-1 RT-1 审批记录持续有效性（`88cb4c8`）、P0-2 RT-2 审计记录唯一绑定 + 口径修订（`2927750`）、P0-3 P0-5 INSERT 时间锚未来部分（`47f1a55`，部分实施），交付提交 `65ee1f9`，--no-ff 合并提交 `610128d`（零冲突）；合并后复跑 Market-First 全组 177 passed（V3 同口径 149 + 新增 28）
- 内容：01 源码与提交（链/diff_28e730f_to_610128d/bundle+verify，bundle 以 c490424 为前置）/ 02 三项补正（fix_note + 对抗测试源码）/ 03 设计文档（DESIGN-V3-CORRECTION + 口径修订补记版 DESIGN-BOTTOM-LAYER）/ 04 回归与隔离（基线/修复双边全量日志 + 分组 -v 日志 + 合并后复跑 + 失败集对照 + 生产 SHA 记录）/ 05 台账（V4 时点快照）/ 06 DELIVERY-V3-CORRECTION 五项交账
- V1/V2/V3 包原样保留，本包为增量覆盖 V3 补正后的新冻结
- 整包状态 NOT_READY：缺项见 00_GAPS（G-01/G-02/G-03 证据形式项沿前，G-06 为 P0-3 未实施部分的冲突登记，据实标注）
