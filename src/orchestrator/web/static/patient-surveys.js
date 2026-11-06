// Voluntary service ratings in the existing authenticated patient portal.
"use strict";

VIEWS.encuestas = async (main, id) => {
  const [data, prompt] = await Promise.all([api("/v1/me/surveys"), api("/v1/me/consents")]);
  main.append(el("h1", { class: "title", text: "Encuestas del servicio" }),
    el("p", { text: "Tu opinión ayuda a revisar la atención del consultorio. Estas encuestas no son tests clínicos." }));
  if (!data.consent) {
    const status = el("p", { class: "status", role: "status" });
    const accept = button("Sí, quiero participar", async () => {
      accept.disabled = true;
      try { await api("/v1/me/consents/surveys", { method: "PUT", body: { granted: true } }); route(); }
      catch (err) { say(status, humanError(err), "error"); accept.disabled = false; }
    });
    const decline = button("No, gracias", async () => {
      decline.disabled = true;
      try { await api("/v1/me/consents/surveys", { method: "PUT", body: { granted: false } }); say(status, "Tu decisión quedó guardada. Tus citas no cambian.", "done"); }
      catch (err) { say(status, humanError(err), "error"); }
      finally { decline.disabled = false; }
    });
    main.append(el("section", { class: "card stack" }, el("h2", { text: "¿Quieres recibir encuestas voluntarias?" }),
      el("p", { text: "Puedes aceptar o rechazar. Tu atención no depende de participar y puedes retirar tu permiso en Mi perfil." }),
      el("div", { class: "row" }, accept, decline), el("p", { class: "muted", text: prompt.footer }), status));
    return;
  }
  const invitation = data.invitations.find((r) => r.id === id);
  if (!invitation) {
    main.append(...data.invitations.map((r) => el("section", { class: "card" }, el("h2", { text: r.title }),
      r.reminder_count ? el("p", { class: "muted", text: "Te recordamos esta encuesta pendiente. Responder sigue siendo voluntario." }) : null,
      el("a", { class: "btn primary", href: `#encuestas/${encodeURIComponent(r.id)}`, text: "Responder esta encuesta" }))));
    if (!data.invitations.length) main.append(el("p", { class: "empty", text: "No tienes encuestas pendientes. Si corresponde, aparecerá una después de una consulta completada." }));
    return;
  }
  const selections = new Map();
  const groups = invitation.questions.map((q) => {
    const choices = [];
    for (let value = q.minimum; value <= q.maximum; value++) {
      const radio = el("input", { type: "radio", name: q.id, value: String(value), required: true });
      radio.addEventListener("change", () => selections.set(q.id, value));
      choices.push(el("label", { class: "check" }, radio, String(value)));
    }
    return el("fieldset", { class: "stack" }, el("legend", { text: q.text }), el("p", { class: "muted", text: `${q.minimum}: ${q.low_label} · ${q.maximum}: ${q.high_label}` }), el("div", { class: "row" }, ...choices));
  });
  const status = el("p", { class: "status", role: "status" });
  const send = el("button", { type: "submit", class: "btn primary", text: "Enviar mi valoración del servicio" });
  const form = el("form", { class: "stack" }, el("h2", { text: invitation.title }), ...groups, send, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault(); send.disabled = true;
    try {
      await api(`/v1/me/surveys/${encodeURIComponent(invitation.id)}`, { method: "POST", body: { answers: Object.fromEntries(selections) } });
      form.replaceChildren(el("p", { class: "status done", role: "status", text: "Gracias. Tu valoración quedó registrada." }), el("a", { class: "btn", href: "#encuestas", text: "Volver a mis encuestas" }));
    } catch (err) { say(status, humanError(err), "error"); send.disabled = false; }
  });
  main.append(form);
};
