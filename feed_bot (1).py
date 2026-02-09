"""
WNBA / Chicago Sky Feed Bot
Pulls from Twitter (via RSS bridges) and Reddit, posts to Slack.
Runs on a schedule (every 10 minutes by default).
"""

import os
import json
import time
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import quote, urlencode

# ---------------------------------------------------------------------------
# CONFIGURATION — Edit these to customize your feed!
# ---------------------------------------------------------------------------

# Slack incoming webhook URL (set as environment variable for security)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Twitter accounts to follow (just the handle, no @)
TWITTER_ACCOUNTS = [
    "chicagosky",
    "WNBA",
    "WNBAChiSky",      # example — replace with real accounts you want
    # Add more accounts here:
    # "SomeReporter",
    # "SkyFanAccount",
]

# Reddit configuration
REDDIT_SUBREDDITS = ["wnba", "chicagosky"]
REDDIT_KEYWORDS = ["chicago sky", "sky wnba", "angel reese", "chennedy carter"]
# Set to True to also search r/all for keywords (broader but noisier)
SEARCH_ALL_FOR_KEYWORDS = False

# How far back to look on first run (in hours)
LOOKBACK_HOURS = 2

# How often this runs (used for logging only — actual schedule set externally)
CHECK_INTERVAL_MINUTES = 10

# File to track what we've already posted (persists between runs)
SEEN_FILE = os.environ.get("SEEN_FILE", "seen_posts.json")

# RSS bridge for Twitter — tries multiple Nitter instances
# You can also use https://rss.app or https://rsshub.app as alternatives
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.woodland.cafe",
]

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("feed_bot")

# ---------------------------------------------------------------------------
# SEEN POSTS TRACKER
# ---------------------------------------------------------------------------

def load_seen() -> set:
    """Load previously seen post IDs."""
    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
            # Clean old entries (older than 7 days) to keep file small
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            cleaned = {k: v for k, v in data.items() if v > cutoff}
            return set(cleaned.keys())
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen_ids: set):
    """Save seen post IDs with timestamps."""
    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    now = datetime.now(timezone.utc).isoformat()
    for sid in seen_ids:
        if sid not in data:
            data[sid] = now

    # Prune entries older than 7 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    data = {k: v for k, v in data.items() if v > cutoff}

    with open(SEEN_FILE, "w") as f:
        json.dump(data, f)


def make_id(text: str) -> str:
    """Create a short hash ID from text."""
    return hashlib.md5(text.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# TWITTER VIA NITTER RSS
# ---------------------------------------------------------------------------

def fetch_twitter_rss(account: str) -> list[dict]:
    """Fetch tweets from a Twitter account via Nitter RSS."""
    posts = []

    for instance in NITTER_INSTANCES:
        url = f"{instance}/{account}/rss"
        try:
            req = Request(url, headers={"User-Agent": "WNBA-Feed-Bot/1.0"})
            with urlopen(req, timeout=15) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)

            for item in root.findall(".//item"):
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                pub_date = item.findtext("pubDate", "")

                # Convert Nitter link back to Twitter link
                if instance in link:
                    link = link.replace(instance, "https://twitter.com")

                posts.append({
                    "source": "twitter",
                    "author": f"@{account}",
                    "text": title[:280] if title else "(no text)",
                    "url": link,
                    "date": pub_date,
                    "id": make_id(link or title),
                })

            log.info(f"Twitter: Got {len(posts)} posts from @{account} via {instance}")
            return posts  # Success — stop trying other instances

        except (URLError, HTTPError, ET.ParseError) as e:
            log.warning(f"Twitter: Failed {instance}/{account}: {e}")
            continue

    # If all Nitter instances fail, try RSSHub as fallback
    try:
        url = f"https://rsshub.app/twitter/user/{account}"
        req = Request(url, headers={"User-Agent": "WNBA-Feed-Bot/1.0"})
        with urlopen(req, timeout=15) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)

        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pub_date = item.findtext("pubDate", "")
            posts.append({
                "source": "twitter",
                "author": f"@{account}",
                "text": title[:280] if title else "(no text)",
                "url": link,
                "date": pub_date,
                "id": make_id(link or title),
            })

        log.info(f"Twitter: Got {len(posts)} posts from @{account} via RSSHub")
    except (URLError, HTTPError, ET.ParseError) as e:
        log.warning(f"Twitter: RSSHub also failed for @{account}: {e}")

    return posts


def fetch_all_twitter() -> list[dict]:
    """Fetch tweets from all configured accounts."""
    all_posts = []
    for account in TWITTER_ACCOUNTS:
        posts = fetch_twitter_rss(account)
        all_posts.extend(posts)
        time.sleep(1)  # Be polite
    return all_posts


# ---------------------------------------------------------------------------
# REDDIT VIA JSON API
# ---------------------------------------------------------------------------

