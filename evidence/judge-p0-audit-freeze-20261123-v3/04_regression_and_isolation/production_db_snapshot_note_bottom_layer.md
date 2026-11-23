# 生产库隔离核对（底层补强批增量）

- 生产库 ~/workspace/paper-trading-sim/db/sim.db SHA256：本批开工、合并后收工两点均为 c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f，与基准逐字节一致
- 全部修复与测试在隔离 worktree（redteam-bottom-layer / market-first）+ 临时库执行，未触生产库
- 形式缺项（无 Backup API 冻结副本）沿 G-02 登记，见 00_GAPS
