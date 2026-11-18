"""
Web front end for MCP DayPlanner.

Visitors bring their own OpenAI API key. Key handling:
  * The browser keeps the key in sessionStorage and sends it in the `X-OpenAI-Key` header over HTTPS.
  * The server uses it only to build the model client for that single request; the client is discarded
    afterwards. The key is never written to disk, never logged, never put in a URL, and never passed
    to the MCP servers (they don't need it).
  * Anything that looks like a key is redacted from error messages before they reach the browser.

Each visitor gets an isolated, temporary session: their own task file and conversation memory,
deleted after SESSION_TTL_HOURS of inactivity.

Run locally:  python -m uvicorn web.app:app --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import openai
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from mcp_client import create_graph  # noqa: E402

log = logging.getLogger("web")

MODEL = os.getenv("MODEL", "gpt-4o-mini")
SESSION_TTL_S = float(os.getenv("SESSION_TTL_HOURS", "3")) * 3600
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "300"))
RATE_PER_MINUTE = int(os.getenv("RATE_PER_MINUTE", "10"))
RATE_PER_HOUR = int(os.getenv("RATE_PER_HOUR", "60"))
TURN_TIMEOUT_S = float(os.getenv("TURN_TIMEOUT_S", "90"))
MAX_MESSAGE_CHARS = 1000
REPO_URL = os.getenv("REPO_URL", "")
SESSION_ROOT = Path(os.getenv("SESSION_DIR", Path(tempfile.gettempdir()) / "mcp-assistant-sessions"))
STATIC = Path(__file__).parent / "static"

KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-*]{6,}")


def redact(text: str) -> str:
    return KEY_PATTERN.sub("sk-***", text)


def root_cause(e: BaseException) -> str:
    """The MCP client wraps server errors in exception groups; dig out the real message."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    msg = str(e)
    return re.sub(r"^Error rendering prompt \S+: ", "", msg)


# ---------------------------------------------------------------- sessions

@dataclass
class Session:
    id: str
    dir: Path
    client: MultiServerMCPClient
    tools: list
    tool_server: dict[str, str]
    prompts: list[dict]
    resources: list[dict]
    last_seen: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def tasks_file(self) -> Path:
        return self.dir / "tasks.txt"

    def tasks(self) -> list[str]:
        if not self.tasks_file.exists():
            return []
        return [l.strip() for l in self.tasks_file.read_text(encoding="utf-8").splitlines() if l.strip()]


sessions: dict[str, Session] = {}
checkpointer = MemorySaver()  # conversation memory for all sessions, keyed by session id


def server_configs(tasks_file: Path) -> dict:
    # MCP stdio servers only inherit a minimal environment, so pass what they need explicitly.
    # Note: the visitor's OpenAI key is deliberately NOT passed here.
    base_env = {"MCP_LOG_LEVEL": "WARNING"}
    return {
        "weather": {
            "command": sys.executable, "args": [str(ROOT / "weather_server.py")], "transport": "stdio", "cwd": str(ROOT),
            "env": {**base_env, "OPENWEATHERMAP_API_KEY": os.getenv("OPENWEATHERMAP_API_KEY", "")},
        },
        "tasks": {
            "command": sys.executable, "args": [str(ROOT / "task_server.py")], "transport": "stdio", "cwd": str(ROOT),
            "env": {**base_env, "TASKS_FILE": str(tasks_file)},
        },
    }


async def create_session() -> Session:
    if len(sessions) >= MAX_SESSIONS:
        cleanup_expired(force_oldest=True)
    sid = secrets.token_urlsafe(24)
    sdir = SESSION_ROOT / sid
    sdir.mkdir(parents=True, exist_ok=True)
    configs = server_configs(sdir / "tasks.txt")
    client = MultiServerMCPClient(configs)

    tools, tool_server, prompts, resources = [], {}, [], []
    for server in configs:
        server_tools = await client.get_tools(server_name=server)
        tools += server_tools
        tool_server.update({t.name: server for t in server_tools})
        async with client.session(server) as s:
            for p in (await s.list_prompts()).prompts:
                prompts.append({"server": server, "name": p.name, "description": (p.description or "").strip().split("\n")[0],
                                "arguments": [a.name for a in (p.arguments or [])]})
            for r in (await s.list_resources()).resources:
                # The MCP library normalizes "file://meeting_notes" to "file://meeting_notes/"; show and match
                # the form people type (and the terminal client uses).
                resources.append({"server": server, "uri": str(r.uri).rstrip("/"), "name": r.name,
                                  "description": (r.description or "").strip().split("\n")[0]})

    session = Session(id=sid, dir=sdir, client=client, tools=tools, tool_server=tool_server, prompts=prompts,
                      resources=resources)
    sessions[sid] = session
    return session


