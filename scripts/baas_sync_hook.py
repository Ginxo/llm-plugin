#!/usr/bin/env python3
"""Lanzador de la sincronización para los hooks `SessionStart` y `SessionEnd` del plugin `baas`.

Un hook no puede retrasar ni estorbar el arranque (ni el cierre) de Claude Code: este script **no
sincroniza**, solo lanza `baas.py sync --quiet` como proceso independiente (sesión propia, sin entrada ni
salida) y vuelve al instante. El trabajo, con su tiempo máximo corto de espera al MCP (5 s, ajustable con
`BAAS_SYNC_TIMEOUT`), lo hace ese proceso, y **falla en abierto**: sin MCP, o con el MCP lento, no toca nada.

- Proyecto no vinculado a BaaS-AI-IP (sin `.claude/baas.local.json`): no hace nada.
- No imprime nunca nada por la salida estándar (la de un hook de `SessionStart` pasaría al contexto del agente).
- Siempre sale con 0, también con Python antiguo o ante cualquier error.
- Lo que la sincronización cuente va a `~/baas-ai-ip/sync.log` (solo si esa carpeta ya existe).
- Un `SessionEnd` dispone de ~1,5 s: por eso se lanza y se vuelve, en lugar de esperar al resultado.

Autónomo a propósito (solo biblioteca estándar, sin importar nada del plugin), como `baas_guard.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

BINDING = os.path.join(".claude", "baas.local.json")
LOG_NAME = "sync.log"
LOG_MAX_BYTES = 64 * 1024
DEFAULT_TIMEOUT = "5"


def _hook_input() -> dict:
    try:
        raw = sys.stdin.read() if sys.stdin is not None and not sys.stdin.isatty() else ""
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _log_file():
    """El registro del hook, si `~/baas-ai-ip` ya existe; si no, se descarta la salida."""
    root = os.path.join(os.path.expanduser("~"), "baas-ai-ip")
    if not os.path.isdir(root):
        return subprocess.DEVNULL
    path = os.path.join(root, LOG_NAME)
    try:
        if os.path.getsize(path) > LOG_MAX_BYTES:
            os.unlink(path)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "ab")
    except OSError:
        return subprocess.DEVNULL


def launch(event: str, project_dir: str) -> bool:
    if sys.version_info < (3, 10):  # noqa: UP036 (un Python antiguo no debe romper el hook)
        return False
    if not os.path.isfile(os.path.join(project_dir, BINDING)):
        return False
    baas = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baas.py")
    timeout = os.environ.get("BAAS_SYNC_TIMEOUT") or DEFAULT_TIMEOUT
    argv = [sys.executable, baas, "sync", "--project", project_dir, "--quiet", "--timeout", timeout]
    argv += ["--trigger", event]
    log = _log_file()
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # sobrevive al cierre de la sesión de Claude Code
    else:  # pragma: no cover - Windows no está probado
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(argv, **kwargs)
    finally:
        if log is not subprocess.DEVNULL:
            log.close()
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    event = args[args.index("--event") + 1] if "--event" in args[:-1] else "session-start"
    try:
        data = _hook_input()
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
        launch(event, str(project_dir))
    except Exception:  # noqa: S110 - un hook nunca debe fallar ni estorbar
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
