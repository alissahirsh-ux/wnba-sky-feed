"""
WNBA / Chicago Sky Feed Bot v2
Pulls from Twitter (via xcancel/RSS Bridge) and Reddit (via RSS), posts to Slack.
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
from urllib.parse import quote

# ---------------------------------------------------------------------------
# CONFIGURATION — Edit these to customize your feed!
# ---------------------------------------------------------------------------

# Slack incoming webhook URL (set as environment variable for security)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Twitter accounts to follow (just the handle, no @)
TWITTER_ACCOUNTS = [
    "chicagosky",
    "WNBA",
     "DougFeinberg",      # example — replace with real accounts you want
    "Kenswift"
    "ItsMeghanLHall"
    "AnnieCostabile"
    "Alexaphilippou"
    "uconnWBB"
    "WomensHoops_USA"
    "espnW"
    "Stewie"
    "PhoenixMercury"
    "Rebecca Lobo"
    "maggie_vanoni"
    "Seattstorm"
    "NYLiberty"
    "ReneeMontgomery"
    "LexieHull"
    "CoachJfernandez"
    "ClevelandWNBA"
    "MAVoepel"
    "Reese10Angel"
    "Unrivaledwbb"
    "linzsports"
    "E_Williams_1"
    "ScottAgness"
    "Kareemcopeland"
    "washMystics"
    "Danaaakianaaa"
    "FireWNBA"
    "Seeratsohi"
    "Chloepeterson67"
    "Philawnba"
    "Detroitwnba"
    "Allisongaler"
    "Disruptthegame"
    "WNBAComms"
    "Taresch"
    "Kamillascsilva"
    "Sheknowssports"
    "Ariivory"
    "Scoutripley"
    "Noadalzell" 
    "Quitalovessports"
    "Robocoko"
    "Nemchocke"
    "NekiasNBA"
    "StephenPG3"
    "SydJColson"
    "Hoop4thought"
    "tonyREast"
    "HunterCruse14"
    "thathleticWBB"
    "Classicjpow"
    "Richardcohen1"
    "Herhoopstats"
    "Howardmegdal"
    # Add more accounts here:
    # "SomeReporter",
]

# Reddit subreddits to monitor
REDDIT_SUBREDDITS = ["wnba", "chicagosky"]

# Reddit keyword searches
REDDIT_KEYWORDS = ["chicago sky", "angel reese", "chennedy carter"]

# Google News search terms (reliable backup source)
GOOGLE_NEWS_QUERIES = ["Chicago Sky WNBA"]

# How often this runs (used for logging only — actual schedule set externally)
CHECK_INTERVAL_MINUTES = 10

# File to track what we've already posted (persists between runs)
SEEN_FILE = os.environ.get("SEEN_FILE", "seen_posts.json")

# User agent — Reddit and other sites require a descriptive one
USER_AGENT = "WNBA-Sky-FeedBot/2.0 (Slack feed aggregator; contact: github.com)"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("feed_bot")

# ---------------------------------------------------------------------------
# SEEN POSTS TRACKER
# ---------------------------------------------------------------------------

def load_seen() -> set:
    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            cleaned = {k: v for k, v in data.items() if v > cutoff}
            return set(cleaned.keys())
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen_ids: set):
    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    now = datetime.now(timezone.utc).isoformat()
    for sid in seen_ids:
        if sid not in data:
            data[sid] = now

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    data = {k: v for k, v in data.items() if v > cutoff}

    with open(SEEN_FILE, "w") as f:
        json.dump(data, f)


def make_id(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


def fetch_url(url: str, timeout: int = 20) -> bytes:
    """Fetch a URL with proper headers."""
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, application/atom+xml, text/xml, */*",
    })
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# TWITTER VIA WORKING NITTER INSTANCES / XCANCEL
# ---------------------------------------------------------------------------

# These are instances known to still work as of early 2026
# xcancel.com is the most reliable remaining Nitter fork
TWITTER_RSS_SOURCES = [
    "https://xcancel.com",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]


def parse_rss_items(xml_data: bytes) -> list[dict]:
    """Parse RSS/Atom items from XML data."""
    items = []
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return items

    # Handle RSS format
    for item in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        title = ""
        link = ""
        title_el = item.find("{http://www.w3.org/2005/Atom}title")
        if title_el is not None and title_el.text:
            title = title_el.text
        link_el = item.find("{http://www.w3.org/2005/Atom}link")
        if link_el is not None:
            link = link_el.get("href", "")
        updated = ""
        updated_el = item.find("{http://www.w3.org/2005/Atom}updated")
        if updated_el is not None and updated_el.text:
            updated = updated_el.text
        items.append({"title": title, "link": link, "date": updated})

    # Also try standard RSS <item> elements
    for item in root.findall(".//item"):
        title = item.findtext("title", "")
        link = item.findtext("link", "")
        pub_date = item.findtext("pubDate", "")
        items.append({"title": title, "link": link, "date": pub_date})

    return items


def fetch_twitter_rss(account: str) -> list[dict]:
    """Fetch tweets from a Twitter account via working Nitter instances."""
    posts = []

    for instance in TWITTER_RSS_SOURCES:
        url = f"{instance}/{account}/rss"
        try:
            xml_data = fetch_url(url)
            items = parse_rss_items(xml_data)

            for item in items:
                link = item["link"]
                # Rewrite links to point to twitter.com
                for inst in TWITTER_RSS_SOURCES:
                    link = link.replace(inst, "https://twitter.com")

                posts.append({
                    "source": "twitter",
                    "author": f"@{account}",
                    "text": (item["title"] or "(no text)")[:280],
                    "url": link,
                    "date": item["date"],
                    "id": make_id(link or item["title"]),
                })

            if posts:
                log.info(f"Twitter: Got {len(posts)} posts from @{account} via {instance}")
                return posts

        except (URLError, HTTPError) as e:
            log.warning(f"Twitter: Failed {instance}/{account}: {e}")
            continue

    if not posts:
        log.warning(f"Twitter: All sources failed for @{account}")

    return posts


def fetch_all_twitter() -> list[dict]:
    all_posts = []
    for account in TWITTER_ACCOUNTS:
        posts = fetch_twitter_rss(account)
        all_posts.extend(posts)
        time.sleep(2)  # Be polite between accounts
    return all_posts


# ---------------------------------------------------------------------------
# REDDIT VIA RSS FEEDS (more reliable than JSON API from cloud servers)
# ---------------------------------------------------------------------------

def fetch_reddit_rss(url: str, label: str) -> list[dict]:
    """Fetch posts from a Reddit RSS feed URL."""
    posts = []
    try:
        xml_data = fetch_url(url)
        items = parse_rss_items(xml_data)

        for item in items:
            # Extract subreddit from link if possible
            subreddit = "r/?"
            link = item["link"]
            if "/r/" in link:
                parts = link.split("/r/")[1].split("/")
                if parts:
                    subreddit = f"r/{parts[0]}"

            posts.append({
                "source": "reddit",
                "author": "",
                "text": item["title"] or "(no title)",
                "url": link,
                "subreddit": subreddit,
                "date": item["date"],
                "id": make_id(link or item["title"]),
            })

        log.info(f"Reddit: Got {len(posts)} posts from {label}")
    except (URLError, HTTPError) as e:
        log.warning(f"Reddit: Failed {label}: {e}")

    return posts


def fetch_all_reddit() -> list[dict]:
    all_posts = []

    # Fetch subreddit feeds
    for sub in REDDIT_SUBREDDITS:
        url = f"https://www.reddit.com/r/{sub}/new/.rss?sort=new&limit=25"
        all_posts.extend(fetch_reddit_rss(url, f"r/{sub}"))
        time.sleep(2)

    # Fetch keyword search feeds
    for kw in REDDIT_KEYWORDS:
        url = f"https://www.reddit.com/search/.rss?q={quote(kw)}&sort=new&t=week&limit=15"
        all_posts.extend(fetch_reddit_rss(url, f"search: {kw}"))
        time.sleep(2)

    # Deduplicate
    seen = set()
    unique = []
    for p in all_posts:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)

    return unique


# ---------------------------------------------------------------------------
# GOOGLE NEWS RSS (reliable backup for Chicago Sky news)
# ---------------------------------------------------------------------------

def fetch_google_news(query: str) -> list[dict]:
    """Fetch news from Google News RSS."""
    posts = []
    url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
    try:
        xml_data = fetch_url(url)
        root = ET.fromstring(xml_data)

        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pub_date = item.findtext("pubDate", "")
            source = item.findtext("source", "")

            posts.append({
                "source": "news",
                "author": source or "Google News",
                "text": title,
                "url": link,
                "date": pub_date,
                "id": make_id(link or title),
            })

        log.info(f"Google News: Got {len(posts)} results for '{query}'")
    except (URLError, HTTPError, ET.ParseError) as e:
        log.warning(f"Google News: Failed for '{query}': {e}")

    return posts


def fetch_all_news() -> list[dict]:
    all_posts = []
    for query in GOOGLE_NEWS_QUERIES:
        all_posts.extend(fetch_google_news(query))
        time.sleep(1)
    return all_posts


# ---------------------------------------------------------------------------
# SLACK POSTING
# ---------------------------------------------------------------------------

def format_slack_message(post: dict) -> dict:
    if post["source"] == "twitter":
        emoji = "🐦"
        source_label = f"Twitter — {post['author']}"
    elif post["source"] == "reddit":
        emoji = "🤖"
        source_label = f"Reddit — {post.get('subreddit', 'r/?')}"
    else:
        emoji = "📰"
        source_label = f"News — {post.get('author', '')}"

    text = f"{emoji} *{source_label}*\n{post['text']}\n{post['url']}"
    return {"text": text}


def post_to_slack(message: dict) -> bool:
    if not SLACK_WEBHOOK_URL:
        log.info(f"[DRY RUN] {message.get('text', '')}")
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
# MAIN
# ---------------------------------------------------------------------------

def run_once():
    log.info("=" * 50)
    log.info("Starting feed check...")

    seen = load_seen()
    new_posts = []

    # Fetch from all sources
    twitter_posts = fetch_all_twitter()
    reddit_posts = fetch_all_reddit()
    news_posts = fetch_all_news()

    all_posts = twitter_posts + reddit_posts + news_posts
    log.info(
        f"Total fetched: {len(all_posts)} "
        f"({len(twitter_posts)} Twitter, {len(reddit_posts)} Reddit, {len(news_posts)} News)"
    )

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
            time.sleep(0.5)

    save_seen(seen)
    log.info(f"Done! Posted {posted} new items to Slack.")


def run_loop():
    interval = CHECK_INTERVAL_MINUTES * 60
    log.info(f"Starting feed bot v2 — checking every {CHECK_INTERVAL_MINUTES} minutes")
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
