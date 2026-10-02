"""Minimal OpenRouter client: chat completions with cost, plus key usage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any

import aiohttp

BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterError(Exception):
    """A request failed; the message is safe to show to the user."""


class OpenRouterAuthError(OpenRouterError):
    """The API key was rejected."""


@dataclass
class ChatResult:
    content: str
    cost: float
    raw: dict[str, Any]

    def usage_summary(self) -> dict[str, Any]:
        """Token, search and cost figures for one call (for the usage breakdown)."""
        usage = self.raw.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        tools = usage.get("server_tool_use") or {}
        return {
            "cost": round(self.cost, 6),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": details.get("reasoning_tokens"),
            "web_searches": tools.get("web_search_requests"),
        }


class OpenRouterClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        app_title: str = "Home Assistant Local Event Scout",
        timeout: float = 180,
    ) -> None:
        self._session = session
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": app_title,
            "HTTP-Referer": "https://github.com/CalebWoelcke/HA-EventSearch",
        }
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def chat(self, payload: dict[str, Any]) -> ChatResult:
        body = {**payload, "usage": {"include": True}}
        raw = await self._request("POST", "/chat/completions", body)
        if "error" in raw:
            raise OpenRouterError(_error_message(raw))
        try:
            message = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as err:
            raise OpenRouterError("OpenRouter returned a response with no message.") from err
        content = message.get("content")
        if isinstance(content, list):  # some providers return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        usage = raw.get("usage") or {}
        try:
            cost = float(usage.get("cost") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        return ChatResult(content=content or "", cost=cost, raw=raw)

    async def key_info(self) -> dict[str, Any]:
        """Usage and limit for the current key (GET /key)."""
        raw = await self._request("GET", "/key", None)
        return raw.get("data") or {}

    async def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        try:
            async with self._session.request(
                method, BASE_URL + path, json=body, headers=self._headers, timeout=self._timeout
            ) as response:
                text = await response.text()
                status = response.status
        except asyncio.TimeoutError as err:
            raise OpenRouterError("OpenRouter took too long to respond.") from err
        except aiohttp.ClientError as err:
            raise OpenRouterError(f"Could not reach OpenRouter: {err}") from err
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {}
        if status in (401, 403):
            raise OpenRouterAuthError(f"OpenRouter rejected the API key (HTTP {status}).")
        if status == 402:
            raise OpenRouterError("OpenRouter spending limit reached or no credits left (HTTP 402).")
        if status >= 400:
            raise OpenRouterError(f"OpenRouter returned HTTP {status}: {_error_message(data) or text[:200]}")
        return data


def _error_message(data: dict[str, Any]) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or "")
