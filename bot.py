"""
Live options-signal Discord bot — posts the TUNED strategy's entry AND exit alerts.

Each scan during market hours: for every watchlist ticker it reads the latest 5-min signal
(via `live_engine`, which mirrors `theta_backtest`), and
  - opens a signal (posts a BUY CALL/PUT alert) when the tuned entry fires inside the entry
    window (trade the open, no new entries after 12:00 ET), then
  - tracks that position and posts a CLOSE alert when the strategy would exit
    (+40% TP / -50% stop / opposite signal / end-of-day).
Open positions persist to disk so a redeploy/restart doesn't lose them. A per-ticker cooldown
after each close prevents flip-flop spam.

Needs the ThetaData Terminal reachable (same host) + Discord creds in .env. See config.py.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, time as dtime

import discord
from discord.ext import commands, tasks

import config
import session
from live_engine import LiveEngine, today_et

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

POSITIONS_FILE = os.getenv("POSITIONS_FILE", "positions.json")
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MINUTES", "30"))   # min minutes between re-entries per ticker

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ── persistent state ────────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        with open(POSITIONS_FILE) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("positions", {})
    state.setdefault("cooldowns", {})
    state.setdefault("closed", [])               # closed-trade history (for the summaries)
    state.setdefault("summary_posted", "")       # date of the last posted DAILY summary
    state.setdefault("week_summary_posted", "")  # ISO week of the last posted WEEKLY summary
    return state


def record_closed(state: dict, pos: dict, exit_bid, reason: str, now) -> None:
    pnl = ((exit_bid - pos["entry"]) / pos["entry"] * 100) if (exit_bid and pos.get("entry")) else 0.0
    state["closed"].append({
        "date": now.strftime("%Y-%m-%d"), "ticker": pos["ticker"], "dir": pos["dir"],
        "strike_d": pos["strike_d"], "entry": pos["entry"], "exit": exit_bid or 0.0,
        "pnl_pct": pnl, "reason": reason,
    })


def save_state(state: dict) -> None:
    tmp = POSITIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, POSITIONS_FILE)


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
    e.add_field(name=f"\U0001f4b0 Contract",
                value=(f"**${c['strike_d']:.0f} {direction}** exp {c['exp_date']} ({c['dte']}d)\n"
                       f"Ask **${c['ask']:.2f}**  bid ${c['bid']:.2f}  (spread {c['spread']:.1f}%)\n"
                       f"{c['otm']:+.1f}% OTM" + (f"  •  IV {c['iv']*100:.0f}%" if c.get('iv') else "")),
                inline=False)
    e.add_field(name="\U0001f3af Manage",
                value=(f"Take profit **+{int(config_tp())}%** premium  •  Stop **-{int(config_sl())}%**  •  "
                       f"exit on opposite signal / end-of-day (bot will post the close)"),
                inline=False)
    e.set_footer(text="Educational, not financial advice • manage your risk")
    return e


def exit_embed(pos: dict, reason: str, exit_bid: float | None) -> discord.Embed:
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
    wins = [t for t in trades if t["pnl_pct"] > 0]
    total = sum(t["pnl_pct"] for t in trades)
    color = discord.Color.green() if total >= 0 else discord.Color.red()
    e = discord.Embed(title=f"\U0001f4ca Daily Summary — {date_str}", color=color, timestamp=datetime.now())
    if not trades:
        e.description = "No trades today."
        return e
    lines = []
    for t in trades:
        mark = "✅" if t["pnl_pct"] > 0 else "❌"
        lines.append(f"{mark} **{t['ticker']}** {t['dir']} ${t['strike_d']:.0f}  →  {t['pnl_pct']:+.0f}%")
    body = "\n".join(lines)
    if len(body) > 1000:                                   # Discord field cap; keep it safe
        keep = lines[:24]
        body = "\n".join(keep) + f"\n… +{len(lines) - len(keep)} more"
    e.add_field(name=f"Trades ({len(trades)})", value=body, inline=False)
    e.add_field(name="Win rate", value=f"{len(wins)}/{len(trades)} ({len(wins)/len(trades)*100:.0f}%)", inline=True)
    e.add_field(name="Total P&L", value=f"**{total:+.0f}%**", inline=True)
    e.set_footer(text="Sum of per-trade % • educational, not financial advice")
    return e


def eod_collect():
    """After market close: flatten any straggler positions, mark the day done, and return
    (date, today's trades) — or None if it's not after-close yet or already summarized.
    Runs in a worker thread (touches ThetaData/disk)."""
    now = session.now_et()
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or now.time() < dtime(16, 0) or session.is_market_open(now):
        return None
    state = load_state()
    if state.get("summary_posted") == today:
        return None
    if state["positions"]:                                 # safety net: close anything still open
        eng = LiveEngine()
        for tk, pos in list(state["positions"].items()):
            bid = eng.current_bid(tk, pos["exp"], pos["strike"], pos["right"])
            record_closed(state, pos, bid, "end of day", now)
            del state["positions"][tk]
    state["summary_posted"] = today
    cutoff = (now - timedelta(days=12)).strftime("%Y-%m-%d")  # prune old history (keep >1 week)
    state["closed"] = [c for c in state["closed"] if c.get("date", "") >= cutoff]
    save_state(state)
    return today, [c for c in state["closed"] if c.get("date") == today]


def weekly_summary_embed(week_label: str, trades: list) -> discord.Embed:
    """Friday recap of the whole week: per-day breakdown + totals (✅/❌ + total P&L%)."""
    wins = [t for t in trades if t["pnl_pct"] > 0]
    total = sum(t["pnl_pct"] for t in trades)
    color = discord.Color.green() if total >= 0 else discord.Color.red()
    e = discord.Embed(title=f"\U0001f4c5 Weekly Summary — {week_label}", color=color, timestamp=datetime.now())
    if not trades:
        e.description = "No trades this week."
        return e
    by_day: dict = {}
    for t in trades:
        by_day.setdefault(t["date"], []).append(t)
    day_lines = []
    for d in sorted(by_day):
        dts = by_day[d]
        dtot = sum(x["pnl_pct"] for x in dts)
        dw = sum(1 for x in dts if x["pnl_pct"] > 0)
        dow = datetime.strptime(d, "%Y-%m-%d").strftime("%a")
        mark = "✅" if dtot >= 0 else "❌"
        day_lines.append(f"{mark} {dow} {d[5:]} — {len(dts)} trades · {dw}/{len(dts)}W · **{dtot:+.0f}%**")
    e.add_field(name="By day", value="\n".join(day_lines), inline=False)
    e.add_field(name="Trades", value=str(len(trades)), inline=True)
    e.add_field(name="Win rate", value=f"{len(wins)}/{len(trades)} ({len(wins)/len(trades)*100:.0f}%)", inline=True)
    e.add_field(name="Total P&L", value=f"**{total:+.0f}%**", inline=True)
    best, worst = max(trades, key=lambda x: x["pnl_pct"]), min(trades, key=lambda x: x["pnl_pct"])
    e.add_field(name="Best / Worst",
                value=f"✅ {best['ticker']} {best['dir']} {best['pnl_pct']:+.0f}%   /   "
                      f"❌ {worst['ticker']} {worst['dir']} {worst['pnl_pct']:+.0f}%", inline=False)
    e.set_footer(text="Sum of per-trade % • educational, not financial advice")
    return e


def _week_bounds(now):
    """ISO-week label + (monday, friday) date strings for the week containing `now`."""
    iso = now.isocalendar()
    wd = now.weekday()                               # 0=Mon … 6=Sun
    monday = now - timedelta(days=wd)
    return f"{iso[0]}-W{iso[1]:02d}", monday.strftime("%Y-%m-%d"), (monday + timedelta(days=4)).strftime("%Y-%m-%d")


def weekly_collect():
    """After Friday's close (through the weekend): post the week's recap once. Returns
    (week_label, week_trades) or None. Runs in a worker thread."""
    now = session.now_et()
    wd = now.weekday()
    after_friday_close = (wd == 4 and now.time() >= dtime(16, 0)) or wd in (5, 6)
    if not after_friday_close:
        return None
    week_label, monday, friday = _week_bounds(now)
    state = load_state()
    if state.get("week_summary_posted") == week_label:
        return None
    state["week_summary_posted"] = week_label
    save_state(state)
    return week_label, [c for c in state["closed"] if monday <= c.get("date", "") <= friday]


def config_tp():
    import strategy_core as sc
    return sc.TAKE_PROFIT_PREMIUM_PCT


def config_sl():
    import strategy_core as sc
    return sc.STOP_LOSS_PREMIUM_PCT


# ── core scan (sync; runs in a worker thread) ─────────────────────────────────────
def scan() -> list[tuple[str, discord.Embed]]:
    """One pass over the watchlist. Returns [(kind, embed)] to post; mutates persisted state.
    A fresh engine per scan = fresh in-memory caches (today's quotes must not be cached stale)."""
    eng = LiveEngine()
    state = load_state()
    positions, cooldowns = state["positions"], state["cooldowns"]
    now = session.now_et()
    eod = session.is_eod(now)
    can_enter = session.in_entry_window(now)
    out: list[tuple[str, discord.Embed]] = []

    for tk in config.WATCHLIST:
        try:
            st = eng.latest(tk)
        except Exception as ex:
            log.warning(f"{tk}: latest() failed: {ex}")
            continue

        if tk in positions:
            pos = positions[tk]
            if st is None and not eod:
                continue
            reason = eng.exit_reason(pos, st, eod)
            if reason:
                bid = eng.current_bid(tk, pos["exp"], pos["strike"], pos["right"])
                out.append(("exit", exit_embed(pos, reason, bid)))
                record_closed(state, pos, bid, reason, now)
                del positions[tk]
                cooldowns[tk] = now.timestamp() + COOLDOWN_MIN * 60
            continue

        # no open position -> consider an entry
        if not can_enter or st is None:
            continue
        if cooldowns.get(tk, 0) > now.timestamp():
            continue
        direction = eng.entry_direction(st)
        if not direction:
            continue
        c = eng.pick_and_quote(tk, direction, st["spot"], st["time"])
        if not c:
            continue
        positions[tk] = {
            "ticker": tk, "dir": direction, "exp": c["exp"], "strike": c["strike"],
            "right": c["right"], "strike_d": c["strike_d"], "entry": c["ask"],
            "entry_time": st["time"].strftime("%Y-%m-%d %H:%M"), "exp_date": c["exp_date"],
            "opened": now.isoformat(),
        }
        out.append(("entry", entry_embed(st, direction, c)))

    save_state(state)
    return out


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
    """After the close: post the DAILY recap (any weekday) and, on Friday, the WEEKLY recap.
    Each fires once; zero-trade days/weeks are skipped."""
    channel = bot.get_channel(config.CHANNEL_ID)
    if channel is None:
        return
    # daily — also flattens any straggler positions before summarizing
    res = await asyncio.to_thread(eod_collect)
    if res is not None:
        date_str, trades = res
        if trades:
            await channel.send(embed=summary_embed(date_str, trades))
            log.info(f"posted daily summary {date_str}: {len(trades)} trades")
        else:
            log.info(f"{date_str}: no trades — no daily summary")
    # weekly — Friday after close (runs after the daily flatten, so Friday's trades are in)
    wres = await asyncio.to_thread(weekly_collect)
    if wres is not None:
        week_label, wtrades = wres
        if wtrades:
            await channel.send(embed=weekly_summary_embed(week_label, wtrades))
            log.info(f"posted weekly summary {week_label}: {len(wtrades)} trades")
        else:
            log.info(f"{week_label}: no trades — no weekly summary")


@eod_loop.before_loop
async def _before_eod():
    await bot.wait_until_ready()


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
    """!positions — currently open signal positions."""
    state = load_state()
    pos = state["positions"]
    if not pos:
        await ctx.send("\U0001f4ed No open positions."); return
    lines = [f"• **{p['ticker']}** {p['dir']} ${p['strike_d']:.0f} {p['exp_date']} @ ${p['entry']:.2f} "
             f"(since {p['entry_time']})" for p in pos.values()]
    await ctx.send("\U0001f4ca **Open positions:**\n" + "\n".join(lines))


@bot.command(name="summary")
async def summary_cmd(ctx):
    """!summary — today's closed trades so far (✅/❌ + total P&L%)."""
    today = session.now_et().strftime("%Y-%m-%d")
    trades = [c for c in load_state()["closed"] if c.get("date") == today]
    await ctx.send(embed=summary_embed(today, trades))


@bot.command(name="weeksummary")
async def weeksummary_cmd(ctx):
    """!weeksummary — this week's closed trades so far (per-day + total P&L%)."""
    label, monday, friday = _week_bounds(session.now_et())
    trades = [c for c in load_state()["closed"] if monday <= c.get("date", "") <= friday]
    await ctx.send(embed=weekly_summary_embed(label, trades))


@bot.command(name="status")
async def status_cmd(ctx):
    import strategy_core as sc
    e = discord.Embed(title="\U0001f916 Options Signal Bot (tuned)", color=discord.Color.blue())
    e.add_field(name="Scan", value=f"{config.SCAN_INTERVAL_MINUTES}m", inline=True)
    e.add_field(name="Signals", value="trend_clean + ADX>30", inline=True)
    e.add_field(name="Entry window", value="open → 12:00 ET", inline=True)
    e.add_field(name="Exits", value=f"+{int(sc.TAKE_PROFIT_PREMIUM_PCT)}% / -{int(sc.STOP_LOSS_PREMIUM_PCT)}% / opp / EOD", inline=True)
    e.add_field(name="Watchlist", value=f"{len(config.WATCHLIST)}", inline=True)
    e.add_field(name="Market", value="open" if session.is_market_open() else "closed", inline=True)
    e.add_field(name="Open positions", value=str(len(load_state()["positions"])), inline=True)
    await ctx.send(embed=e)


@bot.command(name="watchlist")
async def watchlist_cmd(ctx):
    await ctx.send(f"\U0001f4cb **{len(config.WATCHLIST)} tickers:** {', '.join(config.WATCHLIST)}")


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set"); raise SystemExit(1)
    if config.CHANNEL_ID == 0:
        log.error("DISCORD_CHANNEL_ID not set"); raise SystemExit(1)
    bot.run(config.DISCORD_TOKEN)
