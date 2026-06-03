#!/usr/bin/env bash
# Long/short SYMMETRY sweep: do PUTs fire, and is the CALL side preserved?
# 4 configs x 2 windows (bear + bull), live gate (--rs 0.5, adx>30, baseline exits).
# 24 liquid names. Online (PUT quotes aren't cached). Same tickers throughout.
set -uo pipefail
cd /home/kamh/code/options-signals/options-signals
PY=.venv/bin/python
TK=NVDA,AMD,MU,SMCI,AVGO,TSLA,COIN,MSTR,MARA,PLTR,SOFI,HOOD,AAPL,MSFT,META,GOOGL,AMZN,NFLX,CRM,ORCL,BABA,UBER,SHOP,NOW
OUT=/tmp/vol_sweep
mkdir -p $OUT

run () { # name window-start window-end  extra-args...
  local name=$1 ws=$2 we=$3; shift 3
  echo "######## $name [$ws..$we] :: $* ########"
  $PY theta_backtest.py --tickers $TK --start $ws --end $we \
      --rs 0.5 --adx-gate 30 --exit-mode baseline \
      --dump $OUT/${name}.json --log $OUT/${name}.log "$@" 2>&1 | tail -3
  echo
}

for win in "bear 2026-03-23 2026-03-31" "bull 2026-05-15 2026-05-29"; do
  set -- $win; tag=$1; ws=$2; we=$3
  run A_baseline_$tag   $ws $we --vol-mode current
  run B_directional_$tag $ws $we --vol-mode directional
  run C_conviction_$tag  $ws $we --vol-mode conviction --min-conv 3
  run D_asym_$tag        $ws $we --vol-mode current --put-min-conv 3
done
echo "ALL VOL-SWEEP DONE"
