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
      else localStorage.removeItem(KEY);
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
  const session = state.session;
  const resp = await request(path, options);
  const data = resp.status === 204 ? null : await resp.json();
  if (session && session !== state.session) throw new ApiError(0, "");
  return data;
}

/** A file from the API, saved by the browser (the key travels in a header, so a plain
 *  link cannot fetch it). */
async function download(path, filename) {
  const session = state.session;
  const blob = await (await request(path)).blob();
  // Reading the response body can outlive a sign-out or an account switch.
  if (!session || session !== state.session) throw new ApiError(0, "");
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

async function request(path, { method = "GET", body } = {}) {
  const sessionAtRequest = state.session;
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
  // A reply from a signed-out account must not update or sign out a newer one.
  if (sessionAtRequest && state.session !== sessionAtRequest) throw new ApiError(0, "");
  if (resp.status === 401 && state.session) {
    signOut("Tu sesión terminó. Vuelve a entrar.");
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

// A step that cannot be undone asks once more on the same button: the first press only
// arms it (it turns red and says what will happen), a second press within 6 seconds does it.
function sure(button, question) {
  if (button.dataset.armed) {
    clearTimeout(Number(button.dataset.armed));
    delete button.dataset.armed;
    button.textContent = button.dataset.label;
    button.classList.remove("danger");
    return true;
  }
  button.dataset.label = button.textContent;
  button.textContent = question;
  button.classList.add("danger");
  button.dataset.armed = String(setTimeout(() => {
    delete button.dataset.armed;
    button.textContent = button.dataset.label;
    button.classList.remove("danger");
  }, 6000));
  return false;
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
function canReadCare() {
  return state.session.roles.some((role) => ["admin", "owner", "reviewer", "reception"].includes(role));
}
/** "Psic. Carla Luzuriaga" or "carla.luzuriaga" -> "Carla": for the greeting. */
function firstName() {
  const words = myName().replace(/^(psic[oó]log[oa]|psic|dra|dr|lic|mgs|msc|mg)\.?\s+/i, "")
    .split(/[\s.]+/).filter(Boolean);
  const first = words[0] || myName();
  return first.charAt(0).toUpperCase() + first.slice(1);
}

// --- sign in and out -------------------------------------------------------------------------
// With e-mail (ADR 0024): e-mail and password, then the 6-digit code of an authenticator
// app. A personal link "#clave=..." (from Equipo, or from Tu cuenta) leads to choosing a
// password and adding the app; its token never stays in the address bar. The personal
// key stays as break-glass access.
const auth = { challenge: null, token: null, enroll: null, generation: 0 };
const STEPS = { email: "#email-form", code: "#code-form", password: "#password-form", enroll: "#enroll-form", key: "#signin-form" };

function showStep(step) {
  for (const [name, sel] of Object.entries(STEPS)) $(sel).hidden = name !== step;
  $("#use-key").hidden = step !== "email";
  $("#use-email").hidden = step !== "key";
  $(STEPS[step]).querySelector("input")?.focus();
}

/** The QR code the server computed, drawn as one SVG path (no script, no inline style). */
function drawQr(rows) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${rows.length} ${rows.length}`);
  svg.setAttribute("shape-rendering", "crispEdges");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Código QR para agregar el consultorio a tu app autenticadora");
  let d = "";
  rows.forEach((row, y) => { for (let x = 0; x < row.length; x += 1) if (row[x] === "1") d += `M${x} ${y}h1v1h-1z`; });
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "#000");
  svg.append(path);
  return svg;
}

async function emailStep(event) {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  if (submit.disabled) return;
  const status = $("#signin-status");
  const email = $("#signin-email").value.trim();
  const password = $("#signin-password").value;
  if (!email || !password) return say(status, "Escribe tu correo y tu contraseña.", "error");
  const generation = ++auth.generation;
  submit.disabled = true;
  say(status, "Revisando…");
  try {
    const started = await api("/v1/auth/sign-in", { method: "POST", body: { email, password } });
    if (generation !== auth.generation) return;
    auth.challenge = started.challenge;
    $("#signin-password").value = "";
    $("#code-sent").textContent = "Abre tu app autenticadora y escribe el código de 6 números que muestra para el consultorio. Cambia cada 30 segundos.";
    say(status, "");
    showStep("code");
  } catch (err) {
    if (generation !== auth.generation) return;
    say(status, err.status === 401 ? "Correo o contraseña incorrectos."
      : err.status === 429 ? "Demasiados intentos seguidos. Espera un minuto." : humanError(err), "error");
  } finally {
    if (generation === auth.generation) submit.disabled = false;
  }
}

async function codeStep(event) {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  if (submit.disabled) return;
  const status = $("#signin-status");
  const code = $("#signin-code").value.replace(/\D/g, "");
  if (code.length !== 6) return say(status, "Escribe los 6 números del código.", "error");
  const remember = $("#code-remember").checked;
  const generation = ++auth.generation;
  submit.disabled = true;
  say(status, "Entrando…");
  try {
    const session = await api("/v1/auth/code", { method: "POST", body: { challenge: auth.challenge, code, remember } });
    if (generation !== auth.generation) return;
    keyStore.set(session.key, remember);
    $("#signin-code").value = "";
    await start();
    say(status, "");
  } catch (err) {
    if (generation !== auth.generation) return;
    say(status, err.status === 401 ? "El código no es correcto. Escribe el que muestra la app ahora, antes de que cambie. Si fallas varias veces, vuelve a empezar."
      : humanError(err), "error");
  } finally {
    if (generation === auth.generation) submit.disabled = false;
  }
}

function forgotPassword() {
  say($("#signin-status"), "Pide a la administración del consultorio un enlace nuevo. Con él eliges otra contraseña (y, si cambiaste de celular, vuelves a agregar la app).", "done");
}

function cancelSetup() {
  auth.challenge = null;
  auth.token = null;
  auth.enroll = null;
  $("#password-form").reset();
  $("#enroll-form").reset();
  $("#enroll-secret").textContent = "";
  $("#enroll-qr").replaceChildren();
  $("#enroll-open").removeAttribute("href");
  signOut();
}

async function passwordStep(event) {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  if (submit.disabled) return;
  const status = $("#signin-status");
  const password = $("#new-password").value;
  if (password.length < 10) return say(status, "La contraseña necesita al menos 10 caracteres.", "error");
  if (password !== $("#new-password-2").value) return say(status, "Las dos contraseñas no son iguales.", "error");
  const generation = ++auth.generation;
  submit.disabled = true;
  say(status, "Revisando…");
  try {
    const step = await api("/v1/auth/link", { method: "POST", body: { token: auth.token, password } });
    if (generation !== auth.generation) return;
    auth.enroll = step.challenge;
    $("#new-password").value = "";
    $("#new-password-2").value = "";
    $("#enroll-open").setAttribute("href", step.uri);
    $("#enroll-qr").replaceChildren(drawQr(step.qr));
    $("#enroll-secret").textContent = step.secret.replace(/(.{4})/g, "$1 ").trim();
    say(status, "");
    showStep("enroll");
  } catch (err) {
    if (generation !== auth.generation) return;
    say(status, err.status === 410 ? "Este enlace ya se usó o venció. Pide uno nuevo a la administración del consultorio."
      : err.status === 422 && err.detail ? err.detail : humanError(err), "error");
  } finally {
    if (generation === auth.generation) submit.disabled = false;
  }
}

async function enrollStep(event) {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  if (submit.disabled) return;
  const status = $("#signin-status");
  const code = $("#enroll-code").value.replace(/\D/g, "");
  if (code.length !== 6) return say(status, "Escribe los 6 números que muestra la app.", "error");
  const remember = $("#enroll-remember").checked;
  const generation = ++auth.generation;
  submit.disabled = true;
  say(status, "Entrando…");
  try {
    const session = await api("/v1/auth/enroll", { method: "POST", body: { challenge: auth.enroll, code, remember } });
    if (generation !== auth.generation) return;
    auth.token = null;
    auth.enroll = null;
    $("#enroll-code").value = "";
    $("#enroll-secret").textContent = "";
    $("#enroll-qr").replaceChildren();
    keyStore.set(session.key, remember);
    await start();
    say(status, "");
  } catch (err) {
    if (generation !== auth.generation) return;
    say(status, err.status === 401 ? "El código no es correcto. Revisa que sea el del consultorio y escríbelo antes de que cambie."
      : err.status === 410 ? "Este paso venció. Vuelve a abrir tu enlace para empezar de nuevo." : humanError(err), "error");
  } finally {
    if (generation === auth.generation) submit.disabled = false;
  }
}

/** A link from an invitation or a reset: keep its token in memory, out of the address bar. */
function takeLinkToken() {
  const found = /^#clave=([\w-]{20,100})$/.exec(location.hash);
  if (!found) return false;
  ++auth.generation;
  auth.token = found[1];
  history.replaceState(null, "", location.pathname);
  here = "";
  keyStore.clear();
  state.session = null;
  state.patients = null;
  state.weekStart = null;
  patientNames = {};
  inboxFilter = "";
  ++turn;
  $("#view").replaceChildren();
  if ($("#dialog").open) $("#dialog").close();
  $("#dialog").replaceChildren();
  $("#app").hidden = true;
  $("#signin").hidden = false;
  say($("#signin-status"), "");
  showStep("password");
  return true;
}

async function signIn(event) {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type=submit]");
  if (submit.disabled) return;
  const status = $("#signin-status");
  const key = $("#signin-key").value.trim();
  if (!key) return say(status, "Escribe tu llave personal.", "error");
  const generation = ++auth.generation;
  submit.disabled = true;
  keyStore.set(key, $("#signin-remember").checked);
  say(status, "Entrando…");
  try {
    await start();
    if (generation !== auth.generation) return;
    $("#signin-key").value = "";
    say(status, "");
  } catch (err) {
    if (generation !== auth.generation) return;
    keyStore.clear();
    say(status, err instanceof ApiError && (err.status === 401 || err.status === 403)
      ? "Esa llave no es válida para el consultorio. Revísala e inténtalo de nuevo."
      : humanError(err), "error");
  } finally {
    if (generation === auth.generation) submit.disabled = false;
  }
}

function signOut(message = "") {
  ++auth.generation;
  auth.challenge = null;
  auth.token = null;
  auth.enroll = null;
  for (const selector of Object.values(STEPS)) {
    $(selector).reset();
    $(selector).querySelector("button[type=submit]").disabled = false;
  }
  $("#enroll-secret").textContent = "";
  $("#enroll-qr").replaceChildren();
  $("#enroll-open").removeAttribute("href");
  setBadge("#badge-alerts", 0, "");
  setBadge("#badge-unread", 0, "");
  const key = keyStore.get() || memoryKey;
  if (key && key.startsWith("ss_")) {
    // End the e-mail session on the server too; the page forgets it either way.
    fetch("/v1/auth/sign-out", { method: "POST", headers: { "X-API-Key": key } }).catch(() => {});
  }
  keyStore.clear();
  state.session = null;
  state.patients = null;
  state.weekStart = null;
  patientNames = {};
  inboxFilter = "";
  ++turn; // invalidate requests still drawing a screen from the previous account
  $("#view").replaceChildren();
  if ($("#dialog").open) $("#dialog").close();
  $("#dialog").replaceChildren();
  $("#app").hidden = true;
  $("#signin").hidden = false;
  showStep("email");
  say($("#signin-status"), message, message ? "error" : "");
}

async function start() {
  const generation = auth.generation;
  const key = keyStore.get() || memoryKey;
  const session = await api("/v1/session");
  if (generation !== auth.generation || key !== (keyStore.get() || memoryKey)) return;
  state.session = session;
  state.tz = state.session.timezone || state.tz;
  $("#me-name").textContent = myName();
  $("#me-initials").textContent = initials(myName());
  const practice = state.session.practice;
  $("#me-practice").textContent = practice && practice !== myName() ? practice : "Consultorio";
  $("#signin").hidden = true;
  $("#app").hidden = false;
  for (const link of document.querySelectorAll("[data-nav]")) {
    link.hidden = ["hoy", "agenda", "pacientes", "mensajes"].includes(link.dataset.nav) && !canReadCare();
  }
  if (!location.hash) location.hash = canReadCare() ? "#hoy" : state.session.roles.includes("marketing") ? "#marketing/publicaciones" : "#ajustes";
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
// Text typed into a note or a reply and not saved yet (a successful save clears the mark).
const unsavedText = () => [...document.querySelectorAll("textarea[data-dirty]")].some((t) => t.value.trim());
let here = location.hash;
let turn = 0; // the latest screen asked for: a slower, older one never paints over it

function route() {
  if (!state.session) return;
  const mine = ++turn;
  const open = $("#dialog");
  if (open.open) open.close(); // a window from the screen left behind must not block this one
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
  Promise.resolve()
    .then(() => view(frame, ...rest.map(decodeURIComponent)))
    .then(() => {
      if (mine !== turn) return;
      main.replaceChildren(...frame.childNodes);
      main.querySelector("[data-focus]")?.focus();
    })
    .catch((err) => {
      if (mine === turn) main.replaceChildren(el("div", { class: "alert", role: "alert" },
        el("span", { text: err instanceof URIError ? "El enlace no es válido." : humanError(err) }),
        link("#hoy", "Volver a Hoy")));
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
    // A symbol gets a spoken name; a word speaks for itself (the name must contain it).
    const b = el("button", { class: "btn", type: "button", "aria-label": label || null, text });
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
    nav(-7, "Semana anterior", "‹"), nav(0, "", "Hoy"), nav(7, "Semana siguiente", "›"),
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
  if (arg === "nueva" && location.hash === "#agenda/nueva") openVisitDialog({});
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
  const b = el("button", { class: "slot f", type: "button" },
    el("b", { text: slot.local.slice(11) }), " ", el("span", { text: "Libre" }),
    el("span", { class: "sr", text: ": agendar a esta hora" }));
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
  const screen = turn;
  let patients;
  try {
    patients = await patientsList();
  } catch (err) {
    if (screen === turn && state.session) dialog("No se pudo abrir la cita", el("p", { role: "alert", text: humanError(err) }));
    return;
  }
  if (screen !== turn || !state.session) return;
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
      if (value === "cancelled" && !sure(b, "Sí, cancelar la cita")) return;
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
  if (section === "redes") return socialAccountsView(main);
  if (section === "test" && which) return testEditor(main, which);
  const parts = [page("Ajustes", "Tu perfil, tu horario, tus tests y la guardia", connectSocialButton())];
  parts.push(s.professional ? await hoursCard(s.professional) : profileCard());
  if (s.clinician) parts.push(await testsCard());
  if (canReadCare()) parts.push(await onCallCard());
  if (isAdmin()) parts.push(await teamCard());
  parts.push(accountCard());
  main.replaceChildren(...parts);
};

// Account setup uses credential names only (ADR 0015); tokens stay on the server.
const SOCIAL_CONNECTIONS = {
  whatsapp: { name: "WhatsApp", id: "ID del número de WhatsApp Business", ref: "WA_MAIN" },
  instagram: { name: "Instagram", id: "ID de la cuenta profesional de Instagram", ref: "IG_MAIN" },
  facebook: { name: "Facebook", id: "ID de la página de Facebook", ref: "FB_MAIN" },
  tiktok: { name: "TikTok", id: "ID de la cuenta de TikTok", ref: "TT_MAIN" },
  telegram: { name: "Telegram", id: "ID del bot de Telegram", ref: "TG_MAIN" },
};

function connectSocialButton() {
  return isAdmin() ? link("#ajustes/redes", "Conectar redes sociales", "btn primary") : null;
}

async function socialAccountsView(main) {
  const head = page("Redes sociales", "Cuentas del consultorio para mensajes y publicaciones.", link("#ajustes", "Volver a Ajustes"));
  if (!isAdmin()) {
    main.replaceChildren(head, empty("Pide a la administración del consultorio que conecte las redes sociales."));
    return;
  }
  // A failed read must remain an error, rather than looking like no accounts exist.
  const accounts = await api("/v1/social/accounts");
  const list = el("div", { class: "stack", "aria-live": "polite" });
  const renderAccounts = () => {
    const active = accounts.filter((a) => a.active !== false);
    list.replaceChildren(...(active.length ? active.map((account) => {
      const status = el("p", { class: "status", role: "status" });
      const disconnect = el("button", { class: "btn small", type: "button", text: "Desconectar" });
      disconnect.addEventListener("click", async () => {
        if (!sure(disconnect, "Sí, desconectar")) return;
        disconnect.disabled = true;
        try {
          await api(`/v1/social/accounts/${encodeURIComponent(account.account_id)}`, { method: "DELETE" });
          account.active = false;
          renderAccounts();
        } catch (err) {
          say(status, humanError(err), "error");
          disconnect.disabled = false;
        }
      });
      const name = SOCIAL_CONNECTIONS[account.network]?.name || account.network;
      return el("article", { class: "doc social-account" },
        el("div", { class: "what" }, el("strong", { text: `${name} · ${account.handle}` }),
          el("p", { class: "muted", text: account.secret_configured
            ? "La autorización está configurada en el consultorio."
            : "Falta preparar la autorización de esta cuenta para poder enviar o publicar." }), status),
        el("span", { class: `chip ${account.secret_configured ? "ok" : "wait"}`, text: account.secret_configured ? "Configuración lista" : "Pendiente de autorización" }),
        disconnect);
    }) : [empty("Todavía no hay cuentas vinculadas. Agrega la primera aquí.")]));
  };
  renderAccounts();

  const network = el("select", { "aria-label": "Red social" }, ...Object.entries(SOCIAL_CONNECTIONS).map(([value, item]) => el("option", { value, text: item.name })));
  const handle = el("input", { type: "text", maxlength: "120", required: true, autocomplete: "off", placeholder: "Ej.: @consultorio" });
  const externalId = el("input", { type: "text", maxlength: "128", required: true, autocomplete: "off", spellcheck: "false" });
  const idLabel = el("span", { text: SOCIAL_CONNECTIONS.whatsapp.id });
  const secretRef = el("input", { type: "text", maxlength: "64", value: SOCIAL_CONNECTIONS.whatsapp.ref, autocomplete: "off", spellcheck: "false" });
  const details = el("details", {}, el("summary", { text: "Configuración de la conexión" }),
    el("label", { class: "field" }, "Nombre de conexión", secretRef),
    el("p", { class: "muted", text: "Usa el nombre de conexión preparado por la administración para esta cuenta. La autorización se guarda en el servidor." }));
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Vincular cuenta" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Red social", network),
    el("label", { class: "field" }, "Nombre de la cuenta", handle),
    el("label", { class: "field" }, idLabel, externalId),
    details, save, status);
  network.addEventListener("change", () => {
    const selected = SOCIAL_CONNECTIONS[network.value];
    idLabel.textContent = selected.id;
    externalId.value = "";
    secretRef.value = selected.ref;
    say(status, "");
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (save.disabled) return;
    if (!handle.value.trim()) {
      say(status, "Escribe el nombre de la cuenta.", "error");
      handle.focus();
      return;
    }
    if (!/^[\w.:@+-]{1,128}$/.test(externalId.value.trim())) {
      say(status, "Escribe el identificador que te proporciona la red para esta cuenta.", "error");
      externalId.focus();
      return;
    }
    if (!/^[A-Z][A-Z0-9_]{2,63}$/.test(secretRef.value.trim())) {
      say(status, "Revisa el nombre de conexión con la administración.", "error");
      details.open = true;
      secretRef.focus();
      return;
    }
    save.disabled = true;
    say(status, "Guardando cuenta…");
    try {
      const account = await api("/v1/social/accounts", { method: "POST", body: {
        network: network.value, external_id: externalId.value.trim(), handle: handle.value.trim(), secret_ref: secretRef.value.trim(),
      } });
      accounts.push(account);
      renderAccounts();
      handle.value = "";
      externalId.value = "";
      say(status, account.secret_configured ? "Cuenta guardada. Su autorización está configurada."
        : "Cuenta guardada. Falta preparar su autorización antes de enviar o publicar.", account.secret_configured ? "done" : "");
    } catch (err) {
      say(status, err.status === 409 ? "Esta cuenta ya está vinculada. Revísala en la lista."
        : err.status === 422 ? "Revisa los datos de la cuenta y el nombre de conexión." : humanError(err), "error");
    } finally {
      save.disabled = false;
    }
  });
  main.replaceChildren(head, card("Cuentas vinculadas", "", list), card("Agregar cuenta", "", form));
}

const ROLE_NAMES = {
  admin: "administración", owner: "dueña", reviewer: "psicóloga (datos clínicos)", reception: "recepción",
  marketing: "marketing", privacy: "protección de datos",
};
const roleList = (roles) => roles.map((r) => ROLE_NAMES[r] || r).join(", ");
// What each profile opens, in the words of the team (see the manual's "Quién ve qué").
const ROLE_CHOICES = [
  ["reviewer", "Psicóloga", "historias clínicas, tests y mensajes de pacientes"],
  ["reception", "Recepción", "agenda, datos de contacto y enlaces de la app"],
  ["owner", "Dueña", "informe del mes, talleres y aprobaciones"],
  ["marketing", "Marketing", "campañas y publicaciones"],
  ["admin", "Administración", "configura el consultorio y el equipo; no ve historias clínicas"],
  ["privacy", "Privacidad", "pedidos de las pacientes sobre sus datos"],
];

function accountCard() {
  const s = state.session;
  const parts = [
    el("p", { text: `Entraste como ${s.id}.` }),
    el("p", { class: "muted", text: `Permisos: ${roleList(s.roles)}.` }),
  ];
  if (s.kind !== "staff") return card("Tu cuenta", "", ...parts);
  const login = s.email_login;
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  if (login && login.active) {
    parts.push(el("p", { text: `Entras con tu correo ${login.email}, tu contraseña y tu app autenticadora. Si cambias de celular, pide un enlace nuevo a la administración.` }));
    return card("Tu cuenta", "", ...parts);
  }
  const email = el("input", { type: "email", maxlength: "254", autocomplete: "email", spellcheck: "false", value: login ? login.email : "" });
  const send = el("button", { class: "btn", type: "submit", text: "Configurar ahora" });
  const form = el("form", { class: "stack", novalidate: true },
    el("p", { text: "Puedes entrar con tu correo, una contraseña y tu app autenticadora en vez de la llave. Escribe tu correo: en dos pasos creas la contraseña y agregas la app. Tu llave seguirá sirviendo como llave de emergencia." }),
    el("label", { class: "field" }, "Tu correo", email), send, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!email.value.trim()) return say(status, "Escribe tu correo.", "error");
    send.disabled = true;
    try {
      const made = await api("/v1/session/email", { method: "POST", body: { email: email.value.trim() } });
      location.hash = new URL(made.link).hash; // opens the two steps right here
    } catch (err) {
      say(status, err.status === 409 ? "Ese correo ya lo usa otra persona del equipo."
        : err.status === 422 ? "Ese correo no parece válido. Revísalo." : humanError(err), "error");
    } finally { send.disabled = false; }
  });
  return card("Tu cuenta", "", ...parts, form);
}

const MEMBER_STATES = {
  active: ["Activa", "ok"], invited: ["Falta usar su enlace", "wait"], key_only: ["Entra con llave", "neutral"],
};

/** A personal link to hand over: copy it and send it by WhatsApp (never shown again). */
function linkBox(who, made) {
  const field = el("input", { type: "text", readonly: true, value: made.link, "aria-label": `Enlace de acceso de ${who}` });
  const note = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const copy = el("button", { class: "btn small", type: "button", text: "Copiar enlace" });
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(made.link); say(note, "Enlace copiado.", "done"); }
    catch { field.select(); say(note, "Selecciónalo y cópialo con Ctrl+C.", ""); }
  });
  return el("div", { class: "link-box" },
    el("p", { text: `Envíale este enlace a ${who} por WhatsApp. Con él crea su contraseña y agrega el consultorio a su app autenticadora. Vence en 72 horas y sirve una sola vez.` }),
    field, copy, note);
}

async function teamCard() {
  const members = (await api("/v1/admin/team")).filter((m) => m.status !== "revoked");
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const rows = members.map((m) => {
    const [label, cls] = MEMBER_STATES[m.status] || [m.status, "neutral"];
    const actions = [];
    const shown = el("div");
    if (m.email && m.id !== state.session.id) {
      const access = el("button", { class: "btn small", type: "button", text: "Enlace de acceso" });
      access.addEventListener("click", async () => {
        access.disabled = true;
        try {
          shown.replaceChildren(linkBox(m.name, await api(`/v1/admin/team/${encodeURIComponent(m.id)}/link`, { method: "POST" })));
        } catch (err) { say(status, humanError(err), "error"); }
        finally { access.disabled = false; }
      });
      actions.push(access);
    }
    if (m.id !== state.session.id) {
      const drop = el("button", { class: "btn small", type: "button", text: "Quitar acceso" });
      drop.addEventListener("click", async () => {
        if (!sure(drop, "Sí, quitar acceso")) return;
        drop.disabled = true;
        try { await api(`/v1/admin/team/${encodeURIComponent(m.id)}`, { method: "DELETE" }); route(); }
        catch (err) { say(status, humanError(err), "error"); drop.disabled = false; }
      });
      actions.push(drop);
    }
    return el("div", { class: "stack" },
      el("div", { class: "doc" },
        el("span", { class: "what" }, el("strong", { text: m.name }), el("br"),
          el("span", { class: "muted", text: `${m.email || "sin correo"} · ${roleList(m.roles)}` })),
        el("span", { class: `chip ${cls}`, text: label }), ...actions),
      shown);
  });

  const name = el("input", { type: "text", maxlength: "120", required: true, autocomplete: "off", placeholder: "Ej.: Ana Pérez" });
  const email = el("input", { type: "email", maxlength: "254", required: true, autocomplete: "off", spellcheck: "false" });
  const boxes = ROLE_CHOICES.map(([value, title, what]) => {
    const box = el("input", { type: "checkbox", value });
    return [box, el("label", { class: "check" }, box, el("span", {}, el("strong", { text: title }), ` — ${what}`))];
  });
  const invite = el("button", { class: "btn primary", type: "submit", text: "Invitar" });
  const form = el("form", { class: "stack", novalidate: true },
    el("div", { class: "fields" }, el("label", { class: "field" }, "Nombre", name), el("label", { class: "field" }, "Correo", email)),
    el("fieldset", { class: "roles" }, el("legend", { text: "Qué puede hacer (solo lo que su trabajo necesita)" }), ...boxes.map(([, label]) => label)),
    invite);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const roles = boxes.filter(([box]) => box.checked).map(([box]) => box.value);
    if (!name.value.trim() || !email.value.trim()) return say(status, "Escribe el nombre y el correo.", "error");
    if (!roles.length) return say(status, "Marca al menos un permiso.", "error");
    invite.disabled = true;
    try {
      const made = await api("/v1/admin/team", { method: "POST", body: { name: name.value.trim(), email: email.value.trim(), roles } });
      form.replaceWith(linkBox(made.name, made));
      say(status, `Listo: ${made.name} quedó en el equipo. Ahora envíale su enlace.`, "done");
    } catch (err) {
      say(status, err.status === 409 ? "Ese correo ya lo usa otra persona del equipo."
        : err.status === 422 ? "Ese correo no parece válido. Revísalo." : humanError(err), "error");
      invite.disabled = false;
    }
  });
  return card("Equipo", "",
    el("p", { class: "muted", text: "Cada persona entra con su correo, su contraseña y el código de su app autenticadora. Si alguien olvida su contraseña o cambia de celular, dale un «Enlace de acceso». Cuando alguien deja el consultorio, quítale el acceso ese mismo día." }),
    ...(rows.length ? rows : [empty("Todavía no hay nadie más en el equipo.")]),
    el("details", {}, el("summary", { text: "Agregar a una persona" }), form),
    status);
}

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
      link(`#ajustes/test/${encodeURIComponent(t.instrument_id)}`, "Editar test", "btn small"));
  });
  return card("Tus tests", "",
    ...(rows.length ? rows : [empty("Aún no tienes tests. Empieza con una plantilla de dominio público.")]),
    el("div", { class: "row tight" }, add("phq9", "Agregar PHQ-9 (depresión)"), add("gad7", "Agregar GAD-7 (ansiedad)")),
    link("#ajustes/test/nuevo", "Crear test propio", "btn"),
    status,
    el("p", { class: "muted", text: "Las bandas y alertas de PHQ-9/GAD-7 vienen de la plantilla estándar. Solo el equipo clínico las ve. Los tests propios no tendrán bandas ni alertas hasta que las configures. Por derechos de autor, las plantillas no traen el texto de las preguntas: cópialo del formulario oficial publicado." }));
}

function itemEditor(item, index, onRemove) {
  const id = el("input", { type: "text", required: true, maxlength: "32", value: item.id || `q${index + 1}` });
  const text = el("textarea", { rows: "2", required: true, maxlength: "1000" });
  text.value = item.text?.includes(PLACEHOLDER) ? "" : item.text || "";
  const type = el("select", {},
    el("option", { value: "choice", text: "Opciones de respuesta" }),
    el("option", { value: "number", text: "Número" }),
    el("option", { value: "text", text: "Respuesta abierta" }));
  type.value = item.type || "choice";
  const options = el("textarea", { rows: "3" });
  options.value = (item.options || []).map((option) => `${option.label}=${option.value}`).join("\n");
  const min = el("input", { type: "number", step: "any", value: item.min ?? "" });
  const max = el("input", { type: "number", step: "any", value: item.max ?? "" });
  const required = el("input", { type: "checkbox", checked: item.required !== false });
  const scored = el("input", { type: "checkbox", checked: item.scored !== false });
  const reverse = el("input", { type: "checkbox", checked: item.reverse === true });
  const optionsField = el("label", { class: "field" }, "Opciones (una por línea: texto=valor numérico)", options);
  const rangeFields = el("div", { class: "fields" },
    el("label", { class: "field" }, "Número mínimo", min),
    el("label", { class: "field" }, "Número máximo", max));
  const reverseField = el("label", { class: "check" }, reverse, "Puntuar al revés");
  const remove = el("button", { class: "btn small", type: "button", text: "Quitar pregunta" });
  const node = el("div", { class: "stack card" },
    el("div", { class: "head" }, el("strong", { text: `Pregunta ${index + 1}` }), remove),
    el("label", { class: "field" }, "ID interno", id),
    el("label", { class: "field" }, "Enunciado", text),
    el("label", { class: "field" }, "Tipo de respuesta", type),
    optionsField, rangeFields,
    el("div", { class: "row tight" },
      el("label", { class: "check" }, required, "Respuesta obligatoria"),
      el("label", { class: "check" }, scored, "Incluir en la puntuación"),
      reverseField));
  const sync = () => {
    optionsField.hidden = type.value !== "choice";
    rangeFields.hidden = type.value !== "number";
    reverseField.hidden = type.value !== "choice";
    if (type.value === "text") {
      scored.checked = false;
      scored.disabled = true;
    } else scored.disabled = false;
  };
  type.addEventListener("change", sync);
  remove.addEventListener("click", () => onRemove(node));
  sync();
  return {
    node,
    id,
    read() {
      const out = {
        id: id.value.trim(),
        text: text.value.trim(),
        type: type.value,
        required: required.checked,
        scored: scored.checked,
      };
      if (type.value === "choice") {
        out.options = options.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
          const split = line.lastIndexOf("=");
          const value = Number(line.slice(split + 1).trim());
          if (split < 1 || !Number.isFinite(value)) throw new Error(`Revisa las opciones de ${out.id}: usa texto=valor.`);
          return { label: line.slice(0, split).trim(), value };
        });
        out.reverse = reverse.checked;
      } else if (type.value === "number") {
        out.min = min.value === "" ? null : Number(min.value);
        out.max = max.value === "" ? null : Number(max.value);
      } else out.scored = false;
      return out;
    },
  };
}

