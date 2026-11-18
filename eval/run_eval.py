"""
System evaluation for the MCP DayPlanner agent.

Runs every case in eval/cases.jsonl through the real LangGraph agent + real MCP servers
(weather server in mock mode, task server on an isolated temp file) and scores:

  tool_selection  right set of tools called, forbidden tools avoided, minimum call counts met
  args            tool arguments match the expected patterns
  grounded        final answer contains the facts returned by the tools (and nothing forbidden)
  state           tasks file ends up in the expected state
  faithfulness    (optional, --judge) LLM-as-judge score >= 4/5 that the answer is supported by tool outputs
  criteria        (optional, --judge) the judge confirms the case's own rubric ("judge_criteria"), e.g.
                  "the snowy day's activities are indoors"
  pass            every applicable check above passed

Usage (from project root):
  weathermcp\\Scripts\\python.exe eval\\run_eval.py
  weathermcp\\Scripts\\python.exe eval\\run_eval.py --trials 3 --judge gpt-4o
  weathermcp\\Scripts\\python.exe eval\\run_eval.py --filter multi_intent --verbose
"""
import argparse
import asyncio
import json
import math
import re
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging

from langchain_core.messages import AIMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, Field

from mcp_client import OPENAI_API_KEY, create_graph

# The agent's "today" is pinned to the forecast fixtures' date, so relative dates ("tomorrow", "March 5")
# resolve the same way on every run, whatever the real date is.
from datetime import date
EVAL_TODAY = date.fromisoformat(next(iter(json.loads(
    (ROOT / "eval" / "fixtures" / "forecast.json").read_text(encoding="utf-8")).values()))["today_local"])

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

CHECKS = ["tool_selection", "args", "grounded", "state", "order", "faithfulness", "criteria"]


# ---------------------------------------------------------------- setup

def server_configs_for(tasks_file: Path, fixtures: Path, forecast_fixtures: Path) -> dict:
    """Same servers as mcp_client.py, but mock weather + isolated task file."""
    py = sys.executable
    return {
        "weather": {
            "command": py,
            "args": [str(ROOT / "weather_server.py")],
            "transport": "stdio",
            "cwd": str(ROOT),
            "env": {"WEATHER_MOCK": "1", "WEATHER_FIXTURES": str(fixtures), "FORECAST_FIXTURES": str(forecast_fixtures)},
        },
        "tasks": {
            "command": py,
            "args": [str(ROOT / "task_server.py")],
            "transport": "stdio",
            "cwd": str(ROOT),
            "env": {"TASKS_FILE": str(tasks_file)},
        },
    }


