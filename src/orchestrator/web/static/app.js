// Agency Console: a thin client over the orchestrator API. No dependencies, no build.
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  mode: "single",
  threadId: null,
  agents: [],
  pinned: [], // single mode: at most one agent; team mode: up to 8
  busy: false,
};

const store = {
  get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* storage unavailable */ } },
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
}

// --- minimal, safe markdown: escape first, then format -----------------------
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" rel="noopener noreferrer" target="_blank">$1</a>');
}
function renderMarkdown(src) {
  const out = [];
  const lines = escapeHtml(src || "").split("\n");
  let list = null;
  let para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join("<br>"))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("```")) {
      flushPara(); flushList();
      const code = [];
      while (++i < lines.length && !lines[i].startsWith("```")) code.push(lines[i]);
      out.push(`<pre><code>${code.join("\n")}</code></pre>`);
      continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (h) { flushPara(); flushList(); const n = Math.min(h[1].length + 1, 4); out.push(`<h${n}>${inline(h[2])}</h${n}>`); }
    else if (ul || ol) {
      flushPara();
      const kind = ul ? "ul" : "ol";
      if (list !== kind) { flushList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline((ul || ol)[1])}</li>`);
    } else if (!line.trim()) { flushPara(); flushList(); }
    else { flushList(); para.push(line); }
  }
  flushPara(); flushList();
  return out.join("");
}
function markdownNode(text, cls = "answer") {
  const div = el("div", { class: cls });
  div.innerHTML = renderMarkdown(text); // input is HTML-escaped before formatting
  return div;
}

// --- API ---------------------------------------------------------------------
function headers() {
  const h = { "Content-Type": "application/json" };
  const key = $("#api-key").value.trim();
  if (key) h["X-API-Key"] = key;
  return h;
}
async function apiError(resp) {
  let detail = `${resp.status} ${resp.statusText}`;
  try {
    const body = await resp.json();
    detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
  } catch { /* not JSON */ }
  if (resp.status === 401) detail = "Invalid or missing API key.";
  if (resp.status === 429) detail = "Rate limit reached. Try again in a minute.";
  return new Error(detail);
}

async function loadAgents() {
  $("#agents-status").textContent = "Loading agents…";
  try {
    const resp = await fetch("/v1/agents", { headers: headers() });
    if (!resp.ok) throw await apiError(resp);
    state.agents = await resp.json();
    const divisions = [...new Set(state.agents.map((a) => a.division))].sort();
    const select = $("#division");
    select.replaceChildren(el("option", { value: "", text: "All divisions" }));
    for (const d of divisions) select.append(el("option", { value: d, text: d.replace(/-/g, " ") }));
    renderAgents();
  } catch (err) {
    $("#agents-status").textContent = `Could not load agents: ${err.message}`;
  }
}

async function* sse(resp) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let event = "message";
      const data = [];
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trim());
      }
      if (data.length) yield { event, data: JSON.parse(data.join("\n")) };
    }
  }
}

// --- catalog -----------------------------------------------------------------
function renderAgents() {
  const q = $("#search").value.trim().toLowerCase();
  const division = $("#division").value;
  const matches = state.agents.filter((a) =>
    (!division || a.division === division) &&
    (!q || `${a.name} ${a.description} ${a.id}`.toLowerCase().includes(q)));
  const list = $("#agents");
  list.replaceChildren(...matches.map((a) => el("li", {},
    el("button", {
      type: "button", class: "agent", "aria-pressed": String(state.pinned.includes(a.id)),
      title: a.description, onclick: () => togglePin(a.id),
    },
      el("span", { class: "agent-name", text: `${a.emoji ? a.emoji + " " : ""}${a.name}` }),
      el("span", { class: "agent-meta", text: `${a.division} · ${a.description}` })))));
  $("#agents-status").textContent = `${matches.length} of ${state.agents.length} agents`;
  renderPinned();
}

function renderPinned() {
  const byId = Object.fromEntries(state.agents.map((a) => [a.id, a]));
  $("#pin-hint").textContent = state.mode === "team"
    ? "Pick agents to build the team yourself, or leave empty and the planner will staff it."
    : "Pick an agent to talk to it directly, or leave empty for automatic routing.";
  $("#pinned").replaceChildren(...state.pinned.map((id) =>
    el("button", { type: "button", class: "chip", title: "Remove", onclick: () => togglePin(id) },
      byId[id]?.name || id, " ×")));
}

function togglePin(id) {
  if (state.pinned.includes(id)) state.pinned = state.pinned.filter((p) => p !== id);
  else if (state.mode === "single") state.pinned = [id];
  else if (state.pinned.length < 8) state.pinned = [...state.pinned, id];
  renderAgents();
}

function setMode(mode) {
  state.mode = mode;
  for (const b of document.querySelectorAll(".mode button")) {
    b.setAttribute("aria-checked", String(b.dataset.mode === mode));
  }
  if (mode === "single" && state.pinned.length > 1) state.pinned = state.pinned.slice(0, 1);
  renderAgents();
}

// --- conversation ------------------------------------------------------------
function addMessage(role) {
  $("#empty")?.remove();
  const msg = el("div", { class: `msg ${role}` });
  $("#thread").append(msg);
  return msg;
}
function scrollDown() { const t = $("#thread"); t.scrollTop = t.scrollHeight; }

function usageLine(usage) {
  const t = usage?.total;
  if (!t) return null;
  const cost = t.cost_usd ? ` · $${t.cost_usd.toFixed(4)}` : "";
  return el("span", { text: `${t.llm_calls} LLM calls · ${t.input_tokens + t.output_tokens} tokens${cost}` });
}

function planView(plan) {
  const view = el("div", { class: "plan" });
  view.append(el("div", { class: "plan-head" },
    el("b", { text: `Team of ${plan.steps.length}` }),
    el("span", { class: "badge", text: plan.method }),
    plan.reasoning ? el("span", { class: "muted", text: plan.reasoning }) : null));
  const steps = el("ol", { class: "steps" });
  for (const s of plan.steps) {
    const deps = s.depends_on.length ? ` · after ${s.depends_on.join(", ")}` : "";
    steps.append(el("li", { class: "step", "data-step": s.id, "data-status": s.depends_on.length ? "pending" : "running" },
      el("span", { class: "dot", "aria-hidden": "true" }),
      el("div", {},
        el("div", {}, el("b", { text: s.agent_name }), el("span", { class: "muted", text: ` ${s.id}${deps}` })),
        el("div", { class: "step-task", text: s.task }),
        el("div", { class: "step-slot" })),
      el("span", { class: "muted small step-time" })));
  }
  view.append(steps);
  return view;
}

function markStep(view, plan, result) {
  const li = view.querySelector(`[data-step="${CSS.escape(result.step_id)}"]`);
  if (!li) return;
  li.dataset.status = result.error ? "failed" : "done";
  li.querySelector(".step-time").textContent = result.duration_s != null ? `${result.duration_s}s` : "";
  const slot = li.querySelector(".step-slot");
  if (result.error) slot.append(el("div", { class: "error small", text: `Failed: ${result.error}` }));
  else slot.append(el("details", {}, el("summary", { text: "Contribution" }), markdownNode(result.output, "answer step-out")));
  // Steps whose dependencies are now all finished start running.
  const finished = new Set([...view.querySelectorAll('[data-status="done"], [data-status="failed"]')].map((n) => n.dataset.step));
  for (const s of plan.steps) {
    const node = view.querySelector(`[data-step="${CSS.escape(s.id)}"]`);
    if (node.dataset.status === "pending" && s.depends_on.every((d) => finished.has(d))) node.dataset.status = "running";
  }
}

async function send(question) {
  if (state.busy || !question.trim()) return;
  state.busy = true;
  $("#send").disabled = true;

  addMessage("user").append(el("div", { class: "bubble", text: question }));
  const reply = addMessage("assistant");
  const meta = el("div", { class: "meta" });
  const working = el("div", { class: "working", text: state.mode === "team" ? "Planning the team…" : "Routing…" });
  const bubble = el("div", { class: "bubble" }, working);
  reply.append(meta, bubble);
  scrollDown();

  const body = { question, mode: state.mode };
  if (state.threadId) body.thread_id = state.threadId;
  if (state.mode === "single" && state.pinned.length) body.agent_id = state.pinned[0];
  if (state.mode === "team" && state.pinned.length) body.agent_ids = state.pinned;

  let plan = null;
  let planNode = null;
  let finishedSteps = 0;
  try {
    const resp = await fetch("/v1/chat/stream", { method: "POST", headers: headers(), body: JSON.stringify(body) });
    if (!resp.ok) throw await apiError(resp);
    for await (const { event, data } of sse(resp)) {
      if (event === "start") state.threadId = data.thread_id;
      else if (event === "knowledge") {
        meta.append(el("span", { class: "badge", text: `${data.sources.length} company source${data.sources.length === 1 ? "" : "s"}` }));
      } else if (event === "routing") {
        meta.append(el("span", { text: `→ ${data.agent_name}` }),
          el("span", { class: "badge", text: `${data.method} · ${Math.round(data.confidence * 100)}%` }));
        working.textContent = `${data.agent_name} is working…`;
      } else if (event === "plan") {
        plan = data;
        planNode = planView(plan);
        reply.insertBefore(planNode, bubble);
        working.textContent = "Specialists are working…";
      } else if (event === "step" && planNode) {
        markStep(planNode, plan, data);
        if (++finishedSteps === plan.steps.length) {
          working.textContent = plan.steps.length > 1 ? "Synthesizing the team's answer…" : "Finishing…";
        }
      } else if (event === "done") {
        if (data.blocked) {
          bubble.replaceChildren(el("div", { class: "error", text: `Blocked by guardrails: ${data.guardrails.reasons.join(", ")}` }));
        } else {
          bubble.replaceChildren(markdownNode(data.answer));
          const sources = sourcesView(data.sources);
          if (sources) bubble.append(sources);
        }
        const usage = usageLine(data.usage);
        if (usage) meta.append(usage);
        if (data.guardrails.flags.length) meta.append(el("span", { class: "badge", text: `guardrails: ${data.guardrails.flags.join(", ")}` }));
      } else if (event === "error") {
        throw new Error(data.message);
      }
      scrollDown();
    }
  } catch (err) {
    bubble.replaceChildren(el("div", { class: "error", text: err.message }));
  } finally {
    state.busy = false;
    $("#send").disabled = false;
    scrollDown();
  }
}

// --- knowledge base ----------------------------------------------------------
async function loadDocs() {
  const status = $("#docs-status");
  try {
    const resp = await fetch("/v1/knowledge/documents", { headers: headers() });
    if (!resp.ok) throw await apiError(resp);
    const docs = await resp.json();
    $("#docs").replaceChildren(...docs.map((d) => el("li", { class: "doc" },
      el("div", { class: "doc-body" },
        el("span", { class: "doc-title", text: d.title, title: d.title }),
        el("span", { class: "muted small", text: `${d.chunks} chunk${d.chunks === 1 ? "" : "s"} · ${d.doc_id}` })),
      el("button", {
        type: "button", class: "icon-btn", "aria-label": `Delete ${d.title}`, title: "Delete",
        onclick: () => deleteDoc(d),
      }, "✕"))));
    status.textContent = docs.length ? `${docs.length} document${docs.length === 1 ? "" : "s"}` : "No documents yet.";
  } catch (err) {
    status.textContent = `Could not load documents: ${err.message}`;
  }
}

async function addDoc(title, text) {
  const resp = await fetch("/v1/knowledge/documents", {
    method: "POST", headers: headers(), body: JSON.stringify({ title, text }),
  });
  if (!resp.ok) throw await apiError(resp);
  return resp.json();
}

async function uploadFiles(files) {
  const status = $("#docs-status");
  for (const file of files) {
    status.textContent = `Indexing ${file.name}…`;
    try {
      const info = await addDoc(file.name.replace(/\.[^.]+$/, ""), await file.text());
      status.textContent = `Indexed ${file.name} (${info.chunks} chunks).`;
    } catch (err) {
      status.textContent = `${file.name}: ${err.message}`;
      return;
    }
  }
  await loadDocs();
}

async function deleteDoc(doc) {
  if (!confirm(`Delete "${doc.title}" from the knowledge base?`)) return;
  const resp = await fetch(`/v1/knowledge/documents/${encodeURIComponent(doc.doc_id)}`, {
    method: "DELETE", headers: headers(),
  });
  if (!resp.ok && resp.status !== 404) $("#docs-status").textContent = (await apiError(resp)).message;
  await loadDocs();
}

function sourcesView(sources) {
  if (!sources?.length) return null;
  return el("details", { class: "sources" },
    el("summary", { text: `Sources from your documents (${sources.length})` }),
    el("ol", {}, ...sources.map((s) => el("li", { value: String(s.n) },
      el("b", { text: s.title }),
      el("div", { class: "excerpt", text: `${s.excerpt}${s.excerpt.length >= 300 ? "…" : ""}` })))));
}

function selectTab(name) {
  for (const tab of ["agents", "knowledge"]) {
    $(`#tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    $(`#panel-${tab}`).hidden = tab !== name;
  }
  if (name === "knowledge") loadDocs();
}

