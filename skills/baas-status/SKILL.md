---
name: baas-status
description: Muestra si este proyecto está conectado a BaaS-AI-IP y en qué estado (organización, permisos, pausa o bloqueo, protección de la sesión y skills de la organización al día, desfasadas o retiradas). Úsala cuando el usuario lance /baas-status o cuando algo de BaaS-AI-IP falle y haya que averiguar por qué.
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" status *)
---

# /baas-status

La persona que te lee **no es técnica**: dile cómo está todo en pocas frases llanas y qué hacer ahora. La salida del script es técnica (rutas, direcciones, nombres de ficheros): **resúmela, no la copies**. No menciones ficheros, comandos, códigos ni palabras como *proxy*, *API key*, *token* o *settings*, y no pidas ni muestres nunca ninguna clave.

1. Ejecuta:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" status --project "${CLAUDE_PROJECT_DIR}"
   ```

2. Resume en pocas líneas: quién eres y a qué organización está conectado el proyecto; qué puedes hacer en ella (tu papel y áreas); si la organización o la plataforma están en pausa o bloqueadas; si el análisis de datos sensibles de la organización está activo; si esta sesión está protegida y si el servidor de conocimiento responde; y las skills de la organización en este equipo, en palabras sencillas: **al día**, **hay una versión nueva**, **retiradas** (se quitarán en la siguiente sincronización), **pendientes de traer**. Si hay alguna que no esté al día, propón `/baas-sync`. Si todo va bien, dilo en una línea: «Todo en orden: estás conectado a <nombre de la organización>.»

3. Casos que debes explicar en llano:

   - **«Esta sesión aún no está protegida»** (el aviso habla de proxy o de que la sesión no pasa por él): «Tu sesión aún no está protegida. Cierra Claude Code, vuelve a abrirlo en esta carpeta y acepta la confianza si te la pide. Si sigue igual, escribe /baas-init.» Si el aviso habla de la pestaña Code de Claude Desktop: «Estás usando la pestaña Code de Claude Desktop, que no admite la protección de BaaS-AI-IP. Usa Claude Code en el terminal o en tu editor.»
   - **El proyecto no está conectado, o la conexión ya no vale** (salida con `[BAAS_KEY_MISSING]`, `[BAAS_AUTH_ERROR]` o «no está vinculado»): «Este proyecto todavía no está conectado a BaaS-AI-IP (o la conexión ha dejado de valer). Escribe /baas-init y apruebas en el navegador.»
   - **Ya no tienes acceso a la organización del proyecto**: «Ya no tienes acceso a la organización a la que estaba conectado este proyecto. Escribe /baas-init para elegir otra.»
   - **Falta la dirección de BaaS-AI-IP** (`[BAAS_APP_URL_MISSING]`): «Escribe /baas-init: te preguntaré la dirección de BaaS-AI-IP de tu organización.»
   - **No hay conexión con el servidor** (`[BAAS_NETWORK_ERROR]`): «No consigo conectar con BaaS-AI-IP. Comprueba tu conexión a internet e inténtalo de nuevo en unos minutos.»
   - **Organización en pausa o bloqueada, o parada general** (el aviso habla de kill switch, pausa o bloqueo): «Tu organización está en pausa o bloqueada ahora mismo; mientras tanto no podrás usar BaaS-AI-IP. Habla con quien la administra.» No intentes rodearlo; informa a la persona.
   - **La configuración local no coincide con la organización**: «La configuración de este proyecto no coincide con tu organización. Escribe /baas-init para arreglarlo.»

   Ante cualquier otro error, di con tus palabras qué ha pasado y que puede volver a intentarlo o escribir /baas-init.
