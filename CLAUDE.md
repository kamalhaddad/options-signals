# Options Signals Bot

Discord bot that analyzes technical indicators and options chain data to generate intraday call/put options scalping signals.

## Project Structure

```
options-signals/
├── bot.py            # Discord bot — runs continuously, scans on interval, posts signals
├── signals.py        # Technical indicator engine (RSI, MACD, EMA, BB, StochRSI, Volume)
├── options.py        # Options chain analysis (contract picker, IV rank, P/C ratio, unusual volume)
├── backtest.py       # Utility: intraday data fetching and per-bar signal computation
├── show_trades.py    # Backtest runner (yfinance, delta-approx P&L) — quick/free sanity tool
├── strategy_core.py  # Self-contained, parity-verified copy of the signal engine (shared)
├── thetadata_client.py # REST client for the local ThetaData Terminal (stock/option/greeks)
├── theta_backtest.py # Best-in-class backtest on REAL ThetaData option prices (see below)
├── STRATEGY.md       # ⭐ Tuned strategy, winning config (= defaults), findings & honest perf — READ FIRST
├── test_parity.py    # Verifies strategy_core matches the original engine exactly
├── config.py         # All tunable parameters (thresholds, weights, watchlist, indicator periods)
├── Dockerfile        # Container for deployment
├── docker-compose.yml
├── requirements.txt
├── .env.example      # Template for Discord token and channel ID
├── .gitignore
├── .dockerignore
├── DEPLOY.md         # DigitalOcean deployment guide
└── backtest.log      # Output from last backtest run
```

## How the Signal Engine Works

### Technical Indicators (6 — scored per 5-min bar)
Each returns a graduated score from -1.0 to +1.0:
- **RSI (7-period)** — momentum, oversold/overbought with graduated levels
- **MACD (5/13/4)** — trend momentum with magnitude-based scoring
- **EMA Cross (3/8)** — trend direction with spread-based strength
- **Bollinger Bands (10, 1.5σ)** — volatility/mean reversion
- **Stochastic RSI (7)** — momentum turning points with depth scoring
- **Volume** — conviction via spike detection (graduated by ratio)

Weights are in `config.WEIGHTS` (must sum to 1.0).

### Options-Specific Signals (3 — computed once per ticker from chain)
Added as a bonus/penalty on top of the technical score:
- **IV Rank** — low IV = cheap options = boost; high IV = expensive = penalize
- **Put/Call Volume Ratio** — low ratio = bullish call flow = boost
- **Unusual Volume vs OI** — high vol/OI = smart money entering = boost

Weights are in `config.OPTIONS_SIGNAL_WEIGHTS`.

### Entry/Exit Logic
- **BUY**: adjusted score (technical + options bonus) >= `BUY_THRESHOLD` AND `MIN_BULLISH_INDICATORS` out of 6 are positive
- **SELL**: score <= `SELL_THRESHOLD`
- **Take Profit**: stock price rises `TAKE_PROFIT_PCT`% from entry
- **Stop Loss**: stock price drops `TRAILING_STOP_PCT`% from peak (trailing)
- **Time filters**: skips first `SKIP_OPEN_MINUTES` and last `SKIP_CLOSE_MINUTES` of trading day

### Contract Selection
When a signal fires, `options.py` picks the optimal contract:
- Nearest expiry 3-14 days out
- Slightly OTM strike (1-3% above for calls)
- Filters by: IV < 150%, spread < 15%, open interest > 10
- Ranks by composite score: distance to target (40%), spread tightness (30%), open interest (30%)

## Key Commands

### Run backtest — real options data (ThetaData) ⭐ best-in-class
Uses REAL underlying bars, the REAL option chain, and REAL per-bar bid/ask. Fills
enter at ask / exit at bid (+$0.65/contract); premium-based −25%/+40% exits; CALLs
on BUY, PUTs on bearish. Needs the **ThetaData Terminal** running locally:
```bash
# 1) Start the ThetaData Terminal (keep it running; binds 127.0.0.1:25510)
java -jar ~/ThetaTerminal.jar <email> <password>
# 2) Run the backtest (any historical date range ThetaData covers)
.venv/bin/python theta_backtest.py --tickers NVDA --start 2024-03-04 --end 2024-03-08
.venv/bin/python theta_backtest.py --tickers NVDA,AAPL,SPY --start 2024-03-01 --end 2024-03-29
```
Notes: runs on the host (no Docker) so the Terminal's single-client-IP lock stays
consistent. `strategy_core.py` holds the (parity-verified) signal engine; verify it
still matches the original with `.venv/bin/python test_parity.py`.

### Run backtest — quick/free (yfinance, delta-approx P&L)
```bash
python show_trades.py                # last 24 hours
python show_trades.py --hours 72     # last 3 days
python show_trades.py --hours 120    # last 5 days (max for 5-min candles)
```
Optimistic (no theta/gamma/real fills) — use as a fast sanity check, not for truth.

### Run the Discord bot
```bash
# Set up .env first (copy from .env.example)
python bot.py
```

### Docker
```bash
docker compose up -d --build   # start
docker compose logs -f         # view logs
docker compose down            # stop
```

### Discord bot commands
- `!scan` — manually trigger a full watchlist scan
- `!check TSLA` — analyze a specific ticker
- `!watchlist` — show current watchlist
- `!status` — show bot settings

## Tuning the Strategy

All parameters are in `config.py`:

| Parameter | Current | Effect |
|-----------|---------|--------|
| `BUY_THRESHOLD` | 0.46 | Higher = fewer but higher conviction trades |
| `SELL_THRESHOLD` | -0.25 | Lower = faster exits on bearish flip |
| `MIN_BULLISH_INDICATORS` | 4 | Higher = more indicators must agree |
| `TAKE_PROFIT_PCT` | 2.5 | Higher = let winners run more |
| `TRAILING_STOP_PCT` | 2.0 | Higher = more room before stop-out |
| `SKIP_OPEN_MINUTES` | 20 | Skip volatile open |
| `SKIP_CLOSE_MINUTES` | 15 | Skip volatile close |

Indicator periods (RSI_PERIOD, MACD_FAST/SLOW/SIGNAL, etc.) are tuned for fast intraday signals. Standard periods work better for swing trading.

## Watchlist

Defined in `config.WATCHLIST`. Currently covers: Big Tech/AI, Semiconductors, SaaS/Cybersecurity, Space/Defense, Healthcare/Biotech, Quantum Computing, Nuclear/Energy, Crypto, Fintech, Data Centers, Photonics/Lasers, and major ETFs (SPY, QQQ, IWM).

## Dependencies

- `discord.py` — Discord bot framework
- `yfinance` — market data and options chains (free)
- `pandas` / `numpy` — data processing
- `python-dotenv` — environment variable management

## Notes

- The backtest uses estimated option P&L via delta approximation, not actual contract price tracking
- yfinance has rate limits — scanning 100+ tickers takes 2-3 minutes
- Options chain data may be delayed 15-30 minutes
- All signals are for educational purposes — not financial advice
