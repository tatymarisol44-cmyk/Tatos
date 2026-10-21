// Mi consultorio: the patient's app (PWA), in Spanish. A thin client over /v1/me: no
// dependencies, no build, no inline script (strict CSP). Server text goes on the page with
// textContent only. The access key arrives in the link's fragment (never sent to a
// server log), is kept on this phone and removed from the address bar at once.
"use strict";

const KEY = "mi.key";
const store = {
  get() { try { return localStorage.getItem(KEY); } catch { return memory; } },
  set(value) { memory = value; try { localStorage.setItem(KEY, value); } catch { /* memory only */ } },
  clear() { memory = null; try { localStorage.removeItem(KEY); } catch { /* nothing kept */ } },
};
let memory = null;
const state = { tz: "America/Guayaquil", me: null, practice: null, booking: null };
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
function button(text, onclick, cls = "btn") {
  const b = el("button", { class: cls, type: "button", text });
  b.addEventListener("click", onclick);
  return b;
}
function say(node, text, kind = "") { node.textContent = text; node.className = `status ${kind}`.trim(); }

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

class ApiError extends Error {
  constructor(status, detail) { super(detail || String(status)); this.status = status; this.detail = detail; }
}

class StaleSessionError extends Error {}

async function api(path, { method = "GET", body } = {}) {
  const requestKey = store.get();
  const headers = { "X-API-Key": requestKey || "" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let resp;
  try {
    resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch { throw new ApiError(0, ""); }
  if (requestKey !== store.get()) throw new StaleSessionError();
  if ([401, 403].includes(resp.status) && path.startsWith("/v1/me")) {
    store.clear();
    noAccess("Tu enlace venció o fue reemplazado. Pide uno nuevo al consultorio.");
    throw new ApiError(resp.status, "");
  }
  if (!resp.ok) {
    let detail = "";
    try { const data = await resp.json(); detail = typeof data.detail === "string" ? data.detail : ""; } catch { /* none */ }
    throw new ApiError(resp.status, detail);
  }
  const result = resp.status === 204 ? null : await resp.json();
  if (requestKey !== store.get()) throw new StaleSessionError();
  return result;
}
function humanError(err) {
  if (!(err instanceof ApiError) || err.status >= 500) return "Algo salió mal. Inténtalo de nuevo en un momento.";
  if (err.status === 0) return "No hay conexión. Revisa tu internet e inténtalo de nuevo.";
  if (err.status === 409 || /free slot|already has a visit/.test(err.detail)) return "Ese horario se acaba de ocupar. Elige otro.";
  return "No se pudo completar. Inténtalo de nuevo.";
}

// --- dates in the practice's time zone ---------------------------------------------------------
const DAY_NAMES = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"];
const MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
  "septiembre", "octubre", "noviembre", "diciembre"];
function parts(iso) {
  const out = {};
  for (const p of new Intl.DateTimeFormat("en-CA", {
    timeZone: state.tz, year: "numeric", month: "2-digit", day: "2-digit", weekday: "short",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(iso))) out[p.type] = p.value;
  const date = new Date(`${out.year}-${out.month}-${out.day}T12:00:00Z`);
  return { ...out, dow: date.getUTCDay(), date: `${out.year}-${out.month}-${out.day}` };
}
function when(iso) {
  const p = parts(iso);
  return `${DAY_NAMES[p.dow]} ${Number(p.day)} de ${MONTHS[Number(p.month) - 1]}, ${p.hour}:${p.minute}`;
}
function firstName(name) { return (name || "").trim().split(/\s+/)[0] || ""; }

// --- access ----------------------------------------------------------------------------------------
function takeKeyFromLink() {
  const match = location.hash.match(/^#k=([\w-]+)$/);
  if (match) {
    store.set(match[1]);
    history.replaceState(null, "", "/mi/#inicio"); // the key never stays in the address bar
  }
}

function noAccess(message) {
  state.me = null;
  ++turn; // Invalidate in-flight screens before rendering the access message.
  $("#tabs").hidden = true;
  $("#view").replaceChildren(el("section", { class: "card" },
    el("h1", { class: "title", text: "Mi consultorio" }),
    el("p", { text: message || "Para entrar, abre el enlace personal que te envió tu consultorio por WhatsApp." }),
    el("p", { class: "muted", text: "Ese enlace es solo tuyo: no lo compartas." }),
    el("h2", { text: "Entrar con mi cuenta" }),
    credentialsForm({ onSuccess: () => { history.replaceState(null, "", "/mi/#inicio"); boot(); } }),
    el("p", { class: "muted", text: "Para crear tu cuenta o recuperar tu contraseña, pide un enlace personal al consultorio." })));
  $("#who").textContent = "";
}

async function boot() {
  ++turn;
  takeKeyFromLink();
  if (!store.get()) return noAccess();
  try {
    const [practice, me] = await Promise.all([api("/v1/me/practice"), api("/v1/me")]);
    state.practice = practice;
    state.tz = practice.timezone || state.tz;
    state.me = me;
    $("#practice").textContent = practice.practice;
    $("#who").textContent = me.profile.display_name;
    $("#tabs").hidden = false;
    const prompt = await api("/v1/me/consents");
    if (prompt.ask.length) return askConsents(prompt);
    route();
    refreshTestsBadge();
  } catch (err) {
    if (err instanceof StaleSessionError) return;
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) return;
    $("#view").replaceChildren(el("p", { class: "status error", text: humanError(err) }));
  }
}

async function refreshTestsBadge() {
  try {
    const tests = await api("/v1/me/tests");
    const badge = $("#badge-tests");
    badge.hidden = !tests.length;
    badge.textContent = String(tests.length);
  } catch { /* a convenience */ }
}

// --- consents: asked once, equal options, nothing pre-selected --------------------------------------
function askConsents(prompt) {
  const [first, ...rest] = prompt.ask;
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const answer = (granted) => async () => {
    try {
      await api(`/v1/me/consents/${encodeURIComponent(first.purpose)}`, { method: "PUT", body: { granted } });
      if (rest.length) askConsents({ ...prompt, ask: rest });
      else { location.hash = "#inicio"; route(); refreshTestsBadge(); }
    } catch (err) { say(status, humanError(err), "error"); }
  };
  $("#tabs").hidden = true;
  $("#view").replaceChildren(el("section", { class: "card" },
    el("p", { class: "muted", text: "Antes de empezar, una pregunta" }),
    el("h1", { class: "title", text: first.title }),
    el("p", { text: first.benefit }),
    el("p", { class: "muted", text: first.detail }),
    el("div", { class: "choice" }, button(first.yes_label, answer(true), "answer"), button(first.no_label, answer(false), "answer")),
    el("p", { class: "muted", text: prompt.footer }), status));
}

// --- routing -----------------------------------------------------------------------------------------
const VIEWS = {};
let turn = 0;
function route() {
  if (!state.me) return;
  $("#tabs").hidden = false;
  const mine = ++turn;
  const [name, arg] = (location.hash.slice(1) || "inicio").split("/");
  const view = VIEWS[name] || VIEWS.inicio;
  for (const a of document.querySelectorAll("[data-nav]")) {
    if (a.dataset.nav === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  const main = $("#view");
  const frame = el("div", { class: "stack" });
  main.replaceChildren(el("p", { class: "muted", text: "Cargando…" }));
  view(frame, arg && decodeURIComponent(arg))
    .then(() => { if (mine === turn) main.replaceChildren(...frame.childNodes); })
    .catch((err) => { if (mine === turn) main.replaceChildren(el("p", { class: "status error", text: humanError(err) })); });
}

// --- INICIO ------------------------------------------------------------------------------------------
VIEWS.inicio = async (main) => {
  const [me, tests, talks] = await Promise.all([api("/v1/me"), api("/v1/me/tests"), api("/v1/me/workshops")]);
  state.me = me;
  const next = me.upcoming_appointments.sort((a, b) => a.starts_at.localeCompare(b.starts_at))[0];
  main.append(el("h1", { class: "title", text: `Hola, ${firstName(me.profile.display_name)}` }));
  main.append(el("a", { href: "#cuenta", class: "btn", text: "Mi correo y contraseña" }));
  main.append(next
    ? el("section", { class: "next-appt", "aria-label": "Tu próxima cita" },
      el("span", { text: "Tu próxima cita" }), el("span", { class: "big", text: when(next.starts_at) }),
      el("span", { text: next.kind }),
      el("span", { class: "chip ok", text: next.status === "confirmed" ? "Confirmada" : "Agendada" }))
    : el("section", { class: "card" }, el("h2", { text: "No tienes citas agendadas" }),
      el("a", { class: "btn primary", href: "#citas", text: "Reservar una cita" })));
  if (tests.length) {
    main.append(el("section", { class: "card" },
      el("h2", { text: tests.length === 1 ? "Tienes un test por responder" : `Tienes ${tests.length} tests por responder` }),
      el("p", { class: "muted", text: "Tu psicóloga te lo envió para conocer cómo te has sentido. Toma unos minutos." }),
      el("a", { class: "btn primary", href: "#tests", text: "Responder" })));
  }
  if (talks.length) {
    const next = talks[0];
    main.append(el("section", { class: "card" },
      el("p", { class: "muted", text: "Próximo taller" }),
      el("h2", { text: next.title }),
      el("p", { class: "muted", text: `${when(next.starts_at)} · ${next.place}` }),
      el("a", { class: "btn", href: "#talleres", text: next.registered ? "Ver mi inscripción" : "Ver e inscribirme" })));
  }
};

// --- CITAS -------------------------------------------------------------------------------------------
VIEWS.citas = async (main) => {
  const me = await api("/v1/me");
  const upcoming = me.upcoming_appointments.sort((a, b) => a.starts_at.localeCompare(b.starts_at));
  main.append(el("h1", { class: "title", text: "Tus citas" }));
  main.append(el("section", { class: "card" }, el("h2", { text: "Próximas" }),
    ...(upcoming.length
      ? upcoming.map((a) => el("div", { class: "doc" }, el("span", { class: "what" }, el("strong", { text: when(a.starts_at) }), el("br"), el("span", { class: "muted", text: a.kind }))))
      : [el("p", { class: "empty", text: "No tienes citas próximas." })]),
    el("p", { class: "muted", text: "Para cambiar o cancelar una cita, escribe al consultorio." })));
  main.append(await bookingCard());
};

async function bookingCard() {
  const team = state.practice.professionals;
  const box = el("section", { class: "card" }, el("h2", { text: "Reservar una cita" }));
  if (!team.length) {
    box.append(el("p", { class: "empty", text: "Por ahora las citas se agendan escribiendo al consultorio." }));
    return box;
  }
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const area = el("div", { class: "stack" });
  const pick = el("select", { "aria-label": "Profesional" }, ...team.map((t) => el("option", { value: t.professional_id, text: t.display_name })));
  const screen = turn;
  const sessionKey = store.get();
  let loadGeneration = 0;
  let submitting = false;
  const active = () => screen === turn && sessionKey === store.get() && state.me !== null;
  const load = async () => {
    if (!active()) return;
    const generation = ++loadGeneration;
    const professionalId = pick.value;
    const current = () => active() && generation === loadGeneration && professionalId === pick.value;
    area.replaceChildren(el("p", { class: "muted", text: "Buscando horarios libres…" }));
    let slots;
    try {
      slots = await api(`/v1/me/slots?professional_id=${encodeURIComponent(professionalId)}&days=60`);
    } catch (err) {
      if (!current()) return;
      area.replaceChildren(el("p", { class: "status error", text: humanError(err) }),
        button("Reintentar horarios", () => { say(status, ""); load(); }));
      return;
    }
    if (!current()) return;
    const days = [...new Set(slots.map((s) => s.local.slice(0, 10)))];
    if (!days.length) {
      let configured = true;
      try { configured = (await api(`/v1/me/booking-status?professional_id=${encodeURIComponent(professionalId)}`)).hours_configured; }
      catch { /* Keep the generic availability message if the diagnostic is unavailable. */ }
      if (!current()) return;
      area.replaceChildren(el("p", { class: "empty", text: configured
        ? "No hay horarios libres en los próximos días. Prueba con otro profesional o escribe al consultorio."
        : "Este profesional todavía no ha publicado sus horarios de atención. Pide al consultorio que los configure o selecciona otro profesional." }),
        button("Actualizar horarios", () => load()));
      return;
    }
    let chosenDay = days[0];
    let chosen = null;
    const pills = el("div", { class: "day-pills", role: "group", "aria-label": "Día" });
    const times = el("div", { class: "times", role: "group", "aria-label": "Hora" });
    const confirm = el("button", { class: "btn primary", type: "button", text: "Elige una hora", disabled: true });
    const drawTimes = () => {
      times.replaceChildren(...slots.filter((s) => s.local.startsWith(chosenDay)).map((s) => {
        const b = el("button", { class: "time-btn", type: "button", "aria-pressed": String(chosen === s), text: s.local.slice(11) });
        b.addEventListener("click", () => {
          if (submitting || !current()) return;
          chosen = s;
          drawTimes();
          confirm.disabled = false;
          confirm.textContent = `Reservar el ${when(s.starts_at)}`;
        });
        return b;
      }));
    };
    const drawDays = () => {
      pills.replaceChildren(...days.map((d) => {
        const p = parts(`${d}T17:00:00Z`);
        const b = el("button", { class: "pill", type: "button", "aria-pressed": String(d === chosenDay) },
          el("span", { text: DAY_NAMES[p.dow].slice(0, 3) }), el("strong", { text: String(Number(d.slice(8))) }));
        b.addEventListener("click", () => { if (submitting || !current()) return; chosenDay = d; chosen = null; confirm.disabled = true; confirm.textContent = "Elige una hora"; drawDays(); drawTimes(); });
        return b;
      }));
    };
    confirm.addEventListener("click", async () => {
      if (!chosen || submitting || !current()) return;
      submitting = true;
      pick.disabled = true;
      for (const control of area.querySelectorAll("button")) control.disabled = true;
      confirm.disabled = true;
      say(status, "Reservando…");
      try {
        await api("/v1/me/appointments", { method: "POST", body: { professional_id: professionalId, starts_at: chosen.starts_at } });
        if (!current()) return;
        say(status, "¡Listo! Tu cita quedó reservada.", "done");
        setTimeout(() => { if (current()) route(); }, 1500);
      } catch (err) {
        if (!current()) return;
        say(status, humanError(err), "error");
        submitting = false;
        pick.disabled = false;
        await load();
      }
    });
    drawDays();
    drawTimes();
    area.replaceChildren(pills, times, confirm);
  };
  pick.addEventListener("change", () => { say(status, ""); load(); });
  if (team.length > 1) box.append(el("label", { class: "field" }, "Con quién", pick));
  box.append(area, status);
  await load();
  return box;
}

// --- TESTS -------------------------------------------------------------------------------------------
VIEWS.tests = async (main, id) => {
  const tests = await api("/v1/me/tests");
  const test = id && tests.find((t) => t.assignment_id === id);
  if (test) return answerTest(main, test);
  main.append(el("h1", { class: "title", text: "Tus tests" }));
  if (!tests.length) {
    main.append(el("p", { class: "empty", text: "No tienes tests por responder." }));
    return;
  }
  for (const t of tests) {
    main.append(el("section", { class: "card" },
      el("h2", { text: t.name }),
      el("p", { class: "muted", text: `${t.items.length} preguntas${t.due_at ? ` · respóndelo antes del ${when(t.due_at).split(",")[0]}` : ""}` }),
      el("a", { class: "btn primary", href: `#tests/${encodeURIComponent(t.assignment_id)}`, text: "Responder" })));
  }
};

function answerTest(main, test) {
  // Drawn into its own box: the thanks replace the questions inside it, after the view
  // was shown (the frame route() drew into is no longer on screen by then).
  const box = el("div", { class: "stack" });
  main.append(box);
  const answers = {};
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const questions = test.items.map((item, i) => {
    let control;
    if (item.type === "choice") {
      control = el("div", { class: "answers", role: "group", "aria-label": `Pregunta ${i + 1}` });
      const draw = () => control.replaceChildren(...item.options.map((o) => {
        const b = el("button", { class: "answer", type: "button", "aria-pressed": String(answers[item.id] === o.value), text: o.label });
        b.addEventListener("click", () => { answers[item.id] = o.value; draw(); });
        return b;
      }));
      draw();
    } else if (item.type === "number") {
      control = el("input", { type: "number", min: item.min ?? undefined, max: item.max ?? undefined, "aria-label": `Pregunta ${i + 1}` });
      control.addEventListener("input", () => { answers[item.id] = control.value === "" ? null : Number(control.value); });
    } else {
      control = el("textarea", { rows: "3", "aria-label": `Pregunta ${i + 1}` });
      control.addEventListener("input", () => { answers[item.id] = control.value; });
    }
    return el("div", { class: "question" }, el("p", { text: `${i + 1}. ${item.text}` }), control);
  });
  const send = el("button", { class: "btn primary", type: "button", text: "Enviar mis respuestas" });
  send.addEventListener("click", async () => {
    const missing = test.items.filter((it) => it.required && (answers[it.id] == null || answers[it.id] === "")).length;
    if (missing) return say(status, missing === 1 ? "Falta responder una pregunta." : `Faltan ${missing} preguntas por responder.`, "error");
    send.disabled = true;
    say(status, "Enviando…");
    try {
      const done = await api(`/v1/me/tests/${encodeURIComponent(test.assignment_id)}`, { method: "POST", body: { answers } });
      const thanks = [el("h1", { class: "title", text: "¡Gracias!" }), el("p", { text: done.message })];
      if (done.help) {
        thanks.unshift(el("section", { class: "alert", role: "alert" }, el("p", { class: "pre", text: done.help })));
      }
      box.replaceChildren(el("section", { class: "card" }, ...thanks, el("a", { class: "btn", href: "#inicio", text: "Volver al inicio" })));
      refreshTestsBadge();
    } catch (err) { say(status, humanError(err), "error"); send.disabled = false; }
  });
  box.append(...[el("h1", { class: "title", text: test.name }),
    test.instructions ? el("p", { class: "muted", text: test.instructions }) : null,
    ...questions, send, status,
    el("p", { class: "muted", text: "No hay respuestas buenas ni malas. Tu psicóloga revisará los resultados contigo." })].filter(Boolean));
}

// --- TALLERES (RF-MKT-05) ----------------------------------------------------------------------------
VIEWS.talleres = async (main) => {
  const list = await api("/v1/me/workshops");
  main.append(el("h1", { class: "title", text: "Talleres y charlas" }));
  if (!list.length) {
    main.append(el("p", { class: "empty", text: "Por ahora no hay talleres programados. Te avisaremos en la app." }));
    return;
  }
  for (const w of list) main.append(workshopCard(w));
};

function workshopCard(w) {
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const companions = el("select", { "aria-label": "Acompañantes" },
    ...[0, 1, 2, 3].map((n) => el("option", { value: String(n), text: n === 0 ? "Voy sola o solo" : `Voy con ${n} acompañante${n === 1 ? "" : "s"}` })));
  companions.value = String(w.companions || 0);
  const join = button(w.registered ? "Guardar cambios" : "Inscribirme", async () => {
    join.disabled = true;
    try {
      await api(`/v1/me/workshops/${encodeURIComponent(w.workshop_id)}`, { method: "POST", body: { companions: Number(companions.value) } });
      say(status, "¡Listo! Estás inscrita o inscrito. Te esperamos.", "done");
      setTimeout(route, 1200);
    } catch (err) {
      say(status, err.status === 409 ? "Ya no quedan cupos suficientes, o el taller cerró su inscripción." : humanError(err), "error");
      join.disabled = false;
    }
  }, "btn primary");
  const leave = w.registered ? button("Ya no puedo ir", async () => {
    if (!sure(leave, "Sí, ya no puedo ir")) return;
    leave.disabled = true;
    try { await api(`/v1/me/workshops/${encodeURIComponent(w.workshop_id)}`, { method: "DELETE" }); route(); }
    catch (err) { say(status, humanError(err), "error"); leave.disabled = false; }
  }) : null;
  const notYet = !w.registered && w.opens_for_you && new Date(w.opens_for_you).getTime() > Date.now();
  if (notYet) { join.disabled = true; join.textContent = `La inscripción abre el ${when(w.opens_for_you)}`; }
  return el("section", { class: "card" },
    el("h2", { text: w.title }),
    el("p", { class: "muted", text: `${when(w.starts_at)} · ${w.place}` }),
    el("p", { text: w.description }),
    w.registered ? el("span", { class: "chip ok", text: "Inscrita/o" }) : el("span", { class: "chip neutral", text: w.seats_left ? `Quedan ${w.seats_left} cupos` : "Sin cupos" }),
    el("label", { class: "field" }, "¿Vas con alguien?", companions),
    ...[join, leave].filter(Boolean), status);
}

// --- PERFIL ------------------------------------------------------------------------------------------
VIEWS.perfil = async (main) => {
  const [me, prompt, loyalty] = await Promise.all([api("/v1/me"), api("/v1/me/consents"), api("/v1/me/loyalty")]);
  const p = me.profile;
  const tier = loyalty.tier;
  main.append(el("h1", { class: "title", text: "Mi perfil" }),
    el("section", { class: "card" },
      el("p", {}, el("strong", { text: p.display_name })),
      el("p", { class: "muted", text: [p.phone, p.email].filter(Boolean).join(" · ") || "Sin datos de contacto" }),
      el("p", { class: "muted", text: "Para corregir tus datos, pídeselo al consultorio." })),
    el("section", { class: "card" },
      el("p", { class: "muted", text: "Tu nivel" }),
      el("h2", { text: tier.name }),
      el("ul", {}, ...tier.benefits.map((b) => el("li", { text: b }))),
      tier.next ? el("p", { class: "muted", text: `Siguiente nivel: ${tier.next.name}. ${tier.next.how}` }) : null,
      el("p", { class: "muted", text: "Los niveles dependen del tiempo que llevas con nosotros y de los talleres a los que asistes, nunca de cuántas sesiones tomas. No son descuentos." })));
  const labels = { marketing: "Novedades del consultorio", analytics: "Ayudarnos a mejorar lo que publicamos", memory: "Recordar tus preferencias" };
  const current = prompt.consents || {};
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const rows = Object.keys(labels).map((purpose) => {
    const value = current[purpose];
    const granted = typeof value === "object" && value !== null ? value.granted : value;
    const flip = button(granted ? "Retirar" : "Aceptar", async () => {
      flip.disabled = true;
      try { await api(`/v1/me/consents/${purpose}`, { method: "PUT", body: { granted: !granted } }); route(); } catch (err) { say(status, humanError(err), "error"); flip.disabled = false; }
    }, "btn small");
    return el("div", { class: "doc" },
      el("span", { class: "what" }, el("strong", { text: labels[purpose] }), el("br"),
        el("span", { class: "muted", text: granted ? "Aceptado" : granted === false ? "No aceptado" : "Sin responder" })),
      flip);
  });
  main.append(el("section", { class: "card" }, el("h2", { text: "Lo que aceptas" }), ...rows, status, el("p", { class: "muted", text: prompt.footer })));
  main.append(el("a", { href: "#cuenta", class: "btn", text: "Mi correo y contraseña" }));
  const out = button("Salir de este celular", async () => {
    if (!sure(out, "Sí, salir de este celular")) return;
    out.disabled = true;
    try { await api("/v1/patient-auth/sign-out", { method: "POST" }); }
    catch (err) { say(status, humanError(err), "error"); out.disabled = false; return; }
    store.clear(); state.me = null; noAccess("Saliste. Puedes volver con tu correo y contraseña o con un enlace vigente del consultorio.");
  });
  main.append(out);
};

// Account creation requires the private link; sign-in itself needs no patient key.
function credentialsForm({ activation = false, email = "", onSuccess }) {
  const mail = el("input", { type: "email", autocomplete: "username", required: true, maxlength: 254, value: email });
  const password = el("input", { type: "password", autocomplete: activation ? "new-password" : "current-password", required: true, maxlength: 200, minlength: activation ? 10 : 1 });
  const repeat = activation ? el("input", { type: "password", autocomplete: "new-password", required: true, maxlength: 200 }) : null;
  const status = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const submit = el("button", { type: "submit", class: "btn primary", text: activation ? "Guardar correo y contraseña" : "Entrar" });
  const form = el("form", { class: "stack" },
    el("label", { class: "field" }, "Correo electrónico", mail),
    el("label", { class: "field" }, activation ? "Contraseña nueva (mínimo 10 caracteres)" : "Contraseña", password),
    repeat ? el("label", { class: "field" }, "Repite la contraseña", repeat) : null, submit, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    if (repeat && password.value !== repeat.value) { say(status, "Las contraseñas no coinciden.", "error"); return; }
    const originalKey = store.get();
    submit.disabled = true;
    try {
      const result = await api(activation ? "/v1/me/account" : "/v1/patient-auth/sign-in", {
        method: "POST", body: { email: mail.value, password: password.value },
      });
      if (!form.isConnected || store.get() !== originalKey) return;
      password.value = "";
      if (repeat) repeat.value = "";
      store.set(result.key);
      onSuccess();
    } catch (err) {
      if (!form.isConnected) return;
      say(status, err instanceof ApiError && [401, 422, 429].includes(err.status)
        ? (err.status === 429 ? "Demasiados intentos. Espera un minuto." : err.detail || "Revisa tu correo y contraseña.")
        : humanError(err), "error");
      submit.disabled = false;
    }
  });
  return form;
}

VIEWS.cuenta = async (main) => {
  const account = await api("/v1/me/account");
  main.append(el("h1", { class: "title", text: "Tu cuenta" }), el("a", { href: "#inicio", class: "btn", text: "Volver al inicio" }));
  if (account.can_activate) {
    main.append(el("p", { text: "Crea tu acceso con correo y contraseña. Se cerrarán tus sesiones anteriores y dejarán de funcionar tus enlaces antiguos." }),
      credentialsForm({ activation: true, email: account.email || state.me.profile.email || "", onSuccess: () => {
        history.replaceState(null, "", "/mi/#inicio"); boot();
      } }));
  } else {
    main.append(el("p", { text: account.email ? `Tu correo de acceso: ${account.email}` : "Pide al consultorio un enlace para activar tu cuenta." }));
  }
  main.append(el("p", { class: "muted", text: "¿Olvidaste tu contraseña o necesitas cambiar tu correo? Pide al consultorio un enlace de recuperación. El consultorio verificará tu identidad antes de entregártelo." }));
};


// --- start ---------------------------------------------------------------------------------------------
window.addEventListener("hashchange", () => {
  if (/^#k=/.test(location.hash)) { boot(); return; }
  route();
});
document.addEventListener("DOMContentLoaded", () => {
  boot();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/mi/sw.js").catch(() => {});
});
