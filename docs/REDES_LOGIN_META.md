# Conectar Facebook, Instagram y WhatsApp iniciando sesión

Preparado el 27/10/2026 para la candidata de Tatos. **Implementación local, todavía sin publicar.** Los tres recorridos se probaron con Meta simulado; no acreditan autorización de una cuenta real ni revisión de la app por Meta.

## Lo que hace cada consultorio

1. Entra en Tatos con su propia cuenta del equipo.
2. Abre **Marketing → Conectar redes sociales**, o **Ajustes → Conectar redes sociales**.
3. Pulsa **Conectar Facebook**, **Conectar Instagram** o **Conectar WhatsApp**.
4. Inicia sesión en la ventana o página oficial de Meta. Tatos no pide su contraseña de Facebook, Instagram ni WhatsApp.
5. Revisa y autoriza los permisos del proveedor.
6. Al volver a Tatos, elige por nombre la cuenta que corresponde a ese consultorio y pulsa **Conectar**.
7. La cuenta aparecerá como **Conectada**. Si la autorización vence o deja de estar disponible, vuelve a iniciar la conexión.

No se introducen IDs de páginas, números ni referencias de secretos. Los identificadores se obtienen del proveedor en el servidor. Marketing, Dueña y Administración pueden conectar y desconectar; pacientes, Recepción y Psicóloga sin esos roles no pueden configurar canales.

Facebook conecta páginas administradas por la persona. Instagram utiliza una cuenta profesional de empresa o creador. WhatsApp abre el registro integrado del negocio; Meta puede solicitar verificación del número. No se elimina una cuenta de WhatsApp del teléfono automáticamente ni se promete compatibilidad de migración/coexistencia sin comprobar la configuración del negocio.

Para desconectar, pulsa **Desconectar** en la cuenta y confirma en el mismo botón. Tatos desactiva la cuenta y elimina su permiso cifrado local. Esto no elimina la página, el perfil, el número ni los mensajes que conserva Meta.

## Configuración que hace la desarrolladora una sola vez

La propietaria confirmó que **la app de Meta ya está creada**. El servicio de Tatos en Render ya contiene `META_APP_SECRET` y `WHATSAPP_VERIFY_TOKEN`; se comprobó solamente su presencia, sin mostrar ni guardar valores. Los otros campos siguientes todavía faltaban al revisar.

| Campo del servidor | Procedencia / uso |
| --- | --- |
| `META_APP_ID` | App ID de la aplicación de Tatos en Meta |
| `META_APP_SECRET` | Secreto de esa misma aplicación; conservar el actual si corresponde |
| `META_LOGIN_CONFIG_ID` | Configuración de Facebook Login para empresas, con los permisos de páginas |
| `META_WHATSAPP_CONFIG_ID` | Configuración de registro integrado de WhatsApp |
| `INSTAGRAM_APP_ID` | App ID del producto Instagram con Instagram Login |
| `INSTAGRAM_APP_SECRET` | Secreto del producto Instagram; no asumir que es el secreto general de Meta |
| `SOCIAL_LOGIN_ORIGIN` | Origen HTTPS canónico de Tatos, sin ruta ni barra final |
| `SOCIAL_LOGIN_ENABLED` | `false` por defecto; activar después de configurar, revisar y publicar |
| `SOCIAL_LOGIN_GRAPH_VERSION` | `v26.0` por defecto, fijada en el servidor |

La ventana privada **Tatos — Conectar la app de Meta** permite guardar los campos de la app en el Administrador de credenciales de Windows. No se solicitan por chat ni se escriben en documentos. Guardarlos allí no los sube a Render ni publica la aplicación.

Cuando la candidata esté publicada y su origen sea el actual `https://agency-api-irim.onrender.com`, las rutas definidas en esta rama serán:

| Uso | Dirección |
| --- | --- |
| Retorno de Facebook Login | `https://agency-api-irim.onrender.com/v1/social/login/facebook/callback` |
| Retorno de Instagram Login | `https://agency-api-irim.onrender.com/v1/social/login/instagram/callback` |
| Página de registro integrado de WhatsApp | `https://agency-api-irim.onrender.com/social-connect` |
| Webhook común | `https://agency-api-irim.onrender.com/v1/channels/meta` |

La página de WhatsApp utiliza el SDK oficial y entrega un código al servidor; no es un retorno OAuth de Facebook para copiar como URL de WhatsApp. Los secretos y los IDs centrales se configuran una vez en Tatos; cada consultorio autoriza sus propios activos desde Meta.

