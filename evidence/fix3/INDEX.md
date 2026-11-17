# 审计包 INDEX — fix3

状态：**READY_FOR_AUDIT**（完整性门禁通过）
分支：market-first/unified-arch；基线：1a178c52b107d7b2e2023cadf1fedf7042df0616；HEAD：936e4c64d58723709b5187a3bacb999741e38092

## 目录
- commits.txt / changed_files.txt / full_diff.patch：提交链与完整 diff
- src_before/ / src_after/：修改前后源码（按 git 基线/HEAD 逐字导出）
- tests/：全量/专项 pytest 与 E2E 原始日志 + test_summary.json
- database/：研发库迁移前后文件级快照（Backup API）+ snapshot_diff.json
- production/：生产 sim.db 与 cache.db 窗口前后文件级快照 + snapshot_diff.json
- security/：安全枚举与对抗证据
- risks/：risks.md + audit_issues.jsonl（跨轮次问题台账）
- metrics.json：效率指标；checksums.txt：全包校验和

## 测试摘要
```json
{
 "full_pytest": {
  "has_summary_line": true,
  "summary_line": "1 failed, 294 passed, 3 warnings in 249.70s (0:04:09)",
  "counts": {
   "passed": 294,
   "failed": 1,
   "errors": 0
  },
  "failed_tests": [
   "tests/test_e2e_batch2.py::test_ledger_fingerprint_idempotent"
  ]
 },
 "special_pytest": {
  "has_summary_line": true,
  "summary_line": "79 passed, 1 warning in 2.55s",
  "counts": {
   "passed": 79,
   "failed": 0,
   "errors": 0
  },
  "failed_tests": []
 },
 "e2e": {
  "lines": 131
 }
}
```

