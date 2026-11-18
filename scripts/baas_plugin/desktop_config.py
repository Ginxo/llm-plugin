"""Configuración de Claude Desktop en modo gateway hacia el proxy de BaaS-AI-IP.

La pestaña Code de Claude Desktop ignora la `ANTHROPIC_BASE_URL` del proyecto: solo enruta a un
gateway con su «configuración de inferencia de terceros», que es **global a la app** (todas las
sesiones de Desktop pasan por la organización elegida) y sustituye el inicio de sesión de
claude.ai por una credencial del gateway. Aquí esa credencial la da un *helper*
(`baas_desktop_credentials.py`) que lee de `~/baas-ai-ip/.env` la API key de Anthropic (el proxy
la reenvía como `x-api-key`) y la de BaaS (`X-Baas-Api-Key`, que el proxy consume).

El plugin no escribe la configuración de Desktop: la imprime para introducirla en
Developer → Configure Third-Party Inference (o distribuirla por MDM).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .envfile import BaasPaths
from .mcp_config import install_helper

HELPER_NAME = "baas_desktop_credentials.py"
_HELPER_SOURCE = Path(__file__).resolve().parent.parent / HELPER_NAME


def install_credentials_helper(paths: BaasPaths) -> Path:
    return install_helper(paths, _HELPER_SOURCE)


def gateway_config(proxy_anthropic_url: str, helper: Path, *, windows: bool | None = None) -> dict[str, Any]:
    """Claves de la configuración de terceros de Claude Desktop (sin secretos)."""
    windows = os.name == "nt" if windows is None else windows
    config: dict[str, Any] = {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": proxy_anthropic_url,
        "inferenceGatewayAuthScheme": "x-api-key",
        "inferenceCredentialKind": "helper-script",
    }
    if windows:  # sin *shebang*: el intérprete es el ejecutable y el script, su argumento
        config["inferenceCredentialHelper"] = sys.executable
        config["inferenceCredentialHelperArgs"] = [str(helper)]
    else:
        config["inferenceCredentialHelper"] = str(helper)
    return config
