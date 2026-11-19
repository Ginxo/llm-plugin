from __future__ import annotations

import dataclasses
import io
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_baas import FakeBaasServer, dev_api_key

from baas_plugin import cli, device, envfile
from baas_plugin.errors import NetworkError
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


@pytest.fixture(autouse=True)
def opened_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Nunca se abre un navegador de verdad; se anota qué enlaces se habrían abierto (con pantalla simulada)."""
    urls: list[str] = []
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url, *a, **k: urls.append(url) or True)
    return urls


FORBIDDEN_IN_MESSAGES = ("~/", ".env", "baas.py", "ANTHROPIC_BASE_URL", "pending.json", "exit code", "Traceback")


def assert_plain_message(out: str) -> None:
    for fragment in FORBIDDEN_IN_MESSAGES:
        assert fragment not in out, fragment


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


def test_init_without_key_and_without_any_url_asks_for_it(home, project, server, fake_claude, capsys) -> None:
    code, out = run(init(project), capsys)
    assert code == 2
    assert "BAAS_APP_URL_MISSING" in out
    assert list(project.iterdir()) == []
    assert_plain_message(out)


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


def assert_plain_network_error(out: str) -> None:
    assert "BAAS_NETWORK_ERROR" in out
    out = "\n".join(line for line in out.splitlines() if not line.startswith("[BAAS_APP_URL "))  # marca para la skill
    assert_plain_message(out)
    for fragment in ("127.0.0.1", "http", "handshake", "HTTP", "Traceback"):
        assert fragment not in out, fragment


def test_init_without_network(home, project, fake_claude, capsys) -> None:
    save_key("analista@demo.test", "http://127.0.0.1:9")
    code, out = run(init(project), capsys)
    assert code == 6
    assert_plain_network_error(out)
    assert list(project.iterdir()) == []


@pytest.mark.parametrize("status", [429, 500, 503])
def test_init_handshake_server_errors_are_plain(home, project, server, fake_claude, capsys, status) -> None:
    save_key("analista@demo.test", server.url)
    server.state.handshake_status = status
    code, out = run(init(project), capsys)
    assert code == 6
    assert_plain_network_error(out)
    assert list(project.iterdir()) == []
    assert envfile.read_env(paths_())[0].get("BAAS_API_KEY")  # una caída del servidor no descarta la clave


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


# --- /baas-init guiado: aprobación en el navegador ---------------------------------------------

APP = "https://app.example.com"
DEVICE_CODE = "dev-code-secret-123"
USER_CODE = "KQRT-7MWB"
LINK = f"{APP}/connect?code={USER_CODE}"


class FakeDevice:
    """Sustituye a `device.start/poll/wait`: ninguna prueba de init toca la red del flujo de dispositivo."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.start_calls: list[tuple[str, str]] = []
        self.wait_calls: list[tuple[str, str, int, int]] = []
        self.start_error: Exception | None = None
        self.wait_result: device.DevicePoll = device.DevicePoll("pending")
        self.wait_error: Exception | None = None
        self.api_key: str | None = None
        monkeypatch.setattr(cli.device, "start", self._start)
        monkeypatch.setattr(cli.device, "wait", self._wait)

    def _start(self, app_url: str, client_name: str, **_: object) -> device.DeviceStart:
        self.start_calls.append((app_url, client_name))
        if self.start_error:
            raise self.start_error
        return device.DeviceStart(DEVICE_CODE, USER_CODE, f"{app_url}/connect?code={USER_CODE}", 600, 3)

    def _wait(self, app_url: str, device_code: str, *, interval: int, max_seconds: int, **_: object):
        self.wait_calls.append((app_url, device_code, interval, max_seconds))
        if self.wait_error:
            raise self.wait_error
        return self.wait_result


@pytest.fixture
def fake_device(monkeypatch: pytest.MonkeyPatch) -> FakeDevice:
    return FakeDevice(monkeypatch)


def paths_() -> envfile.BaasPaths:
    return envfile.BaasPaths.default()


def seed_pending(expires_at: float | None = None, app_url: str = APP) -> None:
    """Lo que deja una primera llamada de `init`: la URL recordada y la conexión pendiente."""
    envfile.update_env(paths_(), {"BAAS_APP_URL": app_url})
    device.save_pending(
        paths_(),
        device.PendingFlow(
            app_url,
            DEVICE_CODE,
            USER_CODE,
            f"{app_url}/connect?code={USER_CODE}",
            time.time() + 600 if expires_at is None else expires_at,
        ),
    )


