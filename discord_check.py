"""
One-off Discord connectivity check (run before deploying).

Logs in with DISCORD_TOKEN, confirms the bot can see DISCORD_CHANNEL_ID, posts a
"connected" message, and exits. Verifies token + intents + channel + post permission
without running the full bot. Reads creds from .env via config (never printed).

  .venv/bin/python discord_check.py
"""
import asyncio
import sys

import discord

import config


async def run() -> int:
    if not config.DISCORD_TOKEN or "PASTE" in config.DISCORD_TOKEN:
        print("❌ DISCORD_TOKEN is not set in .env"); return 1
    if not config.CHANNEL_ID:
        print("❌ DISCORD_CHANNEL_ID is not set in .env"); return 1

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    result = {"code": 1}

    @client.event
    async def on_ready():
        try:
            ch = client.get_channel(config.CHANNEL_ID)
            if ch is None:
                ch = await client.fetch_channel(config.CHANNEL_ID)   # API lookup if not cached
            await ch.send("✅ Options Signals bot connected — connectivity check OK.")
            print(f"✅ logged in as {client.user}  |  posted to #{getattr(ch, 'name', ch.id)} "
                  f"in '{getattr(getattr(ch, 'guild', None), 'name', '?')}'")
            result["code"] = 0
        except discord.NotFound:
            print(f"❌ channel {config.CHANNEL_ID} not found — wrong ID, or the bot isn't in that server "
                  f"(did you run the OAuth invite URL?)")
            result["code"] = 2
        except discord.Forbidden:
            print("❌ bot lacks permission in that channel — check View Channel / Send Messages / Embed Links")
            result["code"] = 3
        except Exception as e:
            print(f"❌ unexpected error: {type(e).__name__}: {e}")
            result["code"] = 4
        finally:
            await client.close()

    try:
        await client.start(config.DISCORD_TOKEN)
    except discord.LoginFailure:
        print("❌ invalid DISCORD_TOKEN (LoginFailure) — re-copy it from the Bot page (Reset Token)")
        return 1
    except discord.PrivilegedIntentsRequired:
        print("❌ enable the Message Content intent on the Bot page, then retry")
        return 1
    return result["code"]


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
