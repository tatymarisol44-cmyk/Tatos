// Clinical workspace: patients, tests the psychologist designs and applies, documents.
// Loaded after app.js and uses its helpers ($, el, headers, apiError, friendlyError).
"use strict";

const clinic = { patients: [], instruments: [] };

async function api(method, path, body) {
  const init = { method, headers: headers() };
  if (body instanceof FormData) {
    delete init.headers["Content-Type"]; // the browser sets the multipart boundary
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(path, init);
  if (!resp.ok) throw await apiError(resp);
  return resp.status === 204 ? null : resp.json();
}

// --- the sidebar: patients ---------------------------------------------------------
async function loadPatients() {
  const status = $("#patients-status");
  status.textContent = "Loading patients…";
  try {
    clinic.patients = await api("GET", "/v1/crm/patients");
    renderPatients();
  } catch (err) {
    $("#patients").replaceChildren();
    status.textContent = err.status === 403
      ? "This key cannot see patients (needs a clinical or reception role)."
      : `Could not load patients: ${friendlyError(err)}`;
  }
}

function renderPatients() {
  const q = $("#patient-search").value.trim().toLowerCase();
  const shown = clinic.patients.filter((p) => !q || `${p.display_name} ${p.id}`.toLowerCase().includes(q));
  $("#patients").replaceChildren(...shown.map((p) =>
    el("li", {},
      el("button", { type: "button", class: "agent", onclick: () => openPatient(p) },
        el("span", { class: "agent-name", text: p.display_name }),
        el("span", { class: "muted small", text: p.id })))));
  $("#patients-status").textContent = `${shown.length} of ${clinic.patients.length} patients`;
}

// --- the main area: a workspace instead of the conversation -------------------------
function openWorkspace(title, ...content) {
  $("#thread").hidden = true;
  $("#composer").hidden = true;
  $("#composer-note").hidden = true;
  const back = el("button", { type: "button", class: "ghost", onclick: closeWorkspace }, "← Back to the assistant");
  const heading = el("h1", { class: "ws-title", tabindex: "-1", text: title });
  $("#workspace").replaceChildren(el("div", { class: "ws-head" }, back, heading), ...content);
  $("#workspace").hidden = false;
  heading.focus(); // screen readers announce the new view
}

function closeWorkspace() {
  $("#workspace").hidden = true;
  $("#workspace").replaceChildren();
  $("#thread").hidden = false;
  $("#composer").hidden = false;
}

function section(title, ...children) {
  return el("section", { class: "ws-section" }, el("h2", { text: title }), ...children);
}

function statusLine() {
  return el("p", { class: "muted small", role: "status" });
}

// --- a patient's record: apply a test, results, documents ----------------------------
async function openPatient(patient) {
  const results = el("div", { class: "ws-list" }, loading("results"));
  const files = el("ul", { class: "ws-list" }, el("li", {}, loading("documents")));
  const apply = el("div", {}, loading("tests"));
  openWorkspace(`${patient.display_name} · ${patient.id}`,
    section("Apply a test", apply),
    section("Results", results),
    section("Documents", uploadForm(patient, files), files));
  await Promise.all([renderApply(patient, apply, results), renderResults(patient, results), renderFiles(patient, files)]);
}

async function loadInstruments() {
  clinic.instruments = await api("GET", "/v1/clinical/instruments");
  return clinic.instruments;
}

async function renderApply(patient, box, results) {
  const msg = statusLine();
  let instruments;
  try {
    instruments = await loadInstruments();
  } catch (err) {
    box.replaceChildren(el("p", { class: "error", role: "alert", text: friendlyError(err) }));
    return;
  }
  if (!instruments.length) {
    box.replaceChildren(el("p", { class: "muted", text: "No tests yet. Use “Manage tests” to create one or add PHQ-9 / GAD-7." }));
    return;
  }
  const select = el("select", { id: "apply-instrument" },
    ...instruments.map((i) => el("option", { value: i.instrument_id, text: `${i.spec.name} (v${i.version})` })));
  const form = el("form", { class: "ws-form" });
  const draw = () => {
    const inst = instruments.find((i) => i.instrument_id === select.value);
    form.replaceChildren(...testForm(inst.spec), el("label", { class: "field" }, "Note (optional)",
      el("textarea", { name: "__note", rows: "2", maxlength: "2000" })),
      el("button", { type: "submit", class: "approve" }, "Save result"), msg);
  };
  select.addEventListener("change", draw);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const inst = instruments.find((i) => i.instrument_id === select.value);
    const data = new FormData(form);
    const answers = {};
    for (const item of inst.spec.items) {
      const raw = data.get(item.id);
      if (raw === null || raw === "") continue;
      answers[item.id] = item.type === "text" ? String(raw) : Number(raw);
    }
    msg.textContent = "Saving…";
    try {
      const result = await api("POST", `/v1/clinical/patients/${encodeURIComponent(patient.id)}/instrument-results`,
        { instrument_id: inst.instrument_id, answers, note: data.get("__note") || null });
      msg.replaceChildren(resultCard(result));
      form.reset();
      await renderResults(patient, results);
    } catch (err) {
      msg.replaceChildren(el("span", { class: "error", role: "alert", text: friendlyError(err) }));
    }
  });
  box.replaceChildren(el("label", { class: "field", for: "apply-instrument" }, "Test"), select, form);
  draw();
}

