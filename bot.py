"""
Live options-signal Discord bot — posts the TUNED strategy's entry AND exit alerts.

State lives in Postgres (`db.py`) as the single source of truth:
  - Every scan loads OPEN positions FROM THE DB, so a restart/redeploy resumes managing them
    (positions are never held only in memory). Entries/exits commit immediately (per-trade),
    so a crash can't lose a position between the alert and the next scan.
  - Daily (after close) and weekly (Friday) summaries + !stats/!perticker read from the DB.
  - Every actionable signal is logged for later live-vs-backtest analysis.

Needs: ThetaData Terminal reachable (same host), Discord creds, and DATABASE_URL — see config.py / .env.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, time as dtime

import discord
from discord.ext import commands, tasks

import config
import session
import db
import strategy_core as sc
from live_engine import LiveEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

COOLDOWN_MIN = int(os.getenv("COOLDOWN_MINUTES", "30"))   # minutes between re-entries per ticker
RS_QUANTILE = float(os.getenv("RS_QUANTILE", "0.5"))      # enter only top-X% leaders by return-since-open (0 = off)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def _right(direction: str) -> str:
    return "C" if direction == "CALL" else "P"


def _posview(row: dict) -> dict:
    """Adapt a DB trade row to the dict shape live_engine/exit_embed expect."""
    return {"ticker": row["ticker"], "dir": row["direction"], "exp": row["exp"],
            "strike": row["strike"], "strike_d": row["strike_d"], "right": _right(row["direction"]),
            "entry": row["entry_px"], "exp_date": str(row.get("exp_date") or "")}


# ── embeds ───────────────────────────────────────────────────────────────────────
def entry_embed(st: dict, direction: str, c: dict) -> discord.Embed:
    is_call = direction == "CALL"
    color = discord.Color.green() if is_call else discord.Color.red()
    emoji = "\U0001f7e2" if is_call else "\U0001f534"
    e = discord.Embed(title=f"{emoji} BUY {direction}: {st['ticker']} @ ${st['spot']:.2f}",
                      color=color, timestamp=datetime.now())
    e.add_field(name="Score", value=f"{st['score']:+.2f}", inline=True)
    e.add_field(name="ADX", value=f"{st['adx']:.0f}", inline=True)
    e.add_field(name="Agree", value=f"{st['bullish'] if is_call else st['bearish']}/5", inline=True)
    e.add_field(name="\U0001f4b0 Contract",
                value=(f"**${c['strike_d']:.0f} {direction}** exp {c['exp_date']} ({c['dte']}d)\n"
                       f"Ask **${c['ask']:.2f}**  bid ${c['bid']:.2f}  (spread {c['spread']:.1f}%)\n"
                       f"{c['otm']:+.1f}% OTM" + (f"  •  IV {c['iv']*100:.0f}%" if c.get('iv') else "")),
                inline=False)
    e.add_field(name="\U0001f3af Manage",
                value=(f"Take profit **+{int(sc.TAKE_PROFIT_PREMIUM_PCT)}%** premium  •  "
                       f"Stop **-{int(sc.STOP_LOSS_PREMIUM_PCT)}%**  •  exit on opposite signal / EOD"),
                inline=False)
    e.set_footer(text="Educational, not financial advice • manage your risk")
    return e


def exit_embed(pos: dict, reason: str, exit_bid) -> discord.Embed:
    pnl = ((exit_bid - pos["entry"]) / pos["entry"] * 100) if (exit_bid and pos["entry"]) else None
    win = (pnl is not None and pnl > 0)
    emoji = "✅" if win else ("\U0001f534" if pnl is not None else "⚪")
    color = discord.Color.green() if win else (discord.Color.red() if pnl is not None else discord.Color.greyple())
    e = discord.Embed(title=f"{emoji} CLOSE {pos['dir']}: {pos['ticker']} ${pos['strike_d']:.0f}",
                      color=color, timestamp=datetime.now())
    e.add_field(name="Entry", value=f"${pos['entry']:.2f}", inline=True)
    e.add_field(name="Exit (bid)", value=(f"${exit_bid:.2f}" if exit_bid else "n/a"), inline=True)
    e.add_field(name="P&L", value=(f"{pnl:+.0f}%" if pnl is not None else "—"), inline=True)
    e.add_field(name="Reason", value=reason, inline=False)
    e.set_footer(text="Educational, not financial advice")
    return e


def summary_embed(date_str: str, trades: list) -> discord.Embed:
    """End-of-day recap: ✅ wins / ❌ losses + total P&L %."""
    wins = [t for t in trades if t["pnl_pct"] and t["pnl_pct"] > 0]
    total = sum((t["pnl_pct"] or 0) for t in trades)
    color = discord.Color.green() if total >= 0 else discord.Color.red()
    e = discord.Embed(title=f"\U0001f4ca Daily Summary — {date_str}", color=color, timestamp=datetime.now())
    if not trades:
        e.description = "No trades today."
        return e
    lines = []
    for t in trades:
        mark = "✅" if (t["pnl_pct"] or 0) > 0 else "❌"
        lines.append(f"{mark} **{t['ticker']}** {t['direction']} ${t['strike_d']:.0f}  →  {t['pnl_pct']:+.0f}%")
    body = "\n".join(lines)
    if len(body) > 1000:
        body = "\n".join(lines[:24]) + f"\n… +{len(lines) - 24} more"
    e.add_field(name=f"Trades ({len(trades)})", value=body, inline=False)
    e.add_field(name="Win rate", value=f"{len(wins)}/{len(trades)} ({len(wins)/len(trades)*100:.0f}%)", inline=True)
    e.add_field(name="Total P&L", value=f"**{total:+.0f}%**", inline=True)
    e.set_footer(text="Sum of per-trade % • educational, not financial advice")
    return e


def weekly_summary_embed(week_label: str, trades: list) -> discord.Embed:
    """Friday recap of the whole week: per-day breakdown + totals."""
    wins = [t for t in trades if t["pnl_pct"] and t["pnl_pct"] > 0]
    total = sum((t["pnl_pct"] or 0) for t in trades)
    color = discord.Color.green() if total >= 0 else discord.Color.red()
    e = discord.Embed(title=f"\U0001f4c5 Weekly Summary — {week_label}", color=color, timestamp=datetime.now())
    if not trades:
        e.description = "No trades this week."
        return e
    by_day: dict = {}
    for t in trades:
        by_day.setdefault(str(t["et_date"]), []).append(t)
    day_lines = []
    for d in sorted(by_day):
        dts = by_day[d]
        dtot = sum((x["pnl_pct"] or 0) for x in dts)
        dw = sum(1 for x in dts if (x["pnl_pct"] or 0) > 0)
        dow = datetime.strptime(d, "%Y-%m-%d").strftime("%a")
        mark = "✅" if dtot >= 0 else "❌"
        day_lines.append(f"{mark} {dow} {d[5:]} — {len(dts)} trades · {dw}/{len(dts)}W · **{dtot:+.0f}%**")
    e.add_field(name="By day", value="\n".join(day_lines), inline=False)
    e.add_field(name="Trades", value=str(len(trades)), inline=True)
    e.add_field(name="Win rate", value=f"{len(wins)}/{len(trades)} ({len(wins)/len(trades)*100:.0f}%)", inline=True)
    e.add_field(name="Total P&L", value=f"**{total:+.0f}%**", inline=True)
    best, worst = max(trades, key=lambda x: x["pnl_pct"] or 0), min(trades, key=lambda x: x["pnl_pct"] or 0)
    e.add_field(name="Best / Worst",
                value=f"✅ {best['ticker']} {best['direction']} {best['pnl_pct']:+.0f}%   /   "
                      f"❌ {worst['ticker']} {worst['direction']} {worst['pnl_pct']:+.0f}%", inline=False)
    e.set_footer(text="Sum of per-trade % • educational, not financial advice")
    return e


# ── core scan (sync; runs in a worker thread) ─────────────────────────────────────
def scan() -> list:
    """One pass. Open positions come FROM THE DB (restart-safe); entries/exits commit immediately."""
    eng = LiveEngine()
    now = session.now_et()
    eod = session.is_eod(now)
    can_enter = session.in_entry_window(now)
    open_pos = {r["ticker"]: r for r in db.open_positions()}   # <-- restart-resume: source of truth is the DB
    out: list = []

    # pass 1: latest signal for every ticker (one fetch each)
    states: dict = {}
    for tk in config.WATCHLIST:
        try:
            states[tk] = eng.latest(tk)
        except Exception as ex:
            log.warning(f"{tk}: latest() failed: {ex}")
            states[tk] = None

    # cross-sectional relative strength: leaders = top RS_QUANTILE by return-since-open
    leaders = None
    if 0 < RS_QUANTILE < 1:
        ranked = sorted(((s["ret_open"], tk) for tk, s in states.items()
                         if s is not None and s.get("ret_open") is not None), reverse=True)
        k = max(1, int(len(ranked) * RS_QUANTILE))
        leaders = {tk for _, tk in ranked[:k]}

    # pass 2: manage exits (always) and consider entries (gated by RS)
    for tk in config.WATCHLIST:
        st = states.get(tk)

        if tk in open_pos:
            pos = _posview(open_pos[tk])
            if st is None and not eod:
                continue
            reason = eng.exit_reason(pos, st, eod)
            if reason:
                bid = eng.current_bid(tk, pos["exp"], pos["strike"], pos["right"])
                pnl = ((bid - pos["entry"]) / pos["entry"] * 100) if (bid and pos["entry"]) else 0.0
                db.close_trade(open_pos[tk]["id"], bid, pnl, reason, now)
                out.append(("exit", exit_embed(pos, reason, bid)))
            continue

        if st is None:
            continue
        direction = eng.entry_direction(st)
        if not direction:
            continue
        rs_leader = leaders is None or tk in leaders
        acted = False
        if can_enter and rs_leader:
            lc = db.last_close_time(tk)
            on_cooldown = lc is not None and (now - lc).total_seconds() < COOLDOWN_MIN * 60
            if not on_cooldown:
                c = eng.pick_and_quote(tk, direction, st["spot"], st["time"])
                if c:
                    db.open_trade({
                        "ticker": tk, "direction": direction, "strike": c["strike"], "strike_d": c["strike_d"],
                        "exp": c["exp"], "exp_date": c["exp_date"], "qty": 1, "score": round(st["score"], 3),
                        "entry_px": c["ask"], "entry_time": now,
                    })
                    out.append(("entry", entry_embed(st, direction, c)))
                    acted = True
        try:   # log the decision (incl. RS-blocked laggards) for live-vs-backtest analysis
            db.log_signal({"ts": now, "ticker": tk, "spot": st["spot"], "score": st["score"],
                           "adx": st["adx"], "bullish": st["bullish"], "bearish": st["bearish"],
                           "direction": direction, "in_window": can_enter, "acted": acted,
                           "note": None if rs_leader else "rs_laggard"})
        except Exception as ex:
            log.warning(f"{tk}: log_signal failed: {ex}")
    return out


# ── EOD / weekly collectors ───────────────────────────────────────────────────────
def eod_collect():
    """After close: flatten any straggler positions, mark the day, return (date, today's trades)."""
    now = session.now_et()
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or now.time() < dtime(16, 0) or session.is_market_open(now):
        return None
    if db.get_meta("summary_posted") == today:
        return None
    eng = None
    for row in db.open_positions():                 # safety net: close anything still open
        eng = eng or LiveEngine()
        pos = _posview(row)
        bid = eng.current_bid(pos["ticker"], pos["exp"], pos["strike"], pos["right"])
        pnl = ((bid - pos["entry"]) / pos["entry"] * 100) if (bid and pos["entry"]) else 0.0
        db.close_trade(row["id"], bid, pnl, "end of day", now)
    db.set_meta("summary_posted", today)
    return today, db.closed_on(today)


def _week_bounds(now):
    iso = now.isocalendar()
    monday = now - timedelta(days=now.weekday())
    return f"{iso[0]}-W{iso[1]:02d}", monday.strftime("%Y-%m-%d"), (monday + timedelta(days=4)).strftime("%Y-%m-%d")


def weekly_collect():
    """After Friday's close (through the weekend): post the week's recap once."""
    now = session.now_et()
    wd = now.weekday()
    if not ((wd == 4 and now.time() >= dtime(16, 0)) or wd in (5, 6)):
        return None
    week_label, monday, friday = _week_bounds(now)
    if db.get_meta("week_summary_posted") == week_label:
        return None
    db.set_meta("week_summary_posted", week_label)
    return week_label, db.closed_between(monday, friday)


# ── discord plumbing ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info(f"Connected as {bot.user} | watching {len(config.WATCHLIST)} tickers | scan {config.SCAN_INTERVAL_MINUTES}m")
    if not scanner_loop.is_running():
        scanner_loop.start()
    if not eod_loop.is_running():
        eod_loop.start()


@tasks.loop(minutes=config.SCAN_INTERVAL_MINUTES)
async def scanner_loop():
    channel = bot.get_channel(config.CHANNEL_ID)
    if channel is None:
        log.error(f"Channel {config.CHANNEL_ID} not found")
        return
    if not session.is_market_open():
        log.info("market closed — skipping")
        return
    log.info("scanning…")
    posts = await asyncio.to_thread(scan)
    for kind, embed in posts:
        await channel.send(embed=embed)
        await asyncio.sleep(1)
    log.info(f"posted {len(posts)} alert(s)")


@scanner_loop.before_loop
async def _before():
    await bot.wait_until_ready()


@tasks.loop(minutes=10)
async def eod_loop():
    """After the close: DAILY recap (weekdays) and, on Friday, the WEEKLY recap. Each fires once."""
    channel = bot.get_channel(config.CHANNEL_ID)
    if channel is None:
        return
    res = await asyncio.to_thread(eod_collect)
    if res is not None:
        date_str, trades = res
        if trades:
            await channel.send(embed=summary_embed(date_str, trades))
            log.info(f"posted daily summary {date_str}: {len(trades)} trades")
    wres = await asyncio.to_thread(weekly_collect)
    if wres is not None:
        week_label, wtrades = wres
        if wtrades:
            await channel.send(embed=weekly_summary_embed(week_label, wtrades))
            log.info(f"posted weekly summary {week_label}: {len(wtrades)} trades")


@eod_loop.before_loop
async def _before_eod():
    await bot.wait_until_ready()


# ── commands ──────────────────────────────────────────────────────────────────────
@bot.command(name="check")
async def check(ctx, ticker: str):
    """!check NVDA — latest signal + would-be entry for one ticker."""
    tk = ticker.upper()
    await ctx.send(f"\U0001f50d Analyzing **{tk}**…")
    eng = LiveEngine()
    st = await asyncio.to_thread(eng.latest, tk)
    if st is None:
        await ctx.send(f"❌ No data for **{tk}** (market closed or insufficient bars)")
        return
    d = eng.entry_direction(st)
    if d and session.in_entry_window():
        c = await asyncio.to_thread(eng.pick_and_quote, tk, d, st["spot"], st["time"])
        if c:
            await ctx.send(embed=entry_embed(st, d, c)); return
    await ctx.send(f"⚪ **{tk}** @ ${st['spot']:.2f} — score {st['score']:+.2f}, ADX {st['adx']:.0f} "
                   f"→ {'would buy ' + d + ' but outside entry window' if d else 'no signal'}")


@bot.command(name="positions")
async def positions_cmd(ctx):
    """!positions — currently open signal positions (from the DB)."""
    rows = await asyncio.to_thread(db.open_positions)
    if not rows:
        await ctx.send("\U0001f4ed No open positions."); return
    lines = [f"• **{r['ticker']}** {r['direction']} ${r['strike_d']:.0f} {r['exp_date']} @ ${r['entry_px']:.2f}"
             for r in rows]
    await ctx.send("\U0001f4ca **Open positions:**\n" + "\n".join(lines))


@bot.command(name="summary")
async def summary_cmd(ctx):
    """!summary — today's closed trades so far."""
    today = session.now_et().strftime("%Y-%m-%d")
    await ctx.send(embed=summary_embed(today, await asyncio.to_thread(db.closed_on, today)))


@bot.command(name="weeksummary")
async def weeksummary_cmd(ctx):
    """!weeksummary — this week's closed trades so far."""
    label, monday, friday = _week_bounds(session.now_et())
    await ctx.send(embed=weekly_summary_embed(label, await asyncio.to_thread(db.closed_between, monday, friday)))


@bot.command(name="stats")
async def stats_cmd(ctx):
    """!stats — all-time performance (closed trades)."""
    s = await asyncio.to_thread(db.stats)
    n = s.get("n", 0) or 0
    if not n:
        await ctx.send("\U0001f4c8 No closed trades yet."); return
    wins = s.get("wins", 0) or 0
    e = discord.Embed(title="\U0001f4c8 All-time performance", color=discord.Color.blurple())
    e.add_field(name="Trades", value=str(n), inline=True)
    e.add_field(name="Win rate", value=f"{wins}/{n} ({wins/n*100:.0f}%)", inline=True)
    e.add_field(name="Avg/trade", value=f"{float(s['avg_pct']):+.1f}%", inline=True)
    e.add_field(name="Total P&L", value=f"**{float(s['total_pct']):+.0f}%**", inline=True)
    await ctx.send(embed=e)


@bot.command(name="perticker")
async def perticker_cmd(ctx):
    """!perticker — per-ticker performance, best to worst."""
    rows = await asyncio.to_thread(db.per_ticker)
    if not rows:
        await ctx.send("No closed trades yet."); return
    lines = [f"{'✅' if float(r['total_pct'])>=0 else '❌'} **{r['ticker']}** — {r['n']} tr · "
             f"{r['wins']}/{r['n']}W · {float(r['total_pct']):+.0f}% (avg {float(r['avg_pct']):+.1f}%)" for r in rows]
    await ctx.send("\U0001f4ca **Per-ticker (closed):**\n" + "\n".join(lines))


@bot.command(name="status")
async def status_cmd(ctx):
    e = discord.Embed(title="\U0001f916 Options Signal Bot (tuned)", color=discord.Color.blue())
    e.add_field(name="Scan", value=f"{config.SCAN_INTERVAL_MINUTES}m", inline=True)
    e.add_field(name="Signals", value="trend_clean + ADX>30", inline=True)
    e.add_field(name="RS gate", value=(f"top {int(RS_QUANTILE*100)}%" if 0 < RS_QUANTILE < 1 else "off"), inline=True)
    e.add_field(name="Entry window", value="open → 12:00 ET", inline=True)
    e.add_field(name="Exits", value=f"+{int(sc.TAKE_PROFIT_PREMIUM_PCT)}% / -{int(sc.STOP_LOSS_PREMIUM_PCT)}% / opp / EOD", inline=True)
    e.add_field(name="Market", value="open" if session.is_market_open() else "closed", inline=True)
    e.add_field(name="Open positions", value=str(len(await asyncio.to_thread(db.open_positions))), inline=True)
    await ctx.send(embed=e)


@bot.command(name="watchlist")
async def watchlist_cmd(ctx):
    await ctx.send(f"\U0001f4cb **{len(config.WATCHLIST)} tickers:** {', '.join(config.WATCHLIST)}")


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set"); raise SystemExit(1)
    if config.CHANNEL_ID == 0:
        log.error("DISCORD_CHANNEL_ID not set"); raise SystemExit(1)
    if not db.configured():
        log.error("DATABASE_URL not set — the bot needs Postgres for trade state"); raise SystemExit(1)
    db.init()
    log.info("DB schema ready")
    bot.run(config.DISCORD_TOKEN)
