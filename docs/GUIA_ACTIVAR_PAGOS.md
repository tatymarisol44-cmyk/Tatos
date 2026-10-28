# Activar los pagos de Tatos, paso a paso

Preparado el 26 de octubre de 2026. Precio aprobado: **USD 100 al mes por consultorio, por toda la aplicación**. La propietaria todavía no tiene cuenta Payphone Business.

## 1. Distingue las dos cuentas receptoras

| Qué se paga | Quién paga | Quién recibe |
| --- | --- | --- |
| Suscripción mensual de Tatos, USD 100 | Cada consultorio | La empresa o persona operadora de Tatos |
| Consulta o cita, según el precio de la agenda | La paciente | Su consultorio |

La cuenta de Tatos no debe recibir por defecto el dinero de las consultas. Cada consultorio necesita su propia configuración receptora. Configurar la cuenta de Tatos no conecta automáticamente las cuentas de todos los consultorios.

## 2. Abre tu cuenta Payphone Business para Tatos

1. Ten disponible el **RUC activo** con el que opera el negocio. Para el registro en Ecuador, Payphone indica que una cédula sola no permite abrir Business. No envíes el número ni documentos por este chat.
2. Abre el [portal de Payphone Business](https://appstore.payphonetodoesposible.com/login), desde la [guía oficial](https://learn.payphone.app/), y elige **Registrarte**.
3. Selecciona el país y completa los datos que solicite el portal: negocio o razón social, correo, teléfono y RUC. La titular de la cuenta debe revisar y aceptar personalmente sus condiciones.
4. Completa la verificación de correo. Si no llega, revisa la carpeta de correo no deseado.
5. Inicia sesión y sigue la verificación comercial y bancaria que indique Payphone. Comprueba que el titular receptor corresponda a quien cobrará las mensualidades de Tatos.
6. Si todavía no tienes RUC activo, detén aquí el alta de Business. La cuenta Payphone Personal no sustituye los permisos de integración Business.

Referencia: [requisitos y registro oficial de Payphone Business](https://help.payphone.app/hc/es/articles/31453417964443--C%C3%B3mo-me-registro-en-Payphone-Business).

## 3. Prepara la integración con tarjeta

1. Dentro de Business, habilita el usuario o rol **Desarrollador** indicado por Payphone.
2. Sigue su configuración de aplicación **WEB**. Payphone solicita el dominio autorizado y una URL de respuesta.
3. El dominio y la URL de respuesta deben corresponder a la integración de Tatos que se publique y pruebe. **Esta rama aún no contiene esa ruta de respuesta**: no registres una dirección inventada ni la URL de inicio como si confirmara pagos.
4. Obtén el **Token** y el **StoreID** en el panel. Se configurarán mediante el mecanismo privado del servidor cuando el adaptador esté listo. No pegues el Token en chat, manuales, capturas, archivos públicos ni código del navegador.
5. Prueba el Botón de Pago en el ambiente del proveedor. La vuelta del navegador no basta: el servidor debe confirmar y comprobar la transacción. Payphone establece un plazo de cinco minutos para su confirmación.
6. Antes de producción, comprueba monto, moneda, destinatario y referencias; también los casos de rechazo, tiempo agotado, abandono, doble clic y notificación repetida. Los ensayos simulados no demuestran cobros reales.

Referencia: [integración oficial del Botón de Pago](https://docs.payphone.app/boton-de-pago).

## 4. Solicita el cobro mensual automático

Actualización del 28/10/2026: PayPhone también ofrece **Suscripciones de Business**, con planes y cobros periódicos gestionados por su propio panel mediante un enlace inicial. Requiere validación comercial y habilitación específicas. Esa modalidad es distinta de programar la recurrencia en Tatos mediante la API de tokenización; todavía no se ha habilitado ni integrado ninguna de las dos. [Guía oficial de Suscripciones de Business](https://help.payphone.app/hc/es/articles/51293618777627-Activa-las-Suscripciones-para-tus-clientes-en-tu-Payphone-Business).

Los pasos siguientes corresponden a la modalidad de **API de tokenización**:

1. Solicita a Payphone la habilitación de **tokenización**. Su documentación exige aprobación previa y una integración inicial de Botón de Pago o Cajita operativa en producción.
2. El alta de Business, por sí sola, no habilita los cargos automáticos. Si Payphone todavía no aprueba la tokenización, la alternativa es la transferencia o un pago individual, cuando sus respectivas integraciones estén activas.
3. Antes del primer cargo recurrente, el consultorio deberá aceptar expresamente los **USD 100 mensuales**, la fecha de renovación y las condiciones de cancelación. La pantalla y el registro persistente de esa aceptación todavía están pendientes de implementación.
4. Los datos de tarjeta se introducen en el formulario del proveedor. Tatos deberá conservar únicamente el token y los datos mínimos autorizados, protegidos en el servidor; nunca el CVV ni el número completo de tarjeta.
5. Con la API de tokenización, Tatos debe implementar el calendario mensual y la conciliación. El producto Suscripciones de Business administra su propio calendario, sujeto a su contratación y habilitación.
6. Verifica una renovación, su comprobante, la cancelación futura, una tarjeta rechazada y la recuperación después de un reinicio antes de habilitar esta opción a los consultorios.

Referencia: [requisitos de tokenización y recurrencia](https://docs.payphone.app/tokenizacion).

## 5. Cómo será la alternativa de transferencia

Los siguientes recorridos **ya están implementados y probados localmente con datos ficticios**. Todavía no están publicados ni habilitados para cobros reales.

### Para las consultas de un consultorio

1. Administración entra en **Ajustes → Cobros y suscripción**.
2. En **Cobros del consultorio**, guarda banco, titular, tipo y número de cuenta. Confirma que los datos son correctos. No escribas claves bancarias.
3. Cuando el entorno tenga los cobros habilitados, abre la cita en **Agenda** y pulsa **Preparar pago**. El importe sale del precio guardado en esa cita.
4. La paciente abre **Citas → Pagos de mis citas** en su app y consulta la cuenta receptora y el importe.
5. La paciente hace la transferencia en su banco y comunica la referencia. Tatos muestra **Transferencia por verificar**.
6. Administración del consultorio abre el pago, verifica el ingreso en el banco e introduce el identificador del movimiento, una nota y la confirmación de que recibió el dinero.
7. Solo después de esa revisión el pago aparece **Pagado**. Si no coincide, utiliza **Solicitar corrección**; la paciente podrá informar nuevamente la referencia correcta.

### Para la mensualidad de Tatos

1. La operadora de Tatos configura su cuenta receptora mediante el acceso de operadora definido en el servidor. Una administradora de consultorio no puede atribuirse esa función.
2. Administración de cada consultorio entra en **Ajustes → Cobros y suscripción → Suscripción de Tatos**.
3. Comprueba **USD 100 por mes** y pulsa **Preparar transferencia de la mensualidad**.
4. Transfiere al banco de la operadora e informa la referencia del movimiento.
5. La operadora comprueba el ingreso y confirma el pago. El consultorio pagador no puede aprobar su propia mensualidad.
6. Esta versión registra una orden del mes calendario. **No activa ni suspende el acceso a la app automáticamente**; falta implementar los períodos y derechos de suscripción.

Una transferencia no se debita automáticamente de una tarjeta. Son dos opciones de pago distintas.

## 6. Lo que falta antes de cobrar en la app publicada

- Crear y verificar Business; obtener las habilitaciones que correspondan.
- Completar el adaptador Payphone y su confirmación; después, tokenización, consentimiento y programación mensual persistentes.
- Definir períodos de acceso, cancelaciones, devoluciones y conservación de registros financieros.
- Configurar las cuentas receptoras y credenciales privadas de Tatos y de cada consultorio.
- Aplicar la migración y publicar una versión revisada, con respaldo y pruebas de integración reales.

Actualmente los cobros vienen desactivados. **La aprobación de USD 100 configura el precio; no autoriza ni ejecuta un débito a ninguna persona.**
