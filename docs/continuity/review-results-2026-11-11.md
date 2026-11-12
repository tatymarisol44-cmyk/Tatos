# Revisión guiada con la propietaria — 06/11/2026

Entorno: https://tatos-pruebas-20261106.onrender.com/lab . Datos ficticios.
Guía completa: [revisión paso a paso](../guides/revision-paso-a-paso.html).

| Control | Estado de revisión de la propietaria | Evidencia |
| --- | --- | --- |
| 01. Entrar como Doctora y ver las cuatro pacientes | Aprobado por la propietaria | Respondió «Sí, aparecen las cuatro» |
| 02. Crear paciente nueva | Creación confirmada por la propietaria | Respondió «ok ya esta creado». Usó datos distintos del ejemplo; los valores no se recopilan ni se registran aquí |
| 03. Editar y conservar los cambios al recargar | Aprobado por la propietaria | Respondió «si» a si se conservan los valores después de recargar |
| 04. Eliminar otra paciente ficticia | Eliminación confirmada por la propietaria | Respondió «si esta elimindad». El comportamiento al cancelar no se confirmó por separado |
| 05a. Equipo: invitación y activación con contraseña/MFA | Aprobado por la propietaria | Respondió «si» a si pudo entrar después de escribir el código del autenticador, tras renovar la invitación en Chrome |
| 05b. Equipo: quitar acceso y mostrar perfil Sin acceso | Aprobado por la propietaria | Respondió «ojk funciona» a ese recorrido; no se confirmó aparte un nuevo inicio de sesión tras revocar |
| 05c. Equipo: eliminar perfil | Aprobado por la propietaria | Respondió «funciona» a eliminar EQUIPO QA 01 y comprobar que no figura incluso entre perfiles sin acceso |
| 05d. Equipo: reutilizar el correo eliminado | Aprobado por la propietaria | Respondió «si paso» a generar enlace sin error de correo registrado |
| 06a. Agenda: reservar desde Paciente | Confirmada por su reporte y contraste | Indicó07/11/2026 a las11:00 y después «OK» al contraste en Doctora |
| 06b. Agenda: coincidencia en Doctora | Confirmada por la propietaria | «OK» se toma como confirmación de cita visible de Paciente ficticia — Encuesta el sábado7 a las11:00 |
| 06c. Agenda: reprogramar desde Doctora | Confirmada por la propietaria | Respondió «OK» a cita visible el07/11/2026 a12:00 y ya no a11:00; no hay comprobación manual del ID/precio/historial |
| 06d. Agenda: cambio visible en Paciente | Confirmada por la propietaria | Respondió «si si» y después «si» al contraste de12:00 y ausencia de11:00. Confirmación recibida el10/11; la cita del07/11 es ya histórica, sin inspección adicional por herramientas |
| 06e–18. Resto de controles de la guía | Pendiente | Disponibilidad, solapamiento y demás controles aún sin confirmar |

No confundir estas confirmaciones con la suite técnica anterior. No dar por creado,
editado o borrado un registro sin comprobarlo. Retomar por el control06e:
Doctora → Ajustes → Fechas especiales y vacaciones; cerrar solo12/11/2026,
confirmar el guardado, contrastar que no se ofrece en Paciente y restaurar
después con Volver al horario semanal. Estas comprobaciones siguen pendientes.
La actualización de invitaciones reinició los datos de la prueba. Controles01–04
conservan su evidencia histórica; el enlace anterior ya no es válido.
No tocar las cuatro pacientes iniciales durante edición/eliminación: sirven para
comparar saldos, encuestas y permisos. La prueba de borrado usa otro registro.
