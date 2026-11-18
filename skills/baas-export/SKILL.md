---
name: baas-export
description: Exporta una skill del MCP de la organización de BaaS-AI-IP a un directorio (la inversa de /baas-publish), verificando el sha256 de cada fichero y restaurando el bit de ejecución. El resultado es una skill válida en cualquier cliente. Úsala solo cuando el usuario lance /baas-export o pida descargar, exportar o sacar una skill del MCP a una carpeta.
argument-hint: "<skill> <directorio>"
disable-model-invocation: true
allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" export *) mcp__baas__baas_list_skills
---

# /baas-export

Baja el paquete completo de una skill (`SKILL.md`, scripts, plantillas, contexto y `baas.yaml`) a una carpeta local. Cada fichero se verifica contra su `sha256` **antes** de escribir nada.

## Si el MCP dice que no: detente e informa

`AUTHORIZATION_DENIED` o `KILL_SWITCH_ACTIVE` (salida `[BAAS_MCP_…]`, código 10): **detente**, di al usuario que no tiene acceso a esa skill o que el acceso está cortado, y no busques otra vía.

## Pasos

1. Obtén del argumento `$ARGUMENTS` (o pregunta) el **nombre de la skill** y el **directorio de destino**. Si no sabe el nombre, lista las que puede ver con `baas_list_skills`.
2. Ejecuta:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" export <skill> <directorio> --project "${CLAUDE_PROJECT_DIR}"
   ```

3. Según la salida:

   | Salida | Qué hacer |
   | :--- | :--- |
   | `0` | Resume: ficheros exportados, ejecutables restaurados y que cada hash se ha verificado. El directorio es una skill válida en Claude Code, Cursor u otros; `baas.yaml` es del MCP y los demás clientes lo ignoran (`--no-manifest` lo omite). |
   | `2` destino no vacío | Pregunta si quiere otra carpeta o `--force` (escribe encima de los ficheros del paquete y respeta los demás). |
   | `7` `[BAAS_PROJECT_UNSAFE]` | El paquete trae una ruta insegura (`..`, absoluta) o el destino tiene un enlace simbólico. No se ha escrito nada: informa y no lo rodees. |
   | `9` `[BAAS_PACKAGE_HASH_MISMATCH]` | Un fichero no coincide con su hash. No se ha escrito nada: informa al usuario. |
   | `10` `NOT_FOUND` | La skill no existe o no puede verla; muestra las disponibles con `baas_list_skills`. |
   | `10` `AUTHORIZATION_DENIED` / `KILL_SWITCH_ACTIVE` | Detente e informa (ver arriba). |

Nunca leas ni muestres `~/baas-ai-ip/.env` ni la API key. Con `--org <slug>` se exporta de otra organización (exige ser administrador en ella).
