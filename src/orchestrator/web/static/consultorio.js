// Consultorio: the practice's staff app, in Spanish. A thin client over the API: no
// dependencies, no build, no inline script (strict CSP). Every text from the server is
// put on the page with textContent, never as HTML.
"use strict";

// --- the key -----------------------------------------------------------------------------
// Kept for the browser session; in localStorage too only when the person asks to be
// remembered on this computer.
const KEY = "consultorio.key";
const keyStore = {
  get() {
    try { return sessionStorage.getItem(KEY) || localStorage.getItem(KEY); } catch { return null; }
  },
  set(value, remember) {
    try {
      sessionStorage.setItem(KEY, value);
      if (remember) localStorage.setItem(KEY, value);
    } catch { /* storage unavailable: the key lives in memory for this page only */ }
    memoryKey = value;
  },
  clear() {
    try { sessionStorage.removeItem(KEY); localStorage.removeItem(KEY); } catch { /* nothing kept */ }
    memoryKey = null;
  },
};
let memoryKey = null;

const state = { session: null, tz: "America/Guayaquil", weekStart: null, patients: null };
const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) if (child != null && child !== false) node.append(child);
  return node;
}

// --- the API -----------------------------------------------------------------------------
class ApiError extends Error {
  constructor(status, detail) { super(detail || String(status)); this.status = status; this.detail = detail; }
}

async function api(path, options = {}) {
  const resp = await request(path, options);
  return resp.status === 204 ? null : resp.json();
}

/** A file from the API, saved by the browser (the key travels in a header, so a plain
 *  link cannot fetch it). */
async function download(path, filename) {
  const blob = await (await request(path)).blob();
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

async function request(path, { method = "GET", body } = {}) {
  const headers = { "X-API-Key": keyStore.get() || memoryKey || "" };
  let payload;
  if (body instanceof FormData) payload = body; // the browser sets the multipart header
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, { method, headers, body: payload });
  } catch {
    throw new ApiError(0, "");
  }
  if (resp.status === 401 && state.session) {
    signOut("Tu sesión terminó. Vuelve a entrar con tu llave.");
    throw new ApiError(401, "");
  }
  if (!resp.ok) {
    let detail = "";
    try {
      const data = await resp.json();
      detail = typeof data.detail === "string" ? data.detail : "";
    } catch { /* no body */ }
    throw new ApiError(resp.status, detail);
  }
  return resp;
}

// The server answers in English for developers; the person reads Spanish.
const KNOWN = [
  [/already has a visit at that time|not a free slot/, "Ese horario ya está ocupado. Elige otro."],
  [/changed meanwhile/, "Alguien cambió esto al mismo tiempo. Vuelve a cargar e inténtalo otra vez."],
  [/cannot move an appointment/, "Esa cita ya no se puede cambiar a ese estado."],
  [/already exists/, "Ya existe un registro con ese identificador."],
  [/blocks overlap/, "Hay dos horarios que se cruzan el mismo día."],
  [/end must be after the start/, "La hora de salida debe ser después de la de entrada."],
  [/session lasts between/, "Cada sesión debe durar entre 10 y 240 minutos."],
  [/restricted after an erasure/, "Este paciente pidió borrar sus datos: su ficha está restringida."],
];

function humanError(err) {
  if (!(err instanceof ApiError)) return "Algo salió mal. Inténtalo de nuevo.";
  if (err.status === 0) return "No hay conexión con el servidor. Revisa tu internet e inténtalo de nuevo.";
  if (err.status === 403) return "Tu cuenta no tiene permiso para esto.";
  if (err.status === 404) return "No se encontró. Puede que ya no exista.";
  if (err.status === 429) return "Demasiados intentos seguidos. Espera un minuto.";
  if (err.status >= 500) return "El servidor tuvo un problema. Inténtalo de nuevo en un momento.";
  for (const [pattern, text] of KNOWN) if (pattern.test(err.detail)) return text;
  return "No se pudo guardar. Revisa los datos e inténtalo de nuevo.";
}

function say(node, text, kind = "") {
  node.textContent = text;
  node.className = `status ${kind}`.trim();
}

// --- dates in the practice's time zone -------------------------------------------------------
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_NAMES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"];
const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];