## 快照摘要
```json
{
 "database": {
  "changed_tables": [],
  "before": {
   "canonical": {
    "file_sha256": "45f54e5e4fab10102d3f14e3b067de3a8eb5b7b07b6ea63ee8b01b117a1de011",
    "hash_verified": true,
    "taken_at": "2026-11-17T04:02:06+00:00",
    "tables": {
     "approvals": 2,
     "candidate_history": 20,
     "candidates": 3,
     "discovery_events": 4,
     "fetched_bars": 0,
     "fetched_coverage": 0,
     "handoffs": 1,
     "research_reports": 1,
     "rule_registry": 0,
     "sample_exclusions": 1,
     "validation_decisions": 0,
     "validation_samples": 2
    },
    "triggers": []
   }
  },
  "after": {
   "canonical": {
    "file_sha256": "3b515fd0012897d7beb18b2c1cf85acb6a203bd6f76349ea6d67e575e197a44e",
    "hash_verified": true,
    "taken_at": "2026-11-17T04:13:28+00:00",
    "tables": {
     "approvals": 2,
     "candidate_history": 20,
     "candidates": 3,
     "discovery_events": 4,
     "fetched_bars": 0,
     "fetched_coverage": 0,
     "handoffs": 1,
     "research_reports": 1,
     "rule_registry": 0,
     "sample_exclusions": 1,
     "validation_decisions": 0,
     "validation_samples": 2
    },
    "triggers": [
     "trg_validation_samples_benchmark_immutable",
     "trg_validation_samples_no_delete"
    ]
   }
  }
 },
 "production": {
  "changed_tables": [],
  "before": {
   "cache": {
    "file_sha256": "5ceccab202b397f549dc6c206b27d82052702d4d89c72f4f8008d1affaf6997a",
    "hash_verified": true,
    "taken_at": "2026-11-17T04:02:06+00:00",
    "tables": {
     "bars": 5549
    },
    "triggers": []
   },
   "sim": {
    "file_sha256": "22cd09013199bd063a424c2af3db6908fbb4c6ad787adf05fb1aa13b7a591f8a",
    "hash_verified": true,
    "taken_at": "2026-11-17T04:02:05+00:00",
    "tables": {
     "approvals": 23,
     "blocked_perf": 9,
     "blocked_signals": 9,
     "broker_receipts": 0,
     "control": 1,
     "daily_report": 1100,
     "fills": 51,
     "fx_rate": 46,
     "graduation_exclusions": 4,
     "inst_lhb_daily": 928,
     "inst_lhb_fetch_days": 22,
     "inst_lhb_raw": 17206,
     "intent_transitions": 0,
     "market_data": 3526,
     "pause_skipped_signals": 0,
     "positions": 1,
     "processed_commands": 17,
     "real_fills": 0,
     "risk_block_events": 9,
     "rotation_vouchers": 1,
     "screen_perf": 400,
     "screen_perf_factors": 506,
     "screen_runs": 68,
     "signals": 26,
     "trade_intents": 0
    },
    "triggers": []
   }
  },
  "after": {
   "cache": {
    "file_sha256": "5ceccab202b397f549dc6c206b27d82052702d4d89c72f4f8008d1affaf6997a",
    "hash_verified": true,
    "taken_at": "2026-11-17T04:13:28+00:00",
    "tables": {
     "bars": 5549
    },
    "triggers": []
   },
   "sim": {
    "file_sha256": "22cd09013199bd063a424c2af3db6908fbb4c6ad787adf05fb1aa13b7a591f8a",
    "hash_verified": true,
    "taken_at": "2026-11-17T04:13:27+00:00",
    "tables": {
     "approvals": 23,
     "blocked_perf": 9,
     "blocked_signals": 9,
     "broker_receipts": 0,
     "control": 1,
     "daily_report": 1100,
     "fills": 51,
     "fx_rate": 46,
     "graduation_exclusions": 4,
     "inst_lhb_daily": 928,
     "inst_lhb_fetch_days": 22,
     "inst_lhb_raw": 17206,
     "intent_transitions": 0,
     "market_data": 3526,
     "pause_skipped_signals": 0,
     "positions": 1,
     "processed_commands": 17,
     "real_fills": 0,
     "risk_block_events": 9,
     "rotation_vouchers": 1,
     "screen_perf": 400,
     "screen_perf_factors": 506,
     "screen_runs": 68,
     "signals": 26,
     "trade_intents": 0
    },
    "triggers": []
   }
  }
 }
}
```

## 效率指标
```json
{
 "release_id": "fix3",
 "issues_total": 12,
 "issues_by_status": {
  "READY_FOR_AUDIT": 10,
  "OPEN": 2
 },
 "issues_by_severity": {
  "P0": 10,
  "P1": 2
 },
 "issues_first_found_in_audit_rounds": [
  "MF-FWD-001",
  "MF-DEDUP-001",
  "MF-DEDUP-002",
  "MF-BENCH-001",
  "MF-REG-001",
  "MF-CACHE-001",
  "MF-PROMO-001",
  "MF-HANDOFF-001"
 ],
 "issues_fixed_this_release": [
  "MF-FWD-002",
  "MF-BENCH-002",
  "MF-BENCH-003",
  "MF-REG-002"
 ],
 "issues_new_this_release": [
  "MF-FWD-002",
  "MF-BENCH-002",
  "MF-BENCH-003",
  "MF-REG-002"
 ],
 "full_pytest_counts": {
  "passed": 294,
  "failed": 1,
  "errors": 0
 },
 "baseline_failures_known": [
  "test_ledger_fingerprint_idempotent"
 ],
 "unregistered_failures": [],
 "production_changed_tables": [],
 "evidence_completeness": "8/8",
 "audit_duration": "待独立审计填写（开发方不自报）",
 "note": "效率指标只作过程度量，不以提交数/测试数/PASS 比例论绩效（指令 V1.0 第七节）。"
}
```

本 INDEX 由 tools/build_audit_package.py 自动生成；开发方摘要不替代原始材料。