function parseMetricLines(value, parse, label) {
  return value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line, i) => {
    try { return parse(line); }
    catch (err) { throw new Error(`${label}, línea ${i + 1}: ${err.message}`); }
  });
}

async function testEditor(main, id) {
  const creating = id === "nuevo";
  const test = creating ? null : await api(`/v1/clinical/instruments/${encodeURIComponent(id)}`);
  const spec = test?.spec || {
    name: "",
    description: "",
    instructions: "",
    administration: "professional",
    items: [{ id: "q1", text: "", type: "choice", options: [{ label: "No", value: 0 }, { label: "Sí", value: 1 }], required: true, scored: true }],
    scoring: { method: "sum", subscales: {}, bands: [], alerts: [] },
    licence: { source: "propio", public_domain: false, attestation: false, note: "" },
  };
  const name = el("input", { type: "text", required: true, maxlength: "120", value: spec.name || "" });
  const instructions = el("textarea", { rows: "3", maxlength: "2000" });
  instructions.value = spec.instructions || "";
  const administration = el("select", {},
    el("option", { value: "professional", text: "La doctora lo aplica en sesión" }),
    el("option", { value: "self_report", text: "La paciente lo responde en su app" }));
  administration.value = spec.administration || "professional";
  const description = el("textarea", { rows: "2", maxlength: "2000" });
  description.value = spec.description || "";
  const itemList = el("div", { class: "stack" });
  const items = [];
  const addItem = (item) => {
    if (items.length >= 200) return;
    const entry = itemEditor(item, items.length, (node) => {
      const index = items.findIndex((value) => value.node === node);
      if (index >= 0) items.splice(index, 1);
      node.remove();
    });
    items.push(entry);
    itemList.append(entry.node);
  };
  for (const item of spec.items) addItem(item);
  const addQuestion = el("button", { class: "btn small", type: "button", text: "Agregar pregunta" });
  addQuestion.addEventListener("click", () => {
    let n = 1;
    while (items.some((entry) => entry.id.value === `q${n}`)) n += 1;
    addItem({
      id: `q${n}`, text: "", type: "choice",
      options: [{ label: "No", value: 0 }, { label: "Sí", value: 1 }], required: true, scored: true,
    });
  });
  const method = el("select", {},
    el("option", { value: "sum", text: "Sumar las respuestas" }),
    el("option", { value: "mean", text: "Promediar las respuestas" }),
    el("option", { value: "none", text: "Sin puntuación total" }));
  method.value = spec.scoring?.method || "sum";
  const subscales = el("textarea", { rows: "3" });
  subscales.value = Object.entries(spec.scoring?.subscales || {}).map(([key, members]) => `${key}: ${members.join(", ")}`).join("\n");
  const bands = el("textarea", { rows: "4" });
  bands.value = (spec.scoring?.bands || []).map((band) => `${band.min}|${band.max}|${band.label}|${band.severity}`).join("\n");
  const alerts = el("textarea", { rows: "3" });
  alerts.value = (spec.scoring?.alerts || []).map((rule) => `${rule.item}|${rule.op}|${rule.value}|${rule.message}`).join("\n");
  const licenceSource = el("input", { type: "text", required: true, maxlength: "300", value: spec.licence?.source || "propio" });
  const publicDomain = el("input", { type: "checkbox", checked: spec.licence?.public_domain === true });
  const attestation = el("input", { type: "checkbox", checked: spec.licence?.attestation === true });
  const licenceNote = el("textarea", { rows: "2", maxlength: "500" });
  licenceNote.value = spec.licence?.note || "";
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: creating ? "Crear test" : "Guardar test" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Nombre del test", name),
    el("label", { class: "field" }, "Instrucciones para la paciente", instructions),
    el("label", { class: "field" }, "Aplicación", administration),
    el("label", { class: "field" }, "Descripción interna (opcional)", description),
    el("h3", { text: "Preguntas" }), itemList, addQuestion,
    el("h3", { text: "Puntuación y métricas calculadas" }),
    el("label", { class: "field" }, "Método de puntuación", method),
    el("label", { class: "field" }, "Subescalas (una por línea: nombre: q1, q2)", subscales),
    el("label", { class: "field" }, "Bandas (una por línea: mínimo|máximo|nombre|none/low/moderate/high)", bands),
    el("label", { class: "field" }, "Alertas (una por línea: id_pregunta|gte/lte/eq|valor|mensaje)", alerts),
    el("p", { class: "muted", text: "Una puntuación total puede mostrarse sin banda. Las bandas solo aparecen en la consola clínica y son una ayuda, no un diagnóstico. No escribas una banda ni una alerta si no la has definido." }),
    el("h3", { text: "Uso y licencia" }),
    el("label", { class: "field" }, "Fuente o titular de derechos", licenceSource),
    el("label", { class: "check" }, publicDomain, "La licencia es de dominio público"),
    el("label", { class: "check" }, attestation, "Confirmo que el consultorio tiene derecho a usar el texto de este test"),
    el("label", { class: "field" }, "Nota de licencia (opcional)", licenceNote),
    save, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!name.value.trim() || !licenceSource.value.trim()) return say(status, "Escribe el nombre del test y la fuente de licencia.", "error");
    if (!attestation.checked) return say(status, "Confirma que el consultorio tiene derecho a usar el texto de este test.", "error");
    if (!items.length) return say(status, "Agrega al menos una pregunta.", "error");
    save.disabled = true;
    let revised;
    try {
      const ids = new Set();
      const newItems = items.map((entry) => entry.read());
      for (const item of newItems) {
        if (ids.has(item.id)) throw new Error("Cada pregunta necesita un ID distinto.");
        ids.add(item.id);
      }
      const newSubscales = {};
      for (const line of subscales.value.split(/\r?\n/).map((part) => part.trim()).filter(Boolean)) {
        const split = line.indexOf(":");
        if (split < 1) throw new Error("Usa nombre: id_pregunta, id_pregunta en cada subescala.");
        newSubscales[line.slice(0, split).trim()] = line.slice(split + 1).split(",").map((item) => item.trim()).filter(Boolean);
      }
      const newBands = parseMetricLines(bands.value, (line) => {
        const [minText, maxText, label, severity, ...extra] = line.split("|").map((part) => part.trim());
        const min = Number(minText), max = Number(maxText);
        if (extra.length || !Number.isFinite(min) || !Number.isFinite(max) || !label || !["none", "low", "moderate", "high"].includes(severity)) {
          throw new Error("usa mínimo|máximo|nombre|none/low/moderate/high");
        }
        return { min, max, label, severity };
      }, "Banda");
      const newAlerts = parseMetricLines(alerts.value, (line) => {
        const [item, op, rawValue, ...message] = line.split("|").map((part) => part.trim());
        const value = Number(rawValue);
        if (message.length !== 1 || !item || !["gte", "lte", "eq"].includes(op) || !Number.isFinite(value) || !message[0]) {
          throw new Error("usa id_pregunta|gte/lte/eq|valor|mensaje");
        }
        return { item, op, value, message: message[0] };
      }, "Alerta");
      revised = {
        ...spec,
        name: name.value.trim(),
        description: description.value.trim(),
        instructions: instructions.value.trim(),
        administration: administration.value,
        items: newItems,
        scoring: { method: method.value, subscales: newSubscales, bands: newBands, alerts: newAlerts },
        licence: { source: licenceSource.value.trim(), public_domain: publicDomain.checked, attestation: true, note: licenceNote.value.trim() },
      };
      if (creating) {
        const made = await api("/v1/clinical/instruments", { method: "POST", body: { spec: revised, visibility: "establishment" } });
        location.hash = `#ajustes/test/${encodeURIComponent(made.instrument_id)}`;
      } else {
        await api(`/v1/clinical/instruments/${encodeURIComponent(id)}`, { method: "PUT", body: { spec: revised } });
        say(status, "Test guardado como una nueva versión; los resultados anteriores conservan su versión original.", "done");
      }
    } catch (err) { say(status, humanError(err), "error"); }
    finally { save.disabled = false; }
  });
  const subtitle = creating ? "Crea preguntas y configura solo las métricas que hayas definido" : `Versión ${test.version}; al guardar se crea una nueva versión`;
  main.replaceChildren(page(creating ? "Crear test propio" : test.name, subtitle, link("#ajustes", "Volver a Ajustes")),
    card(creating ? "Definición del test" : "Preguntas, puntuación y alertas", "", form));
}

