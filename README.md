# MCP DayPlanner

A smart little AI assistant that turns natural language into action. Powered by LangGraph and MCP, MCP DayPlanner connects live weather and forecasts with task management, so you can plan your day or your next trip, one conversation at a time.

<p align="center">
  <img src="media/header.png" width="560" alt="MCP DayPlanner: a LangGraph agent (gpt-4o-mini) connected through MCP to a Weather Server and a Task Server.">
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white">
  <img alt="LangGraph" src="https://img.shields.io/badge/LangGraph-1.2-1C3C3C?logo=langchain&logoColor=white">
  <img alt="MCP" src="https://img.shields.io/badge/MCP-FastMCP%20%C2%B7%20stdio-000000?logo=modelcontextprotocol&logoColor=white">
  <img alt="OpenAI" src="https://img.shields.io/badge/OpenAI-GPT--4o--mini-412991">
  <img alt="FastAPI" src="https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="OpenWeather" src="https://img.shields.io/badge/Data-OpenWeather-EB6E4B">
  <br>
  <img alt="Docker" src="https://img.shields.io/badge/Docker-GHCR-2496ED?logo=docker&logoColor=white">
  <img alt="GitHub Actions" src="https://img.shields.io/badge/CI%2FCD-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white">
  <img alt="Azure" src="https://img.shields.io/badge/Deploy-Azure%20App%20Service-0078D4">
  <img alt="pytest" src="https://img.shields.io/badge/Tests-pytest%20%C2%B7%2078-0A9EDC?logo=pytest&logoColor=white">
  <a href="eval/README.md"><img alt="Eval" src="https://img.shields.io/badge/Eval-98.7%25%20pass%20%C2%B7%20228%20runs-2EA44F"></a>
  <a href="https://github.com/Jiawen-Wang06/MCP-DayPlanner/actions/workflows/docker-image.yml"><img alt="Build status" src="https://github.com/Jiawen-Wang06/MCP-DayPlanner/actions/workflows/docker-image.yml/badge.svg"></a>
</p>

<p align="center">
  <b><a href="https://mcpdayplanner-cefzefg8dkgfh4gp.canadaeast-01.azurewebsites.net">▶ Try it live</a></b> (bring your own OpenAI key, never stored)
</p>

One LangGraph agent works with **two MCP servers**, one for weather and one for tasks. It decides which tools to call for each request, such as *"if it's raining in Vancouver, add 'pack umbrella'"* or *"plan my Quebec City trip from Friday to Sunday"*. For the trip, it checks the forecast for those dates, schedules indoor activities on snowy days, and writes the itinerary to the task list.

