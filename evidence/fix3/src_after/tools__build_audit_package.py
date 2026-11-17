#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计包自动生成工具（效率升级指令 V1.0 阶段 3）。

从 staging 目录汇编标准审计包到 evidence/<release_id>/，并做
完整性门禁：必需文件缺失、pytest 日志无结尾汇总行（判截断）、
出现未登记的测试失败（与已知基线失败清单比对）、快照文件与
manifest 哈希不一致 -> 包状态只能 NOT_READY 并列明原因；
全部满足才标 READY_FOR_AUDIT。

Staging 约定（evidence/.staging/<release_id>/）：
  tests/full_pytest.log      全量 pytest 原始日志（必需）
  tests/special_pytest.log   专项测试原始日志（可选）
  tests/e2e.log              E2E 原始日志（必需）
  production/before|after/<name>/  snapshot_tool 产物（sim、cache 两库）
  database/before|after/<name>/     同上（研发库 canonical）
  security/*.txt             安全枚举类证据（必需至少 1 个）
  risks.md                   未解决问题与剩余风险（必需）

用法：
  python tools/build_audit_package.py --release-id fix3 \
      --base 1a178c5 [--head <code-commit>] \
      [--staging evidence/.staging/fix3] [--out evidence/fix3]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 以脚本方式运行时 tools/ 在 sys.path[0]，仓库根不在其中；
# 补入仓库根以便 import market_first.snapshot_tool。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SUMMARY_RE = re.compile(
    r"(=+ .*\b(passed|failed|error)\b.* in \d+(\.\d+)?s.*=+"
    r"|\d+ passed.* in \d+(\.\d+)?s)")
FAILED_RE = re.compile(r"^FAILED (\S+?)(?:\s+-.*)?$", re.M)
COUNT_RES = {
    "passed": re.compile(r"(\d+) passed"),
    "failed": re.compile(r"(\d+) failed"),
    "errors": re.compile(r"(\d+) errors?"),
}

SRC_PREFIXES = ("market_first/", "tools/", "tests/")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(repo: Path, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True).stdout


def parse_pytest_log(text: str) -> dict:
    summary_lines = [ln for ln in text.splitlines()
                     if SUMMARY_RE.search(ln)]
    counts = {}
    tail = "\n".join(text.splitlines()[-30:])
    for key, rx in COUNT_RES.items():
        m = rx.search(tail)
        counts[key] = int(m.group(1)) if m else 0
    failed = FAILED_RE.findall(text)
    return {
        "has_summary_line": bool(summary_lines),
        "summary_line": summary_lines[-1] if summary_lines else None,
        "counts": counts,
        "failed_tests": sorted(set(failed)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release-id", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--staging", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--ledger", default="audit_issues.jsonl")
    ap.add_argument("--known-baseline-failures",
                    default="tools/baseline_failures.txt")
    args = ap.parse_args()

    repo = Path.cwd()
    staging = Path(args.staging or
                   "evidence/.staging/%s" % args.release_id)
    out = Path(args.out or "evidence/%s" % args.release_id)
    reasons = []  # NOT_READY 原因

    head = git(repo, "rev-parse", args.head).strip()
    base = git(repo, "rev-parse", args.base).strip()
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

    if out.exists():
        shutil.rmtree(out)
    for sub in ("src_before", "src_after", "tests", "database",
                "production", "security", "risks"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    # ---- git 材料 -------------------------------------------------
    commits = git(repo, "log", "--format=%H %ci %s",
                  "%s..%s" % (base, head))
    changed = git(repo, "diff", "--name-only",
                  "%s..%s" % (base, head)).split()
    (out / "commits.txt").write_text(
        "release_id: %s\nbranch: %s\nbase: %s\nhead: %s\n\n%s"
        % (args.release_id, branch, base, head, commits),
        encoding="utf-8")
    (out / "changed_files.txt").write_text(
        "\n".join(changed) + "\n", encoding="utf-8")
    (out / "full_diff.patch").write_text(
        git(repo, "diff", "%s..%s" % (base, head)), encoding="utf-8")
    for rel in changed:
        if not (rel.startswith(SRC_PREFIXES) and rel.endswith(".py")):
            continue
        name = rel.replace("/", "__")
        before = subprocess.run(
            ["git", "-C", str(repo), "show", "%s:%s" % (base, rel)],
            capture_output=True, text=True)
        if before.returncode == 0:
            (out / "src_before" / name).write_text(
                before.stdout, encoding="utf-8")
        after = subprocess.run(
            ["git", "-C", str(repo), "show", "%s:%s" % (head, rel)],
            capture_output=True, text=True)
        if after.returncode == 0:
            (out / "src_after" / name).write_text(
                after.stdout, encoding="utf-8")

    # ---- 测试日志 --------------------------------------------------
    test_info = {}
    for key, fname, required in (
            ("full_pytest", "full_pytest.log", True),
            ("special_pytest", "special_pytest.log", False),
            ("e2e", "e2e.log", True)):
        src = staging / "tests" / fname
        if not src.exists():
            if required:
                reasons.append("必需测试日志缺失：%s" % fname)
            test_info[key] = None
            continue
        shutil.copy2(src, out / "tests" / fname)
        text = src.read_text(encoding="utf-8", errors="replace")
        info = parse_pytest_log(text) if "pytest" in fname else {
            "lines": len(text.splitlines())}
        test_info[key] = info
        if "pytest" in fname and not info["has_summary_line"]:
            reasons.append("pytest 日志无结尾汇总行（判截断）：%s"
                           % fname)
    known = set()
    kb = Path(args.known_baseline_failures)
    if kb.exists():
        known = {ln.strip() for ln in kb.read_text(
            encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}
    unregistered = []
    full = test_info.get("full_pytest")
    if full:
        for t in full["failed_tests"]:
            func = t.split("::")[-1]
            if not any(k in t or k == func for k in known):
                unregistered.append(t)
        if unregistered:
            reasons.append("全量 pytest 出现未登记失败：%s"
                           % ", ".join(unregistered))
    (out / "tests" / "test_summary.json").write_text(
        json.dumps({"logs": test_info,
                    "known_baseline_failures": sorted(known),
                    "unregistered_failures": unregistered},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 快照（database / production）------------------------------
    # 结构：staging/<kind>/<when>/<name>/manifest.json（production
    # 含 sim 与 cache 两库，database 含 canonical 一库）。
    from market_first.snapshot_tool import diff_manifests
    snap_info = {}
    for kind in ("database", "production"):
        snap_info[kind] = {"changed_tables": []}
        manifests = {}
        for when in ("before", "after"):
            src_root = staging / kind / when
            if not src_root.exists():
                reasons.append("快照缺失：%s/%s" % (kind, when))
                continue
            names = sorted(d.name for d in src_root.iterdir()
                           if d.is_dir()
                           and (d / "manifest.json").exists())
            if not names:
                reasons.append("快照 manifest 缺失：%s/%s"
                               % (kind, when))
                continue
            manifests[when] = {}
            for name in names:
                src = src_root / name
                dst = out / kind / when / name
                shutil.copytree(src, dst)
                manifest = json.loads((dst / "manifest.json")
                                      .read_text(encoding="utf-8"))
                manifests[when][name] = manifest
                db_file = dst / manifest["snapshot_file"]
                actual = (sha256_file(db_file)
                          if db_file.exists() else None)
                ok = (actual == manifest["file_sha256"])
                if not ok:
                    reasons.append(
                        "快照哈希不一致：%s/%s/%s manifest=%s "
                        "actual=%s" % (kind, when, name,
                                       manifest["file_sha256"],
                                       actual))
                snap_info[kind].setdefault(when, {})[name] = {
                    "file_sha256": manifest["file_sha256"],
                    "hash_verified": ok,
                    "taken_at": manifest["taken_at"],
                    "tables": {t: v["row_count"] for t, v in
                               manifest["tables"].items()},
                    "triggers": manifest.get("triggers", []),
                }
        if "before" in manifests and "after" in manifests:
            all_diff = {}
            for name in sorted(set(manifests["before"])
                               & set(manifests["after"])):
                diff = diff_manifests(manifests["before"][name],
                                      manifests["after"][name])
                all_diff[name] = diff
                snap_info[kind]["changed_tables"].extend(
                    "%s.%s" % (name, t)
                    for t in diff["changed_tables"])
            (out / kind / "snapshot_diff.json").write_text(
                json.dumps(all_diff, ensure_ascii=False, indent=1),
                encoding="utf-8")

    # ---- security / risks / 台账 -----------------------------------
    sec_src = staging / "security"
    sec_files = []
    if sec_src.exists():
        for f in sorted(sec_src.iterdir()):
            if f.is_file():
                shutil.copy2(f, out / "security" / f.name)
                sec_files.append(f.name)
    if not sec_files:
        reasons.append("security/ 证据为空")
    risks_src = staging / "risks.md"
    if risks_src.exists():
        shutil.copy2(risks_src, out / "risks" / "risks.md")
    else:
        reasons.append("risks.md 缺失")
    ledger_rows = []
    ledger_p = Path(args.ledger)
    if ledger_p.exists():
        shutil.copy2(ledger_p, out / "risks" / "audit_issues.jsonl")
        ledger_rows = [json.loads(ln) for ln in ledger_p.read_text(
            encoding="utf-8").splitlines() if ln.strip()]
    else:
        reasons.append("问题台账缺失：%s" % args.ledger)

    # ---- 效率指标 ---------------------------------------------------
    release_commits = set(re.findall(r"^([0-9a-f]{40}) ",
                                     commits, re.M))
    short = {c[:7] for c in release_commits}
    by_status, by_severity = {}, {}
    for r in ledger_rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        by_severity[r["severity"]] = \
            by_severity.get(r["severity"], 0) + 1
    fixed_this = [r["issue_id"] for r in ledger_rows
                  if short & set(r.get("fix_commits") or [])]
    new_this = [r["issue_id"] for r in ledger_rows
                if args.release_id in str(r.get("first_found_in"))]
    audit_found = [r["issue_id"] for r in ledger_rows
                   if "audit" in str(r.get("first_found_in"))]
    prod_tables_changed = (
        snap_info.get("production", {}).get("changed_tables") or [])
    required_total = 8  # commits/diff/tests×2/database/production/security/risks/ledger 概念项
    present = required_total - len(
        [r for r in reasons if "缺失" in r or "为空" in r])
    metrics = {
        "release_id": args.release_id,
        "issues_total": len(ledger_rows),
        "issues_by_status": by_status,
        "issues_by_severity": by_severity,
        "issues_first_found_in_audit_rounds": audit_found,
        "issues_fixed_this_release": fixed_this,
        "issues_new_this_release": new_this,
        "full_pytest_counts": (full or {}).get("counts"),
        "baseline_failures_known": sorted(known),
        "unregistered_failures": unregistered,
        "production_changed_tables": prod_tables_changed,
        "evidence_completeness": "%d/%d" % (max(present, 0),
                                            required_total),
        "audit_duration": "待独立审计填写（开发方不自报）",
        "note": ("效率指标只作过程度量，不以提交数/测试数/PASS "
                 "比例论绩效（指令 V1.0 第七节）。"),
    }
    (out / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=1),
        encoding="utf-8")

    # ---- STATUS + INDEX（先于 checksums 生成，使其一并入校验） ----
    status = "READY_FOR_AUDIT" if not reasons else "NOT_READY"
    (out / "STATUS.txt").write_text(
        "status: %s\nrelease_id: %s\nbase: %s\nhead: %s\n%s"
        % (status, args.release_id, base, head,
           "".join("NOT_READY reason: %s\n" % r for r in reasons)),
        encoding="utf-8")
    index = [
        "# 审计包 INDEX — %s" % args.release_id,
        "",
        "状态：**%s**（完整性门禁%s）"
        % (status, "通过" if not reasons else "未通过，见 STATUS.txt"),
        "分支：%s；基线：%s；HEAD：%s" % (branch, base, head),
        "",
        "## 目录",
        "- commits.txt / changed_files.txt / full_diff.patch：提交链与完整 diff",
        "- src_before/ / src_after/：修改前后源码（按 git 基线/HEAD 逐字导出）",
        "- tests/：全量/专项 pytest 与 E2E 原始日志 + test_summary.json",
        "- database/：研发库迁移前后文件级快照（Backup API）+ snapshot_diff.json",
        "- production/：生产 sim.db 与 cache.db 窗口前后文件级快照 + snapshot_diff.json",
        "- security/：安全枚举与对抗证据",
        "- risks/：risks.md + audit_issues.jsonl（跨轮次问题台账）",
        "- metrics.json：效率指标；checksums.txt：全包校验和",
        "",
        "## 测试摘要",
        "```json",
        json.dumps(test_info, ensure_ascii=False, indent=1),
        "```",
        "",
        "## 快照摘要",
        "```json",
        json.dumps(snap_info, ensure_ascii=False, indent=1),
        "```",
        "",
        "## 效率指标",
        "```json",
        json.dumps(metrics, ensure_ascii=False, indent=1),
        "```",
        "",
        "本 INDEX 由 tools/build_audit_package.py 自动生成；"
        "开发方摘要不替代原始材料。",
    ]
    (out / "INDEX.md").write_text("\n".join(index) + "\n",
                                  encoding="utf-8")

    # ---- checksums（覆盖除自身外全部文件）+ 自检 -------------------
    files = sorted(p for p in out.rglob("*") if p.is_file())
    (out / "checksums.txt").write_text(
        "".join("%s  %s\n" % (sha256_file(p), p.relative_to(out))
                for p in files if p.name != "checksums.txt"),
        encoding="utf-8")
    verify_ok = True
    for ln in (out / "checksums.txt").read_text(
            encoding="utf-8").splitlines():
        digest, rel = ln.split("  ", 1)
        if sha256_file(out / rel) != digest:
            verify_ok = False
    if not verify_ok:
        reasons.append("checksums 自检不一致")
        status = "NOT_READY"
        (out / "STATUS.txt").write_text(
            "status: NOT_READY\nrelease_id: %s\nbase: %s\nhead: %s\n"
            "NOT_READY reason: checksums 自检不一致\n"
            % (args.release_id, base, head), encoding="utf-8")
    print(json.dumps({"status": status, "reasons": reasons,
                      "out": str(out)}, ensure_ascii=False))
    return 0 if status == "READY_FOR_AUDIT" else 3


if __name__ == "__main__":
    raise SystemExit(main())
