"""
Postgres persistence for the live bot — the single source of truth.

Why a DB (see the chat): durability/reliability across restarts AND a permanent, queryable
trade history. A managed Postgres lives OFF the droplet, so data survives a droplet failure.

  - `trades`  : one row per position. Open = `status='open'` (written the instant we enter,
                so a crash between the BUY alert and the next scan can't lose it). Closed =
                `status='closed'` with exit/pnl. This replaces positions.json entirely:
                open positions on restart = SELECT … WHERE status='open'; history = closed.
  - `signals` : every actionable evaluation (for live-vs-backtest analysis).

Connection: `DATABASE_URL` (e.g. the managed-PG URI incl. ?sslmode=require). Autocommit so
each entry/exit is durable immediately. Reconnects on a dropped connection.
"""
from __future__ import annotations
import os
import logging

import psycopg2
import psycopg2.extras

log = logging.getLogger("db")
DATABASE_URL = os.getenv("DATABASE_URL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id          BIGSERIAL PRIMARY KEY,
    ticker      TEXT NOT NULL,
    direction   TEXT NOT NULL,
    strike      INTEGER NOT NULL,
    strike_d    DOUBLE PRECISION NOT NULL,
    exp         INTEGER NOT NULL,
    exp_date    DATE,
    qty         INTEGER NOT NULL DEFAULT 1,
    score       DOUBLE PRECISION,
    entry_px    DOUBLE PRECISION NOT NULL,
    entry_time  TIMESTAMPTZ NOT NULL,
    exit_px     DOUBLE PRECISION,
    exit_time   TIMESTAMPTZ,
    pnl_pct     DOUBLE PRECISION,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    opened_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_trades_status   ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_closed   ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_ticker   ON trades(ticker);

CREATE TABLE IF NOT EXISTS signals (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    ticker      TEXT NOT NULL,
    spot        DOUBLE PRECISION,
    score       DOUBLE PRECISION,
    adx         DOUBLE PRECISION,
    bullish     INTEGER,
    bearish     INTEGER,
    direction   TEXT,
    in_window   BOOLEAN,
    acted       BOOLEAN,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts     ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# ET-local date of a timestamptz column, for grouping trades by trading day.
_ET_DATE = "(closed_at AT TIME ZONE 'America/New_York')::date"

_conn = None


def configured() -> bool:
    return bool(DATABASE_URL)


def _connect():
    global _conn
    _conn = psycopg2.connect(DATABASE_URL)
    _conn.autocommit = True
    return _conn


def _run(query: str, params=None, fetch: str | None = None):
    """Execute with one reconnect retry. fetch: 'one' | 'all' | None."""
    global _conn
    for attempt in range(2):
        try:
            if _conn is None or _conn.closed:
                _connect()
            with _conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(query, params or ())
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            log.warning(f"db connection issue (attempt {attempt + 1}): {e}")
            try:
                _conn and _conn.close()
            except Exception:
                pass
            _conn = None
            if attempt == 1:
                raise


def init() -> None:
    _run(SCHEMA)


# ── trades (source of truth) ──────────────────────────────────────────────────
def open_trade(t: dict) -> int:
    """Insert an open position the instant we enter. Returns the trade id."""
    row = _run(
        """INSERT INTO trades (ticker,direction,strike,strike_d,exp,exp_date,qty,score,entry_px,entry_time,status)
           VALUES (%(ticker)s,%(direction)s,%(strike)s,%(strike_d)s,%(exp)s,%(exp_date)s,%(qty)s,%(score)s,%(entry_px)s,%(entry_time)s,'open')
           RETURNING id""", t, fetch="one")
    return row["id"]


def close_trade(trade_id: int, exit_px, pnl_pct, reason: str, closed_at) -> None:
    _run("""UPDATE trades SET exit_px=%s, pnl_pct=%s, reason=%s, exit_time=%s, closed_at=%s, status='closed'
            WHERE id=%s AND status='open'""",
         (exit_px, pnl_pct, reason, closed_at, closed_at, trade_id))


def open_positions() -> list:
    return _run("SELECT * FROM trades WHERE status='open' ORDER BY opened_at", fetch="all") or []


def last_close_time(ticker: str):
    r = _run("SELECT max(closed_at) AS t FROM trades WHERE ticker=%s AND status='closed'", (ticker,), fetch="one")
    return r["t"] if r else None


def closed_on(date_str: str) -> list:
    return _run(f"SELECT *, {_ET_DATE} AS et_date FROM trades WHERE status='closed' AND {_ET_DATE} = %s ORDER BY closed_at",
                (date_str,), fetch="all") or []


def closed_between(d1: str, d2: str) -> list:
    return _run(f"SELECT *, {_ET_DATE} AS et_date FROM trades WHERE status='closed' AND {_ET_DATE} BETWEEN %s AND %s ORDER BY closed_at",
                (d1, d2), fetch="all") or []


# ── analytics ─────────────────────────────────────────────────────────────────
def stats() -> dict:
    return _run("""SELECT count(*) AS n,
                          count(*) FILTER (WHERE pnl_pct > 0) AS wins,
                          COALESCE(sum(pnl_pct), 0) AS total_pct,
                          COALESCE(avg(pnl_pct), 0) AS avg_pct
                   FROM trades WHERE status='closed'""", fetch="one") or {}


def per_ticker(limit: int = 15) -> list:
    return _run("""SELECT ticker,
                          count(*) AS n,
                          count(*) FILTER (WHERE pnl_pct > 0) AS wins,
                          round(COALESCE(sum(pnl_pct), 0)::numeric, 0) AS total_pct,
                          round(COALESCE(avg(pnl_pct), 0)::numeric, 1) AS avg_pct
                   FROM trades WHERE status='closed'
                   GROUP BY ticker ORDER BY total_pct DESC LIMIT %s""", (limit,), fetch="all") or []


# ── meta (small key/value: summary-posted flags, etc.) ────────────────────────
def get_meta(key: str, default=None):
    r = _run("SELECT value FROM meta WHERE key=%s", (key,), fetch="one")
    return r["value"] if r else default


def set_meta(key: str, value: str) -> None:
    _run("INSERT INTO meta (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
         (key, value))


# ── signal log ────────────────────────────────────────────────────────────────
def log_signal(s: dict) -> None:
    _run("""INSERT INTO signals (ts,ticker,spot,score,adx,bullish,bearish,direction,in_window,acted,note)
            VALUES (%(ts)s,%(ticker)s,%(spot)s,%(score)s,%(adx)s,%(bullish)s,%(bearish)s,%(direction)s,%(in_window)s,%(acted)s,%(note)s)""", s)
