# GAPS：缺项与残余登记（V3，不补造）

## G-01（沿 V1/V2，未完全关闭）回归日志 verbosity 形式
全量日志为 pytest -q 原始捕获（逐点进度 + FAILURES 全段 + 汇总行）；专项日志同为默认 verbosity，无逐用例名。替代互证：计数、退出码、测试文件源码（02 节）齐全，审计方可凭 bundle 以 -v 重跑复核。底层批两份全量日志与合并后复跑日志同此形式。

## G-02（沿 V1/V2，仍在）生产库取证形式
只有文件级 SHA256 一致记录（V3 批为开工/合并后两点，均为 c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f），无 Backup API 冻结副本（理由同前：事后副本不能证明当时状态，不补造）。

## G-03（形态更新）远端 HEAD 未独立复读
V2 冻结 HEAD `a84fb4a` 已由主会话推送并 ls-remote 核对一致（V2 的 G-03 已关闭）；本包 V3 冻结 HEAD 为本地 unified-arch `bf2135b`，推送归主会话在本包之后执行，组装时远端尚无此 HEAD，远端核对结果由主会话在推送后另行报告。

## G-04（V1 缺项，V2 已关闭）回归分组构成未登记
三组专项的文件构成已逐项登记于 DELIVERY-REDTEAM-FIX-20261123.md 与 04 节对照说明；底层批 Market-First 相关组口径（test_market_first_*.py + test_adversarial_regression.py）登记于 04 节 bottom_layer_baseline_vs_fix.md。

## G-05（沿 V2，范围更新）DB 直写固有界限（待审计裁定）
同主体全套一致伪造（含本批新增两闸的对应形态：先伪造齐全 approvals 行再改 promotion_status、先伪造逐字对应 candidate_history 审计行再写 sample_exclusions）、直写 INSERT 伪造 discovery_events 新行、直写污染 fetched_bars，在 SQLite 单文件模型下与合法写入不可区分；已封类别：缺项/回填时间/无对应行/落字后篡改，及本批新增的「无有效审批记录置 approved」「无对应审计行写排除」。余下界限在设计文档与台账残余中明示，handoff 下游核验与审计抽查为第二道防线。

整包状态：NOT_READY（因 G-01/G-02/G-03 证据形式项未完全关闭，据实标注；被审计代码、测试与台账不在缺项之列）。