def test_init_without_key_starts_device_flow_exit_20(home, project, fake_device, opened_urls, capsys) -> None:
    code, out = run(init(project, "--app-url", APP), capsys)
    assert code == 20, out
    assert "BAAS_APPROVAL_PENDING" in out
    assert LINK in out
    assert USER_CODE in out
    assert_plain_message(out)
    assert DEVICE_CODE not in out
    assert opened_urls == [LINK]
    (url, client_name) = fake_device.start_calls[0]
    assert url == APP
    assert client_name.startswith(f"{project.name} en ")

    pending = paths_().pending_file
    if os.name == "posix":
        assert mode_of(pending) == 0o600
    data = json.loads(pending.read_text())
    assert data["device_code"] == DEVICE_CODE
    assert "api_key" not in data
    assert envfile.read_env(paths_())[0]["BAAS_APP_URL"] == APP  # la URL se guarda tras contactar con éxito
    assert list(project.iterdir()) == []  # el proyecto no se toca hasta que se aprueba


def test_init_uses_url_from_environment(home, project, fake_device, monkeypatch, capsys) -> None:
    monkeypatch.setenv("BAAS_APP_URL", APP + "/")
    assert run(init(project), capsys)[0] == 20
    assert fake_device.start_calls[0][0] == APP


def test_init_browser_failure_is_ignored(home, project, fake_device, monkeypatch, capsys) -> None:
    def boom(*a: object, **k: object) -> bool:
        raise RuntimeError("sin navegador")

    monkeypatch.setattr(cli.webbrowser, "open", boom)
    code, out = run(init(project, "--app-url", APP), capsys)
    assert code == 20
    assert LINK in out


def test_init_client_name_is_limited_to_100_chars(home, tmp_path, fake_device, capsys) -> None:
    long_project = tmp_path / ("p" * 150)
    long_project.mkdir()
    assert run(init(long_project, "--app-url", APP), capsys)[0] == 20
    assert 1 <= len(fake_device.start_calls[0][1]) <= 100


def test_init_invalid_app_url_is_explained_in_plain_spanish(home, project, fake_device, capsys) -> None:
    for bad in ("esto no es una url", "http://app.example.com"):
        code, out = run(init(project, "--app-url", bad), capsys)
        assert code == 2
        assert "BAAS_CONFIG_ERROR" in out
        assert_plain_message(out)
        assert "HTTP" not in out
    assert fake_device.start_calls == []
    assert not paths_().env_file.exists()


def test_init_revoked_key_restarts_flow_with_friendly_notice(home, project, server, fake_device, capsys) -> None:
    old = save_key("nadie@demo.test", server.url)  # la clave no existe en el servidor: el handshake la rechaza
    code, out = run(init(project), capsys)
    assert code == 20, out
    assert "Tu conexión anterior ya no es válida; vamos a renovarla" in out
    assert old not in out
    assert_plain_message(out)
    assert fake_device.start_calls[0][0] == server.url
    assert paths_().pending_file.exists()
    assert list(project.iterdir()) == []


def test_init_malformed_stored_key_is_treated_as_stale(home, project, fake_device, capsys) -> None:
    envfile.update_env(paths_(), {"BAAS_API_KEY": "no-es-una-clave", "BAAS_APP_URL": APP})
    code, out = run(init(project), capsys)
    assert code == 20
    assert "Tu conexión anterior ya no es válida; vamos a renovarla" in out
    assert "no-es-una-clave" not in out


def test_init_wait_pending_exit_21(home, project, fake_device, capsys) -> None:
    seed_pending()
    code, out = run(init(project, "--wait", "--app-url", APP), capsys)
    assert code == 21, out
    assert "BAAS_APPROVAL_WAITING" in out
    assert_plain_message(out)
    assert fake_device.wait_calls == [(APP, DEVICE_CODE, 3, 90)]
    assert fake_device.start_calls == []
    assert paths_().pending_file.exists()  # se puede volver a esperar


def test_init_wait_approved_writes_key_and_binds(home, project, server, fake_claude, fake_device, capsys) -> None:
    seed_pending(app_url=server.url)
    new_key = dev_api_key("analista@demo.test")
    fake_device.wait_result = device.DevicePoll("approved", new_key, "analista@demo.test")
    code, out = run(init(project, "--wait"), capsys)
    assert code == 0, out
    assert new_key not in out
    assert not paths_().pending_file.exists()
    values = envfile.read_env(paths_())[0]
    assert values["BAAS_API_KEY"] == new_key
    assert values["BAAS_APP_URL"] == server.url
    if os.name == "posix":
        assert mode_of(paths_().env_file) == 0o600
    env = json.loads((project / ".claude/settings.local.json").read_text())["env"]
    assert env["ANTHROPIC_BASE_URL"] == f"{server.url}/proxy/org/demo/anthropic"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == f"X-Baas-Api-Key: {new_key}"
    assert json.loads((project / ".claude/baas.local.json").read_text())["org"] == "demo"
    assert fake_device.start_calls == []


