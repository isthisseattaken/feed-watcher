"""
Feed watcher: builds one merged RSS feed from the sources in sources.yml.

For each source it either reads an existing feed (type: feed) or reads a
web page and keeps the links that match your rules (type: page). Every
item it has ever seen is remembered in data/seen.json, so an item keeps
the same ID and date across runs and never shows up as "new" twice.

Output:
  docs/feed.xml    the merged RSS feed (point Make at this)
  docs/index.html  a simple reading page of the latest items
"""

import html
import json
import sys
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "sources.yml"
SEEN_FILE = ROOT / "data" / "seen.json"
FEED_FILE = ROOT / "docs" / "feed.xml"
INDEX_FILE = ROOT / "docs" / "index.html"

PAUSE_BETWEEN_REQUESTS = 2      # seconds; be polite to the sites you read
TIMEOUT = 20                    # seconds before giving up on a site
FORGET_AFTER_DAYS = 90          # forget items not seen on any page for this long

# Link text that says nothing about the article; ignored when picking a title.
GENERIC_TEXT = {"read more", "read the article", "continue reading", "more", "link"}


# ── Helpers ──────────────────────────────────────────────────

def now():
    return datetime.now(timezone.utc)


def clean_url(url, strip_query=True):
    """Drop #fragments and (by default) ?tracking=bits so one article = one address."""
    parts = urlsplit(url)
    query = "" if strip_query else parts.query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def clean_text(text):
    return " ".join((text or "").split())


class Fetcher:
    """Downloads pages, checks robots.txt first, and pauses between requests."""

    def __init__(self, user_agent):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.user_agent = user_agent
        self.robots = {}
        self.last_request = 0.0

    def _wait(self):
        gap = time.monotonic() - self.last_request
        if gap < PAUSE_BETWEEN_REQUESTS:
            time.sleep(PAUSE_BETWEEN_REQUESTS - gap)
        self.last_request = time.monotonic()

    def allowed(self, url):
        parts = urlsplit(url)
        site = f"{parts.scheme}://{parts.netloc}"
        if site not in self.robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                self._wait()
                r = self.session.get(site + "/robots.txt", timeout=TIMEOUT)
                parser.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                parser.parse([])          # no robots.txt reachable: treat as allowed
            self.robots[site] = parser
        return self.robots[site].can_fetch(self.user_agent, url)

    def get(self, url):
        if not self.allowed(url):
            raise RuntimeError("robots.txt asks automated tools not to read this page")
        self._wait()
        r = self.session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r


# ── Reading sources ──────────────────────────────────────────

def items_from_page(source, response):
    """Find article links on a page using the source's rules."""
    # If the server names its character encoding, trust it; otherwise let
    # BeautifulSoup work it out from the page (avoids garbled “quotes”).
    declared = "charset" in response.headers.get("Content-Type", "").lower()
    soup = BeautifulSoup(response.text if declared else response.content, "html.parser")
    selector = source.get("selector")
    anchors = soup.select(selector) if selector else soup.find_all("a")
    # A selector may point at the link itself or at something containing links.
    links = []
    for el in anchors:
        links.extend([el] if el.name == "a" else el.find_all("a"))

    starts = source.get("link_starts_with", "")
    skips = source.get("skip_if_contains", []) or []
    strip_query = source.get("strip_query", True)

    def article_url(a):
        """The cleaned article address this link points to, or None if it isn't one."""
        href = a.get("href")
        if not href:
            return None
        url = clean_url(urljoin(response.url, href), strip_query)
        if starts and not url.startswith(starts):
            return None
        if url.rstrip("/") == starts.rstrip("/"):
            return None                    # the listing page itself
        if any(s in url for s in skips):
            return None
        return url

    found = {}      # url -> best title
    summaries = {}  # url -> short description, when the page has one
    for a in links:
        url = article_url(a)
        if not url:
            continue
        text = clean_text(a.get_text(" "))
        if text.lower().rstrip(" ›»>.") in GENERIC_TEXT:
            text = ""
        # The same article is often linked several times (image, headline,
        # "read more"). Keep the longest meaningful text as the title.
        if len(text) > len(found.get(url, "")):
            found[url] = text
        else:
            found.setdefault(url, "")
        if url not in summaries:
            summary = nearby_summary(a, url, article_url)
            if summary:
                summaries[url] = summary

    items = []
    for url, title in found.items():
        summary = summaries.get(url, "")
        if summary and title and summary.lower().startswith(title.lower()):
            summary = ""                   # just the headline again
        items.append({"link": url, "title": title or url, "summary": summary})
    return items