def fetch_reddit_subreddit(subreddit: str) -> list[dict]:
    """Fetch new posts from a subreddit."""
    posts = []
    url = f"https://www.reddit.com/r/{subreddit}/new.json?limit=25"
    try:
        req = Request(url, headers={"User-Agent": "WNBA-Feed-Bot/1.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            posts.append({
                "source": "reddit",
                "author": f"u/{post.get('author', 'unknown')}",
                "text": post.get("title", "(no title)"),
                "url": f"https://reddit.com{post.get('permalink', '')}",
                "subreddit": f"r/{subreddit}",
                "score": post.get("score", 0),
                "date": datetime.fromtimestamp(
                    post.get("created_utc", 0), tz=timezone.utc
                ).isoformat(),
                "id": make_id(post.get("id", post.get("title", ""))),
            })

        log.info(f"Reddit: Got {len(posts)} posts from r/{subreddit}")
    except (URLError, HTTPError, json.JSONDecodeError) as e:
        log.warning(f"Reddit: Failed r/{subreddit}: {e}")

    return posts


def fetch_reddit_search(keyword: str) -> list[dict]:
    """Search Reddit for a keyword."""
    posts = []
    search_sub = "all" if SEARCH_ALL_FOR_KEYWORDS else "+".join(REDDIT_SUBREDDITS)
    url = (
        f"https://www.reddit.com/r/{search_sub}/search.json?"
        f"q={quote(keyword)}&sort=new&t=day&limit=15"
    )
    try:
        req = Request(url, headers={"User-Agent": "WNBA-Feed-Bot/1.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            posts.append({
                "source": "reddit",
                "author": f"u/{post.get('author', 'unknown')}",
                "text": post.get("title", "(no title)"),
                "url": f"https://reddit.com{post.get('permalink', '')}",
                "subreddit": f"r/{post.get('subreddit', '?')}",
                "score": post.get("score", 0),
                "date": datetime.fromtimestamp(
                    post.get("created_utc", 0), tz=timezone.utc
                ).isoformat(),
                "id": make_id(post.get("id", post.get("title", ""))),
            })

        log.info(f"Reddit: Got {len(posts)} results for '{keyword}'")
    except (URLError, HTTPError, json.JSONDecodeError) as e:
        log.warning(f"Reddit: Search failed for '{keyword}': {e}")

    return posts


def fetch_all_reddit() -> list[dict]:
    """Fetch from all configured subreddits and keyword searches."""
    all_posts = []

    for sub in REDDIT_SUBREDDITS:
        all_posts.extend(fetch_reddit_subreddit(sub))
        time.sleep(1)

    for kw in REDDIT_KEYWORDS:
        all_posts.extend(fetch_reddit_search(kw))
        time.sleep(1)

    # Deduplicate by ID
    seen = set()
    unique = []
    for p in all_posts:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)

    return unique


# ---------------------------------------------------------------------------
# SLACK POSTING
# ---------------------------------------------------------------------------

def format_slack_message(post: dict) -> dict:
    """Format a post as a simple Slack message."""
    if post["source"] == "twitter":
        emoji = "🐦"
        source_label = f"Twitter — {post['author']}"
    else:
        emoji = "🤖"
        source_label = f"Reddit — {post.get('subreddit', 'r/?')} — {post['author']}"
        if post.get("score", 0) > 0:
            source_label += f" (⬆ {post['score']})"

    text = f"{emoji} *{source_label}*\n{post['text']}\n{post['url']}"

    return {"text": text}


def post_to_slack(message: dict) -> bool:
    """Send a message to Slack via webhook."""
    if not SLACK_WEBHOOK_URL:
        log.warning("No SLACK_WEBHOOK_URL set — printing to console instead:")
        log.info(message.get("text", ""))
        return True

    try:
        data = json.dumps(message).encode("utf-8")
        req = Request(
            SLACK_WEBHOOK_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except (URLError, HTTPError) as e:
        log.error(f"Slack post failed: {e}")
        return False


# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------

def run_once():
    """Run one cycle: fetch, filter, post."""
    log.info("=" * 50)
    log.info("Starting feed check...")

    seen = load_seen()
    new_posts = []

    # Fetch from all sources
    twitter_posts = fetch_all_twitter()
    reddit_posts = fetch_all_reddit()

    all_posts = twitter_posts + reddit_posts
    log.info(f"Total fetched: {len(all_posts)} ({len(twitter_posts)} Twitter, {len(reddit_posts)} Reddit)")

    # Filter to only new posts
    for post in all_posts:
        if post["id"] not in seen:
            new_posts.append(post)
            seen.add(post["id"])

    log.info(f"New posts to share: {len(new_posts)}")

    # Post to Slack
    posted = 0
    for post in new_posts:
        msg = format_slack_message(post)
        if post_to_slack(msg):
            posted += 1
            time.sleep(0.5)  # Avoid Slack rate limits

    # Save updated seen list
    save_seen(seen)
    log.info(f"Done! Posted {posted} new items to Slack.")


def run_loop():
    """Run continuously on a schedule."""
    interval = CHECK_INTERVAL_MINUTES * 60
    log.info(f"Starting feed bot — checking every {CHECK_INTERVAL_MINUTES} minutes")
    while True:
        try:
            run_once()
        except Exception as e:
            log.error(f"Unexpected error: {e}", exc_info=True)
        log.info(f"Sleeping {CHECK_INTERVAL_MINUTES} minutes...\n")
        time.sleep(interval)


if __name__ == "__main__":
    mode = os.environ.get("RUN_MODE", "loop")
    if mode == "once":
        run_once()
    else:
        run_loop()
