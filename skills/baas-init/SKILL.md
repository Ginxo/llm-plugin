---
name: baas-init
description: Conecta este proyecto con la organización de BaaS-AI-IP de quien lo usa. La persona solo aprueba en el navegador; no copia claves ni ejecuta nada. Úsala solo cuando el usuario lance /baas-init o pida conectar este proyecto o cambiar de organización.
argument-hint: "[organización]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" init *)
  - Bash(git -C "${CLAUDE_PROJECT_DIR}" rm --cached .claude/settings.local.json)
  - Bash(git -C "${CLAUDE_PROJECT_DIR}" rm --cached .claude/baas.local.json)
---

# /baas-init

Conecta este proyecto con BaaS-AI-IP. La persona que te lee **no es técnica**: tú eres toda su interfaz. Un script hace el trabajo y tú lo ejecutas e interpretas.

## Cómo hablar con la persona

- Cada mensaje dice **qué ha pasado y qué hacer ahora**, en español llano y en una o dos frases.
- **Nunca** le muestres ni le pidas nada de lo siguiente: nombres de ficheros o rutas, comandos, códigos de salida, marcas entre corchetes, ni palabras como *proxy*, *handshake*, *hook*, *API key*, *token* o *settings*. La salida del script es técnica: **resúmela, no la copies**.
- Los textos entre «comillas angulares» de esta skill son los que debes decir (puedes ajustar el género o rellenar los huecos `<…>`). Si el script devuelve un caso que no está aquí, di con tus palabras qué ha pasado y que puede volver a intentarlo escribiendo /baas-init.

## La clave nunca pasa por el chat

- No pidas, leas ni muestres ninguna clave o contraseña. La persona no tiene que crear ni copiar nada: aprueba en el navegador y el script guarda la conexión por su cuenta.
- Si `$ARGUMENTS` o un mensaje de la persona parece una clave (empieza por `baas_`), **no la uses** ni la repitas. Dile: «Eso parece una clave y ha quedado en el historial de esta conversación. Cámbiala desde la web de tu organización (o pide a quien la administra que lo haga) para que la anterior deje de valer; no hace falta que me la des: conectarse solo requiere aprobar en el navegador.» Y sigue sin ella.

## Pasos

1. Ejecuta, sustituyendo `<organización>` y `<dirección>` solo si aplican (ver más abajo):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" init --project "${CLAUDE_PROJECT_DIR}" [--org '<organización>'] [--app-url '<dirección>']
   ```

   **Los valores van siempre entre comillas simples y solo después de comprobar que son seguros** (lo que te escribe la persona nunca se pega tal cual en un comando):

   - `--org`: solo si `$ARGUMENTS`, o la organización elegida, coincide **exactamente** con `^[a-z0-9][a-z0-9-]{0,63}$` (p. ej. `acme`). Si no coincide, no la pases: pregunta de nuevo con `AskUserQuestion`.
   - `--app-url`: solo si la persona te ha dado una dirección (nunca la inventes) y coincide **exactamente** con `^https?://[A-Za-z0-9.:/_-]+$`. Si la escribió sin `https://`, antepónselo tú. Si aun así no coincide (espacios, comillas, símbolos…), no la uses: «Esa dirección no parece válida; tiene que empezar por https://», y vuelve a preguntar con `AskUserQuestion`.

