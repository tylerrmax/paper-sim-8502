# 数据库隔离证明（judge-p0-integration-20261123）

## 生产库（全程只读，字节级未动）

- 路径：`~/workspace/paper-trading-sim/db/sim.db`
- 开工 SHA256（2026-11-23 集成开始前）：
  `c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f`
- 收工 SHA256（合并与全部回归完成后）：
  `c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f`
- 结论：开工/收工逐字节一致，生产库未被本轮任何步骤写入。

## 隔离环境

- 代码：独立 worktree `~/workspace/l2-remediation/judge-p0-integration`
  （分支 `market-first/judge-p0-integration-20261123`，基线 936e4c6）；
  合并目标 worktree `~/workspace/l2-remediation/market-first`
  （分支 `market-first/unified-arch`）。
- 数据库：全部测试使用 pytest `tmp_path` 下的临时 SQLite 库
  （`mf.db` 等），由 CandidatePool 在临时目录 init_db 生成；
  未连接、未导入生产库数据。
- 缓存/配置/日志：沿用仓库既有隔离约定（market_first.bars
  use_cache=False + 自有 fetched_bars 表；测试日志只写本
  evidence 目录与 pytest 临时目录）。
- judge-g1g2 worktree 保持 7e0e05a 检出原样，全程只读引用
  p0fix 分支对象（git diff/show），未改动其检出状态。
