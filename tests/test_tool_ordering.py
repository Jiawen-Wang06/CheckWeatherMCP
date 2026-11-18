"""Regression tests for the ordered tool node (concurrent tool calls used to scramble task order)."""
import asyncio
import random
import time

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from mcp_client import State, make_ordered_tool_node



def make_tools(log: list):
    async def add_task(task_description: str) -> str:
        await asyncio.sleep(random.uniform(0.01, 0.08))  # simulate variable server latency
        log.append(task_description)
        return "ok"

    async def get_weather(location: str) -> str:
        await asyncio.sleep(0.1)
        return location

    return [
        StructuredTool.from_function(coroutine=add_task, name="add_task", description="add"),
        StructuredTool.from_function(coroutine=get_weather, name="get_weather", description="weather",
                                     metadata={"readOnlyHint": True}),
    ]


def state_with_calls(name: str, arg: str, values: list[str]) -> dict:
    calls = [{"name": name, "args": {arg: v}, "id": f"c{i}", "type": "tool_call"} for i, v in enumerate(values)]
    return {"messages": [AIMessage(content="", tool_calls=calls)]}


ITINERARY = [f"Day {d}: item {i}" for d in (1, 2, 3) for i in (1, 2)]


def test_old_toolnode_scrambles_writes():
    """Documents the original bug: with random latency, plain ToolNode loses the issue order."""
    random.seed(1)
    scrambled = 0
    for _ in range(5):
        log: list = []
        g = StateGraph(State)
        g.add_node("tools", ToolNode(make_tools(log)))
        g.add_edge(START, "tools")
        g.add_edge("tools", END)
        asyncio.run(g.compile().ainvoke(state_with_calls("add_task", "task_description", ITINERARY)))
        scrambled += log != ITINERARY
    assert scrambled > 0


def test_ordered_node_preserves_write_order():
    random.seed(1)
    for _ in range(5):
        log: list = []
        out = asyncio.run(make_ordered_tool_node(make_tools(log))(
            state_with_calls("add_task", "task_description", ITINERARY)))
        assert log == ITINERARY
        assert [m.tool_call_id for m in out["messages"]] == [f"c{i}" for i in range(len(ITINERARY))]


def test_read_only_batch_still_runs_concurrently():
    node = make_ordered_tool_node(make_tools([]))
    t0 = time.perf_counter()
    out = asyncio.run(node(state_with_calls("get_weather", "location", ["A", "B", "C", "D"])))
    assert time.perf_counter() - t0 < 0.3  # 4 x 0.1s sequentially would be >= 0.4s
    assert len(out["messages"]) == 4


def test_unknown_tool_returns_error_message_not_crash():
    node = make_ordered_tool_node(make_tools([]))
    out = asyncio.run(node(state_with_calls("delete_everything", "x", ["1"])))
    assert "error" in out["messages"][0].content.lower()



def test_mixed_batch_runs_lookups_and_holds_back_changes():
    """'Add pack umbrella IF it's raining': a write issued in the same step as the lookup must not run blindly."""
    log: list = []
    node = make_ordered_tool_node(make_tools(log))
    calls = [{"name": "get_weather", "args": {"location": "Sydney"}, "id": "w1", "type": "tool_call"},
             {"name": "add_task", "args": {"task_description": "pack umbrella"}, "id": "a1", "type": "tool_call"}]
    out = asyncio.run(node({"messages": [AIMessage(content="", tool_calls=calls)]}))
    assert log == []  # the task was NOT added
    assert [m.tool_call_id for m in out["messages"]] == ["w1", "a1"]  # one reply per call, in order
    assert out["messages"][0].content == "Sydney"
    assert out["messages"][1].content.startswith("NOT EXECUTED") and out["messages"][1].status == "error"


def test_write_only_batch_still_runs():
    log: list = []
    asyncio.run(make_ordered_tool_node(make_tools(log))(state_with_calls("add_task", "task_description", ["a", "b"])))
    assert log == ["a", "b"]
