# Cobros del consultorio y suscripción de Tatos

## Alcance confirmado por la propietaria

Hay dos cobros diferentes. Los pacientes pagan consultas y citas a su consultorio. Cada consultorio paga una mensualidad a la operadora de Tatos por usar toda la aplicación. Se solicitaron tarjeta automática para la suscripción y la alternativa de transferencia.

La propietaria confirmó **USD 100 al mes por consultorio**, por toda la aplicación, el 26/10/2026. Los importes de 25 USD de algunas pruebas siguen siendo ejemplos ficticios y no constituyen la tarifa comercial. También confirmó que todavía no tiene cuenta Payphone Business.

## Estado de esta rama

Implementado localmente:

- Separación de destinatarios y órdenes por consultorio y tipo de cobro.
- Importe de una cita obtenido del servidor; el pagador no lo decide.
- Orden mensual por consultorio, con tarifa central configurable. La renovación por tarjeta todavía no está implementada.
- Cuenta bancaria del consultorio y cuenta bancaria de la operadora, configuradas por sus respectivos responsables.
- Referencia de transferencia informada por el pagador y revisión por el destinatario. Un comprobante no marca el pago como recibido.
- Identificador del ingreso bancario único por destinatario: no permite aplicar el mismo movimiento a dos órdenes.
- En Ajustes, **Cobros y suscripción**; en Agenda, **Preparar pago**; en la app de pacientes, **Citas → Pagos de mis citas**.
- Exportación completa de pagos propios en solicitudes de acceso de la paciente, con aislamiento por consultorio y sin truncar el historial al límite de la interfaz.
- Reglas de consentimiento, aniversario mensual, confirmaciones repetidas e incertidumbre de cargos probadas como dominio puro. Esto no es un procesador de tarjetas ni un planificador de cargos ya operativo.

Pendiente, antes de afirmar que existe la pasarela completa:

- Adaptador Payphone con preparación, confirmación y conciliación autenticadas.
- Alta y cancelación del mandato de tarjeta, token cifrado y vinculado a una transacción comprobada por el servidor.
- Planificador persistente de cargos mensuales, recuperación tras reinicios y conciliación de resultados inciertos.
- Definición de inicio/vencimiento de cada período y acceso de la suscripción. La orden de transferencia actual usa el mes calendario; no concede ni restringe acceso automáticamente.
- Política de cancelaciones, devoluciones, cambios de precio y retención de comprobantes, definida por la operadora.
- Revisión de conservación y tratamiento de referencias financieras en solicitudes de borrado; todavía no se define una política financiera de retención.
- Configuración real del comercio y pruebas de integración con el proveedor, separadas de los servicios simulados.

No se han publicado estos cambios ni activado cobros en producción. Las tarjetas no se solicitan ni se guardan en estas pantallas.

## Permisos y destinatarios

| Operación | Quién puede hacerla |
| --- | --- |
| Ver cobros del consultorio y preparar el pago de una cita | Administración, Recepción y Dueña |
| Preparar o informar un pago de cita propio | Paciente autenticado de esa ficha |
| Configurar la cuenta receptora del consultorio y verificar un ingreso | Administración del consultorio |
| Preparar e informar la transferencia de la mensualidad de Tatos | Administración del consultorio pagador |
| Configurar la cuenta de Tatos y verificar mensualidades | Llave de servicio de la organización operadora configurada en el servidor |

Una administradora de un consultorio no puede marcar su propia mensualidad como pagada. El modo de operadora se deriva de la identidad autenticada y de configuración del servidor; nunca de un parámetro enviado por el navegador.

## Configuración pendiente de activación

- `BILLING_ENABLED=false` por defecto: las órdenes nuevas y las verificaciones permanecen cerradas.
- `BILLING_PLATFORM_TENANT`: organización operadora; vacío por defecto. No se infiere de quien abre la pantalla.
- `BILLING_MONTHLY_PRICE_CENTS=10000`: precio aprobado de USD 100 al mes. Un valor explícito de cero permite mantener la tarifa sin configurar.
- La migración 0025 añade las órdenes y destinatarios de transferencia. No se ejecutó en producción.

## Requisitos de Payphone comprobados

La tokenización requiere autorización previa del proveedor y consentimiento explícito del titular. Payphone entrega tokens, pero la recurrencia debe programarse en Tatos. El Botón de Pago exige confirmación de servidor dentro de cinco minutos. Las notificaciones externas también requieren habilitación; una notificación o redirección sin verificar no acredita un pago.

Fuentes oficiales consultadas el 26/10/2026:

- [Botón de Pago](https://docs.payphone.app/boton-de-pago)
- [Tokenización](https://docs.payphone.app/tokenizacion)
- [Notificación externa](https://docs.payphone.app/notificacion-externa)

No enviar contraseñas, tokens ni datos de tarjetas por chat, adjuntos o commits. La configuración de credenciales debe usar el mecanismo privado del servidor.

[Guía para la propietaria: alta de Business, tarjeta automática y transferencias](GUIA_ACTIVAR_PAGOS.md).