// One fieldset per item: radio buttons, a number, or an open answer.
function testForm(spec) {
  const out = [];
  if (spec.instructions) out.push(el("p", { class: "muted", text: spec.instructions }));
  spec.items.forEach((item, n) => {
    const legend = el("legend", { text: `${n + 1}. ${item.text}${item.required === false ? " (optional)" : ""}` });
    if (item.type === "choice") {
      out.push(el("fieldset", { class: "item" }, legend, ...item.options.map((o, k) => {
        const id = `ans-${item.id}-${k}`;
        const radio = el("input", { type: "radio", id, name: item.id, value: String(o.value) });
        if (item.required !== false) radio.required = true;
        return el("label", { class: "choice", for: id }, radio, ` ${o.label}`);
      })));
    } else if (item.type === "number") {
      const input = el("input", { type: "number", name: item.id, step: "any", "aria-label": item.text });
      if (item.min !== null && item.min !== undefined) input.min = String(item.min);
      if (item.max !== null && item.max !== undefined) input.max = String(item.max);
      if (item.required !== false) input.required = true;
      out.push(el("fieldset", { class: "item" }, legend, input));
    } else {
      out.push(el("fieldset", { class: "item" }, legend,
        el("textarea", { name: item.id, rows: "2", maxlength: "4000", "aria-label": item.text })));
    }
  });
  return out;
}

function resultCard(r) {
  const alerts = (r.alerts || []).map((a) => el("p", { class: "alert", role: "alert", text: `⚠ ${a.message}` }));
  const subs = Object.entries(r.subscales || {}).map(([k, v]) => `${k}: ${v}`).join(" · ");
  return el("div", { class: `result severity-${r.severity || "none"}` },
    el("b", { text: `${r.instrument_name} (v${r.version})` }),
    el("span", { text: `Score ${r.total ?? "—"}${r.band ? ` · ${r.band}` : ""}${subs ? ` · ${subs}` : ""}` }),
    el("span", { class: "muted small", text: `${new Date(r.created_at).toLocaleString()} · severity band, not a diagnosis` }),
    ...alerts);
}

async function renderResults(patient, box) {
  try {
    const rows = await api("GET", `/v1/clinical/patients/${encodeURIComponent(patient.id)}/instrument-results`);
    box.replaceChildren(...(rows.length ? rows.slice().reverse().map(resultCard)
      : [el("p", { class: "muted", text: "No results yet." })]));
  } catch (err) {
    box.replaceChildren(el("p", { class: "error", role: "alert", text: friendlyError(err) }));
  }
}

function uploadForm(patient, list) {
  const msg = statusLine();
  const form = el("form", { class: "ws-form inline" },
    el("label", { class: "field" }, "File (PDF, PNG, JPEG or text, up to 10 MB)",
      el("input", { type: "file", name: "file", required: "", accept: ".pdf,.png,.jpg,.jpeg,.txt,application/pdf,image/png,image/jpeg,text/plain" })),
    el("label", { class: "field" }, "What is it",
      el("input", { name: "label", required: "", maxlength: "120", placeholder: "Signed informed consent" })),
    el("label", { class: "field" }, "Who can see it",
      el("select", { name: "access" },
        el("option", { value: "care_team", text: "The care team" }),
        el("option", { value: "author_only", text: "Only me (the author)" }))),
    el("button", { type: "submit", class: "ghost" }, "Upload"), msg);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    msg.textContent = "Uploading…";
    try {
      const info = await api("POST", `/v1/clinical/patients/${encodeURIComponent(patient.id)}/files`, new FormData(form));
      msg.textContent = `Saved “${info.filename}” · SHA-256 ${info.sha256.slice(0, 16)}…`;
      form.reset();
      await renderFiles(patient, list);
    } catch (err) {
      msg.replaceChildren(el("span", { class: "error", role: "alert", text: friendlyError(err) }));
    }
  });
  return form;
}