def test_init_wait_approved_with_many_orgs_keeps_key_and_asks_for_org(
    home, project, server, fake_claude, fake_device, capsys
) -> None:
    seed_pending(app_url=server.url)
    new_key = dev_api_key("multi@demo.test")
    fake_device.wait_result = device.DevicePoll("approved", new_key, "multi@demo.test")
    code, _ = run(init(project, "--wait"), capsys)
    assert code == 3
    assert envfile.read_env(paths_())[0]["BAAS_API_KEY"] == new_key
    assert not paths_().pending_file.exists()
    assert run(init(project, "--org", "otra"), capsys)[0] == 0  # sigue por el camino de siempre


def test_init_wait_approved_without_orgs_exit_4(home, project, server, fake_claude, fake_device, capsys) -> None:
    seed_pending(app_url=server.url)
    fake_device.wait_result = device.DevicePoll("approved", dev_api_key("sinorg@demo.test"), "sinorg@demo.test")
    code, _ = run(init(project, "--wait"), capsys)
    assert code == 4


def test_init_wait_denied_exit_22(home, project, fake_device, capsys) -> None:
    seed_pending()
    fake_device.wait_result = device.DevicePoll("denied")
    code, out = run(init(project, "--wait", "--app-url", APP), capsys)
    assert code == 22
    assert "BAAS_APPROVAL_DENIED" in out
    assert_plain_message(out)
    assert not paths_().pending_file.exists()
    assert list(project.iterdir()) == []


def test_init_wait_expired_exit_23(home, project, fake_device, capsys) -> None:
    seed_pending()
    fake_device.wait_result = device.DevicePoll("expired")
    code, out = run(init(project, "--wait", "--app-url", APP), capsys)
    assert code == 23
    assert "BAAS_APPROVAL_EXPIRED" in out
    assert_plain_message(out)
    assert not paths_().pending_file.exists()


def test_init_wait_network_failure_keeps_pending(home, project, fake_device, capsys) -> None:
    seed_pending()
    fake_device.wait_error = NetworkError("No he podido conectar con BaaS-AI-IP.")
    code, _ = run(init(project, "--wait", "--app-url", APP), capsys)
    assert code == 6
    assert paths_().pending_file.exists()


def test_init_wait_ignores_the_stored_key(home, project, server, fake_device, capsys) -> None:
    save_key("analista@demo.test", server.url)
    seed_pending(app_url=server.url)
    server.state.handshake_status = 500  # si --wait hiciera handshake con la clave de antes, fallaría con 6
    code, _ = run(init(project, "--wait"), capsys)
    assert code == 21


def test_init_expired_pending_starts_over(home, project, fake_device, capsys) -> None:
    seed_pending(expires_at=time.time() - 5)
    code, out = run(init(project, "--wait", "--app-url", APP), capsys)
    assert code == 20, out
    assert fake_device.wait_calls == []
    assert len(fake_device.start_calls) == 1
    assert json.loads(paths_().pending_file.read_text())["expires_at"] > time.time()


def test_init_wait_without_pending_starts_the_flow(home, project, fake_device, capsys) -> None:
    code, out = run(init(project, "--wait", "--app-url", APP), capsys)
    assert code == 20
    assert LINK in out


def test_init_wait_for_another_server_starts_over(home, project, fake_device, capsys) -> None:
    seed_pending(app_url="https://otra.example.com")
    code, _ = run(init(project, "--wait", "--app-url", APP), capsys)
    assert code == 20
    assert fake_device.wait_calls == []
    assert fake_device.start_calls[0][0] == APP


def test_init_unreachable_url_not_saved_exit_6(home, project, fake_device, capsys) -> None:
    fake_device.start_error = NetworkError("No he podido conectar con BaaS-AI-IP.", hint="Inténtalo más tarde.")
    code, out = run(init(project, "--app-url", "https://no-existe.example.com"), capsys)
    assert code == 6
    assert "BAAS_NETWORK_ERROR" in out
    assert_plain_message(out)
    assert not paths_().env_file.exists()
    assert not paths_().pending_file.exists()