def drop_session(sid: str):
    s = sessions.pop(sid, None)
    if s:
        shutil.rmtree(s.dir, ignore_errors=True)
    try:
        checkpointer.delete_thread(sid)
    except Exception:
        pass


def cleanup_expired(force_oldest: bool = False):
    now = time.time()
    for sid in [sid for sid, s in sessions.items() if now - s.last_seen > SESSION_TTL_S]:
        drop_session(sid)
    if force_oldest and len(sessions) >= MAX_SESSIONS:
        drop_session(min(sessions.values(), key=lambda s: s.last_seen).id)


def get_session(session_id: str | None) -> Session:
    s = sessions.get(session_id or "")
    if not s:
        raise HTTPException(404, "Session expired or not found. Start a new session.")
    s.last_seen = time.time()
    return s


# ---------------------------------------------------------------- rate limiting (per client IP, in memory)

hits: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # Azure App Service puts the original client address first in X-Forwarded-For ("ip:port").
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if fwd:
        return fwd.rsplit(":", 1)[0] if fwd.count(":") == 1 else fwd
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request):
    now, q = time.time(), hits[client_ip(request)]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE_PER_HOUR or sum(1 for t in q if now - t < 60) >= RATE_PER_MINUTE:
        raise HTTPException(429, f"Rate limit reached ({RATE_PER_MINUTE}/minute, {RATE_PER_HOUR}/hour). Please wait a bit.")
    q.append(now)


def require_key(key: str | None) -> str:
    key = (key or "").strip()
    if not key.startswith("sk-") or not (20 <= len(key) <= 300) or any(c.isspace() for c in key):
        raise HTTPException(400, "Please enter a valid OpenAI API key (it starts with 'sk-').")
    return key


def openai_error(e: Exception) -> HTTPException:
    if isinstance(e, openai.AuthenticationError):
        return HTTPException(401, "OpenAI rejected this API key. Check that it's correct and active.")
    if isinstance(e, openai.PermissionDeniedError):
        return HTTPException(403, f"This key can't use {MODEL}. Check the key's project permissions.")
    if isinstance(e, openai.RateLimitError):
        return HTTPException(429, "Your OpenAI account hit a rate limit or has no remaining credit.")
    return HTTPException(502, "The model call failed: " + redact(str(e))[:300])


# ---------------------------------------------------------------- app

async def cleanup_loop():
    while True:
        await asyncio.sleep(600)
        cleanup_expired()


@asynccontextmanager
async def lifespan(app: FastAPI):
    SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(cleanup_loop())
    yield
    task.cancel()
    for sid in list(sessions):
        drop_session(sid)


