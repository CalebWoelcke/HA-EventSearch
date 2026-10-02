"""Fetch saved source pages and turn them into compact text for the model.

Many event pages embed schema.org ``Event`` data (JSON-LD). When present it is
used first because it carries exact dates and links; otherwise the visible text
is used, with link targets kept so the model can return real event URLs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit
import xml.etree.ElementTree as ET

import aiohttp

MAX_BYTES = 2_000_000
MAX_TEXT = 14_000
MAX_STRUCTURED = 80

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HA-EventSearch/0.3; +https://github.com/CalebWoelcke/HA-EventSearch)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "en",
}
_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
_BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section", "header", "footer", "td", "dt", "dd", "time"}


def normalize_url(url: str) -> str:
    """Canonical form used to compare sources (no fragment, no trailing slash, lower-case host)."""
    url, _ = urldefrag(url.strip())
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    path = parts.path.rstrip("/") or ""
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{parts.netloc.lower()}{path}{query}"


def host_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class Page:
    url: str
    text: str = ""
    error: str | None = None
    structured_events: int = 0
    feed: str | None = None  # calendar/RSS feed used instead of the page, if any


class _TextExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.json_ld: list[str] = []
        self._skip_depth = 0
        self._in_ld = False
        self._ld_buffer: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self.feeds: list[tuple[int, str]] = []  # (preference, url); lower is better

    def _feed(self, rank: int, href: str) -> None:
        url = urljoin(self.base_url, href)
        if url.startswith(("http://", "https://")) and all(url != f for _, f in self.feeds):
            self.feeds.append((rank, url))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "alternate" in (attrs.get("rel") or "").lower() and attrs.get("href"):
            kind = (attrs.get("type") or "").lower()
            if "calendar" in kind:
                self._feed(0, attrs["href"])
            elif "rss" in kind or "atom" in kind:
                title = (attrs.get("title") or "").lower()
                if "comment" not in title:
                    self._feed(2 if "event" in title or "event" in attrs["href"].lower() else 3, attrs["href"])
        if tag == "a" and attrs.get("href"):
            href = attrs["href"].lower()
            if href.endswith(".ics") or "ical=1" in href or "ical-feed" in href or href.startswith("webcal:"):
                self._feed(1, attrs["href"].replace("webcal://", "https://", 1))
        if tag == "script" and (attrs.get("type") or "").lower() == "application/ld+json":
            self._in_ld = True
            self._ld_buffer = []
            return
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if tag in _BLOCK:
            self.parts.append("\n")
        if tag == "a" and attrs.get("href"):
            href = attrs["href"].strip()
            if not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self._href = urljoin(self.base_url, href)
                self._link_text = []

    def handle_endtag(self, tag):
        if self._in_ld and tag == "script":
            self._in_ld = False
            self.json_ld.append("".join(self._ld_buffer))
            return
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a" and self._href:
            text = " ".join("".join(self._link_text).split())
            if text and len(text) > 3:
                self.parts.append(f" [{self._href}]")
            self._href = None
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_ld:
            self._ld_buffer.append(data)
            return
        if self._skip_depth:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = (" ".join(line.split()) for line in raw.splitlines())
        return "\n".join(line for line in lines if line)


def _walk_events(node: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_events(item, found)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    if any(isinstance(k, str) and k.endswith("Event") for k in kinds):
        found.append(node)
    for key in ("@graph", "itemListElement", "item", "subEvent", "event", "events"):
        if key in node:
            _walk_events(node[key], found)


def _location_text(location: Any) -> str:
    if isinstance(location, list):
        location = location[0] if location else {}
    if isinstance(location, str):
        return location
    if not isinstance(location, dict):
        return ""
    name = location.get("name") or ""
    address = location.get("address") or ""
    if isinstance(address, dict):
        address = ", ".join(
            str(address.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion") if address.get(k)
        )
    return ", ".join(p for p in (str(name), str(address)) if p)


def structured_events(json_ld_blocks: list[str], base_url: str) -> list[str]:
    """One compact line per schema.org Event found in the page."""
    events: list[dict[str, Any]] = []
    for block in json_ld_blocks:
        try:
            _walk_events(json.loads(block), events)
        except (json.JSONDecodeError, ValueError):
            continue
    lines, seen = [], set()
    for event in events:
        name = " ".join(str(event.get("name") or "").split())
        start = str(event.get("startDate") or "")
        if not name or not start:
            continue
        key = (name.lower(), start[:10])
        if key in seen:
            continue
        seen.add(key)
        url = event.get("url") or ""
        if isinstance(url, str) and url:
            url = urljoin(base_url, url)
        parts = [name, f"start {start}"]
        if event.get("endDate"):
            parts.append(f"end {event['endDate']}")
        if where := _location_text(event.get("location")):
            parts.append(f"at {where}")
        if url:
            parts.append(str(url))
        lines.append(" | ".join(parts))
        if len(lines) >= MAX_STRUCTURED:
            break
    return lines


def page_to_text(html: str, base_url: str) -> tuple[str, int]:
    text, count, _feeds = parse_page(html, base_url)
    return text, count


def parse_page(html: str, base_url: str) -> tuple[str, int, list[str]]:
    parser = _TextExtractor(base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML should not kill the run
        pass
    structured = structured_events(parser.json_ld, base_url)
    text = re.sub(r"\n{3,}", "\n\n", parser.text())
    feeds = [url for _, url in sorted(parser.feeds)]
    if structured:
        body = "Structured event data on this page:\n" + "\n".join(structured)
        body += "\n\nStart of the page text:\n" + text[:3000]
        return body[:MAX_TEXT], len(structured), feeds
    return text[:MAX_TEXT], 0, feeds


def _ics_value(line: str) -> tuple[str, str]:
    name, _, value = line.partition(":")
    return name.split(";")[0].upper(), value.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip()


def _ics_date(value: str) -> str:
    value = value.strip()
    try:
        if len(value) >= 15 and "T" in value:
            moment = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
            return moment.strftime("%Y-%m-%dT%H:%M") + (" UTC" if value.endswith("Z") else "")
        return datetime.strptime(value[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return value


def ics_events(text: str, today: date | None = None) -> list[str]:
    """One compact line per upcoming VEVENT in an iCalendar feed."""
    today = today or date.today()
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    lines, current = [], None
    for raw in unfolded.splitlines():
        if raw.startswith("BEGIN:VEVENT"):
            current = {}
        elif raw.startswith("END:VEVENT") and current is not None:
            start = _ics_date(current.get("DTSTART", ""))
            if current.get("SUMMARY") and start[:10] >= today.isoformat():
                parts = [current["SUMMARY"], f"start {start}"]
                if current.get("DTEND"):
                    parts.append(f"end {_ics_date(current['DTEND'])}")
                if current.get("LOCATION"):
                    parts.append(f"at {current['LOCATION']}")
                if current.get("URL"):
                    parts.append(current["URL"])
                lines.append(" | ".join(parts))
            current = None
        elif current is not None and ":" in raw:
            key, value = _ics_value(raw)
            if key in ("SUMMARY", "DTSTART", "DTEND", "LOCATION", "URL") and key not in current:
                current[key] = value
    lines.sort(key=lambda l: l.split(" | start ")[1][:16] if " | start " in l else "")
    return lines[:MAX_STRUCTURED]


def rss_items(text: str) -> list[str]:
    """One line per RSS/Atom item: title | link | short description."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "link" and child.get("href"):
                fields.setdefault("link", child.get("href"))
            elif child.text:
                fields.setdefault(name, " ".join(child.text.split()))
        title = fields.get("title")
        if not title:
            continue
        desc = re.sub(r"<[^>]+>", " ", fields.get("description") or fields.get("summary") or "")
        desc = " ".join(desc.split())[:200]
        items.append(" | ".join(p for p in (title, fields.get("link", ""), desc) if p))
        if len(items) >= MAX_STRUCTURED:
            break
    return items


