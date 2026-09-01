# Web search

Web search lets the assistant query Tavily or Serper during a conversation when a search key is configured on the server or pasted in Tools.

## Sub-features

- `search-config` reports whether search is available via `/api/config`.
- `search-enable` turns on the Local web search toggle in Tools.
- `search-api` returns titles, snippets, and URLs for a query.
- `search-tool-call` runs during an active voice session when the model calls `web_search`.

## How to get to it (user POV)

- Open `Tools` and enable **Local web search** when a key is available.
- Ask a question that needs current facts during a connected voice session.

## Driving it with browser-use

Preconditions:

- `doctor.sh` is green for this run.
- `TAVILY_API_KEY` or `SERPER_API_KEY` is set in `~/.config/chatbot/env` (or will be supplied in the request body).
- `GET /api/config` returns `"search": true`.

- **Config check.** Run `curl -sf "$BASE_URL/api/config"`. `search` is `true`.
- **Tools UI.** Open Tools. `#tool-web` is enabled when `search` is true. Toggle `#tool-web` on if testing the browser path.
- **API search.** Run `curl -sf -X POST "$BASE_URL/api/search" -H 'Content-Type: application/json' -d '{"query":"OpenRouter API status"}'`. Response JSON includes `results` array with at least one item having `title`, `snippet`, and `url`.
- **Empty query guard.** Run the same endpoint with `{"query":""}`. HTTP status is `400`.
- **No-key guard.** On a run without keys, `POST /api/search` returns `503` and Tools disables the toggle.
- **Proof.** Save `search-response.json` and `tools-search.png` under `artifacts/verify-chatbot/runs/$RUN_ID/web-search/`.

## Gotchas

- Server keys in `~/.config/chatbot/env` take precedence over the per-browser `#search-key` field.
- Tavily keys start with `tvly-`. Serper keys use the Serper HTTP API shape.
- Search hits the real network. Use a harmless query and expect provider latency.
- Voice tool-call proof needs a connected orb session and model cooperation; prefer the API path for deterministic verification.
