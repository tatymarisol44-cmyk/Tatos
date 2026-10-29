# dLocal Go en Tatos

Preparación del 29/10/2026. La propietaria eligió dLocal Go. **No hay todavía
credenciales, cuenta aprobada, pruebas contra el sandbox del proveedor o cobros reales.**
El código y las pantallas se verificaron con transporte HTTP simulado. No se ha
publicado este borrador ni ejecutado su migración en producción.

## Destinatarios

- La operadora de Tatos recibe USD 100 mensuales por consultorio para toda la app.
- Cada consultorio conecta su propia cuenta para recibir consultas y citas.
- Las claves del consultorio nunca se usan para cobrar la mensualidad de Tatos.
- La alternativa de transferencia conserva la revisión del ingreso por el destinatario.

## Preparar una prueba sin dinero

1. Crear una cuenta de pruebas en https://dashboard-sbx.dlocalgo.com/signup . Es un
   entorno separado del registro de la cuenta real.
2. Ir a **Integraciones → Integración API** y localizar **Sandbox API Key** y
   **Sandbox Secret Key**. Introducirlas solo en el formulario privado, nunca en
   chat, documentos, parámetros de proceso o repositorios.
3. En una instalación de prueba de este borrador: **Ajustes → Cobros y suscripción**.
   La operadora configura la cuenta en **Administración de suscripciones de Tatos**;
   el administrador del consultorio usa **Cobros del consultorio**. Elegir
   **Pruebas sin dinero real**. Guardar comprueba autenticación mediante GET /v1/me;
   no certifica verificación empresarial, retiros o elegibilidad del comercio.
4. Para la mensualidad, marcar la autorización de USD 100 cada mes y pulsar
   **Configurar tarjeta automática**. Abrir **Autorizar mensualidad en dLocal Go**.
   Los datos de la tarjeta se introducen en el checkout del proveedor.
5. Volver a Tatos y pulsar **Comprobar suscripción**. Un plan preparado no significa
   suscripción activa o mensualidad pagada. La API del proveedor confirma los estados.
6. Para una cita, confirmar su precio en Agenda y pulsar **Preparar pago**. Desde
   **Ver pago → Pagar con tarjeta**, abrir dLocal Go. También está disponible en
   la app del paciente, **Citas → Pagos de mis citas**. Al volver, **Comprobar pago**.
7. **Cancelar renovación** pide confirmación y desactiva exclusivamente el plan de
   ese consultorio. Cierra también su enlace de alta. No devuelve pagos anteriores.

Estas pantallas web pertenecen al borrador; no están en producción. El auxiliar
local de claves de sandbox usa Windows Credential Manager con destino separado de Meta.

## Contrato e integridad

- dLocal **Go**: api-sbx.dlocalgo.com / api.dlocalgo.com; autenticación servidor
  Bearer API_KEY:SECRET_KEY. No confundir con dLocal Enterprise.
- Checkout alojado: POST /v1/payments. Precio de la cita desde el servidor,
  USD/EC, referencia aleatoria y descripción genérica. No se envían ficha, diagnóstico,
  nombre, correo, documento de paciente o datos de tarjeta desde Tatos.
- La creación se reserva en base antes de contactar al proveedor. Ante timeout no se
  repite automáticamente el POST: queda incierta hasta conciliación.
- Webhooks: HMAC-SHA256 de los bytes originales, luego API autenticada y cotejo de
  destinatario, ID, referencia, importe y moneda. Repetir una notificación no duplica
  el ingreso. La URL de retorno nunca establece el estado pagado.
- Plan propio por consultorio en la cuenta operadora, MONTHLY/1 e importe de su
  autorización. dLocal Go programa las renovaciones; Tatos no inicia un cron de cargos
  ni almacena tokens de tarjetas.
- Claves y enlaces cifrados con AES-GCM, propósito y vinculación por registro.
  No se devuelven claves al navegador. Sandbox/live separados y hosts HTTPS exactos.
- Respuestas de billing sin caché; administración conecta cuentas y autoriza la
  mensualidad. Las pacientes solo pagan/consultan citas propias.
- Migración 0027 después de 0026: cuentas, reservas de checkout, planes y renovaciones.
  Ambas siguen sin aplicar en producción.

## Activación real y límites actuales

`BILLING_ENABLED` y `BILLING_DLOCAL_LIVE_ENABLED` siguen false por defecto. Para
modo live, el titular además debe confirmar aprobación y retiros habilitados.
Es una declaración administrativa, no certificación KYC obtenida de GET /me.
PUBLIC_BASE_URL debe ser un origen HTTPS exacto, sin rutas, credenciales o consultas.

Antes de procesar dinero se requiere validación real de dLocal Go. Sus guías
discrepan sobre documentación tributaria de personas físicas: **no se ha confirmado
que la propietaria pueda aprobar retiros sin RUC**. Un registro o sandbox funcionando
no acredita esa aprobación.

La transferencia es una alternativa inicial. Abrir un plan de tarjeta bloquea el
cambio a transferencia hasta conciliación del destinatario para evitar un doble
pago del período; aún no hay cambio automático de método. Las renovaciones se
muestran separadas de las órdenes manuales.

Pendientes: acceso a la app condicionado al pago, prorrateo, devoluciones, cambios
de precio de mandatos existentes, reactivación de planes cancelados y política de
retención financiera. Una creación incierta sin ID confirmado requiere conciliación
del destinatario. Si hay varios mandatos activos, la conciliación se detiene para
revisión; el enlace del proveedor no impide altas duplicadas fuera de Tatos. Cancelar
el plan permite detener todas sus renovaciones sin afectar a otro consultorio.

## Fuentes oficiales

- https://docs.dlocalgo.com/integration-api
- https://docs.dlocalgo.com/integration-api/welcome-to-dlocal-go-api/payments/create-a-payment
- https://docs.dlocalgo.com/integration-api/welcome-to-dlocal-go-api/payments/notifications
- https://docs.dlocalgo.com/integration-api/welcome-to-dlocal-go-api/subscriptions/create-a-plan
- https://docs.dlocalgo.com/integration-api/welcome-to-dlocal-go-api/subscriptions/cancel-plan
- https://helpcenter.dlocalgo.com/es/articles/6960212-faq-configuracion-de-cuenta
- https://helpcenter.dlocalgo.com/es/articles/7897382-consejos-para-completar-el-paso-2-de-la-configuracion-de-tu-cuenta-sin-sobresaltos
