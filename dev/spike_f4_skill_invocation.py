#!/usr/bin/env python3
"""Spike T0 de la observabilidad de uso (F4): cómo aparece una invocación de skill en el texto de las trazas.

Lanza el CLI real de Claude Code (`claude -p`) contra el proxy de una pila de desarrollo de `deploy` con el
proveedor falso (`scripts/dev-up.sh --fake-upstream`) y vuelca los eventos de `baas:traces` que produce cada caso:

  tools  `hola`: la petición real de Claude Code, para leer en `tools` la definición de la herramienta `Skill`.
  a      `/demo:facturacion …` escrito por el usuario (skill de un plugin local `demo` cargado con `--plugin-dir`).
  b      el modelo llama a la herramienta `Skill` con `demo:facturacion` (el proveedor falso responde con un
         `tool_use` de `Skill` cuando el último texto del usuario dice «usa la herramienta skill <nombre>»; parche
         desechable de `deploy/dev/fake-provider/fake_provider.py`) y Claude Code devuelve el `tool_result`.
  c      lo mismo con una skill de otro plugin (`otro:cosa`): por herramienta y escrita por el usuario.

Seguridad: Claude Code corre con HOME y `CLAUDE_CONFIG_DIR` temporales, credencial de proveedor ficticia y en un
espacio de nombres de red vacío (`unshare -rnm`): solo llega al puerto de la pila por `dev/netns_bridge.py`. Las
trazas se leen con `XRANGE` (no destructivo); como la webapp consume y borra (`XACK` + `XDEL`) cada entrada, su
contenedor se **pausa** mientras dura la captura y se reanuda al final (`--webapp-container ''` no pausa nada).

Uso (pila `f4-uso`, ver `deploy/README.md`):

    uv run python dev/spike_f4_skill_invocation.py --app-port 48080 --fake-url http://127.0.0.1:48090 \\
        --project-name f4-uso --out /ruta/skill_invocation_traces.json

No se distribuye con el plugin.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_h2 import dev_api_key

REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "dev" / "netns_bridge.py"
FAKE_PROVIDER_KEY = "sk-ant-api03-FAKE-spike-f4-provider-key"  # credencial de proveedor ficticia
HOSTS = "127.0.0.1 localhost app.localhost proxy.localhost mcp.localhost\n::1 localhost\n"
TEXT_FIELDS = ("request_original", "request_sent", "response_received", "response_returned")

SKILL_MD = """---
name: {name}
description: {description}
---

