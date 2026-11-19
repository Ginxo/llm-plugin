# /baas-init guiado: vinculación sin terminal ni claves a mano

Fecha: 2026-11-19 · Estado: borrador para aprobar · Ámbito: `llm-plugin` y `webapp` (con retoques en `deploy` y e2e)

## Intención

Una persona **no técnica** instala el plugin, abre Claude, lanza `/baas-init` y queda conectada a su
organización. Responde solo lo imprescindible; el resto lo hace el sistema.

Hoy hay que crear una API key en la webapp, guardarla con `baas.py set-key` en una terminal aparte,
y lanzar el `init`. Si algo falla (clave revocada, URL equivocada, hook que bloquea el propio
`/baas-init`), el usuario se queda sin salida.

### Máxima del cambio

> Proceso inequívoco, amistoso y sin pasos técnicos: nada de ejecutar scripts, editar ficheros ni
> copiar claves. Cada fallo tiene su salida, expresada en lenguaje llano y con una sola acción
> siguiente. «Un proceso para tontos.»

Se usa como criterio de revisión: todo mensaje al usuario dice **qué ha pasado, qué hacer ahora y
nada más**, sin nombres de ficheros, códigos ni jerga (proxy, handshake, hook).

## Objetivos

1. `/baas-init` completa la vinculación con, como mucho, estas intervenciones del usuario:
   aprobar en el navegador y reiniciar Claude Code.
2. El usuario nunca ve ni teclea una API key. La clave no pasa por el chat.
3. Cada situación que impide seguir tiene un camino de salida explícito (ver «Fallos y salidas»).
4. Funciona igual en el CLI, en la extensión del IDE y, con su limitación conocida, en Claude Desktop.
5. Las claves y vinculaciones existentes siguen funcionando.

## No objetivos

- Cambiar el modelo de claves (PERSONAL, expiración, revocación) ni el proxy.
- Alta de usuarios u organizaciones: eso sigue en la webapp.
- Resolver Claude Desktop (ignora la Base URL del proyecto): solo se explica bien (ver fallos).

## Antiobjetivos (sería un fracaso aunque «funcione»)

- Pedir al usuario la clave, la contraseña o un TOTP en el chat.
- Dejar un mensaje con pasos técnicos («edita `.env`», «ejecuta `baas.py`…»).
- Que un bloqueo del guard impida arreglar el propio problema.
- Sobrescribir en silencio una vinculación válida de otro proyecto.

## Cómo es el recorrido

1. El usuario escribe `/baas-init`.
2. Claude responde con una frase y un enlace: «Abre este enlace para conectar este proyecto con tu
   organización» y un código corto de 8 caracteres (p. ej. `KQRT-7MWB`). El plugin intenta abrir el
   navegador por su cuenta; el enlace es el plan B.
3. En la webapp (con su sesión, o iniciando sesión), el usuario ve: «¿Conectar *Claude Code en
   <equipo/proyecto>* a <organización>?», comprueba el código y pulsa **Aprobar**. Si pertenece a
   varias organizaciones, la elige ahí.
4. El plugin, que espera la aprobación sondeando el servidor, la recibe, guarda la credencial
   (permisos 0600, fuera del control de versiones) y escribe la configuración del proyecto.
5. Claude responde: «Listo. Cierra y vuelve a abrir Claude Code en esta carpeta, y acepta la
   confianza». Tras reiniciar, `/baas-status` confirma «Conectado a <organización>».

## Fallos y salidas

Cada fila es un requisito: debe tener mensaje propio y prueba.

| Situación | Qué ve el usuario | Salida |
| :--- | :--- | :--- |
| La URL del servidor no existe o no responde | «No encuentro el servidor de tu organización en `<url>`.» | Se le pide la dirección correcta (campo de texto en Claude) con la actual precargada; se prueba antes de guardar; se puede reintentar o cancelar. |
| No hay URL conocida (primer uso, sin valor por defecto) | «¿Cuál es la dirección de BaaS-AI-IP de tu organización?» | Misma pregunta; la dirección que funcione se recuerda. |
| La clave guardada ya no es válida (revocada, base de datos nueva, caducada) | «Tu conexión anterior ya no es válida. Vamos a renovarla.» | Se descarta la clave y arranca el recorrido normal sin pedir nada más. |
| El usuario no aprueba a tiempo / el código caduca | «El enlace ha caducado.» | Se ofrece generar otro en un paso. |
| El usuario rechaza en la webapp | «Se ha cancelado la conexión.» | Se puede repetir. |
| Pertenece a varias organizaciones | Elige en la webapp (o en Claude si ya llegó la lista) | Una sola elección. |
| No pertenece a ninguna organización | «Tu cuenta aún no tiene organización. Pide acceso a un administrador.» | Nada más que hacer; mensaje con quién avisar si el servidor lo informa. |
| Sin red | «No tengo conexión con internet o con el servidor.» | Reintentar. |
| Demasiados intentos (límite de tasa) | «Demasiados intentos. Espera un minuto.» | Reintento automático tras la espera. |
| La sesión no pasa por el proxy (p. ej. Claude Desktop o carpeta sin confiar) | Mensaje corto que dice qué hacer en su caso (reiniciar y aceptar la confianza, o usar el CLI/IDE) | El guard **deja pasar `/baas-init` y `/baas-status`** aunque no haya proxy; solo bloquea el resto. |
| Fichero local corrupto o versionado en git | «He encontrado una configuración dañada; la repara el asistente.» | Se hace copia y se regenera, con aviso; nunca pedir al usuario que lo arregle. |
| Ya está conectado y todo va bien | «Ya estás conectado a <organización>. ¿Cambiar de organización?» | Cambiar o salir. |

