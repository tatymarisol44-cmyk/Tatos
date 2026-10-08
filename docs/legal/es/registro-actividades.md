# Registro de actividades de tratamiento (RAT) — plantilla del consultorio

> **Plantilla.** Cada consultorio mantiene su propio registro, lo actualiza **al menos una vez al
> año** o cuando cambie el tratamiento (Art. 6, SPDP-SPD-2026-0005-R) y conserva la evidencia del
> cálculo. La plataforma mantiene el suyo, como encargada, con las mismas columnas y sumando los
> titulares de todos los consultorios.

## 1. Identificación

| Campo | Valor |
|---|---|
| Responsable | [razón social], RUC [número], [domicilio, teléfono, correo] |
| Delegado de protección de datos | [nombre o empresa], registrado en la SPDP el [fecha] |
| Encargado principal | [plataforma], con contrato de encargo del [fecha] |
| Fecha de esta versión | [fecha] (próxima revisión: [fecha + 12 meses]) |

## 2. Actividades

| # | Actividad | Finalidad | Base legal | Titulares | Datos | Destinatarios y encargados | Conservación | Medidas |
|---|---|---|---|---|---|---|---|---|
| 1 | Historia clínica y notas | Atención en salud mental | Arts. 7.5 y 31.1 LOPDP | Pacientes | Identificación, contacto, salud | Encargado y subencargados de hosting | [plazo de la normativa sanitaria] | Anexo C del contrato; notas de psicoterapia solo para el autor |
| 2 | Tests e instrumentos | Evaluación clínica | Arts. 7.5 y 31.1 | Pacientes | Respuestas y puntajes (salud) | Encargado | Igual que la historia | Solo personal clínico; auditado sin respuestas |
| 3 | Documentos clínicos | Adjuntar informes y consentimientos firmados | Arts. 7.5 y 31.1 | Pacientes | Archivos (salud) | Encargado | Igual que la historia | Tipo verificado por contenido; SHA-256; lectura auditada |
| 4 | Agenda y recordatorios | Gestión de citas | Art. 7.5 | Pacientes, personal | Nombre, teléfono, citas | Encargado; Meta (WhatsApp); Telegram si el paciente lo vincula | Mientras sea paciente | Sin contenido clínico en recordatorios |
| 5 | Asistente de WhatsApp | Responder consultas administrativas | Art. 7.5 | Pacientes, personas que escriben | Número, texto del mensaje | Encargado; Meta; Anthropic; OpenAI | El texto no se conserva | Crisis fuera del modelo; control posterior anti-consejo médico |
| 6 | Seguimiento clínico | Asistencia y evolución del paciente | Art. 31.1 | Pacientes | Asistencia, puntajes | Encargado | Igual que la historia | Calculado al momento, sin modelo, sin almacenamiento adicional |
| 7 | Campañas y novedades | Marketing propio | **Consentimiento** (Art. 8) | Pacientes que consintieron | Nombre, canal | Encargado; canal elegido | Hasta retirar el consentimiento | Grupo de control; STOP; tope de contactos |
| 8 | Analítica de uso | Mejorar el contenido | **Consentimiento** (Art. 8) | Pacientes que consintieron | Eventos de la app | Encargado | Anonimización irreversible al retirar | Sin datos clínicos |
| 9 | Auditoría de accesos | Demostrar cumplimiento | Arts. 7.2 y 47 | Personal, pacientes | Quién, cuándo, qué registro | Encargado | [plazo] | Cadena de hash con anclas externas |

## 3. Cálculo de gran escala (MTGE, Arts. 7 a 10, SPDP-2026-0005)

**Calificación directa:** el tratamiento de datos de salud y la gestión de historias clínicas
son de gran escala **sin necesidad de calcular** (Art. 14.1). El cálculo se registra igualmente,
porque el Art. 7 exige que el resultado conste en el RAT.

Ejemplo para un consultorio pequeño (actualizar con los valores reales):

| Variable | Situación | Puntos (Art. 8) |
|---|---|---|
| Titulares en 12 meses | hasta 1.000 pacientes | 1 |
| Volumen | 11 a 30 tipos de datos por paciente | 1 |
| Categorías | una categoría especial (salud) — 3 si también hay menores | 2 |
| Frecuencia | continua (agenda, mensajes en tiempo real) | 2 |
| Permanencia | prolongada (3 años o más) | 2 |
| Alcance geográfico | local | 1 |
| **Total** | | **9** (umbral: 6) → **gran escala** |

**Para la plataforma** (todos los consultorios): titulares de 1.001 a 10.000 al inicio (2),
categorías con menores (3), alcance transfronterizo por los servidores (3): **13 puntos**.

## 4. Consecuencias que se cumplen (Art. 12 y Título V, SPDP-2026-0005)

- [ ] Evaluación de impacto previa (`evaluacion-de-impacto.md`).
- [ ] Delegado designado y registrado en la SPDP dentro de 15 días (SPDP-2025-0028, Art. 5).
- [ ] Este registro actualizado y con evidencia del cálculo.
- [ ] Auditoría anual; informe conservado 5 años.
- [ ] Política de privacidad que identifica el tratamiento a gran escala (`aviso-de-privacidad.md`).
- [ ] Informe anual de cumplimiento conservado 5 años.
