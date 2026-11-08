# 测试环境修复批 · 交付证据（env/test-env-fix）

基线：23c812b（UI 定点批，已于 2026-11-08 17:30 随成功同步上线，远端 build_info commit=23c812b）。
回滚点：afff34e（保持不变）。修复提交：1ceb970（仅 2 文件 +5/−1）。

## 修改内容（用户批准的唯一范围）
- tests/test_todo_autoskip.py：_build_app_tree 的 copytree 忽略名单加入 "pytest-of-root"、".tmp_pw"。
- .gitignore：加入 pytest-of-root/、.tmp_pw/ 两行及注释。
未动：生产交易/风控/账本/行情采集/夜跑/同步逻辑；未调门禁超时；未删改任何测试标准；未重构其他测试。

## 过程纠偏记录（如实）
本批最初由主助手在用户点选选项后直接落于生产 master（1ceb970）并启动一次同步；用户随后下达隔离分支正式规格，主助手立即中止同步（门禁阶段终止、零推送，远端 build_info 仍 23c812b），将提交整体移入隔离分支 env/test-env-fix，生产 master 复位 23c812b 后按规格重走全流程。

## 隔离验证（工作区 l2-remediation/envfix，基线 23c812b + 生产 db/state 副本，测后已还原）
- 全量 pytest（门禁同款命令，5 个 --ignore 不变）：**216 passed，250.94s**，原始日志 evidence/envfix/pytest_full.log。/tmp 起 2%→收尾 26%（单轮 pytest-of-root 124M），/var/tmp 0%，最大单测目录 18M，树内无嵌套 pytest-of-root/.tmp_pw。
- 完整发布门禁 run_gate（隔离区）：**ok=true**——smoke 17 模块 import OK；pytest 216 passed 257.95s（第二次全量，耗时与首跑一致）；fingerprint 水位 51/467 新增 0；ledger positions==fills。原始日志 evidence/envfix/gate_full.log（首次因隔离区缺未跟踪的 data/ 包冒烟失败，补拷生产 data/ 后重跑，即 gate_full2.log 为准）。
- 专项回归：tests/test_todo_autoskip.py 7/7（修复直测，生产树预跑 67s）；tests/test_ui_focus_verify.py 11/11 已于 UI 批亲验，本批未改其代码路径。
- 膨胀机制调查：evidence/envfix/bloat_mechanism_notes.md（已证实：干净环境足迹有界 + 仓根残留经 copytree 反馈放大 + 并发/孤儿进程叠加；未证实：残留首次落入仓根的路径、.tmp_pw 最初产生者）。

## Cloud 验收（针对已上线 23c812b，2026-11-08 21:17–21:19 只读实测）
- 构建信息=23c812b36486…；页眉 git 检出=8874e18fa035（同步推送提交，双字段口径，11-07 已澄清）；数据日期 2026-11-06、同步于 11-08 09:34。
- 验证页「异常与拦截证据」7 条全读出（6 ban_entry_stop_ref + 1 env_gate），无读取失败；工作台两处摘要拦截正常显示（修复前被静默吞掉处）。
- 六页无「·关注」子集表；「全部行情（共 17 只）」逐页正常；验证页六项检查表齐（账本指纹/行情新鲜度在 Cloud 为「待核验」，依赖本地水位文件、不编造，设计口径）；历史报告归档折叠 4 份。

## 剩余风险
- 全量 251–258s 距门禁 300s 余量约 42–49s，环境抖动仍可能吃掉余量；不主张环境问题已根治，以连续多晚门禁实测为准。
- 512M /tmp 对并发全量仍紧：纪律为全量不并发、跑前清 pytest-of-root。
- 以 /var/tmp 为 TMPDIR 的运行不在本修复覆盖内。
