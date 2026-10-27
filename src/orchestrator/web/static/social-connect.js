"use strict";
// Meta's SDK is loaded only on this bridge. The main app keeps its original CSP.
// No staff key, provider token, user id or phone id is passed to the SDK by Tatos.
const button = document.querySelector("#connect");
const message = document.querySelector("#status");
const [flow_id, state] = location.hash.slice(1).split(".");
history.replaceState(null, "", location.pathname);
let config;
async function post(path, extra = {}) {
  const response = await fetch(`/v1/social/login/whatsapp/${path}`, {
    method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ flow_id, state, ...extra }),
  });
  if (!response.ok) throw new Error("authorization_failed");
  return response.json();
}
async function prepare() {
  if (!/^[0-9a-f]{32}$/.test(flow_id || "") || !/^[\w-]{32,128}$/.test(state || "")) throw new Error("invalid_request");
  config = await post("launch");
  window.fbAsyncInit = () => {
    window.FB.init({ appId: config.app_id, autoLogAppEvents: false, xfbml: false, version: config.version });
    button.disabled = false;
    message.textContent = "Continúa para elegir tu cuenta de WhatsApp Business en Meta.";
  };
  const sdk = document.createElement("script");
  sdk.src = "https://connect.facebook.net/es_LA/sdk.js";
  sdk.async = true;
  sdk.onerror = () => { message.textContent = "No se pudo abrir Meta. Revisa la conexión o los bloqueadores del navegador."; };
  document.head.append(sdk);
}
button.addEventListener("click", () => {
  button.disabled = true;
  // FB.login opens the provider's official Embedded Signup from this user gesture.
  window.FB.login(async (response) => {
    try {
      const code = response?.authResponse?.code;
      await post("complete", { code: code || null, cancelled: !code });
      location.replace("/app#ajustes/redes");
    } catch {
      message.textContent = "No se pudo completar la autorización. Vuelve a Tatos e inicia una conexión nueva.";
    }
  }, { config_id: config.config_id, response_type: "code", override_default_response_type: true,
    extras: { setup: {}, sessionInfoVersion: "3" } });
});
prepare().catch(() => { message.textContent = "Esta solicitud caducó. Vuelve a Tatos y pulsa Conectar WhatsApp otra vez."; });
