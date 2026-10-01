"""Cheap check that event links actually exist."""

from __future__ import annotations

import asyncio

import aiohttp

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HA-EventSearch/0.2; +https://www.home-assistant.io)",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


async def check_url(session: aiohttp.ClientSession, url: str) -> bool | None:
    """Return True if the page loads, False if it clearly doesn't, None if unsure.

    Many event sites block bots (403/429) or reject HEAD, so only definite
    failures (404/410, DNS errors, malformed URLs) count as broken.
    """
    if not url.startswith(("http://", "https://")):
        return False
    timeout = aiohttp.ClientTimeout(total=10)
    for method in ("HEAD", "GET"):
        try:
            async with session.request(
                method, url, headers=_HEADERS, timeout=timeout, allow_redirects=True
            ) as response:
                status = response.status
        except aiohttp.InvalidURL:
            return False
        except aiohttp.ClientConnectorError as err:
            # DNS failure means the site does not exist; other connect errors are unclear.
            return False if "Name or service not known" in str(err) or "nodename" in str(err) else None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        if status < 400:
            return True
        if status in (404, 410):
            return False
        if method == "HEAD" and status in (400, 403, 405, 501):
            continue  # some servers only answer GET
        return None
    return None


async def check_urls(session: aiohttp.ClientSession, urls: list[str], concurrency: int = 5) -> dict[str, bool | None]:
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(url: str) -> tuple[str, bool | None]:
        async with semaphore:
            return url, await check_url(session, url)

    return dict(await asyncio.gather(*(_one(u) for u in dict.fromkeys(urls))))
