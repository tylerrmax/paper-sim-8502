# 测试资源膨胀机制 · 调查笔记（2026-11-08 晚，隔离区实测）

## 已证实（本机实测/读码）
1. 干净环境下一次全量（216 项，门禁同款命令）临时足迹有界：/tmp/pytest-of-root 单轮 124M（pytest-0），pytest 保留最近 3 轮 → 稳态上限约 380M。最大单测目录 test_apptest_counts_drop_and_i*=18M（pre/post 两棵树各 8.7M）；单棵 AppTest 复制树 ≈8.7M（生产仓工作集 .git 11M/db 7.2M/reports 3.1M/evidence 1.5M/tests 1.5M，.venv 约 600M 已被忽略名单排除）。
2. 全量耗时 250.9s（用户态仅 21.9s，绝大部分为等待/AppTest 渲染），/tmp 收尾 26%、/var/tmp 0%，最大树内未见嵌套 pytest-of-root/.tmp_pw。
3. 反馈环机制（读码+历史观测）：_build_app_tree 修复前忽略名单不含 pytest-of-root/.tmp_pw。11-07 仓根存在 pytest-of-root/（18:47–18:50 运行残留，du 表观 7.9G 含嵌套重复计数）时，每棵 AppTest 树都会把整份残留复制进去，残留内又含更早的测试树（/var/tmp 曾观测 .tmp_pw 内嵌 pytest-of-root），形成递归放大：复制量随测试数线性放大至 GB 级 → /tmp（512M tmpfs）数分钟打满、AppTest 在第 ~198 项附近停滞、门禁 300s 超时。此为三次拒推的形态学解释，与全部观测一致。
4. 并发放大：11-08 02:xx 前后统计批次并发 pytest、运行时重启后的孤儿 pytest 进程与 UI 发布三次门禁尝试共用 /tmp/pytest-of-root（pytest-0/-1 编号交错），多轮残留叠加加速打满。/var/tmp（2.0G tmpfs）被另一批以其为 TMPDIR 的运行残留占满（pytest-of-root），是 11-07 晚 Disk quota exceeded 写失败之源，已清理。

## 修复如何断环
- copytree 忽略名单 + .gitignore 加入 pytest-of-root/.tmp_pw：无论残留如何再次落入仓根，都不再被复制进测试树，递归放大链断裂；git 层亦不再跟踪。
- 既有残留已清理（/tmp、/var/tmp、仓根）。

## 未证实（如实保留）
- 11-07 pytest-of-root 首次落入仓根的具体路径（相对 basetemp / TMPDIR 指向仓根 / 其他封装）无日志留存，无法复原，不作为结论。
- .tmp_pw 的最初产生者（仓库代码内无 tmp_pw/basetemp/TMPDIR 引用）未定位；仅确认其可被复制放大，现已被忽略名单覆盖。

## 剩余风险
- 512M /tmp 对并发全量运行仍紧：单轮 124M×保留 3 轮 + 并发第二轮可再逼近上限。纪律：全量 pytest 不并发跑；跑前清 pytest-of-root。
- 以 /var/tmp 为 TMPDIR 的运行不受本次修复覆盖（仓根忽略名单只断复制环）；若再现占满，按同一清理口径处置。
- 250.9s 距 300s 门禁余量 49s：环境抖动（并发、冷缓存）仍可能吃掉余量。本次不主张已根治，结论以连续多晚门禁实测为准。
