# GAPS：缺项与残余登记（V5，不补造）

## G-01（沿 V1–V4，未完全关闭）回归日志 verbosity 形式
全量日志为 pytest -q 原始捕获（逐点进度 + FAILURES 全段 + 汇总行）；本批分组日志（P0-3 组与合并后复跑）中 P0-3 组已按补正指令为 -v 逐用例输出（含用例名、结果、退出码），合并后复跑沿登记口径为 -q。替代互证：计数、退出码、测试文件源码（02 节）齐全，审计方可凭 bundle 以 -v 重跑复核全量。

## G-02（沿 V1–V4，仍在）生产库取证形式
只有文件级 SHA256 一致记录（V5 批为开工/收工两点，均为 c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f），无 Backup API 冻结副本（理由同前：事后副本不能证明当时状态，不补造）。

## G-03（形态更新）远端 HEAD 未独立复读
V4 冻结 HEAD `61b1d98` 已由主会话推送并 ls-remote 核对一致（V4 的 G-03 已关闭）；本包 V5 冻结 HEAD 为本地 unified-arch `2cf2234`（及随后的组装提交），推送归主会话在本包之后执行，组装时远端尚无此 HEAD，远端核对结果由主会话在推送后另行报告。

## G-04（V1 缺项，V2 已关闭）回归分组构成未登记
历批分组构成已登记于各 DELIVERY 与 04 节对照说明；本批分组构成：P0-3 组 = p0_5_channel_marks + p0_5_insert_anchor + p0_5_created_at_guard + p0_5_time_anchor + forward_time + rt3_instflow_anchor；合并后组 = tests/test_market_first_*.py + tests/test_adversarial_regression.py（沿 V2 起登记口径）。

## G-05（沿 V4，范围更新）DB 直写固有界限（待审计裁定）
同主体全套一致伪造在 SQLite 单文件模型下与合法写入不可区分，本批后形态更新：先直写把 candidates 改出 approved、删 approvals、再伪造新 approvals 改回 approved；先整套伪造审计行再按其内容写带绑定 id 的排除行；**直写自造 ingest 通道标记行再按其 id 写回填 discovery 事件行（本批新增形态，对抗实测放行——标记证明经受控写序的形态、不证明调用方身份，伪造标记落字后被绑定锁定禁改禁删，行为本身留痕）**；直写污染 fetched_bars。本批已封类别新增：「无有效标记的 NULL/历史回填/不可解析 created_at INSERT」「标记复用/悬空引用/跨候选引用」「已绑定标记的改删」。余下界限在设计文档与台账残余中明示，handoff 下游核验与审计抽查为第二道防线。

## G-06（V4 新增，V5 更新）P0-3 未实施部分：已按方案 A 实施，待独立审计复核
discovery_events 直接 INSERT 的 created_at 校验，V4 时点仅实施未来时间拒绝，NULL 与历史回填因与可信时钟夹具的结构性冲突（固定字符串历史时刻年龄随墙钟无限增长；缺锚用例前提需 NULL 行；触发器无法区分夹具与直写）登记待裁定。用户 2026-11-23 15:13 定案方案 A（受控通道标记），本批已完整实施（提交 c0a062c）：按写序而非时间值区分合法写入与直写——ingest 同事务先落标记、事件行以 ingest_mark_id 一对一绑定；无有效标记的 NULL/回填/不可解析 INSERT 一律拒绝；夹具与缺锚前提经通道合法落字，与墙钟无关，未采用任何时限性窗口。冲突分析原文见 03 节 DESIGN-V3-CORRECTION.md，实施设计见 DESIGN-P0-3-CHANNEL-A.md。本项状态：**已实施、待独立审计复核**（复核前不视为关闭）。

整包状态：NOT_READY（因 G-01/G-02/G-03 证据形式项未完全关闭，据实标注；G-06 已实施待复核；被审计代码、测试与台账不在缺项之列）。
