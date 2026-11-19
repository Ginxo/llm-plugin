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
# Gramática real de los dos comandos (con o sin el prefijo del plugin), en una sola línea y sin más
# texto: lo que pasa la guardia viaja al proveedor sin AI-IP, así que no se admiten argumentos libres.
# `/baas-init` admite una organización opcional (un identificador corto, con o sin `--org`);
# `/baas-status` no admite argumentos. Solo espacios y tabuladores ASCII (nada de `\s` Unicode).
ALLOWED_PROMPT = re.compile(
    r"[ \t]*/(?:baas:)?baas-(?:"
    r"init(?:[ \t]+(?:--org[ \t]+)?[a-z0-9][a-z0-9-]{0,63})?"
    r"|status"
    r")[ \t]*"
)
GENERIC_PROBLEM = (
    "BaaS-AI-IP: No he podido comprobar si esta sesión está protegida. Cierra Claude Code, vuelve a"
    " abrirlo en esta carpeta y escribe /baas-init"
)


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
            " o en tu editor. Si ya enviaste algún mensaje desde aquí, tu clave de BaaS-AI-IP pudo llegar"
            " al proveedor del modelo; cámbiala desde la web de tu organización (o pídelo a quien la administra)."
        )
    return (
        f"BaaS-AI-IP: Este proyecto está conectado a {where}, pero esta sesión aún no está protegida."
        " Cierra Claude Code, vuelve a abrirlo en esta carpeta y acepta la confianza."
        " Si sigue igual, escribe /baas-init"
    )


def is_allowed_prompt(data: dict) -> bool:
    """True si el prompt entero es `/baas-init [org]` o `/baas-status` (ver `ALLOWED_PROMPT`).

    Estricta y anclada: una sola línea (también para separadores Unicode), sin más texto.
    """
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        return False
    text = prompt.strip(" \t\r\n")
    if len(text.splitlines()) != 1:
        return False
    return ALLOWED_PROMPT.fullmatch(text) is not None


def read_binding(project_dir: str) -> dict | None:
    """Lee la vinculación. Un fichero ilegible cuenta como vinculado sin proxy (fallo cerrado)."""
    path = os.path.join(project_dir, BINDING)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # ilegible (incluida una recursión excesiva): vinculado sin proxy, se bloquea
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
    # Mientras no se sepa si el proyecto está vinculado, cualquier fallo es silencioso: la guardia no
    # debe romper la sesión de quien no usa BaaS.
    try:
        data = _hook_input()
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
        binding = read_binding(str(project_dir))
    except Exception:
        return 0
    if binding is None:
        return 0
    # Proyecto vinculado: un fallo ya no puede dejar pasar el prompt sin protección.
    try:
        allowed = event == "prompt" and is_allowed_prompt(data)
    except Exception:
        allowed = False
    try:
        problem = diagnose(binding, dict(os.environ))
        if problem and not allowed:
            _emit(event, problem)
    except Exception:
        if not allowed:
            try:
                _emit(event, GENERIC_PROBLEM)
            except Exception:
                return 0
    return 0


def _emit(event: str, problem: str) -> None:
    # ensure_ascii (por defecto): una salida que no sea UTF-8 no puede hacer perder el bloqueo.
    sys.stdout.write(json.dumps(hook_output(event, problem)) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
