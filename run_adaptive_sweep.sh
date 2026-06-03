#!/usr/bin/env bash
# Adaptive-exit comparison vs the fixed +40/-50 baseline. ALL runs are --offline:
# they reuse the May cache warmed by run_trail_sweep.sh's baseline. Same window +
# tickers throughout so the only variable is the exit logic.
set -uo pipefail
cd /home/kamh/code/options-signals/options-signals
PY=.venv/bin/python
START=${1:-2026-05-01}
END=${2:-2026-05-30}
TK=all
OUT=/tmp/adaptive_sweep
mkdir -p $OUT

run () { # name  args...
  local name=$1; shift
  echo "######## $name :: $* ########"
  $PY theta_backtest.py --tickers $TK --start $START --end $END --offline \
      --dump $OUT/${name}.json --log $OUT/${name}.log "$@" 2>&1 | tee $OUT/${name}.out
  echo
}

# Reference: current live behaviour (fixed +40 TP / -50 stop)
run A_baseline_fixed   --exit-mode baseline --tp 40 --stop 50

# 1) Signal-decay: ride until momentum fades; -60% catastrophic backstop
run B_decay            --exit-mode decay --decay-floor 0.15 --catastrophic 60
run B_decay_loose      --exit-mode decay --decay-floor 0.00 --catastrophic 60

# 2) Ratchet: -35% init stop -> breakeven after +25% -> trail 20% off peak
run C_ratchet          --exit-mode ratchet --init-stop 35 --be-trigger 25 --trail 20
run C_ratchet_tight    --exit-mode ratchet --init-stop 30 --be-trigger 20 --trail 15

# 3) Vol-scaled brackets: TP/SL scaled by option IV (ref 0.6 = 1.0x)
run D_vol              --exit-mode vol --tp 40 --stop 50 --ref-iv 0.6

# 4) Combo: IV-scaled init stop + breakeven ratchet (down) + signal-decay TP (up)
run E_combo            --exit-mode combo --init-stop 35 --be-trigger 25 --trail 20 --decay-floor 0.15 --ref-iv 0.6

echo "ALL ADAPTIVE DONE"
