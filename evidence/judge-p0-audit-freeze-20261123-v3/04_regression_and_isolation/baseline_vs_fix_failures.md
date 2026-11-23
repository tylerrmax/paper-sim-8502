# 基线与修复后对照

- 纯基线 c490424 全量：311 passed / 1 failed（baseline_full_pytest.log）
- 修复后全量（同口径命令、300 秒门槛未动）：354 passed / 1 failed，250.26s（item6_full_regression_v2.log）
- 两轮唯一失败均为既有基线失败 test_ledger_fingerprint_idempotent，失败集相同、新增失败为零
- 新增用例 43（311 -> 354），与八项新增测试计数逐项吻合
- 专项构成登记见仓库根 DELIVERY-REDTEAM-FIX-20261123.md