| | |
|---|---|
| **Evaluation** | 76-case system eval, **98.7% pass** over 228 runs (95% CI 96–100%), validated with negative controls. [See below](#evaluation) |
| **Quality** | 78 unit tests; GitHub Actions runs the tests, builds the Docker image and publishes it to GHCR for Azure |
| **Security** | Visitors bring their own OpenAI key, which is never stored or logged. The OpenWeather key never appears in any output. Sessions are isolated and rate-limited |

![Web app demo: the user enters their own OpenAI key, asks for Vancouver's weather with a conditional task, then runs the dated Quebec City trip prompt. Each MCP tool call is shown with the server that handled it, and the itinerary fills the task panel.](media/demo.gif)

## Evaluation

An agent that works in a demo can still fail on real requests, so this one is measured. The [eval harness](eval/README.md) runs the **real agent against the real MCP servers** on 76 labeled cases in 18 categories. The categories include forecasts with relative dates, multi-turn follow-ups, MCP prompts and resources, prompt injection hidden in tool results, and requests where the right action is to do nothing.

**Result: 98.7% overall pass** (gpt-4o-mini, 3 trials per case, gpt-4o as judge).

| tool selection | arguments | grounded in tool output | final task state | write order | LLM-judge faithfulness | per-case rubrics |
|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| 100% | 100% | 98.1% | 100% | 100% | 100% | 100% |

The only failing case is a real gap in the model's knowledge: gpt-4o-mini says MCP means "Model *Control* Protocol".

**How the evaluation is made trustworthy:**

- **Reproducible.** Weather comes from fixtures, every trial uses its own temporary task file, and the agent's "today" is pinned, so "tomorrow" means the same date on every run.
- **Graded on more than the final text.** Tool choice, arguments, the facts in the answer, the task file after the run and write order are each checked. An LLM judge scores faithfulness and per-case rubrics (e.g. *"the snowy day's activities are indoors"*).
- **The grader is shown to fail.** With tools removed, the pass rate drops to 14.5%. With each city given another city's weather, "grounded" drops to 37.7%. These controls also caught a weak check that had passed a wrong answer.
- **Statistically honest.** Scores come with Wilson 95% confidence intervals and pass^k over repeated trials. Every change to an existing case is recorded in a [change log](eval/README.md#change-log), so cases aren't quietly edited to pass.

**What the eval caught and how each was fixed:**

| Finding | Impact | Fix |
|---|---|---|
| Parallel tool calls saved tasks in random order | `order` 8.3% → 100% | Ordered tool node ([below](#engineering-decisions)) |
| "Rain tomorrow?" was answered with today's forecast | forecast category 61.9% → 100% | Explicit date range, and today's date in the system prompt |
| A prompt fix made the agent ask "which London?" instead of looking it up | caught a regression, 98.3% → 93.9%, then fixed | Prompt rule rewritten |
| The agent added "pack umbrella" before checking for rain | blind conditional writes → 0 | "Look before you write" guard |
| A rubric found an outdoor plan for a 70%-rain day | criteria → 100% | Trip prompt rule: 50% or more chance of rain → indoors |

```powershell
python -m pytest                                      # 78 unit tests, no keys needed
python .\eval\run_eval.py --trials 3 --judge gpt-4o   # system eval
python .\eval\run_eval.py --min-pass 0.85             # use as a CI gate
```

The [full methodology](eval/README.md) covers the metric definitions, all cases, the controls, the history and the change log.

## Architecture

```text
Browser / terminal ──► LangGraph agent (gpt-4o-mini, MemorySaver)
                            │  MultiServerMCPClient (stdio)
                ┌───────────┴────────────┐
          weather_server            task_server
  get_weather, get_forecast       add_task, list_tasks
  compare_weather_prompt          plan_trip_prompt, plan_trip_for_future_date
  file://delivery_log             file://meeting_notes
          │
   OpenWeather API (HTTPS, 5-min cache)
```

The task server's trip prompts use the weather server's forecast tool, so a single request uses both servers.

### Engineering decisions

- **Ordered tool execution.** LangGraph's `ToolNode` runs all tool calls in a step concurrently, which scrambled the order of writes. A custom node runs read-only calls (marked with MCP `readOnlyHint`) in parallel and runs writes in order. In a mixed step, it runs the reads and holds the writes back, so the model sees the result before acting on it.
- **The cache lives on disk, not in memory.** The MCP client starts a new server process for every tool call, so an in-memory cache would never be hit. Errors aren't cached.
- **The code supplies the dates.** The forecast is grouped by each location's *local* day, and weekdays are filled in by code, because models often get weekdays wrong. Each day lists every condition, because showing only the most common one hid a 19 mm downpour.
- **Secrets are kept out of the model's context.** Tool errors never include request URLs, key-shaped text is redacted before it reaches the model or memory, and the MCP servers' environment is built explicitly, without the visitor's key.

## Quick start

```powershell
python -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }   # add OPENWEATHERMAP_API_KEY and OPENAI_API_KEY
python .\mcp_client.py                            # terminal chat
python -m uvicorn web.app:app --port 8000         # web app at http://localhost:8000
```

Both clients support `/prompts`, `/prompt <server> <name> "args"`, `/resources` and `/resource <server> <uri>`. The [reference](docs/reference.md) covers the commands, server APIs, web app security, settings and troubleshooting.

```text
mcp_client.py         LangGraph agent, ordered tool node, terminal chat
weather_server.py     MCP server: current weather and dated forecasts
task_server.py        MCP server: tasks and weather-aware trip prompts
web/                  FastAPI app and chat UI (bring your own key)
eval/                 System eval: runner, 76 cases, fixtures, methodology
tests/                78 unit tests
Dockerfile            Single container deployed to Azure App Service
.github/workflows/    CI: test → build → push to GHCR → deploy
```

## Credits

Weather data provided by [OpenWeather](https://openweathermap.org/).
