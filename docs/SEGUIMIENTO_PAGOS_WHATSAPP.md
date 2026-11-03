# Seguimiento de pagos por WhatsApp

La propietaria pidió después [verificación y envío automático](AUTOMATIZACION_PAGOS_WHATSAPP.md):
un recordatorio diario después del vencimiento, máximo tres. Ese circuito nuevo
tiene activación separada, cerrada por defecto. Esta guía describe el modo manual
que sigue disponible para los cobros sin automatización.

Preparación del 03/11/2026 para el borrador de pagos de Tatos. Estas pantallas y la
migración 0028 están verificadas localmente, sin publicar. La conexión real de
[dLocal Go](DLOCAL_GO.md) sigue pendiente de credenciales y pruebas del proveedor.

## Qué aparece

En **Ajustes → Cobros y suscripción**, el personal de Administración, Recepción y
Dueña puede consultar las órdenes de citas pendientes del consultorio. La operadora
de Tatos tiene una sección separada para las mensualidades registradas de los
consultorios. Las organizaciones conservan sus permisos y destinatarios separados.

La lista contiene **órdenes registradas**, no todas las personas sin un pago.
Una cita sin orden o un mes sin mensualidad registrada no se convierten en deuda.
Las ejecuciones de los planes automáticos de dLocal Go siguen en su sección de
conciliación: un fallo del proveedor no genera aquí otra orden para cobrarla.
Las órdenes pagadas salen de la lista; las transferencias informadas y los estados
inciertos quedan señalados para revisión y no permiten preparar recordatorios.

## Preparar y registrar un recordatorio

1. Comprobar el estado de la orden y su importe. Si la persona dice que ya pagó,
   verificar primero el ingreso con el destinatario. Una transferencia informada
   o un mensaje de WhatsApp no confirman un pago.
2. Guardar el teléfono con código de país en la ficha y comprobar que corresponde
   al destinatario. Tatos no adivina el país de los números locales.
3. Contar con el permiso específico **Recordatorios de pagos de este consultorio
   por WhatsApp**. La paciente puede concederlo o retirarlo en su app, **Perfil →
   Lo que aceptas**. Recepción y Privacidad pueden registrar una autorización ya
   obtenida, con su fuente, desde **Gestionar seguimiento**. No marcarla por la
   persona sin haber recibido su autorización; el permiso de marketing no la sustituye.
4. Pulsar **Preparar WhatsApp**. Revisar el borrador: importe y referencia de la
   orden, sin diagnóstico, historia, nombre o detalles clínicos.
5. Pulsar **Abrir WhatsApp**, revisar el número y enviar desde WhatsApp si procede.
   Abrir el enlace solamente no envía ni registra un envío.
6. Volver a Tatos y pulsar **Lo envié por WhatsApp** solo si efectivamente se envió.
   Esto registra la declaración del personal, sin acreditar entrega, lectura o pago.
   Si no se envió, pulsar **Descartar borrador**.

Para mensualidades, la administración de cada consultorio guarda su propio número
y autorización en su sección de suscripción. La operadora puede preparar el
recordatorio cuando exista una mensualidad pendiente y ese permiso esté activo.

## Próximo contacto, promesa e historial

En **Gestionar seguimiento** se puede elegir el próximo contacto, anotar la fecha
prometida de pago o pausar el seguimiento. Una fecha futura bloquea la preparación
hasta que llegue. Después de registrar manualmente un envío, se propone el siguiente
contacto para tres días después; una fecha posterior ya guardada se conserva. El
personal puede modificar estas fechas. No hay un envío programado al vencerlas.

La vista conserva los borradores preparados, descartados y envíos declarados, con
fecha y responsable. Para consultar el historial después de pagar, abrir la orden
en **Ver pago → Ver seguimiento del pago**. La exportación de datos de la paciente
incluye el seguimiento de sus órdenes, sin teléfonos internos ni claves.

La preparación comprueba de nuevo el pago, el permiso y las bajas de WhatsApp
registradas en Tatos. Los pagos de tarjeta de citas se concilian antes de preparar
el texto. Si cambió el teléfono de un borrador abierto, hay que descartarlo antes
de preparar uno nuevo. Las fichas restringidas y las citas canceladas o ausentes
no permiten recordatorios.

## Alcance comprobado

Este circuito usa un borrador revisado y envío manual mediante WhatsApp. **No hay
mensajería automática, envío masivo, tarea de fondo ni confirmación del proveedor.**
Los recordatorios no modifican la orden ni conceden acceso a la app.

Una futura automatización mediante la API de WhatsApp requiere su conexión real
y la plantilla apropiada. Las reglas oficiales exigen permiso y respeto a las bajas;
fuera de la ventana de atención, los mensajes iniciados mediante la plataforma
usan plantillas aprobadas. WhatsApp incluye los recordatorios de pagos entre los
mensajes de utilidad. Fuentes: [política de WhatsApp Business](https://whatsappbusiness.com/policy/)
y [mensajes de utilidad](https://whatsappbusiness.com/products/conversation-categories/utility/).

Validación local del 03/11: 127 pruebas de API, aislamiento, pagos, privacidad y
migraciones aprobadas; una de PostgreSQL omitida por entorno. Tres recorridos de
Chromium aprobados, incluido seguimiento, promesa, registro manual e historial
tras pagar. Datos ficticios y proveedor simulado: no se enviaron mensajes reales,
no se recibieron claves financieras y no se hicieron cargos o migraciones remotas.