## Enfoques considerados

- **A. Aprobación en navegador con código de dispositivo (elegido).** El plugin pide un código al
  servidor, el usuario lo aprueba en la webapp y el plugin recoge una clave personal creada para ese
  equipo. Sin copiar nada; la clave no pasa por el chat. Coste: endpoints y una pantalla nuevos en
  la webapp, y sondeo en el plugin.
- **B. Formulario dentro del chat.** Pedir usuario, contraseña o clave en la conversación. Más
  simple, pero rompe la regla de que ningún secreto pasa por el chat. Descartado.
- **C. Dejar el flujo actual y mejorar los mensajes.** Barato, pero no cumple la máxima: sigue
  habiendo creación de clave y terminal. Descartado.

## Decisiones tomadas

(Las marcadas **[mía]** son decisiones del diseñador, no del usuario; conviene revisarlas.)

- **Sin scripts a mano.** El usuario no ejecuta nada. **[mía]** Por dentro el plugin sigue usando
  código propio (Bash permitido por la skill, o una herramienta de un MCP del plugin si encaja mejor
  en la sesión); lo importante es que no se le pida ni se le enseñe. El constructor decide cuál.
- **Credencial resultante:** una clave PERSONAL nueva, nombrada «Claude Code · <equipo/proyecto>»,
  visible y revocable en «Mis API keys». **[mía]**
- **Código de dispositivo:** 8 caracteres sin ambigüedad (sin 0/O/1/I), caduca a los 10 minutos, un
  solo uso, limitado por IP. **[mía]**
- **Dirección por defecto:** valor del plugin, sustituible por variable de entorno (desarrollo:
  `localhost`). Si no hay ninguna, se pregunta una vez y se recuerda. **[mía]**
- **Compatibilidad:** las claves existentes y `set-key` siguen funcionando; el flujo nuevo se
  añade, no reemplaza. **[mía]**
- **Guard:** `/baas-init` y `/baas-status` pasan siempre. Es un cambio acotado en `baas_guard.py`,
  que debe seguir siendo autónomo y sin romper la sesión de quien no use BaaS.
- **Servidor:** tres operaciones, acotadas a un flujo de autorización de dispositivo:
  iniciar (devuelve código y enlace), consultar (el plugin sondea) y aprobar (la webapp, con sesión
  del usuario). Reutiliza `load_active_key`, límites de tasa y modelo de claves existentes.
- **Webapp:** una página de aprobación en español, accesible, con el código visible, la
  organización y botones Aprobar/Cancelar.

## Seguridad (restricciones)

- El código de dispositivo sirve solo para esta aprobación; no es una credencial.
- La clave se entrega una sola vez al plugin que inició el flujo (vinculada a un secreto de sondeo
  que solo él conoce) y nunca se escribe en logs ni en la salida.
- La página de aprobación exige sesión y muestra con claridad qué equipo pide acceso, para evitar
  que alguien aprueba sin saber qué (phishing de código).
- Se registran en auditoría la creación y la aprobación (sin el secreto).

## Para el constructor (decide él)

- Forma exacta de los endpoints y su esquema, almacenamiento temporal del código (Redis ya existe).
- Cómo abrir el navegador de forma portable (Linux/macOS/Windows) y qué hacer si no hay.
- Si el sondeo corre en el script de la skill o en un MCP del plugin.
- Textos finales (el tono ya está fijado arriba) y su idioma si el usuario usa otro.
- Cómo probar el recorrido completo en e2e (webapp real contra proveedor falso).

## Criterios de éxito

- Un usuario nuevo llega a «Conectado» tras: `/baas-init`, un clic de aprobación y un reinicio.
- Cada fila de «Fallos y salidas» tiene una prueba automática (unitaria o e2e).
- Ningún mensaje contiene rutas de ficheros ni comandos para el usuario.
- Revisión final: alguien sin conocimientos técnicos completa el recorrido sin ayuda.
