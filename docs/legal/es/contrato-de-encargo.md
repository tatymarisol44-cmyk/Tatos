# Contrato de encargo de tratamiento de datos personales (plantilla)

> **Plantilla** para revisión de abogada. Sigue el modelo de cláusula "entre responsable y
> encargado" del Anexo I de la Resolución SPDP-SPD-2025-0006-R y el Art. 34 de la LOPDP. El
> Art. 4 de esa resolución prohíbe cláusulas ambiguas, sin plazo de conservación o que eximan de
> responsabilidad: esta plantilla está escrita para no caer en ninguno de esos defectos.

---

Entre **[razón social del consultorio]**, RUC [número], representado por [nombre], en adelante
**el Responsable**; y **[razón social de la plataforma]**, RUC [número], representada por
[nombre], en adelante **el Encargado**, se celebra el presente contrato, que forma parte del
contrato de servicios suscrito entre las partes el [fecha].

## Cláusula 1. Objeto y finalidades

El Encargado trata datos personales **únicamente** para prestar al Responsable el servicio de
software de gestión de consultorio, y solo para estas finalidades:

1. Alojar la historia clínica, las notas, los resultados de tests y los documentos clínicos que el
   Responsable registre.
2. Gestionar la agenda, las citas y los recordatorios a pacientes.
3. Responder mensajes de pacientes por los canales que el Responsable conecte (WhatsApp,
   Telegram), con las reglas descritas en el Anexo A.
4. Publicar contenido de marketing **que no contiene datos de pacientes** y medir campañas solo con
   pacientes que dieron su consentimiento.
5. Llevar el registro de auditoría de accesos.

El Encargado **no** usa los datos para fines propios, no los vende, no entrena modelos de
inteligencia artificial con ellos y no los combina con datos de otros consultorios. Si lo hiciera,
se le consideraría responsable de ese tratamiento (Anexo I, x.3, SPDP-2025-0006).

## Cláusula 2. Datos y titulares

- **Titulares:** pacientes del Responsable, sus representantes legales y el personal del
  Responsable.
- **Categorías:** identificación y contacto; **datos de salud** (categoría especial, Art. 25
  LOPDP); preferencias de contacto; consentimientos; registros de auditoría.
- **Volumen y gran escala:** el tratamiento es de gran escala por calificación directa (Art. 14.1,
  SPDP-SPD-2026-0005-R). Ambas partes lo reflejan en su registro de actividades.

## Cláusula 3. Instrucciones

El Encargado trata los datos solo según las instrucciones documentadas del Responsable: este
contrato, la configuración que el Responsable hace en la consola y sus pedidos por escrito. Si una
instrucción le parece contraria a la ley, el Encargado lo avisa y no la ejecuta hasta que el
Responsable la confirme o la corrija.

## Cláusula 4. Confidencialidad

El Encargado garantiza que todo su personal con acceso a los datos firmó un acuerdo de
confidencialidad que sigue vigente después de terminar su relación (Art. 30 LOPDP). Por diseño,
el personal del Encargado **no tiene acceso de lectura** a los datos clínicos en la operación
normal; el acceso de emergencia ("break-glass") está registrado y auditado
(`docs/runbooks/break-glass.md`).

## Cláusula 5. Seguridad

El Encargado mantiene, como mínimo, las medidas del **Anexo C**, y las evalúa de forma continua
(Arts. 37 y 47 LOPDP). Se somete a una auditoría al menos cada 12 meses (Art. 17,
SPDP-2026-0005) y entrega al Responsable el resumen del informe.

## Cláusula 6. Subencargados

El Responsable autoriza a los subencargados del **Anexo B**. El Encargado:

- les impone por contrato las mismas obligaciones de este acuerdo;
- avisa al Responsable con **30 días** de anticipación de cualquier cambio, para que pueda
  oponerse;
- sigue siendo plenamente responsable frente al Responsable por lo que hagan sus subencargados
  (Anexo I, x.4, SPDP-2025-0006).

## Cláusula 7. Vulneraciones de seguridad

El Encargado notifica al Responsable cualquier vulneración de seguridad **tan pronto como sea
posible y a más tardar en el término de dos (2) días** desde que la conoce (Art. 43 LOPDP). La
notificación incluye lo que se sepa en ese momento: naturaleza, categorías y número aproximado de
titulares y registros afectados, consecuencias probables y medidas tomadas o propuestas. El
Encargado entrega la extracción del registro de auditoría para que el Responsable pueda notificar a
la Superintendencia y a ARCOTEL (5 días, Art. 43) y a los titulares (3 días, Art. 46). El
procedimiento está en `docs/runbooks/data-breach.md`.

