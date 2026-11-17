from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_baas import FakeBaasServer

from baas_plugin import cli, envfile, runner
from baas_plugin.errors import ConfigError, McpToolError, ScriptIntegrityError

HELLO = "skills/informe/scripts/hola.py"


@pytest.fixture
def server() -> Iterator[FakeBaasServer]:
    with FakeBaasServer() as srv:
        yield srv


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Directorio temporal del sistema aislado para comprobar que no queda nada."""
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    return tmp


def mcp(server: FakeBaasServer) -> str:
    return f"{server.url}/org/demo/mcp"


def test_runs_verified_script_and_deletes_it(server, analista_key, scratch, project, capfd, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", f"X-Baas-Api-Key: {analista_key}")
    code = runner.run_script(mcp(server), analista_key, HELLO, args=["a", "b"], cwd=project)
    out = capfd.readouterr().out
    assert code == 0
    assert "hola desde hola.py args=a,b" in out
    assert "cabeceras-en-entorno=False" in out  # la clave no llega al script
    assert list(scratch.iterdir()) == []
    call = json.loads(server.state.requests[2]["body"])
    # Firma real del MCP: baas_get_script(skill, script, org?)
    assert call["params"] == {"name": "baas_get_script", "arguments": {"skill": "informe", "script": "hola.py"}}


def test_expected_sha256_and_org_are_forwarded(server, analista_key, scratch, project, capfd) -> None:
    sha = server.state.scripts["demo"][HELLO].sha256
    assert (
        runner.run_script(mcp(server), analista_key, HELLO, expected_sha256=sha.upper(), org="demo", cwd=project) == 0
    )
    call = json.loads(server.state.requests[2]["body"])
    assert call["params"]["arguments"] == {"skill": "informe", "script": "hola.py", "org": "demo"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("skills/informe/scripts/hola.py", ("informe", "hola.py")),
        ("informe/hola.py", ("informe", "hola.py")),
    ],
)
def test_script_path_is_split_into_skill_and_script(path, expected) -> None:
    assert runner.split_script_path(path) == expected


@pytest.mark.parametrize(
    "path",
    ["hola.py", "skills/informe/hola.py", "skills/informe/scripts/sub/hola.py", "../informe/hola.py", ""],
)
def test_invalid_script_path_is_rejected_before_calling_the_mcp(path) -> None:
    with pytest.raises(ConfigError):
        runner.split_script_path(path)


def test_failing_script_returns_its_code_and_is_deleted(server, analista_key, scratch, project, capfd) -> None:
    code = runner.run_script(mcp(server), analista_key, "skills/informe/scripts/falla.py", cwd=project)
    assert code == 3
    assert "voy a fallar" in capfd.readouterr().out
    assert list(scratch.iterdir()) == []


def test_hash_mismatch_never_executes(server, analista_key, scratch, project, capfd) -> None:
    with pytest.raises(ScriptIntegrityError):
        runner.run_script(mcp(server), analista_key, "skills/informe/scripts/alterado.py", cwd=project)
    assert "no debería ejecutarse" not in capfd.readouterr().out
    assert list(scratch.iterdir()) == []


def test_expected_hash_mismatch_never_executes(server, analista_key, scratch, project, capfd) -> None:
    with pytest.raises(ScriptIntegrityError):
        runner.run_script(mcp(server), analista_key, HELLO, expected_sha256="a" * 64, cwd=project)
    assert "hola desde" not in capfd.readouterr().out
    assert list(scratch.iterdir()) == []


def test_execution_error_still_deletes(scratch, project, monkeypatch) -> None:
    content = "print('x')\n"
    script = runner.Script("x.py", content, hashlib.sha256(content.encode()).hexdigest())

    def boom(*a, **k):
        raise OSError("intérprete roto")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(OSError, match="intérprete roto"):
        runner.execute(script, [], cwd=project)
    assert list(scratch.iterdir()) == []


def test_temp_dir_is_private(scratch, project, capfd) -> None:
    content = (
        "import os, stat\nd = os.path.dirname(os.path.abspath(__file__))\nprint(oct(os.stat(d).st_mode & 0o777))\n"
    )
    script = runner.Script("p.py", content, hashlib.sha256(content.encode()).hexdigest())
    assert runner.execute(script, [], cwd=project) == 0
    assert "0o700" in capfd.readouterr().out


def test_unsupported_script_type(scratch, project) -> None:
    script = runner.Script("x.exe", "MZ", hashlib.sha256(b"MZ").hexdigest())
    with pytest.raises(ConfigError):
        runner.execute(script, [], cwd=project)


def test_authorization_denied_is_reported(server, analista_key, scratch, project) -> None:
    with pytest.raises(McpToolError) as exc:
        runner.run_script(mcp(server), analista_key, "skills/revision/scripts/solo-revisor.py", cwd=project)
    assert exc.value.code == "AUTHORIZATION_DENIED"


def test_cli_run_script_uses_project_binding(home, project, server, scratch, capfd, monkeypatch) -> None:
    key = runner_key = None  # noqa: F841
    from fake_baas import dev_api_key

    key = dev_api_key("analista@demo.test")
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": key, "BAAS_APP_URL": server.url})
    monkeypatch.setenv("BAAS_CLAUDE_BIN", str(project / "sin-claude"))
    assert cli.main(["init", "--project", str(project)]) == 0
    capfd.readouterr()
    code = cli.main(["run-script", HELLO, "--project", str(project), "--", "--flag", "x"])
    out, err = capfd.readouterr()
    assert code == 0, err
    assert "args=--flag,x" in out
    assert key not in out + err


def test_cli_run_script_requires_binding(home, project, scratch, capfd) -> None:
    from fake_baas import dev_api_key

    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("analista@demo.test")})
    code = cli.main(["run-script", HELLO, "--project", str(project)])
    assert code == 2
    assert "/baas-init" in capfd.readouterr().err