function partsIn(date, tz = state.tz) {
  const out = {};
  for (const p of new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date)) out[p.type] = p.value;
  return out;
}
function todayISO() { const p = partsIn(new Date()); return `${p.year}-${p.month}-${p.day}`; }
function nowHour() { return Number(partsIn(new Date()).hour); }
function addDays(iso, n) {
  const d = new Date(`${iso}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function weekday(iso) { return (new Date(`${iso}T12:00:00Z`).getUTCDay() + 6) % 7; } // 0 = Monday
function mondayOf(iso) { return addDays(iso, -weekday(iso)); }
function longDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return `${DAY_NAMES[weekday(iso)]} ${d} de ${MONTHS[m - 1]}${y === Number(todayISO().slice(0, 4)) ? "" : ` de ${y}`}`;
}
/** "2026-10-14" + "09:30" in the practice's zone -> an ISO time with its offset. */
function zoned(dateISO, time) {
  const probe = new Date(`${dateISO}T${time}:00Z`);
  const name = new Intl.DateTimeFormat("en-US", { timeZone: state.tz, timeZoneName: "longOffset" })
    .formatToParts(probe).find((p) => p.type === "timeZoneName")?.value || "GMT";
  const offset = name === "GMT" ? "+00:00" : name.replace("GMT", "");
  return `${dateISO}T${time}:00${offset}`;
}
function ago(iso) {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (minutes < 1) return "hace un momento";
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `hace ${hours} h` : `hace ${Math.round(hours / 24)} días`;
}
function initials(name) {
  return name.split(/[\s.]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
}

// --- words shared by the views ---------------------------------------------------------------
const STATUS = {
  scheduled: ["Por confirmar", "wait", "w"],
  confirmed: ["Confirmada", "ok", "c"],
  completed: ["Atendida", "neutral", "c"],
  no_show: ["No asistió", "bad", "x"],
  cancelled: ["Cancelada", "neutral", "x"],
};
const FLAGS = {
  "no-show risk": "Riesgo de que falte",
  "a test got worse": "Un test empeoró",
  "test alert": "Alerta en un test",
};
function flagText(flag) { return FLAGS[flag] || (flag.startsWith("no visit") ? "Más de 30 días sin cita y ninguna agendada" : flag); }
// The scales' bands (scales.py) in the words the professional uses.
const BANDS = {
  minimal: "mínima", mild: "leve", moderate: "moderada",
  moderately_severe: "moderadamente grave", severe: "grave",
};
const bandName = (band) => BANDS[band] || band;
const NETWORKS = { whatsapp: ["WhatsApp", "wa"], instagram: ["Instagram", "ig"], facebook: ["Facebook", "fb"], app: ["App", "app"] };

function chip(status) {
  const [text, cls] = STATUS[status] || [status, "neutral"];
  return el("span", { class: `chip ${cls}`, text });
}
function myName() {
  const s = state.session;
  return s.professional?.display_name || s.id;
}
function isAdmin() { return state.session.roles.includes("admin"); }
/** "Psic. Carla Luzuriaga" or "carla.luzuriaga" -> "Carla": for the greeting. */
function firstName() {
  const words = myName().replace(/^(psic[oó]log[oa]|psic|dra|dr|lic|mgs|msc|mg)\.?\s+/i, "")
    .split(/[\s.]+/).filter(Boolean);
  const first = words[0] || myName();
  return first.charAt(0).toUpperCase() + first.slice(1);
}

// --- sign in and out -------------------------------------------------------------------------
async function signIn(event) {
  event.preventDefault();
  const status = $("#signin-status");
  const key = $("#signin-key").value.trim();
  if (!key) return say(status, "Escribe tu llave personal.", "error");
  keyStore.set(key, $("#signin-remember").checked);
  say(status, "Entrando…");
  try {
    await start();
    $("#signin-key").value = "";
  } catch (err) {
    keyStore.clear();
    say(status, err instanceof ApiError && (err.status === 401 || err.status === 403)
      ? "Esa llave no es válida para el consultorio. Revísala e inténtalo de nuevo."
      : humanError(err), "error");
  }
}

function signOut(message = "") {
  keyStore.clear();
  state.session = null;
  $("#app").hidden = true;
  $("#signin").hidden = false;
  say($("#signin-status"), message, message ? "error" : "");
  $("#signin-key").focus();
}

async function start() {
  state.session = await api("/v1/session");
  state.tz = state.session.timezone || state.tz;
  $("#me-name").textContent = myName();
  $("#me-initials").textContent = initials(myName());
  const practice = state.session.practice;
  $("#me-practice").textContent = practice && practice !== myName() ? practice : "Consultorio";
  $("#signin").hidden = true;
  $("#app").hidden = false;
  if (!location.hash) location.hash = "#hoy";
  else route();
  refreshBadges();
}

async function refreshBadges() {
  if (!state.session?.clinician) return;
  try {
    const [alerts, inbox] = await Promise.all([api("/v1/social/alerts"), api("/v1/inbox")]);
    setBadge("#badge-alerts", alerts.length, "alertas abiertas");
    setBadge("#badge-unread", inbox.reduce((n, c) => n + c.unread, 0), "mensajes sin leer");
  } catch { /* the badges are a convenience */ }
}
function setBadge(sel, n, label) {
  const badge = $(sel);
  badge.hidden = !n;
  badge.textContent = String(n);
  badge.setAttribute("aria-label", `${n} ${label}`);
}

// --- routing ---------------------------------------------------------------------------------
const VIEWS = {};
let turn = 0; // the latest screen asked for: a slower, older one never paints over it

function route() {
  if (!state.session) return;
  const mine = ++turn;
  const [name, ...rest] = (location.hash.slice(1) || "hoy").split("/");
  const view = VIEWS[name] || VIEWS.hoy;
  for (const link of document.querySelectorAll("[data-nav]")) {
    if (link.dataset.nav === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const main = $("#view");
  main.replaceChildren(el("p", { class: "muted", text: "Cargando…" }));
  // Each view draws into its own frame, shown only if it is still the latest.
  const frame = el("div");
  view(frame, ...rest.map(decodeURIComponent))
    .then(() => {
      if (mine !== turn) return;
      main.replaceChildren(...frame.childNodes);
      main.querySelector("[data-focus]")?.focus();
    })
    .catch((err) => {
      if (mine === turn) main.replaceChildren(el("div", { class: "alert", role: "alert" }, el("span", { text: humanError(err) })));
    });
  main.focus({ preventScroll: true });
}

function page(title, subtitle, ...actions) {
  return el("div", { class: "head" },
    el("div", {}, subtitle ? el("p", { class: "sub", text: subtitle }) : null, el("h1", { class: "title", text: title })),
    actions.length ? el("div", { class: "row tight" }, ...actions) : null);
}
function card(title, cls, ...content) {
  return el("section", { class: `card ${cls || ""}`.trim(), "aria-label": title },
    el("h2", { text: title }), ...content);
}
function empty(text) { return el("p", { class: "empty", text }); }
function link(href, text, cls = "btn") { return el("a", { class: cls, href, text }); }

// --- HOY -------------------------------------------------------------------------------------
VIEWS.hoy = async (main) => {
  const s = state.session;
  const greeting = nowHour() < 12 ? "Buenos días" : nowHour() < 19 ? "Buenas tardes" : "Buenas noches";
  const head = page(`${greeting}, ${firstName()}`, longDate(todayISO()),
    link("#agenda/nueva", "Nueva cita", "btn primary"), link("#pacientes/nuevo", "Nuevo paciente"));
  const parts = [head];
  if (!s.professional && isAdmin()) {
    parts.push(el("div", { class: "notice", role: "note" },
      el("span", { text: "Completa tu perfil profesional y tu horario para que la agenda ofrezca espacios libres." }),
      link("#ajustes", "Completar ahora", "btn small")));
  }
  const clinician = s.clinician;
  const [today, alerts, inbox, worklist] = await Promise.all([
    api(`/v1/agenda?days=1`),
    clinician ? api("/v1/social/alerts") : [],
    clinician ? api("/v1/inbox") : [],
    clinician ? api("/v1/clinical/follow-up") : [],
  ]);
  for (const alert of alerts) parts.push(alertBox(alert));
  const unread = inbox.reduce((n, c) => n + c.unread, 0);
  const active = today.filter((a) => a.status !== "cancelled");
  parts.push(el("section", { class: "stats", "aria-label": "Resumen del día" },
    stat("#agenda", active.length, "Citas hoy"),
    stat("#mensajes", unread, "Mensajes sin leer"),
    stat("#pacientes", worklist.length, "Pacientes por revisar"),
    stat("#hoy", alerts.length, "Alertas abiertas")));
  const visits = active.length
    ? active.map((a) => el("a", { class: "list-item", href: `#paciente/${encodeURIComponent(a.patient_id)}` },
      el("span", { class: "time", text: a.local.slice(11) }),
      el("span", { class: "what" }, el("strong", { text: a.patient_name }), el("br"), el("span", { class: "muted", text: a.kind })),
      chip(a.status)))
    : [empty("No tienes citas hoy.")];
  const todos = [];
  for (const item of worklist.slice(0, 6)) {
    todos.push(el("a", { class: "todo", href: `#paciente/${encodeURIComponent(item.patient_id)}` },
      el("strong", { text: item.name }), el("span", { class: "muted", text: item.flags.map(flagText).join(" · ") })));
  }
  const waiting = inbox.filter((c) => c.unread);
  if (waiting.length) {
    todos.push(el("a", { class: "todo", href: "#mensajes" },
      el("strong", { text: `${waiting.length} conversación${waiting.length === 1 ? "" : "es"} sin leer` }),
      el("span", { class: "muted", text: "La IA puede prepararte un borrador; tú decides si se envía." })));
  }
  if (!todos.length) todos.push(empty("Nada pendiente por ahora."));
  parts.push(el("div", { class: "row" },
    card("Citas de hoy", "grow-3", ...visits, link("#agenda", "Ver la agenda completa", "")),
    card("Pendientes", "grow-2", ...todos)));
  main.replaceChildren(...parts);
};

function stat(href, value, label) {
  return el("a", { class: "stat", href }, el("b", { text: String(value) }), el("span", { class: "sub", text: label }));
}

function alertBox(alert) {
  const [network] = NETWORKS[alert.network] || [alert.network];
  const crisis = alert.kind === "crisis";
  const what = crisis ? "Posible crisis" : "Pide hablar con una persona";
  const status = el("p", { class: "status", role: "status" });
  const done = el("button", { class: "btn", type: "button", text: "Ya la atendí" });
  done.addEventListener("click", async () => {
    done.disabled = true;
    try {
      await api(`/v1/social/alerts/${encodeURIComponent(alert.alert_id)}/resolve`, { method: "POST" });
      refreshBadges();
      route();
    } catch (err) { say(status, humanError(err), "error"); done.disabled = false; }
  });
  const go = alert.subject_id
    ? link(`#paciente/${encodeURIComponent(alert.subject_id)}`, "Ver ficha", "btn danger")
    : link("#mensajes", "Ver conversación", "btn danger");
  return el("section", { class: "alert", role: "alert" },
    el("div", {},
      el("strong", { text: `${what} · ${network} · ${ago(alert.created_at)}` }), el("br"),
      el("span", { text: crisis
        ? "Se le envió la contención automática con el ECU 911 y la línea 171 (opción 6). Falta que la contactes."
        : "Se le avisó que una persona del equipo le escribirá." }),
      alert.address && (alert.network === "whatsapp" || alert.network === "app")
        ? el("span", { text: ` Número para devolver la llamada: ${alert.address}.` })
        : el("span", { text: alert.network === "app" ? "" : " Respóndele desde Mensajes." }),
      status),
    el("div", { class: "row tight" }, go, done));
}

