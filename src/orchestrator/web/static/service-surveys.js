// Uses the existing staff API, form controls and role-aware marketing route.
"use strict";

async function serviceSurveysTab() {
  const [definitions, config] = await Promise.all([api("/v1/surveys/definitions"), api("/v1/surveys/configuration")]);
  const selected = el("select", {}, el("option", { value: "", text: "Elige una encuesta" }), ...definitions.map((d) => el("option", { value: `${d.id}:${d.version}`, text: `${d.spec.title} · versión ${d.version}` })));
  selected.value = config.settings.survey_id ? `${config.settings.survey_id}:${config.settings.version}` : "";
  const enabled = el("input", { type: "checkbox", checked: config.settings.enabled });
  const after = el("input", { type: "number", min: "0", max: "43200", value: String(config.settings.after_minutes) });
  const reminder = el("input", { type: "number", min: "1", max: "720", value: String(config.settings.reminder_hours) });
  const maximum = el("input", { type: "number", min: "0", max: "2", value: String(config.settings.max_reminders) });
  const status = el("p", { class: "status", role: "status" });
  const save = el("button", { type: "submit", class: "btn primary", text: "Guardar automatización de encuestas" });
  const settings = el("form", { class: "stack" }, el("label", { class: "check" }, enabled, "Invitar después de completar una consulta, con consentimiento"), el("label", { class: "field" }, "Encuesta y versión para próximas consultas", selected), el("div", { class: "fields" }, el("label", { class: "field" }, "Minutos después de completar la consulta", after), el("label", { class: "field" }, "Horas para recordar una encuesta pendiente", reminder), el("label", { class: "field" }, "Máximo de recordatorios", maximum)), el("p", { class: "muted", text: "La app está disponible. WhatsApp, Facebook e Instagram quedan pendientes de su integración y permisos; todavía no envían invitaciones." }), save, status);
  settings.addEventListener("submit", async (event) => {
    event.preventDefault(); save.disabled = true;
    try {
      const [id, version] = selected.value.split(":");
      await api("/v1/surveys/configuration", { method: "PUT", body: { revision: config.revision, settings: { enabled: enabled.checked, survey_id: id || null, version: version ? Number(version) : null, after_minutes: Number(after.value), reminder_hours: Number(reminder.value), max_reminders: Number(maximum.value), channels: ["app"] } } });
      route();
    } catch (err) { say(status, humanError(err), "error"); save.disabled = false; }
  });
  const reportBox = el("div", { class: "stack" });
  const month = el("input", { type: "month", value: todayISO().slice(0, 7), "aria-label": "Mes del informe de encuestas" });
  const draw = async () => {
    const report = await api(`/v1/surveys/report?month=${encodeURIComponent(month.value)}`);
    reportBox.replaceChildren(...report.groups.map((g) => {
      const definition = definitions.find((d) => d.id === g.survey_id && d.version === g.version);
      return card(`${definition?.spec.title || "Encuesta"} · versión ${g.version}`, "",
        el("p", { text: `Invitaciones registradas: ${g.invited}. Respuestas: ${g.answered}.` }),
        el("p", { text: g.mean == null ? "Todavía no hay respuestas." : `Valoración media del servicio: ${Math.round(g.mean)} de 100.` }),
        ...g.question_means.map((q) => el("p", { class: "muted", text: `${definition?.spec.questions.find((item) => item.id === q.question_id)?.text || q.question_id}: media ${q.mean_rating.toFixed(1)} (${q.responses} respuestas).` })));
    }));
    if (!report.groups.length) reportBox.append(empty("No hay invitaciones registradas en este mes."));
    if (report.critical_alerts.length) reportBox.append(card("Valoraciones del servicio por revisar", "",
      ...report.critical_alerts.map((a) => {
        const mark = action("Marcar como revisada", async () => { mark.disabled = true; try { await api(`/v1/surveys/responses/${a.id}/acknowledge`, { method: "POST" }); await draw(); } catch (err) { say(status, humanError(err), "error"); mark.disabled = false; } });
        return el("div", { class: "notice" }, el("p", { text: `Valoración de satisfacción: ${a.satisfaction} de 100.` }), canReadCare() ? link(`#paciente/${encodeURIComponent(a.patient_id)}`, "Abrir contacto del paciente", "btn") : el("p", { class: "muted", text: "Coordina el seguimiento con recepción o la profesional responsable." }), mark);
      })));
  };
  month.addEventListener("change", () => draw().catch((err) => say(status, humanError(err), "error")));
  const process = action("Revisar recordatorios pendientes", async () => {
    process.disabled = true;
    try { const done = await api("/v1/surveys/run", { method: "POST" }); say(status, `Recordatorios disponibles en la app: ${done.app_reminders}. Envíos externos: ${done.external_sent}.`, "done"); }
    catch (err) { say(status, humanError(err), "error"); }
    finally { process.disabled = false; }
  });
  await draw();
  return [card("Encuestas voluntarias del servicio", "", el("p", { text: "Evalúan la atención del consultorio. Se guardan separadas de los tests y resultados clínicos." }), settings), surveyDefinitionForm(definitions), card("Informe de satisfacción", "", el("label", { class: "field" }, "Mes del informe de encuestas", month), process, reportBox)];
}

