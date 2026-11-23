# 全量回归双边对照（同命令同环境）

命令：`pytest tests/ -q --ignore=tests/test_gapfill.py --ignore=tests/test_ui_regression.py`
解释器：paper-trading-sim/.venv/bin/python（pytest 9.1.1，与基线同环境）。

- 基线（纯 61b1d98）：396 passed / 7 failed，exit 1
  （logs/baseline_full_61b1d98.log，241.52s）
- 修复后（c0a062c）：408 passed / 7 failed，exit 1
  （logs/fixed_full.log，241.15s）；+12 恰为本批新增
  用例（test_market_first_p0_5_channel_marks.py 12 个）
- 失败集逐项 diff：完全相同（baseline_failures.txt 与
  fixed_failures.txt 已随附，diff 为空），新增失败为零。
- 7 个失败逐项：tests/stress_test.py 6 个
  （test_4_exactly_once_commands / test_5_snapshot_atomicity /
  test_6_ashare_dryrun / test_7_concurrent_apply /
  test_9_table_consistency / test_10_datasource_fallback，
  环境依赖型，基线同名同因）+ 既有基线失败
  tests/test_e2e_batch2.py::test_ledger_fingerprint_idempotent。
  环境依赖缺失单独登记于此，不伪报 PASS。
- 分组 -v（P0-3 组 = p0_5_channel_marks + p0_5_insert_anchor
  + p0_5_created_at_guard + p0_5_time_anchor + forward_time
  + rt3_instflow_anchor）：40 passed，exit 0
  （logs/p0_3_group_tests.log，逐用例名/结果/退出码齐全）。
- 合并后复跑（2cf2234，Market-First 登记口径
  tests/test_market_first_*.py + tests/test_adversarial_regression.py）：
  189 passed，exit 0（logs/postmerge_market_first_group.log；
  V4 时点同口径 177 + 本批新增 12）。
