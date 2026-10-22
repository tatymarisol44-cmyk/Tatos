# Manual: conectar redes sociales con el consultorio

**Para:** dueña del consultorio, administración y persona responsable de sistemas
**Alcance:** Instagram, Facebook, WhatsApp y TikTok
**Estado:** describe el flujo de la versión actual del consultorio; no presupone que una cuenta real ya esté conectada.

## Antes de comenzar: qué permite hoy el sistema

La conexión no se hace todavía con un botón de autorización de Meta o TikTok. Requiere que una persona con permisos de administración de la cuenta social y una persona técnica configuren las credenciales en el despliegue y registren la cuenta en el consultorio.

| Red | Función prevista | Estado y límites de esta versión |
| --- | --- | --- |
| Instagram | Publicar imágenes y recibir mensajes directos de Meta | La cuenta debe ser profesional. Las imágenes para publicar deben ser JPEG y estar disponibles por una URL pública segura cuando se publique. Requiere configurar y verificar la aplicación y sus permisos de Meta. |
| Facebook | Publicar fotos de una Página y recibir mensajes de Messenger | Se admite la publicación de fotos; no se debe asumir que publicar videos está habilitado. Requiere un token de Página con permisos de publicación. |
| WhatsApp | Recibir mensajes, mostrar alertas y permitir que el equipo responda a alertas | Requiere configurar el webhook de Meta y una cuenta de WhatsApp Business Platform. Las respuestas del equipo a alertas dependen de la ventana de servicio de WhatsApp y de las bajas/solicitudes de no contacto. |
| TikTok | Publicación mediante la API | La API tiene restricciones de auditoría; un cliente no auditado solo publica en privado. TikTok todavía no aparece como opción en el formulario de publicaciones del consultorio. |

La documentación del proyecto indica que las conexiones reales y la revisión de las aplicaciones de las plataformas aún deben confirmarse para cada despliegue. Por eso, que una red aparezca en la lista de funciones no significa que ya esté conectada ni que se haya probado con una cuenta real.

## Quién necesita participar

1. **Responsable de la red:** debe tener acceso administrativo a la cuenta profesional de Instagram, a la Página de Facebook o a WhatsApp Business Platform.
2. **Administración del consultorio:** necesita el rol de administración para registrar o desactivar cuentas.
3. **Responsable técnico del despliegue:** configura secretos en el almacén de secretos del entorno, verifica webhooks y prueba la conexión. No debe recibir credenciales por un chat compartido ni guardarlas en un documento.

## Preparación de la cuenta de Instagram

1. Cambia la cuenta a **profesional** si todavía es personal.
2. Si se utilizará la conexión de Meta, vincula Instagram con la Página de Facebook del consultorio y con el portafolio comercial que administra el negocio.
3. La persona administradora de Meta debe crear o seleccionar la aplicación de Meta y habilitar el producto y permisos vigentes para Instagram. Meta puede exigir revisión de la aplicación antes de autorizar cuentas de otras personas.
4. Obtén el identificador de la cuenta profesional de Instagram y el token autorizado para publicar. La persona técnica confirmará el tipo de token y permisos necesarios según el producto de Meta habilitado.
5. Entrega el identificador y el nombre público de la cuenta a administración. Entrega el token directamente a la persona técnica por el canal seguro aprobado; no lo envíes junto con el identificador en un canal público.

La publicación desde el consultorio acepta imágenes JPEG y requiere que Meta pueda descargar el archivo desde una URL pública al momento de publicarlo. No subas historias clínicas, resultados de pruebas, nombres de pacientes ni imágenes identificables como contenido publicitario.

## Preparación de Facebook

1. Usa una **Página** del consultorio, no un perfil personal.
2. La persona que autoriza la integración necesita permiso para crear contenido en esa Página.
3. La aplicación de Meta debe tener los permisos que exija Meta para publicar y consultar la Página. En la implementación documentada se requieren `pages_manage_posts`, `pages_read_engagement` y `pages_show_list`; Meta puede exigir revisión o ajustar el proceso.
4. Obtén el identificador de la Página y un token de acceso de Página con los permisos aprobados.
5. Entrega el identificador y el nombre público a administración y el token a la persona técnica por el canal seguro aprobado.