async function renderFiles(patient, list) {
  try {
    const rows = await api("GET", `/v1/clinical/patients/${encodeURIComponent(patient.id)}/files`);
    list.replaceChildren(...(rows.length ? rows.map((f) => el("li", { class: "doc" },
      el("div", { class: "doc-body" },
        el("span", { class: "doc-title", text: f.label }),
        el("span", { class: "muted small", text: `${f.filename} · ${(f.size / 1024).toFixed(1)} KB · ${f.access === "author_only" ? "only the author" : "care team"} · SHA-256 ${f.sha256.slice(0, 12)}…` })),
      el("button", { type: "button", class: "ghost", onclick: () => download(f) }, `Download ${f.filename}`)))
      : [el("li", { class: "muted", text: "No documents yet." })]));
  } catch (err) {
    list.replaceChildren(el("li", { class: "error", role: "alert", text: friendlyError(err) }));
  }
}

// The key travels in a header, so the file is fetched and handed to the browser as a blob.
async function download(file) {
  const resp = await fetch(`/v1/clinical/files/${encodeURIComponent(file.file_id)}`, { headers: headers() });
  if (!resp.ok) { alert((await apiError(resp)).message); return; }
  const url = URL.createObjectURL(await resp.blob());
  const a = el("a", { href: url, download: file.filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// --- managing tests: list, templates, builder ----------------------------------------
const loading = (what) => el("p", { class: "muted", role: "status", text: `Loading ${what}…` });

async function openTests() {
  const list = el("div", { class: "ws-list" }, loading("tests"));
  const msg = statusLine();
  const templates = el("div", { class: "ws-row" },
    ...[["phq9", "Add PHQ-9 (depression)"], ["gad7", "Add GAD-7 (anxiety)"]].map(([t, label]) =>
      el("button", { type: "button", class: "ghost", onclick: async () => {
        try {
          await api("POST", "/v1/clinical/instruments/from-template", { template: t });
          msg.textContent = `${label.replace("Add ", "")} added.`;
          await renderTests(list);
        } catch (err) { msg.textContent = friendlyError(err); }
      } }, label)));
  openWorkspace("Tests",
    section("Your tests", list),
    section("Public-domain templates", templates, msg),
    section("Design a new test", builder(list)));
  await renderTests(list);
}

async function renderTests(box) {
  try {
    const rows = await loadInstruments();
    box.replaceChildren(...(rows.length ? rows.map((i) => el("div", { class: "doc" },
      el("div", { class: "doc-body" },
        el("span", { class: "doc-title", text: i.spec.name }),
        el("span", { class: "muted small", text: `v${i.version} · ${i.spec.items.length} items · ${i.visibility === "private" ? "private" : "shared with the practice"} · ${i.spec.licence.public_domain ? "public domain" : `licence: ${i.spec.licence.source}`}` })),
      el("button", { type: "button", class: "icon-btn", "aria-label": `Retire ${i.spec.name}`, onclick: async () => {
        try { await api("DELETE", `/v1/clinical/instruments/${encodeURIComponent(i.instrument_id)}`); await renderTests(box); }
        catch (err) { alert(friendlyError(err)); }
      } }, "Retire")))
      : [el("p", { class: "muted", text: "No tests yet." })]));
  } catch (err) {
    box.replaceChildren(el("p", { class: "error", role: "alert", text: friendlyError(err) }));
  }
}

const DEFAULT_OPTIONS = "Nunca = 0\nA veces = 1\nA menudo = 2\nSiempre = 3";

function itemEditor(n) {
  const id = `q${n}`;
  const type = el("select", { name: `type-${id}`, "aria-label": `Item ${id} type` },
    el("option", { value: "choice", text: "Choice" }), el("option", { value: "number", text: "Number" }),
    el("option", { value: "text", text: "Open answer" }));
  const options = el("textarea", { name: `options-${id}`, rows: "4", "aria-label": `Item ${id} options, one per line: label = value` });
  options.value = DEFAULT_OPTIONS;
  const row = el("fieldset", { class: "item", "data-item": id },
    el("legend", { text: `Item ${id}` }),
    el("label", { class: "field" }, "Question", el("input", { name: `text-${id}`, required: "", maxlength: "1000" })),
    el("label", { class: "field" }, "Type", type),
    el("label", { class: "field" }, "Options (one per line: label = value)", options),
    el("label", { class: "choice" }, el("input", { type: "checkbox", name: `reverse-${id}` }), " Reverse-scored"),
    el("label", { class: "choice" }, el("input", { type: "checkbox", name: `optional-${id}` }), " Optional"));
  type.addEventListener("change", () => { options.closest("label").hidden = type.value !== "choice"; });
  return row;
}

function builder(list) {
  let count = 0;
  const items = el("div", { class: "ws-list" });
  const addItem = () => items.append(itemEditor(++count));
  const msg = statusLine();
  const form = el("form", { class: "ws-form" },
    el("label", { class: "field" }, "Name", el("input", { name: "name", required: "", maxlength: "120" })),
    el("label", { class: "field" }, "Instructions for the patient", el("textarea", { name: "instructions", rows: "2", maxlength: "2000" })),
    items,
    el("button", { type: "button", class: "ghost", onclick: addItem }, "+ Add item"),
    el("label", { class: "field" }, "Scoring",
      el("select", { name: "method" }, el("option", { value: "sum", text: "Sum" }),
        el("option", { value: "mean", text: "Mean" }), el("option", { value: "none", text: "No score" }))),
    el("label", { class: "field" }, "Bands (one per line: min-max: label (none|low|moderate|high))",
      el("textarea", { name: "bands", rows: "3", placeholder: "0-4: mínimo (none)\n5-9: leve (low)\n10-30: alto (high)" })),
    el("label", { class: "field" }, "Alerts (one per line: item >= value: message)",
      el("textarea", { name: "alerts", rows: "2", placeholder: "q3 >= 2: revisar en la próxima sesión" })),
    el("label", { class: "field" }, "Subscales (one per line: name: q1, q2)",
      el("textarea", { name: "subscales", rows: "2", placeholder: "afectivo: q1, q2" })),
    el("label", { class: "field" }, "Source or author", el("input", { name: "source", required: "", maxlength: "300", placeholder: "Own instrument, or the publisher" })),
    el("label", { class: "choice" }, el("input", { type: "checkbox", name: "attestation", required: "" }),
      " I have the right to use this instrument in my practice"),
    el("label", { class: "field" }, "Who can use it",
      el("select", { name: "visibility" }, el("option", { value: "establishment", text: "Everyone in the practice" }),
        el("option", { value: "private", text: "Only me" }))),
    el("button", { type: "submit", class: "approve" }, "Save test"), msg);
  addItem();
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const spec = specFrom(new FormData(form), [...items.querySelectorAll("[data-item]")].map((n) => n.dataset.item));
      msg.textContent = "Saving…";
      const made = await api("POST", "/v1/clinical/instruments", { spec, visibility: new FormData(form).get("visibility") });
      msg.textContent = `Saved “${made.spec.name}” (v${made.version}).`;
      form.reset();
      items.replaceChildren();
      count = 0;
      addItem();
      await renderTests(list);
    } catch (err) {
      msg.replaceChildren(el("span", { class: "error", role: "alert", text: friendlyError(err) }));
    }
  });
  return form;
}