def test_init_unreachable_url_keeps_the_saved_one(home, project, fake_device, capsys) -> None:
    envfile.update_env(paths_(), {"BAAS_APP_URL": APP})
    fake_device.start_error = NetworkError("No he podido conectar con BaaS-AI-IP.")
    code, _ = run(init(project, "--app-url", "https://no-existe.example.com"), capsys)
    assert code == 6
    assert envfile.read_env(paths_())[0]["BAAS_APP_URL"] == APP


def test_init_existing_valid_key_keeps_old_behaviour(home, project, server, fake_claude, fake_device, capsys) -> None:
    key = save_key("analista@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert key not in out
    assert fake_device.start_calls == []
    assert fake_device.wait_calls == []
    assert not paths_().pending_file.exists()


def test_init_repairs_corrupt_settings_local(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", server.url)
    (project / ".claude").mkdir()
    (project / ".claude/settings.local.json").write_text('{"env": {"viejo": ')
    (project / ".claude/baas.local.json").write_text("no es json")
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert "He reparado una configuración dañada (copia guardada)" in out
    assert (project / ".claude/settings.local.json.bak").read_text() == '{"env": {"viejo": '
    assert (project / ".claude/baas.local.json.bak").read_text() == "no es json"
    assert "ANTHROPIC_BASE_URL" in json.loads((project / ".claude/settings.local.json").read_text())["env"]
    assert json.loads((project / ".claude/baas.local.json").read_text())["org"] == "demo"
    gitignore = (project / ".gitignore").read_text()
    assert ".claude/settings.local.json.bak" in gitignore  # la copia podía contener la clave


def test_init_valid_config_is_not_reported_as_repaired(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", server.url)
    assert run(init(project), capsys)[0] == 0
    code, out = run(init(project), capsys)
    assert code == 0
    assert "reparado" not in out
    assert not (project / ".claude/settings.local.json.bak").exists()


# --- reparaciones y copias --------------------------------------------------------------------


def test_init_org_selection_exit_leaves_damaged_files_untouched(home, project, server, fake_claude, capsys) -> None:
    save_key("multi@demo.test", server.url)
    (project / ".claude").mkdir()
    (project / ".claude/settings.local.json").write_text("{roto")
    (project / ".claude/baas.local.json").write_text("también roto")
    code, out = run(init(project), capsys)
    assert code == 3
    assert "reparado" not in out
    assert (project / ".claude/settings.local.json").read_text() == "{roto"
    assert (project / ".claude/baas.local.json").read_text() == "también roto"
    assert sorted(p.name for p in (project / ".claude").iterdir()) == ["baas.local.json", "settings.local.json"]
    assert not (project / ".gitignore").exists()
    # elegida la organización, ya sí se repara y se ignoran las copias
    assert run(init(project, "--org", "otra"), capsys)[0] == 0
    assert (project / ".claude/settings.local.json.bak").read_text() == "{roto"
    assert ".claude/settings.local.json.bak*" in (project / ".gitignore").read_text()


def test_init_no_organizations_exit_leaves_damaged_files_untouched(home, project, server, fake_claude, capsys) -> None:
    save_key("sinorg@demo.test", server.url)
    (project / ".claude").mkdir()
    (project / ".claude/settings.local.json").write_text("{roto")
    code, _ = run(init(project), capsys)
    assert code == 4
    assert (project / ".claude/settings.local.json").read_text() == "{roto"
    assert not (project / ".claude/settings.local.json.bak").exists()


def test_init_ignores_preexisting_backups_on_next_run(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", server.url)
    (project / ".claude").mkdir()
    (project / ".claude/settings.local.json.bak").write_text("copia con clave")
    (project / ".gitignore").write_text("node_modules/\n")
    assert run(init(project), capsys)[0] == 0
    gitignore = (project / ".gitignore").read_text()
    assert ".claude/settings.local.json.bak*" in gitignore
    assert ".claude/baas.local.json.bak*" in gitignore
    assert "node_modules/" in gitignore


def test_init_without_backups_does_not_add_backup_patterns(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", server.url)
    assert run(init(project), capsys)[0] == 0
    assert ".bak" not in (project / ".gitignore").read_text()


def test_init_keeps_earlier_backups_on_second_repair(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", server.url)
    (project / ".claude").mkdir()
    (project / ".claude/settings.local.json").write_text("primero roto")
    assert run(init(project), capsys)[0] == 0
    (project / ".claude/settings.local.json").write_text("segundo roto")
    assert run(init(project), capsys)[0] == 0
    assert (project / ".claude/settings.local.json.bak").read_text() == "primero roto"
    assert (project / ".claude/settings.local.json.bak.1").read_text() == "segundo roto"


# --- enlace pendiente, navegador y clave revocada ---------------------------------------------


def test_second_init_reuses_the_live_pending_link(home, project, fake_device, opened_urls, capsys) -> None:
    code, _ = run(init(project, "--app-url", APP), capsys)
    assert code == 20
    assert len(fake_device.start_calls) == 1
    assert opened_urls == [LINK]

    code, second = run(init(project), capsys)
    assert code == 20
    assert "BAAS_APPROVAL_PENDING" in second
    assert LINK in second
    assert USER_CODE in second
    assert len(fake_device.start_calls) == 1  # no se pidió otro código
    assert opened_urls == [LINK]  # y no se abrió otra pestaña


def test_init_with_another_url_does_not_reuse_the_pending_link(home, project, fake_device, capsys) -> None:
    seed_pending(app_url="https://otra.example.com")
    code, _ = run(init(project, "--app-url", APP), capsys)
    assert code == 20
    assert len(fake_device.start_calls) == 1
    assert json.loads(paths_().pending_file.read_text())["app_url"] == APP


def test_init_with_expired_pending_requests_a_new_link(home, project, fake_device, capsys) -> None:
    seed_pending(expires_at=time.time() - 1)
    assert run(init(project), capsys)[0] == 20
    assert len(fake_device.start_calls) == 1


def test_browser_is_not_opened_on_linux_without_a_display(home, project, fake_device, opened_urls, monkeypatch, capsys):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    code, out = run(init(project, "--app-url", APP), capsys)
    assert code == 20
    assert LINK in out  # el enlace se muestra igualmente
    assert opened_urls == []


@pytest.mark.parametrize("variable", ["DISPLAY", "WAYLAND_DISPLAY"])
def test_browser_is_opened_on_linux_with_a_display(
    home, project, fake_device, opened_urls, monkeypatch, capsys, variable
):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv(variable, "wayland-0")
    assert run(init(project, "--app-url", APP), capsys)[0] == 20
    assert opened_urls == [LINK]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_browser_is_opened_on_desktop_platforms_without_display_variables(
    home, project, fake_device, opened_urls, monkeypatch, capsys, platform
):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(cli.sys, "platform", platform)
    assert run(init(project, "--app-url", APP), capsys)[0] == 20
    assert opened_urls == [LINK]


def test_a_hanging_browser_does_not_block_the_flow(home, project, fake_device, monkeypatch, capsys) -> None:
    release = threading.Event()
    monkeypatch.setattr(cli, "BROWSER_GRACE", 0.05)
    monkeypatch.setattr(cli.webbrowser, "open", lambda *a, **k: release.wait(5))
    started = time.monotonic()
    code, out = run(init(project, "--app-url", APP), capsys)
    release.set()
    assert code == 20
    assert LINK in out
    assert time.monotonic() - started < 2


def test_revoked_key_line_is_removed_from_env(home, project, server, fake_device, capsys) -> None:
    old = save_key("nadie@demo.test", server.url)
    with paths_().env_file.open("a") as fh:
        fh.write("OTRA=valor\n")
    assert run(init(project), capsys)[0] == 20
    text = paths_().env_file.read_text()
    assert "BAAS_API_KEY" not in text
    assert old not in text
    assert "OTRA=valor" in text
    assert f"BAAS_APP_URL={server.url}" in text


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
    assert "AVISO: Este proyecto está conectado a «demo», pero esta sesión aún no está protegida" in out


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


# --- marcas de resultado y de dirección (las lee la skill /baas-init) ---------------------------------------


def last_line(out: str) -> str:
    return [line for line in out.splitlines() if line.strip()][-1]


def test_init_new_binding_ends_with_result_linked(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT linked]"
    assert out.count("[BAAS_RESULT") == 1


def test_init_rerun_on_a_bound_project_ends_with_result_unchanged(home, project, server, fake_claude, capsys) -> None:
    save_key("analista@demo.test", server.url)
    assert last_line(run(init(project), capsys)[1]) == "[BAAS_RESULT linked]"
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT unchanged]"


def test_init_switching_organisation_is_linked(home, project, server, fake_claude, capsys) -> None:
    save_key("multi@demo.test", server.url)
    assert last_line(run(init(project, "--org", "demo"), capsys)[1]) == "[BAAS_RESULT linked]"
    code, out = run(init(project), capsys)  # conserva la organización: nada que cambiar
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT unchanged]"
    code, out = run(init(project, "--org", "otra"), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT linked]"


def test_init_renewed_key_via_wait_is_linked_even_if_already_bound(
    home, project, server, fake_claude, fake_device, capsys
) -> None:
    save_key("analista@demo.test", server.url)
    assert last_line(run(init(project), capsys)[1]) == "[BAAS_RESULT linked]"
    envfile.remove_env_keys(paths_(), ["BAAS_API_KEY"])  # la clave se revocó: la aprobación trae otra
    seed_pending(app_url=server.url)
    new_key = dev_api_key("analista@demo.test")
    fake_device.wait_result = device.DevicePoll("approved", new_key, "analista@demo.test")
    code, out = run(init(project, "--wait"), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT linked]"  # el proyecto ya estaba vinculado, pero la clave es nueva


def test_failed_init_prints_no_result_marker(home, project, fake_device, capsys) -> None:
    code, out = run(init(project, "--app-url", APP), capsys)
    assert code == 20
    assert "BAAS_RESULT" not in out


def test_network_error_prints_the_address_that_was_tried(home, project, fake_device, capsys) -> None:
    fake_device.start_error = NetworkError("No he podido conectar con BaaS-AI-IP.")
    code, out = run(init(project, "--app-url", "https://no-existe.example.com/"), capsys)
    assert code == 6
    assert last_line(out) == "[BAAS_APP_URL https://no-existe.example.com]"
    assert_plain_message(out.replace("[BAAS_APP_URL https://no-existe.example.com]", ""))


def test_address_marker_never_carries_credentials(home, project, fake_device, capsys) -> None:
    fake_device.start_error = NetworkError("No he podido conectar con BaaS-AI-IP.")
    code, out = run(init(project, "--app-url", "https://usuario:secreto@no-existe.example.com:8443/base"), capsys)
    assert code == 6
    marker = last_line(out)
    assert marker == "[BAAS_APP_URL https://no-existe.example.com:8443/base]"
    assert "@" not in marker
    assert "secreto" not in out
    assert "usuario" not in out


def test_missing_address_prints_no_address_marker(home, project, fake_device, capsys) -> None:
    code, out = run(init(project), capsys)
    assert code == 2
    assert "BAAS_APP_URL_MISSING" in out
    assert "[BAAS_APP_URL " not in out


def test_invalid_address_prints_no_address_marker(home, project, fake_device, capsys) -> None:
    code, out = run(init(project, "--app-url", "esto no es una url"), capsys)
    assert code == 2
    assert "[BAAS_APP_URL " not in out


# --- `unchanged` exige que no se haya reescrito nada del proyecto ---------------------------------------------


def bound_project(project: Path, server: FakeBaasServer, capsys: pytest.CaptureFixture[str]) -> str:
    key = save_key("analista@demo.test", server.url)
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT linked]"
    return key


def test_init_with_nothing_to_rewrite_is_unchanged(home, project, server, fake_claude, capsys) -> None:
    bound_project(project, server, capsys)
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT unchanged]"


def test_init_rewriting_a_stale_key_in_the_project_is_linked(home, project, server, fake_claude, capsys) -> None:
    """La clave compartida se renovó desde otro proyecto: aquí la sesión en marcha aún tiene la vieja."""
    bound_project(project, server, capsys)
    settings = project / ".claude/settings.local.json"
    data = json.loads(settings.read_text())
    data["env"]["ANTHROPIC_CUSTOM_HEADERS"] = "X-Baas-Api-Key: baas_vieja"
    settings.write_text(json.dumps(data, indent=2) + "\n")
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT linked]"
    assert "baas_vieja" not in settings.read_text()


def test_init_recreating_a_deleted_settings_file_is_linked(home, project, server, fake_claude, capsys) -> None:
    bound_project(project, server, capsys)
    (project / ".claude/settings.local.json").unlink()
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT linked]"


