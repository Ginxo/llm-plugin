---
name: baas-init
description: Vincula este proyecto a una organización de BaaS-AI-IP (handshake con la webapp, proxy de Anthropic y servidor MCP `baas`). Úsala solo cuando el usuario lance /baas-init o pida configurar o cambiar la organización del proyecto.
argument-hint: "[slug-de-organización]"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" init *)
---

# /baas-init

Configura este proyecto para BaaS-AI-IP. El trabajo lo hace un script; tú lo ejecutas e interpretas su salida.

## Regla de oro: la API key nunca pasa por el chat

- **No pidas nunca la API key en la conversación**, no la leas de `~/baas-ai-ip/.env` ni de `.claude/settings.local.json`, y no la muestres.
- Si el argumento `$ARGUMENTS` o un mensaje del usuario parece una API key (empieza por `baas_`), **no la uses**: dile que ya ha quedado en el historial de la conversación, que la **revoque en la webapp** (Mis API keys), cree otra y la guarde con `set-key` (abajo).

## Pasos

1. Ejecuta (si `$ARGUMENTS` es un slug de organización, añade `--org <slug>`; si no, nada más):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" init --project "${CLAUDE_PROJECT_DIR}"
   ```

2. Según el código de salida y la marca entre corchetes de la salida:

   | Salida | Qué hacer |
   | :--- | :--- |
   | `0` | Resume al usuario la organización, las URLs y los `AVISO:` (kill switch, MCP sin registrar). Dile que **reinicie Claude Code** en este proyecto (y acepte la confianza en la carpeta) para cargar el MCP `baas` y el proxy, que lo compruebe con `/baas-status` y que lance `/baas-sync` para traer las skills de la organización. |
   | `2` `[BAAS_KEY_MISSING]` | Falta la API key. Pide al usuario que la guarde **fuera del chat**, en una terminal aparte: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" set-key --app-url <URL de la webapp>` (la pide sin mostrarla y la guarda en `~/baas-ai-ip/.env` con permisos 0600), o que edite ese fichero con la línea `BAAS_API_KEY=...`. Cuando confirme, vuelve al paso 1. |
   | `2` `[BAAS_APP_URL_MISSING]` | Pregunta la URL de la webapp (p. ej. `https://app.<dominio>`; no es secreta) y repite el paso 1 con `--app-url <URL>`. |
   | `3` `[BAAS_ORG_SELECTION_REQUIRED]` | El usuario pertenece a varias organizaciones: muéstrale la lista y pregúntale cuál vincular (AskUserQuestion si está disponible). Repite el paso 1 con `--org <slug>`. |
   | `4` `[BAAS_NO_ORGANIZATIONS]` | Avisa de que no pertenece a ninguna organización y de que debe pedir acceso a un administrador. No hay nada más que hacer. |
   | `5` `[BAAS_AUTH_ERROR]` | La clave no es válida o está revocada: que cree otra en la webapp y la guarde con `set-key`. |
   | `6` `[BAAS_NETWORK_ERROR]` | Sin red, URL incorrecta o servidor no disponible / límite de tasa: muestra el mensaje y sugiere reintentar. |
   | `7` `[BAAS_PROJECT_UNSAFE]` | Un fichero local está versionado o no es JSON válido: muestra el mensaje y la solución propuesta. **No** lo arregles por tu cuenta sin que el usuario lo pida. |
   | `8` `[BAAS_ORG_NOT_FOUND]` | El slug pedido no es una de sus organizaciones: muestra las disponibles. |

3. Relanzar `/baas-init` es seguro: actualiza la configuración sin duplicar nada. Para **cambiar de organización**: `/baas-init <slug>`.

Qué escribe el script (todo local y fuera del control de versiones): `~/baas-ai-ip/.env` (clave, 0600), `.claude/settings.local.json` (`ANTHROPIC_BASE_URL` y la cabecera `X-Baas-Api-Key`), `.claude/baas.local.json` (organización y URLs), entradas en `.gitignore` y el servidor MCP `baas` en el ámbito *local* de Claude Code, con un `headersHelper` que lee la clave de `~/baas-ai-ip/.env`.
