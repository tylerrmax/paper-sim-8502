# P1 自动重试机制 — 隔离验证据包

- 分支：`l2/p1`（基线 `e9583e1`，与生产仓同源的隔离克隆）
- 工作区：`~/workspace/l2-remediation/p1`
- 日期：2026-11-07
- 范围：仅开发 + 测试 + 留证。未合并、未推送、未部署，未改生产调度，未碰生产 DB。

## 1. 修改文件清单与 Diff 概要

| 文件 | 变更 |
|---|---|
| `sync_8502_github.py` | 唯一生产代码文件。+215/−3（完整 diff：`evidence/change.diff`） |
| `tests/test_sync_retry.py` | 新增，7 项故障注入/分类测试 |
| `evidence/` | 本证据包原始输出（baseline/full 两轮 pytest 日志、新测试日志、门禁接线测试日志、change.diff） |

`sync_8502_github.py` 具体改动：
1. 导入改名：`from gh_api import api` → `from gh_api import api as _api_transport`（传输本体不变）。
2. 文件头 docstring 增加 P1 重试口径说明（3 行）。
3. 新增「P1 自动重试」节（约 190 行）：错误分类 `_classify_error`、指数退避 `_backoff_delay`、通用重试 `_call_with_retry` 与同名包装 `api()`、PUT 幂等核对 `_put_file_contents` / `_remote_blob_sha`、只追加 JSONL 留痕 `_log_retry_event`，以及注入点 `_sleep` / `_RETRY_LOG_PATH` / `_api_transport`。
4. `push_file` 的 PUT 一行改为调用 `_put_file_contents(path, repo, data, lsha)`，核对确认时打印专门提示；其余逻辑（先 GET 比对 blob sha、无变更跳过、回执过滤副本）逐字未动。

## 2. 落点选择（任务要求二选一并说明理由）

**选：sync 的推送调用层**（包装 sync 内的 `api` 调用），不改 gh_api 传输层。理由：
- `gh_api.py` 位于 `~/workspace/custom-connectors/custom.github/bin/`，是 custom.github 连接器的共享基础设施，不在本仓库版本控制内、多方复用；在隔离分支里改它既无法纳入本分支的 diff/回滚，也会把重试策略泄漏给其他调用方。
- 可重试/不可重试的分类、退避节奏、留痕格式、PUT 幂等核对都属于「同步」这一业务的策略，应与同步脚本同版本、同评审、同回滚。
- gh_api.api 本身保持哑传输（urllib，30s 超时，HTTP 错误抛 HTTPError、连接类抛 URLError/ConnectionError），恰好为调用层分类提供完整异常信息，无需改动。

## 3. 实现口径

- **可重试**：URLError / ConnectionError / TimeoutError / http.client.HTTPException（含 IncompleteRead、RemoteDisconnected）→ 类别 `connection`；HTTP 500/502/503/504 → `http_5xx`；HTTP 429（无终止语义）→ `http_429`。
- **不可重试**：HTTP 401/403/404/422 等确定性错误；**限流终止**——正文含 `rate limit` 或响应头 `X-RateLimit-Remaining: 0`（含 GitHub 以 403 返回的限流）→ 类别 `rate_limited`（依据：配额耗尽/滥用检测窗口内重试无意义且可能延长封禁，这是 429/403 中唯一被明确要求按终止语义处理的一类）；重定向拒绝等确定性 RuntimeError、本地文件/序列化错误、其余一切未分类异常 → `non_retryable`，fail-closed。
- **退避**：总尝试 ≤4 次（首试 + 3 次重试），间隔 20s→60s→180s + ≤10% 随机抖动；睡眠经 `_sleep` 注入，测试零真睡。
- **门禁关系**：release_gate 在任何 API 调用之前把关，GateRefusal/exit 3 不经过本层；分类器对 GateRefusal 的判定实测为 `non_retryable`（测试 ii 断言）。代码中不存在、也不新增任何 `--force-bypass` 路径（该开关的既有实现未触碰）。
- **留痕**：只追加 `reports/sync_retry_log.jsonl`（路径可注入），事件 `attempt_failed` / `retry_success` / `retry_exhausted` / `idempotent_reconciled`，字段含 ts、method、api_path、file、repo、attempt、category、error（截断 300 字）、delay_s、will_retry。写日志异常被吞掉，绝不反噬同步；耗尽后抛原异常，cron 既有失败告警通道照常触发。

## 4. 修改前后对生产链路的影响分析

- **修改前**：任一 API 调用遇连接重置/超时即抛错，整个同步当晚失败（这正是 P0 期间 VM→GitHub 连接频繁 reset 时的脆弱点之一；门禁拒绝是另一回事，不在本项范围）。
- **修改后（若获批上线）**：GET/PUT 遇临时故障自动退避重试，多数单次抖动当晚自愈；PUT 结果未知时先核对远端 sha，避免重复提交产生多余 commit；确定性错误与门禁拒绝行为与修改前完全一致（立即失败/拒绝、零推送语义不变）。
- **不改变**：release_gate.py（零改动，规则与阈值原样）、engine/、app.py、db schema、策略、账本、FILES 推送清单、推送内容（含 db 过滤副本口径）、水位线推进逻辑、cron 调度。
- **新增运行时行为**：同步目录下多一个只追加日志文件 `reports/sync_retry_log.jsonl`（不在 FILES 推送清单内，不会同步上 GitHub）；最坏情况下单文件推送耗时增加约 4×30s 超时 + 260s 退避 ≈ 6.5 分钟（仅当故障持续且最终仍失败时）。

