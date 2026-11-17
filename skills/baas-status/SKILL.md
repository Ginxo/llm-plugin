---
name: baas-status
description: Muestra el estado de BaaS-AI-IP en este proyecto (usuario, organización vinculada, roles, kill switch global, bloqueo, pausa, AI-IP, proxy y MCP). Úsala cuando el usuario lance /baas-status o cuando una tool del MCP `baas` o el proxy fallen y haya que diagnosticar por qué.
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" status *)
---

# /baas-status

1. Ejecuta:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" status --project "${CLAUDE_PROJECT_DIR}"
   ```

2. Resume al usuario, en pocas líneas: usuario y rol de plataforma; organización del proyecto; rol de gobierno y roles funcionales; kill switch global, bloqueo de plataforma y pausa; AI-IP; si el proxy está activo en esta sesión y si el MCP `baas` responde. Destaca cada línea `AVISO:`.

3. Si la salida trae un error entre corchetes (`[BAAS_KEY_MISSING]`, `[BAAS_AUTH_ERROR]`…), explica la causa y propón `/baas-init`. Nunca pidas ni muestres la API key.

Con un kill switch activo (global, bloqueo o pausa), el MCP y el proxy rechazan las peticiones: no intentes rodearlo; informa al usuario.
