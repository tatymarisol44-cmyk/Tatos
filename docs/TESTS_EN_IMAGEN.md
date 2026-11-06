# Tests en imagen e informe visual

Implementación en una rama independiente de pagos, basada en el código publicado
`c7b8919`. No se desplegó ni se procesaron imágenes clínicas reales.

## Recorrido de la profesional

1. Entra en la ficha de una paciente y abre **Tests**.
2. En **Tests en imagen**, pulsa **Cargar test en imagen**.
3. En **Parámetros del test y datos de aplicación**, carga versión/edición,
   criterios e indicadores, baremos/reglas, fuente/manual y población de referencia.
   Registra fecha de aplicación, edad de entonces (años y meses adicionales, si la
   conoces) y contexto. No se deduce la fecha del momento de subir la imagen ni la
   edad de los rasgos del dibujo.
4. Escribe el nombre y elige **Cuestionario o resultados** o **Dibujo**.
5. Selecciona una imagen PNG o JPEG. Comprueba la vista previa. Cada informe
   corresponde a una imagen de una página, hasta 10 MB por defecto y 20 megapíxeles.
6. Con motor privado configurado, pulsa **Cargar y generar borrador**. Sin motor,
   **Cargar imagen** conserva el original y permite redactar manualmente; no inventa
   una lectura ni presenta una simulación como análisis real.
7. Compara el texto, las marcas y las observaciones con el original. Corrige el
   borrador y añade tu valoración profesional. **Guardar borrador** permite continuar
   después. **Generar resumen visual** vuelve a leer la imagen y reemplaza el texto
   del borrador después de confirmarlo.
8. Marca que comparaste el informe con el original y pulsa **Guardar informe revisado**.
9. Descarga el informe `.txt` o el original. El archivo exportado identifica si es
   borrador o revisado e incluye la huella SHA-256 de la imagen fuente.
10. Un borrador se puede eliminar con su imagen después de dos pulsaciones de
   confirmación. Un informe revisado queda inmutable; una corrección requiere otro
   informe. La ficha restringida conserva lecturas pero bloquea nuevos análisis y cambios.

## Alcance del resumen

Los parámetros se escriben en campos separados. Puedes descargar una plantilla
`.json` para reutilizar **solo los parámetros del test**, sin la fecha, edad o
contexto de una paciente, e importarla en otra aplicación. La importación admite
hasta 32 KB y solo esos campos. También puedes modificarlos antes de la revisión.
**Guardar parámetros** conserva el contexto incluso sin texto del informe.

Para generar o marcar como revisado son obligatorios versión, criterios, fuente y
fecha de aplicación. Edad, baremos y población pueden quedar sin registrar si no
se conocen o no aplican; no se inventan valores. Los parámetros y los datos de
aplicación quedan guardados con el informe, se incluyen en su descarga y no se
pueden modificar después de revisarlo. La referencia es declarada por la
profesional y su carga no acredita la validez clínica o del baremo.

La visión conserva su función de lectura descriptiva. No se envían estos campos
adicionales al modelo; la profesional los usa al cotejar y completar su informe.

Cuestionarios: transcripción del texto legible y descripción de marcas visibles,
con dudas o partes cortadas. Si asocias un test configurado, el motor propone
respuestas por ítem y, cuando puede, una región normalizada de la imagen. La tabla
permite comparar y corregir cada valor. No se completan marcas dudosas o duplicadas.

## Puntuación y bandas desde una imagen

1. Configura el test en **Ajustes → Tus tests → Editar textos → Puntuación y bandas**.
   Carga los valores de respuesta, los ítems inversos, el método (suma o promedio)
   y los rangos con etiqueta y nivel de alerta. Los límites se incluyen y no pueden
   solaparse. Los huecos se muestran como «Sin banda», sin elegir una por aproximación.
2. Confirma que cotejaste los datos con el manual y guarda. Cada cambio crea una
   versión. Puedes exportar la configuración para reutilizarla e importar otros tests
   con su fuente y autorización de uso. Los cambios simultáneos se rechazan.
3. En el test en imagen, elige **Test y versión para calcular bandas**. Se guarda una
   copia exacta de esa definición: las versiones futuras no alteran el informe.
4. Genera la lectura. El motor recibe imagen e ítems/opciones, no los rangos de
   bandas, la edad ni los parámetros narrativos adicionales. Propone respuestas
   numéricas y evidencia; el resultado no lo decide el modelo de visión.
5. Compara todas las respuestas con el original, corrige las dudosas o ausentes,
   marca la comprobación y pulsa **Calcular puntuación y banda**. El mismo motor
   determinista de los tests calcula puntuación, subescalas, banda y alertas.
6. Revisa el informe. Un test asociado sin respuestas comprobadas no puede marcarse
   como revisado. Cambiar fecha, parámetros o test, o volver a analizar, obliga a
   comprobar de nuevo; los cambios de texto del informe conservan el cálculo.

El resultado queda en el informe privado de la autora y en su descarga. No crea
automáticamente un resultado independiente ni envía avisos o puntuaciones a pacientes.
Los baremos narrativos del formulario son una referencia; los rangos que se calculan
proceden de la definición numérica seleccionada, no se interpretan desde texto libre.
La versión actual calcula suma/promedio, inversión, subescalas y alertas declaradas.
Tablas de transformación por edad, percentiles u otros algoritmos requieren el
manual concreto y una implementación específica; no se sustituyen por rangos inventados.