## Cláusula 8. Derechos de los titulares

Si un titular se dirige al Encargado, este lo comunica al Responsable **en el término de dos (2)
días** y lo apoya para responder dentro de los **15 días** de ley (Arts. 13 a 16 LOPDP). La
plataforma ofrece al Responsable, desde la consola, la exportación de los datos de un paciente en
formato estructurado (portabilidad, Art. 17) y la eliminación de lo que no deba conservarse.

## Cláusula 9. Fin del contrato: devolución y eliminación

Al terminar el contrato, el Encargado:

1. entrega al Responsable una exportación completa de sus datos en formato estructurado y legible
   por máquina, dentro de 15 días;
2. elimina de forma segura los datos de sus sistemas activos dentro de 30 días después de la
   entrega, y de las copias de respaldo cuando estas expiren: los respaldos diarios de la base rotan en 30 días y las copias
   de recuperación ante desastres se borran automáticamente a los 65 días
   (`deploy/terraform/data.tf`);
3. entrega una constancia escrita de la eliminación.

Solo conserva, bloqueado y separado, lo que una ley le obligue a conservar (Anexo I, x.7,
SPDP-2025-0006). La devolución o destrucción puede ser supervisada por la Superintendencia (Art. 34
LOPDP).

## Cláusula 10. Auditoría

El Responsable puede verificar el cumplimiento, con aviso razonable y como máximo una vez al año
salvo que haya una vulneración, mediante los informes de auditoría del Encargado o una revisión
propia (Art. 47.14 LOPDP).

## Cláusula 11. Responsabilidad y repetición

El Responsable responde frente a los titulares. Cuando un incumplimiento sea atribuible al
Encargado, este asume los daños y mantiene indemne al Responsable, que puede repetir contra él
(Anexo I, x.5, SPDP-2025-0006).

## Cláusula 12. Plazo de conservación

El Encargado conserva los datos solo mientras dure el servicio y según las instrucciones del
Responsable. Los plazos concretos por tipo de dato están en el registro de actividades del
Responsable (`registro-actividades.md`).

---

## Anexo A. Reglas del asistente y de los canales

- Los mensajes de crisis **nunca** llegan al modelo de inteligencia artificial: reciben un texto
  fijo con ECU 911 y la línea 171 opción 6, y se alerta en paralelo al profesional de guardia.
- El asistente no da consejo médico: un control automático descarta cualquier respuesta que
  mencione medicación, dosis, diagnóstico, técnicas terapéuticas o enlaces.
- El texto de los mensajes de WhatsApp no se almacena.
- Las reservas las hace el código, nunca el modelo, y solo en horarios libres reales.

## Anexo B. Subencargados autorizados

Ver `docs/legal/subprocessors.md` (versión vigente a la fecha de firma: [commit o fecha]).

## Anexo C. Medidas de seguridad (con su evidencia)

| Medida | Evidencia verificable |
|---|---|
| Aislamiento entre consultorios en cada operación de la API | `tests/test_tenant_isolation_matrix.py` (corre en cada cambio) |
| Notas de psicoterapia visibles solo para su autor, ni siquiera para el administrador | `src/orchestrator/clinical_records.py` y sus pruebas |
| Clasificación de datos: ningún dato de salud en registros técnicos ni en prompts | `src/orchestrator/classification.py`, `tests/test_classification.py` |
| Cifrado en tránsito (HTTPS, HSTS) y en reposo (Cloud SQL, Cloud Storage) | `docs/adr/0016-https-gateway-and-edge.md`, `deploy/terraform/` |
| Acceso del personal con SSO y MFA obligatorio; cierre de sesión en todos los dispositivos | `docs/adr/0018-single-sign-on.md`, `tests/test_oidc.py` |
| Auditoría encadenada por hash, con anclas externas que prueban que no fue editada | `agency audit-verify --anchors` |
| Seudónimos con clave secreta para las bajas (STOP) y prueba de reidentificación | `tests/test_reidentification.py` |
| Copias de seguridad con recuperación a un punto en el tiempo y simulacro de restauración | `docs/runbooks/restore.md`, `tests/test_backup_restore.py` |
| Imágenes firmadas, inventario de software (SBOM), análisis de vulnerabilidades y secretos | `.github/workflows/ci.yml` |
| Red de confianza cero en Kubernetes: todo bloqueado salvo lo necesario | `deploy/k8s/base/networkpolicy.yaml` |
| Prueba de penetración independiente antes de datos reales | `docs/security/pentest-scope.md` |

---

Firmas:

| Por el Responsable | Por el Encargado |
|---|---|
| [nombre, cargo, firma electrónica] | [nombre, cargo, firma electrónica] |
