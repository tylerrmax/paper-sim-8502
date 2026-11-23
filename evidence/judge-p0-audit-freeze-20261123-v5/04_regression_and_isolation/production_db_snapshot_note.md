# 生产库隔离记录（P0-3 方案 A 批）

生产库 ~/workspace/paper-trading-sim/db/sim.db SHA256：
- 开工（基线核对）：c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f
- 收工（合并后）：同上，逐字节一致。
全程只用隔离临时库（pytest tmp_path）与隔离 worktree，
未连接、未写入生产库。形式缺项沿 G-02（无 Backup API
冻结副本，理由同前：事后副本不能证明当时状态，不补造）。
