"""Spike S1: pruebas con el CLI real de Claude Code, sin red y con HOME y proyecto temporales.

Ejecutar SIEMPRE dentro de un espacio de nombres de red vacío, para que nada salga a Anthropic:

    unshare -rn sh -c 'ip link set lo up && uv run --no-sync python dev/spike_s1.py'

(o `BAAS_SPIKE_ALLOW_NETWORK=1` bajo tu responsabilidad). Usa servidores falsos (BaaS y API de Anthropic)
en 127.0.0.1 y credenciales de proveedor ficticias. Imprime un informe JSON con cada comprobación.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from fake_anthropic import TEXT, FakeAnthropic
from fake_baas import FakeBaasServer, dev_api_key

REPO = Path(__file__).resolve().parents[1]
BAAS = [sys.executable, str(REPO / "scripts" / "baas.py")]
KEY = dev_api_key("analista@demo.test")
FAKE_PROVIDER_KEY = "sk-ant-api03-FAKE-provider-key"
FAKE_OAUTH = "sk-ant-oat01-FAKE-oauth-token"


def isolated() -> bool:
    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=2).close()
    except OSError:
        return True
    return False


def main() -> int:
    if not isolated() and os.environ.get("BAAS_SPIKE_ALLOW_NETWORK") != "1":
        print("Hay salida a Internet: ejecuta dentro de `unshare -rn` (ver docstring).", file=sys.stderr)
        return 2
    claude = shutil.which("claude")
    if not claude:
        print("No se encontró el CLI `claude`.", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="baas-spike-"))
    home, proj = work / "home", work / "proyecto"
    home.mkdir()
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    anthropic = FakeAnthropic()
    report: dict[str, object] = {"claude_version": subprocess.run([claude, "--version"], capture_output=True,
                                                                  text=True).stdout.strip()}  # fmt: skip
    base_env = {
        "HOME": str(home),
        "PATH": os.environ["PATH"],
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }

    with FakeBaasServer() as baas:
        baas.state.proxy_base = anthropic.url
        baas.state.stateless = True

        def run(args: list[str], extra: dict[str, str] | None = None, **kw: object) -> subprocess.CompletedProcess:
            env = {**base_env, **(extra or {})}
            return subprocess.run(args, cwd=proj, env=env, capture_output=True, text=True, timeout=120, **kw)

        # 1) /baas-init por su script, con el CLI real para registrar el MCP
        r = run([*BAAS, "set-key", "--stdin", "--app-url", baas.url], input=KEY + "\n")
        r = run([*BAAS, "init", "--project", str(proj)])
        report["init_exit"] = r.returncode
        report["init_output"] = r.stdout + r.stderr
        claude_json = (home / ".claude.json").read_text() if (home / ".claude.json").exists() else ""
        cfg = json.loads(claude_json or "{}")
        local = {p: v.get("mcpServers") for p, v in (cfg.get("projects") or {}).items() if v.get("mcpServers")}
        report["mcp_local_scope_entry"] = local
        report["key_absent_from_claude_json"] = KEY not in claude_json

        # 2) headersHelper: sin confianza en la carpeta y con ella
        baas.state.requests.clear()
        run([claude, "mcp", "list"])
        report["mcp_auth_without_trust"] = [r["headers"].get("Authorization", "-")[:12] for r in baas.state.requests]
        cfg = json.loads((home / ".claude.json").read_text())
        for path in list(local) or [str(proj)]:
            cfg.setdefault("projects", {}).setdefault(path, {})["hasTrustDialogAccepted"] = True
        (home / ".claude.json").write_text(json.dumps(cfg))
        baas.state.requests.clear()
        r = run([claude, "mcp", "list"])
        report["mcp_list_output"] = r.stdout.strip()
        report["mcp_auth_with_trust_ok"] = any(
            r["headers"].get("Authorization") == f"Bearer {KEY}" for r in baas.state.requests
        )

        def ask(prompt: str, extra: dict[str, str], *flags: str) -> dict[str, object]:
            anthropic.requests.clear()
            res = run([claude, "-p", *flags, prompt], extra)
            msgs = anthropic.messages()
            last = msgs[-1] if msgs else None
            return {
                "exit": res.returncode,
                "answered": TEXT in res.stdout,
                "stderr_tail": res.stderr.strip()[-300:],
                "paths": sorted({m["path"].split("?")[0] for m in msgs}),
                "x_baas_api_key_ok": bool(last and last["headers"].get("X-Baas-Api-Key") == KEY),
                "x_api_key": (last["headers"].get("x-api-key") or "")[:16] if last else None,
                "authorization": (last["headers"].get("Authorization") or "")[:20] if last else None,
                "anthropic_beta": last["headers"].get("anthropic-beta") if last else None,
                "body": b"\n".join(m["body"] for m in msgs).decode("utf-8", "replace"),
            }

        # 3) Base URL y cabecera desde .claude/settings.local.json, con API key de proveedor
        res = ask("hola", {"ANTHROPIC_API_KEY": FAKE_PROVIDER_KEY})
        res.pop("body")
        report["proxy_with_provider_api_key"] = res

        # 4) Con OAuth (token ficticio de suscripción): ¿se reenvía al proxy?
        res = ask("hola", {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_OAUTH})
        res.pop("body")
        report["proxy_with_oauth_token"] = res

        # 5) Prompts del MCP como /mcp__baas__<skill>
        res = ask("/mcp__baas__informe", {"ANTHROPIC_API_KEY": FAKE_PROVIDER_KEY})
        reached = "PROMPT-INFORME" in str(res["body"])
        report["mcp_prompt_slash_command"] = {"exit": res["exit"], "prompt_reached_model": reached}

        # 6) Skills del plugin: nombre corto y con espacio de nombres
        for cmd in ("/baas-status", "/baas:baas-status"):
            res = ask(cmd, {"ANTHROPIC_API_KEY": FAKE_PROVIDER_KEY}, "--plugin-dir", str(REPO))
            report[f"plugin_skill {cmd}"] = {"exit": res["exit"], "skill_loaded": "# /baas-status" in res["body"]}

        # 7) ¿Expande Claude Code ${VAR} en el bloque env de settings?
        settings_path = proj / ".claude" / "settings.local.json"
        settings = json.loads(settings_path.read_text())
        settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = "X-Baas-Api-Key: ${BAAS_API_KEY}"
        settings_path.write_text(json.dumps(settings))
        res = ask("hola", {"ANTHROPIC_API_KEY": FAKE_PROVIDER_KEY, "BAAS_API_KEY": KEY})
        last = anthropic.messages()[-1] if anthropic.messages() else None
        report["env_var_expansion_in_settings"] = {
            "expanded": res["x_baas_api_key_ok"],
            "header_received": (last["headers"].get("X-Baas-Api-Key") or "")[:20] if last else None,
        }

    print(json.dumps(report, indent=2, ensure_ascii=False))
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