def test_init_repairing_damaged_json_is_linked(home, project, server, fake_claude, capsys) -> None:
    bound_project(project, server, capsys)
    (project / ".claude/settings.local.json").write_text("{esto no es json")
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT linked]"


def test_init_rewriting_the_binding_is_linked(home, project, server, fake_claude, capsys) -> None:
    """Si cambia la dirección del servidor de conocimiento, el MCP se registra de nuevo y hay que reiniciar."""
    bound_project(project, server, capsys)
    binding_file = project / ".claude/baas.local.json"
    binding = json.loads(binding_file.read_text())
    binding["mcp_url"] = "https://otro.example.com/mcp"
    binding_file.write_text(json.dumps(binding, indent=2, ensure_ascii=False) + "\n")
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT linked]"


def test_init_with_mcp_not_registered_is_linked(home, project, server, fake_claude, capsys, monkeypatch) -> None:
    bound_project(project, server, capsys)
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")  # el registro falla: queda un paso pendiente
    code, out = run(init(project), capsys)
    assert code == 0, out
    assert "AVISO" in out
    assert last_line(out) == "[BAAS_RESULT linked]"


# --- revisión final ----------------------------------------------------------------------------------------


def test_handshake_rate_limit_and_server_errors_keep_their_own_plain_wording(
    home, project, server, fake_claude, capsys
) -> None:
    save_key("analista@demo.test", server.url)
    server.state.handshake_status = 429
    code, out = run(init(project), capsys)
    assert code == 6
    assert "demasiadas" in out
    assert "Espera un minuto" in out
    assert_plain_network_error(out)
    server.state.handshake_status = 503
    code, out = run(init(project), capsys)
    assert code == 6
    assert "no está disponible ahora mismo" in out
    assert "Comprueba tu conexión" not in out
    assert_plain_network_error(out)


