"""Ejecutor de scripts del MCP (fase-1-mvp §2.1, contrato §10, riesgo S6).

Mecanismo:
1. `baas_get_script` por HTTP con el cliente MCP mínimo (`{"skill", "script", "org"?}`; la ruta
   `skills/<skill>/scripts/<script>` se traduce con `split_script_path`).
2. Se calcula el SHA-256 del contenido (UTF-8) y debe coincidir con el `sha256` que declara el MCP y,
   si se pasa `--sha256` (p. ej. el de `baas_get_skill`), también con ese. Si no coincide, no se ejecuta.
3. Se escribe en un directorio temporal privado (`mkdtemp`, 0700; fichero 0600) y se ejecuta con el
   intérprete según la extensión (`.py` con este mismo Python; `.sh` con bash/sh). El directorio de
   trabajo es el del proyecto (el script suele trabajar sobre él), no el temporal.
4. El entorno del script no incluye la API key (`ANTHROPIC_CUSTOM_HEADERS`, `BAAS_API_KEY`).
5. El directorio temporal se borra siempre: al terminar, si el script falla o si se interrumpe.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ConfigError, ScriptIntegrityError

SECRET_ENV = ("ANTHROPIC_CUSTOM_HEADERS", "BAAS_API_KEY")


@dataclass(frozen=True)
class Script:
    path: str
    content: str = field(repr=False)
    sha256: str


def split_script_path(path: str) -> tuple[str, str]:
    """`skills/<skill>/scripts/<script>` (la ruta que devuelve el MCP) o `<skill>/<script>` → (skill, script).

    El MCP expone `baas_get_script(skill, script, org?)`; el usuario y el agente trabajan con la ruta.
    """
    parts = PurePosixPath(path).parts if path else ()
    if len(parts) == 4 and parts[0] == "skills" and parts[2] == "scripts":
        skill, script = parts[1], parts[3]
    elif len(parts) == 2:
        skill, script = parts
    else:
        raise ConfigError(
            f"Ruta de script no válida: «{path}».",
            hint="Usa skills/<skill>/scripts/<script> (como la devuelve baas_get_skill) o <skill>/<script>.",
        )
    if any(p in ("", ".", "..") for p in (skill, script)):
        raise ConfigError(f"Ruta de script no válida: «{path}».")
    return skill, script


def fetch_script(client: Any, path: str, org: str | None = None) -> Script:
    skill, script = split_script_path(path)
    arguments = {"skill": skill, "script": script}
    if org:
        arguments["org"] = org
    data = client.call_tool("baas_get_script", arguments)
    try:
        return Script(str(data.get("path") or path), str(data["content"]), str(data["sha256"]).lower())
    except (KeyError, AttributeError, TypeError) as exc:
        raise ScriptIntegrityError(f"Respuesta de baas_get_script incompleta (falta {exc}).") from exc


def verify(script: Script, expected_sha256: str | None = None) -> None:
    actual = hashlib.sha256(script.content.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual, script.sha256):
        raise ScriptIntegrityError(
            f"El SHA-256 de {script.path} no coincide con el que declara el MCP; no se ejecuta.",
            hint=f"Declarado {script.sha256}, calculado {actual}. Informa al usuario.",
        )
    if expected_sha256 and not hmac.compare_digest(actual, expected_sha256.strip().lower()):
        raise ScriptIntegrityError(
            f"El SHA-256 de {script.path} no es el esperado ({expected_sha256}); no se ejecuta.",
            hint="El script ha cambiado desde que se consultó la skill. Informa al usuario.",
        )


def _interpreter(name: str) -> list[str]:
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == ".py":
        return [sys.executable]
    if suffix == ".sh":
        return [shutil.which("bash") or "/bin/sh"]
    raise ConfigError(f"Tipo de script no soportado: {name} (solo .py y .sh).")


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env["BAAS_SCRIPT"] = "1"
    return env


def execute(script: Script, args: list[str], *, cwd: Path) -> int:
    name = PurePosixPath(script.path).name or "script"
    interpreter = _interpreter(name)
    tmpdir = tempfile.mkdtemp(prefix="baas-script-")  # 0700
    previous = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
        target = Path(tmpdir) / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(script.content)
        res = subprocess.run([*interpreter, str(target), *args], cwd=cwd, env=_child_env(), check=False)
        return res.returncode
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        signal.signal(signal.SIGTERM, previous)


def run_script(
    mcp_url: str,
    api_key: str,
    path: str,
    *,
    args: list[str] | None = None,
    expected_sha256: str | None = None,
    org: str | None = None,
    cwd: Path,
) -> int:
    from .mcp_client import McpClient

    with McpClient(mcp_url, api_key) as client:
        script = fetch_script(client, path, org)
    verify(script, expected_sha256)
    return execute(script, list(args or []), cwd=cwd)


def cmd_run_script(ns: argparse.Namespace) -> int:
    from . import project
    from .envfile import BaasPaths, load_credentials

    creds = load_credentials(BaasPaths.default())
    proj = project.resolve_project_dir(ns.project or os.environ.get("CLAUDE_PROJECT_DIR"), cwd=Path.cwd())
    binding = project.read_binding(proj)
    if not binding or not binding.get("mcp_url"):
        raise ConfigError(f"El proyecto {proj} no está vinculado a BaaS-AI-IP: lanza /baas-init.")
    args = list(ns.script_args)
    return run_script(
        str(binding["mcp_url"]),
        creds.api_key,
        ns.path,
        args=args,
        expected_sha256=ns.sha256,
        org=ns.org,
        cwd=Path(ns.cwd).resolve() if ns.cwd else Path.cwd(),
    )


def add_run_script_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("run-script", help="Descarga un script del MCP, verifica su SHA-256, lo ejecuta y lo borra")
    p.add_argument("path", help="Ruta del script tal como la devuelven baas_list_skills / baas_get_skill")
    p.add_argument("--sha256", help="SHA-256 esperado (p. ej. el que lista baas_get_skill)")
    p.add_argument("--org", help="Otra organización (exige ORG_ADMIN en ella o PLATFORM_ADMIN)")
    p.add_argument("--project", help="Raíz del proyecto (por defecto CLAUDE_PROJECT_DIR o la raíz git)")
    p.add_argument("--cwd", help="Directorio de trabajo del script (por defecto el actual)")
    p.epilog = "Los argumentos para el script van al final, tras `--`."
    p.set_defaults(func=cmd_run_script)
