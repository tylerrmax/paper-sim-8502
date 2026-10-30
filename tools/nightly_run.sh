#!/usr/bin/env bash
# 夜间任务原始日志留存（2026-10-31 专项整改 P2-06）。
#
# 背景：此前 run_daily 的原始 stdout/stderr 未单独落盘，逐轮核查
# 只能依赖执行 agent 撰写的 result_summary，不可复核。本包装把
# 每次运行的原始输出按运行时刻留存为独立文件（只追加、永不
# 覆盖历史），并记录开始/结束时间、退出码、运行版本（git
# HEAD）、任务日期。日志内容不得包含 PAT/密码等敏感信息——
# 本包装只记录 run_daily 自身输出与上述元数据。
#
# 行为约束：
# - 包装的退出码 = 被包装命令的退出码，输出原样透传给调用方
#   （cron worker 的检查逻辑不受影响）；
# - 日志目录不可用时响亮打印 FAIL 行并照常执行任务本体
#   （日志失败不得静默忽略，也不得拖垮交易任务安全边界）。
#
# 用法：bash tools/nightly_run.sh
# 可调（仅供测试/演练）：NIGHTLY_CMD 覆盖被包装命令；
# PAPER_SIM_NIGHTLY_LOG_DIR 覆盖日志目录。
set -u
BASE="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${PAPER_SIM_NIGHTLY_LOG_DIR:-$HOME/workspace/goals/paper-trading-simulator/hidden_files/nightly_logs}"
CMD="${NIGHTLY_CMD:-$BASE/.venv/bin/python $BASE/run_daily.py}"
TS="$(date +%Y%m%d-%H%M%S)-$$"
TASK_DATE="$(date +%F)"
VERSION="$(git -C "$BASE" rev-parse HEAD 2>/dev/null || echo unknown)"
START="$(date -Is)"

if mkdir -p "$LOG_DIR" 2>/dev/null; then
  OUT="$LOG_DIR/$TS.stdout.log"
  ERR="$LOG_DIR/$TS.stderr.log"
  META="$LOG_DIR/$TS.meta.json"
  bash -c "$CMD" > "$OUT" 2> "$ERR"
  RC=$?
  END="$(date -Is)"
  printf '{"task":"run_daily","task_date":"%s","start":"%s","end":"%s","exit_code":%d,"version":"%s"}\n' \
    "$TASK_DATE" "$START" "$END" "$RC" "$VERSION" > "$META" \
    || echo "FAIL 夜间日志留存：元数据写入失败 $META" >&2
  cat "$OUT"
  cat "$ERR" >&2
  exit "$RC"
else
  echo "FAIL 夜间日志留存：日志目录不可用 $LOG_DIR（本次运行未留存原始日志，任务本体照常执行）" >&2
  bash -c "$CMD"
  exit $?
fi
