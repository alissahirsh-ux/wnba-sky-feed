#!/usr/bin/env python3
"""
Wayback Machine WNBA Team Jobs Scraper

Uses the Wayback Machine CDX API to discover historical snapshots of
TeamWork Online's WNBA team jobs page, then fetches each snapshot
to extract job postings organized by team.

Usage:
    python3 wayback_wnba_scraper.py [--from YYYYMMDD] [--to YYYYMMDD] [--output-dir DIR]
                                     [--sample-html] [--max-snapshots N] [--delay SECONDS]

Examples:
    # Scrape last 3 years (default)
    python3 wayback_wnba_scraper.py

    # Scrape a specific date range
    python3 wayback_wnba_scraper.py --from 20230601 --to 20240601

    # Dump sample HTML to inspect page structure (useful for debugging)
    python3 wayback_wnba_scraper.py --sample-html

    # Limit number of snapshots processed
    python3 wayback_wnba_scraper.py --max-snapshots 50
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_URL = "www.teamworkonline.com/basketball-jobs/wnbateamjobs/wnba-team-jobs"
CDX_API_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org/web"
DEFAULT_YEARS_BACK = 3
DEFAULT_REQUEST_DELAY = 2  # seconds between requests (be respectful)
DEFAULT_OUTPUT_DIR = "wnba_jobs_data"
USER_AGENT = "WNBA-Jobs-Research-Bot/1.0 (Historical job posting research)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def fetch_url(url, retries=3, timeout=30):
    """Fetch a URL with retries and exponential backoff."""
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = 2 ** (attempt + 2)
                logger.warning("Rate-limited (429). Waiting %ds …", wait)
                time.sleep(wait)
                continue
            if exc.code in (404, 503) and attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("HTTP %d for %s – retrying in %ds", exc.code, url, wait)
                time.sleep(wait)
                continue
            logger.error("HTTP %d fetching %s", exc.code, url)
            return None
        except (urllib.error.URLError, OSError) as exc:
            wait = 2 ** (attempt + 1)
            logger.warning("Network error (%s) – retrying in %ds", exc, wait)
            time.sleep(wait)
    logger.error("Failed to fetch %s after %d attempts", url, retries)
    return None


# ---------------------------------------------------------------------------
# CDX API – discover Wayback Machine snapshots
# ---------------------------------------------------------------------------


def discover_snapshots(target_url, from_date, to_date):
    """
    Query the Wayback Machine CDX API to find all snapshots of the target URL.
    Returns a list of dicts with keys: timestamp, original, statuscode, mimetype.
    """
    params = {
        "url": target_url,
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "filter": ["statuscode:200", "mimetype:text/html"],
        "from": from_date,
        "to": to_date,
        "collapse": "digest",  # deduplicate identical page content
    }

    # Build query string (filter appears multiple times)
    qs_parts = []
    for key, val in params.items():
        if isinstance(val, list):
            for v in val:
                qs_parts.append(f"{key}={urllib.parse.quote(str(v))}")
        else:
            qs_parts.append(f"{key}={urllib.parse.quote(str(val))}")

    url = f"{CDX_API_URL}?{'&'.join(qs_parts)}"
    logger.info("Querying CDX API: %s", url)

    body = fetch_url(url, timeout=60)
    if not body:
        logger.error("CDX API returned no data")
        return []

    rows = json.loads(body)
    if len(rows) < 2:
        logger.warning("CDX API returned no snapshots")
        return []

    header = rows[0]
    snapshots = [dict(zip(header, row)) for row in rows[1:]]
    logger.info("Discovered %d unique snapshots", len(snapshots))
    return snapshots


def dedupe_snapshots_by_month(snapshots):
    """Keep at most one snapshot per month to avoid redundant fetches."""
    by_month = {}
    for snap in snapshots:
        month_key = snap["timestamp"][:6]  # YYYYMM
        if month_key not in by_month:
            by_month[month_key] = snap
    deduped = sorted(by_month.values(), key=lambda s: s["timestamp"])
    logger.info("After monthly dedup: %d snapshots", len(deduped))
    return deduped


# ---------------------------------------------------------------------------
# HTML parsing – extract job listings from TeamWork Online pages
# ---------------------------------------------------------------------------


class TeamWorkOnlineParser(HTMLParser):
    """
    Parse TeamWork Online job listing pages.

    TeamWork Online uses several common patterns across their templates:
      - Job cards in <div> or <a> elements with classes containing
        "organization-portal__job-title" or "OpportunitySearchResult"
      - Team names in elements with "organization-portal__profile-link"
        or nested inside organization containers
      - Links to individual job postings under /basketball-jobs/

    The parser is intentionally flexible to handle variations across
    different Wayback Machine snapshots (the site has been redesigned
    multiple times over 3 years).
    """

    def __init__(self):
        super().__init__()
        self.jobs = []
        self._current_job = {}
        self._capture_text = False
        self._capture_target = None
        self._depth = 0
        self._in_job_card = False
        self._job_card_depth = 0
        self._all_links = []
        self._text_buffer = []
        self._tag_stack = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")
        href = attrs_dict.get("href", "")
        self._depth += 1
        self._tag_stack.append(tag)

        # Detect job card containers
        if self._is_job_card_start(tag, cls):
            self._in_job_card = True
            self._job_card_depth = self._depth
            self._current_job = {}

        # Inside a job card, look for specific elements
        if self._in_job_card:
            # Job title link
            if tag == "a" and href and self._is_job_link(href):
                self._current_job["url"] = href
                self._capture_text = True
                self._capture_target = "title"
                self._text_buffer = []

            # Team / organization name
            if self._is_team_element(tag, cls):
                self._capture_text = True
                self._capture_target = "team"
                self._text_buffer = []

            # Location
            if self._is_location_element(tag, cls):
                self._capture_text = True
                self._capture_target = "location"
                self._text_buffer = []

        # Also collect ALL job-like links as fallback
        if tag == "a" and href and self._is_job_link(href):
            self._all_links.append({"href": href, "text": ""})
            if not self._in_job_card:
                self._capture_text = True
                self._capture_target = "_link_text"
                self._text_buffer = []

    def handle_endtag(self, tag):
        if self._capture_text and self._tag_stack and self._tag_stack[-1] == tag:
            text = " ".join("".join(self._text_buffer).split()).strip()
            if self._capture_target == "title" and text:
                self._current_job["title"] = text
            elif self._capture_target == "team" and text:
                self._current_job["team"] = text
            elif self._capture_target == "location" and text:
                self._current_job["location"] = text
            elif self._capture_target == "_link_text" and text:
                if self._all_links:
                    self._all_links[-1]["text"] = text
            self._capture_text = False
            self._capture_target = None
            self._text_buffer = []

        if self._in_job_card and self._depth <= self._job_card_depth:
            if self._current_job.get("title") or self._current_job.get("url"):
                self.jobs.append(self._current_job)
            self._in_job_card = False
            self._current_job = {}

        if self._tag_stack:
            self._tag_stack.pop()
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if self._capture_text:
            self._text_buffer.append(data)

    # -- Detection heuristics --

    @staticmethod
    def _is_job_card_start(tag, cls):
        cls_lower = cls.lower()
        patterns = [
            "organization-portal__job",
            "opportunitysearchresult",
            "search-result",
            "job-listing",
            "job-card",
            "job-item",
            "opportunity-listing",
        ]
        if tag in ("div", "li", "article", "section"):
            return any(p in cls_lower for p in patterns)
        return False

    @staticmethod
    def _is_job_link(href):
        href_lower = href.lower()
        # TeamWork Online job links typically contain these patterns
        if "/basketball-jobs/" in href_lower:
            return True
        if "teamworkonline.com" in href_lower and "/jobs/" in href_lower:
            return True
        if "/opening/" in href_lower:
            return True
        return False

    @staticmethod
    def _is_team_element(tag, cls):
        cls_lower = cls.lower()
        patterns = [
            "organization-portal__profile",
            "organization-name",
            "team-name",
            "employer",
            "company",
            "org-name",
        ]
        return any(p in cls_lower for p in patterns)

    @staticmethod
    def _is_location_element(tag, cls):
        cls_lower = cls.lower()
        patterns = [
            "location",
            "city",
            "job-location",
        ]
        return any(p in cls_lower for p in patterns)


def extract_jobs_from_html(html, snapshot_url=""):
    """
    Extract job listings from a TeamWork Online HTML page.

    Uses the structured parser first, then falls back to link-based
    extraction if no structured results are found.
    """
    parser = TeamWorkOnlineParser()
    try:
        parser.feed(html)
    except Exception as exc:
        logger.warning("HTML parse error for %s: %s", snapshot_url, exc)

    jobs = parser.jobs

    # Fallback: if structured parsing found nothing, extract from links
    if not jobs and parser._all_links:
        for link in parser._all_links:
            href = link["href"]
            text = link["text"]
            if text and not any(skip in text.lower() for skip in [
                "sign in", "log in", "register", "home", "about",
                "contact", "privacy", "terms",
            ]):
                job = {"title": text, "url": href}
                # Try to extract team name from URL path segments
                team = _team_from_url(href)
                if team:
                    job["team"] = team
                jobs.append(job)

    # Second fallback: regex-based extraction for heavily dynamic pages
    if not jobs:
        jobs = _regex_fallback(html)

    return jobs


def _team_from_url(url):
    """Try to extract a team/organization name from a TeamWork Online URL."""
    # URLs like /basketball-jobs/chicago-sky/some-job-title
    m = re.search(
        r"/basketball-jobs/([^/]+?)(?:jobs|team)?/",
        url,
        re.IGNORECASE,
    )
    if m:
        slug = m.group(1).strip("-")
        if slug.lower() not in ("wnbateamjobs", "wnba-team"):
            return _slug_to_name(slug)
    return None


def _slug_to_name(slug):
    """Convert a URL slug like 'chicago-sky' to 'Chicago Sky'."""
    return " ".join(word.capitalize() for word in slug.split("-"))


def _regex_fallback(html):
    """Last-resort regex extraction for job titles and links."""
    jobs = []
    # Look for links to job postings
    pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']*?/basketball-jobs/[^"\']+)["\'][^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html):
        href = match.group(1)
        text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        if text and len(text) > 3:
            job = {"title": text, "url": href}
            team = _team_from_url(href)
            if team:
                job["team"] = team
            jobs.append(job)
    return jobs


# ---------------------------------------------------------------------------
# Wayback URL helpers
# ---------------------------------------------------------------------------


def make_wayback_url(timestamp, original_url):
    """Build a Wayback Machine URL for a specific snapshot."""
    return f"{WAYBACK_BASE}/{timestamp}id_/{original_url}"


def clean_wayback_url(url):
    """Strip Wayback Machine wrapper from a URL to get the original."""
    m = re.search(r"/web/\d+(?:id_)?/(https?://.+)", url)
    if m:
        return m.group(1)
    return url


# ---------------------------------------------------------------------------
# Main scraping pipeline
# ---------------------------------------------------------------------------


def scrape_snapshot(snapshot, delay):
    """Fetch and parse a single Wayback Machine snapshot."""
    ts = snapshot["timestamp"]
    original = snapshot["original"]
    wayback_url = make_wayback_url(ts, original)

    snap_date = datetime.strptime(ts[:8], "%Y%m%d").strftime("%Y-%m-%d")
    logger.info("Fetching snapshot from %s …", snap_date)

    html = fetch_url(wayback_url)
    if not html:
        return []

    jobs = extract_jobs_from_html(html, wayback_url)

    # Annotate each job with the snapshot date
    for job in jobs:
        job["snapshot_date"] = snap_date
        job["wayback_url"] = wayback_url
        # Normalize the job URL (strip Wayback wrapper if present)
        if "url" in job:
            job["original_url"] = clean_wayback_url(job["url"])

    logger.info("  → Found %d job listings", len(jobs))
    time.sleep(delay)
    return jobs


def scrape_all(snapshots, delay, max_snapshots=None):
    """Scrape job listings from all snapshots."""
    if max_snapshots and len(snapshots) > max_snapshots:
        logger.info("Limiting to %d snapshots (of %d)", max_snapshots, len(snapshots))
        # Evenly sample from the full range
        step = max(1, len(snapshots) // max_snapshots)
        snapshots = snapshots[::step][:max_snapshots]

    all_jobs = []
    for i, snapshot in enumerate(snapshots, 1):
        logger.info("Processing snapshot %d/%d", i, len(snapshots))
        jobs = scrape_snapshot(snapshot, delay)
        all_jobs.extend(jobs)

    return all_jobs


# ---------------------------------------------------------------------------
# Post-processing and deduplication
# ---------------------------------------------------------------------------


# Known WNBA teams (2023–2026) for categorization
WNBA_TEAMS = [
    "Atlanta Dream",
    "Chicago Sky",
    "Connecticut Sun",
    "Dallas Wings",
    "Golden State Valkyries",
    "Indiana Fever",
    "Las Vegas Aces",
    "Los Angeles Sparks",
    "Minnesota Lynx",
    "New York Liberty",
    "Phoenix Mercury",
    "Portland",
    "Seattle Storm",
    "Washington Mystics",
    "WNBA League Office",
    "WNBA",
]


def classify_team(job):
    """
    Classify a job posting's team. Uses the explicit 'team' field if
    available, otherwise infers from the URL or title.
    """
    if job.get("team"):
        return job["team"]

    text = f"{job.get('title', '')} {job.get('original_url', '')} {job.get('url', '')}"
    text_lower = text.lower()

    for team in WNBA_TEAMS:
        if team.lower() in text_lower:
            return team
        # Also check slug form: "chicago-sky" in URL
        slug = team.lower().replace(" ", "-")
        if slug in text_lower:
            return team

    return "Unknown / WNBA General"


def deduplicate_jobs(jobs):
    """Remove duplicate job postings (same title + team, keep earliest)."""
    seen = {}
    for job in jobs:
        key = (job.get("title", "").lower().strip(), classify_team(job).lower())
        if key not in seen:
            seen[key] = job
        else:
            # Keep the one with the earlier snapshot date
            existing_date = seen[key].get("snapshot_date", "9999")
            new_date = job.get("snapshot_date", "9999")
            if new_date < existing_date:
                seen[key] = job
    return list(seen.values())


def organize_by_team(jobs):
    """Group jobs by team, sorted alphabetically."""
    by_team = defaultdict(list)
    for job in jobs:
        team = classify_team(job)
        job["team"] = team
        by_team[team].append(job)
    return dict(sorted(by_team.items()))


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def save_json(data, filepath):
    """Save data as formatted JSON."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Saved JSON: %s", filepath)


