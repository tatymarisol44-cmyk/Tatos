# Manual para configurar y operar el consultorio

**Guía para:** dueña, administración, psicología, recepción, marketing y soporte técnico
**Producto:** consola web del consultorio
**Alcance:** configuración inicial y uso de las funciones disponibles. Los nombres de botones y secciones corresponden a la versión actual; algunas capacidades requieren configuración técnica previa.

> **Importante:** este manual describe el producto y sus límites conocidos. No confirma que las cuentas, redes, servicios de mensajería o integraciones de un despliegue estén conectados. Antes de usar datos reales, pide a administración o soporte que confirme qué se habilitó en tu entorno.

## 1. Preparación inicial: quién hace qué

Conviene completar la configuración en este orden:

1. **Administración:** confirma que el consultorio tiene un entorno activo, el nombre visible correcto y una cuenta de administración inicial.
2. **Administración:** invita al equipo y asigna a cada persona solo los permisos necesarios.
3. **Psicóloga o profesional clínica:** configura su perfil y horario de atención.
4. **Administración y responsable clínico:** revisan la guardia para alertas y deciden qué tests se utilizarán.
5. **Recepción o psicología:** crea las fichas necesarias y verifica citas.
6. **Psicología:** prepara la historia clínica, los tests y las métricas que utilizará.
7. **Marketing o dueña:** configura talleres, campañas y publicaciones solo después de verificar permisos, consentimientos y conexiones.
8. **Soporte técnico:** configura servicios externos, secretos, redes sociales, webhooks y almacenamiento cuando corresponda.

La consola es multiusuario. El acceso se asigna por funciones: una persona administrativa no obtiene automáticamente acceso a la historia clínica.

## 2. Roles y permisos del equipo

La dueña o una persona con rol de **administración** puede abrir **Ajustes → Equipo → Agregar a una persona**. Ingresa nombre y correo, marca los roles necesarios y selecciona **Invitar**.

| Rol | Uso principal |
| --- | --- |
| Psicóloga (`reviewer`) | Agenda, fichas clínicas, tests y mensajes de pacientes. |
| Recepción (`reception`) | Agenda, datos de contacto y enlaces de acceso a la app de pacientes. No ve la historia clínica. |
| Dueña (`owner`) | Informe del mes, talleres y aprobaciones de marketing. |
| Marketing (`marketing`) | Campañas y publicaciones. No necesita acceso clínico. |
| Administración (`admin`) | Configura el consultorio y el equipo; no ve historias clínicas por ese rol solamente. |
| Privacidad (`privacy`) | Atiende solicitudes de las pacientes sobre sus datos. |

Una misma persona puede necesitar más de un rol; asigna únicamente los indispensables. El rol clínico y el administrativo son permisos separados.

### Primer acceso del personal

1. La administración invita a la persona.
2. Copia y envía su enlace de acceso **en privado**. El enlace es de un solo uso y vence a las 72 horas.
3. La persona abre el enlace y crea su contraseña.
4. Configura la aplicación autenticadora solicitada y guarda sus códigos de respaldo de acuerdo con el procedimiento interno.
5. Entra con correo, contraseña y código de la aplicación. La llave personal de emergencia debe mantenerse protegida y usarse solo si corresponde.
6. Si la persona cambia de teléfono, la administración puede generar un enlace nuevo. Si deja el consultorio, selecciona **Quitar acceso** ese mismo día.

No envíes enlaces de acceso en grupos abiertos ni compartas una cuenta entre varias personas.

## 3. Perfil profesional y horario

En **Ajustes**, la profesional completa su perfil antes de abrir espacios de reserva:

1. En **Tu perfil profesional**, escribe el nombre visible que verán las pacientes.
2. Selecciona el perfil profesional correcto para el país/entorno configurado (por ejemplo, psicología clínica o psiquiatría en Ecuador).
3. Selecciona **Guardar mi perfil**.
4. En **Tu horario de atención**, define la duración de cada sesión.
5. Marca los días de atención y define la hora de inicio y fin. Si existe una pausa, activa el segundo bloque de horario y completa su inicio y fin.
6. Selecciona **Guardar horario** y confirma el mensaje de guardado.

Las pacientes solo pueden reservar dentro de los espacios libres generados por este horario. Revisa la zona horaria del consultorio y los espacios publicados después de guardar.

Si el perfil u horario no están configurados, la agenda avisará que no puede mostrar espacios libres. Si cambian los días habituales, actualiza el horario antes de avisar disponibilidad.

## 4. Guardia para alertas urgentes

La guardia determina a quién se avisa cuando llega una alerta que requiere atención. En **Ajustes → Guardia para crisis**, la administración agrega:

1. Nombre de la persona responsable.
2. Nivel de escalamiento:
   - **Nivel 1: De guardia**, primer aviso.
   - **Nivel 2: Respaldo**, recibe aviso si nadie responde al primer nivel en 5 minutos.
3. Al menos un medio de contacto disponible: WhatsApp o correo.
4. Selecciona **Agregar a la guardia** y confirma que la persona aparece en la lista.

Mantén la lista actualizada y confirma periódicamente que los contactos funcionan. Sin guardia configurada, las alertas solo se ven dentro de la aplicación; no asumas que alguien recibirá un aviso externo.

La alerta no sustituye el criterio clínico ni los protocolos locales de emergencia. Sigue el protocolo del consultorio y contacta personalmente a la paciente cuando corresponda. No envíes por canales de guardia detalles clínicos que no sean necesarios.

## 5. Fichas de pacientes

### Crear una ficha

1. Abre **Pacientes → Nuevo paciente** (o el acceso directo **Nuevo paciente**).
2. Escribe el nombre y apellido.
3. Añade celular, correo y fecha de nacimiento solo cuando sean necesarios.
4. Selecciona **Crear paciente**.
5. Revisa que se abrió la ficha correcta.

El celular puede ayudar con el contacto y la identificación cuando una persona escribe. Recoge únicamente los datos necesarios para la atención. Si hay homónimos, confirma la identidad antes de añadir información clínica.

### Acceso de la paciente a su aplicación

En la ficha, selecciona **Acceso a su app**:

- **Generar su enlace personal** crea un enlace que se muestra una vez y vence en 90 días. Compártelo únicamente con esa paciente, por un canal privado. Quien tenga el enlace puede entrar a su app.
- La paciente puede activar el inicio con correo y contraseña desde su aplicación, según las opciones que se le presenten.
- **Crear enlace para recuperar contraseña** sirve para ayudar a una paciente que perdió el acceso. Verifica su identidad antes de generarlo; vence en 3 días.

El enlace da acceso a la app de la paciente, no a su historia clínica. Aun así, debe tratarse como una credencial privada. No publiques enlaces, no los pegues en notas compartidas y no uses el enlace de una paciente para hacer pruebas.

## 6. Agenda y citas

La agenda usa el perfil profesional y el horario configurado en Ajustes.

### Registrar una cita desde la consola

1. Abre **Agenda → Nueva cita** o selecciona un espacio libre.
2. Elige la paciente.
3. Confirma fecha, hora, duración y tipo (seguimiento, primera consulta o evaluación).
4. Selecciona la modalidad (presencial o en línea) y registra el valor si corresponde.
5. Selecciona **Guardar cita**.

El sistema no permite guardar dos citas superpuestas. Revisa fecha, hora y paciente antes de confirmar.

### Actualizar el estado

Selecciona la cita en la agenda. Según su estado, podrás confirmarla, marcarla como atendida, indicar que no asistió o cancelarla. Actualizar el estado permite que la agenda y los informes reflejen lo ocurrido.

La disponibilidad se ofrece conforme al horario configurado. No uses el calendario como sustituto de la historia clínica.

## 7. Ficha clínica: historia, tests, métricas y documentos

Al abrir una paciente, encontrarás secciones como **Resumen**, **Historia**, **Tests**, **Documentos**, **Citas** y **Mensajes**. La historia, los resultados y los mensajes clínicos requieren el permiso clínico correspondiente.

### Resumen

El resumen previo a la sesión es un borrador automático compuesto por fechas y números de la ficha. No lee las notas de historia ni utiliza inteligencia artificial. Incluye indicadores de seguimiento cuando existe consentimiento de analítica. Sus bandas son ayudas, no diagnósticos.

### Historia

1. Abre **Historia**.
2. Selecciona el tipo de entrada disponible para tu perfil.
3. Escribe la nota y selecciona **Guardar en la historia**.
4. Verifica que aparezca en la ficha.

Las entradas se conservan. Si algo requiere corrección, selecciona **Corregir** en la entrada original, redacta la corrección y guárdala. La original no se borra: queda vinculada a la corrección.

### Tests y métricas

En **Ajustes → Tus tests**, la profesional puede:

- Añadir las plantillas disponibles y completar el texto autorizado de cada pregunta.
- Crear un test propio con preguntas, tipos de respuesta, puntuación, subescalas, bandas y alertas.
- Guardar una revisión nueva sin reescribir resultados anteriores.

Una plantilla con textos pendientes no se puede enviar a la paciente. El consultorio no incluye el texto protegido de los instrumentos; utiliza solo contenido para el que el consultorio tenga derecho de uso.

**No configures bandas o alertas por intuición.** Añádelas únicamente cuando la profesional haya definido el método y los rangos adecuados para el instrumento y población que atiende. Las bandas no son diagnósticos. La salvaguarda de la alerta de seguridad del PHQ‑9 debe mantenerse.

