#!/usr/bin/env python3
"""Guardia de sesión del plugin `baas`: hooks `SessionStart` y `UserPromptSubmit`.

En un proyecto vinculado a BaaS-AI-IP (`.claude/baas.local.json`), la sesión de Claude Code solo
puede hablar con el modelo a través del proxy de la organización: si `ANTHROPIC_BASE_URL` del
proceso no es la del proxy, las peticiones irían directas al proveedor, sin AI-IP ni trazas.

Pasa, sobre todo, en la pestaña Code de Claude Desktop: Desktop fija su propia
`ANTHROPIC_BASE_URL` e ignora la de `.claude/settings.local.json` (ver README, «Claude Desktop»).
También cuando no se ha aceptado la confianza en la carpeta.

- `--event session-start`: avisa (mensaje al usuario y contexto para el agente).
- `--event prompt`: **bloquea** el prompt (fallo cerrado).
- Proyecto no vinculado o sesión por el proxy: no imprime nada.

Autónomo a propósito (solo biblioteca estándar, sin importar nada del plugin y sin sintaxis
moderna en tiempo de ejecución): corre en cada prompt de cada proyecto y no debe romper la sesión
de quien no use BaaS. `/baas-status` reutiliza `diagnose()`.
"""

from __future__ import annotations

import json
import os
import sys

BINDING = os.path.join(".claude", "baas.local.json")
DESKTOP_ENTRYPOINT = "claude-desktop"


def _norm(url: object) -> str:
    return str(url or "").strip().rstrip("/")


def is_desktop(environ: dict[str, str]) -> bool:
    return environ.get("CLAUDE_CODE_ENTRYPOINT") == DESKTOP_ENTRYPOINT


def diagnose(binding: dict | None, environ: dict[str, str]) -> str | None:
    """Explica por qué esta sesión no pasa por el proxy de la organización, o None si pasa."""
    if binding is None:
        return None
    expected = _norm(binding.get("proxy_anthropic_url"))
    current = _norm(environ.get("ANTHROPIC_BASE_URL"))
    org = binding.get("org") or "?"
    if not expected:
        return (
            "BaaS-AI-IP: la vinculación del proyecto (.claude/baas.local.json) no tiene la URL del proxy;"
            " lanza /baas-init desde el CLI de Claude Code."
        )
    if current == expected:
        return None
    head = (
        f"BaaS-AI-IP: este proyecto está vinculado a la organización «{org}», pero esta sesión no pasa"
        f" por el proxy de la organización (ANTHROPIC_BASE_URL={current or 'sin definir'}; se esperaba {expected})."
        " Sin el proxy, lo que escribas iría directo al proveedor sin AI-IP."
    )
    if is_desktop(environ):
        return (
            head + " Estás en la pestaña Code de Claude Desktop, que fija su propia Base URL e ignora la del"
            " proyecto. Opciones: (1) usa el CLI `claude` o la extensión del IDE en este proyecto;"
            " (2) configura Claude Desktop en modo gateway hacia el proxy: en una terminal ejecuta"
            " `python3 <plugin>/scripts/baas.py desktop-config` y sigue sus pasos (ver README del plugin)."
            " Si llegaste a enviar algún prompt aquí antes de este aviso, tu API key de BaaS pudo viajar"
            " al proveedor en la cabecera X-Baas-Api-Key: revócala en la webapp y crea otra."
        )
    return head + (
        " Reinicia Claude Code en el proyecto y acepta la confianza en la carpeta (el proxy se configura"
        " en .claude/settings.local.json); si persiste, relanza /baas-init."
    )


def read_binding(project_dir: str) -> dict | None:
    """Lee la vinculación. Un fichero ilegible cuenta como vinculado sin proxy (fallo cerrado)."""
    path = os.path.join(project_dir, BINDING)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _hook_input() -> dict:
    try:
        raw = sys.stdin.read() if sys.stdin is not None and not sys.stdin.isatty() else ""
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def hook_output(event: str, problem: str) -> dict:
    if event == "prompt":
        return {"decision": "block", "reason": problem}
    return {
        "systemMessage": problem,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": problem},
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    event = args[args.index("--event") + 1] if "--event" in args[:-1] else "prompt"
    if event not in ("prompt", "session-start"):
        sys.stderr.write(f"baas guard: evento desconocido {event!r}\n")
        return 1
    data = _hook_input()
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    problem = diagnose(read_binding(str(project_dir)), dict(os.environ))
    if problem:
        sys.stdout.write(json.dumps(hook_output(event, problem), ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
