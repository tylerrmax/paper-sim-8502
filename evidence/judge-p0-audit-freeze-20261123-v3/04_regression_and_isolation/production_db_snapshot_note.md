# 生产库隔离核对

- 生产库 ~/workspace/paper-trading-sim/db/sim.db SHA256：开工、收工、合并后三点均为 c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f，逐字节一致
- 全部修复与测试在隔离 worktree + 临时库执行，未触生产库
- 形式缺项（无 Backup API 冻结副本）沿 V1 G-02 登记，见 00_GAPS
