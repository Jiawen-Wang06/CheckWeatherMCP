"""Web app tests: key handling, session isolation, rate limiting, security headers (no OpenAI calls)."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web import app as web

FAKE_KEY = "sk-proj-unit-test-key-0123456789abcdef"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "SESSION_ROOT", tmp_path / "sessions")
    web.hits.clear()
    with TestClient(web.app) as c:
        yield c
    web.hits.clear()


def test_page_is_served_with_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "MCP DayPlanner" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "We don&#39;t store your key" in r.text or "We don't store your key" in r.text


def test_redact_removes_keys_from_error_text():
    msg = f"Incorrect API key provided: {FAKE_KEY}. You can find your key at ..."
    assert FAKE_KEY not in web.redact(msg)
    assert "sk-***" in web.redact(msg)


@pytest.mark.parametrize("bad", ["", "hello", "sk-short", "sk-has space in it 1234567890"])
def test_malformed_keys_rejected_before_any_model_call(client, bad):
    r = client.post("/api/key/check", headers={"X-OpenAI-Key": bad})
    assert r.status_code == 400


def test_visitor_key_is_never_passed_to_mcp_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    configs = web.server_configs(tmp_path / "tasks.txt")
    for cfg in configs.values():
        assert not any("OPENAI" in k for k in cfg["env"])
        assert FAKE_KEY not in str(cfg)


def test_sessions_are_isolated_and_deleted(client):
    a = client.post("/api/session").json()
    b = client.post("/api/session").json()
    assert a["session_id"] != b["session_id"]
    assert {t["name"] for t in a["tools"]} == {"get_weather", "get_forecast", "add_task", "list_tasks",
                                                   "clear_tasks", "update_task"}

    sa = web.sessions[a["session_id"]]
    sa.tasks_file.write_text("secret task of A\n", encoding="utf-8")
    assert client.get("/api/tasks", headers={"X-Session-Id": a["session_id"]}).json()["tasks"] == ["secret task of A"]
    assert client.get("/api/tasks", headers={"X-Session-Id": b["session_id"]}).json()["tasks"] == []

    sdir = sa.dir
    client.delete("/api/session", headers={"X-Session-Id": a["session_id"]})
    assert not Path(sdir).exists()
    assert client.get("/api/tasks", headers={"X-Session-Id": a["session_id"]}).status_code == 404


def test_chat_requires_valid_session_and_key(client):
    sid = client.post("/api/session").json()["session_id"]
    assert client.post("/api/chat", json={"message": "hi"}, headers={"X-Session-Id": "nope", "X-OpenAI-Key": FAKE_KEY}).status_code == 404
    assert client.post("/api/chat", json={"message": "hi"}, headers={"X-Session-Id": sid}).status_code == 400
    too_long = {"message": "x" * 1001}
    assert client.post("/api/chat", json=too_long, headers={"X-Session-Id": sid, "X-OpenAI-Key": FAKE_KEY}).status_code == 422


def test_unknown_mcp_prompt_rejected(client):
    sid = client.post("/api/session").json()["session_id"]
    r = client.post("/api/chat", json={"prompt": {"server": "tasks", "name": "rm_rf"}},
                    headers={"X-Session-Id": sid, "X-OpenAI-Key": FAKE_KEY})
    assert r.status_code == 400


def test_rate_limit(client):
    codes = [client.post("/api/key/check", headers={"X-OpenAI-Key": "bad"}).status_code for _ in range(web.RATE_PER_MINUTE + 2)]
    assert codes[: web.RATE_PER_MINUTE] == [400] * web.RATE_PER_MINUTE
    assert codes[-1] == 429


def test_client_ip_from_azure_forwarded_header():
    class Req:
        def __init__(self, fwd):
            self.headers = {"x-forwarded-for": fwd}
            self.client = None
    assert web.client_ip(Req("203.0.113.7:54321, 10.0.0.1")) == "203.0.113.7"
    assert web.client_ip(Req("2001:db8::1")) == "2001:db8::1"


def test_key_pasted_into_chat_is_redacted_before_reaching_the_agent(client, monkeypatch):
    """If a visitor pastes their key into the message box, it must not reach conversation memory or the model."""
    seen = {}

    class FakeAgent:
        async def ainvoke(self, state, config):
            seen["message"] = state["messages"][0][1]
            from langchain_core.messages import AIMessage, HumanMessage
            return {"messages": [HumanMessage(content=seen["message"]), AIMessage(content="ok")]}

    monkeypatch.setattr(web, "create_graph", lambda *a, **k: FakeAgent())
    sid = client.post("/api/session").json()["session_id"]
    r = client.post("/api/chat", json={"message": f"my key is {FAKE_KEY} please use it"},
                    headers={"X-Session-Id": sid, "X-OpenAI-Key": FAKE_KEY})
    assert r.status_code == 200
    assert FAKE_KEY not in seen["message"] and "sk-***" in seen["message"]


def _fake_agent(monkeypatch, seen: dict):
    class FakeAgent:
        async def ainvoke(self, state, config):
            seen["message"] = state["messages"][0][1]
            from langchain_core.messages import AIMessage, HumanMessage
            return {"messages": [HumanMessage(content=seen["message"]), AIMessage(content="ok")]}
    monkeypatch.setattr(web, "create_graph", lambda *a, **k: FakeAgent())


def test_capabilities_list_prompts_and_resources(client):
    sid = client.post("/api/session").json()["session_id"]
    caps = client.get("/api/capabilities", headers={"X-Session-Id": sid}).json()
    assert {(p["server"], p["name"]) for p in caps["prompts"]} == {
        ("weather", "compare_weather_prompt"), ("tasks", "plan_trip_prompt"), ("tasks", "plan_trip_for_future_date")}
    assert {(r["server"], r["uri"]) for r in caps["resources"]} == {("weather", "file://delivery_log"), ("tasks", "file://meeting_notes")}


def test_resource_with_instruction_reaches_agent_as_context(client, monkeypatch):
    seen = {}
    _fake_agent(monkeypatch, seen)
    sid = client.post("/api/session").json()["session_id"]
    r = client.post("/api/chat", json={"resource": {"server": "tasks", "uri": "file://meeting_notes", "instruction": "List the action items"}},
                    headers={"X-Session-Id": sid, "X-OpenAI-Key": FAKE_KEY})
    assert r.status_code == 200
    assert r.json()["resource"]["uri"] == "file://meeting_notes"
    assert seen["message"].startswith("CONTEXT from a loaded resource:")
    assert "Weekly UX Sync" in seen["message"] and seen["message"].endswith("TASK: List the action items")


def test_resource_without_instruction_is_added_as_context(client, monkeypatch):
    seen = {}
    _fake_agent(monkeypatch, seen)
    sid = client.post("/api/session").json()["session_id"]
    r = client.post("/api/chat", json={"resource": {"server": "weather", "uri": "file://delivery_log"}},
                    headers={"X-Session-Id": sid, "X-OpenAI-Key": FAKE_KEY})
    assert r.status_code == 200
    assert seen["message"].startswith("Please remember the following context") and "Order #10583" in seen["message"]


def test_unknown_resource_rejected(client):
    sid = client.post("/api/session").json()["session_id"]
    r = client.post("/api/chat", json={"resource": {"server": "tasks", "uri": "file:///etc/passwd"}},
                    headers={"X-Session-Id": sid, "X-OpenAI-Key": FAKE_KEY})
    assert r.status_code == 400 and "/resources" in r.json()["error"]


def test_prompt_with_wrong_arguments_rejected(client):
    sid = client.post("/api/session").json()["session_id"]
    r = client.post("/api/chat", json={"prompt": {"server": "tasks", "name": "plan_trip_prompt", "args": {"destination": "Rome"}}},
                    headers={"X-Session-Id": sid, "X-OpenAI-Key": FAKE_KEY})
    assert r.status_code == 400 and "duration_in_days" in r.json()["error"]


def test_prompt_command_runs_through_agent(client, monkeypatch):
    seen = {}
    _fake_agent(monkeypatch, seen)
    sid = client.post("/api/session").json()["session_id"]
    r = client.post("/api/chat", json={"prompt": {"server": "weather", "name": "compare_weather_prompt",
                                                  "args": {"location_a": "London", "location_b": "Paris"}}},
                    headers={"X-Session-Id": sid, "X-OpenAI-Key": FAKE_KEY})
    assert r.status_code == 200 and r.json()["prompt"]["name"] == "compare_weather_prompt"
    assert '"London"' in seen["message"] and '"Paris"' in seen["message"]



def test_prompt_validation_error_is_shown_cleanly(client):
    sid = client.post("/api/session").json()["session_id"]
    r = client.post("/api/chat", json={"prompt": {"server": "tasks", "name": "plan_trip_for_future_date",
                                                  "args": {"destination": "Rome", "from_date": "2026-03-05", "to_date": "2026-03-01"}}},
                    headers={"X-Session-Id": sid, "X-OpenAI-Key": FAKE_KEY})
    assert r.status_code == 400
    assert r.json()["error"] == "Couldn't run that prompt: to_date (2026-03-01) must be on or after from_date (2026-03-05)."
