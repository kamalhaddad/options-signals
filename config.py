import os
from dotenv import load_dotenv

load_dotenv()

# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

# Watchlist
WATCHLIST = [
    # Big Tech / AI / Streaming
    "NVDA", "MSFT", "GOOGL", "META", "AMD", "AAPL", "AMZN", "TSLA",
    "PLTR", "SMCI", "ARM", "SNOW", "MRVL", "AVGO", "INTC",
    "ORCL", "IBM", "QCOM", "MU", "ADBE", "IONQ", "RGTI", "BBAI",
    "SOUN", "UPST", "PATH", "CDNS", "SNPS", "ANET", "NFLX",
    # Semiconductors / Memory
    "TSM", "ASML", "ON", "TXN", "WDC",
    # Tech / Telecom
    "BB", "NOK",
    # SaaS / Cybersecurity
    "CRM", "NOW", "DDOG", "NET", "CRWD", "MDB", "PANW", "SHOP",
    "HUBS", "TEAM", "OKTA", "FTNT",
    # Space & Defense / Drones
    "RTX", "LHX", "RKLB", "LUNR", "RDW", "ASTS",
    # Healthcare / Biotech / MedTech
    "UNH", "LLY", "JNJ", "ABBV", "ISRG", "DXCM", "VEEV", "MRNA",
    "REGN", "VRTX", "HIMS", "CRSP", "NVAX", "BSX",
    # Quantum Computing
    "QBTS", "ARQQ",
    # Nuclear / Energy
    "SMR", "OKLO", "NNE", "VST",
    # Robotics / Automation
    "SERV",
    # Crypto-Adjacent
    "COIN", "MSTR", "MARA",
    # Fintech / Consumer / Ride-hailing
    "AFRM", "DUOL", "CVNA", "HOOD", "SOFI", "PYPL", "UBER", "C",
    # Consumer / Retail / Social
    "NKE", "CELH", "RDDT",
    # Data Centers / AI Infrastructure / Mining
    "EQIX", "VRT", "CLS", "IREN", "NBIS", "APLD",
    # International / China Tech
    "BABA",
    # AI / Other
    "CRWV",
    # Energy / Solar
    "ENPH",
    # Other
    "RBRK",
    # Photonics / Optics / Lasers
    "LITE", "COHR", "MKSI", "IPGP", "AAOI", "AXTI",
    "LASR", "LFUS", "NOVT",
    # Indices / ETFs
    "SPY", "QQQ", "IWM", "ARKK",
]

# Scan interval in minutes
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "2"))

# Long/short symmetry — how the (direction-blind) volume signal votes.
#   "current"     = legacy, volume only votes bullish (CALL-biased; bot was long-only)
#   "directional" = volume confirms the move (heavy red bar votes bearish) → enables quality PUTs
VOL_MODE = os.getenv("VOL_MODE", "current")
# Broad-market (SPY) regime gate: only CALL when SPY is bullish, only PUT when bearish,
# stand down when chop. Suppresses low-quality counter-trend PUTs in strong uptrends.
MARKET_GATE = os.getenv("MARKET_GATE", "0") == "1"

# Signal thresholds (score ranges from -1.0 to +1.0)
BUY_THRESHOLD = 0.46    # High conviction
SELL_THRESHOLD = -0.25   # Exit on bearish flip
MIN_BULLISH_INDICATORS = 4  # Need 4/6 indicators agreeing

# Fixed stops — wider take profit to let winners run more
TRAILING_STOP_PCT = 2.0
TAKE_PROFIT_PCT = 2.5

# Session window — "morning-session-only" edge (validated on 215-trade month + 24h/72h):
# trade the open, but take NO new entries after 12:00 ET. Afternoon entries (chop,
# reversals, theta into the close) bleed; cutting them lifts win rate AND total return.
SKIP_OPEN_MINUTES = 0          # 0 = trade the open (the strong open-momentum trends)
SKIP_CLOSE_MINUTES = 15
ENTRY_CUTOFF = "12:00"         # no new entries after this (ET); "" = off

# Technical indicator weights (must sum to 1.0) — 6 indicators
WEIGHTS = {
    "rsi": 0.15,
    "macd": 0.25,
    "ema_cross": 0.10,
    "bollinger": 0.10,
    "stoch_rsi": 0.25,
    "volume": 0.15,
}

# Options-specific signal weights (added as bonus/penalty on top of technical score)
# These act as tiebreakers — boost good setups, penalize bad ones
OPTIONS_SIGNAL_WEIGHTS = {
    "iv_rank": 0.03,       # Low IV = cheap options = boost entry
    "put_call": 0.03,      # Bullish call flow = boost entry
    "unusual_vol": 0.02,   # Smart money entering = boost entry
}

# Indicator parameters — short periods for fast intraday signals
RSI_PERIOD = 7
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65

MACD_FAST = 5
MACD_SLOW = 13
MACD_SIGNAL = 4

EMA_FAST = 3
EMA_SLOW = 8

BOLLINGER_PERIOD = 10
BOLLINGER_STD = 1.5

STOCH_RSI_PERIOD = 7
STOCH_RSI_OVERSOLD = 25
STOCH_RSI_OVERBOUGHT = 75

VOLUME_SPIKE_MULTIPLIER = 1.2