// On call: who is told at once about a crisis, by WhatsApp, Telegram or e-mail (ADR 0017).
async function onCallCard() {
  const contacts = (await api("/v1/admin/on-call")).filter((c) => c.active);
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const rows = contacts.sort((a, b) => a.level - b.level).map((c) => {
    const drop = el("button", { class: "btn small", type: "button", text: "Quitar" });
    drop.addEventListener("click", async () => {
      if (!sure(drop, "Sí, quitar")) return;
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
  let searchTurn = 0;
  const show = async () => {
    const mine = ++searchTurn;
    const q = search.value.trim();
    let rows;
    try {
      rows = await api(`/v1/crm/patients?limit=500${q ? `&search=${encodeURIComponent(q)}` : ""}`);
    } catch (err) {
      if (mine !== searchTurn || q !== search.value.trim()) return;
      throw err;
    }
    if (mine !== searchTurn || q !== search.value.trim()) return;
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
  search.addEventListener("input", () => {
    ++searchTurn;
    clearTimeout(timer);
    timer = setTimeout(() => show().catch((err) => {
      if (!search.isConnected) return;
      box.replaceChildren(el("p", { class: "alert", role: "alert", text: humanError(err) }));
    }), 250);
  });
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
// Entry names come from the server (clinical_records.LABELS_ES): one source for every screen.
const TABS = [["resumen", "Resumen"], ["historia", "Historia"], ["tests", "Tests"], ["documentos", "Documentos"], ["citas", "Citas"], ["mensajes", "Mensajes"]];

VIEWS.paciente = async (main, id, tab = "resumen") => {
  const [patient, loyalty] = await Promise.all([
    api(`/v1/crm/patients/${encodeURIComponent(id)}`),
    api(`/v1/crm/patients/${encodeURIComponent(id)}/loyalty`).catch(() => null),
  ]);
  const contact = [patient.phone, patient.email].filter(Boolean).join(" · ") || "Sin datos de contacto";
  const tierChip = loyalty ? el("span", { class: "chip ok", title: "Nivel de fidelización: tiempo en acompañamiento y talleres, nunca sesiones", text: `Nivel ${loyalty.name}` }) : null;
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
    page(patient.display_name, contact, tierChip, patient.restricted ? null : access, visit),
    patient.restricted ? el("div", { class: "notice", role: "note" }, el("span", { text: "Esta paciente pidió borrar sus datos: la ficha está restringida y solo se conserva la historia clínica que exige la ley." })) : null,
    tabs, body].filter(Boolean));
  const render = { resumen: tabResumen, historia: tabHistoria, tests: tabTests, documentos: tabDocumentos, citas: tabCitas, mensajes: tabMensajes }[tab] || tabResumen;
  try {
    body.replaceChildren(...await render(patient));
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 403) throw err;
    body.replaceChildren(empty(ROLE_NOTES.clinical));
  }
};

/** The patient's personal link to their app: shown once, sent by the professional. */
async function openAccessDialog(patient) {
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const make = el("button", { class: "btn primary", type: "button", text: "Generar su enlace personal" });
  const area = el("div", { class: "stack" });
  const recover = el("button", { class: "btn", type: "button", text: "Crear enlace para recuperar contraseña" });
  const generate = async (recovery = false) => {
    if (recovery && !sure(recover, "Identidad verificada: generar enlace")) return;
    recover.disabled = true;
    make.disabled = true;
    try {
      const access = await api(`/v1/crm/patients/${encodeURIComponent(patient.id)}/${recovery ? "login-link" : "access"}`, { method: "POST" });
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
        el("p", { class: "muted", text: `Vence en ${recovery ? 3 : 90} días. El paciente puede crear su correo y contraseña desde Mi correo y contraseña.` }));
      make.hidden = true; recover.hidden = true;
    } catch (err) { say(status, humanError(err), "error"); make.disabled = false; recover.disabled = false; }
  };
  make.addEventListener("click", () => generate(false));
  recover.addEventListener("click", () => generate(true));
  dialog("Acceso a su app",
    el("p", { text: `Con este enlace, ${patient.display_name} ve sus citas, reserva en tus horarios libres y responde los tests que le envíes.` }),
    el("p", { class: "muted", text: "Envíaselo solo a ella: quien tenga el enlace entra a su app. No muestra su historia clínica." }),
    make, recover, area, status);
}

async function tabResumen(patient) {
  if (!state.session.clinician) return [empty("El resumen clínico es solo para el rol de psicóloga.")];
  const pid = encodeURIComponent(patient.id);
  const [briefing, engagement, metrics] = await Promise.all([
    api(`/v1/clinical/patients/${pid}/briefing`),
    api(`/v1/crm/patients/${pid}/engagement`).catch(() => null),
    api(`/v1/clinical/patients/${pid}/metrics`),
  ]);
  const [title, ...lines] = briefing.lines;
  return [card("Resumen antes de la sesión", "",
    el("p", {}, el("span", { class: "chip wait", text: "Borrador automático" })),
    el("p", { class: "muted", text: title }),
    el("ul", {}, ...lines.map((line) => el("li", { text: line }))),
    el("p", { class: "muted", text: "Se arma con fechas y números de la ficha. No lee tus notas ni usa inteligencia artificial." })),
    engagementCard(engagement),
    metricsSummaryCard(metrics, patient.id)].filter(Boolean);
}

function metricsSummaryCard(metrics, patientId) {
  const latest = new Map();
  for (const metric of metrics) {
    const key = `${metric.name}\u0000${metric.unit}`;
    if (!latest.has(key)) latest.set(key, metric);
  }
  const rows = [...latest.values()].map((metric) => el("li", {
    text: `${metric.name}: ${metric.value}${metric.unit ? ` ${metric.unit}` : ""} · ${longDate(metric.measured_on)}`,
  }));
  return card("Métricas de seguimiento", "",
    ...(rows.length ? [el("ul", {}, ...rows)] : [empty("Aún no hay métricas registradas.")]),
    link(`#paciente/${encodeURIComponent(patientId)}/tests`, "Ver historial y registrar métrica", "btn small"));
}

function engagementCard(e) {
  if (!e) return null;
  if (!e.available) {
    return card("Compromiso", "", el("p", { class: "muted", text: "No se calcula: la paciente no dio su consentimiento de analítica." }));
  }
  const d = e.dimensions;
  const show = (v) => (v == null ? "sin datos" : `${v}/100`);
  return card("Compromiso (últimos 6 meses)", "",
    el("p", {}, el("span", { class: `chip ${e.level === "alto" ? "ok" : e.level === "medio" ? "wait" : "bad"}`, text: e.level ? `Nivel ${e.level}` : "Sin datos suficientes" })),
    el("ul", {},
      el("li", { text: `Atención (abre los mensajes del consultorio): ${show(d.atencion)} — ${e.basis.messages_opened}` }),
      el("li", { text: `Participación (asiste, responde tests): ${show(d.participacion)} — citas ${e.basis.visits_attended}, tests ${e.basis.tests_answered}` }),
      el("li", { text: `Conexión social (talleres): ${show(d.conexion)} — ${e.basis.workshops_attended} asistidos` })),
    el("p", { class: "muted", text: "Indicador para ti, no para publicidad. Una dimensión sin datos no cuenta como cero." }));
}

async function tabHistoria(patient) {
  const pid = encodeURIComponent(patient.id);
  const [entries, types] = await Promise.all([
    api(`/v1/clinical/patients/${pid}/documents`),
    api("/v1/clinical/document-types"),
  ]);
  const kind = el("select", {}, ...types.map((t) => el("option", { value: t.doc_type, text: t.label })));
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
          el("strong", { text: d.label || d.doc_type }),
          d.access === "author_only" ? el("span", { class: "chip neutral tag", text: "Solo tú la ves" }) : null,
          d.amends ? el("span", { class: "chip wait", text: " Corrección" }) : null,
          el("p", { class: "muted", text: `${longDate(d.created_at.slice(0, 10))} · ${d.author_id}` })),
        fix),
      el("p", { class: "pre", text: d.body }));
  });
  return [card("Nueva entrada", "", form), ...(list.length ? list : [empty("La historia todavía no tiene entradas.")])];
}

