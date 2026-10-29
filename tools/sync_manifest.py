#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""部署清单检查：从入口文件解析 import 闭包，找出"被代码引用却不在同步清单里"的文件。
  python3 tools/sync_manifest.py [--list 清单文件.py(含 FILES=[...])]   默认读 sync_8502_github.py
退出码 1 = 有缺失。也被 tools/sync_atomic.py 作为推送前置检查。"""
import ast
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = ["app.py", "run_daily.py"]
LOCAL_PKGS = ("engine", "data")
# 运行时按字符串路径读取、import 解析不到的文件
STATIC = ["requirements.txt", ".streamlit/config.toml", "db/schema.sql", "db/sim.db",
          "config/strategy.yaml", "engine/lightweight-charts.standalone.js"]
STATIC_GLOBS = ["db/migrations/*.sql", "screen_inputs/*.json"]
EXCLUDE_SUFFIX = (".example.json",)


def _mod_file(mod):
    parts = mod.split(".")
    for cand in ("/".join(parts) + ".py", "/".join(parts) + "/__init__.py"):
        if os.path.exists(os.path.join(ROOT, cand)):
            return cand
    return None


def import_closure(entries=None, root=None):
    root = root or ROOT
    seen, stack = set(), list(entries or ENTRY)
    while stack:
        f = stack.pop()
        if f in seen or not os.path.exists(os.path.join(root, f)):
            continue
        seen.add(f)
        try:
            tree = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            for m in mods:
                if m.split(".")[0] in LOCAL_PKGS:
                    mf = _mod_file(m)
                    if mf:
                        stack.append(mf)
                        pkg = os.path.dirname(mf)
                        if pkg and os.path.exists(os.path.join(root, pkg, "__init__.py")):
                            stack.append(pkg + "/__init__.py")
    return seen


def required_files():
    req = set(import_closure()) | {f for f in STATIC if os.path.exists(os.path.join(ROOT, f))}
    for g in STATIC_GLOBS:
        for p in glob.glob(os.path.join(ROOT, g)):
            rel = os.path.relpath(p, ROOT)
            if not rel.endswith(EXCLUDE_SUFFIX):
                req.add(rel)
    return sorted(req)


def read_files_list(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "FILES" for t in n.targets):
            return list(ast.literal_eval(n.value))
    raise ValueError(f"{path} 中找不到 FILES = [...]")


def missing_from(files_list):
    have = set(files_list)
    return [f for f in required_files() if f not in have]


if __name__ == "__main__":
    lst = sys.argv[sys.argv.index("--list") + 1] if "--list" in sys.argv else os.path.join(ROOT, "sync_8502_github.py")
    miss = missing_from(read_files_list(lst))
    print("应同步的文件（共 %d）：" % len(required_files()))
    for f in required_files():
        print(("  ❌缺 " if f in miss else "  ✅ ") + f)
    sys.exit(1 if miss else 0)
