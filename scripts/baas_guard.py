#!/usr/bin/env python3
"""Guardia de sesión del plugin `baas`: hooks `SessionStart` y `UserPromptSubmit`.

En un proyecto vinculado a BaaS-AI-IP (`.claude/baas.local.json`), la sesión de Claude Code solo
puede hablar con el modelo a través del proxy de la organización: si `ANTHROPIC_BASE_URL` del
proceso no es la del proxy, las peticiones irían directas al proveedor, sin AI-IP ni trazas.

Pasa, sobre todo, en la pestaña Code de Claude Desktop: Desktop fija su propia
`ANTHROPIC_BASE_URL` e ignora la de `.claude/settings.local.json` (ver README, «Claude Desktop»).
También cuando no se ha aceptado la confianza en la carpeta.

- `--event session-start`: avisa (mensaje al usuario y contexto para el agente).
- `--event prompt`: **bloquea** el prompt (fallo cerrado), salvo `/baas-init` y `/baas-status`, que son
  justo lo que arregla o explica el problema (si no, habría un círculo vicioso).
- Proyecto no vinculado o sesión por el proxy: no imprime nada.

Autónomo a propósito (solo biblioteca estándar, sin importar nada del plugin y sin sintaxis
moderna en tiempo de ejecución): corre en cada prompt de cada proyecto y no debe romper la sesión
de quien no use BaaS (cualquier excepción inesperada termina en silencio y éxito). `/baas-status`
reutiliza `diagnose()`. Los mensajes van en lenguaje llano para quien no es técnico: qué pasa y qué
hacer, sin variables, rutas ni comandos.
"""

from __future__ import annotations

import json
import os
import re
import sys

BINDING = os.path.join(".claude", "baas.local.json")
DESKTOP_ENTRYPOINT = "claude-desktop"
# Solo `/baas-init` y `/baas-status` (con o sin el prefijo del plugin), seguidos de nada o de espacio.
ALLOWED_PROMPT = re.compile(r"^\s*/(?:baas:)?baas-(?:init|status)(?:\s|$)")
MAX_ALLOWED_PROMPT = 200


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
    org = binding.get("org")
    where = f"«{org}»" if org else "BaaS-AI-IP"
    if not expected:
        return (
            f"BaaS-AI-IP: la conexión de este proyecto con {where} está incompleta. Escribe /baas-init para repetirla."
        )
    if current == expected:
        return None
    if is_desktop(environ):
        return (
            f"BaaS-AI-IP: Este proyecto está conectado a {where}, pero estás usando la pestaña Code de"
            " Claude Desktop, que no admite la protección de BaaS-AI-IP. Usa Claude Code en el terminal"
            " o en tu editor. Si ya enviaste algún mensaje desde aquí, conviene cambiar tu clave de"
            " BaaS-AI-IP desde la web de la organización; si no sabes cómo, pídeselo a tu administrador."
        )
    return (
        f"BaaS-AI-IP: Este proyecto está conectado a {where}, pero esta sesión aún no está protegida."
        " Cierra Claude Code, vuelve a abrirlo en esta carpeta y acepta la confianza."
        " Si sigue igual, escribe /baas-init"
    )


def is_allowed_prompt(data: dict) -> bool:
    """True si el prompt entero es `/baas-init` o `/baas-status` (con argumentos en la misma línea).

    Lo que pasa la guardia viaja al proveedor sin AI-IP, así que la lista es estricta: ancla al
    principio, rechaza cualquier línea más (también separadores Unicode) y limita la longitud.
    """
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        return False
    text = prompt.strip()
    if not text or len(text) > MAX_ALLOWED_PROMPT or len(text.splitlines()) != 1:
        return False
    return ALLOWED_PROMPT.match(text) is not None


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
    try:
        data = _hook_input()
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
        problem = diagnose(read_binding(str(project_dir)), dict(os.environ))
        if problem and not (event == "prompt" and is_allowed_prompt(data)):
            sys.stdout.write(json.dumps(hook_output(event, problem), ensure_ascii=False) + "\n")
    except Exception:  # la guardia nunca debe romper la sesión de quien no usa BaaS
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