// --- AGENDA ----------------------------------------------------------------------------------
VIEWS.agenda = async (main, arg) => {
  state.weekStart = state.weekStart || mondayOf(todayISO());
  const prof = state.session.professional;
  const weekEnd = addDays(state.weekStart, 6);
  const [visits, slots] = await Promise.all([
    api(`/v1/agenda?from=${state.weekStart}&days=7`),
    prof ? freeSlotsUntil(weekEnd) : [],
  ]);
  const nav = (delta, label, text) => {
    const b = el("button", { class: "btn", type: "button", "aria-label": label, text });
    b.addEventListener("click", () => {
      state.weekStart = delta ? addDays(state.weekStart, delta) : mondayOf(todayISO());
      route();
    });
    return b;
  };
  const [y1, m1, d1] = state.weekStart.split("-").map(Number);
  const [, m2, d2] = weekEnd.split("-").map(Number);
  const range = m1 === m2 ? `${d1} al ${d2} de ${MONTHS[m1 - 1]}` : `${d1} de ${MONTHS[m1 - 1]} al ${d2} de ${MONTHS[m2 - 1]}`;
  const head = page("Agenda", `Semana del ${range} de ${y1}`,
    nav(-7, "Semana anterior", "‹"), nav(0, "Ir a esta semana", "Hoy"), nav(7, "Semana siguiente", "›"),
    newVisitButton());
  const columns = [];
  for (let i = 0; i < 7; i++) {
    const day = addDays(state.weekStart, i);
    const [, , dd] = day.split("-").map(Number);
    const items = [
      ...visits.filter((v) => v.local.startsWith(day)).map((v) => ({ at: v.local.slice(11), visit: v })),
      ...slots.filter((s) => s.local.startsWith(day)).map((s) => ({ at: s.local.slice(11), slot: s })),
    ].sort((a, b) => a.at.localeCompare(b.at));
    columns.push(el("div", {},
      el("div", { class: `day-head${day === todayISO() ? " today" : ""}` },
        el("span", { text: DAY_NAMES[i].slice(0, 3) }), el("b", { text: String(dd) })),
      ...items.map((item) => item.visit ? visitButton(item.visit) : slotButton(item.slot))));
  }
  const parts = [head];
  if (!prof) {
    parts.push(el("div", { class: "notice", role: "note" },
      el("span", { text: "Para ver tus espacios libres, completa tu perfil y tu horario en Ajustes." }),
      link("#ajustes", "Ir a Ajustes", "btn small")));
  }
  parts.push(el("section", { class: "card week", "aria-label": "Semana" }, el("div", { class: "week-grid" }, ...columns)),
    el("p", { class: "muted", text: "Las pacientes solo pueden reservar los espacios libres. Toca un espacio libre para agendar ahí." }));
  main.replaceChildren(...parts);
  if (arg === "nueva") openVisitDialog({});
};

async function freeSlotsUntil(lastDay) {
  const today = todayISO();
  if (lastDay < today) return [];
  const days = Math.min(60, Math.round((new Date(`${lastDay}T12:00:00Z`) - new Date(`${today}T12:00:00Z`)) / 86400000) + 1);
  return api(`/v1/agenda/slots?professional_id=${encodeURIComponent(state.session.professional.professional_id)}&days=${days}`);
}

function newVisitButton() {
  const b = el("button", { class: "btn primary", type: "button", text: "Nueva cita" });
  b.addEventListener("click", () => openVisitDialog({}));
  return b;
}

function visitButton(visit) {
  const [, , cls] = STATUS[visit.status] || ["", "", "c"];
  const b = el("button", { class: `slot ${cls}`, type: "button" },
    el("b", { text: visit.local.slice(11) }), el("span", { text: visit.patient_name }),
    el("span", { class: "sr", text: `, ${STATUS[visit.status]?.[0] || visit.status}` }));
  b.addEventListener("click", () => openVisitActions(visit));
  return b;
}

function slotButton(slot) {
  const b = el("button", { class: "slot f", type: "button", "aria-label": `Agendar a las ${slot.local.slice(11)}` },
    el("b", { text: slot.local.slice(11) }), el("span", { text: "Libre" }));
  b.addEventListener("click", () => openVisitDialog({ date: slot.local.slice(0, 10), time: slot.local.slice(11), minutes: slot.minutes }));
  return b;
}

function dialog(title, ...content) {
  const d = $("#dialog");
  const close = el("button", { class: "btn small", type: "button", text: "Cerrar" });
  close.addEventListener("click", () => d.close());
  d.replaceChildren(el("div", { class: "card" },
    el("div", { class: "head" }, el("h2", { id: "dialog-title", text: title }), close), ...content));
  d.showModal();
  return d;
}

async function patientsList() {
  if (!state.patients) state.patients = await api("/v1/crm/patients?limit=500");
  return state.patients;
}

async function openVisitDialog({ date, time, minutes, patientId }) {
  const patients = await patientsList();
  const who = el("select", { required: true },
    el("option", { value: "", text: patients.length ? "Elige un paciente" : "Primero crea un paciente" }),
    ...[...patients].sort((a, b) => a.display_name.localeCompare(b.display_name, "es"))
      .map((p) => el("option", { value: p.id, text: p.display_name })));
  const day = el("input", { type: "date", value: date || todayISO(), required: true });
  const at = el("input", { type: "time", value: time || "09:00", step: "300", required: true });
  const length = el("input", { type: "number", min: "10", max: "240", step: "5", value: String(minutes || 50) });
  const kind = el("select", {}, ...["Seguimiento", "Primera consulta", "Evaluación"].map((k) => el("option", { value: k, text: k })));
  const mode = el("select", {}, el("option", { value: "presencial", text: "Presencial" }), el("option", { value: "en línea", text: "En línea" }));
  const price = el("input", { type: "number", min: "0", step: "0.01", value: "0" });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Guardar cita" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Paciente", who),
    el("div", { class: "fields" }, el("label", { class: "field" }, "Fecha", day), el("label", { class: "field" }, "Hora", at)),
    el("div", { class: "fields" }, el("label", { class: "field" }, "Tipo", kind), el("label", { class: "field" }, "Modalidad", mode)),
    el("div", { class: "fields" }, el("label", { class: "field" }, "Duración (minutos)", length), el("label", { class: "field" }, "Valor (USD)", price)),
    save, status,
    el("p", { class: "muted", text: "Si el horario ya está ocupado, el sistema no deja guardar: nunca hay dos citas a la vez." }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!who.value) return say(status, "Elige un paciente.", "error");
    save.disabled = true;
    say(status, "Guardando…");
    try {
      await api("/v1/crm/appointments", {
        method: "POST",
        body: {
          patient_id: who.value,
          starts_at: zoned(day.value, at.value),
          duration_min: Number(length.value) || 50,
          kind: `${kind.value} · ${mode.value}`,
          price: Number(price.value) || 0,
          professional_id: state.session.professional?.professional_id ?? null,
        },
      });
      $("#dialog").close();
      state.weekStart = mondayOf(day.value);
      if (location.hash.startsWith("#agenda")) { history.replaceState(null, "", "#agenda"); route(); } else location.hash = "#agenda";
    } catch (err) { say(status, humanError(err), "error"); save.disabled = false; }
  });
  dialog("Nueva cita", form);
  if (patientId) who.value = patientId;
  who.focus();
}

function openVisitActions(visit) {
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const actions = [];
  const next = {
    scheduled: [["confirmed", "Confirmar"], ["completed", "Atendida"], ["no_show", "No asistió"], ["cancelled", "Cancelar cita"]],
    confirmed: [["completed", "Atendida"], ["no_show", "No asistió"], ["cancelled", "Cancelar cita"]],
  }[visit.status] || [];
  for (const [value, label] of next) {
    const b = el("button", { class: `btn${value === "cancelled" ? "" : " primary"}`, type: "button", text: label });
    b.addEventListener("click", async () => {
      for (const x of actions) x.disabled = true;
      try {
        await api(`/v1/crm/appointments/${encodeURIComponent(visit.id)}/status`, { method: "POST", body: { status: value } });
        $("#dialog").close();
        route();
      } catch (err) { say(status, humanError(err), "error"); for (const x of actions) x.disabled = false; }
    });
    actions.push(b);
  }
  dialog(visit.patient_name,
    el("p", { text: `${longDate(visit.local.slice(0, 10))} · ${visit.local.slice(11)} · ${visit.duration_min} min` }),
    el("p", { class: "muted", text: visit.kind }),
    el("div", {}, chip(visit.status)),
    actions.length ? el("div", { class: "row tight" }, ...actions) : null,
    link(`#paciente/${encodeURIComponent(visit.patient_id)}`, "Abrir la ficha", "btn"),
    status);
  $("#dialog").querySelector("a.btn")?.addEventListener("click", () => $("#dialog").close());
}

