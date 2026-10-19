# Auditoría del consultorio — 19 de octubre de 2026

## Alcance y resultado

Revisión del código local de `agency-orchestrator`, con pruebas sintéticas:
acceso con llave y correo/contraseña/TOTP, navegación, permisos, agenda,
pacientes, historia, documentos, tests, mensajes, campañas, publicaciones y talleres.
No se leyeron ni modificaron pacientes o credenciales de producción.

Las comprobaciones cubren casos concretos; no equivalen a garantizar la ausencia
absoluta de errores ni a una certificación de seguridad o cumplimiento sanitario.

## Fallos corregidos

| Caso | Consecuencia anterior | Corrección |
|---|---|---|
| Atrás durante configuración de cuenta | Cambiaba el hash pero quedaba la contraseña en pantalla | Salida explícita al acceso y botones de regreso; incluido en `dfff715` |
| Ajustes de marketing | La consulta de guardias devolvía 403 y bloqueaba toda la pantalla, incluida Tu cuenta | Cargar guardias solo con permisos de atención |
| Pantalla inicial por rol | Marketing y privacidad caían en Hoy sin permisos; Marketing abría el informe de la dueña | Inicio accesible según el rol; pestaña inicial adecuada; ocultar navegación de atención cuando no corresponde |
| Creación de talleres | Roles de lectura veían un formulario que no podían guardar | Mostrar creación solo con un rol organizador |
| Caché al cambiar de cuenta | Pacientes del usuario anterior podían aparecer en una nueva cita, incluso entre consultorios | Limpiar pacientes, nombres, filtros, semana, indicadores y contenido al salir o abrir una invitación |
| Respuestas anteriores a cerrar sesión | Una respuesta tardía podía dibujar datos o cerrar una cuenta posterior | Invalidar pantallas y respuestas de sesiones anteriores |
| Recordarme desmarcado | Una llave persistente anterior podía seguir en localStorage | Borrar la persistencia anterior al elegir una sesión temporal |
| Búsquedas simultáneas | Un resultado o error antiguo sustituía la búsqueda actual; algunos errores se ocultaban | Verificar orden y consulta; mostrar errores actuales |
| Enlaces malformados | decodeURIComponent lanzaba una excepción y dejaba Cargando | Capturar el error y ofrecer un enlace para volver |
| Configuración o acceso cancelados | Una respuesta lenta podía reabrir el autenticador o entrar tras cancelar | Invalidar intentos anteriores y limpiar secretos temporales |
| Doble envío del acceso | Podía consumir enlaces de un solo uso o competir con otro intento | Un envío pendiente por formulario; reactivación al terminar o cancelar |
| Notas sin guardar | Cualquier POST/PUT/DELETE borraba el indicador de cambios, aunque guardara otra cosa | Conservar el indicador; limpiar solo el formulario de test que se guardó |
| Ventana de nueva cita tardía | Podía aparecer encima de Ajustes después de navegar; errores de red quedaban sin gestionar | Verificar la pantalla de origen y mostrar errores controlados |

Implementación: [consultorio.js](../src/orchestrator/web/static/consultorio.js).
Pruebas de navegador: [test_consultorio_e2e.py](../tests/test_consultorio_e2e.py).

## Evidencia

- Suite general ejecutada al iniciar esta auditoría: **1035 aprobadas, 33 omitidas**;
  cobertura Python **95,16 %** (mínimo exigido: 80 %).
- Suite ampliada del consultorio: **28 pruebas Chromium**: acceso, perfil, agenda,
  historia, documentos, tests, guardias, mensajes, campañas, publicaciones,
  talleres, ambos pasos de configuración, cambio de consultorio, cancelaciones,
  búsquedas fuera de orden y los seis roles.
- Ruff: análisis y formato correctos.
- Mypy: sin incidencias en 124 archivos de código.
- Sintaxis JavaScript y `git diff --check`: correctos.
- Las advertencias de la suite general corresponden a deprecaciones de pytest-asyncio
  y del adaptador datetime de SQLite; no fueron fallos de prueba.

La suite general y la ampliada son ejecuciones diferentes; sus totales no deben sumarse.
La cobertura indicada corresponde a Python, no al porcentaje de funcionalidades probadas
ni a cobertura JavaScript.

## Límites y comprobaciones pendientes

Las 33 omisiones se desglosan en:

- 1 restauración real: requiere PostgreSQL y pg_dump/pg_restore.
- 9 integraciones PostgreSQL: requieren TEST_POSTGRES_URL.
- 1 bloqueo advisory: requiere PostgreSQL.
- 4 contratos A2A externos: requieren TEST_A2A_AGENT_URL.
- 18 comprobaciones de scripts: requieren Bash y/o jq disponibles para las pruebas.

La suite local usa proveedores y datos sintéticos. Falta comprobar con la
configuración real de producción:

- Render sirviendo exactamente la nueva imagen, y comprobación posterior del acceso.
- Conexión al LLM real con credenciales y modelos configurados por la administración.
- Publicación y recepción reales en Meta, WhatsApp y demás servicios externos.
- Copia y restauración de seguridad en el entorno operativo.

Al revisar GitHub no estaban configurados DEPLOY_TARGET ni las variables/secretos de
Render requeridos por el flujo de publicación. El archivo local de despliegue tenía
RENDER_API_KEY vacío. Esto impide completar un despliegue automático desde aquí.
No se incorporaron secretos al código ni se sustituyeron por valores de ejemplo.
