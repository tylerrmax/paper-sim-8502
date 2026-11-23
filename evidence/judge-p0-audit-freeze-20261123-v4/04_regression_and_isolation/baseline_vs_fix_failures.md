# 全量回归双边对照（同命令同环境）

命令：`pytest tests/ -q --ignore=tests/test_gapfill.py --ignore=tests/test_ui_regression.py`
解释器：paper-trading-sim/.venv/bin/python（pytest 同环境）。

- 基线（纯 28e730f）：368 passed / 7 failed，exit 1
  （logs/baseline_full_28e730f.log，244.52s）
- 修复后（47f1a55）：396 passed / 7 failed，exit 1
  （logs/fixed_full.log，240.50s）；+28 恰为本批新增用例
  （P0-1 12 + P0-2 11 + P0-3 5）
- 失败集逐项 diff：完全相同（baseline_failures.txt 与
  fixed_failures.txt 已随附，diff 为空），新增失败为零。
- 7 个失败逐项：tests/stress_test.py 6 个
  （test_4_exactly_once_commands / test_5_snapshot_atomicity /
  test_6_ashare_dryrun / test_7_concurrent_apply /
  test_9_table_consistency / test_10_datasource_fallback，
  环境依赖型，基线同名同因）+ 既有基线失败
  tests/test_e2e_batch2.py::test_ledger_fingerprint_idempotent。
  环境依赖缺失单独登记于此，不伪报 PASS。
- 合并后复跑（610128d，Market-First 登记口径
  tests/test_market_first_*.py + tests/test_adversarial_regression.py）：
  177 passed，exit 0（logs/postmerge_market_first_group.log；
  V3 时点同口径 149 + 本批新增 28）。
