<p align="center">
  <img src="media/header.png" width="80%" alt="MCP DayPlanner: a LangGraph agent (gpt-4o-mini) connected through MCP to a Weather Server and a Task Server.">
</p>

| | |
| --- | --- |
| **Agent** | ![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white) ![LangGraph](https://img.shields.io/badge/LangGraph-1.2-1C3C3C?logo=langchain&logoColor=white) ![MCP](https://img.shields.io/badge/MCP-FastMCP%20%C2%B7%20stdio-000000?logo=modelcontextprotocol&logoColor=white) ![LangChain MCP adapters](https://img.shields.io/badge/LangChain-MCP%20adapters-1C3C3C?logo=langchain&logoColor=white) |
| **LLM** | ![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4o--mini-412991?logo=openai&logoColor=white) ![Judge](https://img.shields.io/badge/LLM%20judge-GPT--4o-412991?logo=openai&logoColor=white) |
| **App** | ![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white) ![OpenWeather](https://img.shields.io/badge/Data-OpenWeather-EB6E4B) |
| **Quality** | [![Eval](https://img.shields.io/badge/Eval-98.7%25%20pass%20%C2%B7%20228%20runs-2EA44F)](eval/README.md) ![pytest](https://img.shields.io/badge/pytest-88%20tests-0A9EDC?logo=pytest&logoColor=white) [![Build status](https://github.com/Jiawen-Wang06/MCP-DayPlanner/actions/workflows/docker-image.yml/badge.svg)](https://github.com/Jiawen-Wang06/MCP-DayPlanner/actions/workflows/docker-image.yml) |
| **Delivery** | ![Docker](https://img.shields.io/badge/Docker-GHCR-2496ED?logo=docker&logoColor=white) ![GitHub Actions](https://img.shields.io/badge/CI%2FCD-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white) ![Azure App Service](https://img.shields.io/badge/Azure-App%20Service-0078D4) |

**An AI agent that plans your day from plain English.** One LangGraph agent uses **two MCP servers**, one for live weather and forecasts and one for tasks. It decides which tools to call for requests like *"if it's raining in Vancouver, add 'pack umbrella'"* or *"plan my Quebec City trip from Friday to Sunday"*. For the trip, it checks the forecast for those dates, moves activities indoors on rainy or snowy days, and writes the itinerary to your task list.

**[▶ Live demo](https://mcpdayplanner-cefzefg8dkgfh4gp.canadaeast-01.azurewebsites.net)**: bring your own OpenAI key (never stored or logged). The first request after a quiet period may take a few seconds.

![Web app demo: the user enters their own OpenAI key, asks for Vancouver's weather with a conditional task, then runs the dated Quebec City trip prompt. Each MCP tool call is shown with the server that handled it, and the itinerary fills the task panel.](media/demo.gif)

## Highlights

- **Multi-server agent.** Turned plain-English requests into correct tool use across two services, with **100% tool selection and 100% argument accuracy** in the system eval, by building a **LangGraph** agent (**gpt-4o-mini**) that connects through **LangChain MCP adapters** over stdio to two **FastMCP** servers exposing 6 tools, 3 prompts and 2 resources.
- **Measurable reliability.** Reached a **98.7% pass rate over 228 runs** (76 test cases × 3 trials each; 95% confidence interval 96.2–99.5%), by building a **76-case evaluation harness** that runs the real agent against the real MCP servers with pinned fixtures. It grades tool choice, arguments, grounding, final task state and write order, and uses **gpt-4o as an LLM judge** for faithfulness and per-case rubrics.
- **An eval that is shown to catch failures.** Found and fixed **5 real defects** (for example, forecast questions went from 61.9% to 100%) and proved the grader can fail (pass rate drops to **14.5%** with tools removed, grounding to **37.7%** with shuffled weather), by adding **negative controls**, Wilson confidence intervals, pass^3 (the share of test cases that pass on all 3 trials, which measures consistency), and a change log for every edit to a case.
- **Correct ordering without losing speed.** Raised task write order from **8.3% to 100%** with no measurable latency cost, by replacing LangGraph's default `ToolNode` with a **custom tool node** that runs read-only calls (MCP `readOnlyHint`) in parallel, writes in order, and holds writes back until lookups return ("look before you write").
- **Weather-aware planning the model can't get wrong.** Brought the forecast category from **61.9% to 100%** and every trip rubric to 100%, by having code group the **OpenWeather** forecast by each location's local day, compute dates and weekdays, and pass explicit date ranges, instead of trusting the model with calendar math.
- **A public demo that is safe to run.** Kept every API key out of storage, logs and the model's context, by building a **FastAPI** web app where visitors bring their own OpenAI key, with isolated per-session task files, key redaction, input limits, and per-IP rate limiting (10/minute, 60/hour).
- **Automated delivery.** Shipped every merge to `master` to production behind a gate of **88 unit tests**, by building a **GitHub Actions** pipeline that runs the tests, builds a **Docker** image, pushes it to **GHCR** tagged with the commit SHA, and deploys that exact image to **Azure App Service**.

## Evaluation

An agent that works in a demo can still fail on real requests. The [eval harness](eval/README.md) runs **the real agent against the real MCP servers** on 76 labeled cases in 18 categories. They include relative-date forecasts, multi-turn follow-ups, MCP prompts and resources, prompt injection hidden in tool results, and requests where the right action is to do nothing.

**98.7% overall pass** (gpt-4o-mini, 3 trials per case, gpt-4o as judge):

| tool selection | arguments | grounded in tool output | final task state | write order | LLM-judge faithfulness | per-case rubrics |
|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| 100% | 100% | 98.1% | 100% | 100% | 100% | 100% |

The only failing case is a real gap in the model's knowledge: gpt-4o-mini says MCP means "Model *Control* Protocol".

**Why the numbers can be trusted:**
- **Reproducible.** Weather comes from fixtures, each trial has its own task file, and "today" is pinned, so "tomorrow" means the same date on every run.
- **Graded on behaviour, not just the final text.** Tool choice, arguments, facts in the answer, the task file after the run and write order are each checked, plus LLM-judged faithfulness and per-case rubrics.
- **The grader is shown to fail.** The negative controls above also caught a weak check that had passed a wrong answer.
- **Statistically honest.** Results come with Wilson 95% confidence intervals, which give the likely range of the true pass rate, and pass^3, the share of test cases that pass on all 3 trials. An LLM can answer the same request differently each time, so pass^3 shows whether the agent is reliable, not just whether it can succeed. A [change log](eval/README.md#change-log) records every edit to an existing case.

**What the eval caught and how each was fixed:**

| Finding | Impact | Fix |
|---|---|---|
| Parallel tool calls saved tasks in random order | write order 8.3% → 100% | Ordered tool node ([below](#engineering-decisions)) |
| "Rain tomorrow?" was answered with today's forecast | forecast category 61.9% → 100% | Explicit date range; today's date in the system prompt |
| A prompt fix made the agent ask "which London?" instead of looking it up | regression caught, 98.3% → 93.9%, then fixed | Prompt rule rewritten |
| "Pack umbrella" was added before checking for rain | blind conditional writes → 0 | "Look before you write" guard |
| A rubric found an outdoor plan for a 70%-rain day | criteria → 100% | Trip rule: 50% or higher chance of rain → indoors |

```powershell
python -m pytest                                      # 88 unit tests, no keys needed
python .\eval\run_eval.py --trials 3 --judge gpt-4o   # system eval
python .\eval\run_eval.py --min-pass 0.85             # use as a CI gate
```

The [full methodology](eval/README.md) covers metric definitions, all cases, the controls, the history and the change log.

## Architecture

```text
Browser / terminal ──► LangGraph agent (gpt-4o-mini, MemorySaver)
                            │  MultiServerMCPClient (stdio)
                ┌───────────┴────────────┐
          weather_server            task_server
  get_weather, get_forecast       add_task, list_tasks,
                                  update_task, clear_tasks
  compare_weather_prompt          plan_trip_prompt, plan_trip_for_future_date
  file://delivery_log             file://meeting_notes
          │
   OpenWeather API (HTTPS, 5-min cache)
```

The task server's trip prompts use the weather server's forecast tool, so a single request crosses both servers.

### Engineering decisions

- **Ordered tool execution.** LangGraph's `ToolNode` runs all of a step's tool calls concurrently, which scrambled the order of writes. A custom node runs read-only calls (MCP `readOnlyHint`) in parallel and writes in order. In a mixed step it runs the reads first and holds the writes back, so the model sees the result before acting.
- **The cache lives on disk, not in memory.** The MCP client starts a new server process for each tool call, so an in-memory cache would never be hit. Errors aren't cached.
- **The code supplies the dates.** Forecasts are grouped by each location's *local* day, and weekdays are filled in by code, because models often get them wrong. Each day lists every condition, because showing only the most common one hid a 19 mm downpour.
- **Secrets stay out of the model's context.** Tool errors never include request URLs, key-shaped text is redacted before it reaches the model or memory, and the MCP servers' environment is built explicitly, without the visitor's key.

## Quick start

```powershell
python -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }   # add OPENWEATHERMAP_API_KEY and OPENAI_API_KEY
python .\mcp_client.py                            # terminal chat
python -m uvicorn web.app:app --port 8000         # web app at http://localhost:8000
```

Both clients support `/prompts`, `/prompt <server> <name> "args"`, `/resources` and `/resource <server> <uri>`. The [reference](docs/reference.md) covers commands, server APIs, web-app security, settings and troubleshooting.

<details>
<summary>Project structure</summary>

```text
mcp_client.py         LangGraph agent, ordered tool node, terminal chat
weather_server.py     MCP server: current weather and dated forecasts
task_server.py        MCP server: tasks and weather-aware trip prompts
web/                  FastAPI app and chat UI (bring your own key)
eval/                 System eval: runner, 76 cases, fixtures, methodology
tests/                88 unit tests
Dockerfile            Single container deployed to Azure App Service
.github/workflows/    CI/CD: test → build → push to GHCR → deploy
```
</details>

## Credits

Weather data provided by [OpenWeather](https://openweathermap.org/).
