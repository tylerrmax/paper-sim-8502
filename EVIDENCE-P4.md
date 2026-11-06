# P4 待办自动隔离 + 公网按钮只读标注 — 证据包

- 分支：`l2/p4`（隔离工作区 `~/workspace/l2-remediation/p4`），基线 `e9583e1`（与生产仓同源）。
- 性质：隔离开发 + 验证完成，**未合并、未推送、未部署、未写生产 DB**；发布另行授权。
- 结论（四词口径）：**已验证通过**（本批范围内；全量回归 1 项失败为基线既有、与本批无关，见 §4）。

## 1. 修改文件清单与 Diff 概要

完整 diff：`evidence/full_diff.patch`（628 行，基线 e9583e1 → 本批）。

| 文件 | 变更 |
|---|---|
| `engine/todo_autoskip.py` | 新增（186 行）。自动隔离规则引擎 + 审计 JSONL 写入，纯 DB 规则，不建表不改 schema |
| `run_daily.py` | +17 行。日批末尾第 7 步挂接自动隔离（try/except 只留 FAIL 行）+ 日报末尾追加一行 |
| `app.py` | +62/−3。交易页新增「已隔离」折叠区（来源区分 + 恢复按钮）；三处写按钮（已建单/跳过/跳过·隔离）加 `disabled=IS_CLOUD`；订单管理区只读态标注 |
| `tests/test_todo_autoskip.py` | 新增（304 行，7 个测试） |
| `evidence/` | 本证据包原始输出（见 §3） |

未改动：db/schema、engine 其余模块（pipeline/signals/execution/order_validate/risk_gate 等）、release_gate、既有测试文件、生产仓任何文件。

## 2. 修改前后生产链路影响分析

**修改前**：待办（待建条件单/待审批信号/风控拦截）由 signals×fills×approvals 联查生成；客观已死的条目（无持仓 SELL、过期信号）每天重复出现，只能人工在本机版逐条点「跳过/隔离」；公网 Cloud 的按钮写的是云端临时库副本，点击不落生产库（生产 approvals 无对应记录，已实测），属假按钮。

**修改后**（发布后才生效，本批未上线）：
- 第一层日批在信号/成交全部落定后多一步自动隔离：仅对「无成交、无任何 approvals 记录、60 日窗口内」的候选按 R1/R2/R3 写 `decision='skipped'`，键法（date=信号日/account/symbol/side）与人工 `_ap_state` 完全一致，各界面计数随之下降；人工已表态（approved/skipped）的条目永不触碰。
- 审计先行：JSONL 落盘成功后才写 DB，日志故障 = DB 零写入（故障注入已验）。
- 交易页多一个只读折叠区（已隔离清单 + 恢复）；公网只读态写按钮禁用 + 明示标注，本机态行为与现状逐字一致（`disabled=False`）。
- 对信号生成、撮合、订单校验、风控拦截规则、账本、门禁：**零改动**。隔离动作不删除任何 signals/fills/blocked_signals 记录，恢复只需删一条 approvals 行。

## 3. 测试原始输出位置

| 证据 | 文件 |
|---|---|
| 本批 7 个测试全量输出 | `evidence/test_todo_autoskip_full.log`（7 passed in 50.38s） |
| 发布门禁命令全量回归原始输出 | `evidence/full_regression.log`（174 passed, 1 failed，见 §4） |
| 基线既有失败复现证明 | `evidence/baseline_failure_proof.log` |
| 生产库影子演算（dry-run）清单 | `evidence/shadow_run.txt` |
| 完整 diff | `evidence/full_diff.patch` |

测试覆盖：R1/R2/R3 命中集合精确断言；有持仓 SELL、2 日内 BUY、最新信号、已成交信号、人工 approved 五类保留断言；approvals 写入集合与 JSONL 三行内容（规则号 + 证据字段）精确匹配；AppTest 双态（本机：计数下降 −3/−2、已隔离区可见且含「自动隔离·R1」、恢复/跳过按钮可用、无只读标注；公网：只读标注出现、全部写按钮 disabled）。

## 4. 失败记录（如实）

