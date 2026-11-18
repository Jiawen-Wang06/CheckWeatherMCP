// MCP DayPlanner – browser client.
// The OpenAI key is kept in sessionStorage only (cleared when the tab closes) and sent in a header.
"use strict";

const KEY_STORE = "openai_key";
const SESSION_STORE = "session_id";
const $ = (id) => document.getElementById(id);

const state = { sessionId: sessionStorage.getItem(SESSION_STORE), busy: false, pendingSend: null, tasks: [] };

function getKey() { try { return sessionStorage.getItem(KEY_STORE) || ""; } catch { return ""; } }
function setKey(k) { try { k ? sessionStorage.setItem(KEY_STORE, k) : sessionStorage.removeItem(KEY_STORE); } catch {} }

// ---------------------------------------------------------------- API

async function api(path, { method = "GET", body, withKey = false, keyOverride } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.sessionId) headers["X-Session-Id"] = state.sessionId;
  if (withKey) headers["X-OpenAI-Key"] = keyOverride || getKey();
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  let data = {};
  try { data = await res.json(); } catch {}
  if (!res.ok) { const err = new Error(data.error || `Request failed (${res.status})`); err.status = res.status; throw err; }
  return data;
}

async function ensureSession() {
  if (state.sessionId) return;
  const data = await api("/api/session", { method: "POST" });
  state.sessionId = data.session_id;
  state.caps = null;
  sessionStorage.setItem(SESSION_STORE, data.session_id);
  $("modelName").textContent = data.model;
  if (data.repo_url) {
    const link = (text) => { const a = document.createElement("a"); a.href = data.repo_url; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = text; return a; };
    $("sourceLink").replaceChildren(" You can ", link("check the source code"), " to verify.");
    $("aboutRepo").replaceChildren("Source code: ", link(data.repo_url));
  }
}