def test_plain_connection_failure_keeps_the_check_your_connection_text(home, project, fake_claude, capsys) -> None:
    save_key("analista@demo.test", "http://127.0.0.1:9")
    code, out = run(init(project), capsys)
    assert code == 6
    assert "Comprueba tu conexión" in out


def test_auth_error_and_missing_key_point_to_baas_init(home, server, capsys) -> None:
    from baas_plugin import handshake

    with pytest.raises(handshake.AuthError) as info:
        handshake.perform_handshake(server.url, dev_api_key("nadie@demo.test"))
    texts = [info.value.render(), envfile.missing_key_error(paths_()).render()]
    for text in texts:
        assert "/baas-init" in text
        for fragment in ("set-key", "~/", ".env", "API key", "baas.py"):
            assert fragment not in text, (fragment, text)


def test_wait_uses_the_servers_interval_but_never_less_than_three(home, project, fake_device, capsys) -> None:
    for interval, expected in ((7, 7), (1, 3)):
        seed_pending()
        pending = device.load_pending(paths_(), now=time.time())
        assert pending is not None
        device.save_pending(paths_(), dataclasses.replace(pending, interval=interval))
        fake_device.wait_calls.clear()
        assert run(init(project, "--wait", "--app-url", APP), capsys)[0] == 21
        assert fake_device.wait_calls == [(APP, DEVICE_CODE, expected, 90)]


