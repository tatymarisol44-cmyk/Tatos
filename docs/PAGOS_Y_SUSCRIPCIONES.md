# Cobros del consultorio y suscripción de Tatos

**Actualización 29/10/2026:** la propietaria eligió dLocal Go. El estado vigente,
la integración local y los pasos de conexión están en [dLocal Go](DLOCAL_GO.md).
Incluye checkout persistente, planes mensuales del proveedor y conciliación. Sin
credenciales o pruebas reales todavía; cobros de producción cerrados. Las secciones
PayPhone siguientes son antecedentes hasta el 28/10 y no son el plan actual de activación.

## Alcance confirmado por la propietaria

Hay dos cobros diferentes. Los pacientes pagan consultas y citas a su consultorio. Cada consultorio paga una mensualidad a la operadora de Tatos por usar toda la aplicación. Se solicitaron tarjeta automática para la suscripción y la alternativa de transferencia.

La propietaria confirmó **USD 100 al mes por consultorio**, por toda la aplicación, el 26/10/2026. Los importes de 25 USD de algunas pruebas siguen siendo ejemplos ficticios y no constituyen la tarifa comercial. También confirmó que todavía no tiene cuenta Payphone Business.

## Estado de esta rama

Actualización del 28/10/2026: la propietaria retomó los pagos y confirmó que **todavía no tiene cuenta de pasarela**. La copia se reconcilió con main `c7b8919` (redes sociales y protección de registros), conservando los menús de redes. La migración de pagos ahora es **0026**, posterior a la 0025 de login social; ninguna se ha aplicado por este trabajo en producción.

Se añadió `payphone.py`, un adaptador aislado del contrato oficial de preparación y confirmación autenticadas. Usa destinos fijos del proveedor, comprueba importe/moneda/referencias/estado y no transmite datos clínicos. Sus pruebas usan un transporte simulado. **Todavía no está conectado al almacén de órdenes, al retorno HTTP ni a las pantallas**; la URL de retorno construida en ese módulo no constituye un endpoint publicado o configurable en PayPhone. No se ha creado un checkout real, conectado un comercio o recibido dinero. Credenciales, persistencia de intentos, conciliación y circuito HTTP siguen pendientes.

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

- Conectar el adaptador PayPhone a intentos persistentes, preparación/retorno HTTP y conciliación autenticada; completar su integración con las pantallas.
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
- La migración 0026 añade las órdenes y destinatarios de transferencia, después de la 0025 de redes. No se ejecutó en producción.

## Requisitos de Payphone comprobados

La tokenización requiere autorización previa del proveedor y consentimiento explícito del titular. Payphone entrega tokens, pero la recurrencia debe programarse en Tatos. El Botón de Pago exige confirmación de servidor dentro de cinco minutos. Las notificaciones externas también requieren habilitación; una notificación o redirección sin verificar no acredita un pago.

Fuentes oficiales consultadas el 26/10/2026:

- [Botón de Pago](https://docs.payphone.app/boton-de-pago)
- [Tokenización](https://docs.payphone.app/tokenizacion)
- [Notificación externa](https://docs.payphone.app/notificacion-externa)

La revisión de fuentes del 28/10/2026 también encontró el producto **Suscripciones de PayPhone Business**: PayPhone puede gestionar el calendario de un plan desde su panel mediante un enlace inicial, sujeto a validación comercial y habilitación propias. Es distinto de la API de tokenización, que por sí sola no programa cargos. No se dispone todavía de una cuenta habilitada ni de un contrato API para sincronizar esa modalidad con Tatos; no inventar endpoints o dar por implantada la renovación automática. Fuente: [Suscripciones de PayPhone Business](https://help.payphone.app/hc/es/articles/51293618777627-Activa-las-Suscripciones-para-tus-clientes-en-tu-Payphone-Business).

Para el alta de Business en Ecuador, la fuente oficial indica RUC activo. La propietaria debe completar sus datos, verificaciones y condiciones en el proveedor; no se solicita el RUC ni documentos por chat. La confirmación actual de si ya dispone de RUC está pendiente. Fuente: [Registro de Business](https://help.payphone.app/hc/es/articles/31453417964443--C%C3%B3mo-me-registro-en-Payphone-Business).

No enviar contraseñas, tokens ni datos de tarjetas por chat, adjuntos o commits. La configuración de credenciales debe usar el mecanismo privado del servidor.

[Guía para la propietaria: alta de Business, tarjeta automática y transferencias](GUIA_ACTIVAR_PAGOS.md).