// --- AJUSTES: profile and hours ------------------------------------------------------------------
VIEWS.ajustes = async (main, section, which) => {
  const s = state.session;
  if (section === "test" && which) return testEditor(main, which);
  const parts = [page("Ajustes", "Tu perfil, tu horario, tus tests y la guardia")];
  parts.push(s.professional ? await hoursCard(s.professional) : profileCard());
  if (s.clinician) parts.push(await testsCard());
  parts.push(await onCallCard());
  parts.push(card("Tu cuenta", "",
    el("p", { text: `Entraste como ${s.id}.` }),
    el("p", { class: "muted", text: `Permisos: ${s.roles.map((r) => ({ admin: "administración", owner: "dueña", reviewer: "psicóloga (datos clínicos)", reception: "recepción", marketing: "marketing", privacy: "protección de datos" })[r] || r).join(", ")}.` })));
  main.replaceChildren(...parts);
};

// Tests: start from a public-domain template, paste the official wording of each item.
const PLACEHOLDER = "(texto oficial del formulario)";

async function testsCard() {
  const tests = await api("/v1/clinical/instruments");
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const add = (template, label) => {
    const b = el("button", { class: "btn", type: "button", text: label });
    b.addEventListener("click", async () => {
      b.disabled = true;
      try {
        const made = await api("/v1/clinical/instruments/from-template", { method: "POST", body: { template } });
        location.hash = `#ajustes/test/${encodeURIComponent(made.instrument_id)}`;
      } catch (err) { say(status, humanError(err), "error"); b.disabled = false; }
    });
    return b;
  };
  const rows = tests.map((t) => {
    const missing = t.spec.items.filter((i) => i.text.includes(PLACEHOLDER)).length;
    return el("div", { class: "doc" },
      el("span", { class: "what" }, el("strong", { text: t.name }), el("br"),
        el("span", { class: "muted", text: `${t.spec.items.length} preguntas · versión ${t.version}` })),
      missing
        ? el("span", { class: "chip wait", text: `Faltan ${missing} textos oficiales` })
        : el("span", { class: "chip ok", text: t.spec.administration === "self_report" ? "Lista para enviar" : "Para aplicar en sesión" }),
      link(`#ajustes/test/${encodeURIComponent(t.instrument_id)}`, "Editar textos", "btn small"));
  });
  return card("Tus tests", "",
    ...(rows.length ? rows : [empty("Aún no tienes tests. Empieza con una plantilla de dominio público.")]),
    el("div", { class: "row tight" }, add("phq9", "Agregar PHQ-9 (depresión)"), add("gad7", "Agregar GAD-7 (ansiedad)")),
    status,
    el("p", { class: "muted", text: "Por derechos de autor, las plantillas no traen el texto de las preguntas: cópialo del formulario oficial publicado. Hasta entonces el test no se puede enviar a las pacientes." }));
}

async function testEditor(main, id) {
  const test = await api(`/v1/clinical/instruments/${encodeURIComponent(id)}`);
  const spec = test.spec;
  const instructions = el("textarea", { rows: "3" });
  instructions.value = spec.instructions || "";
  const fields = spec.items.map((item, i) => {
    const area = el("textarea", { rows: "2", required: true });
    area.value = item.text.includes(PLACEHOLDER) ? "" : item.text;
    return { item, area, node: el("label", { class: "field" }, `Pregunta ${i + 1}`, area) };
  });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Guardar textos" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Instrucciones para la paciente", instructions),
    ...fields.map((f) => f.node),
    el("p", { class: "muted", text: `Opciones de respuesta: ${spec.items[0]?.options.map((o) => o.label).join(" · ") || "—"}` }),
    save, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const empties = fields.filter((f) => !f.area.value.trim()).length;
    if (empties) return say(status, `Faltan ${empties} preguntas por completar.`, "error");
    save.disabled = true;
    const revised = { ...spec, instructions: instructions.value.trim(),
      items: fields.map((f) => ({ ...f.item, text: f.area.value.trim() })) };
    try {
      await api(`/v1/clinical/instruments/${encodeURIComponent(id)}`, { method: "PUT", body: { spec: revised } });
      say(status, "Textos guardados. Ya puedes enviar este test a tus pacientes.", "done");
    } catch (err) { say(status, humanError(err), "error"); }
    save.disabled = false;
  });
  main.replaceChildren(page(test.name, "Pega el texto oficial de cada pregunta", link("#ajustes", "Volver a Ajustes")),
    card("Textos del test", "", form));
}

// On call: who is told at once about a crisis, by WhatsApp, Telegram or e-mail (ADR 0017).
async function onCallCard() {
  const contacts = (await api("/v1/admin/on-call")).filter((c) => c.active);
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const rows = contacts.sort((a, b) => a.level - b.level).map((c) => {
    const drop = el("button", { class: "btn small", type: "button", text: "Quitar" });
    drop.addEventListener("click", async () => {
      drop.disabled = true;
      try { await api(`/v1/admin/on-call/${encodeURIComponent(c.contact_id)}`, { method: "DELETE" }); route(); } catch (err) { say(status, humanError(err), "error"); drop.disabled = false; }
    });
    const ways = [c.whatsapp_number && `WhatsApp ${c.whatsapp_number}`, c.telegram_chat_id && "Telegram", c.email].filter(Boolean).join(" · ");
    return el("div", { class: "doc" },
      el("span", { class: "what" }, el("strong", { text: c.display_name }), el("br"),
        el("span", { class: "muted", text: `${c.level === 1 ? "De guardia" : `Respaldo (nivel ${c.level})`} · ${ways || "sin medio de aviso"}` })),
      isAdmin() ? drop : null);
  });
  const parts = [...(rows.length ? rows : [el("div", { class: "notice", role: "note" }, el("span", { text: "Nadie está de guardia: las alertas de crisis solo se ven dentro de la app. Agrega a quien debe recibir el aviso al instante." }))])];
  if (isAdmin()) {
    const name = el("input", { type: "text", maxlength: "120", required: true });
    const level = el("select", {}, el("option", { value: "1", text: "De guardia (primer aviso)" }), el("option", { value: "2", text: "Respaldo (si nadie responde en 5 minutos)" }));
    const phone = el("input", { type: "tel", maxlength: "24", placeholder: "Ej.: +593 99 123 4567" });
    const mail = el("input", { type: "email", maxlength: "254" });
    const save = el("button", { class: "btn primary", type: "submit", text: "Agregar a la guardia" });
    const form = el("form", { class: "stack", novalidate: true },
      el("div", { class: "fields" }, el("label", { class: "field" }, "Nombre", name), el("label", { class: "field" }, "Nivel", level)),
      el("div", { class: "fields" }, el("label", { class: "field" }, "WhatsApp", phone), el("label", { class: "field" }, "Correo", mail)),
      save, status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!name.value.trim() || (!phone.value.trim() && !mail.value.trim())) return say(status, "Escribe el nombre y al menos un WhatsApp o un correo.", "error");
      save.disabled = true;
      try {
        await api("/v1/admin/on-call", { method: "POST", body: { display_name: name.value.trim(), level: Number(level.value), whatsapp_number: phone.value.trim() || null, email: mail.value.trim() || null } });
        route();
      } catch (err) { say(status, humanError(err), "error"); save.disabled = false; }
    });
    parts.push(form);
  }
  return card("Guardia para crisis", "", ...parts,
    el("p", { class: "muted", text: "El aviso dice solo que hay una alerta y el enlace a la app: nunca el mensaje ni datos de la paciente." }));
}

