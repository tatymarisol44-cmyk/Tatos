# Conectar redes sociales en Consultorio

El botón **Conectar redes sociales** está en la cabecera de **Ajustes** y **Marketing**, para el rol de administración. Abre `#ajustes/redes`, donde se pueden vincular cuentas de WhatsApp, Instagram, Facebook, TikTok y Telegram, revisar su preparación y desconectarlas con confirmación. Los demás roles conservan sus permisos y reciben una indicación para contactar a administración si abren esa ruta directamente.

El formulario pide la red, el nombre público y el identificador de la cuenta proporcionado por la plataforma. WhatsApp usa el ID del número de WhatsApp Business e Instagram su ID de cuenta profesional. El apartado **Configuración de la conexión** permite usar el nombre preparado por administración. Las referencias predeterminadas son `WA_MAIN`, `IG_MAIN`, `FB_MAIN`, `TT_MAIN` y `TG_MAIN`.

Se usa la API existente `POST /v1/social/accounts`, con credenciales por referencia según ADR 0015. El operador configura la autorización en el almacén de secretos del servidor con el nombre `SOCIAL_SECRET_<referencia>`. Los tokens no se solicitan ni se envían desde este formulario. Registrar una cuenta sin ese secreto muestra **Pendiente de autorización**; con el secreto presente muestra **Configuración lista**. Ese estado indica presencia de configuración y no verifica la validez del token en la plataforma. La vinculación no publica ni envía mensajes. Este cambio no añade un flujo OAuth.

Las cuentas se consultan mediante la API del tenant y la desconexión usa el DELETE existente, con confirmación. Un fallo al consultar las cuentas se presenta como error; no se convierte en una lista vacía ni habilita un registro engañoso.

## Validación local del 26 de octubre de 2026

Los tres recorridos nuevos de Chromium aprobaron: alta, estado pendiente/configurado, rechazo de duplicados, desconexión, representación segura del nombre, permisos de administración y errores de lectura. La pantalla se comprobó a 390 × 844 sin desbordamiento horizontal. También aprobaron la comprobación sintáctica de JavaScript, Ruff y Gitleaks sobre los tres archivos cambiados, sin modificar excepciones.

La revisión de interfaz y API social ejecutó 53 casos: **52 aprobados y un fallo previo** en `test_tests_with_official_texts_and_the_on_call_list`, que busca «Guardar textos» mientras el editor actual utiliza «Guardar test». Ese mismo fallo se reprodujo sobre el commit base sin los cambios de redes. No se alteró la funcionalidad clínica ni se omitió ese test. No se presenta esta batería como completamente aprobada.

Pruebas con cuentas, credenciales y datos sintéticos. No se conectaron redes reales, usaron credenciales productivas, cambiaron migraciones ni desplegó esta modificación en Render. [Evidencia](evidence/social-connections-2026-10-26.json).
