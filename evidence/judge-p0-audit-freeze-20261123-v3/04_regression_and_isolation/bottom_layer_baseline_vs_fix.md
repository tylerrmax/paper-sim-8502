# 底层补强批回归对照（基线 a84fb4a vs 修复分支）

同命令双边实测：`pytest tests/ -q --ignore=tests/test_gapfill.py --ignore=tests/test_ui_regression.py`
（两 ignore 文件为脚本型/环境型收集期失败，与 V2 批次 354/1 的较窄口径差异在此登记，本批以同命令双边对照为准）

- pristine 基线 a84fb4a：358 passed / 7 failed（245.60s）—— logs/full_regression_baseline_a84fb4a.log
- 修复分支（f273d5b+59b2eb7）：368 passed / 7 failed（247.17s）—— logs/full_regression_fixbranch.log
- 差值 +10 恰为新增底层闸对抗用例（rt1_db_guard 5 + rt2_db_guard 5）
- 两轮 FAILED 清单程序化 diff 逐项完全相同：6 个 stress_test.py 环境性失败（ModuleNotFoundError 等）+ 既有基线失败 test_ledger_fingerprint_idempotent
- **新增失败为零**；300 秒门槛未动；不删测试、不降断言
- 快速组：新 10 + 相关既有 31 = 41 passed
- 合并后（bf2135b）复跑 Market-First 相关组（V2 同口径：test_market_first_*.py + test_adversarial_regression.py）：149 passed —— logs/postmerge_market_first_all.log
- 触发器枚举实证：init_db 后 trg_rt_* 共 7 条（原 4：registry_history 禁改/禁删、rule_registry approved insert/update、registry_history approved insert、discovery_events created_at 禁改；新 3：candidates promotion approved insert/update、sample_exclusions audit insert）
