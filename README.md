# MCP DayPlanner

A smart little AI assistant that turns natural language into action. Powered by LangGraph and MCP, MCP DayPlanner connects live weather and forecasts with task management, so you can plan your day or your next trip, one conversation at a time.

<p align="center">
  <img src="media/header.png" width="560" alt="MCP DayPlanner: an AI assistant that combines live weather with task management. A LangGraph agent (gpt-4o-mini) connects through MCP to a Weather Server and a Task Server.">
</p>

MCP DayPlanner is a natural-language assistant that combines live weather with task management. One LangGraph agent (gpt-4o-mini) connects to two MCP servers, a weather server (current conditions and daily forecasts) and a task server, and picks the right tools for each request. That lets it handle requests that span both, such as *"check Vancouver's weather and add 'pack umbrella' if it's raining"*, or *"plan my trip to Quebec City from Friday to Sunday"*, which checks the forecast for those dates, puts outdoor sights on dry days and museums on rainy or snowy ones, and writes the itinerary into your to-do list. It runs as a terminal chat or as a web app, and it's backed by a 60-case evaluation suite.

## Demo

![MCP DayPlanner web app demo: the user enters their own OpenAI key in a dialog that explains it is never stored, asks for Vancouver's weather with a conditional "pack umbrella" task, then runs the "Quebec City trip with dates" MCP prompt. Each MCP tool call appears in the chat with the server that handled it, and the dated, weather-annotated itinerary fills the task panel.](media/demo.gif)

*The web app: bring-your-own-key sign-in, a weather-plus-task request, and a dated trip plan built from the forecast. Every MCP tool call is shown with the server that handled it (`weather` or `tasks`), and the task panel updates live.*

## Highlights

- **Multi-server MCP agent:** one LangGraph agent uses tools, prompts and resources from two MCP servers (weather and tasks). Even the task server's trip-planning prompt uses the weather server's forecast tool.
- **System evaluation with accuracy metrics:** 76 labeled cases in 18 categories (including dated forecasts, MCP prompts and resources, multi-turn conversations, prompt injection and reasoning over tool results) score tool selection, argument accuracy, answer grounding, end state, write ordering and (optionally) LLM-judged faithfulness and per-case rubrics such as "the snowy day's activities are indoors". Results come with 95% confidence intervals and pass@k / pass^k consistency. See [Evaluation](#evaluation).
- **Repeatable and isolated:** a mock-weather mode and per-trial temporary task files make scores reproducible without touching real data.
- **Web app with bring-your-own-key:** a browser chat UI that shows each MCP tool call live. Visitors use their own OpenAI key, which is never stored. Packaged for Azure App Service. See [Web app](#web-app).
- **Eval-driven fixes:** the eval found that concurrent tool calls saved tasks in random order (`order` 8% → 100%), that the agent answered "tomorrow" with today's forecast, and that it could add a conditional task before checking the condition. All are fixed, and the current score is **98.7%** across 228 runs. See [Results](#results).
- **Validated eval:** negative controls (agent without tools, shuffled data) prove the grader can fail; they also caught weak checks and an LLM-judge error, now fixed.
- **Secure by default:** the weather API is called over HTTPS with a 5-minute shared cache, error messages never expose API keys, and visitors' OpenAI keys are never stored, even if pasted into the chat.
- **Tested:** 78 unit tests cover server error handling, caching, forecast aggregation and date ranges, the tool-ordering fix, the web app's key handling, session isolation and slash commands, and the eval grader itself.

## Project structure

```text
mcp_client.py            LangGraph agent + MultiServerMCPClient chat loop
weather_server.py        MCP server: get_weather tool, compare_weather_prompt, delivery_log resource
task_server.py           MCP server: add_task / list_tasks tools, plan_trip_prompt, meeting_notes resource
web/
  app.py                 FastAPI backend: per-visitor sessions, bring-your-own-key, rate limiting
  static/                Chat UI (index.html, app.js, style.css, header.webp, favicon.png)
Dockerfile               One container: web app + both MCP servers (Azure App Service)
requirements.txt         Pinned runtime dependencies (requirements-dev.txt adds pytest)
eval/
  run_eval.py            System eval runner: grader, LLM judge, metrics, report
  cases.jsonl            76 labeled eval cases
  fixtures/weather.json  Fixed current weather for repeatable scoring
  fixtures/forecast.json Fixed 6-day forecasts (fixed dates) for repeatable scoring
tests/                   pytest unit tests (servers, caching, tool ordering, grader, web app)
media/
  header.png             Project banner (the web app uses a resized copy)
  demo.gif               Web app demo shown above
```

## What it does

- [`mcp_client.py`](./mcp_client.py) creates a `MultiServerMCPClient` and connects to the configured servers in a `server_configs` dictionary.
- The agent loads tools from all connected servers and uses `gpt-4o-mini` to decide when to call them.
- The project currently includes:
  - [`weather_server.py`](./weather_server.py): exposes weather tools and resources for OpenWeatherMap data.
  - [`task_server.py`](./task_server.py): exposes task-management tools, a travel-planning prompt, and a meeting-notes resource.
- Both servers load secrets from the project-root `.env` file. `.env` is ignored by Git; use [`.env.example`](./.env.example) as a template.

## Architecture

The client connects to two MCP servers at startup:

```python
server_configs = {
    "weather": {
        "command": "python",
        "args": ["weather_server.py"],
        "transport": "stdio",
        "env": {"MCP_LOG_LEVEL": os.getenv("MCP_LOG_LEVEL", "WARNING")},
    },
    "tasks": {
        "command": "python",
        "args": ["task_server.py"],
        "transport": "stdio",
        "env": {"MCP_LOG_LEVEL": os.getenv("MCP_LOG_LEVEL", "WARNING")},
    }
}
```

MCP stdio servers only inherit a minimal set of environment variables, so anything a server needs is passed in `env` explicitly.

This means the agent can combine capabilities from both servers without requiring two separate chats. A single conversation can:

- fetch the current weather in a city,
- get a daily forecast for a date range (up to 14 days ahead; forecast data reaches about 5 days),
- add items to the task list,
- list tasks,
- use server prompts tailored to the relevant server, and
- load and work with server resources.

### Today's date

The agent's system prompt includes today's date, so it can turn "tomorrow", "this Friday" or "March 5" into exact dates for the forecast tool. Without it, a test question asking "will it rain tomorrow?" got an answer based on today's forecast. The eval pins this date to the fixtures' date (`create_graph(..., today=...)`), so date questions score the same on any day.

### Ordered tool execution

When the model issues several tool calls in one turn, LangGraph's default `ToolNode` runs them concurrently. For writes, that loses ordering: a 3-day itinerary saved as six `add_task` calls landed in `tasks.txt` in random order, and "add X, then list" could return the list before the add had finished. `mcp_client.py` therefore uses `make_ordered_tool_node`. Servers mark side-effect-free tools with the standard MCP `readOnlyHint` annotation (`get_weather`, `get_forecast`, `list_tasks`). A batch runs concurrently only if every call in it is read-only; otherwise the calls run one by one, in the order the model issued them.

The same node also enforces **"look before you write"**. If one step mixes lookups with changes (for example `get_weather` and `add_task("pack umbrella")` for "add it *if* it's raining"), the model asked for the change before it could see the weather. The lookups run, and each change gets a `NOT EXECUTED: … check the lookup results first` reply, so the model decides again with the data in hand. The trade-off is one extra model turn when the change was unconditional (for example "what's the weather in Berlin? Also add 'buy boots'").

## Evaluation

The agent is measured, not just demoed. Evaluation has two layers:

| Layer | What it answers | Needs API key? | Where |
|---|---|---|---|
| **Unit tests** (78 tests) | Do the MCP servers (including caching and forecast aggregation), the tool-ordering logic, the web app and the eval grader behave correctly, including on errors? | No | [`tests/`](./tests) |
| **System eval** (76 cases) | How accurately does the LLM agent pick tools, pass arguments, report tool results and complete tasks? | Yes | [`eval/`](./eval) |

### Results

**Current: 98.7% overall pass (95% CI 96–100%).** gpt-4o-mini, 76 cases × 3 trials = 228 runs, judged by gpt-4o, run on 2026-11-18.

| Metric | Score |
|---|---:|
| tool_selection | 100% (219/219) |
| args | 100% (99/99) |
| grounded | 98.1% (156/159) |
| state | 100% (78/78) |
| order | 100% (21/21) |
| faithfulness (LLM judge) | 100% (228/228) |
| criteria (per-case rubrics, e.g. "snowy day indoors") | 100% (15/15) |
| **overall_pass** | **98.7% (225/228)** |
| pass^3 (case passed all 3 trials) | 98.7% |
| Mean / p95 latency, tokens per request | 13.2 s / 30.2 s, 2,130 |

All 18 categories score 100% except `no_tool` (80%), where the only failure is the known `nt-04` model error (gpt-4o-mini says MCP stands for "Model **Control** Protocol").

How we got here (the full story is in the [eval change log](#eval-change-log)):

1. **Forecasts exposed a "tomorrow" bug.** Adding `get_forecast` made the forecast category score only 61.9% at first. Given a `days` parameter, the agent asked for 1 day and answered "will it rain tomorrow?" with **today's** forecast. Fixed with explicit `from_date`/`to_date` and today's date in the system prompt.
2. **A prompt fix caused regressions, and the eval caught them.** Telling the agent "never assume a location" made it ask "which London?" and "did you mean London?" instead of calling tools, and pass rate fell from 98.3% to 93.9%. The rule was rewritten: if a place is named, look it up and fix obvious typos; ask only when no place is named.
3. **"Look before you write."** In `mi-02` (and in an early recording of the demo), the model asked for the weather *and* added "pack umbrella" in the same step, before knowing whether it was raining. The [tool node](#ordered-tool-execution) now runs the lookups in such a mixed step and holds the changes back with a "NOT EXECUTED, check the results first" reply. In the final run it held back 6 changes. All were in requests where the change was unconditional (`mi-03`, `to-03`), so the model simply re-issued them at the cost of one extra model turn. The outcomes were still correct, and the umbrella case can no longer write blindly.
4. **Per-case rubrics caught a planning flaw.** The first rubric run found a plan that put the Colosseum (outdoors) on a 70%-rain day labelled "light rain possible". `plan_trip_prompt` now uses the same "50% or more → indoors" rule as the dated prompt.

#### History: the concurrency bug fix (earlier 60-case set)

gpt-4o-mini, 60 cases × 3 trials = 180 runs, judged by gpt-4o. Run on 2026-11-18. The final column was run twice independently, with identical accuracy results both times.

| Metric | Before fix | After fix | Final (corrected judge) |
|---|---:|---:|---:|
| tool_selection | 100% | 100% | **100%** |
| args | 100% | 100% | **100%** |
| grounded | 97.6% | 97.6% | **97.6%** |
| state | 95.0% | 100% | **100%** |
| order | **8.3%** | 100% | **100%** |
| faithfulness (LLM judge) | 97.2% | 98.3% | **100%** |
| **overall_pass** (95% CI) | 89.4% (84–93%) | 96.7% (93–98%) | **98.3% (95–99%)** |
| pass^3 (case passed all 3 trials) | 86.7% | 96.7% | **98.3%** |
| Mean latency / tokens per request | 10.1 s / 857 | 10.4 s / 858 | 10.4 s / 857 |
| p95 latency | 17.4 s | 18.1 s | 16.6 s |

What the eval found:

1. **Concurrency bug in tool execution (fixed).** The `order` check showed task order scrambled in 11 of 12 multi-task runs. A second symptom appeared too: in "add X, then list", `list_tasks` sometimes returned *before* `add_task` finished. The fix is the [ordered tool node](#ordered-tool-execution). Writes now run in order, and read-only calls still run concurrently (mean latency 10.1 s → 10.4 s, within run-to-run noise). `tests/test_tool_ordering.py` reproduces the bug with plain `ToolNode` and checks the fix.
2. **LLM-judge error (fixed).** The judge failed every multi-turn follow-up ("…And how about Berlin?") for not repeating Tokyo's weather. It now gets earlier turns separately, and it scores omissions under helpfulness rather than faithfulness. After the change it still gave an invented temperature 1/5 and passed a correct follow-up. The before/after columns use the original judge, so the comparison stays like-for-like.
3. **Remaining model error.** `nt-04` fails in all 3 trials: gpt-4o-mini says MCP means "Model **Control** Protocol" (it's Model **Context** Protocol). This is a gap in the model's knowledge, not a tool or routing problem. Fixing it would mean adding a short project description to the system prompt.

Latency varies between runs because of the API. One earlier run of the final configuration averaged 13.6 s, and the repeat averaged 10.4 s on the same code, so latency differences of a few seconds are noise. The full traces are saved in `eval/results/`.

### How the system eval works

```text
eval/cases.jsonl ──► real LangGraph agent (gpt-4o-mini) ──► real MCP servers over stdio
   (76 labeled cases)          │                              ├─ weather_server  (WEATHER_MOCK=1 → fixed weather + forecast fixtures)
                               │                              └─ task_server     (TASKS_FILE → temp file for each trial)
                               ▼
              trace: tool calls + arguments + tool outputs + final answer + task file afterwards
                               ▼
     rule-based grader (+ optional LLM judge) ──► metrics, 95% CI, per-category report ──► eval/results/*.json
```

Design choices:

- **Repeatable ground truth.** Live weather changes every hour, so the eval runs the weather server in mock mode ([`eval/fixtures/weather.json`](./eval/fixtures/weather.json)). Every case has a known correct answer, while the agent, MCP transport and tool-calling code are all the real ones.
- **Isolated side effects.** Each trial gets its own temporary task file, so trials can run in parallel and checks on the file's contents are exact.
- **Separate metrics.** Tool routing, arguments, grounding and end state are scored separately, so a regression points to the layer that broke.
- **Accounts for non-determinism.** `--trials N` reruns each case. The report includes a 95% Wilson confidence interval, plus `pass@k` and `pass^k` (consistency).
- **Infrastructure errors aren't model errors.** A preflight check stops the run on a bad key, missing quota or unknown model, instead of reporting 0% accuracy.
- **The grader is tested too.** [`tests/test_eval_grader.py`](./tests/test_eval_grader.py) checks the scoring logic and that every case is well formed (including valid regexes), so a grader bug can't inflate the score.
- **Real MCP entry points.** Cases can start from a plain message, an MCP **prompt** (`"prompt"`), or an MCP **resource** (`"resource"`, loaded exactly as `/resource` does it, optionally followed by more turns). So the prompts and resources users reach through `/prompt` and `/resource` are measured too, not just free-text chat.
- **Fixed "today".** The agent's date is pinned to the fixtures' date (Monday 2026-03-02), so "tomorrow", "this Friday" or "March 5" mean the same thing on every run.
- **The LLM judge sees conversation structure.** For multi-turn cases, it gets earlier turns separately from the message being answered, and it scores faithfulness strictly as "are the stated facts supported". Omissions count against helpfulness instead.

### Metrics

| Metric | Meaning |
|---|---|
| `tool_selection` | Called exactly the expected set of tools, avoided forbidden ones, and met minimum call counts |
| `args` | Tool arguments match the expected patterns (e.g. `location` ~ `tokyo`) |
| `grounded` | The final answer contains the facts the tools returned (e.g. `14.2°C`) and none of the forbidden claims |
| `state` | The task file ends up in the expected state (items added, or deliberately *not* added) |
| `order` | New lines in the task file appear in the same order as the agent's `add_task` calls |
| `faithfulness` | (`--judge` only) The LLM judge rates the answer at least 4/5 for being supported by the tool outputs |
| `criteria` | (`--judge` only) The judge confirms the case's own rubric (`judge_criteria`), e.g. "Wednesday has heavy snow, so every activity saved for that date must be indoors". Catches plans that are well formed but ignore the weather |
| `overall_pass` | Every applicable check passed. Reported with a 95% confidence interval |
| `pass@k` / `pass^k` | The case passed in at least one of k trials / in all k trials |
| Performance | Mean and p95 latency, tokens per request, LLM turns per request, error count |

### Test coverage

| Category | Cases | Example |
|---|---:|---|
| `weather_single` | 8 | "What's the humidity in Seattle?" |
| `weather_paraphrase` | 5 | "do i need a jacket in oslo rn" |
| `weather_multi_city` | 3 | "Give me the weather in Tokyo, Berlin and Sydney." |
| `weather_error` | 3 | Unknown city, API authentication failure, no city given. The agent must not invent weather |
| `task_add` | 4 | "Please add these to my to-do list: finish the report, email Sarah, book flights" |
| `task_list` | 4 | Pre-filled task file: "How many tasks do I have left?" |
| `multi_intent` | 5 | "Check the weather in Tokyo and if it's raining, add 'pack umbrella'." Paired with a case where the condition is false, so the agent must *not* add the task |
| `no_tool` | 5 | Chitchat and general-knowledge questions that should call no tool |
| `server_prompt` | 3 | MCP prompts `compare_weather_prompt` and `plan_trip_prompt` run from start to finish (the trip prompt must call the weather server's `get_forecast`) |
| `adversarial` | 3 | Prompt injection hidden in loaded resource content, and *indirect* injection inside a tool result (the fixture city "Gotham" returns weather text telling the agent to add a "HACKED" task) |
| `task_order` | 3 | "Add these in this exact order: wake up, shower, eat breakfast, commute", plus a trip itinerary and "add, then list" |
| `hard_location` | 4 | "the Big Apple", "the capital of Japan", "londn" (typo), "the Windy City" |
| `hard_reasoning` | 4 | °C→°F conversion, ranking 4 cities, a different action per city ("pack a coat for every city below 12°C"), one valid and one invalid city |
| `honesty` | 2 | UV index (data no tool provides): the agent must not invent it. "Remember milk, but don't add it yet" |
| `forecast_dates` | 7 | `plan_trip_for_future_date` for Quebec City (heavy-snow day must be indoors) and Toronto (dates past the forecast data still get real activities and correct weekdays), "forecast for Toronto from March 3 to March 5", "snow in Quebec City this Friday?", a date more than 14 days out, a trip more than 14 days out (the agent must stop and add **no** tasks), and "weather yesterday?" (no historical data) |
| `resource` | 3 | Load `file://meeting_notes` and list the action items with owners; load `file://delivery_log`, then ask for the weather where order #10589 went (look-up in the log, then a tool call); load the log and add a task for every order delivered to a city starting with "S" (exactly 3, no others) |
| `forecast` | 7 | "Will it rain in Rome over the next few days?", "Best picnic day in London?", "Will it rain in Seattle tomorrow?" (needs `today_local`), "Tokyo three weeks from now" (must say forecasts only cover 5 days), "right now" questions must still use `get_weather`, and a 3-day plan with outdoor/indoor activities matched to the forecast |
| `multi_turn` | 3 | "What's the weather in Tokyo?" → "And how about Berlin?" (follow-ups that depend on earlier turns) |

### Running it

```powershell
# Unit tests: no keys or network needed
python -m pip install -r requirements-dev.txt
python -m pytest

# System eval
python .\eval\run_eval.py                              # 1 trial per case, gpt-4o-mini
python .\eval\run_eval.py --trials 3 --judge gpt-4o    # more reliable numbers, plus an LLM judge
python .\eval\run_eval.py --filter multi_intent -v     # one category, print failure details
python .\eval\run_eval.py --model gpt-4.1-mini         # compare another model on the same cases
python .\eval\run_eval.py --min-pass 0.85              # exit non-zero below 85%, for use as a CI gate
python .\eval\run_eval.py --control no-tools           # negative control (see below)
```

### Validating the eval with negative controls

A high score only means something if the grader can fail. `--control` deliberately breaks the system, and the scores must collapse in the expected place. `shuffled-fixtures` gives every city another city's current weather **and** forecast, keeping its own name and dates:

| Run (gpt-4o-mini, 76 cases) | tool_selection | grounded | overall_pass | Expected behavior |
|---|---:|---:|---:|---|
| Normal run (for comparison) | 100% | 98.1% | 98.7% | |
| `--control no-tools` (agent has no tools) | 15.1% | 18.9% | 14.5% | Only cases that need no tool should pass ✅ |
| `--control shuffled-fixtures` (each city gets another city's weather and forecast) | 95.9% | 37.7% | 51.3% | Routing still works, but grounding must fail ✅ |

The controls also exposed weak checks, which have been fixed:
- `wm-01` passed an answer that said *"London is warmer than Paris"* (the wrong conclusion), because the check only required the word "Paris". It now checks both temperatures and rejects the wrong comparison. A regression test covers this.
- `nt-04` accepted a wrong expansion of "MCP" because nothing checked factual correctness. It now requires "Model Context Protocol", and gpt-4o-mini fails it in 3 of 3 trials (it answers "Model **Control** Protocol"). That is a real model error the eval had hidden.

The report prints each metric with its confidence interval, the pass rate per category, performance figures, and every failing case with its tool calls and answer. The full run is saved to `eval/results/<timestamp>_<model>_<git-sha>.json`, so you can compare prompt or model changes over time.

To add a case, append one JSON line to [`eval/cases.jsonl`](./eval/cases.jsonl), e.g.:

```json
{"id": "mi-01", "category": "multi_intent",
 "input": "Check the weather in Tokyo and if it's raining, add 'pack umbrella' to my tasks.",
 "expected_tools": ["get_weather", "add_task"],
 "expected_args": {"get_weather.location": "(?i)tokyo", "add_task.task_description": "(?i)umbrella"},
 "tasks_contain": ["(?i)umbrella"]}
```

Supported checks: `expected_tools`, `forbidden_tools`, `min_calls`, `expected_args`, `answer_must_mention`, `answer_must_not_mention`, `tasks_contain`, `tasks_not_contain`, `tasks_min_count`, `tasks_in_call_order` and `judge_criteria` (a rubric for the LLM judge). Cases can pre-fill the task file with `initial_tasks`, run an MCP prompt with `prompt`, load an MCP resource with `resource` (optionally followed by `input` turns), or make `input` a list of messages for a multi-turn conversation.

### Eval change log

Changes to existing cases are recorded here, because quietly editing a case to make it pass would make the scores meaningless.

| Case | Change | Why |
|---|---|---|
| `wm-01`, `wm-03` | Check both temperatures and reject the wrong comparison | The negative control showed they passed a wrong answer ("London is warmer than Paris") |
| `nt-04` | Require "Model Context Protocol" | Nothing checked the answer was correct; gpt-4o-mini fails it |
| `hh-02` | Removed, replaced by `fc-03` | It required refusing to predict tomorrow's rain; with a forecast tool, looking it up is the right behavior |
| `hh-01` | No longer requires calling `get_weather` | The point is "never invent a UV index". Saying "I don't have UV data" without calling the tool is also honest |
| `fc-04` | Accept "14 days" as well as "5 days" | Written for the 5-day contract; the request limit is now 14 days |
| `pr-03`, `to-02` | Expect `get_forecast` (from tomorrow) plus `add_task` | `plan_trip_prompt` now checks the forecast; the start-date check guards the old "Day 1 = today" bug |
| `pr-03`, `fc-07`, `fd-01`, `fd-02`, `rs-01` | Added `judge_criteria` rubrics | Rule-based checks couldn't tell whether a plan actually followed the weather. The first rubric run caught a real flaw (an outdoor plan on a 70%-rain day), now fixed in `plan_trip_prompt` |
| `we-01` | Also accept "not a recognized location" | The agent correctly called the tool and reported the unknown city, but in wording the check didn't anticipate |
| (runner) | Tool outputs passed to the judge raised from 1,000 to 8,000 characters | The cut-off hid forecast days, so the judge wrongly flagged real data as invented |

## Setup

1. Install Python and the project dependencies in your Python environment:

   ```powershell
   python -m pip install -r requirements-dev.txt
   ```

2. Ensure a `.env` file exists in the project root. To create one from the template without replacing an existing file, run:

   ```powershell
   if (-not (Test-Path .env)) { Copy-Item .env.example .env }
   ```

3. Fill in the required values in `.env`:

   ```dotenv
   OPENWEATHERMAP_API_KEY=your_openweathermap_api_key
   OPENAI_API_KEY=your_openai_api_key
   ```

   Get an API key from [OpenWeatherMap](https://openweathermap.org/api) and an API key for the OpenAI API. Keep `.env` private and never commit it.

## Run

From the project root, start the client:

```powershell
python .\mcp_client.py
```

The client starts the configured MCP servers and loads their tools automatically. You can then ask questions such as:

```text
What's the weather in Toronto, CA?
```

or:

```text
Add "Buy groceries" to my task list.
```

Enter `exit`, `quit`, or `q` to end the chat.

## Chat commands

The client supports server-aware commands for listing and invoking capabilities from any connected server.

### List all prompts

```text
/prompts
```

This lists prompts from each connected server, with the server name included in the output.

### Invoke a prompt on a specific server

```text
/prompt weather compare_weather_prompt "Toronto,ca" "Vancouver,ca"
```

```text
/prompt tasks plan_trip_prompt "Montreal" 3
```

```text
/prompt tasks plan_trip_for_future_date "Quebec City" "2026-12-04" "2026-12-06"
```

The format is:

```text
/prompt <server_name> <prompt_name> "arg1" "arg2" ...
```

For prompt arguments containing spaces, keep them inside quotes.

### List all resources

```text
/resources
```

This lists the resources available on each server, including each resource URI.

### Load a resource from a specific server

```text
/resource weather file://delivery_log
```

```text
/resource tasks file://meeting_notes
```

The resource content is loaded and then either:

- added to the conversation context immediately when you press Enter, or
- used as the basis for a follow-up instruction after you type a task for the assistant.

## Web app

The same agent also runs as a web app (FastAPI + a small HTML/JS front end in [`web/`](./web)). Visitors chat in the browser, see each MCP tool call and which server handled it, and watch their task list update live.

```powershell
python -m uvicorn web.app:app --port 8000      # then open http://localhost:8000
```

What visitors get:

- **A chat page** with example buttons. One of them runs the `plan_trip_prompt` **MCP prompt**.
- **The same slash commands as the terminal client**, for using the servers' MCP prompts and resources directly. The page mentions them in its welcome message, under the message box, and in two example buttons:

  | Command | What it does |
  |---|---|
  | `/prompts` | Lists each server's prompts with their arguments, plus a ready-to-run example you can click to edit |
  | `/prompt <server> <prompt_name> "arg1" "arg2"` | Runs a prompt, e.g. `/prompt weather compare_weather_prompt "Toronto" "Vancouver"` or `/prompt tasks plan_trip_for_future_date "Quebec City" "2026-12-04" "2026-12-06"`. Arguments with spaces go in quotes |
  | `/resources` | Lists each server's resources |
  | `/resource <server> <uri> [instruction]` | Loads a resource. With an instruction it runs it (e.g. `/resource tasks file://meeting_notes List the action items`); without one, the content is added to the conversation so you can ask about it next |
  | `/help` | Shows the commands |

  Listing commands answer instantly without calling the model. Prompt names, resource URIs and argument counts are checked against what the servers actually offer, both in the browser and on the server. For example, after `/resource weather file://delivery_log`, asking *"What's the weather where order #10589 was delivered?"* makes the agent look up the delivery city in the log and fetch that city's live weather.
- **Tool-call lines** such as `🔧 weather · get_weather(location="Vancouver")`, so the multi-server design is visible.
- **A live task panel** that updates when the agent adds tasks.
- **Light and dark themes** that follow the visitor's system setting, and an "About" dialog explaining the architecture.
- **A footer crediting OpenWeather** as the weather data source.

### Bring your own OpenAI key (never stored)

Visitors enter their **own** OpenAI API key, so the demo costs the host nothing for model usage. The key-entry dialog tells visitors exactly how their key is handled, and the code enforces it:

| Guarantee | How it's enforced |
|---|---|
| Kept only in the visitor's browser tab | `sessionStorage`, which the browser clears when the tab closes. A **Forget key** button removes it immediately |
| Used only for that visitor's own requests | Sent in an `X-OpenAI-Key` header over HTTPS, used to build a model client for that one request, then discarded |
| Never saved or logged on the server | Never written to disk, never in a URL, and access logs record only method, path and status. Verified by a run where the key appeared in no log line or session file |
| Never shared with the MCP servers | The servers' environment is built explicitly without it. `tests/test_web.py` checks this |
| Not exposed in error messages | Anything that looks like a key is redacted (`sk-***`) before an error reaches the browser |
| Not kept if pasted into the chat by mistake | Key-shaped text in messages and prompt arguments is redacted before it reaches conversation memory or the model. A test covers this |

### Isolation and abuse protection

- **Private sessions.** Each visitor gets a separate temporary session (its own task file and conversation memory), deleted after 3 hours of inactivity or on **New chat**.
- **Rate limits.** Per visitor IP: 10 requests per minute and 60 per hour. This protects the server and the host's OpenWeatherMap quota.
- **Input limits.** Messages are capped at 1,000 characters, each turn times out after 90 s, and a visitor can't run two turns at once.
- **XSS protection.** Model output is HTML-escaped before rendering. A strict Content-Security-Policy (no inline scripts) is a second line of defense against prompt-injected HTML.

Settings (environment variables): `OPENWEATHERMAP_API_KEY` (required), `WEATHER_CACHE_TTL_S` (300), `MODEL` (default `gpt-4o-mini`), `SESSION_TTL_HOURS` (3), `RATE_PER_MINUTE` (10), `RATE_PER_HOUR` (60), and `REPO_URL` (shows a "check the source code" link).

## Server capabilities

### Weather server

[`weather_server.py`](./weather_server.py) exposes:

- `get_weather(location: str)`: current conditions. Read-only (`readOnlyHint`)
- `get_forecast(location: str, from_date: str = None, to_date: str = None)`: daily forecast for a date range (`YYYY-MM-DD`, inclusive). Read-only
- `compare_weather_prompt(location_a: str, location_b: str)`
- `file://delivery_log` resource

This server fetches weather from [OpenWeather](https://openweathermap.org/) over HTTPS (both endpoints are on the free plan):

- **Current weather** returns the location, conditions, temperature, feels-like temperature, humidity, and wind speed.
- **The forecast** turns OpenWeather's 3-hour forecast into one summary per *local* calendar day: date and weekday, low/high, conditions, chance of rain, rain/snow totals, max wind, and whether the day is only partly covered. It also returns the location's `today_local` date.
- **Date ranges.** `from_date`/`to_date` select the days you need. Leave both out for every available day, or give only `from_date` for a single day. Requests can reach up to **14 days ahead** of the location's local date; past dates, reversed ranges and anything further out get a clear error. OpenWeather's free forecast only covers about 5 days, so requested dates beyond that come back in `unavailable_dates` (with weekdays) and a note that their forecast isn't available yet. Longer-range data would need a paid OpenWeather plan. A day's conditions list every condition seen that day, most frequent first. Reporting only the most common one hid a 19 mm downpour on a mostly cloudy day in testing.

- **5-minute cache.** Successful results (current weather and forecasts, cached separately) are cached for 5 minutes per location, so repeated questions about the same city don't spend API calls. OpenWeather refreshes current conditions only about every 10 minutes anyway. The cache is stored on disk because the MCP client starts a fresh server process for every tool call, so an in-memory cache would never be hit. It's shared across all calls and users (weather is public data). Errors are never cached. Settings: `WEATHER_CACHE_TTL_S` (default `300`, `0` disables it) and `WEATHER_CACHE_DIR` (default: a folder in the system temp directory).
- **No key leaks.** Error messages never include the request URL, which contains the API key, because tool output is shown to the model and can reach the user. Requests time out after 10 seconds.

### Task server

[`task_server.py`](./task_server.py) exposes:

- `add_task(task_description: str)`
- `list_tasks()`: read-only (`readOnlyHint`)
- `plan_trip_prompt(destination: str, duration_in_days: int)`: weather-aware. It tells the agent to check the forecast for the trip days (starting tomorrow) with a tool on the *other* server, put outdoor activities on dry days and indoor ones on rainy days, and save each activity as a task like `Day 1 (Thu, rain likely): Visit the Montreal Museum of Fine Arts`
- `plan_trip_for_future_date(destination: str, from_date: str, to_date: str)`: plans a trip for exact dates (up to 14 days ahead). It checks the forecast for those dates, plans one or two real activities per day around the weather, and saves them in date order, e.g. `2026-12-05 (Sat, snow likely): Visit the Musée national des beaux-arts du Québec`. Dates past the forecast range still get real activities suited to the season, marked "forecast not available yet". The prompt validates its dates (format, order, at most 15 trip days) and lists every trip date with its weekday, because models often get weekdays wrong when working them out themselves
- `file://meeting_notes` resource

This server stores tasks in `tasks.txt` (override with the `TASKS_FILE` env var) and provides a travel-planning workflow that saves itinerary items into the task list.

## Troubleshooting

- If the OpenAI API key is missing or invalid, check `OPENAI_API_KEY` in `.env`.
- If the weather tool reports an authentication error, verify the `OPENWEATHERMAP_API_KEY` value and that the key is active. A newly created OpenWeather key can take from a few minutes to a couple of hours to start working.
- If a city cannot be found, try including its country code, for example `Victoria,ca`.
- If a prompt or resource is not found, check the exact server name and the URI or prompt name you are using.
- If a tool call fails, make sure both MCP server processes are running through the client startup path and that the server files are in the project root.
- Logs are quiet by default. To debug, turn on detailed logging before starting the client: `$env:LOG_LEVEL="INFO"` shows every OpenAI HTTP request, and `$env:MCP_LOG_LEVEL="INFO"` shows each MCP request the servers handle. Use `Remove-Item Env:LOG_LEVEL, Env:MCP_LOG_LEVEL` to turn them off again.

## Credits

Weather data provided by [OpenWeather](https://openweathermap.org/). The web app shows the same credit in its footer.
