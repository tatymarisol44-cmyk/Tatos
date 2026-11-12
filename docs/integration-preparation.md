# Integraciones por consultorio — preparación del 10/11/2026

Entrada en la aplicación: **Ajustes → Integraciones del consultorio → Preparar
integraciones** (`/app#ajustes/integraciones`). Visible a Administración y Dueña.
Esta implementación está preparada en código. No se ha publicado un despliegue nuevo.

Cada consultorio conserva sus propias referencias. Guardar un dato no demuestra
su titularidad, no cambia permisos externos y no activa un conector.

| Proveedor | Datos de preparación | Cómo se obtiene acceso real |
| --- | --- | --- |
| Meta | RUC de 13 dígitos e ID numérico del portafolio empresarial | OAuth existente: iniciar sesión, conceder permisos y seleccionar un activo autorizado. Meta determina la elegibilidad de la app, la cuenta y los activos |
| TuFacturero | RUC y referencia de cuenta opcional | Documentación y credenciales de integración entregadas por el proveedor; implementación y prueba de su contrato pendientes |
| MentalTestLab | Referencia de cuenta, sin datos de pacientes | Confirmación de que ofrece API para este uso, documentación y autorización del proveedor; conector pendiente |

## Meta y las pruebas

No existe una equivalencia entre declarar un RUC y tener un rol o permiso de Meta.
Tampoco basta un ID para demostrar que se administra un negocio. No se ha
implementado autoasignación de roles de desarrollo. Cada consultorio actúa con su
propia autorización; declarar su RUC no cambia la empresa propietaria de la app.
El formulario no recibe un App Secret de un cliente
ni modifica el identificador central de la aplicación.

Los botones existentes de Facebook, Instagram y WhatsApp siguen pasando por el
proveedor, con estado vinculado al navegador y selección posterior del activo.
Los datos declarados de esta preparación no autorizan activos ni sustituyen esas
comprobaciones. Tener las referencias guardadas no implica que Meta haya aprobado
la aplicación para consultorios externos.

La documentación oficial de Meta publicada en su colección de Marketing distingue
gestionar las cuentas propias de gestionar cuentas publicitarias ajenas: estas
últimas requieren Advanced Access. Su colección de conversaciones también exige
acceso avanzado para los casos externos que describe. Esas reglas dependen de la
API y permiso concreto; no se promete habilitación universal.

Fuentes consultadas el 10/11/2026:

- [Meta: Marketing API, requisitos y permisos](https://www.postman.com/meta/facebook-marketing-api/collection/0zr4mes/facebook-marketing-api-mapi).
- [Meta: Conversations API](https://www.postman.com/meta/messenger-platform-api/folder/22794852-255610cd-47f5-4f4d-b3fa-71aec360be9a).
- Las páginas directas de developers.facebook.com devolvieron HTTP 429 durante
  esta consulta. No se atribuye una verificación nueva de su panel ni sus roles.

## Solicitud técnica para TuFacturero

Su [respuesta oficial de integración](https://tufacturero.ec/faq-items/puedo-integrar-tufacturero-con-mi-sistema-administrativo/)
confirma la existencia de una API y remite a su departamento de desarrollo.
No se obtuvo una especificación pública de endpoints y autenticación.

Texto preparado para solicitar documentación, sin enviarlo a terceros:

> Deseamos integrar Tatos con TuFacturero para que cada consultorio emita con su
> propia cuenta y RUC. Necesitamos documentación oficial o especificación OpenAPI,
> ambiente de pruebas, autenticación por empresa, alcances, límites y catálogo de
> errores. También los contratos para crear un comprobante, consultar su estado,
> obtener XML/RIDE, gestionar notas o anulaciones y evitar duplicados después de
> un timeout. Confirmen si ofrecen webhooks y cómo se valida su autenticidad, así
> como requisitos para establecer la titularidad de la cuenta y la habilitación
> del emisor. Las credenciales se entregarán únicamente por un canal privado.

No se han inventado endpoints, métodos de autenticación o callbacks del proveedor.
El módulo anterior de ensayos fiscales continúa siendo una simulación sin valor
tributario. No es una conexión a TuFacturero ni prueba de autorización del SRI.

## Solicitud técnica para MentalTestLab

El enlace canónico utilizado es [Evaluaciones](https://mentaltestlab.com/evaluaciones/).
La dirección `/.well-known/sgcaptcha/` enviada en el chat es un desafío de acceso,
no documentación de API. No se conserva su consulta con IP/marca temporal ni se
automatiza resolver o evitar el CAPTCHA. No se encontró documentación pública
de API en la búsqueda realizada; ello no demuestra que no exista una API privada.

Texto preparado para solicitar información, sin enviarlo a terceros:

> Deseamos integrar evaluaciones de MentalTestLab en Tatos, con una cuenta propia
> por consultorio. ¿Ofrecen una API autorizada? Necesitamos documentación,
> sandbox, autenticación, límites y contrato para catálogo, asignación de una
> evaluación, consulta de estado e importación de resultados. Confirmen requisitos
> de licencia, versión y baremos de cada instrumento; alcance de exportación de
> informes, consentimiento, conservación y eliminación de datos. Si ofrecen
> notificaciones, necesitamos su esquema y validación de autenticidad.

No se copian instrumentos ni baremos del sitio. Los [términos del proveedor](https://mentaltestlab.com/terminos-y-condiciones/)
exigen autorización para reproducir o comercializar su contenido. No se enviaron
datos clínicos ni se incorporaron resultados externos.

## Contrato interno implementado

- `GET /v1/integration-setup`: tres fichas y requisitos, exclusivamente del tenant
  autenticado.
- `PUT /v1/integration-setup/{provider}`: `{version, profile: {ruc, account_id}}`.
  Proveedores admitidos: `meta`, `tufacturero`, `mentaltestlab`.
- `DELETE /v1/integration-setup/{provider}?version=N`: elimina referencias;
  conserva una revisión vacía para impedir que una escritura antigua las restaure.

Los campos adicionales, URLs arbitrarias y credenciales son rechazados. El RUC se
comprueba solo por formato: **no** equivale a validación contra el SRI.
Respuesta: `references_saved` o `not_configured`; `identity_verified=false` y
`external_connection=not_checked`. El transporte OAuth de Meta existe previamente;
los transportes de TuFacturero y MentalTestLab no están implementados.

Persistencia: `integration_setup_profiles`, clave primaria `(tenant, provider)`;
contenido cifrado con AES-GCM y asociado a tenant/proveedor. Auditoría transaccional
sin RUC ni ID de cuenta. Escrituras condicionales por versión, conflicto HTTP 409.
Migración nueva: `0038_integration_setup`, sobre `0037_tax_trials`.

La prueba en memoria de Render perderá estas referencias al reiniciar si se publica
allí. La persistencia de producción requiere ejecutar y comprobar la migración en
su base correspondiente. No se ha ejecutado en producción.