### Permisos de la candidata

- Facebook: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `pages_manage_metadata`, `pages_messaging`.
- Instagram: `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_messages`.
- WhatsApp: `whatsapp_business_management`, `whatsapp_business_messaging`.

Para usar cuentas externas al equipo de la app, deben comprobarse el modo de publicación, la revisión y los accesos que exige Meta. Crear la app por sí solo no acredita esas habilitaciones. No se han aceptado condiciones ni solicitado revisiones en nombre de la propietaria.

## Lo que verifica el servidor

- Estado de autorización impredecible, de un solo uso, ligado a una cookie HttpOnly del navegador y con caducidad de quince minutos.
- La selección final requiere la misma persona autenticada y el mismo consultorio; vuelve a comprobar sus permisos actuales.
- Páginas y números obtenidos por llamadas autenticadas del servidor; no se confía en un ID enviado desde el navegador.
- Grant de Meta ligado a la app de Tatos y con los permisos requeridos. Los errores o rechazos no conectan cuentas.
- Tokens cifrados mediante AES-GCM con clave derivada y propósito separado, ligados al consultorio, referencia, red e identificador. No salen por las APIs ni se incluyen en la exportación de pacientes, modelos o campañas.
- Una cuenta OAuth no puede quedar activa en dos consultorios. La configuración manual de Meta queda cerrada cuando se habilita este flujo.
- Desconectar elimina el token local. Un permiso OAuth ausente, alterado o vencido no se presenta como una publicación simulada exitosa.
- La política de scripts del panel principal sigue restringida a Tatos. Solo la página dedicada de WhatsApp permite cargar el SDK oficial de Meta.
- Los códigos, estados y secretos en URLs se eliminan de los registros técnicos. El trabajo de retención purga las autorizaciones temporales caducadas.

Las autorizaciones tienen su propia vigencia: esta candidata pide volver a conectar cuando caducan. Todavía no implementa una renovación periódica de tokens de Instagram, ni callbacks de revocación/eliminación de datos de Meta, ni el registro de números nuevos con PIN mediante API. Esos requisitos deben contrastarse con la revisión y el onboarding reales del producto antes de anunciar acceso general. Los números que necesitan registro, migración, facturación o coexistencia pueden requerir pasos adicionales de Meta.

## Publicación pendiente

Verificación local del 27/10: suite completa de 1.128 pruebas aprobadas, 39 omitidas y cobertura del 94,69 %. Después se ligó la lectura de cada token a la red y al activo solicitantes, y se añadió una prueba de firma por producto: la verificación final de esas rutas y sus consumidores aprobó 141 pruebas, incluidas ocho de navegador. Mypy aprobó 129 archivos y Ruff aprobó formato y lint. Gitleaks no encontró secretos con la configuración vigente del repositorio, sin ampliar sus excepciones. Los proveedores se simularon; ninguna de estas cifras acredita una autorización real de Meta. CI de Linux/PostgreSQL todavía debe comprobar esta candidata.

La migración `0025_social_login.py` añade los intentos temporales y los permisos cifrados. **No se aplicó en producción.** La rama de pagos en pausa también propone una migración 0025: no se pueden fusionar ambas sin reconciliar esa numeración.

Antes de habilitar para personas externas: completar configuración privada y permisos de Meta, contrastar requisitos de onboarding, verificar CI y el respaldo, publicar por los controles existentes y realizar una autorización real elegida por la propietaria. No se enviaron mensajes ni publicaciones reales durante estas pruebas.

## Referencias

- [Colección oficial de Meta para Instagram Login](https://www.postman.com/meta/instagram/folder/6raa77c/instagram-api-with-instagram-login).
- [Colección oficial de Meta: páginas y permisos](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api?entity=request-23987686-894be833-d0b6-4877-859e-c61ae6474d64).
- [Colección oficial de WhatsApp: permisos y suscripciones](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api?entity=folder-13382743-148258e1-3240-4eed-8a78-049bb1d1fe25).
- [Configuración de variables de Render](https://render.com/docs/configure-environment-variables).

Las páginas directas de Meta para Facebook Login, Instagram Business Login y Embedded Signup devolvieron HTTP 429 al intentar consultarlas en esta sesión. Se consultaron las colecciones oficiales de Meta disponibles en Postman; los contratos completos y sus posibles pasos adicionales siguen pendientes de contraste en el panel y en una integración real. Los simuladores de las pruebas no sustituyen esa comprobación.
