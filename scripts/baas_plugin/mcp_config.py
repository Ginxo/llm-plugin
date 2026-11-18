"""Registro del servidor MCP `baas` en Claude Code (ámbito `local`, ver docs/spike-s1.md).

El servidor no lo declara el plugin porque Claude Code renombra los servidores de plugins
(`plugin:baas:<nombre>`) y las skills servidas como prompts no serían `/mcp__baas__<skill>`
(contrato §10). Se registra con el CLI oficial (`claude mcp add-json ... --scope local`), que lo
guarda en `~/.claude.json` solo para este proyecto, con un `headersHelper` que lee la API key de
`~/baas-ai-ip/.env` en cada conexión: la clave no se copia a la configuración de Claude Code.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .envfile import BaasPaths, ensure_private_dir

SERVER_NAME = "baas"
HELPER_NAME = "baas_mcp_headers.py"
_HELPER_SOURCE = Path(__file__).resolve().parent.parent / HELPER_NAME


@dataclass
class RegisterResult:
    registered: bool
    manual_command: str | None = None
    detail: str | None = None


def install_helper(paths: BaasPaths, source: Path) -> Path:
    """Copia un *helper* autónomo a `~/baas-ai-ip/bin/` (ruta estable entre versiones del plugin)."""
    ensure_private_dir(paths.root)
    ensure_private_dir(paths.bin_dir)
    target = paths.bin_dir / source.name
    shutil.copyfile(source, target)
    if os.name == "posix":
        target.chmod(0o700)
    return target


def install_headers_helper(paths: BaasPaths) -> Path:
    return install_helper(paths, _HELPER_SOURCE)


def helper_command(helper: Path, python: str | None = None) -> str:
    # Se ejecuta en un shell: rutas entre comillas. Intérprete absoluto, el mismo que ejecuta el plugin.
    return f"{shlex.quote(python or sys.executable)} {shlex.quote(str(helper))}"


def server_config(mcp_url: str, helper_cmd: str) -> dict[str, str]:
    return {"type": "http", "url": mcp_url, "headersHelper": helper_cmd}


def find_claude() -> str | None:
    explicit = os.environ.get("BAAS_CLAUDE_BIN")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("claude")


def register_mcp_server(project: Path, mcp_url: str, helper_cmd: str) -> RegisterResult:
    config = json.dumps(server_config(mcp_url, helper_cmd))
    args = ["mcp", "add-json", SERVER_NAME, config, "--scope", "local"]
    manual = f"cd {shlex.quote(str(project))} && claude mcp add-json {SERVER_NAME} {shlex.quote(config)} --scope local"
    claude = find_claude()
    if claude is None:
        return RegisterResult(False, manual, "No se encontró el CLI `claude` en el PATH.")
    try:
        # Quitar la entrada anterior hace el registro idempotente (también al cambiar de organización).
        subprocess.run(
            [claude, "mcp", "remove", SERVER_NAME, "--scope", "local"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=60,
        )
        res = subprocess.run([claude, *args], cwd=project, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return RegisterResult(False, manual, f"No se pudo ejecutar `claude`: {exc}")
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["sin detalle"]
        return RegisterResult(False, manual, f"`claude mcp add-json` falló: {detail[0]}")
    return RegisterResult(True)
