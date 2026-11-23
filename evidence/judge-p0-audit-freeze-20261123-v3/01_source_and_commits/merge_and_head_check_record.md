# 合并与 HEAD 核对记录（V3）

## 红队修复 V1.0 八项（V2 已冻结部分）
- 基线：`c490424939422639280d19fa36bcccd34da83184`
- 合并提交：`0d93eb8`（--no-ff 合并 market-first/judge-p0-redteam-fix-20261123，零冲突）
- 交付提交：`dd70483`（台账+证据+DELIVERY）
- V2 冻结 HEAD（unified-arch）：`dd70483c47841ced7dbf23f873d601ed2592535d`；V2 组装提交 `a84fb4a`（仅新增 V2 冻结包目录）
- 合并后复跑 Market-First 全部：139 passed（V2 包 04 节 postmerge_market_first_all.log）

## 底层边界补齐批（V3 增量）
- 基线：`a84fb4a`（V2 组装后 HEAD）
- 修复提交：`f273d5b`（RT-1 晋升 DB 闸）+ `59b2eb7`（RT-2 排除 DB 闸），隔离分支 market-first/redteam-bottom-layer-20261123
- 交付提交：`2bfef8f`（台账 MF-PROMO-002/MF-VAL-001 底层闸引用 + 回归日志 + DELIVERY-BOTTOM-LAYER）
- 合并提交：`bf2135b47e1a2ad407192f20e624274c7c53667e`（--no-ff 合并入 unified-arch，零冲突）
- **V3 冻结 HEAD（unified-arch 代码冻结对象）：`bf2135b47e1a2ad407192f20e624274c7c53667e`**（其后的组装提交仅新增本冻结包目录）
- 合并后复跑 Market-First 相关组（与 V2 同口径：tests/test_market_first_*.py + tests/test_adversarial_regression.py）：**149 passed**（V2 的 139 + 新增底层闸对抗 10；04 节 logs/postmerge_market_first_all.log）
- 生产库 sim.db SHA256：开工、合并后两点均为 c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f（04 节 production_db_snapshot_note_bottom_layer.md）
- 推送：归主会话执行，本包组装时尚未推送（见 00_GAPS G-03）
