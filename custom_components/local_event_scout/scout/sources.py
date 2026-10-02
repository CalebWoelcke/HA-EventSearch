"""Fetch saved source pages and turn them into compact text for the model.

Many event pages embed schema.org ``Event`` data (JSON-LD). When present it is
used first because it carries exact dates and links; otherwise the visible text
is used, with link targets kept so the model can return real event URLs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

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

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
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
    parser = _TextExtractor(base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML should not kill the run
        pass
    structured = structured_events(parser.json_ld, base_url)
    text = re.sub(r"\n{3,}", "\n\n", parser.text())
    if structured:
        body = "Structured event data on this page:\n" + "\n".join(structured)
        body += "\n\nStart of the page text:\n" + text[:3000]
        return body[:MAX_TEXT], len(structured)
    return text[:MAX_TEXT], 0


async def fetch_page(session: aiohttp.ClientSession, url: str) -> Page:
    try:
        async with session.get(
            url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=20), allow_redirects=True
        ) as response:
            if response.status >= 400:
                return Page(url, error=f"HTTP {response.status}")
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type and "xml" not in content_type and content_type:
                return Page(url, error=f"not a web page ({content_type.split(';')[0]})")
            raw = await response.content.read(MAX_BYTES)
            html = raw.decode(response.charset or "utf-8", errors="replace")
            final_url = str(response.url)
    except asyncio.TimeoutError:
        return Page(url, error="timed out")
    except aiohttp.ClientError as err:
        return Page(url, error=f"could not load ({err.__class__.__name__})")
    text, structured = page_to_text(html, final_url)
    if len(text) < 200 and not structured:
        return Page(url, text=text, error="page has almost no text (it may need JavaScript)")
    return Page(url, text=text, structured_events=structured)


async def fetch_pages(session: aiohttp.ClientSession, urls: list[str], concurrency: int = 4) -> list[Page]:
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(url: str) -> Page:
        async with semaphore:
            return await fetch_page(session, url)

    return list(await asyncio.gather(*(_one(u) for u in urls)))
