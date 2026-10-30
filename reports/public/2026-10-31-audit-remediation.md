# 8502 机构影子模块及交易安全机制专项整改 · 交付报告（2026-10-31）

总判定（按任务书 §8）：**CONDITIONAL**——P1 三项全部修复并独立测试通过；P2 仅剩一项未完成：云端实际加载版本需用户在公网页面核对新设的「运行版本」标识（本机无法直探 Streamlit Cloud 进程，见 R-5）。其余条件满足前，机构模块维持影子观察、权限不扩大。本次整改不触发因子准入；执行方不宣布最终验收通过，等待独立验收。

## 1. 修改清单

| 文件 | 修改点 | 修改前缺陷 | 修改后行为 | 提交 |
|---|---|---|---|---|
| engine/inst_lhb.py | `_consec` 重写为 `_consec_detail`；`consecutive_detail` 新增 missing_dates/calendar_ok/uncertainty；模块头补数据字典与样本时间口径 | 只按已登记 fetch_days 回溯，未登记交易日被静默跨越拼接，连续天数可虚高且无标记 | 计算前以 market_data A 股日期（或显式传入日历）做差集校验：中部未登记/失败日截断并标 certain=False、给出缺失日期；日历缺失/过期时 calendar_ok=False、结果一律不确定；头部失败/未登记按既有口径以最近成功日为起点并记录缺失 | 6a5991a（见下方提交说明） |
| engine/safety.py | 新增 `atomic_unit`（BEGIN IMMEDIATE / SAVEPOINT 双模式）；`transition` 与 `submit_intent` 的成对写入纳入原子单元 | UPDATE 与 INSERT 两条独立语句、无 commit，autocommit 下可出现状态前进但迁移记录缺失（审计实测复现） | 任一失败整体回滚，不依赖调用方事务；条件 UPDATE+rowcount 并发防护保留 | 6a5991a |
| engine/data_health.py | `expire_queued_buys` 重写 | from_state 固定写字面量 "queued"，真实来源不可复原；无事务 | 逐条读取真实状态、条件 UPDATE 锁定该状态、真实 from_state 落迁移记录、与 INSERT 同处 atomic_unit；并发推进（submitting）自动跳过不留假记录；可作废集合与终态（rejected+原因）不变，不扩大迁移范围；历史错误记录未重写 | 6a5991a |
| tests/test_audit_remediation.py | 新增 24 项测试（P1 三项 + P2-05/07） | 关键缺陷无常驻回归 | 先写测试：旧代码 11 failed/10 passed（证据 prefix-test-run.txt），修复后全过 | 6a5991a + d5b2a6a |
| tools/hash_baseline.py | 新增：hash-spec-v1 复算脚本（只读） | 哈希口径只存在于审计脚本，未固定 | 规范全文写入文件头（见 §3），三表复算与登记基线逐字一致 | d5b2a6a |
| tools/nightly_run.sh | 新增：夜跑原始日志包装 | run_daily 原始 stdout/stderr 未落盘 | stdout/stderr 分文件 + meta（起止/退出码/版本/日期）按运行时刻只追加留存至 hidden_files/nightly_logs；退出码与输出原样透传；日志不可用时响亮 FAIL 但不拖垮任务 | d5b2a6a |
| app.py | 数据健康行下新增「运行版本」只读 caption | 云端实际加载版本无从核对 | 优先 git 检出 HEAD，其次 build_info.json，都无则明确显示「未标记（实际加载版本未验证）」；不改任何交易逻辑 | d5b2a6a |
| sync_8502_github.py | FILES 增补 init_db.py、tools/hash_baseline.py、tools/nightly_run.sh、三套关键测试、build_info.json；推送前生成 build_info.json | 关键测试不在公开仓，公开仓不可复现；无构建标识 | 门禁通过后生成构建标识并随同步推送 | d5b2a6a |
| README.md / .gitignore | 新增「关键测试复现」节；build_info.json 入 .gitignore | — | 环境/命令/依赖版本明示（Python 3.12.3、pytest 9.1.1、`python -m pytest`、tmp_path 独立库） | d5b2a6a |
| cron paper-sim-nightly | 第 1 步改为经 tools/nightly_run.sh 执行 | 无原始日志留存 | 其余步骤与 17:12 调度不变，已 cron.view 复核 | —（调度系统） |
| files/机构因子数据可用性隔离说明-2026-10-31.md | 新增（P2-01） | backfill/forward 口径散落在登记与规格草案中 | 六条强制口径集中成文：分段登记、available_at 事后重构不得冒充、正式研究默认只用 forward、回溯探索单独标识、不改原始数据、缺失≠零 | —（目标文件） |

