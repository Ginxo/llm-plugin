#!/usr/bin/env python3
"""`headersHelper` del servidor MCP `baas` (ver docs/spike-s1.md).

Claude Code lo ejecuta al conectar con el MCP e imprime en la salida estándar las cabeceras en
JSON: `{"Authorization": "Bearer <API key>"}`. Lee la clave de `~/baas-ai-ip/.env`, de modo que
la clave no se copia a `~/.claude.json` ni a ningún fichero del proyecto.

`/baas-init` copia este fichero a `~/baas-ai-ip/bin/` (ruta estable entre versiones del plugin).
Autónomo a propósito: solo biblioteca estándar y sin importar nada del plugin.
"""

import json
import os
import sys


def read_key(env_path: str) -> str:
    with open(env_path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            if line.startswith("BAAS_API_KEY="):
                value = line.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                return value
    return ""


def main() -> int:
    env_path = os.path.join(os.path.expanduser("~"), "baas-ai-ip", ".env")
    try:
        key = read_key(env_path)
    except OSError:
        key = ""
    if not key:
        sys.stderr.write("baas: no hay BAAS_API_KEY en ~/baas-ai-ip/.env; lanza /baas-init\n")
        return 1
    sys.stdout.write(json.dumps({"Authorization": f"Bearer {key}"}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
