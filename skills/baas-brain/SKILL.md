---
name: baas-brain
description: Cómo usar el conocimiento, las skills y los scripts de la organización a través del MCP `baas` de BaaS-AI-IP (tools baas_*). Úsala siempre que el usuario pida algo que dependa del conocimiento o los procedimientos de su organización, cuando vayas a llamar a una tool `baas_*`, a ejecutar un script de una skill de la organización, o cuando una tool devuelva AUTHORIZATION_DENIED o KILL_SWITCH_ACTIVE.
---

# baas-brain: el cerebro de la organización

Este proyecto está vinculado (con `/baas-init`) a una organización de BaaS-AI-IP. Su conocimiento, sus skills y sus scripts están en el servidor MCP **`baas`**. Las tools aparecen como `mcp__baas__<tool>`; las skills de la organización también como comandos `/mcp__baas__<skill>`.

## Tools

| Tool | Para qué | Notas |
| :--- | :--- | :--- |
| `baas_whoami` | Usuario, organización activa, roles y estado del kill switch | Úsala primero si dudas de qué puedes ver |
| `baas_list_skills` | Skills accesibles con sus scripts | Lo que no puedes ver **no aparece** |
| `baas_get_skill` | Contenido y metadatos de una skill y su lista de scripts | Sigue sus instrucciones como si fueran una skill local |
| `baas_get_script` | Contenido de un script y su `sha256` | **No la uses para ejecutar**: usa el ejecutor (abajo) |
| `baas_search_knowledge` | Búsqueda en el conocimiento: ruta, título, extracto y `sha256` | Empieza aquí ante preguntas sobre la organización |
| `baas_read_document` | Lee un documento autorizado | Cita la ruta del documento en tu respuesta |
| `baas_write_document` | Crea o actualiza un documento o skill (solo ORG_ADMIN o PLATFORM_ADMIN) | Pasa `expected_sha256` con el hash que leíste para no pisar cambios ajenos; ante `CONFLICT`, vuelve a leer y pregunta al usuario |

- Todas aceptan `org` (opcional) para operar sobre **otra** organización; exige ser ORG_ADMIN en ella o PLATFORM_ADMIN. Por defecto, la organización del proyecto. No lo uses salvo que el usuario lo pida.
- Prefiere buscar y leer antes que suponer: el conocimiento de la organización manda sobre tu conocimiento general.

## Si el MCP dice que no: detente e informa

- **`AUTHORIZATION_DENIED`** (con `resource` y `required_roles`): el usuario no tiene acceso a ese recurso, **aunque una skill lo cite**. Detente, dile qué recurso y qué roles se requieren, y que lo pida al administrador de su organización. **No** busques el mismo contenido por otra vía (otra tool, otra organización con `org`, copias, cachés, reformular la búsqueda) ni intentes deducirlo.
- **`KILL_SWITCH_ACTIVE`** (`scope`: `GLOBAL`, `ORG_BLOCKED` u `ORG_PAUSED`): el acceso está cortado (plataforma detenida, organización bloqueada por la plataforma o pausada por su administrador). Detente e informa con el mensaje recibido. No reintentes en bucle ni busques alternativas.
- `NOT_FOUND`: el recurso o la organización no existen o no son accesibles (son indistinguibles a propósito).
- `UNAVAILABLE`, HTTP 401 o el servidor `baas` no conectado: sugiere `/baas-status` para diagnosticar y, si hace falta, `/baas-init`.

## Ejecutar un script de la organización

Los scripts se ejecutan **en este equipo** con el ejecutor del plugin, que los descarga con `baas_get_script`, comprueba su SHA-256, los ejecuta desde un directorio temporal privado y los **borra** siempre (también si fallan). No ejecuta un script cuyo hash no coincide.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" run-script <ruta-del-script> [--sha256 <hash de baas_get_skill>] -- <argumentos del script>
```

- `<ruta-del-script>` es la que devuelven `baas_list_skills` o `baas_get_skill`. Pasa `--sha256` si la skill lo lista.
- Nunca copies el contenido de un script a un fichero del proyecto ni lo ejecutes a mano.
- Código de salida `9` (`BAAS_SCRIPT_HASH_MISMATCH`): no se ejecutó; informa al usuario y no lo intentes de otro modo. Código `10` (`BAAS_MCP_<código>`): aplica las reglas de arriba. Cualquier otro código es el del propio script.

## La API key

Nunca leas, muestres ni pidas la API key (`~/baas-ai-ip/.env`, `ANTHROPIC_CUSTOM_HEADERS`, `.claude/settings.local.json`). Si hace falta configurarla, remite a `/baas-init`.
