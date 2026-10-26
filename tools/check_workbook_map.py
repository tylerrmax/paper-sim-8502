#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
母板映射核对（四项补齐①，2026-10-27）：derive + diff。

背景：config/workbook_map.json 是 tools/build_workbook_map.py 从封版
母板生成的静态快照，此前靠人工想起来才重跑，无任何自动核对。本工具
每次调用都从母板 xlsx **重新推导**一份映射，与现行 json 逐项 diff：
- 一致 → 返回 ok（夜跑留一行正常记录）；
- 不一致 → 逐项列出差异路径与两侧取值，响亮报错（CLI 退出码 1）；
- **绝不自动覆盖**现行 json——是否重跑生成器刷新映射由人决定。

推导方式：子进程调用 build_workbook_map.py 本体生成到临时文件再
读回（不复制其抽取逻辑，保证同一口径）。生成器依赖 openpyxl，运行
解释器须为装有 openpyxl 的系统 python3（.venv 未装，生成器文档
即如此声明）；--python 可指定。

比较范围：symbols / sectors / b31_manual / gate_b79 / release /
v8_risk / source / source_sha256；忽略 generated_at（生成时间必不同）。

用法:
    python3 tools/check_workbook_map.py [--xlsx PATH] [--map PATH]
    run_daily 以模块方式复用 run()，不一致时打印 FAIL 行。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_XLSX = os.path.expanduser(
    "~/workspace/your_files/交易母版V8.2.1-Final.xlsx")
DEFAULT_MAP = os.path.join(BASE, "config", "workbook_map.json")
GENERATOR = os.path.join(BASE, "tools", "build_workbook_map.py")
IGNORE_KEYS = {"generated_at"}
COMPARE_KEYS = ("symbols", "sectors", "b31_manual", "gate_b79",
                "release", "v8_risk", "source", "source_sha256")
MAX_DIFFS = 50


def _default_python():
    return "/usr/bin/python3" if os.path.exists("/usr/bin/python3") \
        else sys.executable


def derive_map(xlsx=DEFAULT_XLSX, python=None):
    """跑生成器到临时文件，读回 dict。失败抛 RuntimeError（带 stderr）。"""
    python = python or _default_python()
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="workbook_map_")
    os.close(fd)
    try:
        r = subprocess.run([python, GENERATOR, "--xlsx", xlsx,
                            "--out", tmp],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError("映射推导失败（生成器退出 %d）：%s"
                               % (r.returncode,
                                  (r.stderr or r.stdout).strip()[:500]))
        with open(tmp, encoding="utf-8") as f:
            return json.load(f)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _diff(path, a, b, out):
    """递归 diff：a=现行 json，b=推导结果。差异记 "path: 现行=x 推导=y"。"""
    if len(out) >= MAX_DIFFS:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k in IGNORE_KEYS:
                continue
            if k not in a:
                out.append("%s.%s: 现行缺失，推导=%r" % (path, k, b[k]))
            elif k not in b:
                out.append("%s.%s: 推导缺失，现行=%r" % (path, k, a[k]))
            else:
                _diff("%s.%s" % (path, k), a[k], b[k], out)
            if len(out) >= MAX_DIFFS:
                return
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append("%s: 长度 现行=%d 推导=%d" % (path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b)):
            _diff("%s[%d]" % (path, i), x, y, out)
            if len(out) >= MAX_DIFFS:
                return
    elif a != b:
        out.append("%s: 现行=%r 推导=%r" % (path, a, b))


def run(xlsx=DEFAULT_XLSX, map_path=DEFAULT_MAP, python=None):
    """执行一次核对。返回 (ok, diffs, summary)：
    summary = {symbols: n, sectors: m, source_sha256: ...}。
    本函数不打印、不写任何文件；调用方决定如何呈现。"""
    with open(map_path, encoding="utf-8") as f:
        current = json.load(f)
    derived = derive_map(xlsx, python=python)
    diffs = []
    for k in COMPARE_KEYS:
        _diff(k, current.get(k), derived.get(k), diffs)
        if len(diffs) >= MAX_DIFFS:
            break
    summary = {"symbols": len(derived.get("symbols") or {}),
               "sectors": len(derived.get("sectors") or {}),
               "source_sha256": derived.get("source_sha256")}
    return (not diffs), diffs, summary


def main():
    ap = argparse.ArgumentParser(description="母板映射 derive+diff 核对")
    ap.add_argument("--xlsx", default=DEFAULT_XLSX)
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--python", default=None)
    args = ap.parse_args()
    try:
        ok, diffs, summary = run(args.xlsx, args.map, args.python)
    except Exception as e:  # noqa: BLE001
        print("母板映射核对失败：%s" % e, file=sys.stderr)
        return 2
    if ok:
        print("母板映射核对一致：标的 %d、板块 %d，母板 sha256 %s…"
              "（现行 workbook_map.json 与母板推导结果逐项相同）"
              % (summary["symbols"], summary["sectors"],
                 (summary["source_sha256"] or "")[:12]))
        return 0
    print("母板映射核对不一致：%d 项差异（现行 json 未被改动，"
          "请人工决定是否重跑 build_workbook_map.py）：" % len(diffs),
          file=sys.stderr)
    for d in diffs:
        print("  - %s" % d, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
