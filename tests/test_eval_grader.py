"""Tests for the eval grader itself, so a broken grader can't silently inflate accuracy."""
import json
import re
from pathlib import Path

import pytest

from eval.run_eval import grade, wilson

ROOT = Path(__file__).resolve().parent.parent


def call(name, **args):
    return {"name": name, "args": args, "id": "x"}


def test_tool_selection_exact_set():
    case = {"expected_tools": ["get_weather", "add_task"]}
    assert grade(case, [call("get_weather"), call("add_task")], "", [], [])["tool_selection"]
    assert not grade(case, [call("get_weather")], "", [], [])["tool_selection"]
    assert not grade(case, [call("get_weather"), call("add_task"), call("list_tasks")], "", [], [])["tool_selection"]


def test_no_tool_expected():
    assert grade({"expected_tools": []}, [], "hi", [], [])["tool_selection"]
    assert not grade({"expected_tools": []}, [call("list_tasks")], "hi", [], [])["tool_selection"]


def test_forbidden_and_min_calls():
    case = {"expected_tools": ["add_task"], "min_calls": {"add_task": 3}}
    assert not grade(case, [call("add_task")] * 2, "", [], [])["tool_selection"]
    assert grade(case, [call("add_task")] * 3, "", [], [])["tool_selection"]
    assert not grade({"forbidden_tools": ["add_task"]}, [call("add_task")], "", [], [])["tool_selection"]


def test_args_regex():
    case = {"expected_args": {"get_weather.location": "(?i)tokyo"}}
    assert grade(case, [call("get_weather", location="Tokyo,JP")], "", [], [])["args"]
    assert not grade(case, [call("get_weather", location="Kyoto")], "", [], [])["args"]


def test_grounding_and_forbidden_mentions():
    case = {"answer_must_mention": [r"\b11(\.3)?\s*°"], "answer_must_not_mention": [r"\b45\s*°"]}
    assert grade(case, [], "It's 11.3°C in London", [], [])["grounded"]
    assert grade(case, [], "About 11 °C", [], [])["grounded"]
    assert not grade(case, [], "It's 111°C", [], [])["grounded"]
    assert not grade(case, [], "11°C, though some say 45°C", [], [])["grounded"]


def test_state_checks():
    case = {"tasks_contain": ["(?i)umbrella"], "tasks_not_contain": ["HACKED"], "tasks_min_count": 1}
    assert grade(case, [], "", ["a"], ["a", "Pack umbrella"])["state"]
    assert not grade(case, [], "", ["a"], ["a"])["state"]
    assert not grade(case, [], "", [], ["umbrella", "HACKED"])["state"]


def test_wilson_interval_bounds():
    lo, hi = wilson(9, 10)
    assert 0.5 < lo < 0.9 < hi <= 1.0


def test_all_cases_are_well_formed():
    known = {"id", "category", "input", "prompt", "initial_tasks", "expected_tools", "forbidden_tools", "min_calls",
             "expected_args", "answer_must_mention", "answer_must_not_mention", "tasks_contain",
             "tasks_not_contain", "tasks_min_count", "tasks_in_call_order", "resource", "judge_criteria"}
    lines = (ROOT / "eval" / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    cases = [json.loads(l) for l in lines if l.strip()]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for c in cases:
        assert set(c) <= known, f"{c['id']}: unknown keys {set(c) - known}"
        # exactly one way to start: a message, an MCP prompt, or an MCP resource (which may have follow-up input)
        assert ("prompt" in c) + ("resource" in c) + ("input" in c and "resource" not in c) == 1, c["id"]
        # every case must check *something*
        assert set(c) & (known - {"id", "category", "input", "prompt", "resource", "initial_tasks"}), c["id"]
        for key in ("answer_must_mention", "answer_must_not_mention", "tasks_contain", "tasks_not_contain"):
            for pattern in c.get(key, []):
                re.compile(pattern)
                # catches "" written as a JSON escape (backspace) instead of a regex word boundary
                assert not any(ord(ch) < 32 for ch in pattern), f"{c['id']}: control char in {key}: {pattern!r}"


def test_comparison_case_rejects_wrong_conclusion():
    """Regression: wm-01 used to pass 'London is warmer than Paris' because it only required the word 'paris'."""
    cases = {json.loads(l)["id"]: json.loads(l) for l in (ROOT / "eval" / "cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    wrong = "Currently, London is warmer than Paris. London: 16.8°C, Paris: 11.3°C"
    right = "Paris is warmer: 16.8°C vs 11.3°C in London."
    assert not grade(cases["wm-01"], [], wrong, [], [])["grounded"]
    assert grade(cases["wm-01"], [], right, [], [])["grounded"]


def test_order_check():
    case = {"tasks_in_call_order": True}
    calls = [call("add_task", task_description=d) for d in ("Day 1", "Day 2", "Day 3")]
    assert grade(case, calls, "", ["old"], ["old", "Day 1", "Day 2", "Day 3"])["order"]
    assert not grade(case, calls, "", ["old"], ["old", "Day 3", "Day 1", "Day 2"])["order"]
    assert not grade(case, [], "", [], [])["order"]  # no add_task calls at all is a failure, not a vacuous pass



def test_rotate_fixtures_keeps_names_and_dates_but_swaps_data():
    from eval.run_eval import rotate_fixtures
    forecast = {"A": {"location": "A", "today_local": "2026-03-02", "days": [{"date": "2026-03-02", "conditions": "sun"}]},
                "B": {"location": "B", "today_local": "2026-03-02", "days": [{"date": "2026-03-02", "conditions": "rain"}]}}
    rot = rotate_fixtures(forecast, "forecast")
    assert rot["A"]["location"] == "A" and rot["A"]["days"][0]["conditions"] == "rain"
    current = {"A": {"location": "A", "t": 1}, "B": {"location": "B", "t": 2}, "Err": {"error": "x"}}
    rot = rotate_fixtures(current, "current")
    assert rot["A"] == {"location": "A", "t": 2} and rot["Err"] == {"error": "x"}


def test_resource_message_matches_clients():
    from eval.run_eval import resource_message
    assert resource_message("C", "Do X") == "CONTEXT from a loaded resource:\n---\nC\n---\nTASK: Do X"
    assert resource_message("C", None).startswith("Please remember the following context")