def nearby_summary(a, url, article_url):
    """Find the short description that sits next to an article's link.

    Climbs from the link to the box around it (an article "card"), stopping
    as soon as a box holds links to more than one article, so a card never
    borrows its neighbour's description.
    """
    box = a
    for _ in range(5):
        box = box.parent
        if box is None or box.name in ("body", "html", "[document]"):
            return ""
        others = {article_url(x) for x in box.find_all("a")} - {None, url}
        if others:
            return ""
        for el in box.find_all(["p", "div", "span"]):
            if el.find(["p", "div", "h1", "h2", "h3", "h4"]):
                continue                   # only innermost blocks of text
            if el.find_parent("a") or el.find_parent(["h1", "h2", "h3", "h4"]):
                continue                   # headlines and link text aren't descriptions
            # Use the block's own words, leaving out any links inside it
            # (like a trailing "Read the article ›").
            words = [s for s in el.find_all(string=True) if not s.find_parent("a")]
            text = clean_text(" ".join(words))
            if len(text) >= 40:
                return shorten(text)
    return ""


def shorten(text, limit=300):
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def html_to_text(fragment):
    return clean_text(BeautifulSoup(fragment or "", "html.parser").get_text(" "))


def items_from_feed(response):
    """Read an RSS 2.0 or Atom feed."""
    root = ET.fromstring(response.content)
    items = []
    # RSS 2.0
    for item in root.iter("item"):
        link = clean_text(item.findtext("link"))
        title = clean_text(item.findtext("title"))
        summary = shorten(html_to_text(item.findtext("description")))
        if link:
            items.append({"link": link, "title": title or link, "summary": summary})
    # Atom
    atom = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(f"{atom}entry"):
        link_el = entry.find(f"{atom}link[@rel='alternate']")
        if link_el is None:
            link_el = entry.find(f"{atom}link")
        link = link_el.get("href") if link_el is not None else ""
        title = clean_text(entry.findtext(f"{atom}title"))
        raw = entry.findtext(f"{atom}summary") or entry.findtext(f"{atom}content")
        summary = shorten(html_to_text(raw))
        if link:
            items.append({"link": link, "title": title or link, "summary": summary})
    return items


# ── Writing output ───────────────────────────────────────────

def write_feed(feed_cfg, entries):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = feed_cfg.get("title", "Feed Watcher")
    ET.SubElement(channel, "link").text = feed_cfg.get("link", "")
    ET.SubElement(channel, "description").text = feed_cfg.get("description") or "Merged feed"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(now())

    for e in entries:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = e["title"]
        ET.SubElement(item, "link").text = e["link"]
        ET.SubElement(item, "guid", isPermaLink="true").text = e["link"]
        ET.SubElement(item, "category").text = e["source"]
        ET.SubElement(item, "description").text = e.get("summary") or f"From {e['source']}"
        first = datetime.fromisoformat(e["first_seen"])
        ET.SubElement(item, "pubDate").text = format_datetime(first)

    ET.indent(rss)
    FEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(rss).write(FEED_FILE, encoding="utf-8", xml_declaration=True)


