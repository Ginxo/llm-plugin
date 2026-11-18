#!/usr/bin/env python3
"""Helper de credenciales de Claude Desktop en modo gateway (`inferenceCredentialHelper`).

Claude Desktop lo ejecuta para obtener la credencial con la que habla con el gateway, que aquí es
el proxy de BaaS-AI-IP. Imprime en la salida estándar:

    {"token": "<API key de Anthropic>", "headers": {"X-Baas-Api-Key": "<API key de BaaS>"}}

Desktop envía el `token` como `x-api-key` (`inferenceGatewayAuthScheme: x-api-key`) y el proxy lo
reenvía intacto a Anthropic; la cabecera `X-Baas-Api-Key` identifica al usuario ante el proxy, que
la quita antes de reenviar. Ambas claves se leen de `~/baas-ai-ip/.env` en cada ejecución, así que
no se copian a la configuración de Desktop.

`baas.py desktop-config` copia este fichero a `~/baas-ai-ip/bin/` (ruta estable entre versiones).
Autónomo a propósito: solo biblioteca estándar y sin importar nada del plugin.
"""

import json
import os
import sys


def read_env(env_path: str) -> dict:
    values = {}
    with open(env_path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def main() -> int:
    env_path = os.path.join(os.path.expanduser("~"), "baas-ai-ip", ".env")
    try:
        values = read_env(env_path)
    except OSError:
        values = {}
    baas_key = values.get("BAAS_API_KEY", "")
    anthropic_key = values.get("ANTHROPIC_API_KEY", "")
    missing = [name for name, value in (("BAAS_API_KEY", baas_key), ("ANTHROPIC_API_KEY", anthropic_key)) if not value]
    if missing:
        sys.stderr.write(
            f"baas: faltan {', '.join(missing)} en ~/baas-ai-ip/.env; guárdalas con `baas.py set-key`"
            " (y `set-key --anthropic` para la de Anthropic)\n"
        )
        return 1
    sys.stdout.write(json.dumps({"token": anthropic_key, "headers": {"X-Baas-Api-Key": baas_key}}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
