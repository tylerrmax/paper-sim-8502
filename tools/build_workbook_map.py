#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从交易母版 Watchlist观察池 + 板块共振半自动 生成 config/workbook_map.json。

用法:
    python3 tools/build_workbook_map.py [--xlsx PATH] [--out PATH]

可重跑：母板更新后重跑即可刷新映射（标的/板块/突破位/止损/催化/共振评分）。
依赖 openpyxl（系统 python3 已装；.venv 未装时请用系统 python3 运行本工具）。

板块口径映射（母板板块标签 -> 新闻 sector_key）为本文件内的显式常量，
与 X 博主每日简报的板块口径对齐。铁律：母板没有“存储”板块标签，
Serenity 的“存储”指存储芯片（memory），只进 semi，绝不映射到
energy_storage（储能、光伏）。
"""
import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_XLSX = os.path.expanduser("~/workspace/your_files/交易母版V8.2.1-Final.xlsx")
DEFAULT_OUT = os.path.join(BASE, "config", "workbook_map.json")

# 母板板块标签 -> sector_key（新闻面口径）
SECTOR_KEY_MAP = {
    "卫星通信（商业航天）": "optical_comm",
    "工业母机": "machinery",
    "机器人": "robotics",
    "智能驾驶": "ai_chip",
    "信创(国产软件)": "software",
    "国防军工": "defense",
    "锂电池": "battery",
    "Ai算力、基础设施": "ai_infra",
    "半导体": "semi",
    "液冷": "liquid_cooling",
    "储能、光伏": "energy_storage",
}


def _norm_code(v):
    """代码归一：A股补零到6位；HK 保持 HK+4位。"""
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip().upper()
    if not s:
        return None
    if s.startswith("HK"):
        return "HK" + s[2:].zfill(4)
    if s.isdigit():
        return s.zfill(6)
    return s


def _num(v):
    return float(v) if isinstance(v, (int, float)) else None


def _yn(v):
    return str(v).strip() == "是" if v is not None else False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=DEFAULT_XLSX)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    try:
        import openpyxl
    except ImportError:
        print("需要 openpyxl：请用系统 python3 运行（.venv 未装 openpyxl）", file=sys.stderr)
        sys.exit(1)

    wb = openpyxl.load_workbook(args.xlsx, data_only=True)

    # ---- 板块共振半自动：板块 -> 自动评分（J列）/ 共振等级（K列） ----
    ws = wb["板块共振半自动"]
    sectors = {}
    for row in ws.iter_rows(min_row=4):
        label = row[0].value  # A 板块
        if not label or not str(label).strip():
            continue
        label = str(label).strip()
        score = _num(row[9].value)  # J 自动评分
        sectors[label] = {
            "sector_key": SECTOR_KEY_MAP.get(label),
            "resonance_score": score,
            "resonance_level": (str(row[10].value).strip()
                                if row[10].value else None),  # K 共振等级
        }

    # ---- Watchlist观察池：表头第3行，数据自第4行，A列代码为空即止 ----
    ws = wb["Watchlist观察池"]
    symbols = {}
    for row in ws.iter_rows(min_row=4):
        code = _norm_code(row[0].value)  # A 代码
        if not code:
            break
        label = str(row[2].value).strip() if row[2].value else None  # C 板块
        symbols[code] = {
            "name": str(row[1].value).strip() if row[1].value else None,  # B 名称
            "sector_label": label,
            "sector_key": SECTOR_KEY_MAP.get(label),
            "resonance_flag": _yn(row[3].value),   # D 板块共振
            "catalyst": _yn(row[4].value),        # E 催化落地
            "breakout": _num(row[5].value),       # F 关键突破位
            "stop_ref": _num(row[6].value),       # G 止损参考
        }

    unknown = [s["sector_label"] for s in symbols.values()
               if s["sector_label"] and s["sector_key"] is None]
    if unknown:
        print("WARN 未映射 sector_key 的板块标签: %s" % sorted(set(unknown)),
              file=sys.stderr)

    out = {
        "source": os.path.basename(args.xlsx),
        "symbols": symbols,
        "sectors": sectors,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("写入 %s：标的 %d 只，板块 %d 个" % (args.out, len(symbols), len(sectors)))
    for code, s in symbols.items():
        print("  %s %s [%s -> %s] 突破=%s 止损=%s 催化=%s 共振=%s"
              % (code, s["name"], s["sector_label"], s["sector_key"],
                 s["breakout"], s["stop_ref"], s["catalyst"], s["resonance_flag"]))


if __name__ == "__main__":
    main()