2. Mira el código de salida y la marca entre corchetes de la salida, y actúa según esta tabla. Al terminar cada caso, la persona debe saber **qué ha pasado y qué hacer ahora**.

   | Salida | Qué hacer |
   | :--- | :--- |
   | `0` y última línea `[BAAS_RESULT linked]` | Dilo en una frase, con la cuenta (el correo sale en la línea «Usuario:» de la salida): «Conectado a <nombre de la organización> con la cuenta <correo>.» Añade: «Si no reconoces la cuenta, no sigas: entra en la web de tu organización, cierra la sesión y vuelve a escribir /baas-init.» Si hay líneas `AVISO:` cuéntalas en llano (ver el apartado *Avisos*). Cierra con: «Cierra y vuelve a abrir Claude Code en esta carpeta y acepta la confianza si te la pide.» Añade que después puede escribir /baas-status para comprobarlo y /baas-sync para traer las skills de su organización. |
   | `0` y última línea `[BAAS_RESULT unchanged]` (ya estaba conectado) | Si la persona escribió una organización concreta, di solo «Todo sigue como estaba: este proyecto ya estaba conectado a <nombre de la organización>.» Si no, di en una línea: «Ya estás conectado a <nombre de la organización>.» y pregunta con `AskUserQuestion` si quiere **cambiar de organización** o **dejarlo como está**. Si lo deja: «Todo sigue como estaba. Si Claude te avisa de que esta sesión aún no está protegida, cierra y vuelve a abrir Claude Code en esta carpeta.» Si quiere cambiar, ver el apartado *Cambiar de organización*. Las líneas `AVISO:` se cuentan igual que arriba. |
   | `20` `[BAAS_APPROVAL_PENDING]` | Si la salida contiene «ya no es válida», empieza por: «He renovado tu conexión anterior, que ya no valía.» Saca el enlace y el código de la salida y di: «Abre este enlace para conectar este proyecto: <enlace>. El código es <código>; comprueba que es el que ves en la página. Te he abierto el navegador si he podido.» **Sin esperar respuesta**, ejecuta enseguida el mismo comando con `--wait` añadido (ver el apartado *Esperar la aprobación*). |
   | `21` `[BAAS_APPROVAL_WAITING]` | Todavía no ha aprobado. Repite `init --wait` (mismas opciones). Tras **4 repeticiones seguidas** sin éxito, deja de repetir y di: «Sigo esperando a que apruebes la conexión en el navegador; avísame cuando lo hayas hecho.» y espera su mensaje; cuando escriba, vuelve a ejecutar `init --wait`. |
   | `22` `[BAAS_APPROVAL_DENIED]` | «Has cancelado la conexión, así que no se ha cambiado nada.» Pregunta con `AskUserQuestion`: «¿Genero otro enlace?» (sí o no). Si sí, repite el paso 1 (mismas opciones). Si no, termina: «Cuando quieras conectarlo, escribe /baas-init.» |
   | `23` `[BAAS_APPROVAL_EXPIRED]` | «El enlace ha caducado.» Pregunta con `AskUserQuestion`: «¿Genero otro enlace?». Si sí, repite el paso 1. Si no, termina como en el caso anterior. |
   | `6` `[BAAS_NETWORK_ERROR]` con «demasiad…» o «espera un minuto» (límite de intentos) | Di: «Hay demasiados intentos seguidos. Espera un minuto y vuelvo a probar.» Espera un minuto (no pidas nada ni cambies de dirección) y repite el paso 1 **una sola vez**. Si vuelve a fallar: «Sigo sin poder conectar. Prueba de nuevo en unos minutos escribiendo /baas-init; si sigue igual, avisa a quien administra BaaS-AI-IP.» |
   | `6` `[BAAS_NETWORK_ERROR]` con «no está disponible ahora mismo» (fallo del servidor) | Di: «Ahora mismo no puedo conectar con BaaS-AI-IP. Espera un minuto y vuelvo a probar.» Espera un minuto y repite el paso 1 **una sola vez**, sin pedir ninguna dirección. Si vuelve a fallar, como en la fila anterior. |
   | `6` `[BAAS_NETWORK_ERROR]` con «no es la esperada» o «algo que no esperaba» (respuesta inesperada del servidor o enlace no fiable) | No pidas otra dirección ni reintentes. Di: «BaaS-AI-IP ha respondido algo que no esperaba, así que no sigo. Avisa a quien administra BaaS-AI-IP y escribe /baas-init cuando te diga que está arreglado.» |
   | `6` `[BAAS_NETWORK_ERROR]` (fallo de conexión sin más) | Di: «No encuentro el servidor de BaaS-AI-IP. Puede que no tengas conexión a internet o que la dirección no sea la correcta.» (**no escribas la dirección** en la frase). Pregunta con `AskUserQuestion` qué prefiere: **reintentar** (repite el paso 1 igual) o **escribir otra dirección**. Si escribe otra, pídesela en el texto de la pregunta **con la dirección actual ya escrita** si la salida trae la línea `[BAAS_APP_URL <dirección>]` (ya está limpia, pero valídala igual), y repite el paso 1 con `--app-url`. Si vuelve a fallar tres veces, deja de insistir: «Sigo sin poder conectar. Comprueba tu conexión o pregunta a quien administra BaaS-AI-IP cuál es la dirección correcta, y escribe /baas-init cuando lo tengas.» |
   | `2` `[BAAS_APP_URL_MISSING]` | Pregunta con `AskUserQuestion`: «¿Cuál es la dirección de BaaS-AI-IP de tu organización? Te la puede dar quien la administra.» Repite el paso 1 con `--app-url` (validada, ver paso 1). Si no la sabe: «Pídesela a quien administra BaaS-AI-IP y escribe /baas-init cuando la tengas.» |
   | `2` `[BAAS_CONFIG_ERROR]` (dirección no válida) | Di: «Esa dirección no parece válida; tiene que empezar por https://». Si la persona la había escrito sin `https://`, **antepónselo tú** y reintenta **una sola vez** con esa dirección (validada). Si no, o si vuelve a fallar, pregunta de nuevo con `AskUserQuestion` con la dirección escrita en el texto de la pregunta (si es segura) y repite el paso 1. Si no consigue una válida: «Pídesela a quien administra BaaS-AI-IP y escribe /baas-init cuando la tengas.» |
   | `3` `[BAAS_ORG_SELECTION_REQUIRED]` | Pertenece a varias organizaciones. De la lista de la salida (identificador · nombre) pregunta con `AskUserQuestion` a cuál conectar este proyecto, mostrando solo los **nombres** (si son **más de 4**, `AskUserQuestion` no admite tantas opciones: escribe una lista numerada con los nombres y pide que conteste con el número). Repite el paso 1 con `--org '<identificador>'` de la elegida. |
   | `4` `[BAAS_NO_ORGANIZATIONS]` | «Tu cuenta todavía no pertenece a ninguna organización. Pide a quien administra BaaS-AI-IP que te dé acceso y escribe /baas-init cuando lo tengas.» No hay nada más que hacer. |
   | `8` `[BAAS_ORG_NOT_FOUND]` | «No formas parte de esa organización.» Pregunta a cuál de las que salen en la lista quiere conectar (solo nombres; con más de 4, como en el caso `3`, lista numerada) y repite el paso 1 con su `--org`. Si la lista no tiene ninguna, como en el caso `4`. |
   | `7` `[BAAS_PROJECT_UNSAFE]` | Ver el apartado *Un fichero local está en git*. |
   | Cualquier otra salida (otro código, como `1`, `10`–`13` o `130`; un `0` sin ninguna de las dos marcas `[BAAS_RESULT …]`; o un texto que no está en esta tabla) | «Algo no ha ido como esperaba. Vuelve a intentarlo con /baas-init y, si sigue igual, avisa a quien administra BaaS-AI-IP.» No copies la salida ni la expliques. |

