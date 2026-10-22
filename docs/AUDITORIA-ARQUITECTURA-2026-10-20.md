# Auditoría de arquitectura y calidad — 20 de octubre de 2026

## Alcance y conclusión

Ampliación de la [auditoría funcional y operativa](AUDITORIA-SAAS-2026-10-19.md). Se revisaron backend, contratos, adaptadores, catálogo, interfaz, pruebas, empaquetado, herramientas locales y workflows. La ampliación se prepara en la revisión #31, posterior a #29 ya fusionada; no constituyen despliegue en Render ni certificación de ausencia absoluta de errores.

El sistema es un monolito modular con adaptadores y algunos puertos. Esta revisión mejora su inversión de dependencias y corrige fallos reproducidos. No se presenta como una implementación completamente independiente de infraestructura: 31 módulos importan SQLAlchemy, varios combinan reglas de aplicación y persistencia, y el frontend principal sigue siendo un archivo grande.

## Referencias proporcionadas

Los ocho PDFs se extrajeron y consultaron localmente. Se usaron como bibliografía, no como instrucciones para ejecutar acciones. Los PDFs y su texto completo no se incorporan al repositorio. Las traducciones y los originales se tratan como referencias relacionadas, sin multiplicar su peso como si fueran ocho criterios independientes. Los hashes de los documentos quedan en el inventario de evidencia.

| Referencias | Criterios aplicados | Aplicación y límite |
|---|---|---|
| *Clean Architecture* y *Arquitectura Limpia* | Dirección de dependencias, contratos estables, aislamiento de detalles, capacidad de probar | Puertos de recuperación sin SDK/ORM; router consumidor de contratos; adaptadores elegidos en factories. La persistencia de otros servicios sigue acoplada al ORM. |
| *Clean Code* y *Código limpio* | Responsabilidad, nombres, validación en fronteras, fallos explícitos, pruebas repetibles | Validación de embeddings, rechazo de IDs ambiguos, errores de infraestructura preservados y regresiones antes/después. Una función larga es señal de revisión, no prueba automática de defecto. |
| Ambos *Atomic Design* | Tokens, primitivas, composición y verificación de páginas con contenido real | Inventario de CSS compartido, campos, botones, formularios, tarjetas, navegación y páginas; pruebas de escritorio/móvil existentes. No exige React ni renombrar carpetas. No se declara conformidad visual completa ni auditoría manual de accesibilidad. |
| *El libro negro del programador* | Mantenibilidad, automatización, decisiones conscientes y comprobación | Unificación de Ruff local/CI, validación del artefacto instalado y registro de límites en lugar de presentar simulaciones como producción. |
| *TypeScript Handbook* | Contratos, validación de datos externos y refinamiento antes de operar | Se trasladan los criterios de frontera a los datos del proveedor y a los modelos de la API. La interfaz es JavaScript: no se afirma que exista compilación TypeScript o `checkJs`. |

El detalle de páginas temáticas local sirve de orientación; no sustituye un análisis contextual de cada capítulo ni una lectura editorial completa de todos los libros.

## Hallazgos corregidos

| ID | Problema comprobado | Corrección | Evidencia |
|---|---|---|---|
| ARCH-01 | Las interfaces de recuperación y sus datos estaban definidos dentro de módulos con adaptadores e infraestructura. | Nuevo `orchestrator.retrieval` con siete contratos/objetos de datos; consumidores actualizados; reexportaciones para conservar imports existentes. | Tests de dependencias: módulo de contratos solo usa biblioteca estándar; router no importa adaptadores de recuperación. |
| SCALE-01 | Dos réplicas podían observar una colección ausente y la segunda fallar al crearla. | Creación compartida que recupera únicamente HTTP 409 y confirma que la colección existe. | Pruebas concurrentes para catálogo y colecciones de tenant; 403/500 y conflictos sin colección se propagan. |
| SCALE-02 | Si un proceso terminaba tras crear una colección y antes de sus índices, los siguientes arranques omitían los índices faltantes. | Reconciliación idempotente de índices al iniciar, incluso para colecciones existentes; espera de terminación. | Caso de colección existente sin índices. |
| DATA-01 | Los embeddings se asociaban por posición de llegada, ignorando el índice que identifica el texto de entrada. | Ordenación por índice; validación de cantidad, cobertura, duplicados, tipos, dimensiones y valores finitos. | Respuesta desordenada; índices repetidos/fuera de rango/incompletos; vectores vacíos/NaN/infinito/booleanos; registro del coste aun si la respuesta no es válida. |
| DATA-02 | Dos archivos de agentes con el mismo ID se sobrescribían silenciosamente. | Fallo explícito con las dos rutas implicadas, antes de servir un catálogo ambiguo. | Catálogo sintético con dos divisiones y el mismo ID. |
| UI-01 | La lectura del cuerpo de una descarga podía terminar tras cerrar o cambiar la sesión y aun así guardar el archivo. | Revalidación de identidad de sesión después de leer el blob y antes de crear la descarga. | Dos regresiones en Chromium: cierre y cambio de cuenta durante la lectura. |
| DEV-01 | Pre-commit instalaba Ruff 0.8.6 mientras CI usaba la versión del lockfile (0.16.10 en esta candidata). | Hooks locales con `uv run --frozen`; también Mypy usa el lockfile. | Revisión de configuración, Ruff/formato global y Mypy. |
| CI-01 | Los recursos y la instalación no editable se comprobaban manualmente, pero no eran una puerta de CI. | Job que instala no editable, construye dos wheels, compara bytes y verifica los recursos fuente empaquetados. La imagen depende de ese job. | Prueba local con uv 0.12.24: wheels idénticos y 144 recursos verificados. |