En la versión actual, limita las pruebas de publicación de Facebook a fotos. La regla de publicación de videos de Página no está verificada en el proyecto.

## Preparación de WhatsApp

1. Configura WhatsApp Business Platform en Meta y selecciona el número de teléfono que usará el consultorio.
2. La persona técnica necesita el identificador de teléfono de WhatsApp (`phone_number_id`) y el token de acceso autorizado para ese número.
3. En la aplicación de Meta, configura el webhook con la URL pública del servicio:

   ```text
   https://<dominio-del-consultorio>/v1/channels/whatsapp
   ```

   El servicio también reconoce `/v1/channels/meta`, pero se recomienda usar `/v1/channels/whatsapp` para que el propósito sea claro.
4. Configura el token de verificación del webhook y el secreto de la aplicación de Meta en los secretos del despliegue. La persona técnica debe confirmar la verificación de Meta y la firma de las notificaciones antes de activar el canal.
5. Suscribe el webhook a los eventos de mensajes que correspondan al producto habilitado y realiza una prueba desde un número autorizado.
6. Registra el `phone_number_id` y el nombre visible del número en el consultorio.

El sistema permite que el equipo responda a una alerta desde el consultorio cuando la política de WhatsApp lo permite. No se debe usar este flujo para enviar información clínica sensible ni para sustituir canales clínicos aprobados. Respeta siempre el consentimiento para recibir mensajes, las solicitudes de baja y el acceso rápido a una persona del equipo.

## Preparación de TikTok

La API contempla la publicación de videos MP4/H.264 y requiere el permiso `video.publish`. Mientras la aplicación de TikTok no haya superado su auditoría, la publicación queda restringida a modo privado. Además, TikTok no es seleccionable actualmente en la pantalla **Marketing → Publicaciones**. No lo registres como canal de publicación operativo hasta que la persona técnica confirme auditoría, permisos, pruebas y soporte de interfaz.

## Registro técnico de una cuenta en el consultorio

Estos pasos los realiza la persona técnica con autorización de administración:

1. Crea un nombre de referencia para el secreto, en mayúsculas y con guiones bajos. Ejemplos: `IG_CONSULTORIO`, `FB_CONSULTORIO` o `WA_CONSULTORIO`.
2. Guarda el token de la plataforma en el almacén de secretos del despliegue bajo el nombre:

   ```text
   SOCIAL_SECRET_<REFERENCIA>
   ```

   Por ejemplo, para la referencia `IG_CONSULTORIO`, el nombre del secreto será `SOCIAL_SECRET_IG_CONSULTORIO`. El valor es el token real. **Nunca escribas el token en el campo `secret_ref`, en la base de datos, en un ticket o en el historial del chat.**
3. Registra la cuenta mediante `POST /v1/social/accounts`, usando la clave administrativa en el encabezado `X-API-Key`. El cuerpo contiene solo los datos públicos y el nombre de referencia del secreto. Ejemplo:

   ```json
   {
     "network": "instagram",
     "external_id": "<ID_DE_LA_CUENTA_PROFESIONAL>",
     "handle": "@<CUENTA_DEL_CONSULTORIO>",
     "secret_ref": "IG_CONSULTORIO"
   }
   ```

   Para Facebook usa `"network": "facebook"` y el identificador de la Página; para WhatsApp usa `"network": "whatsapp"` y el `phone_number_id`. Si la cuenta pertenece a una profesional específica, administración puede añadir `professional_id` con el identificador de esa profesional activa.
4. Consulta `GET /v1/social/accounts` con una clave autorizada. Verifica que la cuenta esté activa y que `secret_configured` sea `true`. Esa respuesta nunca debe mostrar el token.
5. Revisa `GET /v1/social/rules` y confirma que las reglas de la red no indiquen una verificación pendiente. Los cambios de permisos y políticas de las plataformas deben revisarse antes de una activación real.
6. La persona técnica realiza una prueba controlada y confirma en el consultorio el resultado. No uses datos ni cuentas de pacientes para probar publicaciones.

La API permite desactivar una cuenta con `DELETE /v1/social/accounts/{account_id}`. Desactívala si se revoca el acceso, cambia el responsable de la cuenta o se detecta actividad no autorizada; después, revoca también el token desde la plataforma y actualiza o elimina el secreto del despliegue.

