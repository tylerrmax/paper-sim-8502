# UI 定点批（关注去重 + 06 验证修复）— 证据包

- 分支：`ui/focus-verify`（隔离工作区 `~/workspace/l2-remediation/ui1`），基线 `afff34e`（当前生产 master）。
- 性质：隔离开发 + 验证完成，**未合并、未推送、未部署、未写生产 DB**（测试用库为复制进工作区的生产库副本，提交前已 `git checkout -- db/sim.db` 还原，提交不含 db 变化）。
- 用户确认口径（2026-11-08）：五页（交易/账户/选股/机构/验证）取消关注子集表、工作台维持方案A、「全部行情」为唯一独立列表、关注标的保留「待建单」标记；任务书原「保留增删管理功能」假设作废——全仓无自选增删 UI，关注集为系统派生（待建单 ∪ 持仓），无管理功能需保留。
- 修改文件：仅 `app.py`（+40/−19，全部展示层）+ 新增 `tests/test_ui_focus_verify.py` + 本证据包 `evidence/ui1/`。未动 engine/、config、run_daily、sync、release_gate、既有测试文件、reports/public/*.md。

## 验收焦点 1 · SQL 修复：三处查询字段与真实 Schema 一致、7 条记录正常读取

**根因（实测）**：`risk_block_events` 真实 schema（`engine/safety.py` 建表语句 + 生产库 `PRAGMA table_info` 双证）无 `rule` 列（实为 `rule_name`）、无 `date` 列（实为 `trade_date`）、无 `id` 列。app.py 三处展示查询引用了不存在的列，SQLite 抛 `OperationalError`，均被 except 吞掉。此前流传的「pandas DataFrame.append」说法与任何代码路径不符，作废。

修复前在本机对生产库副本逐条复现（三条全部 FAIL）：

```
FAIL: OperationalError no such column: rule    （验证页拦截证据，原 app.py:3133）
FAIL: OperationalError no such column: date    （工作台优先级队列，原 app.py:1773）
FAIL: OperationalError no such column: date    （工作台「最近拦截」，原 app.py:1886）
```

修复（三处均为最小字段对齐，排序键 `id DESC` 同为不存在列，改为同表 `ts DESC` 保持「最新在前」原语义）：

| 位置 | 修复前 | 修复后 |
|---|---|---|
| 验证页拦截证据 | `... symbol, side, rule, reason FROM risk_block_events ORDER BY ts DESC` | `rule` → `rule_name` |
| 工作台优先级队列 | `SELECT date, symbol, reason ... WHERE rule='env_gate' ORDER BY date DESC, id DESC` | `trade_date` + `rule_name='env_gate'`，`ORDER BY trade_date DESC, ts DESC` |
| 工作台最近拦截 | `SELECT date, symbol, reason ... ORDER BY date DESC, id DESC` | `trade_date`，`ORDER BY trade_date DESC, ts DESC` |

修复后读取结果样本（生产库副本，表内共 7 条，验证页按 ts 倒序取 8 条即全部；页面渲染见焦点 2 截图与 after 转储）：

```
2026-11-03 · 688305 BUY · ban_entry_stop_ref · 跌破母板止损参考：688305 判定价 42.53 < stop_ref 52.50（禁入优先，buyback 不例外）
2026-11-03 · 600150 BUY · ban_entry_stop_ref · 跌破母板止损参考：600150 判定价 34.11 < stop_ref 36.50（禁入优先，buyback 不例外）
2026-10-16 · 002446 BUY · ban_entry_stop_ref · 跌破母板止损参考：002446 判定价 6.93 < stop_ref 7.70（禁入优先，buyback 不例外）
2026-10-16 · 600150 BUY · ban_entry_stop_ref · 跌破母板止损参考：600150 判定价 35.45 < stop_ref 36.50（禁入优先，buyback 不例外）
2026-10-19 · 002472 BUY · ban_entry_stop_ref · 跌破母板止损参考：002472 判定价 32.94 < stop_ref 33.50（禁入优先，buyback 不例外）
2026-10-20 · 688305 BUY · ban_entry_stop_ref · 跌破母板止损参考：688305 判定价 44.81 < stop_ref 52.50（禁入优先，buyback 不例外）
2026-10-28 · 002639 BUY · env_gate · 市场环境闸·禁止新开仓：…上证未站上20日线…B31=否…无可用换仓额度券…
```

同批连带修复效果（工作台，此前从不显示）：优先级队列出现 P1 行「风控拦截：002639 雪人集团 BUY 被环境闸拦截（2026-10-28）」；今日执行卡出现「最近拦截：688305 …（2026-11-03）」。工作台 before/after 渲染转储 diff 恰为这 3 行，别无他变（`evidence/ui1/before|after/🏠_工作台.txt`）。

## 验收焦点 2 · UI 结果：六页无关注子集表、全部行情正常；06 验证三项

**任务一**：共享行情区删除五页的关注子集表渲染（原 `if _focus and not _wb_first` 分支），全部页面统一为工作台原有的一行 caption「关注标的行情已收纳至下方「全部行情」（工作台首屏减负 · 方案A）」（用词与基线工作台逐字一致，工作台渲染零变化）。`_focus` 派生计算、持仓查询失败告警横幅、「📊 全部行情」折叠区（关注标的在其中带「待建单」标记）保持原样；释放空间不新增内容。
验证：AppTest 六页转储（before/after）——before 五页含「A股 · 关注」聚合表、after 六页 `grep "· 关注"` 命中 0；六页均有「📊 全部行情（共 17 只）」expander。截图 `shots/before_trade.png`（关注表 4 行实景）→ `shots/after_trade.png`（表消失、caption 一行、全部行情保留、订单管理上移）。

**任务二（06 验证）**：
- (a) 拦截证据：见焦点 1；after 截图 `shots/after_verify_check.png` 显示 7 条记录逐行渲染，不再出现「拦截记录读取失败」（before 同位截图为该失败文案）。
- (c) 检查表新增「同步」行：插于「行情新鲜度」之后，六项顺序 = 账本指纹 / 行情新鲜度 / 同步 / 选股运行 / 机构抓取 / 数据健康。数据源只用 `build_info.json`（同步脚本生成，含 `built_at` 与 `commit`，与页面运行版本标识同源；注：页眉「同步于」在云端取的是 db mtime `CLOUD_DATA_ASOF`，非同一字段，本行按任务口径采用 build_info）。当前值 = `2026-11-07 23:44:17 · afff34e`，结论 = 文件存在且可解析为「通过」、否则「待核验」+「无记录」，不编造。
- (d) 历史报告归档：去掉平铺小标题「📄 验收报告」，整体收进默认折叠的单个 expander「📄 历史验收报告（归档 · N 份）」（N 按 `reports/public/*.md` 实际计数，本工作区为 1 份），内层每份报告原 expander 与全文保留；`reports/public/` 文件零增删改。截图 `shots/after_verify_archive.png`。
- (e) 文字精简审阅结论：通读验证页全部渲染文本（清单见下），可确证属「重复介绍/历史背景/低价值说明」且不承载状态值者，仅有已随 (d) 移除的平铺小标题；其余候选逐条审阅后保留，其中 3 条列「保留待议」，不在本批擅删：
  - 保留待议 1：单账户详情下「毕业标准：期望值>0 · 盈亏比≥2.5（扣费后） · 最大回撤≤10% · 样本20-30笔 · 执行分≥90 · 零数据事故 · 股票与加密分开算」——7 分项中 4 项与下方 KPI 卡硬线标签重复，但「执行分≥90 / 零数据事故 / 股票与加密分开算」全页仅此一处，删之即丢标准信息。
  - 保留待议 2：审计 expander 内「成交保留，仅不计入毕业指标：」——证据条目的语义说明，非介绍文字。
  - 保留待议 3：浮亏提示 caption 尾注「（仅提示浮亏，不作止损判定）」——告警口径澄清，防误读为止损判定。
  - 审阅保留（状态值本身，非说明文字）：多重校验绩效 caption（样本外绩效/积累中两态）、各空态文案（暂无报告/暂无拦截记录/无剔除记录）、各错误兜底 caption（读取失败类，属告警）。

## 验收焦点 3 · 回归：原有五页功能不受影响

全量门禁（`release_gate.default_pytest_cmd`，5 个 --ignore）原始输出：`evidence/ui1/full_pytest.log`（本批全量首跑）、`evidence/ui1/baseline_full_pytest.log`（基线 afff34e 同命令对照）、`evidence/ui1/tail_pytest.log` / `baseline_tail_pytest.log`（尾部组合在本批与基线的同位停滞对照）。

- 基线（afff34e 克隆 + 生产库副本）：**202 passed / 3 failed**，失败仅 `test_weekly_consistency.py` 3 项（克隆/数据口径既有失败，与本批无关）。
- 本批首跑：**209 passed / 7 failed**。多出的 4 项（test_todo_autoskip ×2、test_topboard_validation_grp ×2）经查为环境伪影：该跑执行期间 /tmp 被打满至 100%（本批取证的 pytest 临时库副本 + chromium 首次下载失败残包），失败点在 fixture `shutil.copy2` 的 OSError（ENOSPC），原始日志可查。清理 /tmp 后同 3 文件单独复跑 = **9 passed / 仅 weekly 3 failed**，与基线逐项一致。
- 通过数口径：基线 205 项（202+3）→ 本批 216 项（+11 专项），不得减少项已满足；失败集与基线一致（weekly_consistency 3 项既有失败）。
- 交易/风控/撮合/账本/engine/config/run_daily/sync 零改动（见完整 diff），生产数据链不受影响。

- 尾部文件集复跑的环境现象（如实记录）：当晚稍后以「sync_retry → todo_autoskip → topboard → weekly」组合复跑时，两次都在第 12 个测试点后停滞（todo_autoskip 的 AppTest 项）直至超时；**同一组合在基线 afff34e 工作区复跑出完全相同的停滞**（`tail_pytest.log` 与 `baseline_tail_pytest.log` 均为 12 个点后无输出），证明该停滞是既有测试组合与当晚环境的交互现象，与本批改动无关；本批全量结论以上述已完整跑毕的首跑 + 基线全量 + 文件级等价复跑为准。

## 验收焦点 4 · 证据完整性

- 完整 Diff：`evidence/ui1/full_diff.patch`（app.py，基线 afff34e → 本批，118 行，+40/−19）。
- 专项测试：`tests/test_ui_focus_verify.py`（11 项：六页无关注子集表题 ×6、caption 与全部行情含持仓、验证页拦截证据渲染、工作台两处拦截行恢复、检查表六项含同步行且位置/证据来源正确、报告归档 expander 与份数/原 expander/全文保留），原始输出 `evidence/ui1/test_ui_focus_verify.log`：**11 passed**。
- 修改前后截图（playwright + 本地双实例，基线实例为同仓 afff34e 临时 worktree、同份库副本与 build_info）：`evidence/ui1/shots/` 共 7 张——before/after ×（交易页顶部、验证页顶部、验证页检查表区）+ after 验证页归档区。另有 AppTest 六页渲染文本转储 before/after 各 6 份（`evidence/ui1/before/`、`evidence/ui1/after/`，生成脚本 `evidence/ui1/apptest_dump.py`）作为逐页文本级证据。
- 环境备注：playwright 首次装 chromium 时 /tmp（512M tmpfs）被 pytest 临时文件占满致 ENOSPC 失败，清理并改 TMPDIR 至工作区后下载成功；此事亦为焦点 3 中 4 项伪影失败的根源，已如实记录。

## 最终 Commit

- 分支 `ui/focus-verify`，代码+测试+证据提交 SHA：见本文件入库后 `git log`（交付回报中给出最终 HEAD）。**未合并、未推送、未部署。**

## 剩余风险

1. 检查表「同步」行在本地/无 build_info.json 环境显示「待核验 · 无记录」（设计内 fail-closed）；生产由同步脚本每次生成该文件，Cloud 侧随 FILES 清单同步，口径已核。
2. 报告归档使用 expander 嵌套（外层归档 + 内层各报告），已在当前 Streamlit 版本（本地 .venv）以 AppTest + 真实浏览器双证可渲染；若未来 Streamlit 大版本禁用嵌套 expander，该区会报错——升级 Streamlit 时需回归此项。
3. `test_weekly_consistency.py` 3 项既有失败（基线同失败）未在本批处理（不在范围）。
4. 验证页「账本指纹」行在本工作区显示「待核验」：工作区 `.release_gate_state.json` 与复制库口径差异所致，before/after 一致，非本批引入；生产口径以发布门禁水位为准。
