"use strict";

// Reuse the authenticated application, forms, routing and dialogs. No third-party JS.
async function instrumentImportsView(main, id) {
  const head = page("Fuentes de tests", "Solo tu autora puede abrir los borradores. No cargues respuestas ni datos de pacientes aquí.", link("#ajustes", "Volver a Tests"));
  const endpoint = "/v1/clinical/instrument-imports";
  if (!id) {
    const drafts = await api(endpoint);
    const fields = {};
    const labels = { name: "Nombre del test", publisher: "Propietario o editor", licence: "Licencia", edition: "Edición", source_reference: "Fuente o referencia bibliográfica", permitted_use: "Uso permitido por la licencia", authorised_api: "API oficial autorizada, si existe (no pegues credenciales)", calculation_reference: "Manual o referencia de puntuación", clinical_validation_reference: "Referencia de validación clínica" };
    const form = el("form", { class: "stack" });
    for (const [key, label] of Object.entries(labels)) {
      fields[key] = el("input", { required: !["authorised_api", "calculation_reference", "clinical_validation_reference"].includes(key), maxlength: key === "name" ? "120" : key === "publisher" ? "200" : key === "edition" ? "100" : "500" });
      form.append(el("label", { class: "field" }, label, fields[key]));
    }
    const file = el("input", { type: "file", required: true, accept: ".json,.docx,.xlsx,.pdf,.png,.jpg,.jpeg" });
    const save = el("button", { type: "submit", class: "btn primary", text: "Cargar como borrador" });
    const status = el("p", { class: "status", role: "status" });
    form.append(el("label", { class: "field" }, "Archivo original, hasta 5 MB", file), save, status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); if (save.disabled || !form.reportValidity()) return;
      if (file.files[0].size > 5 * 1024 * 1024) return say(status, "El archivo supera 5 MB.", "error");
      save.disabled = true;
      try {
        const data = new FormData(); data.append("file", file.files[0]);
        data.append("manifest", JSON.stringify(Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.value.trim()]))));
        const draft = await api(endpoint, { method: "POST", body: data });
        location.hash = `#ajustes/importaciones/${draft.id}`;
      } catch (err) { say(status, humanError(err), "error"); save.disabled = false; }
    });
    main.replaceChildren(head,
      card("Borradores privados", "", ...drafts.map((d) => link(`#ajustes/importaciones/${d.id}`, `${d.manifest.name} · ${d.format} · ${d.state}`, "btn"))),
      card("Incorporar una fuente", "", el("p", { text: "Word: texto literal. Excel: celdas sin fórmulas. JSON: estructura de definición. PDF e imágenes: adjuntos, sin OCR en esta pantalla. La lectura no valida baremos ni interpreta dibujos." }), form));
    return;
  }
  const draft = await api(`${endpoint}/${encodeURIComponent(id)}`);
  const status = el("p", { class: "status", role: "status" });
  const preview = el("pre", { text: draft.preview.text || draft.preview.notice || "Fuente retirada; se conserva su trazabilidad." });
  const parts = [head, card(draft.manifest.name, "", el("p", { text: `${draft.manifest.publisher} · ${draft.manifest.edition} · ${draft.manifest.licence}` }), el("p", { class: "muted", text: `SHA-256: ${draft.sha256}` }), el("p", { text: draft.manifest.authorised_api ? "La API declarada sigue bloqueada: faltan contrato, permisos y adaptador verificado." : "No hay una API oficial declarada." }), preview)];
  if (draft.state === "draft") {
    const spec = el("textarea", { rows: "14", "aria-label": "Definición JSON revisada", placeholder: "Pega la configuración JSON exportada del editor de Tests y revisada contra el manual autorizado.", text: draft.preview.suggested_spec ? JSON.stringify(draft.preview.suggested_spec, null, 2) : "" });
    const calculation = el("input", { required: true, maxlength: "500", value: draft.manifest.calculation_reference || "" });
    const validation = el("input", { required: true, maxlength: "500", value: draft.manifest.clinical_validation_reference || "" });
    const checks = ["Tengo derecho a usar esta edición y su puntuación.", "Revisé preguntas, cálculo, rangos y validación con el manual autorizado.", "La definición no contiene respuestas ni datos personales de pacientes."].map((label) => ({ input: el("input", { type: "checkbox", required: true }), label }));
    const save = el("button", { class: "btn primary", type: "submit", text: "Incorporar test privado tras revisión" });
    const form = el("form", { class: "stack" }, el("label", { class: "field" }, "Definición JSON revisada", spec), el("label", { class: "field" }, "Referencia autorizada de cálculo", calculation), el("label", { class: "field" }, "Referencia de validación clínica", validation), ...checks.map((c) => el("label", { class: "check" }, c.input, c.label)), save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); if (save.disabled || !form.reportValidity()) return;
      save.disabled = true;
      try {
        const result = await api(`${endpoint}/${id}/incorporate`, { method: "POST", body: { revision: draft.revision, spec: JSON.parse(spec.value), visibility: "private", licensed: checks[0].input.checked, reviewed: checks[1].input.checked, no_patient_data: checks[2].input.checked, calculation_reference: calculation.value.trim(), validation_reference: validation.value.trim() } });
        delete spec.dataset.dirty;
        location.hash = `#ajustes/test/${result.instrument_id}`;
      } catch (err) { say(status, err instanceof SyntaxError ? "Revisa el JSON de la definición." : humanError(err), "error"); save.disabled = false; }
    });
    parts.push(card("Revisión profesional obligatoria", "", el("p", { text: "La app no determina si una licencia o un baremo son válidos. No copies rangos de otra edición ni datos ficticios a un test clínico." }), form));
  } else if (draft.instrument_id) parts.push(link(`#ajustes/test/${draft.instrument_id}`, "Abrir definición incorporada", "btn"));
  const purge = action("Retirar el archivo fuente", async () => {
    if (!sure(purge, "Sí, retirar archivo y vista previa")) return;
    try { await api(`${endpoint}/${id}/purge`, { method: "POST", body: { revision: draft.revision } }); route(); }
    catch (err) { say(status, humanError(err), "error"); }
  });
  parts.push(purge, status); main.replaceChildren(...parts);
}

