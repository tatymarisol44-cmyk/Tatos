# Confirmación de pagos y recordatorios automáticos

Borrador del 03/11/2026. La propietaria pidió verificar los pagos en la pasarela y
enviar WhatsApp automáticamente, tanto para pacientes como para mensualidades de
Tatos. Eligió **un recordatorio diario después del vencimiento, máximo tres por
pago**. La implementación y la demostración usan proveedores simulados; la conexión
real y la publicación siguen pendientes.

## Recorrido

1. Cada consultorio conecta su cuenta de dLocal Go para las citas; Tatos usa su
   cuenta operadora para sus planes mensuales. Los destinatarios permanecen separados.
2. La pasarela notifica cambios mediante un webhook firmado. Tatos verifica la
   firma y consulta la API autenticada, cotejando cuenta, referencia, importe y moneda.
   También consulta periódicamente los pagos en curso y los planes, sin crear cargos.
3. Administración configura el WhatsApp remitente y el nombre/idioma de una
   plantilla de utilidad aprobada, con dos variables: importe y referencia. El
   guardado comprueba la autorización local de la cuenta; Meta decide si acepta
   cada mensaje. No se afirma haber certificado su plantilla en Meta.
4. Para una cita con un pago registrado en la pasarela, activar el seguimiento
   automático en **Gestionar seguimiento** e indicar su vencimiento. El primer
   aviso se programa para las 09:00 del día siguiente, en la zona del consultorio;
   los siguientes, cada día, con máximo tres. Horario de envío: 09:00 a 18:00.
5. Antes de cada aviso se vuelve a consultar el pago. Un pago confirmado cierra
   el seguimiento. Un estado incierto o una consulta fallida impiden enviar.

Ejemplo: una orden de USD 25 vence el martes. El miércoles puede salir el primer
recordatorio. Si la pasarela confirma el pago ese día, Tatos registra Pagado y
detiene los siguientes. Si permanece pendiente, puede recordar el jueves y viernes.
Las pausas, promesas de pago y fechas de contacto acordadas siguen teniendo prioridad.

En **Automatización de pagos del consultorio** se configura la cuenta remitente,
plantilla, idioma y límites. La operadora tiene **Automatización de mensualidades
de Tatos**. Cada remitente puede contactar a un mismo número como máximo una vez
por día, aunque existan varias órdenes; los contadores por pago se conservan al pausar.

## Qué puede verificarse automáticamente

| Caso | Comportamiento |
| --- | --- |
| Tarjeta de cita, registrada en dLocal Go | Consulta por ID y referencia; confirma Pagado solo con respuesta comprobada. Un PENDING_PAYMENT verificado puede generar el aviso al vencer. |
| Última ejecución rechazada de un plan mensual activo | Crea un seguimiento de ese intento, sin generar una segunda factura/cargo. Consulta de nuevo antes de recordar. |
| Renovación pagada o ejecución más reciente que sustituye a la fallida | Cierra el recordatorio anterior. No sigue reclamando ese intento. |
| Transferencia directa a una cuenta bancaria | Requiere revisión del destinatario. No hay conexión bancaria que confirme depósitos externos. |
| Proveedor inaccesible, fechas incompletas, resultado desconocido o duplicidad de mandatos | No deduce una deuda ni envía el aviso; conserva el caso para revisión. |

La cronología de las renovaciones se toma de sus fechas del proveedor. Cuando el
proveedor no indica zona horaria, esa fecha solo ordena sus propios intentos; no
se convierte en una fecha local de vencimiento. Datos incompletos, tiempos iguales
o mezcla de fechas con/sin zona bloquean la decisión automática.

## Envío e historial

Se exige el permiso específico de recordatorios de pago y se respetan las bajas
registradas. Se comprueban fichas restringidas, citas canceladas/ausentes y pausas.
La plantilla utiliza importe y referencia, sin nombres, diagnósticos o historia clínica.

La reserva del intento se persiste antes del POST a WhatsApp. Las restricciones
de base y la actualización condicional evitan duplicados entre réplicas y limitan
el contacto diario. Un timeout o reinicio durante un envío lo deja **por conciliar**;
no se vuelve a enviar automáticamente, aunque se pause y se intente reanudar.
Actualmente esos casos necesitan investigación del destinatario/proveedor; no hay
un botón que certifique por sí solo que el mensaje no salió.

Una respuesta de Meta con ID acredita aceptación, no entrega o lectura. El historial
lo diferencia del registro manual y de una simulación. La confirmación de pago
procede de la pasarela. Si un pago se realiza justo mientras el proveedor está
aceptando un mensaje, ese mensaje ya iniciado puede llegar; las comprobaciones
previas y el cierre del seguimiento detienen los siguientes. No hay una transacción
atómica compartida entre la pasarela y Meta.

## Activación pendiente

`BILLING_AUTOMATION_ENABLED=false` por defecto, además de los gates financieros
existentes. Cada organización debe habilitar su política y las citas que seguirá.
La conciliación de respaldo corre en el worker existente, cuando está habilitado;
sin worker, los webhooks pueden confirmar pagos pero no se ejecuta ese barrido.

Para enviar realmente hacen falta la cuenta de dLocal Go operativa, WhatsApp
conectado y la plantilla aprobada correspondiente. Los pagos sandbox tienen los
envíos reales bloqueados; la excepción de simulación exige `app_env=test` y un
transporte ficticio inyectado. Ninguna cuenta real, mensaje, cargo o retiro ha sido
verificado por estas pruebas. La migración 0029 sigue sin aplicar en producción.

No se crean cargos desde el worker, no se automatizan transferencias bancarias
externas y no se cambia el acceso de la app por deudas. Devoluciones, reintentos
de cargos, prorrateos y retención financiera siguen fuera de este cambio.

## Referencias oficiales consultadas el 03/11/2026

- [Notificaciones de dLocal Go](https://docs.dlocalgo.com/integration-api/welcome-to-dlocal-go-api/payments/notifications).
- [Consulta de un pago](https://docs.dlocalgo.com/integration-api/welcome-to-dlocal-go-api/payments/retrieve-a-payment).
- [Ejecuciones de una suscripción](https://docs.dlocalgo.com/integration-api/welcome-to-dlocal-go-api/subscriptions/retrieve-all-the-executions-for-a-subscription).
- [Política de WhatsApp Business](https://whatsappbusiness.com/policy/).
- [Mensajes de utilidad y recordatorios de pago](https://whatsappbusiness.com/products/conversation-categories/utility/).
