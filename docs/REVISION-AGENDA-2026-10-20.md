# Inicio y agenda: opciones de reserva — 20 de octubre de 2026

Estado: cambios locales, sin despliegue en Render. Alcance de esta entrega: asistente de reservas por WhatsApp y validación de disponibilidad al confirmar desde WhatsApp y el portal del paciente.

## Comportamiento

- Se eligen hasta tres espacios reales del profesional habitual, dentro de los próximos 14 días. Si hay menos, se ofrecen los existentes; si no hay ninguno y se pide una reserva, se deriva al equipo.
- La preferencia explícita de mañana o tarde prevalece. El reconocimiento es deliberadamente limitado: «mañana» sin más significa un día, no una preferencia matinal; frases negativas o ambiguas no se guardan como preferencias.
- Con «Recordar tus preferencias» habilitado por el paciente y permitido por la configuración, se conserva la preferencia explícita mediante la memoria existente y su caducidad. Si no hay una preferencia guardada vigente, se consultan hasta 12 citas completadas de los últimos 180 días. Las más recientes pesan más; hacen falta dos citas y una mayoría ponderada de dos tercios.
- Las cancelaciones, ausencias y citas futuras no demuestran una preferencia. Solo se usan fechas y horas del propio paciente y consultorio. No se envían las citas históricas al modelo.
- Sin consentimiento de memoria, se puede atender la preferencia del mensaje actual, pero no se guarda ni se utiliza el historial para inferirla. La preferencia del ofrecimiento permanece únicamente en las opciones temporales existentes, con vigencia de dos horas.
- Mañana significa antes de las 12:00 en la zona del consultorio; tarde incluye los horarios posteriores. La preferencia ordena opciones, no garantiza disponibilidad ni constituye una restricción. Si no bastan los horarios preferidos, se completan con otros espacios disponibles.
- Los horarios y números de opción del mensaje se generan desde los datos del servidor. El modelo no puede sustituirlos ni confirmar una reserva.
- Al elegir, se comprueban de nuevo la hora, la duración, los horarios de atención y los solapamientos. PostgreSQL serializa las reservas y los cambios de horario mediante la fila del profesional. Si la opción dejó de ser válida, se ofrecen alternativas.
- Se corrigió además el rechazo de espacios válidos posteriores a los primeros 200 resultados: se comprueba el día elegido directamente.

## Verificación

Antes del cambio fallaron siete de las ocho regresiones iniciales. Después, la batería relacionada de agenda, CRM, mensajes, memoria, consentimiento, esquema y arquitectura aprobó 213 pruebas. Se agregó y verificó por separado una regresión de revocación de consentimiento.

Una prueba adicional con dos instancias y PostgreSQL sintético local confirma que dos reservas simultáneas para el mismo horario producen una sola cita. La instancia temporal se cerró al terminar. También se simuló un cambio de horario entre la comprobación inicial y la inserción para verificar el control dentro de la transacción.

No se realizaron llamadas a proveedores reales ni se enviaron mensajes a pacientes. Las pruebas utilizan FakeLLM, HTTP simulado y datos sintéticos. Ruff, formato y Mypy se ejecutan sobre la candidata; no se atribuye a esta entrega la cobertura histórica de otras revisiones.

## Pendiente en la revisión de agenda

El fallo previamente identificado de anticipación máxima por nivel del paciente sigue pendiente: esta entrega no redefine esa política. Tampoco incorpora estas recomendaciones a la pantalla de la agenda del personal ni añade chat de agentes a `/app`. Las sugerencias de tres opciones pertenecen al flujo de WhatsApp existente. Debe comprobarse la configuración real del canal y del modelo antes de publicar.


## Seguimiento: reserva desde el portal del paciente

El reporte del propietario indica que no puede reservar y que falta acceso mediante correo y contraseña. Son dos asuntos distintos: `/mi/` utiliza actualmente un enlace personal; el correo de la ficha es un dato de contacto y el inicio de sesión con contraseña existente corresponde al personal. No se ha implementado una cuenta de paciente con contraseña en esta entrega. Su incorporación requiere vinculación con la ficha, activación, recuperación, sesiones y pruebas de aislamiento entre pacientes y consultorios.

Se consultaron únicamente las páginas públicas `/mi/` y `/static/mi.js` de Render: respondieron HTTP 200 y coincidían byte a byte con los archivos locales antes de esta corrección. No se inspeccionaron fichas reales ni se crearon citas en producción. La reserva básica con enlace y disponibilidad configurada pasó en Chromium local. Esto no identifica por sí solo la causa del incidente del propietario; falta conocer en qué paso se detiene su recorrido.

Se reprodujeron dos fallos con pruebas que fallaron antes de corregirlos:

- Un error al cargar los horarios sustituía toda la pantalla por un error sin recuperación. Ahora se conserva la pantalla y se ofrece «Reintentar horarios».
- Una consulta lenta de un profesional podía sobrescribir los horarios del profesional seleccionado después. Ahora cada respuesta queda vinculada al profesional, la consulta, la pantalla y la sesión que la originaron; la reserva usa ese mismo profesional.

También se bloquean los controles mientras se envía una reserva para evitar dobles envíos; una respuesta posterior no refresca una pantalla que el paciente ya abandonó. Si vence el enlace, se invalidan las pantallas pendientes antes de mostrar el mensaje de acceso. El mensaje de éxito confirma únicamente la reserva: se retiró la promesa incondicional de recordatorio, que depende de la configuración del canal.

Validación posterior: **31 pruebas aprobadas, cero fallos y cero omitidas**, incluyendo nueve pruebas del portal, 18 de opciones de reserva y cuatro de agenda. Las pruebas de navegador recorren reserva móvil, recuperación tras fallo de horarios, respuestas fuera de orden, caducidad del enlace y bloqueo durante el envío. Ruff y `git diff --check` aprobados. Un aviso de deprecación del fixture de asyncio continúa presente.

**Estado: cambios locales; todavía no desplegados en Render.** La configuración real de horarios y el recorrido específico reportado siguen pendientes de comprobar. No se presenta este resultado como resolución confirmada del incidente en producción.
