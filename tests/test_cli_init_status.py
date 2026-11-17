from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_baas import FakeBaasServer, dev_api_key

from baas_plugin import cli, envfile
from conftest import mode_of

FAKE_CLAUDE = """#!{python}
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""


@pytest.fixture
def server() -> Iterator[FakeBaasServer]:
    with FakeBaasServer() as srv:
        yield srv


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE_CLAUDE.format(python=sys.executable))
    exe.chmod(0o755)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("BAAS_CLAUDE_BIN", str(exe))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    return log


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def save_key(email: str, app_url: str | None) -> str:
    key = dev_api_key(email)
    values = {"BAAS_API_KEY": key}
    if app_url:
        values["BAAS_APP_URL"] = app_url
    envfile.update_env(envfile.BaasPaths.default(), values)
    return key


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = cli.main(args)
    out = capsys.readouterr()
    return code, out.out + out.err


def init(project: Path, *extra: str) -> list[str]:
    return ["init", "--project", str(project), *extra]


# --- /baas-init --------------------------------------------------------------------------


def test_init_single_org_writes_everything(home, project, server, fake_claude, capsys) -> None:
    key = save_key("analista@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert key not in out
    assert "demo" in out

    env = json.loads((project / ".claude/settings.local.json").read_text())["env"]
    assert env["ANTHROPIC_BASE_URL"] == f"{server.url}/proxy/org/demo/anthropic"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == f"X-Baas-Api-Key: {key}"
    binding = json.loads((project / ".claude/baas.local.json").read_text())
    assert binding["org"] == "demo"
    assert binding["mcp_url"] == f"{server.url}/org/demo/mcp"
    assert key not in json.dumps(binding)
    gitignore = (project / ".gitignore").read_text()
    assert ".claude/settings.local.json" in gitignore
    assert ".claude/baas.local.json" in gitignore

    helper = home / "baas-ai-ip" / "bin" / "baas_mcp_headers.py"
    assert helper.exists()
    if os.name == "posix":
        assert mode_of(helper) == 0o700

    remove, add = calls(fake_claude)
    assert remove["argv"][:3] == ["mcp", "remove", "baas"]
    assert add["argv"][:3] == ["mcp", "add-json", "baas"]
    assert add["argv"][-2:] == ["--scope", "local"]
    assert Path(add["cwd"]).resolve() == project.resolve()
    server_cfg = json.loads(add["argv"][3])
    assert server_cfg["type"] == "http"
    assert server_cfg["url"] == f"{server.url}/org/demo/mcp"
    assert str(helper) in server_cfg["headersHelper"]
    assert key not in add["argv"][3]  # la clave no se copia a ~/.claude.json


def test_init_is_idempotent(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", server.url)
    assert run(init(project), capsys)[0] == 0
    snapshot = {p: (project / p).read_bytes() for p in (".gitignore", ".claude/settings.local.json")}
    assert run(init(project), capsys)[0] == 0
    assert {p: (project / p).read_bytes() for p in snapshot} == snapshot


def test_init_without_key_does_not_touch_project(home, project, server, fake_claude, capsys) -> None:
    code, out = run(init(project), capsys)
    assert code == 2
    assert "BAAS_KEY_MISSING" in out
    assert "set-key" in out
    assert list(project.iterdir()) == []


def test_init_without_app_url_asks_for_it(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", None)
    code, out = run(init(project), capsys)
    assert code == 2
    assert "BAAS_APP_URL_MISSING" in out


def test_init_app_url_flag_is_saved(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", None)
    assert run(init(project, "--app-url", server.url), capsys)[0] == 0
    assert envfile.load_credentials(envfile.BaasPaths.default()).app_url == server.url


def test_init_many_orgs_choose_keep_and_switch(home, project, server, fake_claude, capsys) -> None:
    save_key("multi@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 3
    assert "BAAS_ORG_SELECTION_REQUIRED" in out
    assert "demo" in out
    assert "otra" in out
    assert not (project / ".claude").exists()

    assert run(init(project, "--org", "otra"), capsys)[0] == 0
    assert run(init(project), capsys)[0] == 0  # conserva la vinculación
    assert json.loads((project / ".claude/baas.local.json").read_text())["org"] == "otra"

    assert run(init(project, "--org", "demo"), capsys)[0] == 0  # cambio de organización
    env = json.loads((project / ".claude/settings.local.json").read_text())["env"]
    assert env["ANTHROPIC_BASE_URL"].endswith("/org/demo/anthropic")
    assert env["ANTHROPIC_CUSTOM_HEADERS"].count("X-Baas-Api-Key") == 1


def test_init_unknown_org(home, project, server, fake_claude, capsys) -> None:
    save_key("multi@demo.test", server.url)
    code, _ = run(init(project, "--org", "ajena"), capsys)
    assert code == 8


def test_init_zero_orgs_warns(home, project, server, fake_claude, capsys) -> None:
    save_key("sinorg@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 4
    assert "ninguna organización" in out
    assert not (project / ".claude").exists()


def test_init_invalid_key(home, project, server, fake_claude, capsys) -> None:
    save_key("nadie@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 5
    assert "no es válida" in out


def test_init_without_network(home, project, fake_claude, capsys) -> None:
    save_key("analista@demo.test", "http://127.0.0.1:9")
    code, _ = run(init(project), capsys)
    assert code == 6


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda s: setattr(s.state, "global_kill_switch", True), "detenida temporalmente"),
        (lambda s: setattr(s.state.orgs["demo"], "blocked", True), "bloqueada por la plataforma"),
        (lambda s: setattr(s.state.orgs["demo"], "paused", True), "pausada por su administrador"),
    ],
)
def test_init_binds_but_warns_on_kill_switch(home, project, server, fake_claude, capsys, setup, expected) -> None:
    setup(server)
    save_key("analista@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 0
    assert expected in out


def test_init_refuses_tracked_settings(home, project, server, fake_claude, capsys) -> None:
    subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
    (project / ".claude").mkdir()
    (project / ".claude/settings.local.json").write_text("{}\n")
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    save_key("analista@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 7, out
    assert (project / ".claude/settings.local.json").read_text() == "{}\n"


def test_init_without_claude_cli_explains_manual_step(home, project, server, fake_claude, capsys, monkeypatch):
    monkeypatch.setenv("BAAS_CLAUDE_BIN", str(project / "no-existe"))
    save_key("analista@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 0
    assert "claude mcp add-json baas" in out
    assert dev_api_key("analista@demo.test") not in out


def test_init_reports_mcp_registration_failure(home, project, server, fake_claude, capsys, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    save_key("analista@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 0  # el proxy queda configurado; el paso del MCP se explica
    assert "claude mcp add-json baas" in out


# --- set-key ----------------------------------------------------------------------------------


def test_set_key_from_stdin_masks_output(home, capsys, monkeypatch, analista_key) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(analista_key + "\n"))
    code, out = run(["set-key", "--stdin", "--app-url", "https://app.example.com"], capsys)
    assert code == 0
    assert analista_key not in out
    assert analista_key[:12] in out
    creds = envfile.load_credentials(envfile.BaasPaths.default())
    assert creds.api_key == analista_key
    assert creds.app_url == "https://app.example.com"


def test_set_key_rejects_bad_key(home, capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-ant-no\n"))
    code, _ = run(["set-key", "--stdin"], capsys)
    assert code == 2
    assert not envfile.BaasPaths.default().env_file.exists()


def test_set_key_without_terminal_refuses_interactive(home, capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    code, out = run(["set-key"], capsys)
    assert code == 2
    assert "terminal" in out


# --- /baas-status -----------------------------------------------------------------------------


def test_status_unbound_project(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", server.url)
    code, out = run(["status", "--project", str(project), "--no-mcp"], capsys)
    assert code == 0
    assert "analista@demo.test" in out
    assert "no está vinculado" in out


def test_status_bound_project_shows_roles_and_switches(home, project, server, fake_claude, capsys) -> None:
    key = save_key("analista@demo.test", server.url)
    run(init(project), capsys)
    server.state.orgs["demo"].paused = True
    code, out = run(["status", "--project", str(project), "--no-mcp"], capsys)
    assert code == 0
    assert key not in out
    for fragment in ("analista@demo.test", "demo", "Demo Clínica", "analista", "USER", "pausada", "AI-IP"):
        assert fragment in out, fragment


def test_status_warns_when_session_bypasses_proxy(home, project, server, fake_claude, capsys, monkeypatch) -> None:
    save_key("analista@demo.test", server.url)
    run(init(project), capsys)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    code, out = run(["status", "--project", str(project), "--no-mcp"], capsys)
    assert code == 0
    assert "no pasa por el proxy" in out


def test_status_reports_lost_access(home, project, server, fake_claude, capsys) -> None:
    save_key("multi@demo.test", server.url)
    run(init(project, "--org", "otra"), capsys)
    server.state.users = [u for u in server.state.users if u.email != "multi@demo.test"] + [
        type(server.state.users[0])("multi@demo.test", False, [])
    ]
    _, out = run(["status", "--project", str(project), "--no-mcp"], capsys)
    assert "ya no tienes acceso" in out


def test_headers_helper_prints_authorization(home, analista_key) -> None:
    helper = Path(__file__).resolve().parents[1] / "scripts" / "baas_mcp_headers.py"
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": analista_key})
    res = subprocess.run([sys.executable, str(helper)], capture_output=True, text=True, env={"HOME": str(home)})
    assert res.returncode == 0
    assert json.loads(res.stdout) == {"Authorization": f"Bearer {analista_key}"}


def test_headers_helper_without_key_fails_quietly(home) -> None:
    helper = Path(__file__).resolve().parents[1] / "scripts" / "baas_mcp_headers.py"
    res = subprocess.run([sys.executable, str(helper)], capture_output=True, text=True, env={"HOME": str(home)})
    assert res.returncode == 1
    assert res.stdout == ""


def test_status_checks_mcp(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", server.url)
    run(init(project), capsys)
    code, out = run(["status", "--project", str(project)], capsys)
    assert code == 0
    assert "MCP `baas`: accesible" in out
    server.state.global_kill_switch = True
    code, out = run(["status", "--project", str(project)], capsys)
    assert "KILL_SWITCH_ACTIVE" in out