def test_new_flow_remembers_the_servers_interval(home, project, fake_device, capsys) -> None:
    assert run(init(project, "--app-url", APP), capsys)[0] == 20
    pending = device.load_pending(paths_(), now=time.time())
    assert pending is not None
    assert pending.interval == 3  # el del servidor de pruebas


def test_wait_without_pending_but_with_a_key_stored_by_another_project_binds_without_a_second_approval(
    home, project, server, fake_claude, fake_device, capsys
) -> None:
    """Otro proyecto terminó su `--wait` y guardó la clave: este no abre una segunda aprobación."""
    key = save_key("analista@demo.test", server.url)
    code, out = run(init(project, "--wait"), capsys)
    assert code == 0, out
    assert fake_device.start_calls == []
    assert fake_device.wait_calls == []
    assert last_line(out) == "[BAAS_RESULT linked]"
    assert key not in out
    assert json.loads((project / ".claude/baas.local.json").read_text())["org"] == "demo"


def test_wait_without_pending_and_with_a_rejected_key_starts_the_flow(
    home, project, server, fake_device, capsys
) -> None:
    save_key("nadie@demo.test", server.url)
    code, _ = run(init(project, "--wait"), capsys)
    assert code == 20
    assert len(fake_device.start_calls) == 1


# --- init --new-account: conectar con otra cuenta -----------------------------------------------------------


def test_new_account_keeps_the_stored_key_until_the_new_approval_succeeds(
    home, project, server, fake_claude, fake_device, capsys
) -> None:
    key = save_key("analista@demo.test", server.url)
    assert last_line(run(init(project), capsys)[1]) == "[BAAS_RESULT linked]"  # ya conectado con esa cuenta
    fake_device.start_calls.clear()
    code, out = run(init(project, "--new-account"), capsys)
    assert code == 20, out
    assert "BAAS_APPROVAL_PENDING" in out
    assert key not in out
    assert_plain_message(out)
    assert envfile.read_env(paths_())[0]["BAAS_API_KEY"] == key  # el MCP y /baas-sync siguen funcionando
    assert paths_().pending_file.exists()
    assert len(fake_device.start_calls) == 1


