# Local Event Scout

A Home Assistant integration that searches the web each night for local events matching your interests, scores them, and shows the best ones on a sidebar page and in a calendar.

It uses [OpenRouter](https://openrouter.ai) for the AI model and web search. The default model is `deepseek/deepseek-v4.1-flash`. You can switch models on the settings page.

## What it does

- **Distance groups.** Each interest belongs to a group: Nearby 15 km, Local 50 km, Day trip 150 km, or Worth travelling 400 km. Each group has its own look-ahead window and search frequency, so far-away festivals show up months ahead while trivia nights are checked daily.
- **Real distances.** Venues are located with OpenStreetMap, and events outside their group's radius are dropped.
- **Scoring.** Every event gets a 0–10 match score with a one-line reason. Picks are events at or above your minimum score.
- **Feedback.** 👍 and 👎 on an event teach future searches. Hiding an event only removes it from view.
- **Weather.** Outdoor events on days with a bad forecast get a warning and a lower score. This uses your Home Assistant weather entity.
- **Clean results.** Today's date goes into every search. Past events, duplicates, and events with broken links are removed.
- **Spending.** Your OpenRouter key's monthly usage and limit appear on the page and as a sensor. Scans are skipped when the limit is nearly used up.

### Entities

| Entity | What it shows |
|---|---|
| `sensor.event_scout_status` | `ok`, `scanning`, `error`, `budget` or `never`, plus details of the last search |
| `sensor.event_scout_upcoming_picks` | Number of picks, with the top five as attributes |
| `sensor.event_scout_openrouter_spend_this_month` | Key usage this month in USD; the limit and remaining amount are attributes |
| `sensor.event_scout_last_search_cost` | Cost of the most recent search |
| `calendar.event_scout_picks` | Picks as calendar events |

Action: `local_event_scout.scan_now`. With `force: false` it only searches groups that are due.

## Install

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/CalebWoelcke/HA-EventSearch` as an **Integration**.
2. Install **Local Event Scout**, then restart Home Assistant.

### Manual

Copy `custom_components/local_event_scout` into your Home Assistant `config/custom_components/` folder and restart.

## Set up

1. Create an OpenRouter API key and **set a monthly limit on it**. $1 is plenty for daily searches.
2. **Settings → Devices & services → Add integration → Local Event Scout**, then paste the key.
3. Open **Event Scout** in the sidebar. On the **Settings** tab, add your location and interests, then save.
4. Press **Search now**, or wait for the nightly search (02:30 by default).

To change the API key later: **Settings → Devices & services → Local Event Scout → ⋮ → Reconfigure**.

## Cost

Each search costs OpenRouter's web-search fee (about $0.005 at the time of writing) plus a small amount of model usage. Ranking adds one cheap request per run. With the default group frequencies, one location and interests in three groups, expect roughly $0.30–$0.60 a month. The last search's cost is shown on the page.

## Developing

The search logic in `custom_components/local_event_scout/scout/` has no Home Assistant imports, so you can run and tune it on any PC:

```bash
pip install aiohttp pytest pytest-asyncio
export OPENROUTER_API_KEY=sk-or-...          # PowerShell: $env:OPENROUTER_API_KEY="sk-or-..."
python scripts/scout.py --city Edmonton --region Alberta --country Canada \
    --interest "trivia nights:nearby" --interest "indie concerts:local" --dislike "kids events"
pytest
```

Prompts are in `scout/prompts.py`. The Home Assistant side (storage, scheduling, sensors, calendar, sidebar page) lives in the rest of the integration folder.