Dibujos: descripción de elementos visibles. El prompt prohíbe inferir diagnósticos,
personalidad, trauma o emociones a partir de rasgos del dibujo. Las respuestas del
modelo no están clínicamente validadas; la profesional debe revisar el contenido.
La revisión humana registrada no valida el modelo ni la prueba psicológica.
Se descartan de observaciones y dudas ciertas inferencias clínicas detectables por
palabras clave, y se conserva el texto transcrito para comparar con el original.
Este filtro es conservador y limitado; no garantiza detectar todas las inferencias
incorrectas ni sustituye la revisión del contenido.

## Activar el motor privado

Se preparó en el equipo una prueba real con **Ollama portátil 0.40.14** y
**Qwen2.5-VL 3B**, sin credenciales externas ni imágenes clínicas. El origen de esa
prueba es `http://127.0.0.1:11435`. El runtime y los modelos se guardan fuera del
código, en `.tools/test-image-reports-2026-11-03/vision-runtime`. La activación en
otros entornos sigue deshabilitada por defecto; no se publicó ni configuró Render.

Una imagen generada con tres valores conocidos (2, 1 y 0) fue leída por el modelo
real y el recorrido de navegador calculó total 3 y «Grupo ficticio B», usando un
baremo inventado únicamente para probar software. Esto no valida tests clínicos,
escritura manual, fotos difíciles ni exactitud de regiones. En esa lectura el modelo
no devolvió regiones; la app conserva la imagen completa para cotejar los valores.
Qwen3-VL 2B también se descargó, pero quedó descartado para esta prueba por salida
vacía/truncada; nunca se utilizó esa salida para asignar una banda.

La integración utiliza Ollama con un modelo de visión instalado en el servidor
privado. La API recibe imágenes en base64 y se solicita una salida JSON estructurada.
Contrato: [Vision](https://docs.ollama.com/capabilities/vision) y
[Structured outputs](https://docs.ollama.com/capabilities/structured-outputs).
Modelo de la prueba: [Qwen2.5-VL](https://ollama.com/library/qwen2.5vl:3b).

Configuración del operador, una vez instalado y evaluado su modelo:

```text
CLINICAL_VISION_ENABLED=true
CLINICAL_VISION_ORIGIN=http://127.0.0.1:11434
CLINICAL_VISION_MODEL=<nombre-del-modelo-local-con-vision>
CLINICAL_VISION_TIMEOUT_SECONDS=120
```

En despliegue, `127.0.0.1` es el servidor de Tatos, no la computadora de la usuaria.
También se admite un origen privado configurado por el operador (IP privada o DNS
`.internal`). No se admiten orígenes públicos, credenciales en la URL, redirecciones,
variables proxy heredadas o una URL enviada por la usuaria. Antes de enviar la imagen
se consulta `/api/show`: debe declarar capacidad `vision`, sin `remote_host` ni
`remote_model`. No hay fallback al chat, RAG o proveedores cloud.

La imagen original se guarda sin alterar en archivos clínicos `author_only`, junto
con su huella. Para inferencia se decodifica completa, se aplica orientación EXIF,
se quitan metadatos y se limita el lado mayor a 3072 píxeles. El texto aún puede
contener datos personales visibles; el motor privado debe operar con las mismas
protecciones del consultorio. El endpoint de configuración indica que fue habilitado,
no acredita que el modelo esté instalado o disponible; el análisis comprueba eso.

## Acceso y persistencia

- Solo quien tiene rol clínico explícito puede acceder. La imagen y el informe se
  limitan a su autora y consultorio; administración, recepción y marketing no acceden.
- Las acciones se auditan sin texto del informe ni bytes de la imagen. Se reusa la
  retención de archivos clínicos y la exportación cuenta los informes restringidos
  sin entregar su contenido a la exportación general de privacidad.
- La revisión usa control de versión. Una edición concurrente o restricción de la
  ficha impide que un análisis lento sobrescriba el estado nuevo.
- Migración nueva: `0026_test_images`, desde `0025`. Antes de combinar con el PR40
  de pagos se debe reconciliar la cadena de migraciones; esta rama no contiene pagos.
- Parámetros y aplicación: `0027_test_parameters`, desde `0026_test_images`. Los
  informes anteriores conservan sus datos y muestran parámetros sin registrar;
  no se les asignan referencias retroactivamente. Ninguna migración se ejecutó en producción.
- Asociación y puntuación de imágenes: `0028_image_scoring`, desde
  `0027_test_parameters`. Añade la definición seleccionada, propuestas, respuestas
  comprobadas y resultado, con los mismos controles privados y de revisión.

## Evidencia y límites

Las pruebas usan PNG creados por código y respuestas de inferencia simuladas. Se
comprueba validación de archivos, aislamiento, fallos del motor, permisos, exportación,
revisión, conflictos, borrado de borradores y recorrido real en Chromium, también móvil.
Eso no demuestra exactitud OCR, lectura de escritura manual, utilidad psicométrica,
validación clínica, integración real con Ollama ni funcionamiento en producción.
La prueba local real descrita arriba sí acredita comunicación con Ollama y el
recorrido sobre esa imagen ficticia concreta, con los demás límites intactos.