async def _get(session: aiohttp.ClientSession, url: str) -> tuple[str | None, str | None, str, str]:
    """Return (body, error, content_type, final_url)."""
    try:
        async with session.get(
            url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True
        ) as response:
            if response.status >= 400:
                return None, f"HTTP {response.status}", "", url
            content_type = response.headers.get("Content-Type", "").lower()
            raw = await response.content.read(MAX_BYTES)
            return raw.decode(response.charset or "utf-8", errors="replace"), None, content_type, str(response.url)
    except asyncio.TimeoutError:
        return None, "timed out", "", url
    except aiohttp.ClientError as err:
        return None, f"could not load ({err.__class__.__name__})", "", url


def _feed_text(body: str, content_type: str) -> tuple[str, int]:
    if "calendar" in content_type or body.lstrip().startswith("BEGIN:VCALENDAR"):
        events = ics_events(body)
        return ("Events from the calendar feed:\n" + "\n".join(events), len(events)) if events else ("", 0)
    items = rss_items(body)
    return ("Items from the feed:\n" + "\n".join(items), len(items)) if items else ("", 0)


async def fetch_page(session: aiohttp.ClientSession, url: str) -> Page:
    body, error, content_type, final_url = await _get(session, url)
    if error:
        return Page(url, error=error)
    if "calendar" in content_type or ("xml" in content_type and "html" not in content_type):
        text, count = _feed_text(body or "", content_type)  # the saved source is itself a feed
        return Page(url, text=text, structured_events=count, feed=url) if text else Page(url, error="feed has no upcoming events")
    if content_type and "html" not in content_type:
        return Page(url, error=f"not a web page ({content_type.split(';')[0]})")
    text, structured, feeds = parse_page(body or "", final_url)
    if structured or len(text) >= 1500 or not feeds:
        if len(text) < 200 and not structured:
            return Page(url, text=text, error="page has almost no text (it may need JavaScript)")
        return Page(url, text=text, structured_events=structured)
    # The page is thin (often a JavaScript calendar); try the feed it advertises.
    for feed_url in feeds[:2]:
        feed_body, feed_error, feed_type, _ = await _get(session, feed_url)
        if feed_error or not feed_body:
            continue
        feed_text, count = _feed_text(feed_body, feed_type)
        if feed_text:
            return Page(url, text=feed_text[:MAX_TEXT], structured_events=count, feed=feed_url)
    if len(text) < 200:
        return Page(url, text=text, error="page has almost no text (it may need JavaScript)")
    return Page(url, text=text)


async def fetch_pages(session: aiohttp.ClientSession, urls: list[str], concurrency: int = 4) -> list[Page]:
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(url: str) -> Page:
        async with semaphore:
            return await fetch_page(session, url)

    return list(await asyncio.gather(*(_one(u) for u in urls)))