function lines(text) {
  return String(text || "").split("\n").map((l) => l.trim()).filter(Boolean);
}

// The form, as the definition the API expects (instruments.InstrumentSpec).
function specFrom(data, ids) {
  const items = ids.map((id) => {
    const type = data.get(`type-${id}`);
    const item = { id, text: String(data.get(`text-${id}`) || "").trim(), type,
      reverse: data.get(`reverse-${id}`) === "on", required: data.get(`optional-${id}`) !== "on" };
    if (type === "choice") {
      item.options = lines(data.get(`options-${id}`)).map((l) => {
        const [label, value] = l.split("=").map((s) => s.trim());
        if (!label || value === undefined || Number.isNaN(Number(value))) {
          throw new Error(`Item ${id}: write each option as “label = number”.`);
        }
        return { label, value: Number(value) };
      });
    } else {
      item.reverse = false;
    }
    return item;
  });
  const bands = lines(data.get("bands")).map((l) => {
    const m = l.match(/^(-?[\d.]+)\s*-\s*(-?[\d.]+)\s*:\s*(.+?)(?:\s*\((none|low|moderate|high)\))?$/);
    if (!m) throw new Error(`Band “${l}”: write it as “min-max: label (severity)”.`);
    return { min: Number(m[1]), max: Number(m[2]), label: m[3], severity: m[4] || "none" };
  });
  const alerts = lines(data.get("alerts")).map((l) => {
    const m = l.match(/^([\w-]+)\s*(>=|<=|=)\s*(-?[\d.]+)\s*:\s*(.+)$/);
    if (!m) throw new Error(`Alert “${l}”: write it as “q3 >= 2: message”.`);
    return { item: m[1], op: { ">=": "gte", "<=": "lte", "=": "eq" }[m[2]], value: Number(m[3]), message: m[4] };
  });
  const subscales = Object.fromEntries(lines(data.get("subscales")).map((l) => {
    const [name, members] = l.split(":");
    if (!members) throw new Error(`Subscale “${l}”: write it as “name: q1, q2”.`);
    return [name.trim(), members.split(",").map((s) => s.trim()).filter(Boolean)];
  }));
  return {
    name: String(data.get("name") || "").trim(),
    instructions: String(data.get("instructions") || ""),
    items,
    scoring: { method: data.get("method"), bands, alerts, subscales },
    licence: { source: String(data.get("source") || "").trim(), attestation: data.get("attestation") === "on" },
  };
}

document.addEventListener("DOMContentLoaded", () => {
  $("#patient-search").addEventListener("input", renderPatients);
  $("#open-tests").addEventListener("click", openTests);
});