提交说明（如实披露）：整改执行期间，另一并行工作流（验收报告直发 8502 通道，用户 2026-10-31 另行下令）的两次整树提交 6a5991a（01:41）、d5b2a6a（01:44）将本整改的未提交改动一并扫入主仓。执行方已逐项核验：三引擎文件含全部修复标记、工作区与 HEAD 一致、全量回归在该树上通过（168 passed），内容即为本报告所述版本，未发生改动丢失或混杂。

## 2. 测试清单

- 新增测试：tests/test_audit_remediation.py（24 项）。命令：`.venv/bin/python -m pytest tests/test_audit_remediation.py tests/test_safety_supplements.py tests/test_inst_shadow_chain.py -q` → **54 passed**。原始输出：hidden_files/remediation-20261031/（prefix-test-run.txt 为修复前 11 failed/10 passed 的完整输出）。
- P1-01 八场景全覆盖：正常 3 日（certain=True）、失败日截断、未登记日截断+missing_dates、周末不影响、日历缺失不确定、日历过期不确定、缺口后仅从重确认段恢复、历史缺口不拼接。
- P1-02：autocommit 正常落盘、autocommit INSERT 故障注入回滚、默认连接 INSERT 故障回滚、UPDATE 故障无日志、非法迁移零变化、重复提交单记录、多进程并发（fork 双进程实测：恰一成功、迁移只落一条、主表与日志一致）、混合故障后一致性。
- P1-03：pending_review/risk_check/released 三来源真实 from_state、终态与重复作废不处理、SELL 不受影响、submitting 跳过且无假记录、故障注入回滚。
- P2-05：运行时 SQL 追踪（隔离库，44 条语句原始输出 sql-trace-raw.txt）：写目标 ⊆ 机构三表，五张禁写表零写入且指纹前后不变；SELL 在 BUY 冻结下全流程走通。
- P2-07：固定测试向量 sha256=971491c9…（规范文本独立计算冻结）+ 空表常量 + 生产三表复算与基线全等。
- 全量回归（发布门禁同款命令）：**168 passed**（整改前 144 + 新增 24），原始输出 full-regression.txt。
- 公开仓独立复现：全新 clone + 全新 venv（仅 pip install pytest==9.1.1）在公开仓内运行三套测试 → **54 passed**。
- 生产不变性旁证：生产库（只读）以旧/新两版 inst_lhb 对全部 284 个标的逐一比对连续天数与确定性、shadow_board 全等（差异 0）——修复只在存在缺口/日历问题时改变结果，不影响既有样本。

## 3. 数据安全证据

- 数据库路径：生产 = ~/workspace/paper-trading-sim/db/sim.db；测试 = pytest tmp_path 独立库（新增测试零生产库引用）。整改全程未对生产库执行任何写操作。
- 整改前后对照（hash-spec-v1 复算，baseline-pre.json / baseline-post.json）：

| 检查对象 | 整改前（行数 / sha256） | 整改后 | 复核方法 | 结论 |
|---|---|---|---|---|
| signals | 14 / adb8a7bd… | 14 / adb8a7bd… | SQL 计数 + V1 哈希 | 一致，未动 |
| approvals | 14 / 6f72e595… | 14 / 6f72e595… | 同上 | 一致，未动 |
| fills | 50 / e1160ca8…（Σqty 253102.52734，Σpnl −4323.26，max_id 466） | 完全相同 | 同上 + 指纹 | 一致，未动 |
| trade_intents | 0 / e3b0c442…（空表） | 完全相同 | 同上 | 一致，未动 |
| intent_transitions | 0 / e3b0c442…（空表） | 完全相同 | 同上 | 一致，未动 |
| inst_lhb_daily | 481 / 163d3b0d… | 完全相同 | 聚合复算口径 + V1 哈希 | 一致，未动 |
| inst_lhb_raw | 8980 / f9da0473… | 完全相同 | 原始数据 V1 哈希 | 一致，未动 |
| inst_lhb_fetch_days | 11 / 42c288c3… | 完全相同 | 日期差集（区间内缺口 0） | 一致，未动 |
| db/sim.db 文件 | 2,789,376 字节 / 500e6560… | 完全相同 | 文件 sha256 | 一致，零写 |