{body}
"""
PLUGINS = {
    "demo": ("facturacion", "Prepara una factura de prueba (spike F4).", "MARCA-SKILL-FACTURACION: suma el IVA."),
    "otro": ("cosa", "Skill de otro plugin (spike F4).", "MARCA-SKILL-COSA: no hagas nada."),
}

# Casos: (nombre, prompt, opciones extra de `claude`). El proveedor falso responde con el eco del último texto
# del usuario; con un tool_use de Skill si ese texto contiene «usa la herramienta skill <nombre>», y con uno de
# `echo` (que Claude Code no tiene: vuelve un tool_result de error) si solo dice «usa la herramienta».
CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("tools", "hola", ()),
    ("a_slash_demo", "/demo:facturacion importe 100", ()),
    ("b_tool_demo", "usa la herramienta skill demo:facturacion", ()),
    ("c_tool_otro", "usa la herramienta skill otro:cosa", ()),
    ("c_slash_otro", "/otro:cosa", ()),
    # Bucle agéntico tras un /demo:facturacion y continuación de esa conversación (`-c`): ¿se repite el comando?
    ("d_slash_loop", "/demo:facturacion usa la herramienta", ()),
    ("d_continue", "segunda pregunta sin skill", ("-c",)),
)


# --- expresiones regulares propuestas para T4.2 -----------------------------------------------------


SKILL_NAME = r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})"  # la del contrato de la Fase 4, §7 (y la de la webapp)


def slash_re(org: str) -> re.Pattern[str]:
    """`/<org>:<skill>` escrito por el usuario: Claude Code lo envía como `<command-name>/<org>:<skill></command-name>`
    (precedido de `<command-message>` y seguido de `<command-args>` y del contenido de la skill). Solo en
    `turn.request.request_original`."""
    return re.compile(r"<command-name>/" + re.escape(org) + ":" + SKILL_NAME + r"</command-name>")


def tool_re(org: str) -> re.Pattern[str]:
    """La herramienta `Skill` en la respuesta del modelo: el proxy la renderiza `[tool_use: Skill] <JSON compacto>`
    (una sola línea; `json.Compact` respeta el orden de las claves, por eso `skill` puede no ir la primera). Solo en
    `turn.completed.response_received`. Dentro de un valor JSON las comillas van escapadas (`\\"`), así que
    `"skill":"` sin escapar solo puede ser la clave de primer nivel."""
    return re.compile(r'\[tool_use: Skill\] \{[^\n]*?"skill":"/?' + re.escape(org) + ":" + SKILL_NAME + '"')


def invoked(org: str, event: dict[str, Any]) -> dict[str, int]:
    """Regla de recuento propuesta para T4.2: `/<org>:<skill>` solo en `turn.request.request_original` y `Skill`
    solo en `turn.completed.response_received` (el texto de cada `turn.request` repite la respuesta anterior del
    modelo, con su `[tool_use: Skill]`, y trae el `[tool_result] Launching skill: …`)."""
    payload = event.get("payload") or {}
    if event.get("type") == "turn.request":
        found = slash_re(org).findall(payload.get("request_original") or "")
    elif event.get("type") == "turn.completed":
        found = tool_re(org).findall(payload.get("response_received") or "")
    else:
        found = []
    out: dict[str, int] = {}
    for skill in found:
        out[skill] = out.get(skill, 0) + 1
    return out


def regex_self_test() -> list[str]:
    """Casos de prefijo exacto: devuelve los que fallan."""
    cases = [
        (slash_re("ginxo"), "<command-name>/ginxo:informe</command-name>", ["informe"]),
        (slash_re("ginxo"), "<command-name>/ginxo2:informe</command-name>", []),
        (slash_re("ginxo"), "<command-name>/xginxo:informe</command-name>", []),
        (slash_re("ginxo"), "<command-name>/ginxo</command-name>", []),
        (slash_re("ginxo"), "<command-name>/plugin:informe</command-name>", []),
        (slash_re("ginxo"), "- ginxo:informe: descripción del listado de skills", []),
        (slash_re("ginxo"), "/ginxo:informe sin envoltorio", []),
        (slash_re("mi-org"), "<command-name>/mi-org:a-b_c</command-name>", ["a-b_c"]),
        (slash_re("ginxo"), "<command-name>/ginxo:Informe.v2</command-name>", ["Informe.v2"]),
        (slash_re("ginxo"), "<command-name>/ginxo:" + "a" * 129 + "</command-name>", []),
        (slash_re("ginxo"), "<command-name>/ginxo:-informe</command-name>", []),
        (tool_re("ginxo"), '[tool_use: Skill] {"skill":"ginxo:informe"}', ["informe"]),
        (tool_re("ginxo"), '[tool_use: Skill] {"args":"x","skill":"ginxo:informe"}', ["informe"]),
        (tool_re("ginxo"), '[tool_use: Skill] {"skill":"/ginxo:informe","args":"y"}', ["informe"]),
        (tool_re("ginxo"), '[tool_use: Skill] {"skill":"ginxo2:informe"}', []),
        (tool_re("ginxo"), '[tool_use: Skill] {"skill":"ginxo"}', []),
        (tool_re("ginxo"), '[tool_use: Skill] {"skill":"plugin:informe"}', []),
        (tool_re("ginxo"), '[tool_use: Skill] {"args":"\\"skill\\":\\"ginxo:x\\"","skill":"otro:y"}', []),
        (tool_re("ginxo"), '[tool_use: Bash] {"command":"echo \\"skill\\":\\"ginxo:x\\""}', []),
        (tool_re("ginxo"), "[tool_result] Launching skill: ginxo:informe", []),
        (tool_re("ginxo"), '[tool_use: Skill] {"skill":"ginxo:Informe.v2"}', ["Informe.v2"]),
    ]
    return [f"{rx.pattern!r} sobre {text!r}: {rx.findall(text)} ≠ {want}" for rx, text, want in cases
            if rx.findall(text) != want]  # fmt: skip


# --- utilidades -----------------------------------------------------------------------------------


def get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 (proveedor falso local)
        return json.loads(resp.read())


def clear_fake(fake_url: str) -> None:
    req = urllib.request.Request(fake_url + "/_debug/requests", method="DELETE")  # noqa: S310
    urllib.request.urlopen(req, timeout=10).close()  # noqa: S310


def redis_xrange(container: str) -> list[tuple[str, dict[str, Any]]]:
    """Entradas de `baas:traces` (XRANGE, no destructivo) con el campo `data` decodificado."""
    cmd = 'REDISCLI_AUTH="$BAAS_REDIS_PASSWORD" redis-cli --json XRANGE baas:traces - +'
    out = subprocess.run(["podman", "exec", container, "sh", "-c", cmd], capture_output=True, text=True, check=True)
    raw = json.loads(out.stdout or "[]")
    entries = []
    for entry_id, fields in raw:
        values = dict(zip(fields[::2], fields[1::2], strict=True)) if isinstance(fields, list) else fields
        entries.append((entry_id, json.loads(values["data"])))
    return entries


def podman(*args: str) -> None:
    subprocess.run(["podman", *args], check=True, capture_output=True)


def write_plugins(root: Path) -> list[Path]:
    dirs = []
    for plugin, (skill, description, body) in PLUGINS.items():
        d = root / plugin
        (d / ".claude-plugin").mkdir(parents=True)
        (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": plugin, "version": "0.0.1"}))
        (d / "skills" / skill).mkdir(parents=True)
        (d / "skills" / skill / "SKILL.md").write_text(SKILL_MD.format(name=skill, description=description, body=body))
        dirs.append(d)
    return dirs


def scrub(value: Any, secrets: list[str]) -> Any:
    """Sustituye las claves de prueba por marcadores (el fixture no lleva credenciales)."""
    text = json.dumps(value, ensure_ascii=False)
    for s in secrets:
        text = text.replace(s, "<REDACTADO>")
    return json.loads(text)


# --- el spike -------------------------------------------------------------------------------------


class Spike:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.key = dev_api_key(args.email)
        self.work = Path(tempfile.mkdtemp(prefix="baas-spike-f4-"))
        self.home, self.config, self.proj = self.work / "home", self.work / "config", self.work / "proyecto"
        for d in (self.home, self.config, self.proj):
            d.mkdir(mode=0o700)
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        (self.work / "hosts").write_text(HOSTS)
        self.plugins = write_plugins(self.work / "plugins")
        self.sock = self.work / "bridge.sock"
        self.base_url = f"http://proxy.localhost:{args.app_port}/org/{args.org}/anthropic"

    def run_claude(self, prompt: str, extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        plugin_flags = [f for d in self.plugins for f in ("--plugin-dir", str(d))]
        # Dentro del espacio de nombres se es root (unshare -r): Claude Code rechaza ahí
        # --dangerously-skip-permissions, así que solo se permite la herramienta Skill.
        return self.isolated(
            ["-p", "--output-format", "json", "--allowedTools", "Skill", *plugin_flags, *extra, prompt]
        )

    def claude_version(self) -> str:
        """`claude --version` con el mismo aislamiento que los casos (HOME temporal, sin red)."""
        return self.isolated(["--version"]).stdout.strip()

    def isolated(self, claude_args: list[str]) -> subprocess.CompletedProcess[str]:
        """`claude <claude_args>` con `env -i` (HOME y configuración temporales, credencial ficticia) y en `unshare`
        sin red (solo el puerto de la pila por el puente)."""
        env = {
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.config),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TMPDIR": str(self.work),
            "ANTHROPIC_BASE_URL": self.base_url,
            "ANTHROPIC_API_KEY": FAKE_PROVIDER_KEY,
            "ANTHROPIC_CUSTOM_HEADERS": f"X-Baas-Api-Key: {self.key}",
            "ANTHROPIC_MODEL": "claude-fake",
            "ANTHROPIC_SMALL_FAST_MODEL": "claude-fake",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
        claude = shutil.which("claude") or "claude"
        script = (
            'mount --bind "$3" /etc/hosts || exit 97\n'
            "ip link set lo up\n"
            '"$4" "$0" tcp-to-unix "127.0.0.1:$1,[::1]:$1" "$2" >/dev/null 2>&1 &\n'
            "bridge=$!\nsleep 0.4\nshift 4\n"
            'timeout --foreground --kill-after=5 180 "$@"\nrc=$?\nkill "$bridge" 2>/dev/null\nexit $rc\n'
        )
        cmd = [
            "unshare", "-rnm", "sh", "-c", script, str(BRIDGE), str(self.args.app_port), str(self.sock),
            str(self.work / "hosts"), sys.executable, claude, *claude_args,
        ]  # fmt: skip
        return subprocess.run(["env", "-i", *(f"{k}={v}" for k, v in env.items()), *cmd], cwd=self.proj,
                              capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)  # fmt: skip

    def case(self, name: str, prompt: str, extra: tuple[str, ...]) -> dict[str, Any]:
        before = {eid for eid, _ in redis_xrange(self.args.redis_container)}
        clear_fake(self.args.fake_url)
        res = self.run_claude(prompt, extra)
        time.sleep(self.args.settle)
        events = [ev for eid, ev in redis_xrange(self.args.redis_container) if eid not in before]
        upstream = get_json(self.args.fake_url + "/_debug/requests")["requests"]
        return {"name": name, "prompt": prompt, "exit": res.returncode, "stdout": res.stdout[-4000:],
                "stderr": res.stderr[-2000:], "events": events, "upstream": upstream}  # fmt: skip

    def run(self) -> dict[str, Any]:
        bridge = subprocess.Popen([sys.executable, str(BRIDGE), "unix-to-tcp", str(self.sock),
                                   f"127.0.0.1:{self.args.app_port}"], stderr=subprocess.DEVNULL)  # fmt: skip
        paused = False
        try:
            time.sleep(0.4)
            if self.args.webapp_container:
                podman("pause", self.args.webapp_container)
                paused = True
            return {
                "claude_version": self.claude_version(),
                "cases": [self.case(*c) for c in CASES if c[0] in self.args.only],
            }
        finally:
            if paused:
                podman("unpause", self.args.webapp_container)
            bridge.terminate()
            if not self.args.keep:
                shutil.rmtree(self.work, ignore_errors=True)


NOTES = {
    # las expresiones normativas del contrato de la Fase 4, §7 (las de la webapp), con {org} en lugar del slug
    "regex_slash": slash_re("ORG").pattern.replace("ORG", "{org}")
    + "  (re.escape(org); solo en turn.request.payload.request_original)",
    "regex_tool": tool_re("ORG").pattern.replace("ORG", "{org}")
    + "  (re.escape(org); solo en turn.completed.payload.response_received)",
    "regla_de_recuento": "Contar /<org>:<skill> solo en turn.request.request_original y Skill solo en "
    "turn.completed.response_received; cada coincidencia, una invocación. request_original es solo lo nuevo del turno "
    "(delta de la correlación), pero empieza por la respuesta anterior del modelo: un [tool_use: Skill] reaparece en "
    "el turn.request siguiente, junto al [tool_result] Launching skill: <org>:<skill>. Las dos fuentes no se solapan: "
    "un /<org>:<skill> lo expande Claude Code sin llamar a Skill, y el resultado de Skill no lleva <command-name>.",
    "no_aparece_o_no_contar": [
        "El listado de skills disponibles ('- demo:facturacion: <descripción>') va en el primer turn.request de cada "
        "conversación: no es una invocación (por eso la regex exige <command-name>).",
        "La definición de la herramienta Skill (array tools) no se renderiza en el texto.",
        "Si la correlación del proxy falla y abre una conversación nueva con todo el historial, el <command-name> "
        "antiguo reaparece en request_original y se contaría de nuevo (no observado en el spike).",
        "Recorte a 256 KB por campo: el comando del primer turno va tras el system prompt (~13 KB en el spike).",
    ],
    "evento": "Entrada de XADD en baas:traces con un único campo 'data' = JSON del sobre (event_id, type, ts, "
    "org_slug, org_id, user_id, api_key_id, conversation_id, turn_index, payload). Cada elemento de events es "
    "json.loads(fields['data']).",
}
FIXTURE_CASES = {
    "a_slash_demo": "(a) /demo:facturacion escrito por el usuario",
    "b_tool_demo": "(b) el modelo llama a Skill con demo:facturacion; bucle agéntico de dos peticiones",
    "c_tool_otro": "(c) el modelo llama a Skill con otro:cosa (otro plugin): no cuenta para demo",
    "c_slash_otro": "(c) /otro:cosa escrito por el usuario: no cuenta para demo",
    "d_slash_loop": "(d) /demo:facturacion seguido de un tool_use y su tool_result en la misma orden",
    "d_continue": "(d) continuación (-c) de la conversación anterior: el comando no se repite",
}


def build_fixture(report: dict[str, Any], org: str) -> dict[str, Any]:
    turns = []
    for case in report["cases"]:
        if case["name"] not in FIXTURE_CASES:
            continue
        expected: dict[str, int] = {}
        for ev in case["events"]:
            for skill, n in invoked(org, ev).items():
                expected[skill] = expected.get(skill, 0) + n
        turns.append({"case": case["name"], "description": FIXTURE_CASES[case["name"]], "prompt": case["prompt"],
                      "expected_invoked": expected, "events": case["events"]})  # fmt: skip
    notes = {k: v.replace("{org}", "<org>") if isinstance(v, str) else v for k, v in NOTES.items()}
    return {"captured_with": {"claude_code": report["claude_version"], "org_slug": org, "stream": "baas:traces"},
            "notes": notes, "turns": turns}  # fmt: skip


def summarize(report: dict[str, Any], org: str) -> None:
    """Imprime, por caso, cada evento de turno con sus campos de texto y lo que cuenta la regla propuesta."""
    rs, rt = slash_re(org), tool_re(org)
    for case in report["cases"]:
        print(f"\n=== {case['name']} · «{case['prompt']}» · exit {case['exit']}")
        if case["exit"] != 0:
            print(case["stderr"][-800:])
        for ev in case["events"]:
            p = ev.get("payload") or {}
            head = f"- {ev['type']} conv={ev['conversation_id'][:8]} turn={ev['turn_index']}"
            if ev["type"] == "conversation.started":
                print(head, "auxiliary=", p.get("auxiliary"), "client=", p.get("client_tool"))
                continue
            print(head, "cuenta=", invoked(org, ev))
            for f in ("request_original", "response_received"):
                if f in p:
                    text = p[f] or ""
                    print(f"    {f} ({len(text)} B) slash={rs.findall(text)} tool={rt.findall(text)}")
                    for line in text.splitlines():
                        if "demo:" in line or "otro:" in line or "[tool_" in line:
                            print("      |", line[:300])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Spike F4-T0: invocaciones de skill en las trazas del proxy")
    p.add_argument("--app-port", type=int, default=8080, help="puerto HTTP de la pila (Caddy)")
    p.add_argument("--fake-url", default="http://127.0.0.1:18090", help="proveedor falso (para /_debug/requests)")
    p.add_argument("--project-name", default="baas-dev", help="COMPOSE_PROJECT_NAME de la pila")
    p.add_argument("--redis-container", help="contenedor de redis-ephemeral (defecto: <proyecto>_redis-ephemeral_1)")
    p.add_argument("--webapp-container", help="contenedor de la webapp a pausar (defecto: <proyecto>_webapp-api_1)")
    p.add_argument("--email", default="analista@demo.test", help="usuario de la clave de desarrollo")
    p.add_argument("--org", default="demo")
    p.add_argument("--settle", type=float, default=1.5, help="segundos de espera de las trazas tras cada caso")
    p.add_argument("--only", nargs="+", default=[c[0] for c in CASES], help="casos a ejecutar")
    p.add_argument("--keep", action="store_true", help="conservar el directorio temporal")
    p.add_argument("--out", type=Path, required=True, help="volcado JSON en bruto de todos los casos")
    p.add_argument("--fixture", type=Path, help="escribe aquí el fixture de T4.2 (casos a–d)")
    args = p.parse_args(argv)
    args.redis_container = args.redis_container or f"{args.project_name}_redis-ephemeral_1"
    if args.webapp_container is None:
        args.webapp_container = f"{args.project_name}_webapp-api_1"
    failed = regex_self_test()
    if failed:
        print("Las expresiones regulares no pasan su autoprueba:\n" + "\n".join(failed), file=sys.stderr)
        return 1
    report = Spike(args).run()
    report = scrub(report, [dev_api_key(args.email), FAKE_PROVIDER_KEY])
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    summarize(report, args.org)
    if args.fixture:
        args.fixture.write_text(json.dumps(build_fixture(report, args.org), ensure_ascii=False, indent=2) + "\n")
        print(f"Fixture en {args.fixture}")
    print(f"\nVolcado en {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
