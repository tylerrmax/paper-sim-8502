#!/bin/bash
# Idempotent: install units if missing, start services, wait for a fresh tunnel URL.
BASE="/home/hatch/workspace/paper-trading-sim"
changed=0
for u in paper-sim-8502 paper-sim-tunnel; do
  if ! systemctl cat "$u" >/dev/null 2>&1; then
    cp "$BASE/tools/systemd/$u.service" /etc/systemd/system/ && changed=1
  fi
done
[ "$changed" = 1 ] && systemctl daemon-reload
systemctl enable --now paper-sim-8502 paper-sim-tunnel >/dev/null 2>&1
systemctl is-active --quiet paper-sim-8502 || systemctl restart paper-sim-8502
systemctl is-active --quiet paper-sim-tunnel || systemctl restart paper-sim-tunnel
for i in $(seq 1 12); do
  if [ -f "$BASE/tunnel_url.txt" ]; then
    url=$(head -n1 "$BASE/tunnel_url.txt")
    age=$(( $(date +%s) - $(stat -c %Y "$BASE/tunnel_url.txt") ))
    if [ -n "$url" ] && [ "$age" -lt 3300 ]; then
      code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$url/_stcore/health" 2>/dev/null)
      if [ "$code" = "200" ]; then echo "OK $url"; exit 0; fi
    fi
  fi
  sleep 10
done
echo "TUNNEL_DOWN"
exit 1
