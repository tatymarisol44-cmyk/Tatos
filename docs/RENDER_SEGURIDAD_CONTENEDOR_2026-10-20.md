# Bibliotecas de la imagen Render, 20 de octubre de 2026

La revisión ampliada detectó bibliotecas del sistema con actualizaciones de seguridad disponibles en la imagen que Render sirve actualmente. El control de imagen de CI solo bloqueaba avisos críticos reparables y permitía pasar los altos. La auditoría Python sin hallazgos no examina estos paquetes Debian.

Trivy 0.70.0, igual que la acción fijada de CI, descargado de su publicación oficial y contrastado con el checksum y digest del asset. Base de vulnerabilidades actualizada el 20/10 a las 13:14 UTC. Lectura de imágenes por digest desde el registro; no se ejecutaron contenedores o pruebas invasivas sobre producción.

| Imagen | Resultado antes del cambio |
|---|---|
| Desplegada `20a753a5...` | 220 coincidencias paquete/aviso: 219 altas y una crítica; 52 IDs distintos. Doce coincidencias tienen versión corregida. |
| Base Python fijada `f77ac9e4...` | Cuatro coincidencias altas reparables: PCRE2 y tres paquetes de OpenSSL. |

No son 220 fallos distintos confirmados como explotables: un mismo aviso se repite en varios paquetes binarios de FFmpeg y otras bibliotecas. La vulnerabilidad crítica sin parche estable de libxml2 también se registra; no se elimina del informe.

Fuentes primarias contrastadas: [OpenSSL / Debian](https://security-tracker.debian.org/tracker/CVE-2026-84782) publica la corrección `3.5.7-1~deb13u3`; [Expat / Debian](https://security-tracker.debian.org/tracker/CVE-2026-76957) publica `2.9.0-1~deb13u1`. [Libxml2 / Debian](https://security-tracker.debian.org/tracker/CVE-2026-6653) sigue vulnerable en trixie y lo clasifica como incidencia menor sin DSA. Esa clasificación del mantenedor no demuestra ausencia de exposición en esta aplicación.

El Dockerfile instala las actualizaciones disponibles del sistema antes de FFmpeg, conservando la base fijada, Python, el usuario sin privilegios y las funciones de video. El análisis de la imagen API pasa a bloquear tanto HIGH como CRITICAL reparables. No se añaden excepciones de avisos o paquetes. `ignore-unfixed` conserva su valor anterior; los avisos sin parche deben permanecer visibles en la evidencia completa y requieren seguimiento.

La prueba de regresión es el análisis real de la imagen construida, seguido de los ensayos del pipeline y los controles de disponibilidad. No se añade una prueba que solo compare texto del Dockerfile.

Estado de esta corrección: pendiente de CI y del análisis de la imagen nueva. No acredita publicación, cero vulnerabilidades, calidad del LLM o aprobación del uso con pacientes reales.
