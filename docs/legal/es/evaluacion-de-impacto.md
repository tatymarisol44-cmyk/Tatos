# Evaluación de impacto en protección de datos (EIPD) — borrador

> **Borrador** preparado por la plataforma para que cada consultorio lo adopte y su delegado lo
> revise. Es obligatoria **antes** de empezar el tratamiento (Art. 42 LOPDP: tratamiento a gran
> escala de categorías especiales). Usa la metodología de riesgos del Art. 40: particularidades del
> tratamiento, de las partes y de las categorías y volumen de datos. El modelo de amenazas técnico
> completo está en `docs/security/threat-model.md`.

## 1. Descripción del tratamiento

Software de gestión para consultorios de salud mental en Ecuador: historia clínica, tests,
documentos, agenda, recordatorios, mensajería con pacientes por WhatsApp con un asistente
automático, seguimiento clínico y marketing con consentimiento. Detalle de actividades, bases
legales y plazos: `registro-actividades.md`.

## 2. Necesidad y proporcionalidad

| Principio | Cómo se cumple |
|---|---|
| Minimización | Los mensajes de WhatsApp no se guardan; los recordatorios no llevan contenido clínico; la base de conocimiento rechaza documentos con cédulas o tarjetas; la memoria del asistente es un vocabulario cerrado de preferencias, no texto libre |
| Limitación de la finalidad | Marketing y analítica solo con consentimiento; los datos clínicos nunca entran a campañas ni a la búsqueda semántica |
| Exactitud | El paciente puede corregir sus datos; los pedidos de corrección se responden en 15 días |
| Limitación de la conservación | Conversaciones no clínicas borradas a los 90 días; marketing borrado al retirar consentimiento; analítica anonimizada |
| Seguridad | Anexo C del contrato de encargo |
| Transparencia | Aviso de privacidad previo; respuestas de IA marcadas como tales |

## 3. Riesgos y medidas

Escala: probabilidad e impacto de 1 (bajo) a 3 (alto). Riesgo = probabilidad × impacto.

| # | Riesgo para los pacientes | P | I | Medidas | Riesgo residual |
|---|---|---|---|---|---|
| R1 | Otro consultorio ve datos de mis pacientes | 2 | 3 | Aislamiento por consultorio probado en cada operación de la API en cada cambio; clave primaria con el consultorio | Bajo (1×3) |
| R2 | Personal no autorizado lee notas de psicoterapia | 2 | 3 | Solo el autor, ni siquiera el administrador; lecturas auditadas | Bajo (1×3) |
| R3 | Una persona en crisis recibe una respuesta automática inadecuada | 2 | 3 | Las crisis nunca llegan al modelo; texto fijo con ECU 911 y 171; alerta inmediata a guardia con escalamiento | Medio (1×3), a revisar con los profesionales (P4) |
| R4 | El asistente da consejo médico | 2 | 3 | Prompt restringido + control determinista posterior que descarta la respuesta | Bajo (1×3) |
| R5 | Filtración por un proveedor de IA | 1 | 3 | Contratos de encargo con retención cero; texto mínimo; crisis fuera del modelo | Bajo (1×3), condicionado a firmar los contratos |
| R6 | Robo de credenciales del personal | 2 | 3 | SSO con MFA obligatorio; cierre de sesión global; claves revocables | Bajo (1×3) |
| R7 | Pérdida de datos | 1 | 3 | Recuperación a un punto en el tiempo; copias en otra región; simulacro de restauración | Bajo (1×3) |
| R8 | Edición encubierta de la auditoría | 1 | 2 | Cadena de hash con anclas en almacenamiento inmutable | Bajo (1×2) |
| R9 | Marketing invasivo o engañoso a personas vulnerables | 2 | 2 | Consentimiento expreso; aprobación humana de cada publicación; frases prohibidas por profesión; tope de contactos; STOP | Bajo (1×2), sujeto a la revisión de publicidad (L5) |
| R10 | Reidentificación de datos "anonimizados" | 2 | 3 | Anonimización con eliminación del vínculo (no hash); prueba de reidentificación; **no se usan datos de salud anonimizados sin autorización de la SPDP** (Art. 31.3) | Bajo (1×3) |
| R11 | Mensajes a números equivocados | 2 | 2 | Coincidencia exacta y única del número; números desconocidos pasan a una persona | Bajo (1×2) |

## 4. Medidas pendientes antes del primer paciente real

1. Firmar los contratos de encargo con los subencargados (`../subprocessors.md`).
2. Prueba de penetración independiente sin hallazgos críticos ni altos abiertos.
3. Designar y registrar al delegado de protección de datos.
4. Revisión jurídica de los textos de crisis y de los consentimientos.

## 5. Conclusión

Con las medidas anteriores, el riesgo residual es **aceptable** para empezar un piloto con un
consultorio, siempre que se cumplan las cuatro medidas pendientes. Esta evaluación se revisa al
cambiar el tratamiento y al menos una vez al año.

| Elaboró | Revisó (delegado) | Aprobó (responsable) |
|---|---|---|
| [plataforma, fecha] | [nombre, fecha] | [nombre, fecha] |