def write_index(feed_cfg, entries):
    def row(e):
        summary = e.get("summary") or ""
        summary_html = f'\n        <p class="summary">{html.escape(summary)}</p>' if summary else ""
        return (f'      <li>\n        <a href="{html.escape(e["link"])}">{html.escape(e["title"])}</a>'
                f'{summary_html}\n'
                f'        <span>{html.escape(e["source"])} · {e["first_seen"][:10]}</span>\n      </li>')

    rows = "\n".join(row(e) for e in entries[:50])
    title = html.escape(feed_cfg.get("title", "Feed Watcher"))
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="alternate" type="application/rss+xml" href="feed.xml">
  <style>
    body {{ font: 16px/1.5 Georgia, serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; color: #222; background: #fbfaf7; }}
    h1 {{ font-size: 1.6rem; margin-bottom: .2rem; }}
    p.meta {{ color: #777; margin-top: 0; }}
    ul {{ list-style: none; padding: 0; }}
    li {{ padding: .6rem 0; border-bottom: 1px solid #e6e2da; }}
    a {{ color: #1a4d6e; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    p.summary {{ margin: .25rem 0 .3rem; color: #555; font-size: .95rem; }}
    span {{ display: block; font-size: .85rem; color: #888; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p class="meta">Updated {now().strftime("%b %d, %Y %H:%M UTC")} · <a href="feed.xml">RSS feed</a></p>
  <ul>
{rows}
  </ul>
</body>
</html>
"""
    INDEX_FILE.write_text(page, encoding="utf-8")


# ── Main ─────────────────────────────────────────────────────

def main():
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    feed_cfg = config.get("feed", {}) or {}
    sources = config.get("sources", []) or []

    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else {}
    user_agent = f"personal-feed-watcher/1.0 (+{feed_cfg.get('link', '')})"
    fetcher = Fetcher(user_agent)
    stamp = now().isoformat(timespec="seconds")
    new_count, problems = 0, []

    for source in sources:
        label = source.get("label") or source.get("url")
        try:
            response = fetcher.get(source["url"])
            if source.get("type", "page") == "feed":
                found = items_from_feed(response)
            else:
                found = items_from_page(source, response)
        except Exception as err:  # one broken source shouldn't stop the rest
            problems.append(f"{label}: {err}")
            print(f"  ✗ {label}: {err}")
            continue

        added = 0
        for item in found:
            key = item["link"]
            if key in seen:
                seen[key]["last_seen"] = stamp
                # Upgrade a placeholder title if we now have a real one.
                if seen[key]["title"] == key and item["title"] != key:
                    seen[key]["title"] = item["title"]
                # Fill in a description for items saved before we had one.
                if item.get("summary") and not seen[key].get("summary"):
                    seen[key]["summary"] = item["summary"]
            else:
                seen[key] = {"title": item["title"], "link": key, "source": label,
                             "summary": item.get("summary", ""),
                             "first_seen": stamp, "last_seen": stamp}
                added += 1
        new_count += added
        print(f"  ✓ {label}: {len(found)} links found, {added} new")
        if not found:
            problems.append(f"{label}: no matching links (has the page changed?)")

    # Forget items that haven't appeared on any page for a long time.
    cutoff = (now() - timedelta(days=FORGET_AFTER_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v["last_seen"] >= cutoff}

    entries = sorted(seen.values(), key=lambda e: (e["first_seen"], e["title"]), reverse=True)
    entries = entries[: int(feed_cfg.get("max_items", 200))]

    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=1, ensure_ascii=False), encoding="utf-8")
    write_feed(feed_cfg, entries)
    write_index(feed_cfg, entries)

    print(f"Done: {new_count} new item(s), {len(entries)} in feed.")
    if problems:
        print("Needs a look:\n  - " + "\n  - ".join(problems))
    # Fail the run only if every source failed, so GitHub emails you.
    if sources and len(problems) == len(sources):
        sys.exit(1)


if __name__ == "__main__":
    main()