Desde la ficha de la paciente, abre **Tests** para enviar un instrumento apto para respuesta en la app y revisar resultados. Los instrumentos que se aplican en sesión no se envían a la app. Las puntuaciones y bandas se muestran al equipo clínico; la paciente responde preguntas, pero no recibe una interpretación diagnóstica del consultorio.

En esa misma sección, **Registrar métrica** permite guardar una medición numérica con nombre, valor, unidad opcional, fecha y nota opcional. Las métricas son parte del registro clínico y no se muestran a la paciente. Revisa el historial antes de registrar una nueva medición para usar el mismo nombre y unidad cuando se trate del mismo indicador.

### Documentos

En **Documentos**, el equipo clínico puede adjuntar archivos permitidos (PDF, PNG, JPEG o texto) y asignarles un nombre. Marca **Solo yo puedo verlo** si el documento debe quedar restringido a quien lo subió. Adjunta únicamente archivos necesarios para la atención y verifica a qué paciente corresponde antes de cargar.

## 8. Mensajes y alertas

La sección **Mensajes** centraliza conversaciones disponibles de canales conectados, como WhatsApp, Instagram Direct o Messenger. Una conversación puede vincularse con la ficha correcta; confirma identidad antes de vincularla.

- Puedes responder personalmente cuando el canal y su ventana de atención lo permiten.
- La IA puede preparar un borrador en algunas conversaciones ordinarias. El borrador no se envía solo: revísalo, corrígelo y selecciona **Enviar**.
- En una crisis o cuando la persona pide hablar con alguien, la sugerencia de IA se desactiva: responde tú y sigue el protocolo clínico.
- Si han pasado más de 24 horas desde el mensaje de la persona, puede no estar permitido responder con texto libre. Usa solo la vía aprobada por la plataforma y el consultorio.
- Los mensajes se conservan cifrados por un periodo limitado (90 días según la interfaz) y las lecturas quedan registradas.
- Respeta las bajas como STOP/BAJA. No vuelvas a contactar a alguien que pidió dejar de recibir mensajes.

Una alerta visible en **Hoy** debe atenderse y cerrarse únicamente cuando la persona responsable haya realizado el seguimiento necesario. Marcarla como atendida no reemplaza el contacto clínico.

## 9. Marketing, talleres, campañas e informes

El acceso depende del rol: marketing administra campañas y publicaciones; la dueña ve informes y aprobaciones; los talleres pueden gestionarse con los roles autorizados.

### Talleres

En **Marketing → Talleres**, crea el taller o charla con los datos solicitados, capacidad y fechas. Verifica que la información sea correcta antes de compartirla y revisa las inscripciones desde la sección. No expongas listas de participantes fuera de los canales autorizados.

### Campañas

En **Marketing → Campañas**:

1. Asigna un nombre y objetivo.
2. Selecciona el segmento permitido y redacta un mensaje informativo que no revele diagnósticos ni tratamientos.
3. Elige el porcentaje de grupo de control.
4. Usa **Prueba** para revisar sin enviar. Cambia a **Real** solo cuando el contenido, destinatarios, consentimiento y canal hayan sido verificados.
5. Envía la campaña a aprobación. La persona autorizada aprueba y luego selecciona **Enviar**.
6. Consulta resultados cuando haya transcurrido la ventana de medición.

El sistema filtra por consentimiento de marketing, evita enviar al grupo de control y no debe usar información clínica para segmentar publicidad. El modo de prueba no envía mensajes.

### Publicaciones

En **Marketing → Publicaciones**, crea el contenido, revisa imagen/texto y solicita aprobación antes de publicar o programar. La aprobación corresponde al contenido exacto. Si necesitas cambiarlo, crea otra publicación y apruébala de nuevo. Si el estado queda como incierto, verifica primero la red social; el sistema no debe reenviar automáticamente.

La integración de redes sociales se configura por separado y necesita apoyo técnico. Consulta el [manual de conexión de redes sociales](./MANUAL_CONECTAR_REDES_SOCIALES.md). No asumas que TikTok, Facebook videos o una cuenta de Meta ya están operativos.

### Informe del mes

En **Marketing → Informe del mes**, revisa métricas de operación, tendencias, objetivos y promedios de compromiso cuando exista consentimiento para analítica. Puedes crear objetivos mensuales medibles y guardar el informe en PDF desde el botón de imprimir. El informe usa totales del consultorio; no muestra datos individuales de pacientes.

## 10. Privacidad y buenas prácticas

