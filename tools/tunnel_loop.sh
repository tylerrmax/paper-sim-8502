#!/bin/bash
# Keep a Pinggy free tunnel alive: each session lives <60 min, run 50 min then refresh.
BASE="/home/hatch/workspace/paper-trading-sim"
while true; do
  timeout 3000 python3 "$BASE/tools/run_pinggy.py" >> /tmp/tunnel_loop.log 2>&1
  echo "$(date '+%F %T') tunnel session ended, restarting" >> /tmp/tunnel_loop.log
  sleep 5
done
