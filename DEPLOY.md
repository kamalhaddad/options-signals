# Deploying the live bot (DigitalOcean droplet + push-to-deploy)

The bot streams the tuned strategy's **entry and exit** signals to Discord during market
hours, using **real-time ThetaData**. Because ThetaData's Terminal is IP-locked to one
client and must run persistently, the cleanest setup is a **single droplet** running **one
container** with both the Terminal and the bot (the bot reaches the Terminal at
`127.0.0.1:25510`). Pushing to `main` auto-deploys via GitHub Actions.

```
GitHub (push to main) ──Action──SSH──▶ Droplet ──▶ docker compose up -d --build
                                                     └─ one container: ThetaTerminal + bot ──▶ Discord
```

## Prerequisites (one-time)

1. **Discord bot** — https://discord.com/developers/applications → New Application → Bot →
   copy the **token**; under *Privileged Gateway Intents* enable **Message Content**. Invite
   it to your server (OAuth2 URL with `bot` scope, Send Messages permission). Right-click your
   target channel → Copy Channel ID (enable Developer Mode first).
2. **ThetaData** — an account **with a real-time data subscription** (historical-only will not
   stream live signals). You have `ThetaTerminal.jar`.
3. A **DigitalOcean droplet** — Ubuntu 24.04, Basic $6/mo (1 GB RAM), region NYC/SFO.

## 1. Set up the droplet

```bash
ssh root@YOUR_DROPLET_IP
curl -fsSL https://get.docker.com | sh
git clone https://github.com/kamalhaddad/options-signals.git ~/options-signals
cd ~/options-signals
```

Upload `ThetaTerminal.jar` to the droplet (from your **local** machine):

```bash
scp /path/to/ThetaTerminal.jar root@YOUR_DROPLET_IP:/root/ThetaTerminal.jar
```

Create `.env` (copy from `.env.example`):

```bash
cp .env.example .env && nano .env
```

```
DISCORD_TOKEN=...
DISCORD_CHANNEL_ID=...
SCAN_INTERVAL_MINUTES=15
COOLDOWN_MINUTES=30
THETA_EMAIL=you@example.com
THETA_PASSWORD=...
THETA_JAR_HOST=/root/ThetaTerminal.jar
```

Bring it up:

```bash
docker compose up -d --build
docker compose logs -f      # watch: Terminal connects, then "Connected as <bot>"
```

The container runs the Terminal **and** the bot; `entrypoint.sh` waits for the Terminal to
connect before starting the bot, and exits (so Docker restarts) if either process dies. Open
positions persist in `./data/positions.json`, so restarts/redeploys don't lose them.

## 2. Push-to-deploy (GitHub Actions)

`.github/workflows/deploy.yml` SSHes into the droplet on every push to `main` and runs
`git reset --hard origin/main && docker compose up -d --build`. Add these **repo secrets**
(GitHub → Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `DROPLET_HOST` | droplet IP |
| `DROPLET_USER` | `root` (or your ssh user) |
| `DROPLET_SSH_KEY` | a **private** key whose public half is in the droplet's `~/.ssh/authorized_keys` |

After that: **edit code → `git push` → it's live in ~1–2 min.** (`.env` and
`ThetaTerminal.jar` stay only on the droplet — never committed.) You can also trigger a
redeploy manually from the Actions tab (`workflow_dispatch`).

> Note: a redeploy recreates the container, so the Terminal re-authenticates from the droplet's
> (unchanged) IP — fine for the IP-lock, but expect a ~30–60s data gap. Deploy outside market
> hours when you can.

## Discord commands

`!status` · `!positions` (open signals) · `!check NVDA` · `!watchlist`

## Useful droplet commands

```bash
docker compose ps                 # status
docker compose logs -f            # live logs
docker compose up -d --build      # manual redeploy
docker compose down               # stop
cat data/positions.json           # current open positions
```

## Monitoring & cost

- DigitalOcean → Droplet → Monitoring → alerts on CPU/memory > 90% (free).
- Cost: **$6/mo** droplet + your ThetaData subscription. (App Platform is **not** suitable here —
  it can't persistently mount the Terminal jar or hold the single-client IP-lock.)