function profileCard() {
  if (!isAdmin()) {
    return card("Tu perfil profesional", "", empty("Pide a la administración del consultorio que registre tu perfil profesional."));
  }
  const name = el("input", { type: "text", required: true, value: "", maxlength: "120", placeholder: "Ej.: Psic. Carla Luzuriaga" });
  const pack = el("select", {},
    el("option", { value: "ec-psychologist", text: "Psicóloga o psicólogo clínico (Ecuador)" }),
    el("option", { value: "ec-psychiatrist", text: "Psiquiatra (Ecuador)" }));
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Guardar mi perfil" });
  const form = el("form", { class: "stack", novalidate: true },
    el("p", { class: "muted", text: "Así te verán las pacientes al reservar. Después configuras tu horario." }),
    el("label", { class: "field" }, "Nombre visible", name),
    el("label", { class: "field" }, "Profesión", pack), save, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!name.value.trim()) return say(status, "Escribe tu nombre visible.", "error");
    save.disabled = true;
    try {
      const s = state.session;
      await api("/v1/admin/professionals", {
        method: "POST",
        body: { professional_id: s.id, display_name: name.value.trim(), pack_id: pack.value, staff_id: s.id },
      });
      await start();
      location.hash = "#ajustes";
      route();
    } catch (err) { say(status, humanError(err), "error"); save.disabled = false; }
  });
  return card("Tu perfil profesional", "", form);
}

async function hoursCard(prof) {
  const current = await api(`/v1/agenda/professionals/${encodeURIComponent(prof.professional_id)}/hours`);
  const byDay = Object.fromEntries(DAYS.map((d) => [d, current.filter((b) => b.day === d)]));
  const minutes = el("input", { type: "number", min: "10", max: "240", step: "5", value: String(current[0]?.slot_minutes || 50) });
  const rows = DAYS.map((day, i) => {
    const [first, second] = byDay[day];
    const [s1, e1] = (first?.hours || "09:00-13:00").split("-");
    const [s2, e2] = (second?.hours || "15:00-18:00").split("-");
    const on = el("input", { type: "checkbox", checked: Boolean(first) });
    const split = el("input", { type: "checkbox", checked: Boolean(second) });
    const inputs = [s1, e1, s2, e2].map((v) => el("input", { type: "time", value: v, step: "300" }));
    for (const [n, input] of inputs.entries()) {
      input.setAttribute("aria-label", `${DAY_NAMES[i]}: ${["desde", "hasta", "segundo horario desde", "segundo horario hasta"][n]}`);
    }
    on.setAttribute("aria-label", `Atiendo el ${DAY_NAMES[i].toLowerCase()}`);
    split.setAttribute("aria-label", `Segundo horario el ${DAY_NAMES[i].toLowerCase()}`);
    return { day, on, split, inputs, node: el("div", { class: "hours-row" },
      el("label", { class: "check" }, on, el("strong", { text: DAY_NAMES[i] })),
      el("span", { class: "hours-span" }, inputs[0], "a", inputs[1]),
      el("label", { class: "check" }, split, "y"),
      el("span", { class: "hours-span" }, inputs[2], "a", inputs[3])) };
  });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Guardar horario" });
  const form = el("form", { class: "stack", novalidate: true },
    el("p", { class: "muted", text: `${prof.display_name}. Las pacientes solo pueden reservar dentro de este horario.` }),
    el("label", { class: "field" }, "Duración de cada sesión (minutos)", minutes),
    el("div", { class: "hours" }, ...rows.map((r) => r.node)),
    el("p", { class: "muted", text: "Marca los días que atiendes. Si tienes un segundo horario (por ejemplo, después del almuerzo), marca «y» y escríbelo." }),
    save, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const hours = [];
    for (const r of rows) {
      if (!r.on.checked) continue;
      hours.push({ day: r.day, hours: `${r.inputs[0].value}-${r.inputs[1].value}`, slot_minutes: Number(minutes.value) || 50 });
      if (r.split.checked) hours.push({ day: r.day, hours: `${r.inputs[2].value}-${r.inputs[3].value}`, slot_minutes: Number(minutes.value) || 50 });
    }
    save.disabled = true;
    say(status, "Guardando…");
    try {
      await api(`/v1/agenda/professionals/${encodeURIComponent(prof.professional_id)}/hours`, { method: "PUT", body: { hours } });
      say(status, "Horario guardado. Ya aparece en la agenda.", "done");
    } catch (err) { say(status, humanError(err), "error"); }
    save.disabled = false;
  });
  return card("Tu horario de atención", "", form);
}

// --- PACIENTES: the list and a new patient ----------------------------------------------------
VIEWS.pacientes = async (main, arg) => {
  if (arg === "nuevo") return newPatientView(main);
  const search = el("input", { type: "search", placeholder: "Buscar por nombre", "aria-label": "Buscar paciente por nombre" });
  const box = el("div", { class: "table-box" });
  const show = async () => {
    const q = search.value.trim();
    const rows = await api(`/v1/crm/patients?limit=500${q ? `&search=${encodeURIComponent(q)}` : ""}`);
    if (!q) state.patients = rows;
    box.replaceChildren(rows.length
      ? el("table", {},
        el("thead", {}, el("tr", {}, el("th", { text: "Paciente" }), el("th", { text: "Desde" }), el("th", { text: "Estado" }))),
        el("tbody", {}, ...rows.map((p) => el("tr", {},
          el("td", {}, el("a", { class: "who", href: `#paciente/${encodeURIComponent(p.id)}` },
            el("span", { class: "ini", "aria-hidden": "true", text: initials(p.display_name) }), el("span", { text: p.display_name }))),
          el("td", { text: p.created_at ? longDate(p.created_at.slice(0, 10)) : "" }),
          el("td", {}, p.restricted ? el("span", { class: "chip neutral", text: "Restringido" }) : el("span", { class: "chip ok", text: "Activo" }))))))
      : empty(q ? "Ningún paciente coincide con esa búsqueda." : "Aún no hay pacientes. Crea el primero."));
  };
  let timer = null;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => show().catch(() => {}), 250); });
  main.replaceChildren(page("Pacientes", "Toda la información clínica se ve solo con tu rol de psicóloga", link("#pacientes/nuevo", "Nuevo paciente", "btn primary")),
    el("label", { class: "field" }, "Buscar", search), box);
  await show();
};

function newPatientView(main) {
  const name = el("input", { type: "text", required: true, maxlength: "200", autocomplete: "off", "data-focus": true });
  const phone = el("input", { type: "tel", maxlength: "32", placeholder: "Ej.: 0991234567", autocomplete: "off" });
  const email = el("input", { type: "email", maxlength: "254", autocomplete: "off" });
  const birth = el("input", { type: "date" });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Crear paciente" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Nombre y apellido", name),
    el("div", { class: "fields" }, el("label", { class: "field" }, "Celular (para WhatsApp)", phone), el("label", { class: "field" }, "Correo", email)),
    el("label", { class: "field" }, "Fecha de nacimiento", birth),
    el("p", { class: "muted", text: "Pide solo los datos que necesitas para atenderla. El celular sirve para los recordatorios y para reconocerla cuando escriba por WhatsApp." }),
    save, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!name.value.trim()) return say(status, "Escribe el nombre.", "error");
    save.disabled = true;
    try {
      const made = await api("/v1/crm/patients", {
        method: "POST",
        body: { display_name: name.value.trim(), phone: phone.value.trim() || null, email: email.value.trim() || null, birth_date: birth.value || null },
      });
      state.patients = null;
      location.hash = `#paciente/${encodeURIComponent(made.id)}`;
    } catch (err) { say(status, humanError(err), "error"); save.disabled = false; }
  });
  main.replaceChildren(page("Nuevo paciente", ""), card("Datos de contacto", "", form));
}