// ---------------------------------------------------------------- rendering

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Minimal, safe markdown: everything is HTML-escaped first, then a few patterns are formatted.
function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  const out = [];
  let list = null, table = [];
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; } };
  const flushTable = () => { if (table.length) { out.push(`<pre>${table.join("\n")}</pre>`); table = []; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (/^\s*\|/.test(line)) { flushList(); table.push(line); continue; }
    flushTable();
    let m;
    if ((m = line.match(/^\s*[-*]\s+(.*)/))) { if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; } list.items.push(m[1]); continue; }
    if ((m = line.match(/^\s*\d+\.\s+(.*)/))) { if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; } list.items.push(m[1]); continue; }
    flushList();
    if ((m = line.match(/^#{1,6}\s+(.*)/))) { out.push(`<p><strong>${inline(m[1])}</strong></p>`); continue; }
    if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  flushList(); flushTable();
  return out.join("");
}

function scrollDown() { const m = $("messages"); m.scrollTop = m.scrollHeight; }

function addMessage(kind, text, { markdown = false } = {}) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  if (markdown) div.innerHTML = renderMarkdown(text); else div.textContent = text;
  $("messages").appendChild(div);
  scrollDown();
  return div;
}

function formatArgs(args) {
  return Object.entries(args || {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
}

function addSteps(steps, prompt, resource) {
  if (!steps.length && !prompt && !resource) return;
  const wrap = document.createElement("div");
  wrap.className = "steps";
  if (resource) {
    const el = document.createElement("div");
    el.className = `step ${resource.server}`;
    el.append(Object.assign(document.createElement("span"), { className: "server", textContent: `📄 ${resource.server}` }),
      ` · loaded resource ${resource.uri} (${resource.chars.toLocaleString()} characters)`);
    wrap.appendChild(el);
  }
  if (prompt) {
    const el = document.createElement("div");
    el.className = `step ${prompt.server}`;
    el.append(Object.assign(document.createElement("span"), { className: "server", textContent: `📝 ${prompt.server}` }),
      ` · MCP prompt ${prompt.name}(${formatArgs(prompt.args)})`);
    wrap.appendChild(el);
  }
  for (const s of steps) {
    const el = document.createElement("div");
    el.className = `step ${s.server}${s.ok ? "" : " failed"}`;
    el.append(Object.assign(document.createElement("span"), { className: "server", textContent: `🔧 ${s.server}` }),
      ` · ${s.tool}(${formatArgs(s.args)})${s.ok ? "" : " ⚠️ error"}`);
    wrap.appendChild(el);
  }
  $("messages").appendChild(wrap);
  scrollDown();
}

function renderTasks(tasks) {
  const ul = $("taskList");
  const before = new Set(state.tasks);
  ul.replaceChildren();
  if (!tasks.length) { ul.appendChild(Object.assign(document.createElement("li"), { className: "empty", textContent: "No tasks yet." })); }
  for (const t of tasks) {
    const li = document.createElement("li");
    li.textContent = t;
    if (!before.has(t)) li.className = "new";
    ul.appendChild(li);
  }
  state.tasks = tasks;
}

function setBusy(busy) {
  state.busy = busy;
  $("sendBtn").disabled = busy;
  document.querySelectorAll("#examples button").forEach((b) => (b.disabled = busy));
}

// ---------------------------------------------------------------- actions

async function send(payload, label) {
  if (state.busy) return;
  if (!getKey()) { state.pendingSend = { payload, label }; openKeyDialog(); return; }
  setBusy(true);
  addMessage("user", label);
  const thinking = addMessage("ai thinking", "Thinking…");
  try {
    await ensureSession();
    let data;
    try {
      data = await api("/api/chat", { method: "POST", body: payload, withKey: true });
    } catch (e) {
      if (e.status !== 404) throw e;
      // Session expired on the server: start a fresh one and retry once.
      state.sessionId = null; sessionStorage.removeItem(SESSION_STORE);
      await ensureSession();
      addMessage("error", "Your previous session expired, so a new one was started (earlier tasks and chat history were cleared).");
      data = await api("/api/chat", { method: "POST", body: payload, withKey: true });
    }
    thinking.remove();
    addSteps(data.steps || [], data.prompt, data.resource);
    addMessage("ai", data.answer || "(no answer)", { markdown: true });
    renderTasks(data.tasks || []);
  } catch (e) {
    thinking.remove();
    addMessage("error", e.message);
    if (e.status === 401) { setKey(""); openKeyDialog(); }
  } finally {
    setBusy(false);
    $("input").focus();
  }
}

// ---------------------------------------------------------------- slash commands (same as the terminal client)

const COMMANDS = [
  ["/prompts", "list the MCP prompts each server offers"],
  ["/prompt <server> <prompt_name> \"arg1\" \"arg2\" …", "run an MCP prompt"],
  ["/resources", "list the MCP resources each server offers"],
  ["/resource <server> <resource_uri> [what to do with it]", "load a resource; without an instruction it's added to the conversation context"],
  ["/help", "show these commands"],
];

// Split on spaces, keeping "quoted strings" (or 'single-quoted') together.
function tokenize(text) {
  const out = [];
  for (const m of text.matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)) out.push(m[1] ?? m[2] ?? m[3]);
  return out;
}

async function getCapabilities() {
  await ensureSession();
  if (!state.caps) state.caps = await api("/api/capabilities");
  return state.caps;
}

// A system message: built with DOM nodes (no innerHTML), so server-provided names/descriptions can't inject HTML.
function addPanel(title, build) {
  const div = document.createElement("div");
  div.className = "msg ai panel";
  div.appendChild(Object.assign(document.createElement("p"), { className: "panel-title", textContent: title }));
  build(div);
  $("messages").appendChild(div);
  scrollDown();
}

// Clickable example: fills the message box so the user can edit and send it.
function usageChip(text) {
  const b = Object.assign(document.createElement("button"), { type: "button", className: "usage", textContent: text, title: "Click to put this in the message box" });
  b.addEventListener("click", () => { $("input").value = text; $("input").focus(); });
  return b;
}

function groupByServer(items) {
  const groups = new Map();
  for (const it of items) { if (!groups.has(it.server)) groups.set(it.server, []); groups.get(it.server).push(it); }
  return groups;
}

// "+N" -> the date N days from today (local), as YYYY-MM-DD
function relativeDate(value) {
  const m = /^\+(\d+)$/.exec(String(value));
  if (!m) return value;
  const d = new Date();
  d.setDate(d.getDate() + Number(m[1]));
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const EXAMPLE_ARGS = { destination: "Montreal", duration_in_days: "3", location_a: "Toronto", location_b: "Vancouver",
  from_date: "+1", to_date: "+3" };

function showHelp() {
  addPanel("Commands", (div) => {
    const ul = document.createElement("ul");
    for (const [cmd, desc] of COMMANDS) {
      const li = document.createElement("li");
      li.append(Object.assign(document.createElement("code"), { textContent: cmd }), ` — ${desc}`);
      ul.appendChild(li);
    }
    div.appendChild(ul);
    div.appendChild(Object.assign(document.createElement("p"), { textContent: "Or just ask in plain English: the agent picks the right tools itself." }));
  });
}

async function showPrompts() {
  const { prompts } = await getCapabilities();
  addPanel("MCP prompts", (div) => {
    if (!prompts.length) { div.appendChild(Object.assign(document.createElement("p"), { textContent: "No prompts found on any server." })); return; }
    for (const [server, items] of groupByServer(prompts)) {
      div.appendChild(Object.assign(document.createElement("p"), { className: "panel-server", textContent: `Server: ${server}` }));
      const ul = document.createElement("ul");
      for (const p of items) {
        const li = document.createElement("li");
        li.append(Object.assign(document.createElement("code"), { textContent: p.name }),
          ` (${p.arguments.length ? "arguments: " + p.arguments.join(", ") : "no arguments"})`);
        if (p.description) li.append(Object.assign(document.createElement("div"), { className: "desc", textContent: p.description }));
        const example = `/prompt ${server} ${p.name} ${p.arguments.map((a) => `"${relativeDate(EXAMPLE_ARGS[a] ?? a)}"`).join(" ")}`.trim();
        li.append(usageChip(example));
        ul.appendChild(li);
      }
      div.appendChild(ul);
    }
    div.appendChild(Object.assign(document.createElement("p"), { className: "desc", textContent: 'Use: /prompt <server_name> <prompt_name> "arg1" "arg2" … (click an example to try it)' }));
  });
}

async function showResources() {
  const { resources } = await getCapabilities();
  addPanel("MCP resources", (div) => {
    if (!resources.length) { div.appendChild(Object.assign(document.createElement("p"), { textContent: "No resources found on any server." })); return; }
    for (const [server, items] of groupByServer(resources)) {
      div.appendChild(Object.assign(document.createElement("p"), { className: "panel-server", textContent: `Server: ${server}` }));
      const ul = document.createElement("ul");
      for (const r of items) {
        const li = document.createElement("li");
        li.append(Object.assign(document.createElement("code"), { textContent: r.uri }));
        if (r.description) li.append(Object.assign(document.createElement("div"), { className: "desc", textContent: r.description }));
        li.append(usageChip(`/resource ${server} ${r.uri} Summarize the key points`));
        ul.appendChild(li);
      }
      div.appendChild(ul);
    }
    div.appendChild(Object.assign(document.createElement("p"), { className: "desc",
      textContent: "Use: /resource <server_name> <resource_uri> [what to do with it]. Without an instruction, the content is added to the conversation so you can ask about it next." }));
  });
}

async function runPromptCommand(text) {
  const [, server, name, ...args] = tokenize(text);
  if (!server || !name) { addMessage("error", 'Usage: /prompt <server_name> <prompt_name> "arg1" "arg2" …  (type /prompts to see what\'s available)'); return; }
  const { prompts } = await getCapabilities();
  const spec = prompts.find((p) => p.server === server && p.name === name);
  if (!spec) { addMessage("error", `Prompt '${name}' was not found on server '${server}'. Type /prompts to see what's available.`); return; }
  if (args.length !== spec.arguments.length) {
    addMessage("error", `Prompt '${name}' expects ${spec.arguments.length} argument(s): ${spec.arguments.join(", ")}. Put arguments that contain spaces in quotes.`);
    return;
  }
  const named = Object.fromEntries(spec.arguments.map((a, i) => [a, args[i]]));
  send({ prompt: { server, name, args: named } }, text);
}

async function runResourceCommand(text) {
  const m = text.match(/^\/resource\s+(\S+)\s+(\S+)\s*([\s\S]*)$/i);
  if (!m) { addMessage("error", "Usage: /resource <server_name> <resource_uri> [what to do with it]  (type /resources to see what's available)"); return; }
  const [, server, rawUri, instruction] = m;
  const uri = rawUri.replace(/\/+$/, "");  // "file://meeting_notes/" and "file://meeting_notes" both work
  const { resources } = await getCapabilities();
  if (!resources.some((r) => r.server === server && r.uri === uri)) {
    addMessage("error", `Resource '${uri}' was not found on server '${server}'. Type /resources to see what's available.`);
    return;
  }
  send({ resource: { server, uri, instruction: instruction.trim() } }, text);
}

async function handleCommand(text) {
  const cmd = text.split(/\s+/, 1)[0].toLowerCase();
  try {
    if (cmd === "/help") { addMessage("user", text); showHelp(); }
    else if (cmd === "/prompts") { addMessage("user", text); await showPrompts(); }
    else if (cmd === "/resources") { addMessage("user", text); await showResources(); }
    else if (cmd === "/prompt") await runPromptCommand(text);
    else if (cmd === "/resource") await runResourceCommand(text);
    else addMessage("error", `Unknown command '${cmd}'. Type /help to see the available commands.`);
  } catch (e) {
    addMessage("error", e.message);
  }
}

function openKeyDialog() {
  $("keyError").textContent = "";
  $("keyInput").value = "";
  $("forgetKeyBtn").hidden = !getKey();
  $("keyDialog").showModal();
  $("keyInput").focus();
}

async function saveKey(ev) {
  ev.preventDefault();
  const key = $("keyInput").value.trim();
  $("keyError").textContent = "";
  $("keySaveBtn").disabled = true;
  $("keySaveBtn").textContent = "Checking…";
  try {
    await api("/api/key/check", { method: "POST", withKey: true, keyOverride: key });
    setKey(key);
    $("keyInput").value = "";
    $("keyDialog").close();
    if (state.pendingSend) { const p = state.pendingSend; state.pendingSend = null; send(p.payload, p.label); }
  } catch (e) {
    $("keyError").textContent = e.message;
  } finally {
    $("keySaveBtn").disabled = false;
    $("keySaveBtn").textContent = "Check & use key";
  }
}

async function resetChat() {
  if (state.busy) return;
  if (state.sessionId) { try { await api("/api/session", { method: "DELETE" }); } catch {} }
  state.sessionId = null;
  state.caps = null;
  sessionStorage.removeItem(SESSION_STORE);
  document.querySelectorAll("#messages > :not(.intro)").forEach((el) => el.remove());
  renderTasks([]);
}

// ---------------------------------------------------------------- wiring

document.addEventListener("DOMContentLoaded", () => {
  $("composer").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const text = $("input").value.trim();
    if (!text || state.busy) return;
    $("input").value = "";
    if (text.startsWith("/")) handleCommand(text); else send({ message: text }, text);
  });
  $("input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("composer").requestSubmit(); }
  });
  $("examples").addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.cmd) { handleCommand(b.dataset.cmd); return; }
    if (b.dataset.prompt) {
      const [server, name] = b.dataset.prompt.split("/");
      const args = Object.fromEntries(Object.entries(JSON.parse(b.dataset.args || "{}")).map(([k, v]) => [k, relativeDate(v)]));
      send({ prompt: { server, name, args } }, `/prompt ${server} ${name} ${Object.values(args).map((v) => `"${v}"`).join(" ")}`);
    } else {
      send({ message: b.dataset.msg }, b.dataset.msg);
    }
  });
  $("keyForm").addEventListener("submit", saveKey);
  $("keyBtn").addEventListener("click", openKeyDialog);
  $("keyCancelBtn").addEventListener("click", () => { state.pendingSend = null; $("keyDialog").close(); });
  $("forgetKeyBtn").addEventListener("click", () => { setKey(""); $("keyDialog").close(); addMessage("ai", "Your API key was removed from this tab."); });
  $("resetBtn").addEventListener("click", resetChat);
  $("aboutBtn").addEventListener("click", () => $("aboutDialog").showModal());
  $("aboutClose").addEventListener("click", () => $("aboutDialog").close());

  // Restore tasks for an existing session (e.g. after a page refresh).
  if (state.sessionId) {
    api("/api/tasks").then((d) => renderTasks(d.tasks)).catch(() => { state.sessionId = null; sessionStorage.removeItem(SESSION_STORE); });
  }
  ensureSession().catch(() => {});
  if (!getKey()) openKeyDialog();
});
