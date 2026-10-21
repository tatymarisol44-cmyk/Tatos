# Acceso de pacientes y disponibilidad — 21 de octubre de 2026

Estado: implementación local, sin publicación ni migración en Render. Continúa la revisión de agenda y portal del paciente. Los cambios previos del repositorio se conservaron.

## Recorrido del paciente

1. El consultorio crea la ficha y entrega su enlace personal desde «Acceso a su app».
2. El paciente abre el enlace y entra en «Mi correo y contraseña». Escribe correo, contraseña de al menos diez caracteres y confirmación.
3. La cuenta queda vinculada a esa ficha, nunca a una ficha elegida por correo. Al activar, se invalidan los enlaces de paciente anteriores y las sesiones previas de esa ficha.
4. Puede entrar en `/mi/` con correo y contraseña, consultar su información y reservar en horarios disponibles. La sesión dura doce horas.
5. «Salir de este celular» elimina la sesión del servidor y el acceso local. Si olvida la contraseña o cambia de correo, el consultorio verifica su identidad y crea un enlace de recuperación desde la ficha. Dura tres días; solo el último enlace de recuperación autoriza sustituir credenciales de una cuenta existente. Al usarlo, se cierran las sesiones anteriores.

El correo funciona como identificador de acceso. No se envía ni verifica un correo automáticamente. La primera identificación se basa en el enlace privado entregado por el consultorio. El correo se guarda también como contacto de la ficha. No se alteran consentimientos. Esta versión admite una cuenta por correo dentro del despliegue, incluso si este contiene varios consultorios; no permite utilizar ese mismo correo en dos fichas distintas. Esta restricción es explícita y no se presenta como soporte de múltiples cuentas por correo.

## Implementación y protección

- `patient_login.py` contiene cuentas y sesiones propias de pacientes; comparte las funciones existentes de hash scrypt y validación de contraseñas del módulo de acceso del personal. No utiliza su flujo TOTP ni concede sus roles.
- Tablas `patient_logins` y `patient_sessions`, con migración Alembic `0025`. La migración no se ha ejecutado en producción. Las contraseñas se almacenan con salt y hash scrypt; las sesiones aleatorias se almacenan solo como SHA-256.
- Autenticación `ps_` devuelve siempre un principal de paciente, sin permisos de personal. El tenant y la ficha se obtienen de la cuenta y la sesión, no de campos enviados por el paciente.
- Cinco contraseñas incorrectas bloquean quince minutos. Los endpoints públicos y de activación/recuperación usan el limitador de autenticación existente. Los errores de ingreso no distinguen correo ausente de contraseña incorrecta.
- Activación y recuperación actualizan credenciales, revocaciones y auditoría en una transacción. La fila del paciente serializa activaciones; las credenciales se bloquean durante ingreso/recuperación. Los eventos no contienen correo, contraseña ni token.
- La supresión de contacto en CRM elimina estas cuentas y sesiones; las fichas restringidas no pueden ingresar. El mantenimiento elimina sesiones caducadas.
- El navegador descarta respuestas de una sesión anterior, incluso errores 401 que llegan después de activar la cuenta. Los formularios tienen etiquetas y atributos de autocompletado, botón para volver y errores visibles.

## Horarios vacíos

Se añadió un diagnóstico que distingue falta de horarios de atención configurados de ausencia de espacios libres. En el primer caso se informa al paciente y puede seleccionar otro profesional o solicitar al consultorio que configure su atención. «Actualizar horarios» consulta de nuevo después de configurarlos.

Se reprodujo localmente: profesional sin horas → aviso; publicación de horas → actualización → aparecen espacios. No se inventan horarios ni se modifica la disponibilidad de producción. La causa concreta del reporte en Render sigue pendiente de contrastar con su configuración real.

## Validación

- Batería relacionada: **124 aprobadas, siete omitidas, cero fallos**. Incluye cuentas, portal, migraciones, esquema, acceso del personal, privacidad, agenda, recomendaciones y recorridos de `/app`. Las omisiones son seis variantes de concurrencia del acceso del personal que requieren PostgreSQL y el bloqueo exclusivo de PostgreSQL en SQLite.
- PostgreSQL sintético local, con dos pools independientes: **nueve aprobadas y una omitida**. Incluye dos regresiones nuevas (doble activación y recuperación concurrente con ingreso mediante contraseña antigua) y migraciones SQLite/PostgreSQL. Se omite únicamente el bloqueo PostgreSQL en SQLite. La instancia temporal se cerró al finalizar.
- Tras añadir el control de respuestas de sesiones anteriores: **19 pruebas de cuentas y portal aprobadas, cero omisiones**. Incluyen activar, salir, entrar y reservar desde Chromium móvil, así como la respuesta 401 atrasada de un enlace invalidado. Estas ejecuciones se solapan; no se suman como pruebas únicas.
- Ruff y Mypy: aprobados; 131 archivos fuente comprobados por Mypy. Sintaxis de ambos JavaScript y `git diff --check`: aprobados. Gitleaks sobre `src`: sin hallazgos. Persisten los avisos previos de deprecación del fixture asyncio y el adaptador datetime de SQLite.
- Instancia local interactiva con datos sintéticos, FakeLLM y base de datos en memoria: reserva HTTP 201, una cita después de recargar y cero errores de JavaScript. El paciente de prueba manual se mantiene separado del usado por la comprobación automática.

## Operación pendiente

Antes de publicar, revisar y ejecutar CI sobre esta candidata, aplicar la migración mediante el procedimiento del proyecto y comprobar el recorrido sobre la revisión realmente desplegada. No se efectuaron reservas reales, envíos a pacientes ni llamadas a modelos. La instancia interactiva es temporal: al detenerla se pierden sus cuentas y citas ficticias.