async function tabTests(patient) {
  const pid = encodeURIComponent(patient.id);
  const [results, sent, available, metrics] = await Promise.all([
    api(`/v1/clinical/patients/${pid}/instrument-results`),
    api(`/v1/clinical/patients/${pid}/assignments`),
    api("/v1/clinical/instruments"),
    api(`/v1/clinical/patients/${pid}/metrics`),
  ]);
  const metricName = el("input", { type: "text", required: true, maxlength: "120" });
  const metricValue = el("input", { type: "number", required: true, step: "any" });
  const metricUnit = el("input", { type: "text", maxlength: "40", placeholder: "Ej.: puntos, horas, sobre 10" });
  const now = new Date();
  const localToday = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const measuredOn = el("input", { type: "date", required: true, value: localToday });
  const metricNote = el("textarea", { rows: "2", maxlength: "2000" });
  const metricStatus = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const metricSave = el("button", { class: "btn primary", type: "submit", text: "Guardar métrica" });
  const metricForm = el("form", { class: "stack", novalidate: true },
    el("div", { class: "fields" },
      el("label", { class: "field" }, "Métrica", metricName),
      el("label", { class: "field" }, "Valor numérico", metricValue)),
    el("div", { class: "fields" },
      el("label", { class: "field" }, "Unidad (opcional)", metricUnit),
      el("label", { class: "field" }, "Fecha de medición", measuredOn)),
    el("label", { class: "field" }, "Nota opcional", metricNote),
    metricSave, metricStatus,
    el("p", { class: "muted", text: "Registro clínico para el equipo tratante. Las métricas se guardan con autor y fecha; no se muestran a la paciente." }));
  metricForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!metricName.value.trim() || metricValue.value === "" || !measuredOn.value) {
      return say(metricStatus, "Completa la métrica, el valor y la fecha.", "error");
    }
    const value = Number(metricValue.value);
    if (!Number.isFinite(value)) return say(metricStatus, "El valor debe ser un número válido.", "error");
    metricSave.disabled = true;
    try {
      await api(`/v1/clinical/patients/${pid}/metrics`, {
        method: "POST",
        body: {
          name: metricName.value.trim(),
          value,
          unit: metricUnit.value.trim(),
          measured_on: measuredOn.value,
          note: metricNote.value.trim() || null,
        },
      });
      route();
    } catch (err) {
      say(metricStatus, humanError(err), "error");
      metricSave.disabled = false;
    }
  });
  const metricRows = metrics.map((metric) => el("div", { class: "doc" },
    el("span", { class: "what" },
      el("strong", { text: `${metric.name}: ${metric.value}${metric.unit ? ` ${metric.unit}` : ""}` }),
      el("br"),
      el("span", { class: "muted", text: `${longDate(metric.measured_on)} · ${metric.author_id}${metric.note ? ` · ${metric.note}` : ""}` }))));
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
      if (!sure(cancel, "Sí, cancelar el envío")) return;
      cancel.disabled = true;
      try { await api(`/v1/clinical/assignments/${encodeURIComponent(a.assignment_id)}`, { method: "DELETE" }); route(); } catch { cancel.disabled = false; }
    });
    return el("div", { class: "doc" }, el("span", { class: "what", text: `${a.name} · enviado el ${longDate(a.assigned_at.slice(0, 10))}` }), el("span", { class: "chip wait", text: "Esperando respuesta" }), cancel);
  });
  const done = [...results].sort((a, b) => b.created_at.localeCompare(a.created_at)).map((r) => {
    const definition = available.find((t) => t.instrument_id === r.instrument_id);
    const standardBands = definition?.spec.description?.startsWith("Plantilla de dominio público");
    return el("article", { class: "card" },
      el("div", { class: "head" },
        el("div", {}, el("strong", { text: r.instrument_name }), el("p", { class: "muted", text: `${longDate(r.created_at.slice(0, 10))}${r.note === "self-report" ? " · respondido en su app" : ""}` })),
        el("span", { class: `chip ${r.severity === "high" ? "bad" : r.severity === "moderate" ? "wait" : "ok"}`, text: r.band ? `${r.total} · ${bandName(r.band)}` : String(r.total ?? "—") })),
      standardBands ? el("p", { class: "muted", text: "Banda predefinida por la plantilla estándar; visible solo para el equipo clínico." }) : null,
      ...Object.entries(r.subscales || {}).filter(([, value]) => value != null).map(([label, value]) => el("p", { class: "muted", text: `${label}: ${value}` })),
      ...r.alerts.map((a) => el("p", { class: "status error", text: `⚠ ${a.message}` })));
  });
  return [
    card("Registrar métrica", "", metricForm),
    card("Historial de métricas", "", ...(metricRows.length ? metricRows : [empty("Aún no hay métricas registradas.")])),
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

// What each role opens, said plainly when a section is not for this account.
const ROLE_NOTES = {
  clinical: "Esta parte de la ficha es solo para el rol de psicóloga: por ley, ni la administración del consultorio la ve.",
  informe: "El informe del mes es para la dueña del consultorio. Pide a la administración el rol «dueña» si lo necesitas.",
  campanas: "Las campañas son para el rol de marketing o la dueña del consultorio.",
  publicaciones: "Las publicaciones son para el rol de marketing o la dueña del consultorio.",
  talleres: "Los talleres son para la dueña y para quien atiende pacientes: la lista de inscritos dice quién es paciente del consultorio.",
  marketing: "Esta sección es para el rol de marketing o la dueña del consultorio.",
};

// --- MARKETING: workshops first (RF-MKT-05); campaigns, calendar and report follow -----------
const MKT_TABS = [["informe", "Informe del mes"], ["talleres", "Talleres"], ["campanas", "Campañas"], ["publicaciones", "Publicaciones"]];

VIEWS.marketing = async (main, tab, arg = "") => {
  tab = tab || (state.session.roles.some((role) => ["admin", "owner"].includes(role))
    ? "informe" : state.session.roles.includes("marketing") ? "publicaciones" : "talleres");
  const tabs = el("div", { class: "tabs", role: "tablist", "aria-label": "Secciones de marketing" },
    ...MKT_TABS.map(([key, label]) => {
      const b = el("button", { type: "button", role: "tab", "aria-selected": String(key === tab), text: label });
      b.addEventListener("click", () => { location.hash = `#marketing/${key}`; });
      return b;
    }));
  let body;
  try {
    body = await ({ talleres: workshopsTab, informe: reportTab, campanas: campaignsTab, publicaciones: publicationsTab }[tab] || reportTab)(arg);
  } catch (err) {
    if (!(err instanceof ApiError) || err.status !== 403) throw err;
    body = [empty(ROLE_NOTES[tab] || ROLE_NOTES.marketing)];
  }
  main.replaceChildren(
    page("Marketing", "Nunca se usan datos clínicos para publicidad. Cada envío lo apruebas tú.", connectSocialButton()),
    tabs, ...body);
};

// The monthly report (RF-MKT-06): evaluate, analyse, set SMART goals, monitor, report.
async function reportTab(month) {
  const r = await api(`/v1/insights/report${month ? `?month=${month}` : ""}`);
  const [y, m] = r.month.split("-").map(Number);
  const title = `${MONTHS[m - 1][0].toUpperCase()}${MONTHS[m - 1].slice(1)} de ${y}`;
  const go = (delta) => {
    const d = new Date(Date.UTC(y, m - 1 + delta, 1));
    return `#marketing/informe/${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
  };
  const cells = Object.entries(r.labels).map(([key, label]) => {
    const now = r.metrics[key];
    const before = r.before[key];
    const shown = now == null ? "—" : key === "tasa_asistencia" ? `${now}\u00a0%` : String(now);
    let trend = "";
    if (now != null && before != null && now !== before) {
      const better = (now > before) !== (key === "inasistencias");
      trend = `${now > before ? "▲" : "▼"} mes anterior: ${before}${key === "tasa_asistencia" ? "\u00a0%" : ""}`;
      return el("div", { class: "stat" }, el("b", { text: shown }), el("span", { class: "sub", text: label }),
        el("span", { class: `chip ${better ? "ok" : "wait"}`, text: trend }));
    }
    return el("div", { class: "stat" }, el("b", { text: shown }), el("span", { class: "sub", text: label }),
      el("span", { class: "muted", text: before == null ? "" : "igual que el mes anterior" }));
  });
  const networks = Object.entries(r.metrics.mensajes_por_red || {})
    .map(([n, count]) => `${(NETWORKS[n] || [n])[0]}: ${count}`).join(" · ");
  const goals = r.goals.map((g) => {
    const drop = el("button", { class: "btn small", type: "button", text: "Quitar" });
    drop.addEventListener("click", async () => {
      if (!sure(drop, "Sí, quitar")) return;
      drop.disabled = true;
      try { await api(`/v1/insights/goals/${encodeURIComponent(g.goal_id)}`, { method: "DELETE" }); route(); } catch { drop.disabled = false; }
    });
    const bar = el("progress", { max: "100", value: String(g.progress ?? 0), "aria-label": `Progreso de ${g.label}` });
    return el("div", { class: "doc" },
      el("span", { class: "what" },
        el("strong", { text: `${g.label}: ${g.metric === "inasistencias" ? "como máximo" : "llegar a"} ${g.target}${g.metric === "tasa_asistencia" ? "\u00a0%" : ""}` }),
        el("br"), el("span", { class: "muted", text: g.note }), el("br"), bar,
        el("span", { class: "muted", text: ` Va en ${g.value ?? "—"}` })),
      el("span", { class: `chip ${g.met ? "ok" : "wait"}`, text: g.met ? "Cumplida" : `${g.progress ?? 0}\u00a0%` }),
      drop);
  });
  const engagement = await api("/v1/insights/engagement").catch(() => null);
  const print = el("button", { class: "btn", type: "button", text: "Imprimir o guardar en PDF" });
  print.addEventListener("click", () => window.print());
  return [
    el("div", { class: "head" },
      el("h2", { class: "title", text: title }),
      el("div", { class: "row tight" }, link(go(-1), "‹ Mes anterior"), link(go(1), "Mes siguiente ›"), print)),
    el("section", { class: "stats", "aria-label": "Cifras del mes" }, ...cells),
    networks ? el("p", { class: "muted", text: `Mensajes por red: ${networks}.` }) : null,
    card("Lo que destaca", "", ...(r.highlights.length ? r.highlights.map((h) => el("p", { text: `• ${h}` })) : [empty("Nada fuera de lo común este mes.")])),
    card("Objetivos SMART del mes", "", ...(goals.length ? goals : [empty("Aún no hay objetivos para este mes.")]), goalForm(r)),
    engagement ? card("Compromiso de las pacientes (con consentimiento de analítica)", "",
      el("p", { text: `${engagement.with_analytics_consent} de ${engagement.patients} pacientes dieron su consentimiento.` }),
      el("p", {}, el("span", { class: "chip ok", text: `Alto: ${engagement.levels.alto}` }), " ",
        el("span", { class: "chip wait", text: `Medio: ${engagement.levels.medio}` }), " ",
        el("span", { class: "chip bad", text: `Bajo: ${engagement.levels.bajo}` }), " ",
        el("span", { class: "chip neutral", text: `Sin datos: ${engagement.levels.sin_datos}` })),
      el("p", { class: "muted", text: `Promedios: atención ${engagement.averages.atencion ?? "—"} · participación ${engagement.averages.participacion ?? "—"} · conexión social ${engagement.averages.conexion ?? "—"}.` }),
      el("p", { class: "muted", text: engagement.note })) : null,
    el("p", { class: "muted", text: "Solo totales del consultorio: ningún dato de una paciente sale en este informe." }),
  ].filter(Boolean);
}

function goalForm(r) {
  const metric = el("select", { "aria-label": "Qué medir" },
    ...Object.entries(r.labels).map(([key, label]) => el("option", { value: key, text: label })));
  const target = el("input", { type: "number", min: "1", step: "1", required: true, "aria-label": "Meta" });
  const note = el("input", { type: "text", maxlength: "300", required: true, placeholder: "Por qué importa (ej.: más pacientes conocen los talleres)" });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Agregar objetivo" });
  const form = el("form", { class: "stack", novalidate: true },
    el("div", { class: "fields" }, el("label", { class: "field" }, "Qué medir", metric), el("label", { class: "field" }, "Meta del mes", target)),
    el("label", { class: "field" }, "Por qué es importante y alcanzable", note), save, status,
    el("p", { class: "muted", text: "SMART: una cifra concreta, medible, alcanzable, relevante para el consultorio y con plazo (este mes). El progreso lo calcula el sistema." }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!Number(target.value) || !note.value.trim()) return say(status, "Escribe la meta y por qué es importante.", "error");
    save.disabled = true;
    try {
      await api("/v1/insights/goals", { method: "POST", body: { month: r.month, metric: metric.value, target: Number(target.value), note: note.value.trim() } });
      route();
    } catch (err) {
      say(status, /this month or a later/.test(err.detail || "") ? "Los objetivos se fijan para este mes o los siguientes."
        : /percentage/.test(err.detail || "") ? "La asistencia es un porcentaje: de 1 a 100." : humanError(err), "error");
      save.disabled = false;
    }
  });
  return form;
}

// Campaigns (RF-MKT-03): a control group measures the real effect; each one approved by a person.
const KIND_NAMES = {
  education: "Psicoeducación (material, talleres)", reactivation: "Invitar a volver",
  recall: "Recordar el control", referral: "Recomendación", birthday: "Cumpleaños", pending_treatment: "Plan pendiente",
};
const SEGMENT_NAMES = {
  dormant: "Sin visitas hace mucho tiempo", at_risk: "Se están alejando", loyal: "Constantes",
  new: "Nuevas", champion: "Muy comprometidas", occasional: "Ocasionales", no_visits: "Sin ninguna visita",
  recall_due: "Con control pendiente",
};
const CAMPAIGN_STATES = {
  draft: ["Borrador", "neutral"], pending_approval: ["Por aprobar", "wait"], approved: ["Aprobada", "ok"],
  queued: ["Enviándose", "wait"], sending: ["Enviándose", "wait"], completed: ["Enviada", "ok"],
  partial_failed: ["Enviada con fallas", "bad"], cancelled: ["Cancelada", "neutral"],
};

async function campaignsTab() {
  const list = await api("/v1/campaigns");
  const rows = [...list].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).map((c) => {
    const [label, cls] = CAMPAIGN_STATES[c.status] || [c.status, "neutral"];
    const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
    const act = (text, path, primary = false) => {
      const b = el("button", { class: `btn small${primary ? " primary" : ""}`, type: "button", text });
      b.addEventListener("click", async () => {
        if (path === "cancel" && !sure(b, "Sí, cancelar")) return;
        b.disabled = true;
        try { await api(`/v1/campaigns/${encodeURIComponent(c.id)}/${path}`, { method: "POST", body: {} }); route(); }
        catch (err) { say(status, humanError(err), "error"); b.disabled = false; }
      });
      return b;
    };
    const results = el("button", { class: "btn small", type: "button", text: "Resultados" });
    results.addEventListener("click", () => openResults(c));
    const actions = [
      ["draft", "pending_approval"].includes(c.status) ? act("Aprobar", "approve", true) : null,
      c.status === "approved" ? act("Enviar", "send", true) : null,
      ["draft", "pending_approval", "approved"].includes(c.status) ? act("Cancelar", "cancel") : null,
      ["queued", "sending", "completed", "partial_failed"].includes(c.status) ? results : null,
    ].filter(Boolean);
    return el("article", { class: "card" },
      el("div", { class: "head" },
        el("div", {}, el("strong", { text: c.name }),
          el("p", { class: "muted", text: `${KIND_NAMES[c.kind] || c.kind} · ${SEGMENT_NAMES[c.segment] || c.segment} · grupo de control ${c.holdout_pct}\u00a0%${c.mode === "simulation" ? " · PRUEBA (no envía nada)" : ""}` })),
        el("span", { class: `chip ${cls}`, text: label })),
      el("p", { class: "pre muted", text: c.template }),
      actions.length ? el("div", { class: "row tight" }, ...actions) : null, status);
  });
  return [
    card("Nueva campaña", "", campaignForm()),
    ...(rows.length ? rows : [empty("Aún no hay campañas.")]),
  ];
}

function campaignForm() {
  const name = el("input", { type: "text", maxlength: "200", required: true, placeholder: "Ej.: Invitación al taller de sueño" });
  const kind = el("select", {}, ...Object.entries(KIND_NAMES).map(([k, v]) => el("option", { value: k, text: v })));
  const segment = el("select", {}, ...Object.entries(SEGMENT_NAMES).map(([k, v]) => el("option", { value: k, text: v })));
  const text = el("textarea", { rows: "4", maxlength: "1000", required: true });
  text.value = "Hola {first_name}, en el consultorio preparamos un taller para dormir mejor. Si te interesa, inscríbete desde la app. Responde STOP para no recibir más mensajes.";
  const holdout = el("input", { type: "number", min: "0", max: "50", value: "20" });
  const mode = el("select", {}, el("option", { value: "simulation", text: "Prueba: revisa todo pero no envía nada" }), el("option", { value: "live", text: "Real: se envía al aprobarla y enviarla" }));
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Crear campaña" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Nombre", name),
    el("div", { class: "fields" }, el("label", { class: "field" }, "Objetivo", kind), el("label", { class: "field" }, "A quiénes", segment)),
    el("label", { class: "field" }, "Mensaje ({first_name} se reemplaza por el nombre)", text),
    el("div", { class: "fields" }, el("label", { class: "field" }, "Grupo de control (%)", holdout), el("label", { class: "field" }, "Modo", mode)),
    save, status,
    el("p", { class: "muted", text: "Solo llega a quienes aceptaron recibir novedades. Nunca menciona tratamiento ni salud. El grupo de control no recibe nada: así se mide el efecto real. Primero aparece en su app; por Telegram solo si no la abre." }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!name.value.trim() || !text.value.trim()) return say(status, "Escribe el nombre y el mensaje.", "error");
    save.disabled = true;
    try {
      await api("/v1/campaigns", { method: "POST", body: { name: name.value.trim(), kind: kind.value, segment: segment.value,
        template: text.value.trim(), holdout_pct: Number(holdout.value) || 0, mode: mode.value } });
      route();
    } catch (err) {
      say(status, /STOP|BAJA|opt.?out/i.test(err.detail || "") ? "El mensaje debe decir cómo dejar de recibirlos (por ejemplo: «Responde STOP para no recibir más mensajes»)."
        : err.status === 422 ? "El mensaje no pasó las reglas de tu profesión (por ejemplo, promesas de cura o datos de salud). Revísalo." : humanError(err), "error");
      save.disabled = false;
    }
  });
  return form;
}

async function openResults(c) {
  const r = await api(`/v1/campaigns/${encodeURIComponent(c.id)}/results`);
  const lines = [];
  if (r.status === "simulation") lines.push("Fue una prueba: no se envió nada, así que no hay efecto que medir.");
  else if (r.status === "provisional") lines.push(`Resultado provisional: la ventana de medición va en el día ${r.progress?.day ?? "?"} de ${r.progress?.of ?? "?"}.`);
  else lines.push("Resultado final de la ventana de medición.");
  const arms = r.itt?.arms;
  if (arms) {
    lines.push(`Recibieron el mensaje: ${arms.treatment.converted} de ${arms.treatment.n} agendaron (${Math.round(100 * arms.treatment.rate)}\u00a0%).`);
    lines.push(`Grupo de control: ${arms.control.converted} de ${arms.control.n} agendaron (${Math.round(100 * arms.control.rate)}\u00a0%).`);
  }
  const itt = r.itt || {};
  if (r.status === "final" && /too small/.test(r.conclusion || "")) {
    lines.push("Los grupos son muy pequeños para sacar una conclusión: repite la campaña con más pacientes antes de decidir.");
  } else if (itt.lift_abs != null) {
    const significant = itt.p_value != null && itt.p_value < 0.05;
    lines.push(`Efecto real: ${itt.lift_abs >= 0 ? "+" : ""}${Math.round(100 * itt.lift_abs)} puntos frente al grupo de control${itt.p_value != null ? ` (p = ${itt.p_value}: ${significant ? "es poco probable que sea casualidad" : "podría ser casualidad"})` : ""}.`);
  }
  dialog(`Resultados: ${c.name}`, ...lines.map((t) => el("p", { text: t })),
    el("p", { class: "muted", text: "Se compara con el grupo de control, elegido al azar: así se sabe cuánto se debe realmente a la campaña." }));
}

// Publications (RF-PUB-04): created by code from a brief, approved by a person, on the calendar.
async function publicationsTab() {
  const [accounts, calendar, all] = await Promise.all([
    api("/v1/social/accounts").catch(() => []),
    api("/v1/social/publications/calendar").catch(() => []),
    api("/v1/social/publications").catch(() => []),
  ]);
  const publishing = accounts.filter((a) => ["instagram", "facebook"].includes(a.network) && a.active !== false);
  const STATES = { pending_approval: ["Por aprobar", "wait"], approved: ["Aprobada", "ok"], published: ["Publicada", "ok"], failed: ["Falló", "bad"], uncertain: ["Incierta", "bad"], cancelled: ["Cancelada", "neutral"], publishing: ["Publicándose", "wait"] };
  const row = (p) => {
    const [label, cls] = STATES[p.status] || [p.status, "neutral"];
    const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
    const post = (text, path, primary = false) => {
      const b = el("button", { class: `btn small${primary ? " primary" : ""}`, type: "button", text });
      b.addEventListener("click", async () => {
        if (path === "cancel" && !sure(b, "Sí, cancelar")) return;
        b.disabled = true;
        try { await api(`/v1/social/publications/${encodeURIComponent(p.publication_id)}/${path}`, { method: "POST" }); route(); }
        catch (err) { say(status, humanError(err), "error"); b.disabled = false; }
      });
      return b;
    };
    const day = el("input", { type: "date", value: addDays(todayISO(), 1), "aria-label": "Fecha de publicación" });
    const at = el("input", { type: "time", value: "10:00", step: "300", "aria-label": "Hora de publicación" });
    const plan = el("button", { class: "btn small", type: "button", text: "Programar" });
    plan.addEventListener("click", async () => {
      plan.disabled = true;
      try {
        await api(`/v1/social/publications/${encodeURIComponent(p.publication_id)}/schedule`, { method: "PUT", body: { at: zoned(day.value, at.value) } });
        route();
      } catch (err) { say(status, /future/.test(err.detail || "") ? "Elige una fecha y hora futuras." : humanError(err), "error"); plan.disabled = false; }
    });
    const when = p.date || p.scheduled_for;
    const whenText = when ? (() => { const x = partsIn(new Date(when)); return `${longDate(`${x.year}-${x.month}-${x.day}`)} · ${x.hour}:${x.minute}`; })() : "Sin fecha";
    const open = ["pending_approval", "approved"].includes(p.status);
    return el("article", { class: "card" },
      el("div", { class: "head" },
        el("div", {}, el("strong", { text: `${(NETWORKS[p.network] || [p.network])[0]} · ${p.media_type === "video" ? "video" : "imagen"}` }),
          el("p", { class: "muted", text: whenText })),
        el("span", { class: `chip ${cls}`, text: p.overdue ? "Atrasada: falta tu aprobación" : label })),
      el("p", { class: "pre", text: p.caption }),
      open ? el("div", { class: "row tight" }, day, at, plan,
        p.status === "pending_approval" ? post("Aprobar", "approve", true) : null,
        p.status === "approved" ? post("Publicar ahora", "publish") : null,
        post("Cancelar", "cancel")) : null,
      status);
  };
  const upcoming = all.filter((p) => ["pending_approval", "approved"].includes(p.status));
  const done = calendar.filter((p) => !["pending_approval", "approved"].includes(p.status));
  return [
    publishing.length ? card("Nueva publicación", "", publicationForm(publishing))
      : el("div", { class: "notice", role: "note" }, el("span", { text: "Para publicar, primero hay que conectar tu cuenta de Instagram o Facebook (requiere la verificación de Meta)." })),
    card("Por publicar", "", ...(upcoming.length ? upcoming.map(row) : [empty("No hay publicaciones pendientes.")]),
      el("p", { class: "muted", text: "Una publicación programada sale sola a su hora solo si ya la aprobaste." })),
    done.length ? card("Este mes", "", ...done.map(row)) : null,
  ].filter(Boolean);
}

function publicationForm(accounts) {
  const account = el("select", { "aria-label": "Cuenta" }, ...accounts.map((a) => el("option", { value: a.account_id, text: `${(NETWORKS[a.network] || [a.network])[0]} · ${a.handle}` })));
  const title = el("input", { type: "text", maxlength: "80", required: true, placeholder: "Ej.: Dormir bien también es salud mental" });
  const points = el("textarea", { rows: "3", required: true, placeholder: "Una idea por línea (2 o 3)" });
  const cta = el("input", { type: "text", maxlength: "60", value: "Agenda tu cita en la app" });
  const caption = el("textarea", { rows: "3", maxlength: "2200", required: true });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Crear la imagen" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Cuenta", account),
    el("label", { class: "field" }, "Título de la imagen", title),
    el("label", { class: "field" }, "Ideas", points),
    el("label", { class: "field" }, "Llamada a la acción", cta),
    el("label", { class: "field" }, "Texto de la publicación", caption),
    save, status,
    el("p", { class: "muted", text: "La imagen se arma con tu identidad visual y pasa las reglas de tu profesión. No sale hasta que la apruebes." }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const ideas = points.value.split("\n").map((x) => x.trim()).filter(Boolean);
    if (!title.value.trim() || !ideas.length || !caption.value.trim()) return say(status, "Completa el título, al menos una idea y el texto.", "error");
    save.disabled = true;
    say(status, "Creando la imagen…");
    try {
      await api("/v1/social/publications", { method: "POST", body: { account_id: account.value, kind: "infographic", caption: caption.value.trim(),
        brief: { title: title.value.trim(), points: ideas.slice(0, 4), cta: cta.value.trim() || "Agenda tu cita", practice_name: state.session.practice } } });
      route();
    } catch (err) {
      say(status, err.status === 422 ? "El texto no pasó las reglas de tu profesión (por ejemplo, promesas de cura o descuentos). Revísalo." : humanError(err), "error");
      save.disabled = false;
    }
  });
  return form;
}

async function workshopsTab() {
  const list = await api("/v1/workshops?include_past=true");
  const now = Date.now();
  const upcoming = list.filter((w) => new Date(w.starts_at).getTime() >= now - 86400000);
  const past = list.filter((w) => new Date(w.starts_at).getTime() < now - 86400000);
  const row = (w) => {
    const at = partsIn(new Date(w.starts_at));
    const open = el("button", { class: "btn small", type: "button", text: "Inscritos" });
    open.addEventListener("click", () => openWorkshop(w));
    const taken = w.capacity - w.seats_left;
    return el("div", { class: "doc" },
      el("span", { class: "what" }, el("strong", { text: w.title }), el("br"),
        el("span", { class: "muted", text: `${longDate(`${at.year}-${at.month}-${at.day}`)} · ${at.hour}:${at.minute} · ${w.place}` })),
      w.status === "cancelled"
        ? el("span", { class: "chip neutral", text: "Cancelado" })
        : el("span", { class: `chip ${w.seats_left ? "ok" : "wait"}`, text: `${taken} de ${w.capacity} cupos` }),
      open);
  };
  return [
    state.session.roles.some((role) => ["admin", "owner", "marketing"].includes(role)) ? card("Nuevo taller o charla", "", workshopForm()) : null,
    card("Próximos", "", ...(upcoming.length ? upcoming.map(row) : [empty("No hay talleres programados.")])),
    past.length ? card("Anteriores", "", ...past.slice(-10).reverse().map(row)) : null,
  ].filter(Boolean);
}

function workshopForm() {
  const title = el("input", { type: "text", maxlength: "120", required: true, placeholder: "Ej.: Taller para dormir mejor" });
  const description = el("textarea", { rows: "3", maxlength: "2000", required: true, placeholder: "Qué aprenderán y para quién es." });
  const day = el("input", { type: "date", value: addDays(todayISO(), 7), required: true });
  const at = el("input", { type: "time", value: "18:00", step: "300", required: true });
  const length = el("input", { type: "number", min: "15", max: "480", step: "15", value: "90" });
  const place = el("input", { type: "text", maxlength: "200", value: "En el consultorio" });
  const seats = el("input", { type: "number", min: "1", max: "500", value: "12" });
  const opensDay = el("input", { type: "date" });
  const opensAt = el("input", { type: "time", value: "09:00", step: "300" });
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const save = el("button", { class: "btn primary", type: "submit", text: "Publicar en la app de las pacientes" });
  const form = el("form", { class: "stack", novalidate: true },
    el("label", { class: "field" }, "Título", title),
    el("label", { class: "field" }, "Descripción", description),
    el("div", { class: "fields" }, el("label", { class: "field" }, "Fecha", day), el("label", { class: "field" }, "Hora", at),
      el("label", { class: "field" }, "Duración (min)", length)),
    el("div", { class: "fields" }, el("label", { class: "field" }, "Lugar", place), el("label", { class: "field" }, "Cupos", seats)),
    el("div", { class: "fields" }, el("label", { class: "field" }, "Inscripción abierta para todas desde (opcional)", opensDay),
      el("label", { class: "field" }, "Hora", opensAt)),
    el("p", { class: "muted", text: "Si eliges una fecha, las pacientes en nivel Constancia se inscriben 48 horas antes y las de nivel Comunidad, 4 días antes. Sin fecha, la inscripción abre de inmediato." }),
    save, status,
    el("p", { class: "muted", text: "Las pacientes lo ven en su app y se inscriben, solas o con acompañante. Invitarlas por mensaje es una campaña: necesita su consentimiento y tu aprobación." }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!title.value.trim() || !description.value.trim()) return say(status, "Escribe el título y la descripción.", "error");
    save.disabled = true;
    try {
      await api("/v1/workshops", { method: "POST", body: {
        title: title.value.trim(), description: description.value.trim(), starts_at: zoned(day.value, at.value),
        duration_min: Number(length.value) || 90, place: place.value.trim() || "En el consultorio", capacity: Number(seats.value) || 10,
        opens_at: opensDay.value ? zoned(opensDay.value, opensAt.value || "09:00") : null,
      } });
      route();
    } catch (err) {
      say(status, /future/.test(err.detail || "") ? "La fecha y hora deben ser futuras."
        : /open before/.test(err.detail || "") ? "La inscripción debe abrir antes de que empiece el taller." : humanError(err), "error");
      save.disabled = false;
    }
  });
  return form;
}

async function openWorkshop(w) {
  const people = await api(`/v1/workshops/${encodeURIComponent(w.workshop_id)}/registrations`);
  const started = new Date(w.starts_at).getTime() <= Date.now();
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const STATES = { registered: "Inscrita", attended: "Asistió", absent: "No asistió" };
  const rows = people.map((r) => {
    const mark = (attended) => {
      const b = el("button", { class: "btn small", type: "button", text: attended ? "Asistió" : "No asistió" });
      b.addEventListener("click", async () => {
        try {
          await api(`/v1/workshops/${encodeURIComponent(w.workshop_id)}/attendance`, { method: "POST", body: { patient_id: r.patient_id, attended } });
          $("#dialog").close();
          openWorkshop(w);
        } catch (err) { say(status, humanError(err), "error"); }
      });
      return b;
    };
    return el("div", { class: "doc" },
      el("span", { class: "what" }, el("strong", { text: r.name }), el("br"),
        el("span", { class: "muted", text: `${STATES[r.status] || r.status}${r.companions ? ` · con ${r.companions} acompañante${r.companions === 1 ? "" : "s"}` : ""}` })),
      started && w.status !== "cancelled" ? el("span", { class: "row tight" }, mark(true), mark(false)) : null);
  });
  const cancel = el("button", { class: "btn", type: "button", text: "Cancelar el taller" });
  cancel.addEventListener("click", async () => {
    if (!sure(cancel, "Sí, cancelar el taller")) return;
    cancel.disabled = true;
    try { await api(`/v1/workshops/${encodeURIComponent(w.workshop_id)}/cancel`, { method: "POST" }); $("#dialog").close(); route(); }
    catch (err) { say(status, humanError(err), "error"); cancel.disabled = false; }
  });
  dialog(w.title,
    el("p", { class: "muted", text: `${w.capacity - w.seats_left} de ${w.capacity} cupos ocupados (contando acompañantes).` }),
    ...(rows.length ? rows : [empty("Todavía nadie se inscribió.")]),
    started ? el("p", { class: "muted", text: "Marca quién asistió: sirve para medir el compromiso (solo de quienes lo consintieron)." }) : null,
    !started && w.status !== "cancelled" ? cancel : null,
    status);
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
        if (!sure(drop, "Sí, descartar")) return;
        drop.disabled = true;
        try { await api(`/v1/inbox/${encodeURIComponent(id)}/drafts/${encodeURIComponent(m.message_id)}`, { method: "DELETE" }); route(); } catch (err) { say(status, humanError(err), "error"); drop.disabled = false; }
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
  $("#email-form").addEventListener("submit", emailStep);
  $("#code-form").addEventListener("submit", codeStep);
  $("#password-form").addEventListener("submit", passwordStep);
  $("#enroll-form").addEventListener("submit", enrollStep);
  $("#forgot").addEventListener("click", forgotPassword);
  $("#password-back").addEventListener("click", cancelSetup);
  $("#enroll-back").addEventListener("click", cancelSetup);
  $("#code-back").addEventListener("click", cancelSetup);
  $("#use-key").addEventListener("click", () => { cancelSetup(); showStep("key"); });
  $("#use-email").addEventListener("click", cancelSetup);
  $("#signout").addEventListener("click", () => signOut());
  window.addEventListener("hashchange", () => {
    if (takeLinkToken()) return;
    // Setup clears the session. Back changes the hash, but route() cannot draw
    // a signed-in screen; explicitly leave setup instead of leaving it stuck.
    if (!state.session) {
      here = location.hash;
      cancelSetup();
      return;
    }
    if (unsavedText() && !confirm("Tienes texto sin guardar en esta pantalla. ¿Salir y perderlo?")) {
      history.replaceState(null, "", here); // stay; replaceState does not fire hashchange
      return;
    }
    here = location.hash;
    route();
  });
  window.addEventListener("beforeunload", (event) => { if (unsavedText()) event.preventDefault(); });
  document.addEventListener("input", (event) => { if (event.target.matches("textarea")) event.target.dataset.dirty = "1"; });
  $("#skip").addEventListener("click", (event) => { event.preventDefault(); $("#view").focus(); });
  if (takeLinkToken()) return;
  if (keyStore.get()) {
    start().catch(() => signOut("Tu sesión terminó. Vuelve a entrar."));
  } else {
    signOut();
  }
});
