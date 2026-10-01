# Local Event Scout

A Home Assistant custom integration that searches for interesting local events every night and presents them in a dedicated sidebar page. It uses an OpenRouter API key and defaults to `deepseek/deepseek-v4.1-flash` with OpenRouter's web search tool.

## Install through Samba

1. Copy `custom_components/local_event_scout` into your Home Assistant configuration directory at `custom_components/local_event_scout`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**, search for **Local Event Scout**, and enter an OpenRouter API key.
4. Open **Event Scout** from the sidebar. Add one or more locations, your interests and dislikes, then select **Search now**.

The API key is held in Home Assistant's config entry and never appears in the panel. The panel stores locations, preferences, results, and deduplication history using Home Assistant's internal storage.

## Notes

- The nightly scan is configurable in the page and uses Home Assistant's configured timezone.
- The default search engine is `parallel`; both `parallel` and `exa` are supported from the UI.
- This is a custom integration, so Home Assistant will show the customary warning until it is added to an official distribution channel.
- Set a low OpenRouter monthly spending limit while testing. A model can choose to make more than one search in a run.