// --- wiring ------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  const key = $("#api-key");
  key.value = store.get("agency.apiKey") || "";
  key.addEventListener("change", () => {
    store.set("agency.apiKey", key.value.trim());
    loadAgents();
    if (!$("#panel-knowledge").hidden) loadDocs();
  });

  $("#tab-agents").addEventListener("click", () => selectTab("agents"));
  $("#tab-knowledge").addEventListener("click", () => selectTab("knowledge"));
  $("#doc-file").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
  $("#doc-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const info = await addDoc($("#doc-title").value.trim(), $("#doc-text").value);
      $("#docs-status").textContent = `Indexed "${info.title}" (${info.chunks} chunks).`;
      e.target.reset();
      await loadDocs();
    } catch (err) {
      $("#docs-status").textContent = err.message;
    }
  });

  for (const b of document.querySelectorAll(".mode button")) b.addEventListener("click", () => setMode(b.dataset.mode));
  $("#search").addEventListener("input", renderAgents);
  $("#division").addEventListener("change", renderAgents);
  $("#toggle-catalog").addEventListener("click", () => $("#catalog").classList.toggle("open"));
  $("#new-thread").addEventListener("click", () => {
    state.threadId = null;
    $("#thread").replaceChildren(el("p", { class: "muted small", text: "New conversation." }));
  });
  for (const b of document.querySelectorAll(".example")) {
    b.addEventListener("click", () => {
      if (b.hasAttribute("data-team")) setMode("team");
      send(b.textContent);
    });
  }
  const question = $("#question");
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = question.value;
    question.value = "";
    send(text);
  });
  question.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
  });

  renderPinned();
  loadAgents();
});