## 5. 测试与故障注入结果（原始输出位置）

| 证据 | 位置 | 结果 |
|---|---|---|
| 改动前全量基线 | `evidence/baseline_pytest.log` | 167 passed，1 failed（见 §6 既存项） |
| 新测试 tests/test_sync_retry.py | `evidence/test_sync_retry.log` | **7 passed**（0.18s，无真睡） |
| 门禁接线回归 tests/test_release_gate.py | `evidence/test_release_gate.log` | 14 passed |
| 改动后全量回归（门禁 pytest 命令，5 个 --ignore） | `evidence/full_pytest.log` | **174 passed，1 failed**（同一既存项，零新增失败） |

故障注入逐项（均在 `tests/test_sync_retry.py`）：
- (i) 两次 ConnectionReset 后成功：共 3 次传输调用、2 次退避睡眠、日志 2 条 attempt_failed（will_retry=true）+ 1 条 retry_success ✔
- (ii) HTTP 403：恰好 1 次调用、零睡眠、立即抛错，日志 will_retry=false；GateRefusal：恰好 1 次调用、类别 non_retryable ✔
- (iii) PUT 已生效但响应丢失：PUT 调用恰好 1 次，GET 核对发现远端 sha 已等于目标 → `idempotent_reconciled` 留痕并判成功，不再 PUT；对照用例（PUT 未生效）→ 核对不匹配后正常重试 PUT 并成功 ✔
- (iv) 持续 ConnectionReset：4 次调用后抛原异常，日志含 attempt=4 的 retry_exhausted ✔
- (v) 退避断言：三次睡眠严格单调递增（20/60/180 基准），最大 ≤198s（180×1.1 封顶）✔
- (vi) 分类口径：503 可重试、404/422 不可重试；429 正文含 rate limit → rate_limited 不可重试；403 正文含限流语义 → 不可重试；X-RateLimit-Remaining: 0 → 不可重试；普通 429 → 可重试；ValueError/GateRefusal → non_retryable ✔

**幂等验证**：即 (iii) 两用例——结果未知不重复 PUT、未生效才真重试；叠加 push_file 原有「先比对 sha 后推送」语义（未改动，门禁接线测试 14 项全过含推送桩流程）。

## 6. 既存失败说明（非本项引入）

`tests/test_e2e_batch2.py::test_ledger_fingerprint_idempotent` 在本隔离克隆中改动前后均失败（基线日志与改动后日志各一份，断言同为 fills rows 50 == 51）。根因：git 跟踪的 `db/sim.db` 是 50 笔旧版，生产活库 51 笔是生产仓未提交的工作区状态，克隆天然不含——属克隆环境产物，与本项改动无关（父级已在含活库的副本中验过该测试通过）。本项未以任何方式掩盖或绕过该失败。

## 7. 回滚方案

- 本分支仅 1 个提交、仅动 1 个生产文件：回滚 = 不合并该分支（生产零影响）；若未来合并后需回退，`git revert` 该提交即可，`api()` 恢复直通传输，行为回到现状。
- 运行时残留仅 `reports/sync_retry_log.jsonl`（只追加日志，可直接删除，无状态依赖）；无 DB 变更、无 schema 变更、无配置变更需要回滚。

## 8. 剩余风险

1. **最坏时延**：持续故障时单文件最多多耗约 6.5 分钟才失败；若多个文件连续抖动，整晚同步总时长拉长（仍远小于当晚整体失败的代价），且 cron 超时设置未在本项评估内——上线前建议核对 push-8502-github-sync 的 timeout_secs 是否覆盖最坏情形。
2. **429 分类是启发式**：以正文 `rate limit` 字样与 `X-RateLimit-Remaining: 0` 判定终止语义；GitHub 若以其他措辞返回软限流（如无正文的 429），会被当作可重试——退避 20s 起步、总尝试 4 次，风险有界但非零。
3. **核对 GET 本身失败时**：`_remote_blob_sha` 返回 None 并继续正常重试流程（可能多发一次 PUT）；GitHub 内容 PUT 带原 sha 前置条件，重复 PUT 最坏产生一次内容相同的冗余提交，不会损坏数据。
4. **日志增长**：sync_retry_log.jsonl 只追加不轮转；按每晚至多几次故障事件估算，年增量为 KB 级，可接受，未做轮转。
5. 本项未在真实 GitHub 端点做在线验证（隔离边界禁止真实推送）；分类所依据的异常类型来自 gh_api 的 urllib 实现与既有故障记录（Connection reset 实测频发），上线后首晚需按既定观察方式核查其日志。
