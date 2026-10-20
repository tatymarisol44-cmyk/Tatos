# Auditoría SaaS — 19 de octubre de 2026

## Resultado y alcance

La verificación local final terminó con **1120 pruebas aprobadas, 5 omitidas, cero fallos y cero errores**. Cobertura Python **95.31%**, sobre 11418 sentencias; mínimo conservado: 80%. Esto no mide cobertura funcional o JavaScript ni acredita producción, eficacia clínica o funcionamiento de proveedores reales.

Se corrigieron el contador concurrente de contraseñas y el aislamiento de los simuladores Bash en Windows. Se ejecutaron PostgreSQL 17.11 real, restauración aislada, Chromium, instalación independiente, auditoría de dependencias y carga sintética. **La versión de Render sigue sin identificarse por deploy/digest y su asset observado difiere de main. No se realizó publicación, push o despliegue.**

Se leyó `docs/AUDITORIA-CONSULTORIO-2026-10-19.md`. No se encontró AGENTS.md del SaaS ni de sus antecesores. Base local y remota observada: `3d290bca94a7bcd69a01fb914db7c41be382a8d7`. Este informe se refiere a `agency-orchestrator` / `tatymarisol44-cmyk/Tatos`, no a la aplicación MÍA de la carpeta vecina.

Al comenzar había diez archivos modificados, dos auxiliares sin seguimiento y entregables ajenos. Se conservaron copias y hashes. Durante la revisión se observaron modificaciones adicionales de dependencias, renovación de leases, CI y un benchmark; se conservaron y no se atribuyen a esta ejecución. Nueve de los diez archivos originalmente modificados permanecen idénticos a la fotografía inicial; el CI cambió por separado. Se preguntó si hay otra ventana trabajando. La suite final incluyó ese estado completo, cuya [huella de 306 archivos](evidence/saas-audit-2026-10-19/final-start-manifest.json) no cambió durante la ejecución. El resultado no significa que todos esos cambios pertenezcan a un único commit.

Cambios de esta ejecución: `src/orchestrator/staff_login.py`, `tests/test_audit_auth_concurrency.py`, `tests/test_single_node.py`, `tests/test_render_profile.py` y este informe/evidencia. No se tocaron entregables ajenos o archivos de credenciales.

El informe y los cambios propios se versionan en la rama local `audit/saas-verificacion-2026-10-19`, usando un índice Git temporal separado: no se mueve main, altera el staging del propietario o hace push. Esa rama contiene solo los cambios propios; las correcciones de otra procedencia permanecen en el working tree. Por ello la batería completa documentada corresponde a la candidata combinada indicada por los hashes, no al checkout aislado de esa rama parcial.

## Inventario y matriz de cobertura

[Inventario estático inicial](evidence/saas-audit-2026-10-19/inventory.json): 124 archivos Python de ejecución, 24 migraciones, 171 decoradores de rutas antes de expandir prefijos. [Inventario OpenAPI efectivo](evidence/saas-audit-2026-10-19/openapi-inventory.json): **157 operaciones**. Su existencia no demuestra corrección. `test_tenant_isolation_matrix.py` exige incorporar cada operación OpenAPI y comprueba lectura/escritura adversaria con identificadores de otra organización, además de pacientes de la misma organización.

| Dimensión solicitada | Evidencia obtenida | Límites pendientes |
|---|---|---|
| 1. Funcionalidad y UX | Batería completa y Chromium: seis roles, cambios de cuenta, historial/direct links, respuestas tardías, formularios, permisos y controles axe | Los casos cubiertos no equivalen a todas las combinaciones posibles de navegador, dispositivo o latencia |
| 2. Autenticación/aislamiento | Contraseña/TOTP, repetición de códigos, expiración/revocación, roles, matriz OpenAPI, dos tenants, sesiones y 12 casos nuevos de contraseña concurrente | Sin cuentas reales de Render/IdP; los límites Redis se prueban con simulador, no con un Redis operativo |
| 3. Integridad/concurrencia | PostgreSQL real: reservas competidoras, outbox doble, auditoría encadenada, revisión única; migración con datos y restauración | Proveedores HTTP simulados; no certifica entrega externa exactamente una vez ante todos los fallos |
| 4. Reproducibilidad | Archivo limpio del commit, uv.lock congelado, instalación separada no editable, wheel/sdist, 24 migraciones y HTML/JS empaquetados | Caché uv local disponible; no prueba descarga desde cero o igualdad binaria Docker; submódulo del catálogo no forma parte de git archive |
| 5. Escala/fiabilidad | Seis ráfagas locales con PostgreSQL, una/dos APIs, latencia, errores, CPU, RSS y conexiones medidos | Sin prueba sostenida, Redis/Qdrant distribuidos, recursos equivalentes a Render o capacidad real de modelos |
| 6. CI/CD | API GitHub: reglas activas, entornos, jobs; OCI: digest y revisión de imagen base; resumen de despliegue existente validado | CI remoto cubre el commit base, no la candidata local; Render pendiente y firma OCI no verificada localmente |
| 7. Seguridad/operación | Negativos, permisos, cabeceras, rate limits, body limits, restauración real aislada; escáner runtime y todos los grupos sin avisos conocidos | Sin escáner Docker local, logs/alertas operativos o recuperación de una copia de producción |
| 8. Agentes/LLM/externos | FakeLLM, contratos HTTP simulados, entradas malformadas, fallbacks, circuit breaker y gasto acumulado | Cuatro contratos Java omitidos localmente; LLM/Meta/WhatsApp reales y límites del proveedor no demostrados |