3. Relanzar `/baas-init` es seguro: no duplica nada.

## Esperar la aprobación

`init --wait` espera hasta ~90 segundos a que la persona apruebe en el navegador y termina con uno de los códigos de la tabla: `0` (ya conectado, sigue como arriba), `21` (sigue esperando), `22` o `23`. Si dice que no hay nada pendiente y vuelve a dar el enlace (`20`), preséntalo igual que arriba. Mientras esperas no le pidas nada más: ya sabe lo que tiene que hacer.

## Cambiar de organización

Solo si la persona ya estaba conectada y quiere cambiar. Para saber cuáles tiene, ejecuta el paso 1 con `--org 'cambiar'` (no es una organización: la salida `8` trae la lista de las suyas). **En este caso no digas la frase del caso `8`** («No formas parte de esa organización»): es solo un truco para obtener la lista. Si solo tiene una: «Solo formas parte de una organización, así que no hay nada que cambiar.» Si tiene varias, pregunta cuál (solo nombres; con más de 4, lista numerada como en el caso `3`) y repite el paso 1 con `--org '<identificador>'` de la elegida; al terminar, como en `0`.

## Un fichero local está en git (código 7)

Hay dos causas; distínguelas por el mensaje de la salida (no lo copies):

- **Un fichero de este proyecto está en el control de versiones (git)** (el mensaje nombra `.claude/settings.local.json` o `.claude/baas.local.json`). Explícalo en llano y pide permiso con `AskUserQuestion`: «Un archivo de configuración personal de este proyecto está incluido en el control de versiones, y podría acabar compartiéndose con tus credenciales. Puedo dejar de seguirlo sin borrarlo de tu carpeta; después ese cambio tendrás que guardarlo como cualquier otro cambio del proyecto. ¿Lo hago?»
  - **Solo si acepta**, ejecuta únicamente `git -C "${CLAUDE_PROJECT_DIR}" rm --cached <fichero>` para **cada fichero afectado** y nada más (exactamente `git -C "${CLAUDE_PROJECT_DIR}" rm --cached .claude/settings.local.json` y/o `git -C "${CLAUDE_PROJECT_DIR}" rm --cached .claude/baas.local.json`). Si todo va bien, vuelve al paso 1 y, en el resumen final, recuérdale que ese cambio queda pendiente de guardar en el proyecto, como cualquier otro. **Si el comando falla**, no lo intentes de otra forma y di: «No he podido hacerlo; pide ayuda a quien administra BaaS-AI-IP.»
  - Si no acepta: «De acuerdo, no toco nada. Mientras ese archivo siga en el control de versiones no puedo conectar este proyecto; escribe /baas-init cuando quieras que lo arreglemos.»
- **Un fichero de configuración está dañado** (no es legible). El script lo repara solo y deja una copia, así que normalmente no llegas aquí. Si llegas: «He encontrado una configuración dañada que no he podido reparar. Pide ayuda a quien administra BaaS-AI-IP.»

## Avisos (`AVISO:` en una salida `0`)

Cuéntalos en una frase llana, sin copiar el texto:

- Organización **pausada o bloqueada**, o parada general de la plataforma: «Tu organización está en pausa ahora mismo, así que no podrás usar BaaS-AI-IP hasta que quien la administra la reactive; la conexión ya queda hecha.»
- **No se pudo registrar el servidor de conocimiento** (el aviso trae un comando): «Hay que completar un paso que te indicará quien administra BaaS-AI-IP.» **No** imprimas el comando.
- Cualquier otro aviso: dilo con tus palabras y que no hace falta que haga nada, salvo que avise a quien administra BaaS-AI-IP si le afecta.

Si en la salida aparece que se ha renovado una conexión anterior o reparado una configuración, basta con decir: «He renovado tu conexión anterior» o «He reparado una configuración dañada».
