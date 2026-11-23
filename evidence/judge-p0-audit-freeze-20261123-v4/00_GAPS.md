# GAPS：缺项与残余登记（V4，不补造）

## G-01（沿 V1–V3，未完全关闭）回归日志 verbosity 形式
全量日志为 pytest -q 原始捕获（逐点进度 + FAILURES 全段 + 汇总行）；本批分组日志（P0-1/P0-2/P0-3 三组与合并后复跑）已按补正指令改为 -v 逐用例输出（含用例名、结果、退出码）。替代互证：计数、退出码、测试文件源码（02 节）齐全，审计方可凭 bundle 以 -v 重跑复核全量。

## G-02（沿 V1–V3，仍在）生产库取证形式
只有文件级 SHA256 一致记录（V4 批为开工/收工两点，均为 c914ec8c7c0df93d5e4d86a531d16dabe48cbb652cecca2fb24c1c52985e138f），无 Backup API 冻结副本（理由同前：事后副本不能证明当时状态，不补造）。

## G-03（形态更新）远端 HEAD 未独立复读
V3 冻结 HEAD `28e730f` 已由主会话推送并 ls-remote 核对一致（V3 的 G-03 已关闭）；本包 V4 冻结 HEAD 为本地 unified-arch `610128d`（及随后的组装提交），推送归主会话在本包之后执行，组装时远端尚无此 HEAD，远端核对结果由主会话在推送后另行报告。

## G-04（V1 缺项，V2 已关闭）回归分组构成未登记
历批分组构成已登记于各 DELIVERY 与 04 节对照说明；本批分组构成：P0-1 组 = rt1_approval_lifecycle + rt1_db_guard + rt1_decide_approval + rt1_entry_census；P0-2 组 = rt2_audit_binding + rt2_db_guard + rt2_exclude_sample + validation + validation_gate；P0-3 组 = p0_5_insert_anchor + p0_5_created_at_guard + p0_5_time_anchor + forward_time + rt3_instflow_anchor；合并后组 = tests/test_market_first_*.py + tests/test_adversarial_regression.py（沿 V2/V3 登记口径）。

## G-05（沿 V3，范围更新）DB 直写固有界限（待审计裁定）
同主体全套一致伪造在 SQLite 单文件模型下与合法写入不可区分，本批后形态更新：先直写把 candidates 改出 approved、删 approvals、再伪造新 approvals 改回 approved；先整套伪造审计行再按其内容写带绑定 id 的排除行；直写 INSERT 伪造 discovery_events 历史新行（见 G-06）；直写污染 fetched_bars。本批已封类别新增：「删/改已生效审批记录」「审计行复用/跨样本复用/事后改删被引用审计行」「INSERT 未来时间锚」。余下界限在设计文档与台账残余中明示，handoff 下游核验与审计抽查为第二道防线。

## G-06（V4 新增）P0-3 未实施部分：NULL 与历史回填窗口（待审计裁定）
discovery_events 直接 INSERT 的 created_at 校验本批只实施未来时间拒绝。NULL 拒绝与 test_missing_ingest_time_rejects_forward 的被测前提（库内存在 NULL created_at 事件行）直接冲突；历史回填窗口与固定字符串可信时钟夹具结构性冲突（夹具时刻最老 2026-11-10、年龄随墙钟无限增长，任何有限窗口终将无代码变更地击穿夹具，且 DB 层无法区分夹具写入与直写写入）。冲突全量与可选方案 A（受控通道标记）/ B（宽限窗口 400 天级、明示时限）/ C（维持现状）见 03 节 DESIGN-V3-CORRECTION.md，本批不以时限性窗口冒充长期修复，P0-3 据实标部分实施。

整包状态：NOT_READY（因 G-01/G-02/G-03 证据形式项与 G-06 待裁定项未完全关闭，据实标注；被审计代码、测试与台账不在缺项之列）。