def test_new_account_always_starts_a_new_flow_even_with_a_live_pending_one(
    home, project, server, fake_device, capsys
) -> None:
    save_key("analista@demo.test", server.url)
    seed_pending(app_url=server.url)  # una conexión a medias de antes
    before = device.load_pending(paths_(), now=time.time())
    assert run(init(project, "--new-account"), capsys)[0] == 20
    assert len(fake_device.start_calls) == 1  # no se reutiliza el enlace anterior
    assert run(init(project, "--new-account"), capsys)[0] == 20
    assert len(fake_device.start_calls) == 2  # tampoco el de la ejecución anterior
    assert device.load_pending(paths_(), now=time.time()) is not None
    assert before is not None


def test_new_account_then_wait_approved_binds_the_new_account(
    home, project, server, fake_claude, fake_device, capsys
) -> None:
    old = save_key("analista@demo.test", server.url)
    assert run(init(project), capsys)[0] == 0
    assert run(init(project, "--new-account"), capsys)[0] == 20
    new_key = dev_api_key("multi@demo.test")
    fake_device.wait_result = device.DevicePoll("approved", new_key, "multi@demo.test")
    code, out = run(init(project, "--wait", "--org", "demo"), capsys)
    assert code == 0, out
    assert last_line(out) == "[BAAS_RESULT linked]"
    assert envfile.read_env(paths_())[0]["BAAS_API_KEY"] == new_key
    settings = json.loads((project / ".claude/settings.local.json").read_text())["env"]
    assert f"X-Baas-Api-Key: {new_key}" in settings["ANTHROPIC_CUSTOM_HEADERS"]
    assert old not in json.dumps(settings)
    assert json.loads((project / ".claude/baas.local.json").read_text())["user_email"] == "multi@demo.test"
    assert new_key not in out
    assert old not in out


@pytest.mark.parametrize(("outcome", "code"), [("denied", 22), ("expired", 23)])
def test_abandoned_new_account_leaves_the_old_key_and_a_plain_init_is_unchanged(
    home, project, server, fake_claude, fake_device, capsys, outcome, code
) -> None:
    key = save_key("analista@demo.test", server.url)
    assert last_line(run(init(project), capsys)[1]) == "[BAAS_RESULT linked]"
    assert run(init(project, "--new-account"), capsys)[0] == 20
    fake_device.wait_result = device.DevicePoll(outcome)
    assert run(init(project, "--wait"), capsys)[0] == code
    assert envfile.read_env(paths_())[0]["BAAS_API_KEY"] == key
    assert not paths_().pending_file.exists()
    again_code, out = run(init(project), capsys)
    assert again_code == 0, out
    assert last_line(out) == "[BAAS_RESULT unchanged]"


def test_new_account_failing_to_reach_the_server_keeps_the_stored_key(
    home, project, server, fake_device, capsys
) -> None:
    key = save_key("analista@demo.test", server.url)
    fake_device.start_error = NetworkError("No he podido conectar con BaaS-AI-IP.")
    code, _ = run(init(project, "--new-account"), capsys)
    assert code == 6
    assert envfile.read_env(paths_())[0]["BAAS_API_KEY"] == key


def test_new_account_combines_with_app_url(home, project, fake_device, capsys) -> None:
    save_key("analista@demo.test", "https://otro.example.com")
    code, out = run(init(project, "--new-account", "--app-url", APP), capsys)
    assert code == 20, out
    assert fake_device.start_calls[0][0] == APP
    assert envfile.read_env(paths_())[0]["BAAS_APP_URL"] == APP


def test_new_account_without_a_stored_key_behaves_like_a_plain_init(home, project, fake_device, capsys) -> None:
    code, out = run(init(project, "--new-account", "--app-url", APP), capsys)
    assert code == 20, out
    assert LINK in out


def test_plain_init_with_a_valid_key_is_unchanged_by_the_new_flag(
    home, project, server, fake_claude, fake_device, capsys
) -> None:
    save_key("analista@demo.test", server.url)
    run(init(project), capsys)
    code, out = run(init(project), capsys)
    assert code == 0
    assert last_line(out) == "[BAAS_RESULT unchanged]"
    assert fake_device.start_calls == []