def read_tasks(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------- grading

def grade(case: dict, calls: list[dict], answer: str, tasks_before: list[str], tasks_after: list[str]) -> dict:
    r: dict = {}
    names = [c["name"] for c in calls]

    if "expected_tools" in case or "forbidden_tools" in case or "min_calls" in case:
        ok = True
        if "expected_tools" in case:
            ok &= set(names) == set(case["expected_tools"])
        ok &= not any(t in names for t in case.get("forbidden_tools", []))
        ok &= all(names.count(t) >= n for t, n in case.get("min_calls", {}).items())
        r["tool_selection"] = ok

    if case.get("expected_args"):
        def arg_ok(key: str, pattern: str) -> bool:
            tool, arg = key.split(".", 1)
            return any(c["name"] == tool and re.search(pattern, str(c["args"].get(arg, ""))) for c in calls)
        r["args"] = all(arg_ok(k, p) for k, p in case["expected_args"].items())

    if case.get("answer_must_mention") or case.get("answer_must_not_mention"):
        r["grounded"] = all(re.search(p, answer) for p in case.get("answer_must_mention", [])) and not any(
            re.search(p, answer) for p in case.get("answer_must_not_mention", [])
        )

    if any(k in case for k in ("tasks_contain", "tasks_not_contain", "tasks_min_count", "tasks_in_call_order")):
        blob = "\n".join(tasks_after)
        ok = all(re.search(p, blob) for p in case.get("tasks_contain", []))
        ok &= not any(re.search(p, blob) for p in case.get("tasks_not_contain", []))
        ok &= len(tasks_after) - len(tasks_before) >= case.get("tasks_min_count", 0)
        r["state"] = ok

    if case.get("tasks_in_call_order"):
        # New lines in the task file must appear in the same order the agent issued add_task calls.
        issued = [str(c["args"].get("task_description", "")).strip() for c in calls if c["name"] == "add_task"]
        r["order"] = bool(issued) and tasks_after[len(tasks_before):] == issued

    return r


class JudgeVerdict(BaseModel):
    faithfulness: int = Field(ge=1, le=5, description="5 = every factual claim about weather/tasks is supported by the tool outputs; 1 = fabricated or contradicts tool outputs")
    helpfulness: int = Field(ge=1, le=5, description="5 = fully and directly addresses the user's request")
    criteria_met: bool | None = Field(default=None, description="Only when CASE-SPECIFIC CRITERIA are given: true if ALL of them are satisfied, else false")
    reason: str = Field(description="One sentence justification")


JUDGE_PROMPT = """You are grading an AI assistant that has weather and to-do-list tools.
{history}
USER MESSAGE BEING ANSWERED:
{request}

TOOL CALLS AND OUTPUTS (ground truth, from the whole conversation):
{tools}

ASSISTANT ANSWER TO THAT MESSAGE:
{answer}

FAITHFULNESS is ONLY about whether the facts stated in the answer are supported: every specific fact about weather,
errors and tasks must match the tool outputs. General knowledge (travel suggestions, definitions) is fine. Invented
numbers, invented conditions, listing tasks a tool did not return, or claiming success when a tool returned an error
are faithfulness failures. Leaving something out is NOT a faithfulness failure (that belongs in helpfulness).
If no tools were called and the answer makes no claims about live weather or tasks, faithfulness is 5.

HELPFULNESS: does the answer fully address the USER MESSAGE BEING ANSWERED?
{criteria}"""

CRITERIA_BLOCK = """
CASE-SPECIFIC CRITERIA (judge them against the answer AND the tool calls, including the exact tasks saved with
add_task). Set criteria_met to true only if ALL of them are satisfied:
{criteria}
"""

HISTORY_BLOCK = """
EARLIER USER MESSAGES (already answered in previous turns; the answer below does NOT need to repeat them):
{turns}
"""


async def judge(judge_llm, turns: list[str], trace: list[dict], answer: str, criteria: str | None = None) -> dict:
    tools_txt = "\n".join(f"- {t['name']}({json.dumps(t['args'], ensure_ascii=False)}) -> {t['output']}" for t in trace) or "(none)"
    history = HISTORY_BLOCK.format(turns="\n".join(f"- {t}" for t in turns[:-1])) if len(turns) > 1 else ""
    prompt = JUDGE_PROMPT.format(history=history, request=turns[-1], tools=tools_txt, answer=answer,
                                 criteria=CRITERIA_BLOCK.format(criteria=criteria) if criteria else "")
    v: JudgeVerdict = await judge_llm.ainvoke(prompt)
    return v.model_dump()


# ---------------------------------------------------------------- running

def resource_message(content: str, instruction: str | None) -> str:
    """Same format the terminal client and the web app use when a resource is loaded."""
    if instruction:
        return f"CONTEXT from a loaded resource:\n---\n{content}\n---\nTASK: {instruction}"
    return ("Please remember the following context for our conversation. "
            f"Just acknowledge that you have received it.\n---\nCONTEXT:\n{content}\n---")


async def run_trial(case: dict, trial: int, args, fixtures: Path, judge_llm) -> dict:
    out = {"id": case["id"], "category": case["category"], "trial": trial}
    with tempfile.TemporaryDirectory() as tmp:
        tasks_file = Path(tmp) / "tasks.txt"
        if case.get("initial_tasks"):
            tasks_file.write_text("\n".join(case["initial_tasks"]) + "\n", encoding="utf-8")
        tasks_before = read_tasks(tasks_file)

        t0 = time.perf_counter()
        try:
            client = MultiServerMCPClient(server_configs_for(tasks_file, fixtures, Path(args.forecast_fixtures)))
            tools = [] if args.control == "no-tools" else await client.get_tools()
            agent = create_graph(tools, model=args.model, today=EVAL_TODAY)

            if "prompt" in case:
                p = case["prompt"]
                msgs = await client.get_prompt(p["server"], p["name"], arguments=p["args"])
                request = msgs[0].content
            elif "resource" in case:
                # Load an MCP resource (as /resource does), optionally followed by more user turns.
                r = case["resource"]
                blobs = await client.get_resources(server_name=r["server"], uris=[r["uri"]])
                followups = case.get("input", [])
                request = [resource_message(blobs[0].as_string(), r.get("instruction"))] + (
                    followups if isinstance(followups, list) else [followups])
                if len(request) == 1:
                    request = request[0]
            else:
                request = case["input"]

            # A list input is a multi-turn conversation on one thread; the final state holds every turn.
            turns = request if isinstance(request, list) else [request]
            if isinstance(request, list):
                request = "\n".join(f"[turn {i + 1}] {t}" for i, t in enumerate(turns))
            cfg = {"configurable": {"thread_id": f"{case['id']}-{trial}"}, "recursion_limit": 25}
            for turn in turns:
                res = await asyncio.wait_for(agent.ainvoke({"messages": [("user", turn)]}, cfg), timeout=args.timeout)
        except Exception as e:  # any crash/timeout = all applicable checks fail
            out.update(error=f"{type(e).__name__}: {e}", latency_s=round(time.perf_counter() - t0, 2))
            empty = grade(case, [], "", tasks_before, tasks_before)
            out["checks"] = {k: False for k in empty}
            out["pass"] = False
            return out

        out["latency_s"] = round(time.perf_counter() - t0, 2)
        messages = res["messages"]
        calls = [tc for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls]
        outputs = {m.tool_call_id: m.content for m in messages if isinstance(m, ToolMessage)}
        trace = [{"name": c["name"], "args": c["args"], "output": str(outputs.get(c["id"], ""))[:8000]} for c in calls]  # judge must see full outputs (1000 chars cut off forecasts)
        answer = messages[-1].content if isinstance(messages[-1].content, str) else str(messages[-1].content)
        tasks_after = read_tasks(tasks_file)

        # Changes held back by the "look before you write" guard never happened: grade only calls that ran.
        executed = [c for c in calls if not str(outputs.get(c["id"], "")).startswith("NOT EXECUTED")]
        out["deferred_writes"] = len(calls) - len(executed)
        checks = grade(case, executed, answer, tasks_before, tasks_after)
        if judge_llm is not None:
            try:
                verdict = await judge(judge_llm, turns, trace, answer, case.get("judge_criteria"))
                out["judge"] = verdict
                checks["faithfulness"] = verdict["faithfulness"] >= 4
                if case.get("judge_criteria"):
                    checks["criteria"] = verdict.get("criteria_met") is True
            except Exception as e:
                out["judge_error"] = str(e)

        out.update(
            checks=checks,
            **{"pass": all(checks.values())},
            tool_calls=trace,  # includes tool outputs, so runs can be re-judged offline
            answer=answer,
            tasks_after=tasks_after,
            llm_turns=sum(isinstance(m, AIMessage) for m in messages),
            total_tokens=sum((m.usage_metadata or {}).get("total_tokens", 0) for m in messages if isinstance(m, AIMessage)),
        )
        return out


def rotate_fixtures(data: dict, kind: str) -> dict:
    """Negative-control helper: give each city the next city's data, keeping its own name (and forecast dates)."""
    cities = [c for c, v in data.items() if "error" not in v]
    out = dict(data)
    for i, c in enumerate(cities):
        donor = data[cities[(i + 1) % len(cities)]]
        if kind == "forecast":
            out[c] = {**data[c], "days": donor["days"]}
        else:
            out[c] = {**donor, "location": data[c]["location"]}
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def summarize(results: list[dict], trials: int) -> dict:
    def rate(rows, key):
        vals = [r["checks"][key] for r in rows if key in r.get("checks", {})]
        return (sum(vals), len(vals))

    summary = {"metrics": {}, "by_category": {}, "cases": {}}
    for key in CHECKS:
        k, n = rate(results, key)
        if n:
            summary["metrics"][key] = {"rate": k / n, "n": n, "ci95": wilson(k, n)}
    k = sum(r["pass"] for r in results)
    summary["metrics"]["overall_pass"] = {"rate": k / len(results), "n": len(results), "ci95": wilson(k, len(results))}

    by_case = defaultdict(list)
    for r in results:
        by_case[r["id"]].append(r)
    # pass@k: case passed in at least one trial; pass^k: passed in every trial (consistency)
    summary["metrics"]["pass@k"] = {"rate": statistics.mean(any(r["pass"] for r in rs) for rs in by_case.values()), "k": trials}
    summary["metrics"]["pass^k"] = {"rate": statistics.mean(all(r["pass"] for r in rs) for rs in by_case.values()), "k": trials}
    for cid, rs in by_case.items():
        summary["cases"][cid] = sum(r["pass"] for r in rs) / len(rs)

    cats = defaultdict(list)
    for r in results:
        cats[r["category"]].append(r["pass"])
    summary["by_category"] = {c: {"rate": sum(v) / len(v), "n": len(v)} for c, v in sorted(cats.items())}

    ok = [r for r in results if "error" not in r]
    lat = sorted(r["latency_s"] for r in results)
    summary["perf"] = {
        "latency_mean_s": round(statistics.mean(lat), 2),
        "latency_p95_s": lat[min(len(lat) - 1, int(0.95 * len(lat)))],
        "tokens_mean": round(statistics.mean(r["total_tokens"] for r in ok)) if ok else 0,
        "llm_turns_mean": round(statistics.mean(r["llm_turns"] for r in ok), 2) if ok else 0,
        "errors": len(results) - len(ok),
        "deferred_writes": sum(r.get("deferred_writes", 0) for r in ok),
    }
    judged = [r["judge"] for r in results if "judge" in r]
    if judged:
        summary["perf"]["judge_faithfulness_mean"] = round(statistics.mean(j["faithfulness"] for j in judged), 2)
        summary["perf"]["judge_helpfulness_mean"] = round(statistics.mean(j["helpfulness"] for j in judged), 2)
    return summary


def print_report(summary: dict, results: list[dict], verbose: bool):
    print("\n================ EVAL REPORT ================")
    for key, m in summary["metrics"].items():
        if "ci95" in m:
            lo, hi = m["ci95"]
            print(f"  {key:16s} {m['rate']:6.1%}   (n={m['n']}, 95% CI {lo:.0%}-{hi:.0%})")
        else:
            print(f"  {key:16s} {m['rate']:6.1%}   (k={m['k']})")
    print("\n  By category (pass rate):")
    for c, m in summary["by_category"].items():
        print(f"    {c:20s} {m['rate']:6.1%}  (n={m['n']})")
    print("\n  Performance:")
    for k, v in summary["perf"].items():
        print(f"    {k:24s} {v}")

    failures = [r for r in results if not r["pass"]]
    if failures:
        print(f"\n  Failures ({len(failures)}):")
        for r in failures:
            failed = [k for k, v in r.get("checks", {}).items() if not v]
            print(f"    {r['id']} [trial {r['trial']}] failed: {', '.join(failed) or '-'}"
                  + (f"  ERROR: {r['error']}" if "error" in r else ""))
            if verbose and "error" not in r:
                print(f"        tools : {[(c['name'], c['args']) for c in r['tool_calls']]}")
                print(f"        answer: {r['answer'][:300]!r}")
                if "judge" in r:
                    print(f"        judge : {r['judge']}")
    print("=============================================")


def git_sha() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    ap.add_argument("--fixtures", default=str(ROOT / "eval" / "fixtures" / "weather.json"))
    ap.add_argument("--forecast-fixtures", default=str(ROOT / "eval" / "fixtures" / "forecast.json"))
    ap.add_argument("--model", default="gpt-4o-mini", help="model under test")
    ap.add_argument("--trials", type=int, default=1, help="runs per case (LLMs are non-deterministic; use 3-5 for reliable numbers)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--judge", metavar="MODEL", help="enable LLM-as-judge with this model, e.g. gpt-4o")
    ap.add_argument("--filter", help="only run cases whose id or category contains this string")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--min-pass", type=float, help="exit non-zero if overall pass rate is below this (for CI), e.g. 0.85")
    ap.add_argument("--control", choices=["no-tools", "shuffled-fixtures"],
                    help="negative control: deliberately break the system to prove the grader can fail. "
                         "no-tools = agent gets no tools (tool_selection should collapse); "
                         "shuffled-fixtures = each city returns another city's weather (grounded should collapse)")
    args = ap.parse_args()

    if not OPENAI_API_KEY:
        sys.exit("OPENAI_API_KEY is not set (see .env.example).")

    cases = [json.loads(l) for l in Path(args.cases).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.filter:
        cases = [c for c in cases if args.filter in c["id"] or args.filter in c["category"]]
    if not cases:
        sys.exit("No cases selected.")

    # Preflight: infrastructure problems (bad key, no quota, unknown model) must abort the run,
    # not be scored as model failures and drag the accuracy number down.
    from langchain_openai import ChatOpenAI
    for m in filter(None, {args.model, args.judge}):
        try:
            await ChatOpenAI(model=m, max_tokens=1, openai_api_key=OPENAI_API_KEY).ainvoke("ping")
        except Exception as e:
            sys.exit(f"Preflight failed for model '{m}': {type(e).__name__}: {e}")

    fixtures, forecast_fixtures = Path(args.fixtures), Path(args.forecast_fixtures)
    if args.control == "shuffled-fixtures":
        # Each city gets another city's current weather AND forecast (keeping its own name and dates), so an
        # agent that faithfully reports tool data no longer matches the expected answers.
        tmp = Path(tempfile.mkdtemp())
        fixtures = tmp / "weather_shuffled.json"
        fixtures.write_text(json.dumps(rotate_fixtures(json.loads(Path(args.fixtures).read_text(encoding="utf-8")), "current"),
                                       ensure_ascii=False), encoding="utf-8")
        forecast_fixtures = tmp / "forecast_shuffled.json"
        forecast_fixtures.write_text(json.dumps(rotate_fixtures(json.loads(Path(args.forecast_fixtures).read_text(encoding="utf-8")), "forecast"),
                                                ensure_ascii=False), encoding="utf-8")
    args.fixtures, args.forecast_fixtures = str(fixtures), str(forecast_fixtures)

    judge_llm = None
    if args.judge:
        judge_llm = ChatOpenAI(model=args.judge, temperature=0, openai_api_key=OPENAI_API_KEY).with_structured_output(JudgeVerdict, method="function_calling")

    print(f"Running {len(cases)} cases x {args.trials} trial(s) against {args.model}"
          + (f", judge={args.judge}" if args.judge else "") + " ...")
    sem = asyncio.Semaphore(args.concurrency)
    done = 0

    async def guarded(case, t):
        nonlocal done
        async with sem:
            r = await run_trial(case, t, args, Path(args.fixtures), judge_llm)
        done += 1
        print(f"  [{done}/{len(cases) * args.trials}] {r['id']} t{t}: {'PASS' if r['pass'] else 'FAIL'}", flush=True)
        return r

    results = await asyncio.gather(*(guarded(c, t) for c in cases for t in range(args.trials)))
    results.sort(key=lambda r: (r["id"], r["trial"]))
    summary = summarize(results, args.trials)
    print_report(summary, results, args.verbose)

    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(exist_ok=True)
    sha = git_sha()
    tag = f"_CONTROL-{args.control}" if args.control else ""
    out_file = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{args.model}{tag}_{sha}.json"
    out_file.write_text(json.dumps({
        "meta": {"model": args.model, "judge": args.judge, "trials": args.trials, "control": args.control, "git_sha": sha,
                 "n_cases": len(cases), "timestamp": datetime.now().isoformat(timespec="seconds")},
        "summary": summary, "results": results,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved: {out_file}")

    if args.min_pass is not None and summary["metrics"]["overall_pass"]["rate"] < args.min_pass:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