- 历史数据改写：无。备份：hidden_files/remediation-20261031/pre/（DB + 六个涉及文件，DB 备份 sha 与生产一致）。
- 私有数据仓（P2-03）：API 下载私有仓 db/sim.db 逐表核验——25 张表行数与 V1 哈希与生产**全部相同**，strategy.yaml 字节一致；文件字节 sha 不同（0eadb41a… vs 500e6560…）的原因已定位：同步脚本对私有仓推送的也是过滤副本（清空 broker_receipts/real_fills 后写出），两表生产侧为空表，故内容零差异、仅 SQLite 物理文件不同。不以文件大小作结论，内容级证据见 private-repo-check.txt。同步规则：私有仓 = 每次 17:30 同步时的过滤副本。

## 4. 部署证据

- 本地 HEAD：d5b2a6a（含整改全部代码，见 §1 提交说明）；整改前基线 HEAD 为 df57741。
- 发布门禁：首次运行被拒绝一次（pytest 失败，exit 3 零推送，已按规落 refusals 日志）——根因经定位为环境问题：/tmp tmpfs 100%（audit2 副本 386MB + pytest 临时目录累积），sqlite 报 "database or disk is full"，与代码无关；清理并把 audit2 副本迁入 hidden_files 保留后，复跑四关全过（smoke ✅ / pytest 168 passed ✅ / 指纹水位 50·466 ✅ / 账本一致 ✅），未使用 --force-bypass。
- GitHub 公开仓：关键文件 blob 与本地逐一相同（app.py、engine/safety.py、engine/data_health.py、engine/inst_lhb.py、三套测试、tools 两件、init_db.py、README.md 共 11 项 SAME）；build_info.json 已推送（commit=d5b2a6a16ce0，built_at 2026-10-31 01:51）。sync_8502_github.py 本身不在同步清单内，公开仓为旧版（与本次功能无关，如实记录）。
- 本地服务：paper-sim-8502 已重启，active，HTTP 200；AppTest 实测「运行版本：d5b2a6a16ce0（来源：git检出）」与「数据健康」行正常渲染。
- 云端实际运行版本：**未验证**。本机访问公网地址仍为 303 跳转登录（数据中心 IP 限制，与既往一致），无法直探 Cloud 进程；页面已具备版本标识（云端将显示 build_info 的 d5b2a6a），待用户打开公网页截图核对后关闭此项。
- 夜间日志：包装与 cron 已就位并通过替身命令实测（stdout/stderr 分离、meta 完整、退出码透传 0/3 两例）；首轮真实夜跑留存待今晚 17:12 后核对。

## 5. 剩余风险

| 编号 | 风险 | 影响范围 | 当前缓解 | 阻断发布 |
|---|---|---|---|---|
| R-5 | 云端 Streamlit 进程实际加载版本未直探验证 | P2-04 关闭标准 | 页面版本标识已上线（build_info= d5b2a6a）；待用户公网截图核对 | 是（CONDITIONAL 的唯一原因） |
| R-6 | P2-06 首轮真实夜跑日志留存尚未发生 | 夜间证据链 | 机制已替身实测；今晚 17:12 首跑后核对 nightly_logs | 否 |
| R-7 | 同步脚本对私有仓推送的也是过滤版 DB：当前 broker_receipts/real_fills 为空表故无差异；若将来两表有数据，私有仓将与生产不一致且云端 _fetch_private_data 读到的是过滤数据 | 私有数据仓一致性（未来） | 本次已内容级核验全等并登记；是否改同步口径（私有仓推未过滤版）待用户裁定，不在本次整改范围 | 否 |
| R-8 | /tmp 为 512MB tmpfs：大型隔离副本/测试临时文件可再次撑满并使门禁 pytest 假失败 | 发布门禁与夜间任务稳定性 | audit2 副本已迁出 /tmp；门禁拒绝会响亮报错不静默（本次即如此暴露） | 否 |
| R-9 | 多进程并发测试为双进程同机同库实测；更高并发/跨机未测 | 状态机并发口径 | 条件 UPDATE+原子单元双保险；意图链未接管主链（R-4 维持原登记） | 否 |
| 既有 | R-1 PAT 轮换 / R-2 异机锚点 / R-3 实时心跳 / R-4 意图链接管 | 见安全补充证据包 | 维持原登记不变；R-4 的前置条件（原子性、来源状态）已由本次 P1-02/P1-03 解决 | 否 |

验收清单对照：A 组（P1）9 项全过；B 组（P2）8 项中 7 项完成、云端版本 1 项未验证；C 组（生产安全）对照表全一致。按任务书 §8 判定 CONDITIONAL，机构因子维持影子观察，等待独立验收。