// --- FICHA: the patient's record ----------------------------------------------------------------
const DOC_NAMES = {
  session_note: "Nota de sesión",
  psychotherapy_note: "Nota de psicoterapia (privada)",
  consent_therapy: "Consentimiento informado para la terapia",
  consent_guardian: "Consentimiento del representante (menor de edad)",
  consent_diary: "Consentimiento para el diario emocional",
  consent_online: "Consentimiento para sesiones en línea",
  consent_voluntary_internment: "Consentimiento de internamiento voluntario",
  referral: "Derivación",
  internment_report: "Informe de internamiento",
  defensoria_notice: "Aviso a la Defensoría",
  attendance_certificate: "Certificado de asistencia",
  psychological_report: "Informe psicológico",
  scale_result: "Resultado de escala",
  prescription: "Receta",
  controlled_worksheet: "Hoja para receta especial (ACESS)",
  medical_certificate: "Certificado médico",
  medication_followup: "Seguimiento de medicación",
};
const docName = (type) => DOC_NAMES[type] || type;
const TABS = [["resumen", "Resumen"], ["historia", "Historia"], ["tests", "Tests"], ["documentos", "Documentos"], ["citas", "Citas"], ["mensajes", "Mensajes"]];

VIEWS.paciente = async (main, id, tab = "resumen") => {
  const patient = await api(`/v1/crm/patients/${encodeURIComponent(id)}`);
  const contact = [patient.phone, patient.email].filter(Boolean).join(" · ") || "Sin datos de contacto";
  const visit = el("button", { class: "btn primary", type: "button", text: "Nueva cita" });
  visit.addEventListener("click", () => openVisitDialog({ patientId: patient.id }));
  const access = el("button", { class: "btn", type: "button", text: "Acceso a su app" });
  access.addEventListener("click", () => openAccessDialog(patient));
  const tabs = el("div", { class: "tabs", role: "tablist", "aria-label": "Secciones de la ficha" },
    ...TABS.map(([key, label]) => {
      const b = el("button", { type: "button", role: "tab", "aria-selected": String(key === tab), text: label });
      b.addEventListener("click", () => { location.hash = `#paciente/${encodeURIComponent(id)}/${key}`; });
      return b;
    }));
  const body = el("div", { class: "stack", role: "tabpanel" }, el("p", { class: "muted", text: "Cargando…" }));
  main.replaceChildren(...[
    page(patient.display_name, contact, patient.restricted ? null : access, visit),
    patient.restricted ? el("div", { class: "notice", role: "note" }, el("span", { text: "Esta paciente pidió borrar sus datos: la ficha está restringida y solo se conserva la historia clínica que exige la ley." })) : null,
    tabs, body].filter(Boolean));
  const render = { resumen: tabResumen, historia: tabHistoria, tests: tabTests, documentos: tabDocumentos, citas: tabCitas, mensajes: tabMensajes }[tab] || tabResumen;
  body.replaceChildren(...await render(patient));
};

/** The patient's personal link to their app: shown once, sent by the professional. */
async function openAccessDialog(patient) {
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const make = el("button", { class: "btn primary", type: "button", text: "Generar su enlace personal" });
  const area = el("div", { class: "stack" });
  make.addEventListener("click", async () => {
    make.disabled = true;
    try {
      const access = await api(`/v1/crm/patients/${encodeURIComponent(patient.id)}/access`, { method: "POST" });
      const url = `${location.origin}/mi/#k=${encodeURIComponent(access.key)}`;
      const field = el("input", { type: "text", readonly: true, value: url, "aria-label": "Enlace personal" });
      const copy = el("button", { class: "btn", type: "button", text: "Copiar enlace" });
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(url); say(status, "Enlace copiado.", "done"); }
        catch { field.select(); say(status, "Selecciónalo y cópialo con Ctrl+C.", ""); }
      });
      const message = `Hola ${patient.display_name.split(/\s+/)[0]}, este es tu enlace personal para la app del consultorio: ${url} . Es solo tuyo: no lo compartas.`;
      const digits = (patient.phone || "").replace(/\D/g, "");
      const number = digits.startsWith("0") ? `593${digits.slice(1)}` : digits;
      area.replaceChildren(field, el("div", { class: "row tight" }, copy,
        number ? el("a", { class: "btn primary", href: `https://wa.me/${number}?text=${encodeURIComponent(message)}`, target: "_blank", rel: "noopener noreferrer", text: "Enviar por WhatsApp" }) : null),
        el("p", { class: "muted", text: `Vence en 90 días. Si lo pierde, genera uno nuevo.` }));
      make.hidden = true;
    } catch (err) { say(status, humanError(err), "error"); make.disabled = false; }
  });
  dialog("Acceso a su app",
    el("p", { text: `Con este enlace, ${patient.display_name} ve sus citas, reserva en tus horarios libres y responde los tests que le envíes.` }),
    el("p", { class: "muted", text: "Envíaselo solo a ella: quien tenga el enlace entra a su app. No muestra su historia clínica." }),
    make, area, status);
}

async function tabResumen(patient) {
  if (!state.session.clinician) return [empty("El resumen clínico es solo para el rol de psicóloga.")];
  const briefing = await api(`/v1/clinical/patients/${encodeURIComponent(patient.id)}/briefing`);
  const [title, ...lines] = briefing.lines;
  return [card("Resumen antes de la sesión", "",
    el("p", {}, el("span", { class: "chip wait", text: "Borrador automático" })),
    el("p", { class: "muted", text: title }),
    el("ul", {}, ...lines.map((line) => el("li", { text: line }))),
    el("p", { class: "muted", text: "Se arma con fechas y números de la ficha. No lee tus notas ni usa inteligencia artificial." }))];
}

async function tabHistoria(patient) {
  const pid = encodeURIComponent(patient.id);
  const [entries, types] = await Promise.all([
    api(`/v1/clinical/patients/${pid}/documents`),
    api("/v1/clinical/document-types"),
  ]);
  const kind = el("select", {}, ...types.map((t) => el("option", { value: t.doc_type, text: docName(t.doc_type) })));
  if (types.some((t) => t.doc_type === "session_note")) kind.value = "session_note";
  const text = el("textarea", { required: true, maxlength: "20000", rows: "8" });
  let amends = null;
  const amending = el("p", { class: "muted", hidden: true });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Guardar en la historia" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Tipo de entrada", kind),
    el("label", { class: "field" }, "Texto", text), amending, save, status,
    el("p", { class: "muted", text: "Lo guardado no se borra ni se edita: para corregir, se agrega una corrección que queda unida a la entrada original." }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!text.value.trim()) return say(status, "Escribe el texto.", "error");
    save.disabled = true;
    try {
      await api(`/v1/clinical/patients/${pid}/documents`, { method: "POST", body: { doc_type: kind.value, body: text.value, amends } });
      route();
    } catch (err) { say(status, humanError(err), "error"); save.disabled = false; }
  });
  const list = [...entries].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((d) => {
    const fix = el("button", { class: "btn small", type: "button", text: "Corregir" });
    fix.addEventListener("click", () => {
      amends = d.document_id;
      kind.value = d.doc_type;
      text.value = d.body;
      amending.hidden = false;
      amending.textContent = `Corrigiendo la entrada del ${longDate(d.created_at.slice(0, 10))}. La original se conserva.`;
      text.focus();
    });
    return el("article", { class: "card" },
      el("div", { class: "head" },
        el("div", {},
          el("strong", { text: docName(d.doc_type) }),
          d.access === "author_only" ? el("span", { class: "chip neutral", text: " Solo tú la ves" }) : null,
          d.amends ? el("span", { class: "chip wait", text: " Corrección" }) : null,
          el("p", { class: "muted", text: `${longDate(d.created_at.slice(0, 10))} · ${d.author_id}` })),
        fix),
      el("p", { class: "pre", text: d.body }));
  });
  return [card("Nueva entrada", "", form), ...(list.length ? list : [empty("La historia todavía no tiene entradas.")])];
}

