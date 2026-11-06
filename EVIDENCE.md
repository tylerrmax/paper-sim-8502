# P3 实时行情心跳监控 — 隔离验证据包

分支：`l2/p3`（基线 `e9583e1`，与生产仓同源）；范围：只开发+测试，未合并/推送/部署，生产仓与生产 DB、生产 reports 零写入。

## 1. 修改文件清单及 Diff 概要

完整 diff：`evidence/full_diff.patch`（646 行）。

| 文件 | 变更 | 概要 |
|---|---|---|
| `engine/heartbeat.py` | 新增 237 行 | 心跳模块：`record()` 只追加 JSONL（默认 `reports/heartbeat.jsonl`，路径可经参数或环境变量 `PAPER_SIM_HEARTBEAT_PATH` 注入）；`check_quotes()` 有效性校验；`status()` 汇总与异常判定。零交易依赖，不被任何交易/信号/风控路径 import。 |
| `engine/realtime.py` | +50/−1 | 仅埋点：新增 `_hb()`/`_hb_check()` 两个观测辅助函数（内部 try/except 吞异常并计数 `_HB_FAILURES`）；`fetch_cn_hk`/`fetch_crypto` 在 request/response/valid/persisted 四阶段调用。取数 URL、请求、解析、回退与返回结构逐字未改（diff 可核）。 |
| `tools/heartbeat_check.py` | 新增 47 行 | 只读 CLI：打印各源四阶段最近时间与异常判定，异常 exit 1、正常 exit 0。本次未接任何生产调度。 |
| `tests/test_heartbeat.py` | 新增 151 行 | 8 项测试（假时钟+假响应，心跳路径注入 tmp）。 |
| `evidence/*` | 新增 | 测试原始输出与本文件引用的证据。 |

## 2. 三阶段落点（读码结论）

`engine/realtime.py` 的唯一调用方是 `app.py` 的 `render_ticker()`（滚动行情条），报价只在内存中展示，**不写 sim.db、不写 cache.db**（这是现行设计，非本次改动）。因此四阶段定义为：
- request：fetch 发起 HTTP 请求（失败记 ok=false 并按原逻辑返回 {}）；
- response：拿到原始响应（HTTP 层成功；内容未验）；
- valid：响应可解析、≥1 个标的价格 >0、源时间戳在 [now−24h, now+5min] 内（A/H 用腾讯时间戳；加密优先用 Binance `closeTime` 校验，缺失时以报价文本无时间戳→接收时间近似，校验结果 detail 注明）；
- persisted：有效报价组装完成并交付调用方（内存）之时。若 valid 判定失败，仍照原样返回取数结果（心跳只告警、不改变取数/回退行为），但不记 persisted。

## 3. 修改前后生产链路影响分析

- 修改前：接口 501/断连时页面靠回退标注兜底，无任何记录能回答"行情多久没有效到达过"。
- 修改后（若未来获批发布）：每次展示取数多写数行 JSONL（单行约 150 字节）；取数结果、页面回退行为、信号/撮合/账本/门禁完全不变；心跳写失败被吞掉并计数，取数照常（测试⑥实证）。
- 本次未发布：生产链路实际影响为零。

## 4. 测试原始输出

- 专项：`evidence/test_heartbeat_output.txt` —— **8 passed**（0.12s）。
- 全量回归（`release_gate.default_pytest_cmd`，5 个 --ignore 与门禁同口径）：`evidence/full_regression_output.txt` —— **175 passed / 1 failed**，exit 1。
- 唯一失败 `test_e2e_batch2.py::test_ledger_fingerprint_idempotent`（断言 fills 50 == 基线 51）：已证为**基线克隆自带、与 P3 无因果**——在干净的 `e9583e1` worktree（零 P3 改动）单跑同测试同失败；根因是 git 跟踪版 `db/sim.db` 为 50 笔快照，而已提交基线 json 按生产工作区库（51 笔）更新，克隆环境两者不一致。原始记录：`evidence/baseline_fingerprint_check.txt`。P3 代码不触账本/DB（可由 diff 核），故不以改测试或改基线"制造通过"。

## 5. 故障注入结果（均在 tests/test_heartbeat.py 内）

| 注入 | 结果 |
|---|---|
| HTTP 200 + 垃圾内容 | request/response 记 ok、valid 记失败、无 persisted；周五盘中 status 判 cn_hk 异常 ✅（证明 200≠有效） |
| HTTP 200 + 价格为 0 | valid 失败 ✅ |
| 源时间戳为 2099 年（未来） | valid 失败且 detail 标注"源时间戳在未来"；fetch 仍原样返回报价（行为不变） ✅ |
| 凌晨（窗口外）无 valid | cn_hk 不报异常 ✅；加密 valid  stale 31 分钟 → 异常、20 分钟 → 正常 ✅ |
| 心跳写盘抛异常（模拟磁盘满） | fetch 正常返回报价，`_HB_FAILURES` 计数增加 ✅ |
| JSONL 追加 | 旧记录逐字保留、只增不改 ✅ |
| CLI 退出码 | 异常 exit 1、正常 exit 0 ✅ |
| 真实路径附带证据 | 全量回归中 AppTest 真实调用 `fetch_all` 在本机网络环境下产生 45 条心跳事件：cn_hk request ok 9 / request 失败 9（接口不可达，与已知 501/断连现象一致）、crypto response 失败 9、valid 失败 9、无 persisted——阶段记录与故障形态吻合（该运行时文件已删除，计数在此留档） |

## 6. 回滚方案

本分支未发布，回滚 = 不合并即可，生产零影响。若未来发布后需回滚：revert 本分支提交即可——heartbeat 为纯新增模块，realtime 埋点为附加调用，revert 后取数行为与现行版本逐字一致；已产生的 `reports/heartbeat.jsonl` 为只追加日志，删除无副作用。

## 7. 验收五项

- 检查对象：engine/heartbeat.py（新增）、engine/realtime.py 埋点、tools/heartbeat_check.py、tests/test_heartbeat.py。
- 实际证据：专项测试原始输出、全量回归原始输出、基线同失败对照记录、完整 diff（均在 evidence/）。
- 复核方法：假时钟+假响应注入六类场景；干净基线 worktree 对照单跑失败测试；diff 逐行核对取数逻辑未动。
- 结论：**已验证通过**（P3 模块本身）。全量回归的 1 项失败为基线环境问题（见 §4），不属本模块缺陷，如实登记、不掩盖。
- 剩余风险：①异常判定未接交易日历，节假日 A/H 可能误报异常（只告警，代价可接受）；②JSONL 无轮转，长期高频运行需后续定轮转策略（属运维项，本次未做）；③加密源文本时间无时间戳，closeTime 缺失时以接收时间近似，极端情况下源端延迟无法被时间戳校验发现（价格有效性仍校验）；④CLI 未接告警调度（按执行令本次不接），异常发现仍靠人工运行 CLI 或后续立项接入。
