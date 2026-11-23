# judge P0 集成证据索引（2026-11-23）

- 集成分支：`market-first/judge-p0-integration-20261123`（worktree
  `~/workspace/l2-remediation/judge-p0-integration`），基线 fix3 `936e4c6`。
- 集成三提交：`7d2ec5a`（P0-3）、`b2badaa`（P0-1）、`bbdbe95`（P0-5）。
- 合并：`086b23e` merge commit 入 `market-first/unified-arch`（零冲突，
  ort 策略，用户 2026-11-23 10:58 授权本批合并）。
- 解释器：`~/workspace/paper-trading-sim/.venv/bin/python`（主仓 .venv）。
- 全量命令（基线与集成后同口径，300 秒硬上限、未调高）：
  `timeout 300 <py> -m pytest tests/ -q --ignore=tests/e2e_8stage.py
  --ignore=tests/stress_test.py --ignore=tests/test_gapfill.py
  --ignore=tests/test_ui_regression.py --ignore=tests/test_pause_resume.py
  -p no:cacheprovider`（即 release_gate.default_pytest_cmd 的 ignore 集）。

## 文件

| 文件 | 内容 |
|---|---|
| `diff_integration_936e4c6_to_bbdbe95.patch` | 集成三提交完整 diff（936e4c6 → bbdbe95，1027 行） |
| `commits.txt` | 936e4c6..086b23e 提交链（含合并提交） |
| `logs/baseline_full_pytest.log` | 纯基线 936e4c6 全量：294 passed / 1 failed，271.26s，exit=1 |
| `logs/item1_p0_adversarial.log` | 第 1 项 三项 P0 对抗：17 passed，exit=0 |
| `logs/item2_fix3_special.log` | 第 2 项 fix3 专项：33 passed，exit=0 |
| `logs/item3_market_first_all.log` | 第 3 项 Market-First 全部（集成分支）：86 passed，exit=0 |
| `logs/item4_g3g5_exclusion.log` | 第 4 项 G3/G5 排除统计：20 passed，exit=0 |
| `logs/item5_promotion_handoff_boundary.log` | 第 5 项 晋升/交接/统计边界：30 passed，exit=0 |
| `logs/item6_full_regression.log` | 第 6 项 集成后全量：311 passed / 1 failed，269.21s，exit=1（唯一失败 = 基线失败，新增失败为零） |
| `logs/postmerge_market_first_all.log` | 合并后 unified-arch@086b23e 重跑 Market-First 全部：86 passed，exit=0 |
| `db_isolation.md` | 生产库开工/收工 SHA256 与隔离说明 |
| `unified-arch.bundle` | 合并后 unified-arch 的 git bundle（交付提交后生成，见交付文档） |

## 基线对照集

纯基线 936e4c6 失败集 = {`tests/test_e2e_batch2.py::test_ledger_fingerprint_idempotent`}
（与 fix3 交付已知基线失败一致）。集成后失败集与之逐项相同；
通过数 294 → 311，增量 +17 恰为三份新对抗测试用例数（4+7+6）。