## Activar el webhook de Meta

Para recibir mensajes de WhatsApp, Instagram Direct o Messenger, la persona técnica configura estos secretos del servicio:

- `WHATSAPP_VERIFY_TOKEN`: valor secreto elegido para completar la verificación inicial del webhook.
- `META_APP_SECRET`: secreto de la aplicación usado para validar la firma de cada notificación recibida.

Meta debe llamar al endpoint HTTPS indicado arriba. La aplicación responde al desafío de verificación y valida las solicitudes entrantes con `X-Hub-Signature-256`. Si falta alguno de los secretos, el endpoint no queda disponible; no se debe desactivar la validación para “hacer que funcione”.

## Usar publicaciones después de conectar Instagram o Facebook

1. Entra al consultorio con una cuenta autorizada para marketing o con el rol de dueña.
2. Abre **Marketing → Publicaciones**. Si no aparece una cuenta elegible, pide a administración que confirme su registro, que esté activa y que `secret_configured` indique que el secreto está disponible.
3. Crea la publicación, revisa el contenido y el texto, y envíala a aprobación.
4. Una persona autorizada debe aprobar la versión exacta del contenido antes de publicarla o programarla.
5. Comprueba el estado en el calendario. Si una entrega termina como `uncertain` (resultado incierto), verifica la publicación en la plataforma antes de tomar cualquier otra acción: el sistema no la reenvía automáticamente.

La aprobación corresponde al archivo y al texto concretos. Si se necesita un cambio, crea una nueva publicación y vuelve a aprobarla. No uses contenido publicitario que identifique a pacientes, revele su atención o sugiera resultados garantizados.

## Diagnóstico rápido

| Síntoma | Qué revisar |
| --- | --- |
| No aparece Instagram o Facebook en Publicaciones | Que la cuenta esté registrada, activa y pertenezca al consultorio correcto; solo esas dos redes aparecen hoy en el formulario de publicación. |
| `secret_configured` es `false` | El secreto no está configurado con el nombre `SOCIAL_SECRET_<REFERENCIA>` en el entorno donde corre el servicio. La persona técnica debe corregirlo sin pedir que se envíe el token por chat. |
| Meta no verifica el webhook | URL HTTPS correcta, endpoint `/v1/channels/whatsapp`, valor de verificación coincidente y `WHATSAPP_VERIFY_TOKEN` configurado en el despliegue. |
| Meta envía notificaciones y el servicio las rechaza | Confirmar que `META_APP_SECRET` corresponde a la aplicación emisora, que Meta envía `X-Hub-Signature-256` y que el servicio está recibiendo el cuerpo original sin modificaciones intermedias. |
| Instagram rechaza la publicación | Cuenta profesional, permisos aprobados, token vigente, imagen JPEG y URL de medios accesible por Meta. |
| Facebook rechaza la publicación | Confirmar que se usa una Página, que el token pertenece a esa Página y que incluye permisos aprobados para crear contenido. En esta versión, probar solo fotos. |
| TikTok no aparece en el formulario | Es la limitación actual del consultorio; no se puede habilitar desde la pantalla de publicaciones. |

## Seguridad y privacidad

- No compartas contraseñas, tokens, secretos de aplicación ni claves administrativas en este manual, mensajes, capturas o documentos compartidos.
- Da acceso solo a las personas responsables y revoca el acceso cuando cambien sus funciones.
- Usa únicamente secretos del entorno de producción aprobado; no copies credenciales reales a desarrollo o pruebas.
- No publiques ni envíes por redes sociales historias clínicas, resultados de tests, información de citas o datos que permitan identificar a una paciente.
- Confirma las políticas actuales de Meta y TikTok y las obligaciones legales aplicables antes de activar una integración o lanzar una campaña. La aprobación de una aplicación por la plataforma no sustituye la revisión legal del contenido.

## Referencias internas

- [Decisión de arquitectura sobre canales sociales](./adr/0015-social-channels.md)
- [Decisión de arquitectura sobre mensajes y bandeja de entrada](./adr/0023-sealed-inbox.md)
