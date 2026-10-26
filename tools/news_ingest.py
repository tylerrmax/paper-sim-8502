#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
板块新闻证据入账工具（四项补齐①，2026-10-27）。

背景：config/sector_news.json 此前靠每日简报 cron 的执行 agent 手工
追加，无 schema 校验、无去重程序、无入账回执（findings 断点①）。
本工具把入账变成一道可校验、可审计的关口：

用法:
    python3 tools/news_ingest.py --file batch.json [--check]
    batch.json 为条目数组，或 {"entries": [...]}。

校验规则（任一条不过 → **整批拒绝**、账文件逐字不动、回执落
rejected 行、退出码 2）：
- 必填: date（YYYY-MM-DD 真实日期，且不晚于当日）、sector_key
  （白名单）、direction（整数 -1 或 1）、strength（整数 1~3）、
  source（枚举 = engine.news_sentiment.SOURCE_WEIGHT 的键，现行
  Serenity / Danny / Musk）、evidence（非空字符串）。
- 可选: tags（字符串数组）、scope（仅许 "market_risk"；
  sector_key="market" 的市场级条目必须带 scope="market_risk"）。
- 去重: 与账内已有或批内条目**完全重复**（date+sector_key+source+
  direction+strength+evidence 全同）→ 拒。同 date+sector+source
  但 evidence 不同的是不同证据，允许入账（现存账本即有此形态，
  如同日同板块两条不同 Serenity 证据）。
- 白名单: config/workbook_map.json 中 sectors/symbols 的 sector_key
  集合 ∪ NEWS_ONLY_SECTORS（新闻侧独有板块：semi_equip 半导体设备
  ——现存 Danny 历史条目与简报口径在用；market 市场级）。母板新增
  板块时白名单自动跟随，新闻侧独有板块在此显式声明，不靠自觉。

回执: 每次调用（成功/拒绝/check）都向只追加日志追加一行 JSON
（默认 reports/news_ingest_receipts.jsonl）：时间、状态、来源、
入账条数、账内总条数、最新条目日期、合并后 entries 的 sha256
（与 screen_runs.news_sha256 同规范化口径）、被拒原因明细。
工具只管入账与校验，不改新闻分计算口径（engine/news_sentiment.py）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_NEWS = os.path.join(BASE, "config", "sector_news.json")
DEFAULT_MAP = os.path.join(BASE, "config", "workbook_map.json")
DEFAULT_RECEIPT = os.path.join(BASE, "reports",
                               "news_ingest_receipts.jsonl")

sys.path.insert(0, BASE)
from engine.news_sentiment import SOURCE_WEIGHT  # noqa: E402  来源枚举真源

NEWS_ONLY_SECTORS = {"semi_equip", "market"}
SCOPES = {"market_risk"}


def sector_whitelist(map_path=DEFAULT_MAP):
    """白名单 = 母板映射板块键 ∪ 新闻侧独有板块。映射读不到 → 抛错
    （真源缺失时不放行入账，不靠内置常量兜底）。"""
    with open(map_path, encoding="utf-8") as f:
        wmap = json.load(f)
    keys = set()
    for sec in (wmap.get("sectors") or {}).values():
        if sec.get("sector_key"):
            keys.add(sec["sector_key"])
    for meta in (wmap.get("symbols") or {}).values():
        if meta.get("sector_key"):
            keys.add(meta["sector_key"])
    return keys | NEWS_ONLY_SECTORS


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _entry_key(e):
    return (e.get("date"), e.get("sector_key"), e.get("source"),
            e.get("direction"), e.get("strength"), e.get("evidence"))


def validate_entry(e, whitelist, today):
    """单条校验，返回错误字符串或 None。"""
    if not isinstance(e, dict):
        return "条目不是对象：%r" % (e,)
    d = e.get("date")
    if not isinstance(d, str):
        return "date 缺失或非字符串：%r" % (d,)
    try:
        dd = datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return "date 格式非法（须 YYYY-MM-DD）：%r" % (d,)
    if dd > today:
        return "date 晚于当日（%s > %s），未来证据不入账" % (d, today)
    sk = e.get("sector_key")
    if sk not in whitelist:
        return "sector_key 不在白名单：%r（白名单 %s）" % (
            sk, sorted(whitelist))
    if not _is_int(e.get("direction")) or e["direction"] not in (-1, 1):
        return "direction 须为整数 -1 或 1：%r" % (e.get("direction"),)
    if not _is_int(e.get("strength")) or e["strength"] not in (1, 2, 3):
        return "strength 须为整数 1~3：%r" % (e.get("strength"),)
    if e.get("source") not in SOURCE_WEIGHT:
        return "source 不在枚举 %s：%r" % (sorted(SOURCE_WEIGHT),
                                          e.get("source"))
    ev = e.get("evidence")
    if not isinstance(ev, str) or not ev.strip():
        return "evidence 须为非空字符串"
    tags = e.get("tags")
    if tags is not None and (not isinstance(tags, list)
                             or any(not isinstance(t, str) for t in tags)):
        return "tags 须为字符串数组：%r" % (tags,)
    scope = e.get("scope")
    if scope is not None and scope not in SCOPES:
        return "scope 仅许 %s：%r" % (sorted(SCOPES), scope)
    if sk == "market" and scope != "market_risk":
        return "sector_key=market 的条目必须带 scope=market_risk"
    return None