app = FastAPI(title="MCP DayPlanner", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class ChatRequest(BaseModel):
    message: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    prompt: dict | None = None  # {"server": ..., "name": ..., "args": {...}} to run an MCP prompt
    resource: dict | None = None  # {"server": ..., "uri": ..., "instruction": "..."} to load an MCP resource


MAX_RESOURCE_CHARS = 8000


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
async def healthz():
    return {"ok": True, "sessions": len(sessions)}


@app.post("/api/session")
async def new_session(request: Request):
    rate_limit(request)
    s = await create_session()
    return {"session_id": s.id, "ttl_hours": SESSION_TTL_S / 3600, "model": MODEL, "repo_url": REPO_URL,
            **capabilities_of(s)}


def capabilities_of(s: Session) -> dict:
    return {"prompts": s.prompts, "resources": s.resources,
            "tools": [{"name": t.name, "server": s.tool_server[t.name]} for t in s.tools]}


@app.get("/api/capabilities")
async def capabilities(x_session_id: str | None = Header(default=None)):
    """What /prompts and /resources list in the chat UI."""
    return capabilities_of(get_session(x_session_id))


@app.post("/api/key/check")
async def check_key(request: Request, x_openai_key: str | None = Header(default=None)):
    rate_limit(request)
    key = require_key(x_openai_key)
    try:
        client = openai.AsyncOpenAI(api_key=key)
        await client.models.retrieve(MODEL)  # free call; confirms the key works and can see the model
        await client.close()
    except Exception as e:
        raise openai_error(e)
    return {"ok": True}


@app.get("/api/tasks")
async def tasks(x_session_id: str | None = Header(default=None)):
    return {"tasks": get_session(x_session_id).tasks()}


@app.post("/api/chat")
async def chat(body: ChatRequest, request: Request,
               x_session_id: str | None = Header(default=None), x_openai_key: str | None = Header(default=None)):
    rate_limit(request)
    s = get_session(x_session_id)
    key = require_key(x_openai_key)

    used_prompt = used_resource = None
    if body.prompt:
        p = body.prompt
        spec = next((x for x in s.prompts if (x["server"], x["name"]) == (p.get("server"), p.get("name"))), None)
        if not spec:
            raise HTTPException(400, f"Unknown MCP prompt '{p.get('name')}' on server '{p.get('server')}'. Type /prompts to see what's available.")
        args = {k: redact(str(v)[:100]) for k, v in (p.get("args") or {}).items()}
        if set(args) != set(spec["arguments"]):
            raise HTTPException(400, f"Prompt '{spec['name']}' expects {len(spec['arguments'])} argument(s): {', '.join(spec['arguments'])}.")
        try:
            msgs = await s.client.get_prompt(p["server"], p["name"], arguments=args)
        except Exception as e:  # e.g. the prompt rejected its arguments (bad dates)
            raise HTTPException(400, "Couldn't run that prompt: " + redact(root_cause(e))[:300])
        message = msgs[0].content
        used_prompt = {"server": p["server"], "name": p["name"], "args": args}
    elif body.resource:
        r = dict(body.resource)
        r["uri"] = str(r.get("uri") or "").rstrip("/")
        if not any((x["server"], x["uri"]) == (r.get("server"), r["uri"]) for x in s.resources):
            raise HTTPException(400, f"Unknown resource '{r.get('uri')}' on server '{r.get('server')}'. Type /resources to see what's available.")
        blobs = await s.client.get_resources(server_name=r["server"], uris=[r["uri"]])
        content = blobs[0].as_string()[:MAX_RESOURCE_CHARS] if blobs else ""
        if not content:
            raise HTTPException(404, "That resource is empty or could not be loaded.")
        instruction = redact(str(r.get("instruction") or "").strip()[:MAX_MESSAGE_CHARS])
        # Same message format as the terminal client (mcp_client.py)
        if instruction:
            message = f"CONTEXT from a loaded resource:\n---\n{content}\n---\nTASK: {instruction}"
        else:
            message = ("Please remember the following context for our conversation. "
                       f"Just acknowledge that you have received it.\n---\nCONTEXT:\n{content}\n---")
        used_resource = {"server": r["server"], "uri": r["uri"], "chars": len(content)}
    elif body.message and body.message.strip():
        message = redact(body.message.strip())  # a key pasted into the chat by mistake is never stored or sent on
    else:
        raise HTTPException(400, "Empty message.")

    if s.lock.locked():
        raise HTTPException(409, "Still working on your previous message.")
    async with s.lock:
        # The key lives only in this graph object, which goes out of scope when the request ends.
        agent = create_graph(s.tools, model=MODEL, api_key=key, checkpointer=checkpointer)
        try:
            res = await asyncio.wait_for(
                agent.ainvoke({"messages": [("user", message)]},
                              {"configurable": {"thread_id": s.id}, "recursion_limit": 25}),
                timeout=TURN_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise HTTPException(504, "The agent took too long to respond. Please try again.")
        except openai.OpenAIError as e:
            raise openai_error(e)
        except Exception as e:
            log.warning("chat failed: %s", redact(repr(e)))
            raise HTTPException(500, "Something went wrong: " + redact(str(e))[:300])
        finally:
            del agent

    # Report only this turn's tool calls (everything after the last user message).
    msgs = res["messages"]
    start = max(i for i, m in enumerate(msgs) if isinstance(m, HumanMessage))
    turn = msgs[start + 1:]
    results = {m.tool_call_id: m for m in turn if isinstance(m, ToolMessage)}
    steps = [{"server": s.tool_server.get(c["name"], "?"), "tool": c["name"], "args": c["args"],
              "ok": getattr(results.get(c["id"]), "status", "success") != "error"}
             for m in turn if isinstance(m, AIMessage) for c in m.tool_calls]
    answer = msgs[-1].content if isinstance(msgs[-1].content, str) else str(msgs[-1].content)
    return {"answer": answer, "steps": steps, "prompt": used_prompt, "resource": used_resource, "tasks": s.tasks()}


@app.delete("/api/session")
async def end_session(x_session_id: str | None = Header(default=None)):
    drop_session(x_session_id or "")
    return {"ok": True}


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
