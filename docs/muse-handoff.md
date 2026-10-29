# 给 muse 的执行清单（本机操作，按顺序）

以下需要改动本机 `~/workspace/paper-trading-sim` 或使用本机 GitHub 连接器，云端沙盒做不到。

## 1. 合并本分支后拉到本机，并先只读检查
```bash
cd ~/workspace/paper-trading-sim
git pull   # 或把 PR 的文件复制过去
python3 tools/check_consistency.py          # 预期当前有 6 个 error（A股成交量单位切换）
python3 tools/sync_manifest.py --list sync_8502_github.py   # 列出清单缺的文件
```

## 2. 应用数据库迁移（本机的真库，自动备份、失败回滚）
```bash
python3 tools/migrate.py db/sim.db --dry-run
python3 tools/migrate.py db/sim.db
```
之后 `signals` 的每次写入/状态变化会自动写入 `signal_log`，审批/成交/启停写入 `audit_log`，非法值被拒绝。
**注意**：pipeline 里若有"一次 executemany 写多条 fills/signals"，遇到非法值会整批回滚——这是设计意图，但请确认跑批不会因此静默失败（看退出码/日志）。

## 3. 在 pipeline 入库前接入行情闸门（engine/pipeline.py 行情写入处）
```python
from engine.data_gate import check_bars, record_quarantine
issues = check_bars(symbol, bars, market)            # bars=[(date,o,h,l,c,v)]
errors = [i for i in issues if i["severity"] == "error"]
if errors:
    record_quarantine(con, issues)                   # 登记隔离，并拒绝写入这批
    raise RuntimeError(f"{symbol} 行情未过闸门：{errors[0]['detail']}")
if issues:
    record_quarantine(con, issues)                   # warn：可入库，但登记
```

## 4. 修复 A股成交量单位（根因在数据源/抓取层）
10-08（600150）与 10-16（002185/002192/002747/002837/600522）起成交量约为此前的 80～150 倍。
请确认抓取接口的量单位（手 vs 股），统一后回填历史；完成后 `check_consistency` 应无 `volume_unit_shift`。
另外 10-22 全体标的有一个共同的巨量日，疑似重复累计，需一并核查。

## 5. 无持仓的待执行 SELL（002472 / 002812 / 002639）
建议在 `engine/signals.py` 生成信号时跳过"该账户无该标的持仓"的 SELL，或标记为"无需执行"。

## 6. 改用原子同步
```bash
python3 tools/sync_atomic.py --dry-run
python3 tools/sync_atomic.py
```
默认读取你现有的 `sync_8502_github.py` 里的 `FILES`；清单不全会直接拒绝。
把 `sync_manifest.py` 提示缺的文件补进 `FILES`（至少：`.streamlit/config.toml`、`engine/indicators.py`、
`engine/news_sentiment.py`、`engine/order_validate.py`、`engine/regime.py`、`engine/risk_gate.py`、`engine/screen.py`、
`db/migrations/003_integrity.sql`），或使用 `--auto-list` 由 import 闭包自动生成。
本脚本的 GitHub 写入路径只用假 API 测过，**第一次请先 `--dry-run`**，再观察首个提交。

## 7. 每周恢复演练
```bash
python3 tools/rebuild_from_fills.py
```

## 8. CI
`.github/workflows/ci.yml` 已加入：测试为强制；清单/一致性/重建三项暂为报告性质（`continue-on-error`），
你完成第 3、4、6 步后请删除这三处 `continue-on-error` 行，让它们变成强制门槛。
