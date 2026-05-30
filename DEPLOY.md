# Deploying to DigitalOcean

## Option 1: Droplet (cheapest — $4-6/mo)

### 1. Create a Droplet

- Go to https://cloud.digitalocean.com/droplets/new
- Choose **Ubuntu 24.04**
- Plan: **Basic → Regular → $6/mo** (1 GB RAM, 1 vCPU) — this is plenty
- Choose a datacenter region (NYC or SFO for lowest latency to US markets)
- Authentication: SSH key (recommended) or password
- Click Create Droplet

### 2. SSH into your Droplet

```bash
ssh root@your_droplet_ip
```

### 3. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

### 4. Clone or upload your project

If you have a git repo:

```bash
git clone https://github.com/your-username/options-signals.git
cd options-signals
```

Or upload directly from your local machine:

```bash
# Run this from your LOCAL machine
scp -r /path/to/options-signals root@your_droplet_ip:/root/options-signals
```

### 5. Create your .env file

```bash
cd /root/options-signals
nano .env
```

Add:

```
DISCORD_TOKEN=your_bot_token_here
DISCORD_CHANNEL_ID=your_channel_id_here
SCAN_INTERVAL_MINUTES=15
```

Save with `Ctrl+X`, then `Y`, then `Enter`.

### 6. Build and run

```bash
docker compose up -d --build
```

### 7. Verify it's running

```bash
docker compose logs -f
```

You should see the bot connecting to Discord. Press `Ctrl+C` to exit logs (bot keeps running).

---

## Option 2: App Platform (no server management — $5/mo)

### 1. Push your code to GitHub

Make sure your repo does NOT contain a `.env` file.

### 2. Create an App

- Go to https://cloud.digitalocean.com/apps/new
- Select your GitHub repo
- Component type: **Worker** (not web — this bot doesn't serve HTTP)
- Plan: **Basic → $5/mo**

### 3. Set environment variables

In the App settings, add:

| Key | Value |
|-----|-------|
| `DISCORD_TOKEN` | your bot token |
| `DISCORD_CHANNEL_ID` | your channel ID |
| `SCAN_INTERVAL_MINUTES` | 15 |

Mark `DISCORD_TOKEN` as encrypted.

### 4. Deploy

Click Deploy. It will build from your Dockerfile automatically.

---

## Useful commands (Droplet)

```bash
# Check if running
docker compose ps

# View live logs
docker compose logs -f

# Restart after code changes
docker compose up -d --build

# Stop the bot
docker compose down

# Update code from git
git pull && docker compose up -d --build
```

## Setting up auto-updates (optional)

To pull and redeploy automatically when you push to GitHub, add a cron job:

```bash
crontab -e
```

Add this line (checks every 30 minutes):

```
*/30 * * * * cd /root/options-signals && git pull && docker compose up -d --build >> /var/log/bot-deploy.log 2>&1
```

## Monitoring

Set up DigitalOcean monitoring alerts (free) to get notified if your Droplet goes down:

- Go to Droplet → Monitoring → Create Alert
- Alert on CPU > 90% for 5 minutes
- Alert on memory > 90% for 5 minutes

## Costs

| Option | Monthly Cost | Notes |
|--------|-------------|-------|
| Droplet (Basic) | $6/mo | Full control, SSH access |
| App Platform (Worker) | $5/mo | Managed, auto-deploys from GitHub |