async function tabTests(patient) {
  const pid = encodeURIComponent(patient.id);
  const [results, sent, available] = await Promise.all([
    api(`/v1/clinical/patients/${pid}/instrument-results`),
    api(`/v1/clinical/patients/${pid}/assignments`),
    api("/v1/clinical/instruments"),
  ]);
  const choice = el("select", {},
    el("option", { value: "", text: available.length ? "Elige un test" : "Aún no tienes tests: créalos desde una plantilla" }),
    ...available.map((t) => el("option", { value: t.instrument_id, text: t.name })));
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const send = el("button", { class: "btn primary", type: "submit", text: "Enviar a su app" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Test", choice), send, status,
    el("p", { class: "muted", text: "Lo responde desde su celular. Ella no ve el puntaje: lo interpretas tú. Si una respuesta indica riesgo, recibe al instante las líneas de emergencia y a ti te llega una alerta." }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!choice.value) return say(status, "Elige un test.", "error");
    send.disabled = true;
    try {
      await api(`/v1/clinical/patients/${pid}/assignments`, { method: "POST", body: { instrument_id: choice.value, due_days: 7 } });
      route();
    } catch (err) {
      say(status, /official text/.test(err.detail || "")
        ? "Antes de enviarlo, pega el texto oficial de cada pregunta del test (por derechos de autor no viene incluido)."
        : /already waiting/.test(err.detail || "") ? "Ese test ya está esperando su respuesta."
          : /applied by the professional/.test(err.detail || "") ? "Ese test lo aplicas tú en sesión; no se envía a la app." : humanError(err), "error");
      send.disabled = false;
    }
  });
  const pending = sent.filter((a) => a.status === "pending").map((a) => {
    const cancel = el("button", { class: "btn small", type: "button", text: "Cancelar envío" });
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try { await api(`/v1/clinical/assignments/${encodeURIComponent(a.assignment_id)}`, { method: "DELETE" }); route(); } catch { cancel.disabled = false; }
    });
    return el("div", { class: "doc" }, el("span", { class: "what", text: `${a.name} · enviado el ${longDate(a.assigned_at.slice(0, 10))}` }), el("span", { class: "chip wait", text: "Esperando respuesta" }), cancel);
  });
  const done = [...results].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((r) => el("article", { class: "card" },
    el("div", { class: "head" },
      el("div", {}, el("strong", { text: r.instrument_name }), el("p", { class: "muted", text: `${longDate(r.created_at.slice(0, 10))}${r.note === "self-report" ? " · respondido en su app" : ""}` })),
      el("span", { class: `chip ${r.severity === "high" ? "bad" : r.severity === "moderate" ? "wait" : "ok"}`, text: r.band ? `${r.total} · ${bandName(r.band)}` : String(r.total ?? "—") })),
    ...r.alerts.map((a) => el("p", { class: "status error", text: `⚠ ${a.message}` }))));
  return [
    card("Enviar un test a su app", "", form),
    card("Esperando respuesta", "", ...(pending.length ? pending : [empty("No hay tests pendientes.")])),
    card("Resultados", "", ...(done.length ? done : [empty("Aún no hay resultados.")]),
      el("p", { class: "muted", text: "La banda es una ayuda para interpretar, no un diagnóstico." })),
  ];
}

async function tabDocumentos(patient) {
  const pid = encodeURIComponent(patient.id);
  const files = await api(`/v1/clinical/patients/${pid}/files`);
  const file = el("input", { type: "file", accept: ".pdf,.png,.jpg,.jpeg,.txt", required: true });
  const label = el("input", { type: "text", maxlength: "120", placeholder: "Ej.: Consentimiento firmado", required: true });
  const own = el("input", { type: "checkbox" });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Subir documento" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Archivo (PDF, imagen o texto, hasta 10 MB)", file),
    el("label", { class: "field" }, "Nombre del documento", label),
    el("label", { class: "check" }, own, "Solo yo puedo verlo"), save, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!file.files.length || !label.value.trim()) return say(status, "Elige el archivo y escribe un nombre.", "error");
    const data = new FormData();
    data.append("file", file.files[0]);
    data.append("label", label.value.trim());
    data.append("access", own.checked ? "author_only" : "care_team");
    save.disabled = true;
    say(status, "Subiendo…");
    try {
      await api(`/v1/clinical/patients/${pid}/files`, { method: "POST", body: data });
      route();
    } catch (err) {
      say(status, err.status === 422 ? "Ese archivo no se aceptó: debe ser PDF, PNG, JPEG o texto, de hasta 10 MB." : humanError(err), "error");
      save.disabled = false;
    }
  });
  const list = [...files].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((f) => {
    const get = el("button", { class: "btn small", type: "button", text: "Descargar" });
    get.addEventListener("click", () => download(`/v1/clinical/files/${encodeURIComponent(f.file_id)}`, f.filename).catch(() => {}));
    return el("div", { class: "doc" },
      el("span", { class: "what" }, el("strong", { text: f.label }), el("br"),
        el("span", { class: "muted", text: `${f.filename} · ${longDate(f.created_at.slice(0, 10))}${f.access === "author_only" ? " · solo tú" : ""}` })),
      get);
  });
  return [card("Subir un documento", "", form), card("Documentos", "", ...(list.length ? list : [empty("No hay documentos.")]),
    el("p", { class: "muted", text: "Cada archivo guarda una huella (SHA-256) que demuestra que no fue modificado." }))];
}

async function tabCitas(patient) {
  const visits = await api(`/v1/crm/appointments?patient_id=${encodeURIComponent(patient.id)}`);
  const rows = [...visits].sort((a, b) => b.starts_at.localeCompare(a.starts_at)).map((v) => {
    const at = partsIn(new Date(v.starts_at));
    return el("div", { class: "list-item" },
      el("span", { class: "time", text: `${at.hour}:${at.minute}` }),
      el("span", { class: "what" }, el("strong", { text: longDate(`${at.year}-${at.month}-${at.day}`) }), el("br"), el("span", { class: "muted", text: v.kind })),
      chip(v.status));
  });
  return [card("Citas", "", ...(rows.length ? rows : [empty("Todavía no tiene citas.")]))];
}

async function tabMensajes(patient) {
  if (!state.session.clinician) return [empty("Los mensajes son solo para el rol de psicóloga.")];
  const conversations = (await api("/v1/inbox")).filter((c) => c.patient_id === patient.id);
  const rows = conversations.map((c) => {
    const [network, cls] = NETWORKS[c.network] || [c.network, "app"];
    return el("a", { class: "thread", href: `#mensajes/${encodeURIComponent(c.conversation_id)}` },
      el("span", {}, el("span", { class: `ch ${cls}`, text: network }), c.unread ? el("span", { class: "count", text: String(c.unread) }) : null),
      el("span", { text: c.preview || "" }));
  });
  return [card("Conversaciones vinculadas", "", ...(rows.length ? rows : [empty("No hay conversaciones vinculadas a esta paciente. Puedes vincularlas desde Mensajes.")]))];
}

// --- MENSAJES: one inbox for WhatsApp, Instagram and Facebook (ADR 0023) ---------------------------
const SENT = {
  sent: "Enviado",
  dry_run: "Guardado sin enviar: la cuenta de la red aún no está conectada",
  failed: "No se pudo enviar",
  uncertain: "Envío incierto: revisa en la red antes de volver a enviar",
};
const BUBBLE_NOTE = { received: "", sent: "Enviado", dry_run: "No enviado (cuenta sin conectar)", failed: "No se pudo enviar", uncertain: "Envío incierto" };
let inboxFilter = "";

