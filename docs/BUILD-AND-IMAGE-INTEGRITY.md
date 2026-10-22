# Build e integridad de imágenes

## Dependencias de construcción de Python

`pyproject.toml` fija las seis herramientas del backend mediante URLs públicas de wheels universales con fragmentos SHA-256. Incluye las dependencias transitivas; no se deja que el aislamiento de build seleccione versiones nuevas. `build-requirements.txt` conserva sus versiones y hashes para auditoría y para el build explícito:

```sh
uv build --build-constraints build-requirements.txt --require-hashes --wheel
```

Esta sintaxis se comprobó con uv 0.12.24. No se usan opciones nuevas que esa versión no admite. Los fragmentos de hash también se verifican durante la instalación aislada con `uv sync`. Una prueba negativa de descarga sin caché confirmó que cambiar el hash del backend rechaza el build y no produce un wheel. [Referencia de uv](https://docs.astral.sh/uv/concepts/projects/build/).

Las herramientas se auditan además de las dependencias runtime. Para actualizarlas, revisar las versiones y los hashes de PyPI, actualizar juntas ambas declaraciones, auditar y repetir las comprobaciones de instalación y empaquetado. No se deben borrar los hashes para conseguir que un build pase.

## Paquetes del sistema

La imagen API usa un snapshot fechado de Debian y otro de Debian Security, ambos seleccionados por `DEBIAN_SNAPSHOT`. Se eliminan las fuentes anteriores, se deriva la distribución de la base fijada por digest y se aplican las actualizaciones de ese snapshot antes de instalar ffmpeg. Se conserva la verificación con el keyring de Debian; solo se desactiva la caducidad temporal de metadatos históricos, como requiere su uso archivado. [Debian Snapshot](https://snapshot.debian.org/).

La fecha es una entrada de build, no una promesa de seguridad perpetua. Debe actualizarse deliberadamente cuando los informes de vulnerabilidades requieran nuevas correcciones, conservando el digest de la imagen anterior para su trazabilidad.

## Construir, escanear y promover el mismo artefacto

1. BuildKit exporta una vez la API (linux/amd64 y linux/arm64) y una vez el agente Java (linux/amd64) a archivos OCI. Los exports incluyen SBOM y procedencia.
2. `oci_artifact.py` rechaza rutas peligrosas, links y entradas duplicadas. Verifica los hashes de blobs y descriptores, los tamaños de capas, el digest del build y las plataformas declaradas frente a la configuración. Conserva las attestations.
3. Trivy recibe el digest concreto de cada manifest runtime desde el layout verificado. Se escanean las tres variantes; HIGH y CRITICAL con corrección disponible bloquean la promoción. [Layouts y selección de manifiestos en Trivy](https://trivy.dev/docs/latest/target/container_image/).
4. Antes de publicar en GHCR, el mismo programa de promoción ensaya ambos archivos contra un registro temporal ligado a 127.0.0.1. También ocurre en PRs. No se publican candidatas de PR en GHCR.
5. `promote_oci.py` comprueba que el archivo no cambió, contrasta su digest raíz y usa Skopeo fijado por digest con `--all --preserve-digests`. Consulta luego los bytes del manifest remoto y exige que su SHA-256 coincida. [Semántica de preservación de Skopeo](https://github.com/containers/skopeo/blob/main/docs/skopeo-copy.1.md).
6. Solo entonces se firman y verifican las dos imágenes publicadas por su digest. No hay una segunda construcción después del escaneo.

El transporte sin TLS queda restringido al ensayo en loopback. Las credenciales de GHCR se pasan mediante un archivo montado de solo lectura al contenedor de promoción, nunca mediante argumentos que contengan su valor. Los informes de vulnerabilidades se conservan como artefactos de CI.

## Límites

Los tests de Python de estos programas usan archivos OCI sintéticos y dobles de subprocess para los fallos. La construcción real, el escaneo y la promoción de prueba necesitan Docker y se verifican en el job de imágenes de CI. Un test unitario no se presenta como una publicación real.

Las entradas de build fijadas reducen variación. No equivalen por sí solas a demostrar imágenes Docker idénticas byte a byte entre fechas, hosts o versiones de BuildKit: la procedencia y otros metadatos pueden contener tiempos. La propiedad que bloquea este pipeline es que **el artefacto publicado sea el mismo que se verificó y escaneó**.