全量回归唯一失败：`tests/test_e2e_batch2.py::test_ledger_fingerprint_idempotent`，断言 `fills rows 50 == 51`。根因：本隔离工作区的 git 跟踪版 db/sim.db 是 2026-10-30 时点副本（50 笔），而基线文件（随 e9583e1 祖先提交 6de332f 更新）对应的是生产活库（51 笔）。已在零改动的 pristine e9583e1 worktree 上复现同一失败（`evidence/baseline_failure_proof.log`），证明与本批改动无关；该测试在生产活库口径下为通过（P0 恢复时生产门禁 168 passed 已实证）。本批未为迁就它改任何基线或测试。

## 5. 幂等、故障注入、回退验证

- **幂等**：夹具 DB 连跑两次，第二次返回 []、approvals 与 JSONL 逐字不变（`test_idempotent_second_run_writes_zero`）。
- **故障注入**：审计 JSONL 路径不可写时 `run_autoskip` 抛错且 approvals 零写入（`test_log_failure_leaves_db_untouched`）；dry-run 模式零写入且不创建日志文件（`test_dry_run_writes_nothing`）。
- **夜跑回退**：挂接点为 try/except 包裹的末尾步骤，异常只打 FAIL 行，日批主链产物（日报/账本）不受影响——与既有第 4–6 步同一降级模式。
- **业务回退**：任一被隔离条目可在交易页「已隔离」区一键恢复（删 approvals 行）；整体回滚 = 还原 approvals 中本批写入行（JSONL 逐条可定位）或不发布本批代码。

## 6. 影子演算（生产库只读副本，dry-run）

源库 SHA256 与副本一致（f54a2e96…），全程只读，演算后副本 approvals skipped 仍为存量 3 条（零写入）。将被隔离 **3 条**：

| 条目 | 规则 | 证据 |
|---|---|---|
| 300827 SELL 2026-11-05 @29.40 | R1 | 现持仓 0、信号日时点未平仓 0 |
| 688305 SELL 2026-10-30 @42.30 | R1 | 同上 |
| 002639 BUY 2026-10-27 @14.27 | R2 | 交易日龄 8 > 5、从未成交 |

两条 BUY 的结论（按任务要求单列）：600150 BUY 2026-11-02 与 688305 BUY 2026-11-02 交易日龄均为 **4 日**，未越 5 日线，**本次不隔离**；若仍未成交，2026-11-10 夜跑时龄达 6 日将按 R2 自动隔离。600150 SELL 2026-10-30 已有成交 #467，非待办未触碰；其余历史信号均已有人工 approvals 记录，未触碰。

## 7. 回滚方案

- 代码回滚：本批为单分支单提交，不发布即无生产影响；发布后回滚 = 回退该提交（app.py/run_daily.py 还原 + 删除 engine/todo_autoskip.py），无 schema 与数据迁移。
- 数据回滚：自动隔离写入的 approvals 行可按 auto_skip_log.jsonl 逐条删除恢复；JSONL 本身只追加，可整表归档。
- 夜跑回滚：移除 run_daily 第 7 步即回到现状，其余步骤不受影响。

## 8. 剩余风险

1. **待审批计数（界面口径 B）的既有联结问题（本批未动，仅登记）**：工作台「待审批信号」计数按 `f.date = s.date` 联结 fills，而成交执行日在信号日之后，故已成交信号仍可能被计入该计数（夹具 S7 实测被计入）。自动隔离按 fills.signal_date 联结判定「已成交」（与待建/拦截口径一致），不会误隔离已成交信号；但该计数本身的口径偏差属既有行为，建议另行立项核查，不在本批范围。
2. R2 的交易日龄以 market_data 指数序列计数；指数数据断更时龄会低估（宁少隔离、不多隔离的方向），且有 A 股日历回退口径。
3. AppTest 的只读态通过注入 `STREAMLIT_CLOUD=1` 覆盖谓词与渲染分支；真实 Cloud 的 HOSTNAME 判定路径未在云端实测（与既有 IS_CLOUD 判定同一表达式，风险低）。
4. 「恢复」按钮删 approvals 行后，若该信号已被管线按新状态处理（如已过期），它不会回到任何界面窗口（窗口外），属无害不一致，界面与计数口径一致。