Correspondencia de módulos con suites: [matriz completa por archivo](evidence/saas-audit-2026-10-19/coverage-matrix.json). Estos grupos se solapan; no sumar sus totales ni interpretar coincidencia de nombres como cobertura exhaustiva de cada rama.

| Módulo | Archivos de prueba asociados | Casos aprobados | Omitidos |
|---|---:|---:|---:|
| Acceso, equipo y roles | 4 | 70 | 0 |
| Consultorio, historial, navegación y accesibilidad | 3 | 36 | 0 |
| Agenda y pacientes | 4 | 28 | 0 |
| Historia, documentos, recetas e instrumentos | 5 | 84 | 0 |
| Bandeja, guardias y derechos | 4 | 61 | 0 |
| Campañas, recordatorios y canales | 4 | 61 | 0 |
| Publicaciones, creatividades y talleres | 5 | 73 | 0 |
| Catálogo, agentes, equipos y evaluación | 8 | 112 | 4 |
| Conocimiento, memoria, consentimientos y evidencia | 2 | 47 | 0 |
| Persistencia, límites, recuperación y despliegue | 15 | 129 | 0 |

## Hallazgos con evidencia y corrección

### SAAS-01 — Alto — Se perdían fallos simultáneos de contraseña — Corregido localmente

**Ubicación:** `StaffLogins.begin`, `src/orchestrator/staff_login.py`. Cada petición leía el contador antes del cálculo de contraseña y escribía una copia incrementada en Python. Dos, cinco o siete intentos simultáneos acababan registrando solo uno; cinco/siete no activaban el bloqueo previsto. Impacto: reducción del control de fuerza bruta por cuenta.

**Reproducción:** cuenta sintética enrolada, SQLite temporal en archivo y barrera tras la lectura; la versión inicial de `tests/test_audit_auth_concurrency.py` falló en sus tres casos. **Corrección:** una sentencia SQL incrementa el contador y establece el bloqueo de quince minutos al quinto fallo. Una condición impide modificar un bloqueo activo creado por otra petición; la auditoría queda en la misma transacción.

Se amplió la regresión a PostgreSQL real y bloqueos caducados. El refinamiento detectó dos fallos adicionales (SQLite/PostgreSQL): el timestamp antiguo permanecía y podía marcar incorrectamente el evento de auditoría como bloqueado. La sentencia ahora limpia ese timestamp cuando no establece un bloqueo nuevo. Después: **12 casos concurrentes aprobados** (2/5/7 intentos × SQLite/PostgreSQL × cuenta nueva/bloqueo caducado); junto al acceso existente, **26 aprobadas**. [Resultados antes/después](evidence/saas-audit-2026-10-19/test-results.json).

### SAAS-02 — Alto — El despliegue verde no actualizó producción — Pendiente operativo

**Ubicación:** `.github/workflows/deploy.yml`, configuración GitHub/Render y asset público. [Lectura inicial](evidence/saas-audit-2026-10-19/initial-remote-observation.json): `/app`, `/healthz`, `/readyz` y JS respondieron 200; `consultorio.js` publicado tiene SHA256 `724a1a65290e6c36026fb97c9b621bcdff78fbaa6efba1fead3a6678fb5d4f5a`, mientras main/local tienen `ed8cc3a8c8735e8fa6d52d82b40f761d9309ad909a881fadc3c5fce08ed7c1fa`. No apareció X-App-Revision. Impacto: se pueden dar por entregadas correcciones que aún no se sirven.