VIEWS.mensajes = async (main, conversationId) => {
  if (!state.session.clinician) {
    main.replaceChildren(page("Mensajes", ""), empty("Los mensajes de pacientes son solo para el rol de psicóloga."));
    return;
  }
  await knownPatients();
  // The open conversation first: reading it marks it read, so the list below agrees.
  const right = conversationId ? await conversationCard(conversationId) : null;
  const list = await api(`/v1/inbox${inboxFilter ? `?network=${inboxFilter}` : ""}`);
  const filters = el("div", { class: "filters", role: "group", "aria-label": "Filtrar por red" },
    ...[["", "Todos"], ["whatsapp", "WhatsApp"], ["instagram", "Instagram"], ["facebook", "Facebook"]].map(([value, label]) => {
      const b = el("button", { type: "button", "aria-pressed": String(inboxFilter === value), text: label });
      b.addEventListener("click", () => { inboxFilter = value; route(); });
      return b;
    }));
  const threads = list.map((c) => {
    const [network, cls] = NETWORKS[c.network] || [c.network, "app"];
    const name = c.patient_id ? patientName(c.patient_id) : "Persona sin vincular";
    return el("a", { class: "thread", href: `#mensajes/${encodeURIComponent(c.conversation_id)}`, "aria-current": String(c.conversation_id === conversationId) },
      el("span", { class: "row tight" },
        el("span", { class: `ch ${cls}`, text: network }), el("strong", { text: name }),
        c.attention === "crisis" ? el("span", { class: "chip bad", text: "Crisis" }) : null,
        c.attention === "human" ? el("span", { class: "chip wait", text: "Pide una persona" }) : null,
        c.unread ? el("span", { class: "count", text: String(c.unread), "aria-label": `${c.unread} sin leer` }) : null),
      el("span", { class: "muted", text: c.preview && c.preview.length >= 80 ? `${c.preview}…` : c.preview || "" }));
  });
  const left = el("section", { class: "card grow-1", "aria-label": "Conversaciones" }, filters,
    ...(threads.length ? threads : [empty("No hay conversaciones todavía. Cuando alguien escriba por WhatsApp, Instagram o Facebook, aparecerá aquí.")]));
  main.replaceChildren(page("Mensajes", "WhatsApp, Instagram y Facebook en un solo lugar. La IA solo sugiere: tú decides qué se envía."),
    el("div", { class: "row" }, left, right || card("Conversación", "grow-2", empty("Elige una conversación de la lista."))));
};

let patientNames = {};
async function knownPatients() {
  const all = await patientsList();
  patientNames = Object.fromEntries(all.map((p) => [p.id, p.display_name]));
}
function patientName(id) { return patientNames[id] || "Paciente vinculada"; }

async function conversationCard(id) {
  const c = await api(`/v1/inbox/${encodeURIComponent(id)}`);
  refreshBadges();
  const [network, cls] = NETWORKS[c.network] || [c.network, "app"];
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const text = el("textarea", { rows: "4", maxlength: "2000", "aria-label": "Tu respuesta" });
  let draftId = null;

  const bubbles = c.messages.filter((m) => m.status !== "used").map((m) => {
    const when = partsIn(new Date(m.created_at));
    const stamp = `${when.day}/${when.month} ${when.hour}:${when.minute}`;
    if (m.status === "draft") {
      const use = el("button", { class: "btn small", type: "button", text: "Usar este borrador" });
      use.addEventListener("click", () => { text.value = m.text; draftId = m.message_id; text.focus(); });
      const drop = el("button", { class: "btn small", type: "button", text: "Descartar" });
      drop.addEventListener("click", async () => {
        drop.disabled = true;
        try { await api(`/v1/inbox/${encodeURIComponent(id)}/drafts/${encodeURIComponent(m.message_id)}`, { method: "DELETE" }); route(); } catch (err) { say(status, humanError(err), "error"); }
      });
      return el("div", { class: "bubble draft" }, el("strong", { text: "Borrador de la IA (no enviado)" }), el("br"), m.text,
        el("small", { text: stamp }), el("span", { class: "row tight" }, use, drop));
    }
    const note = m.direction === "out"
      ? [m.author?.startsWith("auto:") ? "Respuesta automática" : "", BUBBLE_NOTE[m.status] || ""].filter(Boolean).join(" · ")
      : "";
    return el("div", { class: `bubble ${m.direction === "in" ? "in" : "out"}` }, m.text, el("small", { text: [stamp, note].filter(Boolean).join(" · ") }));
  });

  const suggest = el("button", { class: "btn", type: "button", text: "Sugerir respuesta con IA" });
  const send = el("button", { class: "btn primary", type: "submit", text: "Enviar" });
  const person = c.attention === "crisis" || c.attention === "human";
  if (person) { suggest.disabled = true; suggest.title = "En una crisis o cuando piden una persona, responde tú."; }
  if (!c.window_open) { send.disabled = true; suggest.disabled = true; }
  suggest.addEventListener("click", async () => {
    suggest.disabled = true;
    say(status, "Preparando un borrador…");
    try {
      await api(`/v1/inbox/${encodeURIComponent(id)}/draft`, { method: "POST" });
      route();
    } catch (err) {
      say(status, err.status === 409 ? "Esta vez no salió una sugerencia segura. Escribe tú la respuesta." : humanError(err), "error");
      suggest.disabled = false;
    }
  });
  const form = el("form", { class: "stack", novalidate: true }, text, el("div", { class: "row tight" }, suggest, send), status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!text.value.trim()) return say(status, "Escribe la respuesta.", "error");
    send.disabled = true;
    say(status, "Enviando…");
    try {
      const out = await api(`/v1/inbox/${encodeURIComponent(id)}/reply`, { method: "POST", body: { body: text.value, draft_id: draftId } });
      say(status, SENT[out.status] || out.status, out.status === "sent" ? "done" : "error");
      if (out.status === "sent" || out.status === "dry_run") setTimeout(route, 900);
      else send.disabled = false;
    } catch (err) {
      say(status, /24-hour/.test(err.detail || "") ? "Pasaron más de 24 horas desde su último mensaje: la red ya no permite responder libremente."
        : /not to be contacted/.test(err.detail || "") ? "Esta persona pidió que no le escribamos (escribió BAJA o STOP)." : humanError(err), "error");
      send.disabled = false;
    }
  });

  // Link to a patient's record, and close.
  const options = Object.entries(patientNames).sort((a, b) => a[1].localeCompare(b[1], "es"));
  const linkTo = el("select", { "aria-label": "Vincular a una paciente" },
    el("option", { value: "", text: c.patient_id ? `Vinculada a ${patientName(c.patient_id)}` : "Vincular a una paciente…" }),
    ...options.map(([pid, name]) => el("option", { value: pid, text: name })));
  linkTo.addEventListener("change", async () => {
    if (!linkTo.value) return;
    try { await api(`/v1/inbox/${encodeURIComponent(id)}/patient`, { method: "PUT", body: { patient_id: linkTo.value } }); route(); } catch (err) { say(status, humanError(err), "error"); }
  });
  const close = el("button", { class: "btn small", type: "button", text: "Marcar como atendida" });
  close.addEventListener("click", async () => {
    close.disabled = true;
    try { await api(`/v1/inbox/${encodeURIComponent(id)}/close`, { method: "POST" }); refreshBadges(); route(); } catch (err) { say(status, humanError(err), "error"); close.disabled = false; }
  });

  return el("section", { class: "card grow-2", "aria-label": "Conversación" },
    el("div", { class: "head" },
      el("div", { class: "row tight" }, el("span", { class: `ch ${cls}`, text: network }),
        el("strong", { text: c.patient_id ? patientName(c.patient_id) : "Persona sin vincular" })),
      el("div", { class: "row tight" }, c.patient_id ? link(`#paciente/${encodeURIComponent(c.patient_id)}`, "Ver ficha", "btn small") : null, close)),
    c.attention === "crisis" ? el("div", { class: "alert", role: "alert" }, el("span", { text: "Posible crisis. Ya recibió la contención automática con el ECU 911 y la línea 171 (opción 6). Contáctala tú; la IA no sugiere respuestas aquí." })) : null,
    c.attention === "human" ? el("div", { class: "notice", role: "note" }, el("span", { text: "Pidió hablar con una persona. Respóndele tú." })) : null,
    !c.window_open ? el("div", { class: "notice", role: "note" }, el("span", { text: "Pasaron más de 24 horas desde su último mensaje: la red solo permite responder con plantillas aprobadas." })) : null,
    el("div", { class: "bubbles" }, ...bubbles),
    form,
    el("label", { class: "field" }, "Ficha", linkTo),
    el("p", { class: "muted", text: "Los mensajes se guardan cifrados y se borran a los 90 días. Cada lectura queda registrada." }));
}

// --- start -------------------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  $("#signin-form").addEventListener("submit", signIn);
  $("#signout").addEventListener("click", () => signOut());
  window.addEventListener("hashchange", route);
  if (keyStore.get()) {
    start().catch(() => signOut("Vuelve a entrar con tu llave personal."));
  } else {
    signOut();
  }
});
