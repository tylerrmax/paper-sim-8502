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
import hashlib
import json
import os
import sys
from datetime import datetime

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

    # ---- 母板环境真源字段（模块A）：环境判定唯一真源 = 母板，不另算 ----
    # 按标签定位（不写死行号），记录来源单元格供追溯；值“是”→True、“否”→False。
    def _find_param(ws, label):
        for row in ws.iter_rows():
            a = row[0].value
            if a and str(a).strip() == label:
                return row[1].coordinate, row[1].value
        return None, None

    def _yesno(cell, raw):
        return {"label": None, "cell": ("参数设置!%s" % cell) if cell else None,
                "raw": (str(raw).strip() if raw is not None else None),
                "value": (True if str(raw).strip() == "是"
                          else False if str(raw).strip() == "否" else None)}

    ws_p = wb["参数设置"]
    # B31：大盘指数站上20日线（参数设置!B31）
    _cell, _raw = _find_param(ws_p, "大盘指数站上20日线")
    b31_manual = _yesno(_cell, _raw)
    b31_manual["label"] = "大盘指数站上20日线"
    if _cell is None:
        print("WARN 参数设置未找到“大盘指数站上20日线”行，b31_manual 记空",
              file=sys.stderr)
    # B79：未放行时允许新开/接回买入（参数设置!B79）
    _cell, _raw = _find_param(ws_p, "未放行时允许新开/接回买入")
    gate_b79 = _yesno(_cell, _raw)
    gate_b79["label"] = "未放行时允许新开/接回买入"
    if _cell is None:
        print("WARN 参数设置未找到“未放行时允许新开/接回买入”行，gate_b79 记空",
              file=sys.stderr)
    # V8 验证期风控参数（参数设置!B78 买入仓位系数 / B80 有效总仓位上限）
    _c1, _v1 = _find_param(ws_p, "验证期买入仓位系数")
    _c2, _v2 = None, None
    for row in ws_p.iter_rows():
        a = row[0].value
        if a and str(a).strip().startswith("有效总仓位上限"):
            _c2, _v2 = row[1].coordinate, row[1].value
            break
    v8_risk = {
        "buy_coef": {"label": "验证期买入仓位系数",
                     "cell": ("参数设置!%s" % _c1) if _c1 else None,
                     "raw": _v1,
                     "value": (float(_v1) if isinstance(_v1, (int, float))
                               else None)},
        "total_pos_cap": {"label": "有效总仓位上限",
                          "cell": ("参数设置!%s" % _c2) if _c2 else None,
                          "raw": _v2,
                          "value": (float(_v2) if isinstance(_v2, (int, float))
                                    else None)},
    }
    # 放行状态：实盘验证“综合结论”行 E 列（实盘验证!E14）
    release = {"label": "综合结论", "cell": None, "raw": None,
               "released": None, "summary": None}
    ws_v = wb["实盘验证"]
    for row in ws_v.iter_rows():
        b = row[1].value if len(row) > 1 else None
        if b and str(b).strip() == "综合结论":
            e = row[4].value if len(row) > 4 else None
            d = row[3].value if len(row) > 3 else None
            release = {
                "label": "综合结论",
                "cell": "实盘验证!%s" % (row[4].coordinate if len(row) > 4 else "?"),
                "raw": (str(e).strip() if e is not None else None),
                "released": (str(e).strip().startswith("已放行")
                             if e is not None else None),
                "summary": (str(d).strip() if d is not None else None),
            }
            break
    if release["cell"] is None:
        print("WARN 实盘验证未找到“综合结论”行，release 记空", file=sys.stderr)

    unknown = [s["sector_label"] for s in symbols.values()
               if s["sector_label"] and s["sector_key"] is None]
    if unknown:
        print("WARN 未映射 sector_key 的板块标签: %s" % sorted(set(unknown)),
              file=sys.stderr)

    # 可复现元数据（P0-2）：生成时间 + 母板文件 sha256。
    # 母板改动而生成器未重跑时，下游可比对哈希/时间发觉映射过期。
    with open(args.xlsx, "rb") as f:
        xlsx_sha = hashlib.sha256(f.read()).hexdigest()
    out = {
        "source": os.path.basename(args.xlsx),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_sha256": xlsx_sha,
        "b31_manual": b31_manual,
        "gate_b79": gate_b79,
        "release": release,
        "v8_risk": v8_risk,
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