[Deploy 39298528515](https://github.com/tatymarisol44-cmyk/Tatos/actions/runs/39298528515) terminó success con `production-render`, `production-vm` y `production` **skipped**. Las variables/secretos del repositorio y de production/staging devolvieron listas vacías. La regla activa `main-protegida` exige PR/checks e impide borrados y force push: el 404 de protección clásica no significa rama desprotegida. Production tiene revisor y política de ramas; permite autoaprobación. [Evidencia administrativa](evidence/saas-audit-2026-10-19/remote-followup.json).

Las correcciones previas de X-App-Revision, Docker BUILD_REVISION y `deploy_summary.py` se conservaron; sus regresiones pasan. No se sustituyeron credenciales por ejemplos ni se activó DEPLOY_TARGET a ciegas. Falta conectar Render, identificar servicio/deploy y autorizar una publicación concreta tras CI de la candidata.

### SAAS-03 — Medio — Los simuladores de despliegue podían escapar a la red en Windows — Corregido localmente

**Ubicación:** helpers `deploy` en `tests/test_single_node.py` y `tests/test_render_profile.py`. Git Bash no resolvía primero el directorio simulado que Python añadía al PATH de Windows. Al habilitar Bash/jq aparecieron **7 fallos y 46 aprobadas**: se usó curl real, incluido un POST a Render con credencial ficticia rechazado con 401, y probes fallidos al loopback. No se presenta esa petición como simulación, acceso válido o despliegue.

**Corrección:** establecer PATH dentro de Bash con ruta POSIX y argumentos posicionales; comprobar que `command -v curl` y `docker` apuntan exactamente al simulador antes de ejecutar el script. Convertir también rutas de los archivos de estado. Después: **53 aprobadas**, con pruebas de rechazo de imagen, firma, migración, readiness, timeout y rollback conservadas. No se modificó la lógica de despliegue productivo ni se añadieron omisiones.

## Límites abiertos de diseño y operación

- **Gasto, medio:** `Spend.check()` compara gasto ya contabilizado y `charge()` cobra al finalizar. La suma es atómica, pero no hay reserva de coste para llamadas en vuelo; llamadas sin precio se cuentan separadamente. Es un control posterior del consumo, no una garantía monetaria estricta del proveedor. No se ha certificado el gasto real ni alterado SRS/umbrales para declarar cumplimiento. Una garantía estricta requiere presupuesto/reserva compatible con todos los modelos/rutas y límites de facturación del proveedor.
- **Imágenes, medio:** CI fija bases/acciones y genera SBOM/provenance/firma, pero el escaneo de imagen configurado filtra CRITICAL y omite unfixed; la construcción multiarch posterior incorpora arm64, mientras el escaneo local de CI cubre amd64. apt instala ffmpeg sin snapshot/versionado exacto. No se afirma cero vulnerabilidades de SO o reconstrucción byte a byte. Docker no estaba disponible localmente y no se cambió este control concurrente sin escanear sus efectos.
- **Evidencia externa, alto antes de aceptación:** no están demostrados versión real de Render, cuenta/IdP, conexión LLM, publicación/recepción Meta/WhatsApp, alertas ni restauración de backups productivos. Las lecturas públicas exitosas no sustituyen esos recorridos.

## Ejecuciones y resultados

| Ejecución | Resultado | Alcance |
|---|---|---|
| Baseline de esta revisión | 1071 aprobadas / 33 omitidas | Estado observado al comienzo; no sumar con la final |
| Contraseña antes | 3 fallos esperados | Reproduce pérdida de contador en SQLite |
| Bloqueo caducado antes del refinamiento | 2 fallos / 4 aprobadas / 6 deseleccionadas | SQLite y PostgreSQL; no se cuenta como suite verde |
| Contraseña y acceso finales | 26 aprobadas | 12 regresiones y 14 casos existentes |
| PostgreSQL y restauración | 14 aprobadas | DB real aislada, no copia productiva |
| Simuladores Bash después | 53 aprobadas | Docker/kubectl/Render simulados |
| Suite final completa | **1120 aprobadas / 5 omitidas** | Cero fallos/errores; cobertura 95.31% |
| Ruff / formato / Mypy / JS / diff | PASS | Mypy 124 fuentes; sintaxis JS, sin afirmar cobertura JS |
| pip-audit 2.10.1 runtime | 138 dependencias, cero avisos conocidos, exit 0 | Export congelado completo, sin paquetes omitidos |
| pip-audit todos los grupos | 187 dependencias, cero avisos conocidos, exit 0 | Incluye desarrollo/carga; no certifica ausencia de vulnerabilidades desconocidas |
| pip-audit unión del lock | 189 paquetes PyPI, cero avisos conocidos, exit 0 | Incluye uvloop/Linux excluido en Windows; no instala paquetes y no omite avisos |

Las omisiones reales de integración A2A en `tests/test_remote_live.py` requieren `TEST_A2A_AGENT_URL` y un agente Java 21 levantado. Se encontró Java 8 local; el job Java/A2A remoto del commit base aparece success, sin convertirlo en prueba local de los cambios. El caso parametrizado de advisory lock sobre SQLite se omite porque no aplica a ese motor; su variante PostgreSQL sí se ejecuta. No se cuentan omisiones como aprobadas. Se eliminó la carencia local de PostgreSQL/Bash/jq sin quitar sus pruebas. Las advertencias son deprecaciones de pytest-asyncio/SQLite y no se cuentan como fallos ni se silencian.

| Caso omitido | Motivo registrado por pytest |
|---|---|
| tests.test_migrations.test_replicas_migrating_together_do_not_collide[sqlite] | the advisory lock is a Postgres feature; SQLite is single-process dev |
| tests.test_remote_live.test_discovers_the_java_agent | set TEST_A2A_AGENT_URL to run |
| tests.test_remote_live.test_jvm_questions_are_routed_and_answered_by_java | set TEST_A2A_AGENT_URL to run |
| tests.test_remote_live.test_team_mode_mixes_java_and_local_specialists | set TEST_A2A_AGENT_URL to run |
| tests.test_remote_live.test_protocol_errors_surface_as_remote_errors | set TEST_A2A_AGENT_URL to run |

Los primeros exports de dependencias resuelven los marcadores de esta plataforma Windows. Se añadió una consulta separada de los 189 paquetes PyPI del lock, sin marcadores de plataforma, para incluir uvloop y las dependencias exclusivas de Linux. Ninguna consulta encontró avisos conocidos o paquetes omitidos. No equivale a escaneo de imagen o de paquetes del sistema operativo. La ejecución local usa Python 3.12.14; Python 3.13 de la candidata queda pendiente del CI correspondiente.

La primera batería final se interrumpió deliberadamente para ampliar y corregir el caso de bloqueo caducado; no se contabiliza como aprobada. Solo se usa la segunda ejecución completa. [Detalle normalizado](evidence/saas-audit-2026-10-19/test-results.json), [comandos y procedimientos](evidence/saas-audit-2026-10-19/commands.json). No se suman ejecuciones solapadas.

## Reproducibilidad y artefactos

Se extrajo un git archive limpio del HEAD, sin archivos privados/cambios locales, y se ejecutaron uv 0.12.24 + Python 3.12.14: `uv sync --frozen --offline` y `uv build --offline`, ambos exit 0. Esto usa la caché de paquetes disponible; el submódulo del catálogo se debe inicializar aparte al clonar. Para la candidata actual se construyó e instaló otro entorno **no editable**, cuyo import procede de site-packages. Siete probes verificaron HTML/JS empaquetados, salud/readiness, rechazo sin auth, catálogo autenticado y A2A no-store. Head de migración: 0024; wheel incluye 24 migraciones. Se reconstruyó después del último ajuste y se comprobó que el código de acceso empaquetado coincide byte a byte con la fuente final. [Base limpia](evidence/saas-audit-2026-10-19/clean-repro.json), [candidata](evidence/saas-audit-2026-10-19/candidate-repro.json), [wheel final](evidence/saas-audit-2026-10-19/candidate-final.json).

PostgreSQL portátil 17.11 se obtuvo del distribuidor enlazado por postgresql.org; no se instaló servicio de Windows. Su descarga está registrada por hash, sin afirmar firma/checksum independiente del editor. jq 1.8.2 se contrastó con el digest del release oficial. Las APIs y bases de prueba solo escucharon en 127.0.0.1 y usaron datos sintéticos. [Procedencia de herramientas](evidence/saas-audit-2026-10-19/temporary-tool-sources.json).

## Carga aislada y límites observados

Ráfagas de 200 solicitudes, sin reintentos: 60% chat, 20% insights, 10% agenda, 10% catálogo; 30 pacientes sintéticos. Una y dos APIs con PostgreSQL 17.11 compartido, FakeLLM 80 ms por llamada y límite de ocho chats en vuelo por API. Índice y rate limiter en memoria (presupuesto aumentado para aislar backpressure), sin workers de entrega. CPU es suma de núcleos (puede superar 100% con dos procesos); RSS suma los procesos API reales. Recursos PostgreSQL y del cliente están excluidos.

| APIs | Concurrencia | 200 | 503 | p95 ms | p99 ms | Pico RSS MiB | CPU % de núcleo | Pico conexiones DB |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4 | 200 | 0 | 232.39 | 268.41 | 180.46 | 98.37 | 2 |
| 1 | 16 | 88 | 112 | 139.95 | 973.75 | 182.77 | 86.07 | 5 |
| 1 | 40 | 88 | 112 | 581.23 | 1072.79 | 184.54 | 94.35 | 6 |
| 2 | 4 | 200 | 0 | 156.25 | 179.47 | 363.44 | 156.58 | 8 |
| 2 | 16 | 188 | 12 | 514.55 | 542.92 | 365.52 | 194.11 | 9 |
| 2 | 40 | 96 | 104 | 784.55 | 814.23 | 366.27 | 185.11 | 12 |

Los 503 se reportan como rechazos, no como solicitudes útiles ni como éxito de capacidad. No hubo 500 o errores de transporte en estas ráfagas; agenda/catálogo/insights respondieron 200. El tenant alternativo no vio los pacientes al terminar y los procesos API de carga se cerraron. No hay periodo sostenido, intervalos de confianza o prueba de Redis/Qdrant/LLM distribuidos: estos números no acreditan usuarios concurrentes o capacidad de Render. Se descartó una medición preliminar de recursos porque observaba el launcher de la venv en vez del proceso API; la tabla corresponde a PIDs reales. [Datos y desglose por ruta](evidence/saas-audit-2026-10-19/load-results.json).

## Código local, commit remoto, imagen y servidor

| Nivel | Evidencia | Estado |
|---|---|---|
| Código local | Base 3d290bc + cambios preservados y correcciones propias, hashes de 306 archivos | Suite final sobre este estado; no equivale a HEAD limpio |
| Commit remoto main | `3d290bca94a7bcd69a01fb914db7c41be382a8d7` y [CI 39296543783](https://github.com/tatymarisol44-cmyk/Tatos/actions/runs/39296543783) success | No incluye correcciones locales sin publicar |
| Imagen construida base | GHCR `sha-3d290bc`, índice `sha256:e0d79a1e1b27a7b9f518f6033db6a44530e78399e74f501969701e1dc5df29ee`, plataformas amd64/arm64 y etiqueta OCI con el SHA completo | Metadatos leídos directamente; firma no verificada aquí, candidata nueva sin imagen construida |
| Versión servida | Asset con hash distinto, X-App-Revision ausente, endpoints públicos 200 | Deploy/commit/digest de Render pendientes de lectura administrativa |

No se solicitó ni consumió aprobación de producción. Para publicar: revisar los cambios de ambas procedencias, CI/build/scan/firma de la candidata, conectar Render, identificar servicio y rollback disponible, completar variables/secretos mediante su almacén seguro y presentar el digest/configuración concretos para aprobación. Luego verificar deploy real, revisión servida y recorridos sintéticos autorizados. No se piden tokens o códigos por chat. Ninguna prueba simulada se presenta como interacción clínica o entrega real a un proveedor.


## Verificaci?n independiente y secretos: 20 de octubre

La ejecuci?n independiente termin? con 1.082 pruebas aprobadas, 39 omitidas, cero fallos y cobertura 95,21 % en 440,18 segundos. Sin habilitar PostgreSQL/Bash en esa ejecuci?n; no se suman resultados de bater?as solapadas. Las huellas de los 306 archivos de la evidencia anterior siguen coincidiendo. [Resumen](evidence/saas-audit-2026-10-19/saas-audit-final-summary.json).

Gitleaks 8.30.1 examin? 215 commits y una copia de 429 archivos actuales. Se verificaron cuatro SHA-256 del manifiesto y una contrase?a ficticia del test de concurrencia. El usuario autoriz? expresamente el 20 de octubre excepciones para esos valores y rutas. Se limitaron a generic-api-key; los dem?s detectores siguen activos. Se restringi? tambi?n la excepci?n preexistente de contrase?as sint?ticas a esa regla, porque una prueba de control demostr? que las excepciones globales pod?an omitir archivos completos. Ambos escaneos finales aprobaron. Dos controles negativos confirmaron que se detectan un valor diferente en el archivo autorizado y el valor autorizado en otro archivo. [Revisi?n](evidence/saas-audit-2026-10-19/saas-secrets-review.json). No acredita ausencia absoluta de secretos.

Estas comprobaciones no constituyen un despliegue ni validan proveedores reales. La configuraci?n de excepciones y la documentaci?n se actualizaron despu?s de los tests, sin cambios al c?digo Python ejecutado.