def save_csv(jobs, filepath):
    """Save flat job list as CSV."""
    fields = ["team", "title", "location", "snapshot_date", "original_url", "wayback_url"]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for job in sorted(jobs, key=lambda j: (j.get("team", ""), j.get("snapshot_date", ""))):
            writer.writerow(job)
    logger.info("Saved CSV:  %s", filepath)


def save_summary(by_team, filepath):
    """Save a human-readable markdown summary."""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# WNBA Team Job Postings (Wayback Machine Historical Data)\n\n")
        f.write(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write(f"Total teams: {len(by_team)}\n")
        total_jobs = sum(len(v) for v in by_team.values())
        f.write(f"Total unique job postings: {total_jobs}\n\n")
        f.write("---\n\n")

        for team, jobs in by_team.items():
            f.write(f"## {team} ({len(jobs)} postings)\n\n")
            for job in sorted(jobs, key=lambda j: j.get("snapshot_date", "")):
                title = job.get("title", "Unknown Title")
                date = job.get("snapshot_date", "N/A")
                location = job.get("location", "")
                loc_str = f" | {location}" if location else ""
                f.write(f"- **{title}** ({date}{loc_str})\n")
            f.write("\n")

    logger.info("Saved summary: %s", filepath)


def print_summary(by_team):
    """Print a brief summary to stdout."""
    total_jobs = sum(len(v) for v in by_team.values())
    print(f"\n{'=' * 60}")
    print(f"  WNBA Team Job Postings – Historical Summary")
    print(f"{'=' * 60}")
    print(f"  Teams found:          {len(by_team)}")
    print(f"  Total unique postings: {total_jobs}")
    print(f"{'=' * 60}\n")

    for team, jobs in by_team.items():
        print(f"  {team}: {len(jobs)} posting(s)")
        # Show a few example titles
        for job in jobs[:3]:
            title = job.get("title", "?")
            date = job.get("snapshot_date", "")
            print(f"    - {title} ({date})")
        if len(jobs) > 3:
            print(f"    … and {len(jobs) - 3} more")
    print()


# ---------------------------------------------------------------------------
# HTML structure inspector (--sample-html mode)
# ---------------------------------------------------------------------------


def dump_sample_html(from_date, to_date, output_dir):
    """Fetch one snapshot and dump its HTML for manual inspection."""
    snapshots = discover_snapshots(TARGET_URL, from_date, to_date)
    if not snapshots:
        logger.error("No snapshots found to sample")
        return

    # Pick one from the middle
    snap = snapshots[len(snapshots) // 2]
    ts = snap["timestamp"]
    wayback_url = make_wayback_url(ts, snap["original"])

    logger.info("Fetching sample snapshot from %s …", ts[:8])
    html = fetch_url(wayback_url, timeout=60)
    if not html:
        logger.error("Failed to fetch sample snapshot")
        return

    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"sample_{ts[:8]}.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nSample HTML saved to: {filepath}")
    print(f"Wayback URL: {wayback_url}")
    print(f"\nInspect this file to verify/adjust the HTML parsing logic.")

    # Also try parsing it to show what we find
    jobs = extract_jobs_from_html(html, wayback_url)
    if jobs:
        print(f"\nParser found {len(jobs)} job listing(s) in this snapshot:")
        for j in jobs[:10]:
            print(f"  - {j.get('title', '?')} | team={j.get('team', '?')}")
    else:
        print("\nParser found 0 jobs. The HTML structure may need adjustment.")
        print("Open the sample HTML and look for job listing elements.")


# ---------------------------------------------------------------------------
# Also scrape individual team job pages linked from the main page
# ---------------------------------------------------------------------------


def discover_team_pages(html):
    """Find links to individual team job pages from the main listing."""
    team_pages = []
    pattern = re.compile(
        r'href=["\']([^"\']*teamworkonline\.com/basketball-jobs/[^"\']+)["\']',
        re.IGNORECASE,
    )
    for match in pattern.finditer(html):
        url = match.group(1)
        # Skip if it's a specific job posting (usually has more path segments)
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        if len(parts) <= 3:  # e.g., basketball-jobs/team-name/team-jobs
            clean = clean_wayback_url(url)
            if clean not in team_pages:
                team_pages.append(clean)
    return team_pages


def discover_and_scrape_team_subpages(snapshots, delay, max_snapshots):
    """
    From the main page snapshots, discover links to individual team pages,
    then query the CDX API for those team pages too.
    """
    if not snapshots:
        return []

    # Fetch a couple of snapshots to discover team page URLs
    sample = snapshots[:3]
    team_urls = set()
    for snap in sample:
        wayback_url = make_wayback_url(snap["timestamp"], snap["original"])
        html = fetch_url(wayback_url)
        if html:
            for page_url in discover_team_pages(html):
                team_urls.add(page_url)
        time.sleep(delay)

    if not team_urls:
        logger.info("No team subpages discovered")
        return []

    logger.info("Discovered %d team subpages to scrape", len(team_urls))

    # For each team page, discover snapshots and scrape
    from_date = snapshots[0]["timestamp"][:8]
    to_date = snapshots[-1]["timestamp"][:8]
    all_jobs = []

    for url in sorted(team_urls):
        logger.info("Checking team page: %s", url)
        parsed = urllib.parse.urlparse(url)
        team_path = parsed.netloc + parsed.path
        team_snapshots = discover_snapshots(team_path, from_date, to_date)
        if team_snapshots:
            team_snapshots = dedupe_snapshots_by_month(team_snapshots)
            per_team_max = max(5, (max_snapshots or 50) // max(len(team_urls), 1))
            jobs = scrape_all(team_snapshots, delay, max_snapshots=per_team_max)
            all_jobs.extend(jobs)
        time.sleep(delay)

    return all_jobs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    now = datetime.now(timezone.utc)
    default_from = (now - timedelta(days=DEFAULT_YEARS_BACK * 365)).strftime("%Y%m%d")
    default_to = now.strftime("%Y%m%d")

    p = argparse.ArgumentParser(
        description="Scrape WNBA team job postings from the Wayback Machine",
    )
    p.add_argument("--from", dest="from_date", default=default_from,
                   help=f"Start date YYYYMMDD (default: {default_from})")
    p.add_argument("--to", dest="to_date", default=default_to,
                   help=f"End date YYYYMMDD (default: {default_to})")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                   help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--max-snapshots", type=int, default=None,
                   help="Max snapshots to process from the main page (default: all)")
    p.add_argument("--delay", type=float, default=DEFAULT_REQUEST_DELAY,
                   help=f"Delay between requests in seconds (default: {DEFAULT_REQUEST_DELAY})")
    p.add_argument("--sample-html", action="store_true",
                   help="Fetch one snapshot and dump HTML for inspection")
    p.add_argument("--skip-subpages", action="store_true",
                   help="Skip scraping individual team subpages")
    p.add_argument("--no-dedup", action="store_true",
                   help="Keep duplicate job postings across snapshots")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Enable debug logging")
    return p.parse_args()


def main():
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info("WNBA Team Jobs Wayback Scraper")
    logger.info("Date range: %s to %s", args.from_date, args.to_date)

    # --sample-html mode: dump HTML and exit
    if args.sample_html:
        dump_sample_html(args.from_date, args.to_date, args.output_dir)
        return

    # Step 1: Discover snapshots
    snapshots = discover_snapshots(TARGET_URL, args.from_date, args.to_date)
    if not snapshots:
        logger.error("No snapshots found. Try adjusting the date range.")
        sys.exit(1)

    snapshots = dedupe_snapshots_by_month(snapshots)

    # Step 2: Scrape main page snapshots
    logger.info("Scraping main listing page snapshots …")
    all_jobs = scrape_all(snapshots, args.delay, args.max_snapshots)

    # Step 3: Optionally scrape team subpages for more coverage
    if not args.skip_subpages:
        logger.info("Discovering and scraping team subpages …")
        subpage_jobs = discover_and_scrape_team_subpages(
            snapshots, args.delay, args.max_snapshots or 50
        )
        all_jobs.extend(subpage_jobs)

    if not all_jobs:
        logger.warning("No job listings found in any snapshot.")
        logger.warning("Run with --sample-html to inspect the page structure.")
        sys.exit(1)

    # Step 4: Deduplicate and organize
    logger.info("Total raw job entries: %d", len(all_jobs))
    if not args.no_dedup:
        all_jobs = deduplicate_jobs(all_jobs)
        logger.info("After deduplication: %d unique listings", len(all_jobs))

    by_team = organize_by_team(all_jobs)

    # Step 5: Save outputs
    os.makedirs(args.output_dir, exist_ok=True)
    save_json(by_team, os.path.join(args.output_dir, "jobs_by_team.json"))
    save_csv(all_jobs, os.path.join(args.output_dir, "all_jobs.csv"))
    save_summary(by_team, os.path.join(args.output_dir, "summary.md"))

    # Also save the raw (non-deduped) data for reference
    save_json(
        {"metadata": {
            "source": f"https://{TARGET_URL}",
            "date_range": f"{args.from_date} – {args.to_date}",
            "snapshots_processed": len(snapshots),
            "total_jobs_found": len(all_jobs),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
        }, "jobs_by_team": by_team},
        os.path.join(args.output_dir, "full_export.json"),
    )

    # Print summary
    print_summary(by_team)
    print(f"Results saved to: {os.path.abspath(args.output_dir)}/")
    print(f"  - jobs_by_team.json   (jobs grouped by team)")
    print(f"  - all_jobs.csv        (flat CSV of all postings)")
    print(f"  - summary.md          (human-readable report)")
    print(f"  - full_export.json    (complete export with metadata)")


if __name__ == "__main__":
    main()