- Usa tu propio acceso; cierra sesión cuando termines en un equipo compartido.
- No compartas claves, contraseñas, códigos de autenticación ni enlaces personales.
- Verifica la identidad antes de revelar información o generar recuperación de acceso.
- No copies información clínica a campañas, publicaciones, grupos sociales ni mensajes publicitarios.
- Limita las notas y archivos a la información necesaria para la atención.
- Respeta consentimientos, solicitudes de baja y solicitudes de privacidad.
- Ante una salida del equipo, revoca su acceso de inmediato y revisa credenciales compartidas/integraciones.
- No borres ni alteres entradas clínicas para corregirlas; utiliza el flujo de corrección.

Las solicitudes de acceso, exportación, restricción o eliminación de datos se tramitan con el rol de privacidad y según las obligaciones legales aplicables. La restricción de una ficha no equivale necesariamente a eliminar el registro clínico que deba conservarse por ley.

## 11. Configuración técnica que requiere soporte

La consola no reemplaza la configuración del entorno donde está alojada. Debe intervenir soporte técnico para:

- Crear o recuperar cuentas del personal y claves iniciales conforme al procedimiento del despliegue.
- Configurar almacenamiento, base de datos, copias de seguridad, correo, URL pública y dominios.
- Guardar tokens sociales y secretos de webhooks en un almacén de secretos.
- Configurar/verificar aplicaciones de Meta y TikTok, permisos y webhooks.
- Habilitar canales de mensajería, notificaciones externas, medios públicos seguros y credenciales de proveedores.
- Ejecutar migraciones y desplegar cambios de la aplicación.

Nunca envíes credenciales por este manual. La cuenta social registrada en el consultorio conserva solo el nombre de referencia del secreto; el token se configura en el entorno. El equipo técnico debe confirmar el despliegue, no solo la existencia del registro.

## 12. Lista de verificación para abrir el consultorio

### Acceso y configuración

- [ ] Existe una persona administradora responsable.
- [ ] Cada integrante tiene su propio usuario, roles mínimos y acceso probado.
- [ ] La profesional tiene perfil visible y profesión correctos.
- [ ] Horario, duración de sesión y zona horaria están revisados.
- [ ] Guardia de primer nivel y respaldo tienen contactos vigentes.

### Atención

- [ ] Se creó una ficha de prueba controlada sin datos reales, o se verificó el proceso antes de registrar pacientes.
- [ ] Se sabe cómo crear la ficha, agendar, confirmar, atender y cancelar una cita.
- [ ] El equipo clínico sabe guardar notas y corregirlas sin borrar originales.
- [ ] Tests, puntuaciones y bandas fueron definidos por la profesional.
- [ ] Hay un proceso seguro para compartir o recuperar el acceso de la app de pacientes.

### Mensajes y difusión

- [ ] Se confirmó qué redes están realmente conectadas en este despliegue.
- [ ] Consentimientos, bajas y responsables de respuesta están claros.
- [ ] El equipo sabe atender alertas y cuándo no usar sugerencias de IA.
- [ ] Campañas y publicaciones tienen aprobación humana y no utilizan datos clínicos.
- [ ] Se probó el modo simulación antes de activar cualquier envío real.

## 13. Solución de problemas frecuentes

| Problema | Qué hacer |
| --- | --- |
| No aparecen espacios para reservar | Completa el perfil profesional, revisa los días y rangos horarios y guarda el horario. |
| No puedo ver historia, tests o mensajes | Pide a administración que confirme que tienes el rol clínico correcto; no solicites acceso administrativo si no lo necesitas. |
| No puedo enviar un test | Verifica que todos los textos autorizados de las preguntas estén completos y que el test sea de respuesta en la app. |
| Aparece una puntuación o banda inesperada | No la interpretes como diagnóstico. Revisa preguntas, puntuación, versión del test, bandas y respuestas con la profesional responsable. |
| No veo una red para publicar | Confirma que la red está soportada en esa pantalla, que la cuenta se registró y que el secreto está configurado por soporte. |
| Una publicación aparece como incierta | Comprueba el estado directamente en la plataforma antes de actuar; no repitas el envío a ciegas. |
| Nadie recibe alertas de guardia | Verifica que haya un contacto activo, medio correcto y configuración de notificaciones del entorno. La consola por sí sola no garantiza un aviso externo. |
| Una persona del equipo perdió el acceso | Administración verifica identidad y genera un enlace nuevo; desactiva el enlace anterior o el acceso comprometido según el procedimiento vigente. |

## Anexo: conexión de redes sociales

El [manual de conexión de redes sociales](./MANUAL_CONECTAR_REDES_SOCIALES.md) detalla los pasos específicos de Instagram, Facebook, WhatsApp y TikTok, permisos de plataformas, secretos y webhooks. Debe leerse junto con esta guía cuando el consultorio quiera publicar o recibir mensajes por esas redes.
