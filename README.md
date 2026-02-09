# 🏀 WNBA / Chicago Sky → Slack Feed Bot

A simple Python bot that pulls from Twitter and Reddit and posts updates to your Slack channel. No paid APIs needed.

---

## How It Works

- **Twitter**: Uses free RSS bridges (Nitter/RSSHub) to follow accounts — no Twitter API key needed
- **Reddit**: Uses Reddit's free public JSON API to monitor subreddits and keyword searches
- **Slack**: Sends clean messages via a free Slack Incoming Webhook
- **Deduplication**: Tracks what's already been posted so you never get duplicates

---

## Setup (3 Steps)

### Step 1: Create a Slack Webhook

1. Go to https://api.slack.com/apps and click **Create New App** → **From Scratch**
2. Name it something like "Sky Feed Bot", pick your workspace
3. In the left sidebar, click **Incoming Webhooks** → toggle it **On**
4. Click **Add New Webhook to Workspace**
5. Pick the channel you want posts to go to (e.g., `#sky-news`)
6. Copy the webhook URL — it looks like: `https://hooks.slack.com/services/T00000/B00000/XXXX`

### Step 2: Customize Your Feeds

Open `feed_bot.py` and edit the configuration section near the top:

```python
# Twitter accounts to follow (just the handle, no @)
TWITTER_ACCOUNTS = [
    "chicagosky",
    "WNBA",
    # Add your own here:
    "SomeReporter",
    "AnotherAccount",
]

# Reddit subreddits to monitor
REDDIT_SUBREDDITS = ["wnba", "chicagosky"]

# Reddit keyword searches
REDDIT_KEYWORDS = ["chicago sky", "angel reese", "chennedy carter"]
```

### Step 3: Deploy (pick one)

---

#### Option A: Railway.app (Recommended — Easiest)

1. Create a free account at https://railway.app
2. Click **New Project** → **Deploy from GitHub Repo**
3. Push this folder to a GitHub repo first, or use **Deploy from Local** with the Railway CLI
4. Add an environment variable:
   - `SLACK_WEBHOOK_URL` = your webhook URL from Step 1
5. That's it — Railway will build the Dockerfile and run it 24/7

**Cost**: Free tier gives you 500 hours/month (enough to run ~20 days). The $5/month Hobby plan runs it 24/7.

---

#### Option B: Render.com

1. Create a free account at https://render.com
2. New → **Background Worker** → connect your GitHub repo
3. Set environment: Docker
4. Add env var: `SLACK_WEBHOOK_URL` = your webhook URL
5. Deploy

**Cost**: Free tier available, $7/month for always-on.

---

#### Option C: Run on Your Own Computer

```bash
# Set your webhook URL
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"

# Run it (loops forever, checking every 10 minutes)
python feed_bot.py

# Or run just once (good for testing with cron)
RUN_MODE=once python feed_bot.py
```

To run it on a schedule with cron (Mac/Linux):
```bash
crontab -e
# Add this line to check every 10 minutes:
*/10 * * * * SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..." RUN_MODE=once python /path/to/feed_bot.py
```

---

## What Posts Look Like in Slack

```
🐦 Twitter — @chicagosky
Chicago Sky announce 2025 training camp roster. Full details:
https://twitter.com/chicagosky/status/...

🤖 Reddit — r/wnba — u/hoopsfan42 (⬆ 127)
Angel Reese highlights from last night's game are insane
https://reddit.com/r/wnba/comments/...
```

---

## Troubleshooting

**Twitter posts not showing up?**
Nitter instances can go down. The bot tries multiple instances plus RSSHub as a fallback. If none work, Twitter may have blocked them temporarily. The Reddit feeds will still work fine.

**Want to test without Slack?**
Just run without setting `SLACK_WEBHOOK_URL` — it'll print posts to the console instead.

**Too many posts?**
Remove keyword searches or reduce subreddits. The `SEARCH_ALL_FOR_KEYWORDS` setting controls whether keyword searches look across all of Reddit or just your configured subreddits.

**Want to change the check frequency?**
Edit `CHECK_INTERVAL_MINUTES` in the script (default: 10 minutes).

---

## Important Notes on Twitter/RSS

Since Twitter locked down its API, free access depends on RSS bridge services (Nitter, RSSHub). These are community-run and can be unreliable. If they all go down permanently, alternatives include:

- **rss.app** — paid service ($7/mo) that creates reliable Twitter RSS feeds
- **Twstalker** or similar scraping services
- Paying for Twitter API Basic ($100/mo)

The Reddit side will always work reliably since Reddit's public JSON API is free and stable.
