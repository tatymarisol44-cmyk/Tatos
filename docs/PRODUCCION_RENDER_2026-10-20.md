# Publicación comprobada en Render, 20 de octubre de 2026

El SaaS **Consultorio / Tatos** está sirviendo la versión `c7d3d356` en [abrir la aplicación](https://agency-api-irim.onrender.com/app). Render activó el despliegue `dep-dbbr2ju1egvs73b181ug` el **20/10/2026 a las 13:23:02 de Ecuador** (18:23:02 UTC). El pipeline y las comprobaciones públicas posteriores aprobaron. Esto acredita la publicación técnica; la IA continúa simulada y quedan verificaciones funcionales externas y riesgos de seguridad documentados abajo.

Este informe corresponde a `tatymarisol44-cmyk/Tatos`, servicio `agency-api`, ID `srv-db6sqj0n74is738tm8jg`. MÍA, su SQL de Supabase y Vercel son otro proyecto. El trabajo no publicado que continúa en otra ventana quedó fuera de esta entrega.

## Código, imagen y pipeline

La [PR 29](https://github.com/tatymarisol44-cmyk/Tatos/pull/29) integró las correcciones de la [auditoría SaaS](AUDITORIA-SAAS-2026-10-19.md). La [PR 32](https://github.com/tatymarisol44-cmyk/Tatos/pull/32) actualizó las bibliotecas Debian disponibles al construir la imagen y amplió el bloqueo de avisos reparables de CRITICAL a HIGH,CRITICAL. Ambas se fusionaron por el flujo normal de PR, conservando los ocho controles y las reglas de rama. Commit final: `c7d3d356fc1675bd894dc829d0be1a1876bb9c5d`.

| Comprobación | Resultado real |
|---|---|
| [CI de main](https://github.com/tatymarisol44-cmyk/Tatos/actions/runs/39437142591) | Ocho trabajos aprobados |
| Python 3.12 | 1120 aprobadas, cinco omitidas, cero fallos/errores; cobertura 95,34 % |
| Python 3.13 | 1120 aprobadas, cinco omitidas, cero fallos/errores; cobertura 95,31 % |
| Dependencias Python | 136 auditadas, cero avisos conocidos y cero paquetes omitidos |
| [Deploy](https://github.com/tatymarisol44-cmyk/Tatos/actions/runs/39439882578) | resolve, Render, single-node, Kubernetes, production-render y resultado final aprobados |
| Producción | Imagen firmada verificada por el job; Render `live`; revisión servida comprobada |

Las baterías Python se informan por separado y no se suman. Cuatro omisiones son contratos Java que aprobaron en el trabajo Java independiente; la quinta es un bloqueo asesor PostgreSQL inaplicable a SQLite. Los ZIP de resultados descargados coinciden con los SHA-256 publicados por GitHub. Los ensayos usan datos sintéticos: Render con servicios TLS sustitutos; single-node con su instalador, backup y reinicio; Kubernetes con rechazo y recuperación de una imagen rota. No son recorridos con pacientes o proveedores reales.

Imagen desplegada: `ghcr.io/tatymarisol44-cmyk/agency-orchestrator@sha256:62ef4b98f4157fb9ffc8465212d109412da98090bbff06614a4ebba93e744dce`. Manifest Linux amd64 observado por Render: `sha256:e391210211fb21abe45cf06848c51a9af95bd15ec4a27ebfcf92ae3802ec2d85`. El índice, los manifests y las configuraciones OCI se contrastaron por digest; revisión OCI y `BUILD_REVISION` coinciden con el commit. El índice incluye amd64, arm64 y dos manifests de attestations. La verificación criptográfica de firma ocurre en el pipeline con identidad CI de main y emisor GitHub OIDC; no se presenta la lectura local de metadatos como otra verificación criptográfica.

Se configuró `DEPLOY_TARGET=render` y las referencias del destino en GitHub. La clave existente se transmitió cifrada al secreto del entorno `production`. La aprobación normal del entorno se tramitó en nombre del propietario dentro de su solicitud de publicar, después de comprobar CI, ensayos y backup. No se desactivaron protecciones. Render conserva la imagen por digest también en su configuración persistente; no hizo falta un PATCH posterior ni otro despliegue para fijarla.

## Comprobación pública posterior

`/healthz`, `/readyz`, `/app` y sus assets respondieron 200 con `X-App-Revision=c7d3d356fc1675bd894dc829d0be1a1876bb9c5d`. El JavaScript público coincide byte por byte con el archivo de ese commit; SHA-256 `ed8cc3a8c8735e8fa6d52d82b40f761d9309ad909a881fadc3c5fce08ed7c1fa`. Las rutas de pacientes y agenda devolvieron 401 sin autenticación y `Cache-Control: no-store`. Se observaron DENY, nosniff y HSTS; la página incorpora CSP.

Playwright abrió el acceso en 1280×800 y 390×844: campos visibles, sin desbordamiento horizontal ni errores JavaScript de página. [Captura escritorio](evidence/render-release-2026-10-20/production-login-1280.png) y [captura móvil](evidence/render-release-2026-10-20/production-login-390.png). Un único POST de autenticación con cuerpo vacío devolvió 422 y no-store; no contenía correo, contraseña o código. Estas comprobaciones no iniciaron sesión ni crearon datos de negocio. No se ejecutó carga sobre producción.

## Base, respaldo y recuperación

PostgreSQL real **17.11**, revisión **0024** antes y después de publicar. La conexión se identificó comparándola de forma privada con Render y se verificó el certificado TLS con `verify-full`. Las consultas administrativas usaron `transaction_read_only=on`. No existen cambios de migraciones o modelo `db.py` entre el commit anterior activo y la nueva entrega. La clave de seudonimización coincide con la copia privada y se conservó.

El propietario autorizó explícitamente copiar la base cifrada a esta computadora. El respaldo nuevo contiene 51 tablas, revisión 0024 y la clave de seudonimización dentro del cifrado. Se guardó en una carpeta privada ignorada por Git, conservando copias anteriores; tamaño cifrado 10633 bytes, SHA-256 `be2f4733c4397cf9f365e62e219627f20b66718d0891464ef19667e45acbb80b`. La huella del archivo cifrado se volvió a contrastar. Solo su metadata se versiona. No se restauró o descifró esta copia aquí: los ensayos de restauración PostgreSQL aislados anteriores son evidencia distinta.

Rollback disponible por referencia anterior: `ghcr.io/tatymarisol44-cmyk/agency-orchestrator@sha256:20a753a5b9eacc5610aca313b7e3f00a7186cb6a89be4ed78f22713e8e3c8b63`, fuente `b5db9c3dafa9d3ca3f220fcac7e3c1e19b245250`, deploy anterior `dep-db8ngc3bc2fs73bgss50`. Las capas antiguas se descargaron realmente al analizarlas. No se ensayó un rollback invasivo sobre producción. Para revertir se usa el workflow Deploy manual con ese digest y la aprobación habitual; la revisión DB no cambió. No se automatizó una restauración destructiva.

## Seguridad y pendientes

Trivy 0.70.0, publicación oficial contrastada con checksum y digest del asset, base de avisos del 20/10 a las 13:14 UTC. Comparando con la misma base: imagen anterior 220 coincidencias paquete/aviso, 52 IDs y 12 coincidencias reparables; imagen final **208 coincidencias, 42 IDs y cero con parche disponible**. En la imagen final son 207 HIGH y una CRITICAL. Un aviso se repite en varios paquetes; esos números no son 208 explotaciones comprobadas.

No hay excepciones nuevas. El filtro previo `ignore-unfixed` continúa; el informe completo conserva lo que el gate no bloquea. Incluye [CVE-2026-6653 de libxml2](https://security-tracker.debian.org/tracker/CVE-2026-6653), sin parche estable en trixie y clasificado por Debian como menor/no-DSA. Esa clasificación no prueba ausencia de exposición en la app. Se requiere seguimiento del mantenedor y reconstrucción cuando exista corrección. **No se declara cero vulnerabilidades ni completada la aceptación para datos clínicos.** El [informe de la PR](RENDER_SEGURIDAD_CONTENEDOR_2026-10-20.md) conserva el estado histórico previo; esta evidencia registra el resultado final.

`LLM_BACKEND=fake`, embeddings hashing, Qdrant, Redis y checkpoints PostgreSQL se conservaron. Faltan recorrido autenticado del usuario real con su segundo factor, proveedor LLM real y calidad, Meta/WhatsApp y entrega real, alertas operativas, restauración de esta copia productiva y validaciones clínicas/legales correspondientes. La evaluación de routing sigue offline: 40 ejemplos, top-1 57,5 %, recall 87,5 %, top-1 español 16,7 %; no acredita calidad suficiente en español. Los resultados de concurrencia y carga aislada del informe anterior conservan su fecha y sus límites, sin atribuirlos a capacidad real de Render.

El hallazgo operativo SAAS-02 queda resuelto en su parte de entrega: el pipeline terminó y el servicio sirve la nueva revisión. Los pendientes de aceptación externos no se convierten en aprobados por esta publicación. [Resultado y evidencias](evidence/render-release-2026-10-20/release-result.json), [checksums](evidence/render-release-2026-10-20/manifest.json).
