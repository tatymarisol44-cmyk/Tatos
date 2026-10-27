# ADR 0025 — Autorizar canales de Meta mediante login

Fecha: 27/10/2026. Estado: implementado localmente, pendiente de configuración y validación real con Meta.

## Problema y decisión

La configuración de WhatsApp, Instagram y Facebook exigía IDs y referencias de secretos. La propietaria pidió que cada usuario iniciase sesión y confirmó que la app de Meta de Tatos ya está creada.

Se conserva la configuración del proveedor en el servidor una sola vez. Cada responsable con rol Administración, Dueña o Marketing autoriza los activos del consultorio mediante Facebook Login para empresas, Instagram Login o WhatsApp Embedded Signup. La elección final se vincula a persona y tenant. No se autentica a una paciente en Meta ni se deduce consentimiento de marketing de esta autorización.

La API de cuentas conserva compatibilidad de referencias de entorno para Telegram/TikTok y cuentas anteriores. Con el login de Meta habilitado se impide añadir manualmente esas tres redes. Los adaptadores de mensajes, avisos, recordatorios y publicaciones leen el mismo almacén de permisos.

## Secretos y transacciones

OAuth entrega permisos nuevos de forma dinámica. Para soportarlo se amplía ADR 0015: los tokens obtenidos mediante login se almacenan **cifrados**, con AES-GCM y HKDF sobre la clave privada existente, propósito distinto del de mensajes y AAD por tenant/referencia/red/activo. Las referencias manuales continúan fuera de la base de datos. No se persiste ningún token en claro ni se incluye en respuestas.

El estado se almacena como hash y se consume mediante actualización condicional antes de canjear el código. Los activos candidatos se conservan cifrados durante quince minutos. La conexión final se confirma en una transacción con el evento de auditoría y una restricción de unicidad del activo OAuth. Desconectar desactiva la cuenta y elimina su permiso local. Los callbacks de desautorización de Meta siguen pendientes de implementar y verificar.

Los adaptadores rechazan un permiso OAuth ausente, alterado, vencido o desconectado. No convierten esa situación en un envío simulado exitoso. No se repite automáticamente un canje cuyo resultado quedó incierto; se comienza una autorización nueva.

## Alcance de la evidencia y consecuencias

Hay pruebas locales con proveedor simulado para los tres recorridos, aislamiento por tenant/persona, CSRF, repetición, revocación de acceso del equipo, expiración y cifrado. No acreditan revisión de la app, onboarding de números nuevos, vigencia de permisos externos, coexistencia ni entrega de un mensaje real.

La configuración central, posibles verificaciones del negocio y requisitos de Meta para usuarios externos son prerrequisitos de activación. Antes de publicar para todos deben contrastarse el registro de números, los callbacks de revocación/eliminación, la renovación de permisos y las políticas de privacidad. El proyecto de pagos queda separado y en pausa por petición expresa.
