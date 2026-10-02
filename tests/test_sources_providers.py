import aiohttp

from scout.providers import GeminiProvider, OpenRouterProvider
from scout.sources import normalize_url, page_to_text

HTML = """<html><head><title>Venue</title>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
  {"@type":"MusicEvent","name":"The Trews","startDate":"2026-10-10T20:00","url":"/shows/trews",
   "location":{"@type":"Place","name":"Starlite Room","address":{"streetAddress":"10030 102 St","addressLocality":"Edmonton"}}},
  {"@type":"Organization","name":"Not an event"}]}</script>
<script>var x = 1;</script><style>.a{}</style></head>
<body><h1>What's on</h1><p>Trivia every Tuesday <a href="/trivia">Details</a></p></body></html>"""


def test_page_to_text_prefers_structured_events_and_keeps_links():
    text, count = page_to_text(HTML, "https://venue.example/events")
    assert count == 1
    assert "The Trews | start 2026-10-10T20:00 | at Starlite Room, 10030 102 St, Edmonton | https://venue.example/shows/trews" in text
    assert "Trivia every Tuesday" in text and "[https://venue.example/trivia]" in text
    assert "var x" not in text and ".a{}" not in text


def test_page_to_text_without_structured_data():
    text, count = page_to_text("<body><div>Folk night Oct 12</div><a href='https://t.example/x'>Tickets here</a></body>", "https://v.example/")
    assert count == 0 and "Folk night Oct 12" in text and "[https://t.example/x]" in text


def test_normalize_url():
    assert normalize_url("https://WWW.Venue.example/events/#top") == "https://www.venue.example/events"
    assert normalize_url("ftp://x") == "" and normalize_url("not a url") == ""


class _Client:
    def __init__(self):
        self.payloads = []

    async def chat(self, payload):
        from scout.openrouter import ChatResult

        self.payloads.append(payload)
        return ChatResult("{}", 0.004, {"usage": {"prompt_tokens": 900, "completion_tokens": 200,
                                                  "completion_tokens_details": {"reasoning_tokens": 150},
                                                  "server_tool_use": {"web_search_requests": 2}, "cost": 0.004}})


async def test_openrouter_provider_limits_search_and_reports_usage():
    client = _Client()
    provider = OpenRouterProvider(client, "deepseek/deepseek-v4.1-flash", "exa")
    done = await provider.complete("sys", "user", web_search=True, reasoning="low")
    params = client.payloads[0]["tools"][0]["parameters"]
    assert params == {"max_uses": 2, "max_results": 6, "max_total_results": 12, "engine": "exa"}
    assert client.payloads[0]["reasoning"] == {"effort": "low"}
    assert done.usage == {"cost": 0.004, "input_tokens": 900, "output_tokens": 200, "reasoning_tokens": 150,
                          "web_searches": 2, "model": "deepseek/deepseek-v4.1-flash"}
    await provider.complete("sys", "user", web_search=False, reasoning="none")
    assert "tools" not in client.payloads[1] and client.payloads[1]["reasoning"] == {"effort": "none"}


async def test_gemini_provider_uses_the_right_key_and_estimates_cost(monkeypatch):
    calls = []
    response = {
        "candidates": [{
            "content": {"parts": [{"text": "thinking...", "thought": True}, {"text": '{"events": []}'}]},
            "groundingMetadata": {"webSearchQueries": ["a", "b"], "groundingChunks": [{"web": {"uri": "https://r.example/1"}}]},
        }],
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 500, "thoughtsTokenCount": 500, "toolUsePromptTokenCount": 3000},
    }
    async with aiohttp.ClientSession() as session:
        provider = GeminiProvider(session, search_key="PAID", process_key="FREE",
                                  search_model="gemini-3.1-flash-lite", process_model="gemini-3.8-flash")

        async def fake_post(model, key, body):
            calls.append((model, key, body))
            return response

        monkeypatch.setattr(provider, "_post", fake_post)
        searched = await provider.complete("sys", "find", web_search=True, reasoning="low")
        ranked = await provider.complete("sys", "rank", web_search=False, reasoning="none")

    assert calls[0][:2] == ("gemini-3.1-flash-lite", "PAID") and calls[0][2]["tools"] == [{"google_search": {}}]
    assert calls[1][:2] == ("gemini-3.8-flash", "FREE") and "tools" not in calls[1][2]
    assert searched.content == '{"events": []}' and searched.sources == ["https://r.example/1"]
    assert searched.usage["web_searches"] == 2 and searched.usage["input_tokens"] == 4000 and searched.usage["output_tokens"] == 1000
    assert abs(searched.cost - (4000 * 0.25 + 1000 * 1.50) / 1e6) < 1e-12
    assert ranked.cost == 0 and ranked.usage["free_tier"] is True