function surveyDefinitionForm(definitions) {
  let editing = null;
  const latest = new Map();
  for (const d of definitions) if (!latest.has(d.id) || latest.get(d.id).version < d.version) latest.set(d.id, d);
  const pick = el("select", {}, el("option", { value: "", text: "Crear una nueva encuesta" }), ...[...latest.values()].map((d) => el("option", { value: d.id, text: `Nueva versión de: ${d.spec.title}` })));
  const title = el("input", { value: "Experiencia del servicio", maxlength: "120", required: true });
  const threshold = el("input", { type: "number", min: "0", max: "100", value: "40" });
  const questionBox = el("div", { class: "stack" });
  const rows = [];
  const status = el("p", { class: "status", role: "status" });
  const add = (question = {}) => {
    const text = el("input", { value: question.text || "", maxlength: "250", required: true, "aria-label": "Pregunta del servicio" });
    const min = el("input", { type: "number", min: "1", max: "9", value: String(question.minimum || 1), "aria-label": "Mínimo de la escala" });
    const max = el("input", { type: "number", min: "2", max: "10", value: String(question.maximum || 5), "aria-label": "Máximo de la escala" });
    const low = el("input", { value: question.low_label || "Muy insatisfecha", maxlength: "60", "aria-label": "Etiqueta del mínimo" });
    const high = el("input", { value: question.high_label || "Muy satisfecha", maxlength: "60", "aria-label": "Etiqueta del máximo" });
    const row = { id: question.id || `p-${crypto.randomUUID().slice(0, 12)}`, text, min, max, low, high };
    const node = el("div", { class: "card stack" }, el("label", { class: "field" }, "Pregunta", text), el("div", { class: "fields" }, el("label", { class: "field" }, "Mínimo", min), el("label", { class: "field" }, "Máximo", max)), el("div", { class: "fields" }, low, high), action("Quitar pregunta", () => { if (rows.length < 2) return say(status, "Debe quedar al menos una pregunta.", "error"); rows.splice(rows.indexOf(row), 1); node.remove(); }));
    rows.push(row); questionBox.append(node);
  };
  add({ text: "¿Qué tan satisfecha estás con el servicio recibido?" });
  pick.addEventListener("change", () => {
    editing = latest.get(pick.value) || null;
    title.value = editing?.spec.title || "Experiencia del servicio";
    threshold.value = String(editing?.spec.critical_below ?? 40);
    rows.splice(0); questionBox.replaceChildren();
    (editing?.spec.questions || [{ text: "¿Qué tan satisfecha estás con el servicio recibido?" }]).forEach(add);
  });
  const save = el("button", { type: "submit", class: "btn primary", text: "Guardar versión de encuesta" });
  const form = el("form", { class: "stack" }, el("label", { class: "field" }, "Crear o revisar una definición", pick), el("label", { class: "field" }, "Nombre de la encuesta", title), questionBox, action("Agregar pregunta de satisfacción", () => add()), el("label", { class: "field" }, "Avisar internamente cuando la valoración sea inferior a (0 a 100)", threshold), el("p", { class: "muted", text: "Las escalas se normalizan para resumir satisfacción. No producen un resultado clínico. Una versión nueva no cambia preguntas que ya fueron asignadas." }), save, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); save.disabled = true;
    try {
      const spec = { title: title.value.trim(), critical_below: Number(threshold.value), questions: rows.map((r) => ({ id: r.id, text: r.text.value.trim(), minimum: Number(r.min.value), maximum: Number(r.max.value), low_label: r.low.value, high_label: r.high.value })) };
      await api("/v1/surveys/definitions", { method: "POST", body: { spec, id: editing?.id || null, version: editing?.version || null } });
      route();
    } catch (err) { say(status, humanError(err), "error"); save.disabled = false; }
  });
  return card("Preguntas y escalas de satisfacción", "", form);
}
