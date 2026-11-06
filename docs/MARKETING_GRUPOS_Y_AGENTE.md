# Grupos de publicidad y agente de marketing

Cada persona pertenece, como máximo, a un grupo. Marketing puede crear grupos,
asignar personas mediante casillas y preparar una campaña distinta para cada grupo.
El agente del catálogo prepara borradores que se pueden llevar a los formularios de
campañas y publicaciones. Estos cambios están implementados localmente, sin publicar.

## Crear y administrar grupos

1. Entrar con rol Marketing, Dueña o Administración.
2. Abrir **Marketing → Grupos**, escribir el nombre y pulsar **Crear grupo**.
3. Marcar las personas y pulsar **Guardar grupo y personas**.
4. Para revisar o cambiar las casillas, abrir **Elegir personas**.
5. Si una persona pertenece a otro grupo, su casilla aparece deshabilitada y se
   indica su grupo. Quitarla de ese grupo y guardar antes de marcarla en el nuevo.
6. Para eliminar un grupo, pulsar **Eliminar grupo** y confirmar en el mismo botón.
   Esto no elimina personas ni sus datos.

Las búsquedas muestran hasta cien personas por página; **Ver más personas** conserva
las casillas marcadas en otras páginas. Los cambios usan control de versión para
evitar que dos ediciones se sobrescriban. La base de datos también impide una doble
pertenencia, incluso ante peticiones simultáneas.

## Publicidad diferente para cada grupo

1. Abrir **Marketing → Campañas**.
2. En **A quiénes**, elegir **Mis grupos de publicidad**.
3. Marcar el grupo destinatario, escribir el nombre y el mensaje.
4. Usar **Modo: Prueba** para verificar sin enviar nada y crear la campaña.
5. Repetir con otro grupo y otro mensaje para una publicidad distinta.
6. Revisar la campaña. Aprobar y enviar siguen siendo acciones separadas.

Si se marcan varios grupos, reciben el mismo mensaje de esa campaña. Los destinatarios
se fijan al crearla; cambiar un grupo después no modifica campañas ya preparadas.
Cancelar y volver a crear la campaña para renovar su lista. La pantalla distingue
personas seleccionadas de aquellas con consentimiento y canal disponible.

Agregar a un grupo no otorga consentimiento. Se comprueba el permiso de marketing al
crear la campaña y otra vez al entregar. Se conservan la baja, los límites de contacto
y el grupo de control existentes. Los contactos restringidos no aparecen ni reciben
campañas. La exportación de privacidad incluye la pertenencia; su eliminación la borra.
No se usan bandas de tests, notas o diagnósticos para asignar personas.

Los grupos son listas internas de destinatarios de Tatos. Una publicación pública de
Facebook o Instagram no se limita a estas listas. Esta función no configura públicos
de anuncios en Meta Ads ni crea grupos en WhatsApp. Los canales de entrega existentes
no se amplían al elegir un canal para el borrador del agente.

## Diseñar con el agente

1. Abrir **Marketing → Agente de marketing**.
2. Escribir qué se quiere promocionar y el público general; elegir canal y tono.
   Usar únicamente información pública, sin nombres de pacientes ni datos de fichas.
3. Pulsar **Diseñar campaña y publicidad**.
4. Revisar el mensaje, título, ideas, llamada a la acción, pie y propuesta visual.
5. **Usar en campaña** rellena nombre y mensaje; elegir después sus grupos.
6. **Usar en publicación** rellena el formulario de imagen. Requiere una cuenta social
   vinculada para crear la publicación mediante el flujo existente.
7. Editar y crear la campaña o imagen. La generación del borrador no guarda una campaña,
   no crea un archivo y no aprueba ni publica contenido por sí sola.

Se selecciona un especialista local de la división Marketing del catálogo, prefiriendo
creación de contenido o estrategia social. La pantalla identifica el agente realmente
cargado; si falta, informa que no está disponible. Usa el proveedor LLM ya configurado
por la aplicación. Con `llm_backend=fake`, se muestra explícitamente una respuesta
simulada; no acredita conexión real con un proveedor de IA.

El agente recibe el encargo público y las reglas publicitarias del consultorio, sin
leer CRM, grupos, historias o tests. Se filtran datos personales detectables y se revisa
la salida con las reglas existentes. Estos filtros no sustituyen la revisión humana.
El borrador reside en memoria de la sesión del navegador y se borra al salir.

## Implementación y verificación

- Migración local `0029_marketing_groups`, después de `0028_image_scoring`:
  tablas de grupos/pertenencias y selección fija de grupos en campañas.
- Los endpoints de grupos, contactos y diseño exigen permisos de marketing.
  Los contactos exponen solo identificador, nombre, grupo y estado del consentimiento.
- No hubo migración remota, conexión a Meta, envío, cobro o publicación.
- Pruebas finales de backend: grupos exclusivos, agente y campañas, con inferencia
  simulada. La prueba visual de la última ampliación requiere ejecución nativa y
  queda pendiente por restricciones del entorno. Consultar la continuidad de MÍA
  para conteos y evidencia, sin confundir las pruebas anteriores con el estado final.
