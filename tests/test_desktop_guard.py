"""Guardia de sesión (hooks) y Claude Desktop en modo gateway (ver README, «Claude Desktop»)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import baas_guard
from baas_plugin import cli, desktop_config, envfile
from test_cli_init_status import fake_claude, init, run, save_key, server  # noqa: F401 (fixtures)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
PLUGIN_ROOT = SCRIPTS.parent
PROXY = "https://proxy.example.test/org/demo/anthropic"
BINDING = {"org": "demo", "proxy_anthropic_url": PROXY}
ANTHROPIC_KEY = "sk-ant-api03-" + "x" * 40


# --- diagnose ---------------------------------------------------------------------------------


def test_unbound_project_passes() -> None:
    assert baas_guard.diagnose(None, {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}) is None


@pytest.mark.parametrize("url", [PROXY, PROXY + "/"])
def test_session_through_proxy_passes(url: str) -> None:
    assert baas_guard.diagnose(BINDING, {"ANTHROPIC_BASE_URL": url}) is None


def test_desktop_session_is_explained() -> None:
    env = {"ANTHROPIC_BASE_URL": "https://api.anthropic.com", "CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}
    problem = baas_guard.diagnose(BINDING, env)
    assert problem is not None
    for fragment in ("«demo»", "Claude Desktop", "desktop-config", "revócala", "api.anthropic.com"):
        assert fragment in problem, fragment


def test_cli_session_without_trust_is_explained() -> None:
    problem = baas_guard.diagnose(BINDING, {"CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert problem is not None
    assert "sin definir" in problem
    assert "confianza" in problem
    assert "Desktop" not in problem


def test_unreadable_binding_fails_closed(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "baas.local.json").write_text("{no es json")
    binding = baas_guard.read_binding(str(tmp_path))
    assert binding == {}
    assert baas_guard.diagnose(binding, {"ANTHROPIC_BASE_URL": PROXY}) is not None


# --- hooks -------------------------------------------------------------------------------------


def run_guard(project: Path, event: str, base_url: str | None, entrypoint: str = "claude-desktop"):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE"))}
    env.update({"CLAUDE_PROJECT_DIR": str(project), "CLAUDE_CODE_ENTRYPOINT": entrypoint})
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    stdin = json.dumps({"hook_event_name": "x", "cwd": str(project), "prompt": "hola"})
    res = subprocess.run(
        [sys.executable, str(SCRIPTS / "baas_guard.py"), "--event", event],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout) if res.stdout.strip() else None


def bind(project: Path) -> None:
    (project / ".claude").mkdir(exist_ok=True)
    (project / ".claude" / "baas.local.json").write_text(json.dumps(BINDING))


def test_prompt_hook_blocks_in_desktop(project: Path) -> None:
    bind(project)
    out = run_guard(project, "prompt", "https://api.anthropic.com")
    assert out["decision"] == "block"
    assert "Claude Desktop" in out["reason"]


def test_session_start_hook_warns(project: Path) -> None:
    bind(project)
    out = run_guard(project, "session-start", "https://api.anthropic.com")
    assert "decision" not in out
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert out["systemMessage"] == out["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("event", ["prompt", "session-start"])
def test_hooks_silent_when_proxied_or_unbound(project: Path, event: str) -> None:
    assert run_guard(project, event, "https://api.anthropic.com") is None  # no vinculado
    bind(project)
    assert run_guard(project, event, PROXY) is None  # por el proxy (CLI o Desktop en modo gateway)


def test_hooks_manifest_points_to_guard() -> None:
    manifest = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
    commands = {
        event: [h["command"] for group in groups for h in group["hooks"]] for event, groups in manifest["hooks"].items()
    }
    assert set(commands) == {"SessionStart", "UserPromptSubmit"}
    assert all("${CLAUDE_PLUGIN_ROOT}/scripts/baas_guard.py" in c for cs in commands.values() for c in cs)
    assert "--event prompt" in commands["UserPromptSubmit"][0]


# --- status ------------------------------------------------------------------------------------


def test_status_explains_desktop(home, project, server, fake_claude, capsys, monkeypatch) -> None:  # noqa: F811
    save_key("analista@demo.test", server.url)
    run(init(project), capsys)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    _, out = run(["status", "--project", str(project), "--no-mcp"], capsys)
    assert "AVISO: este proyecto está vinculado" in out
    assert "desktop-config" in out


def test_status_proxy_active(home, project, server, fake_claude, capsys, monkeypatch) -> None:  # noqa: F811
    save_key("analista@demo.test", server.url)
    run(init(project), capsys)
    binding = json.loads((project / ".claude" / "baas.local.json").read_text())
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", binding["proxy_anthropic_url"])
    _, out = run(["status", "--project", str(project), "--no-mcp"], capsys)
    assert "Proxy activo en esta sesión: sí." in out


# --- Claude Desktop en modo gateway ------------------------------------------------------------


def test_gateway_config_posix_and_windows(tmp_path: Path) -> None:
    helper = tmp_path / "baas_desktop_credentials.py"
    posix = desktop_config.gateway_config(PROXY, helper, windows=False)
    assert posix == {
        "inferenceProvider": "gateway",
        "inferenceGatewayBaseUrl": PROXY,
        "inferenceGatewayAuthScheme": "x-api-key",
        "inferenceCredentialKind": "helper-script",
        "inferenceCredentialHelper": str(helper),
    }
    win = desktop_config.gateway_config(PROXY, helper, windows=True)
    assert win["inferenceCredentialHelper"] == sys.executable
    assert win["inferenceCredentialHelperArgs"] == [str(helper)]


def test_desktop_config_prints_config_without_secrets(home, project, server, fake_claude, capsys) -> None:  # noqa: F811
    key = save_key("analista@demo.test", server.url)
    run(init(project), capsys)
    code, out = run(["desktop-config", "--project", str(project)], capsys)
    assert code == 0
    assert key not in out
    assert '"inferenceProvider": "gateway"' in out
    assert "/org/demo/anthropic" in out
    assert "GLOBAL" in out
    assert "set-key --anthropic" in out  # falta la de Anthropic
    helper = home / "baas-ai-ip" / "bin" / "baas_desktop_credentials.py"
    assert helper.is_file()
    if os.name == "posix":
        assert helper.stat().st_mode & 0o777 == 0o700


def test_set_key_anthropic(home, capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(ANTHROPIC_KEY + "\n"))
    code, out = run(["set-key", "--anthropic", "--stdin"], capsys)
    assert code == 0
    assert ANTHROPIC_KEY not in out
    values, _ = envfile.read_env(envfile.BaasPaths.default())
    assert values["ANTHROPIC_API_KEY"] == ANTHROPIC_KEY


def test_set_key_anthropic_rejects_baas_key(home, capsys, monkeypatch, analista_key) -> None:
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(analista_key + "\n"))
    code, out = run(["set-key", "--anthropic", "--stdin"], capsys)
    assert code == 2
    assert analista_key not in out


def test_credentials_helper(home, analista_key) -> None:
    helper = SCRIPTS / "baas_desktop_credentials.py"
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": analista_key})
    res = subprocess.run([sys.executable, str(helper)], capture_output=True, text=True, env={"HOME": str(home)})
    assert res.returncode == 1
    assert res.stdout == ""
    assert "ANTHROPIC_API_KEY" in res.stderr
    assert analista_key not in res.stderr
    envfile.update_env(envfile.BaasPaths.default(), {"ANTHROPIC_API_KEY": ANTHROPIC_KEY})
    res = subprocess.run([sys.executable, str(helper)], capture_output=True, text=True, env={"HOME": str(home)})
    assert res.returncode == 0
    assert json.loads(res.stdout) == {"token": ANTHROPIC_KEY, "headers": {"X-Baas-Api-Key": analista_key}}


def test_cli_module_exposes_desktop_config() -> None:
    assert "desktop-config" in cli.build_parser().format_help()
