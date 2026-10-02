"""Model providers: OpenRouter (any model, paid web search) and Google Gemini (AI Studio keys).

Both expose the same ``complete()`` call so the pipeline does not care which one
is in use. ``web_search=True`` lets the model search the web; the other calls
(reading saved pages, ranking) never search.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from typing import Any, Protocol

import aiohttp

from .openrouter import OpenRouterAuthError, OpenRouterClient, OpenRouterError

# Search limits per OpenRouter call: the main cost driver is how many results the
# model reads (and re-reads on every search round), so keep this tight.
OPENROUTER_MAX_SEARCHES = 2
OPENROUTER_RESULTS_PER_SEARCH = 6
OPENROUTER_TOTAL_RESULTS = 12

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# USD per 1M tokens (input, output) on Google's paid tier, used only to estimate
# the cost of calls made with the billing-enabled search key.
GEMINI_PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
}


class ProviderError(OpenRouterError):
    """A model call failed; the message is safe to show."""


class ProviderAuthError(OpenRouterAuthError):
    """An API key was rejected."""


@dataclass
class Completion:
    content: str
    cost: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)  # pages the search used, when known


class LLMProvider(Protocol):
    name: str

    async def complete(self, system: str, user: str, *, web_search: bool, reasoning: str) -> Completion: ...


# ----------------------------------------------------------------- OpenRouter


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, client: OpenRouterClient, model: str, engine: str = "auto") -> None:
        self._client = client
        self.model = model
        self._engine = engine

    async def complete(self, system: str, user: str, *, web_search: bool, reasoning: str) -> Completion:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2 if web_search else 0.1,
            "reasoning": {"effort": reasoning},
        }
        if web_search:
            params: dict[str, Any] = {
                "max_uses": OPENROUTER_MAX_SEARCHES,
                "max_results": OPENROUTER_RESULTS_PER_SEARCH,
                "max_total_results": OPENROUTER_TOTAL_RESULTS,
            }
            if self._engine and self._engine != "auto":
                params["engine"] = self._engine
            payload["tools"] = [{"type": "openrouter:web_search", "parameters": params}]
        chat = await self._client.chat(payload)
        usage = chat.usage_summary()
        usage["model"] = self.model
        return Completion(content=chat.content, cost=chat.cost, usage=usage)


# --------------------------------------------------------------------- Gemini


class GeminiProvider:
    """Google AI Studio keys.

    Google Search grounding is not available on the free tier, so searches use
    ``search_key`` (a project with billing enabled, which includes 5,000 free
    searches a month) while everything else can use a free-tier ``process_key``.
    """

    name = "gemini"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        search_key: str,
        process_key: str | None,
        search_model: str,
        process_model: str,
        process_key_is_free: bool = True,
        timeout: float = 180,
    ) -> None:
        self._session = session
        self._search_key = search_key
        self._process_key = process_key or search_key
        self.search_model = search_model
        self.process_model = process_model
        self._process_free = bool(process_key) and process_key_is_free
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def complete(self, system: str, user: str, *, web_search: bool, reasoning: str) -> Completion:
        model = self.search_model if web_search else self.process_model
        key = self._search_key if web_search else self._process_key
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2 if web_search else 0.1},
        }
        thinking = _thinking_config(model, reasoning)
        if thinking:
            body["generationConfig"]["thinkingConfig"] = thinking
        if web_search:
            body["tools"] = [{"google_search": {}}]
        try:
            data = await self._post(model, key, body)
        except ProviderError as err:
            # Older/unknown models may reject the thinking settings; retry once without them.
            if thinking and "thinking" in str(err).lower():
                body["generationConfig"].pop("thinkingConfig", None)
                data = await self._post(model, key, body)
            else:
                raise
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise ProviderError(f"Gemini returned no answer{f' ({reason})' if reason else ''}.")
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
        meta = data.get("usageMetadata") or {}
        grounding = candidate.get("groundingMetadata") or {}
        searches = len(grounding.get("webSearchQueries") or []) if web_search else 0
        input_tokens = (meta.get("promptTokenCount") or 0) + (meta.get("toolUsePromptTokenCount") or 0)
        output_tokens = (meta.get("candidatesTokenCount") or 0) + (meta.get("thoughtsTokenCount") or 0)
        free = (not web_search) and self._process_free
        price = GEMINI_PRICES.get(model)
        cost = 0.0 if free or not price else (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
        sources = [
            chunk["web"]["uri"]
            for chunk in grounding.get("groundingChunks") or []
            if isinstance(chunk, dict) and isinstance(chunk.get("web"), dict) and chunk["web"].get("uri")
        ]
        usage = {
            "model": model,
            "cost": round(cost, 6),
            "cost_estimated": not free and price is not None,
            "free_tier": free,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": meta.get("thoughtsTokenCount"),
            "web_searches": searches,
        }
        return Completion(content=text, cost=cost, usage=usage, sources=sources)

    async def _post(self, model: str, key: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{GEMINI_BASE}/models/{model}:generateContent"
        try:
            async with self._session.post(
                url, json=body, headers={"x-goog-api-key": key}, timeout=self._timeout
            ) as response:
                text = await response.text()
                status = response.status
        except asyncio.TimeoutError as err:
            raise ProviderError("Gemini took too long to respond.") from err
        except aiohttp.ClientError as err:
            raise ProviderError(f"Could not reach Gemini: {err}") from err
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {}
        message = ((data.get("error") or {}).get("message") or text[:200]) if isinstance(data, dict) else text[:200]
        if status in (401, 403) or (status == 400 and "API key" in message):
            raise ProviderAuthError(f"Google rejected the API key: {message}")
        if status == 429:
            raise ProviderError(f"Gemini rate limit or quota reached: {message}")
        if status >= 400:
            raise ProviderError(f"Gemini returned HTTP {status}: {message}")
        return data


def _thinking_config(model: str, reasoning: str) -> dict[str, Any] | None:
    if model.startswith("gemini-3"):
        # Gemini 3 cannot fully disable thinking; "low" is the cheapest supported level.
        return {"thinkingLevel": "low"}
    if model.startswith("gemini-2.5"):
        if "pro" in model:
            return {"thinkingBudget": 128}
        return {"thinkingBudget": 0 if reasoning == "none" else 512}
    return None


async def validate_gemini_key(session: aiohttp.ClientSession, key: str) -> str | None:
    """Return an error key for the config flow, or None if the key works."""
    try:
        async with session.get(
            f"{GEMINI_BASE}/models", headers={"x-goog-api-key": key}, timeout=aiohttp.ClientTimeout(total=20)
        ) as response:
            if response.status in (400, 401, 403):
                return "invalid_auth"
            if response.status >= 400:
                return "cannot_connect"
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return "cannot_connect"
    return None