def validate_batch(entries, existing, whitelist, today):
    """整批校验：逐条 + 去重。返回错误列表（空 = 通过）。"""
    errors = []
    for i, e in enumerate(entries):
        err = validate_entry(e, whitelist, today)
        if err:
            errors.append("第 %d 条：%s" % (i + 1, err))
    if errors:
        return errors
    seen = {_entry_key(e) for e in existing}
    for i, e in enumerate(entries):
        k = _entry_key(e)
        if k in seen:
            errors.append("第 %d 条：与已有/批内条目完全重复"
                          "（date+sector+source+direction+strength"
                          "+evidence 全同）：%s %s %s"
                          % (i + 1, e["date"], e["sector_key"],
                             e["source"]))
        seen.add(k)
    return errors


def entries_sha256(entries):
    """与 screen 落库同口径：规范化 JSON（排序键、紧凑）后 sha256。"""
    raw = json.dumps(entries, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _append_receipt(receipt_path, entry):
    os.makedirs(os.path.dirname(receipt_path), exist_ok=True)
    with open(receipt_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ingest(batch_entries, news_path=DEFAULT_NEWS, map_path=DEFAULT_MAP,
           receipt_path=DEFAULT_RECEIPT, today=None, check_only=False):
    """执行一次入账（或 --check 校验）。返回结果 dict：
    {ok, added, total, latest_date, errors}。无论成败都落回执。"""
    today = today or date.today()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    receipt = {"ts": ts, "check_only": bool(check_only)}
    try:
        with open(news_path, encoding="utf-8") as f:
            book = json.load(f)
        existing = book.get("entries", [])
    except Exception as e:  # noqa: BLE001 - 账文件损坏时整批拒绝并留痕
        receipt.update(status="rejected",
                       errors=["现有账文件不可读：%s" % e])
        _append_receipt(receipt_path, receipt)
        return {"ok": False, "added": 0, "errors": receipt["errors"]}
    errors = validate_batch(batch_entries, existing,
                            sector_whitelist(map_path), today)
    sources = sorted({e.get("source") for e in batch_entries
                      if isinstance(e, dict)})
    receipt["sources"] = sources
    if errors:
        receipt.update(status="rejected", errors=errors,
                       batch_size=len(batch_entries))
        _append_receipt(receipt_path, receipt)
        return {"ok": False, "added": 0, "errors": errors}
    merged = existing + list(batch_entries)
    latest = max(e["date"] for e in merged) if merged else None
    receipt.update(status="ok" if not check_only else "checked",
                   added=len(batch_entries), total=len(merged),
                   latest_date=latest,
                   entries_sha256=entries_sha256(merged))
    if not check_only:
        book["entries"] = merged
        tmp = news_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(book, f, ensure_ascii=False, indent=2,
                      sort_keys=True)
        os.replace(tmp, news_path)
    _append_receipt(receipt_path, receipt)
    return {"ok": True, "added": len(batch_entries), "total": len(merged),
            "latest_date": latest, "errors": []}


def main():
    ap = argparse.ArgumentParser(description="板块新闻证据入账（校验+回执）")
    ap.add_argument("--file", required=True, help="批次 JSON（数组或 {entries:[...]}）")
    ap.add_argument("--news", default=DEFAULT_NEWS)
    ap.add_argument("--map", default=DEFAULT_MAP)
    ap.add_argument("--receipt", default=DEFAULT_RECEIPT)
    ap.add_argument("--today", default=None, help="校验基准日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--check", action="store_true", help="只校验不入账")
    args = ap.parse_args()
    with open(args.file, encoding="utf-8") as f:
        batch = json.load(f)
    entries = batch.get("entries") if isinstance(batch, dict) else batch
    if not isinstance(entries, list):
        print("入账拒绝：批次文件须为条目数组或 {entries: [...]}", file=sys.stderr)
        return 2
    today = (datetime.strptime(args.today, "%Y-%m-%d").date()
             if args.today else date.today())
    res = ingest(entries, news_path=args.news, map_path=args.map,
                 receipt_path=args.receipt, today=today,
                 check_only=args.check)
    if not res["ok"]:
        print("入账拒绝（整批）：", file=sys.stderr)
        for e in res["errors"]:
            print("  - %s" % e, file=sys.stderr)
        return 2
    print("入账回执：%s %d 条，账内共 %d 条，最新条目日期 %s（回执已留痕 %s）"
          % ("校验通过（未入账）" if args.check else "已入账",
             res["added"], res["total"], res["latest_date"],
             args.receipt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