async function taxTrialsView(main, id) {
  const endpoint = "/v1/tax-trials";
  const head = page("Ensayo fiscal privado", "SIMULACIÓN LOCAL SIN VALIDEZ TRIBUTARIA. No se firma, no se envía al SRI y no genera RIDE.", link("#ajustes", "Volver a Ajustes"));
  if (id) {
    const document = await api(`${endpoint}/documents/${encodeURIComponent(id)}`);
    const status = el("p", { class: "status", role: "status" });
    const retry = action("Reintentar simulación cuando venza la espera", async () => {
      try { await api(`${endpoint}/documents/${id}/retry`, { method: "POST", body: { revision: document.revision } }); route(); }
      catch (err) { say(status, humanError(err), "error"); }
    });
    main.replaceChildren(head, card(`ENSAYO-${document.id}`, "", el("p", { text: `${document.state} · intento ${document.attempts} · secuencia privada ${document.sequence}` }), el("p", { text: document.outcome.message }), el("pre", { text: document.xml }), el("p", { class: "muted", text: `Huella XML: ${document.xml_sha256}` }), ...(document.state === "retry_wait" ? [retry] : []), status), link("#ajustes/fiscal-ensayo", "Volver a los ensayos", "btn"));
    return;
  }
  const [config, documents, obligations] = await Promise.all([api(`${endpoint}/issuers`), api(`${endpoint}/documents`), api("/v1/billing/orders")]);
  const fields = {};
  const labels = { ruc: "RUC de prueba (13 dígitos; no se verifica contra SRI)", legal_name: "Razón social de prueba", trade_name: "Nombre comercial", address: "Dirección fiscal de prueba", establishment: "Establecimiento (3 dígitos)", emission_point: "Punto de emisión (3 dígitos)", tax_regime: "Régimen declarado, pendiente de revisión tributaria" };
  const issuerForm = el("form", { class: "stack" });
  const selected = el("select", {}, el("option", { value: "", text: "Crear emisor de prueba" }), ...config.issuers.map((r) => el("option", { value: r.id, text: `${r.profile.trade_name} · ${r.ruc}` })));
  issuerForm.append(el("label", { class: "field" }, "Emisor", selected));
  for (const [key, label] of Object.entries(labels)) { fields[key] = el("input", { required: true, maxlength: ["establishment", "emission_point"].includes(key) ? "3" : key === "ruc" ? "13" : key === "address" ? "300" : "200" }); issuerForm.append(el("label", { class: "field" }, label, fields[key])); }
  selected.addEventListener("change", () => { const row = config.issuers.find((r) => r.id === selected.value); for (const [key, field] of Object.entries(fields)) field.value = row?.profile[key] || ""; });
  const status = el("p", { class: "status", role: "status" });
  const save = el("button", { type: "submit", class: "btn", text: "Guardar emisor de prueba" });
  issuerForm.append(save);
  issuerForm.addEventListener("submit", async (event) => {
    event.preventDefault(); if (save.disabled || !issuerForm.reportValidity()) return; save.disabled = true;
    const previous = config.issuers.find((r) => r.id === selected.value);
    try { await api(`${endpoint}/issuers`, { method: "PUT", body: { id: previous?.id || null, version: previous?.version || 0, profile: { ...Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.value.trim()])), signing_reference: previous?.profile.signing_reference || "" } } }); route(); }
    catch (err) { say(status, humanError(err), "error"); save.disabled = false; }
  });
  const issuer = el("select", { required: true }, ...config.issuers.map((r) => el("option", { value: r.id, text: r.profile.trade_name })));
  const order = el("select", { required: true }, ...obligations.filter((o) => o.kind === "appointment").map((o) => el("option", { value: o.invoice_id, text: `${billingMoney(o.amount_cents)} · ${o.resource_id}` })));
  const tax = el("input", { type: "number", required: true, min: "0", step: "1", value: "0" });
  const basis = el("input", { required: true, maxlength: "300", placeholder: "Referencia ficticia de la regla del ensayo" });
  const scenario = el("select", {}, ...[["accept", "Aceptar en simulador"], ["reject", "Rechazar en simulador"], ["transient", "Fallo temporal y reintento"]].map(([value, text]) => el("option", { value, text })));
  const run = el("button", { type: "submit", class: "btn primary", text: "Generar ensayo no fiscal" });
  let requestId = crypto.randomUUID();
  const form = el("form", { class: "stack" }, el("label", { class: "field" }, "Emisor de prueba", issuer), el("label", { class: "field" }, "Obligación registrada de demostración", order), el("label", { class: "field" }, "Impuesto en centavos (explícito; no se infiere IVA)", tax), el("label", { class: "field" }, "Fundamento del importe para este ensayo", basis), el("label", { class: "field" }, "Escenario del proveedor simulado", scenario), run);
  form.addEventListener("input", () => { requestId = crypto.randomUUID(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); if (run.disabled || !form.reportValidity()) return;
    const amount = obligations.find((o) => o.invoice_id === order.value)?.amount_cents;
    const cents = Number(tax.value);
    if (!Number.isInteger(cents) || amount == null || cents < 0 || cents > amount) return say(status, "El impuesto debe ser un número de centavos entre cero y el total.", "error");
    run.disabled = true;
    try { const result = await api(`${endpoint}/documents`, { method: "POST", body: { issuer_id: issuer.value, request_id: requestId, order_id: order.value, net_cents: amount - cents, tax_cents: cents, tax_basis_reference: basis.value.trim(), scenario: scenario.value, mode: "non_fiscal_simulation" } }); location.hash = `#ajustes/fiscal-ensayo/${result.id}`; }
    catch (err) { say(status, humanError(err), "error"); run.disabled = false; }
  });
  main.replaceChildren(head, card("Emisores separados por consultorio", "", el("p", { text: "No cargues firmas ni contraseñas. Los secuenciales son exclusivos del prototipo y no se usarán en producción." }), issuerForm), card("Nuevo ensayo", "", form, status), card("Historial privado", "", ...documents.map((d) => link(`#ajustes/fiscal-ensayo/${d.id}`, `ENSAYO-${d.id} · ${d.state}`, "btn"))));
}
