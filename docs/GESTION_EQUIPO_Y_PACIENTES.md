# Retirar perfiles del equipo y datos de pacientes

## Equipo

En **Ajustes → Equipo**, Administración puede elegir entre:

- **Quitar acceso:** revoca la cuenta y sus sesiones, conservando el perfil y su correo.
- **Eliminar perfil:** exige escribir el nombre y confirmar. Revoca el acceso y retira los datos de inicio de sesión, incluidos correo, contraseña, autenticador y pasos de alta/recuperación. El correo queda disponible para una nueva invitación con otra identidad; la identidad anterior no se reactiva.

**Mostrar perfiles sin acceso** permite encontrar los perfiles retirados que todavía reservan un correo. Si todos los perfiles están retirados, aparecen automáticamente. Ante una invitación duplicada, la pantalla señala el perfil retirado de ese consultorio cuando corresponde. No se revelan datos de otras organizaciones.

La cuenta con la que se está trabajando aparece como **Tu cuenta actual**. No puede retirarse ni eliminarse a sí misma. Para retirarla se necesita otro acceso administrativo autorizado.

Eliminar el perfil de acceso desactiva su perfil profesional para reservas nuevas. Las citas existentes, los registros clínicos y su autoría se conservan. Revisa las citas pendientes antes de retirar una profesional. La autorización de eliminación del equipo no concede acceso clínico.

API: `DELETE /v1/admin/team/{principal_id}?erase_profile=true`, solo Administración. Sin el parámetro explícito conserva el comportamiento de retirar acceso. La operación conserva el principal mínimo de auditoría y confirma todos los cambios en una transacción. Las peticiones de autenticación tardías no pueden volver a crear credenciales para la cuenta retirada.

## Pacientes

En **Pacientes**, el listado ofrece **Editar datos** y **Eliminar datos**, según los permisos de la cuenta. Al editar se carga primero la ficha completa para conservar los campos que no aparecen en el listado. También siguen disponibles las acciones en la cabecera de la ficha.

Para editar se necesita Administración o Recepción. Para eliminar datos se necesita Administración o Privacidad. Las cuentas sin permiso de eliminación reciben una explicación y pueden consultar sus permisos en **Ajustes → Tu cuenta**. No se amplían los permisos de Dueña, Marketing o el rol clínico.

La eliminación exige escribir el nombre exacto. Retira los contactos y accesos y saca el registro del listado activo. Los registros sin historia asociada pierden también nombre y nacimiento; la información clínica conservada permanece restringida. **Mostrar fichas restringidas** permite comprobar la conservación aplicable.

## Comprobación

Las pruebas utilizan datos ficticios: cancelación, fallo temporal, reutilización de correo con identidad nueva, credenciales/sesiones anteriores invalidadas, autoría conservada, protección de la cuenta actual, rechazo entre organizaciones, acciones desde el listado y presentación móvil. La publicación de los controles no elimina perfiles ni pacientes automáticamente: la responsable elige y confirma cada registro.
