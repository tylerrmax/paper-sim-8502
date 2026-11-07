# EVIDENCE — 第二层 P2：Cloud 数据过期强提示

- 分支：`l2/p2`（基线 `e9583e1`，与生产仓同源的隔离克隆）
- 代码提交：`3d8c0ad`（本文件与 evidence/ 为其后的证据提交）
- 生产仓 `~/workspace/paper-trading-sim`：全程只读，未写入、未合并、未推送、未部署。

## 1. 修改文件清单及 Diff 概要

| 文件 | 变更 | 说明 |
|---|---|---|
| `engine/page_freshness.py` | 新增 156 行 | 五态新鲜度纯函数模块（无 streamlit 依赖、无 IO） |
| `app.py` | +34 / −3（页眉一处） | 仅改标题下「数据日期」块：判定调用 + 黄/红条渲染 + 休市小字；其余页面与计算零改动（完整 diff 见 `evidence/app_py.diff`） |
| `tests/test_page_freshness.py` | 新增 209 行 | 固定时钟矩阵 + AppTest 集成/注入测试 |

合计：3 files changed, 399 insertions(+), 3 deletions(-)。

设计要点：
- 五态：`ok` 正常 / `closed` 休市 / `delayed` 延迟（落后 1 个交易日）/ `stale` 过期（落后 ≥2 个交易日）/ `unavailable` 不可用（asof 为 None）。
- expected 定义：交易日 17:30 后→当日；17:30 前→上一交易日；非交易日→最近交易日（17:30 线对齐夜跑 17:12 入库 + 17:30 同步发布的生产节奏）。
- 交易日历默认**复用仓内既有工具** `engine.execution.is_stock_trading_day`（A股口径：周末 + 2026 法定假日，与执行桥/管线同一口径，避免两套日历漂移）；支持注入 `is_trading_day` 函数或 `holidays` 集合（注入假日时为周末规则 + 该集合，完全替代默认假日表）。
- app.py 集成：判定用的数据日期与原页眉同一 SQL（`MAX(date) FROM daily_report WHERE symbol='TOTAL'`）；读不到日期改为走 unavailable 红条而非抛异常；`now` 用固定 UTC+8 换算市场时区（云端服务器为 UTC，中国无夏令时）后再判定 17:30 应到线。ok/closed 保持原小字，closed 时小字追加「· 休市」。

## 2. 修改前后生产链路影响分析

- 第一层生产链（采集→校验→入库→计算→同步→展示的计算部分）：**零影响**。未改 pipeline、signals、execution、risk_gate、screen、账本、DB schema、strategy.yaml、release_gate 与同步脚本。
- 展示层：app.py 仅页眉一块。新增开销为一次原本云端模式下就执行的 `MAX(date)` 查询（现改为无条件执行并 try/except 包裹）；无任何写库动作；本地与云端行为差异仅在横幅是否出现。
- 判定模块为纯函数：不读环境、不写文件、不依赖 streamlit，可独立单测；app 渲染失败路径（DB 不可读）由红条兜底，不新增崩溃面。
- 交易安全边界：横幅只提示，不拦截、不改任何 BUY/SELL/止损/仓位逻辑（买入冻结等既有口径在 engine/data_health 与 risk_gate，未触碰）。

## 3. 测试原始输出位置

| 证据 | 路径 | 结果 |
|---|---|---|
| P2 专项测试（详细） | `evidence/test_page_freshness.log` | **15 passed**（矩阵 11 + AppTest 4） |
| 全量回归（门禁命令） | `evidence/full_regression.log` + `evidence/pytest_cmd.txt` | 182 passed，**1 failed**（见下节归因） |
| 既有失败基线对照 | `evidence/baseline_failure_check.log` | 同一失败在 pristine `e9583e1` 上原样复现 |
| app.py 完整 diff | `evidence/app_py.diff` | 52 行 |

全量回归命令为 `release_gate.default_pytest_cmd` 原样（5 个 `--ignore`，见 pytest_cmd.txt），未删改任何既有测试、未降低标准。

## 4. 失败记录与归因（如实）

1. **全量回归唯一失败**：`tests/test_e2e_batch2.py::test_ledger_fingerprint_idempotent`。
   归因：隔离克隆内**已提交**的 `db/sim.db` 快照为 50 笔成交（asof 2026-10-30），而基线 json 期望活库的 51 笔（#467 在生产活库中，从未提交进 git）。已用 pristine `e9583e1` 工作树（零 P2 改动）复跑同一测试，失败原样复现 → **既有环境性失败，与 P2 无因果**。生产活库下该测试在 P0 恢复时已实测通过（四关全过记录在案）。
2. **P2 专项首轮 1 项失败**（记录未保留，被复跑覆盖，在此如实说明）：首版 AppTest「无误报」用例误设工作区 DB asof=2026-11-06，实际为已提交旧快照 2026-10-30，应用据此**正确**渲染了 stale 红条（落后 5 个交易日），断言不成立。已改为「渲染与判定一致性契约」用例 + 注入 ok 的无误报用例；首轮现象本身即页面层事故复现证据（见下节）。

## 5. 故障注入结果

- **单元级事故复刻**：asof=2026-10-30、now=2026-11-06（周五）18:00 → `stale`，lag=5 个交易日，与 2026-11-02~06 真实停滞天数一致（`test_incident_replica_five_trading_days_stale` 通过）。
- **AppTest 注入 stale**（monkeypatch 判定→stale）：页面真实渲染 `st.error` 红条且含 asof 日期（通过）。
- **AppTest 注入 delayed**：页面真实渲染 `st.warning` 黄条（通过）。
- **AppTest 注入 ok**：页面无任何新鲜度红/黄条（无误报，通过）。
- **未打桩真实渲染**：工作区 DB 本身就是旧快照（asof 2026-10-30），凌晨实时运行下应用自发渲染红条「数据已过期：页面数据截至 2026-10-30，应到 2026-11-06（已落后 5 个交易日）」——与事故场景等价（`test_apptest_banners_match_classification` 通过）。

## 6. 回滚方案

- 全部改动收敛于单个提交 `3d8c0ad`：`git revert 3d8c0ad` 或直接弃用 `l2/p2` 分支即完整回滚，app.py 页眉恢复 `e9583e1` 原状。
- 无 DB/配置/调度变更，无数据迁移，回滚无副作用；生产从未合入，无需生产回滚动作。

## 7. 剩余风险

1. 默认假日表来自 `engine.execution` 的 2026 年表：2027 年须与执行桥同步更新（单点维护、同一口径）；若未更新，周末规则仍兜底，但节假日会被误判为交易日，可能出现 delayed 误报（偏保守方向，不会把过期误报为正常）。
2. 「新鲜数据端到端无横幅」的真实 DB 证据受限于隔离克隆只有旧快照 DB：ok/closed 渲染路径由注入式 AppTest 覆盖、判定本身由固定时钟矩阵覆盖；待合并后在活库环境复跑一次 AppTest 可补齐该证据。
3. delayed/stale 以**交易日**计：长假期间的停滞不会因自然日拉长而升级 stale（设计口径）；若运营上希望长假后首日更敏感，需另行调整阈值定义。
4. app.py 的 `MAX(date)` 查询现于每次页面渲染执行（原仅云端模式）——单行聚合、毫秒级，无性能风险；DB 彻底不可读时以红条呈现而非报错页（行为变化仅限此极端情形，且符合「不可用强提示」要求）。
