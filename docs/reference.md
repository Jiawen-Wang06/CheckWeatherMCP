# Reference

This page has the usage and implementation details. The overview is in the [main README](../README.md).

## Setup

```powershell
python -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }   # then fill in the two keys
```

```dotenv
OPENWEATHERMAP_API_KEY=your_openweathermap_api_key
OPENAI_API_KEY=your_openai_api_key
```

`.env` is ignored by Git. A new OpenWeather key can take from a few minutes to a couple of hours to start working.

## Terminal client

```powershell
python .\mcp_client.py      # exit, quit or q to leave
```

| Command | Example |
|---|---|
| `/prompts` | Lists each server's prompts |
| `/prompt <server> <name> "arg1" "arg2"` | `/prompt weather compare_weather_prompt "Toronto,ca" "Vancouver,ca"`<br>`/prompt tasks plan_trip_prompt "Montreal" 3`<br>`/prompt tasks plan_trip_for_future_date "Quebec City" "2026-12-04" "2026-12-06"` |
| `/resources` | Lists each server's resources |
| `/resource <server> <uri> [instruction]` | `/resource tasks file://meeting_notes List the action items`. Without an instruction, the content is added to the conversation |

## Web app

```powershell
python -m uvicorn web.app:app --port 8000      # http://localhost:8000
```

The web app supports the same slash commands as the terminal client, plus `/help`. Listing commands answer without calling the model. Prompt names, resource URIs and argument counts are checked against what the servers actually offer, both in the browser and on the server.

### Bring your own OpenAI key

| Guarantee | How it's enforced |
|---|---|
| Kept only in the visitor's tab | `sessionStorage`, which is cleared when the tab closes. **Forget key** removes it immediately |
| Used only for that visitor's requests | Sent in an `X-OpenAI-Key` header over HTTPS, used to build a model client for one request, then discarded |
| Never saved or logged | Never written to disk or put in a URL. Access logs record only method, path and status |
| Never given to the MCP servers | Their environment is built explicitly without it (tested) |
| Not exposed in errors | Key-shaped text is redacted to `sk-***` |
| Not kept if pasted into the chat | Redacted before it reaches conversation memory or the model (tested) |

### Isolation and abuse protection

- **Sessions:** each visitor gets a private temporary task file and conversation memory, deleted after 3 hours of inactivity or on **New chat**.
- **Rate limits:** 10 requests per minute and 60 per hour per IP, which also protects the OpenWeather quota.
- **Input limits:** messages up to 1,000 characters, a 90 s timeout per turn, and one turn at a time per visitor.
- **XSS:** model output is HTML-escaped, and a strict Content-Security-Policy (no inline scripts) is a second line of defense against prompt-injected HTML.

### Settings

| Variable | Default |
|---|---|
| `OPENWEATHERMAP_API_KEY` | required |
| `MODEL` | `gpt-4o-mini` |
| `WEATHER_CACHE_TTL_S` | `300` (`0` disables the cache) |
| `WEATHER_CACHE_DIR` | a folder in the system temp directory |
| `SESSION_TTL_HOURS` | `3` |
| `RATE_PER_MINUTE` / `RATE_PER_HOUR` | `10` / `60` |
| `REPO_URL` | shows a "source code" link |
| `TASKS_FILE` | `tasks.txt` (terminal client) |

## MCP servers

### Weather server ([`weather_server.py`](../weather_server.py))

- **`get_weather(location)`** (read-only): current conditions, temperature, feels-like, humidity and wind.
- **`get_forecast(location, from_date=None, to_date=None)`** (read-only): turns OpenWeather's 3-hour forecast into one summary per *local* day, with weekday, low and high, every condition seen that day, chance of rain, rain and snow totals, and max wind.
  - Requests can reach 14 days ahead. Past dates, reversed ranges and dates further out return clear errors.
  - The free forecast covers about 5 days. Later dates are returned in `unavailable_dates`, with their weekdays.
- **`compare_weather_prompt(location_a, location_b)`** and the **`file://delivery_log`** resource.
- **5-minute disk cache.** The MCP client starts a new server process for every tool call, so an in-memory cache would never be hit. Errors are never cached.
- **HTTPS with a 10 s timeout.** Error messages never include the request URL, which contains the API key.

### Task server ([`task_server.py`](../task_server.py))

- **`add_task(task_description)`**, and **`list_tasks()`** (read-only).
- **`clear_tasks()`**: empties the whole list.
- **`update_task(task, new_description)`**: replaces one task in place. `task` is its number (counting from 1) or its full current text, case-insensitive. Unknown or ambiguous targets return an error with the numbered list, so the model can retry.
- **`plan_trip_prompt(destination, duration_in_days)`**: the trip starts tomorrow. The prompt checks the forecast using the *weather* server's tool, puts outdoor activities on dry days and indoor ones on days with a 50% or higher chance of rain, and saves each activity as a task.
- **`plan_trip_for_future_date(destination, from_date, to_date)`**: plans exact dates up to 14 days ahead.
  - It validates the dates and lists each trip date with its weekday, because models often work out weekdays wrong.
  - It saves tasks such as `2026-12-05 (Sat, snow likely): Visit the Musée national des beaux-arts du Québec`.
- **`file://meeting_notes`** resource.

## Troubleshooting

- **Authentication errors:** check the keys in `.env`. A new OpenWeather key may not be active yet.
- **City not found:** add a country code, e.g. `Victoria,ca`.
- **Detailed logs:** `$env:LOG_LEVEL="INFO"` shows the OpenAI requests, and `$env:MCP_LOG_LEVEL="INFO"` shows the MCP requests the servers handle.