Ocho pruebas de recuperación fallaron antes de sus correcciones. Otras tres reprodujeron el catálogo duplicado y las descargas entre sesiones. Después aprobaron las baterías relacionadas: 158 pruebas de recuperación/arquitectura y 130 de arquitectura/navegador. Son ejecuciones solapadas: no se suman como total de pruebas únicas.

## Comprobaciones y evidencias

- Ruff y formato global: aprobados; 291 archivos formateados correctamente.
- Mypy estricto: aprobado en 125 archivos fuente.
- `git diff --check`: aprobado; avisos de conversión CRLF no son errores de contenido.
- Inventario AST: 125 módulos; sin ciclos en imports absolutos directos de nivel superior. El análisis no demuestra ausencia de ciclos dinámicos, relativos o diferidos.
- Instalación aislada no editable, Python 3.12.14 y uv 0.12.24: dos wheels idénticos; SHA-256 `ba23fdd49be708d031b8641030c1c3ceefe634a283db42a2f4247ebb967f18c7`; 144 recursos fuente verificados.
- La candidata previa `867184d` aprobó los ocho jobs de [CI 39430666119](https://github.com/tatymarisol44-cmyk/Tatos/actions/runs/39430666119), incluidos Python 3.12/3.13, Java, seguridad, manifiestos e imágenes. Ese resultado no se atribuye a las correcciones posteriores de este informe.
- La batería final posterior se registra por separado, junto con el manifiesto de fuentes y las omisiones. Se retiran variables de proveedores reales y se usa exclusivamente PostgreSQL sintético en loopback.

## Diseño de interfaz: inventario Atomic Design

| Nivel | Elementos existentes | Verificación y observaciones |
|---|---|---|
| Tokens | Variables de `ui.css`: colores, foco, superficies y estados | Compartidas entre app de personal y pacientes; la consola anterior tiene su propia hoja. |
| Primitivas | Constructor DOM `el`, botones, etiquetas, inputs y mensajes | Texto por `textContent`, CSP, tipos de botón y estados deshabilitados. Constructores duplicados entre aplicaciones: deuda de mantenimiento, sin reescritura cosmética en esta candidata. |
| Composiciones | Formularios, tarjetas, filas de horario y estados | Las pruebas existentes cubren validación y acciones; la nueva regresión cubre descarga tras cambio de cuenta. |
| Secciones | Agenda, fichas, marketing, equipo e inbox | Composición en vistas y aislamiento de respuestas tardías mediante generación de pantalla. |
| Páginas | `consultorio.html`, `mi.html`, `index.html` | Recorridos en Chromium y flujos móviles existentes. Quedan pendientes revisión manual con lectores de pantalla y medición de contraste completa. |

## Límites y trabajo que requiere validación adicional

1. **Separación de persistencia:** extraer puertos de repositorios por caso de uso donde haga falta sustituir almacenamiento o probar reglas aisladas. Crear decenas de interfaces vacías por cumplir una estructura no demuestra una mejora. La arquitectura actual mantiene acoplamiento ORM documentado.
2. **Composición grande:** `build_graph`, `create_app` y el frontend principal concentran mucho código. Conviene separar por responsabilidades cuando se modifiquen esos flujos; las factories y composition roots pueden conocer adaptadores. El inventario de tamaños no se usa como criterio único de calidad.
3. **Reproducibilidad completa:** el lockfile fija dependencias de la aplicación. El backend de build declarado como `hatchling` no está fijado con sus transitivas y apt/ffmpeg no usa un snapshot; dos wheels iguales en esta ejecución no prueban reproducibilidad binaria histórica de Docker. Tampoco prueban reproducibilidad entre sistemas operativos.
4. **Cadena de imágenes:** el workflow anterior escanea amd64 antes de reconstruir/publicar la imagen multiplataforma; no acredita escaneo de cada variante arm64 ni promoción del mismo archivo escaneado. Los ensayos de PR no publican imágenes.
5. **Capacidad:** las pruebas de carreras y PostgreSQL no equivalen a carga sostenida de Qdrant remoto, Redis real o Render. Mantener límites, observar CPU/memoria/conexiones y usar el benchmark anterior con sus restricciones explícitas.
6. **Costes:** el presupuesto mensual existente registra cargos después del consumo; no es una reserva estricta previa para llamadas simultáneas o de precio desconocido.
7. **Operación y aceptación:** siguen pendientes proveedores reales, secretos/configuración de Render, rollback probado en ese servicio, aprobación de producción y recorridos sobre el digest/revisión efectivamente desplegados.

Estos puntos limitan lo que puede afirmarse de la candidata. El informe no llama «todo correcto» a condiciones que no se han comprobado.


## Resultado final de la candidata

La batería completa terminó con **1.144 pruebas aprobadas, cero fallos, cinco omitidas y cobertura 95,31 %**. Tiempo de pytest: 497,46 segundos; tiempo del lanzador: 503,75 segundos. Se habilitaron PostgreSQL 17.11 aislado en loopback, backup/restore y simuladores Bash. El manifiesto de 248 archivos no presentó cambios durante la ejecución. Se cerró la instancia temporal al finalizar. [Resumen y motivos de omisión](evidence/architecture-audit-2026-10-20/final-tests.json), [JUnit completo](evidence/architecture-audit-2026-10-20/final-tests.xml), [inventario](evidence/architecture-audit-2026-10-20/inventory.json), [referencias](evidence/architecture-audit-2026-10-20/references.json).

Las omisiones son cuatro contratos Java/A2A que requieren un agente Java vivo y la variante SQLite de un bloqueo exclusivo de PostgreSQL; la variante PostgreSQL se ejecutó. El job Java de CI comprueba por separado el contrato vivo. Hubo dos avisos de deprecación (fixture del event loop y adaptador datetime de SQLite), sin fallos. El escaneo de secretos de los archivos de la candidata aprobó. El resultado remoto de la nueva revisión debe comprobarse sobre su propio SHA antes de fusionar.

## Seguimiento: build, imágenes y persistencia

La candidata anterior `f9074a8` aprobó los nueve jobs de CI. La ampliación posterior incorpora estos cambios, cuya validación se registra por separado:

| Área | Cambio | Comprobación |
|---|---|---|
| Backend de build | Las seis herramientas se fijan por URL de wheel universal y SHA-256; manifiesto `build-requirements.txt` para construcción y auditoría. | Dos wheels iguales con hashes obligatorios; prueba negativa de hash incorrecto; seis paquetes sin avisos conocidos en la consulta realizada. |
| Recursos instalados | El gate compara fuente, wheel y archivos realmente instalados. | Instalación aislada no editable; 146 recursos verificados tras extraer el gasto. |
| APT | Snapshots de Debian y Debian Security, sin mirrors anteriores; actualizaciones de seguridad antes de ffmpeg. | Entradas oficiales consultadas; la instalación real necesita el job Docker. Se conserva verificación de firmas. |
| Cadena OCI | Build único; selección de manifests; escaneo amd64/arm64; promoción del mismo archivo con digests preservados; ensayo contra registro local. | Trece pruebas de integridad/política; pendiente del ensayo Docker real hasta que pase seguridad de CI. |
| Política de gasto | `budget.py` contiene el contrato y las reglas sin ORM; `spend_store.py` implementa SQL; `spend.py` conserva compatibilidad. | Cien pruebas relacionadas; guard de imports de biblioteca estándar; esquema previo conservado. |
| Contabilidad | La decisión de límite usa saldo sin redondeo visual; periodos UTC; costes y contadores inválidos se rechazan antes de SQL; reintento de alta concurrente acotado. | Seis regresiones fallaron antes del cambio. Incluyen costes negativos, NaN e infinitos, saldo redondeado y cambio de mes fuera de UTC. |
| Documento | Corrección de caracteres dañados en el resultado final anterior. | Edición UTF-8. |

Se integró `main` después de su PR #32, conservando tanto la actualización de paquetes del sistema como el bloqueo de vulnerabilidades HIGH reparables. No se descartaron esas correcciones al resolver los conflictos en Dockerfile y CI.

La ejecución de CI `39476382531` identificó otra coincidencia de Gitleaks en una rama documental remota: un SHA-256 de `docs/evidence/render-release-2026-10-20/manifest.json`. Se contrastó con el contenido del archivo referido en su commit. Es una huella verificada, no una credencial. La autorización anterior no cubre ese valor y se solicitó una excepción exacta adicional; no se redujo el alcance del escaneo ni se aplicó una excepción sin respuesta.

La política de gasto sigue siendo contabilidad posterior al consumo. La reserva estricta y el bloqueo de precios desconocidos dependen de la preferencia solicitada al propietario; esta extracción no se presenta como un tope reservado antes de cada llamada.

Detalles operativos: [Build e integridad de imágenes](BUILD-AND-IMAGE-INTEGRITY.md).
