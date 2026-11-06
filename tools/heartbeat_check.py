#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时行情心跳检查 CLI（第二层 P3，只读）。

打印各行情源最近 request/response/valid/persisted 时间与异常判定；
存在异常时 exit 1，否则 exit 0。供未来接入告警通道，本次不接任何
生产调度。心跳异常只告警，绝不触发交易。

用法：
    python tools/heartbeat_check.py [--path reports/heartbeat.jsonl]
                                    [--now 2026-11-06T10:00:00+08:00]
"""
import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import heartbeat  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="实时行情心跳检查（只读）")
    ap.add_argument("--path", default=None,
                    help="心跳 JSONL 路径（默认 reports/heartbeat.jsonl）")
    ap.add_argument("--now", default=None,
                    help="判定时刻（ISO 格式，默认当前时间）")
    args = ap.parse_args(argv)

    now = datetime.fromisoformat(args.now) if args.now else None
    st = heartbeat.status(now=now, path=args.path)
    print(f"心跳检查 @ {st['generated_at']}")
    for source, info in st["sources"].items():
        flag = "异常" if info["anomaly"] else "正常"
        print(f"[{flag}] {source}（{info['label']}）：{info['reason']}")
        last = info["last"]
        print(f"       request={last['request']}  response={last['response']}"
              f"  valid={last['valid']}  persisted={last['persisted']}")
        if info["valid_age_min"] is not None:
            print(f"       最近有效行情距今 {info['valid_age_min']} 分钟")
    return 1 if st["anomaly"] else 0


if __name__ == "__main__":
    sys.exit(main())
