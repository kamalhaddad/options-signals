#!/usr/bin/env bash
# Trailing-stop vs fixed +40% TP comparison. Baseline warms the cache (online),
# the rest reuse it (--offline). All trades are intraday so the month's quotes
# cached by the baseline cover every variant. Same window + tickers throughout.
set -uo pipefail
cd /home/kamh/code/options-signals/options-signals
PY=.venv/bin/python
START=2026-05-01
END=2026-05-30
TK=all
OUT=/tmp/trail_sweep
mkdir -p $OUT

run () { # name  extra-args...  (first run must be ONLINE to warm cache)
  local name=$1; shift
  echo "######## $name :: $* ########"
  $PY theta_backtest.py --tickers $TK --start $START --end $END \
      --dump $OUT/${name}.json --log $OUT/${name}.log "$@" 2>&1 | tee $OUT/${name}.out
  echo
}

# 1) BASELINE = current live config: fixed +40% TP, -50% stop, no trail (ONLINE → warms cache)
run baseline_tp40

# 2) Pure trailing (cap disabled with --tp 9999) at several widths — OFFLINE
run trail20 --tp 9999 --trail 20 --offline
run trail25 --tp 9999 --trail 25 --offline
run trail30 --tp 9999 --trail 30 --offline
run trail35 --tp 9999 --trail 35 --offline

# 3) Hybrid: higher cap (+75%) but trail 25% under it — OFFLINE
run hybrid_tp75_tr25 --tp 75 --trail 25 --offline

echo "ALL DONE"
