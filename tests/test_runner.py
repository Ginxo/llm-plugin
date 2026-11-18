from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_baas import FakeBaasServer, dev_api_key

from baas_plugin import cli, envfile, package_fetch, runner
from baas_plugin.errors import ConfigError, McpToolError, ScriptIntegrityError

HELLO = "skills/informe/scripts/hola.py"
BAAS = Path(__file__).resolve().parents[1] / "scripts" / "baas.py"


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


def package(files: dict[str, str | bytes], executable: tuple[str, ...] = (), **meta) -> package_fetch.Package:
    """Un paquete local ya verificado, para probar `execute` sin red."""
    return package_fetch.Package.build("pkg", files, executable=executable, **meta)


# --- ejecución de punta a punta contra el MCP falso ----------------------------------------------


def test_runs_verified_script_and_deletes_it(server, analista_key, scratch, project, capfd, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", f"X-Baas-Api-Key: {analista_key}")
    code = runner.run_script(mcp(server), analista_key, HELLO, args=["a", "b"], cwd=project)
    out = capfd.readouterr().out
    assert code == 0
    assert "hola desde hola.py args=a,b" in out
    assert "cabeceras-en-entorno=False" in out  # la clave no llega al script
    assert list(scratch.iterdir()) == []
    calls = server.state.tool_calls()
    # H5: se descarga el paquete completo (manifiesto + ficheros), no solo el script
    assert [name for name, _ in calls] == ["baas_get_skill_package", "baas_get_skill_files"]
    assert calls[0][1] == {"skill": "informe"}


def test_expected_sha256_and_org_are_forwarded(server, analista_key, scratch, project, capfd) -> None:
    sha = server.state.scripts["demo"][HELLO].sha256
    assert (
        runner.run_script(mcp(server), analista_key, HELLO, expected_sha256=sha.upper(), org="demo", cwd=project) == 0
    )
    assert all(args["org"] == "demo" for _, args in server.state.tool_calls())


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("skills/informe/scripts/hola.py", ("informe", "scripts/hola.py")),
        ("skills/informe/scripts/sub/hola.py", ("informe", "scripts/sub/hola.py")),
        ("skills/informe/tools/hola.js", ("informe", "tools/hola.js")),
        ("informe/scripts/hola.py", ("informe", "scripts/hola.py")),
        ("informe/hola.py", ("informe", "scripts/hola.py")),  # forma de H1–H4: <skill>/<script>
    ],
)
def test_script_path_is_split_into_skill_and_relative_path(path, expected) -> None:
    assert runner.split_script_path(path) == expected


@pytest.mark.parametrize("path", ["hola.py", "../informe/hola.py", "", "skills/informe", "skills//x/y", "a/../b"])
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
        runner.run_script(mcp(server), analista_key, "skills/alterada/scripts/alterado.py", cwd=project)
    assert "no debería ejecutarse" not in capfd.readouterr().out
    assert list(scratch.iterdir()) == []


def test_expected_hash_mismatch_never_executes(server, analista_key, scratch, project, capfd) -> None:
    with pytest.raises(ScriptIntegrityError):
        runner.run_script(mcp(server), analista_key, HELLO, expected_sha256="a" * 64, cwd=project)
    assert "hola desde" not in capfd.readouterr().out
    assert list(scratch.iterdir()) == []
    assert server.state.tool_calls("baas_get_skill_files") == []  # falla antes de descargar nada más


def test_authorization_denied_is_reported(server, analista_key, scratch, project) -> None:
    with pytest.raises(McpToolError) as exc:
        runner.run_script(mcp(server), analista_key, "skills/revision/scripts/solo-revisor.py", cwd=project)
    assert exc.value.code == "AUTHORIZATION_DENIED"


def test_script_missing_from_package(server, analista_key, scratch, project) -> None:
    with pytest.raises(ConfigError, match="no está en el paquete"):
        runner.run_script(mcp(server), analista_key, "skills/informe/scripts/no-existe.py", cwd=project)


def test_whole_package_is_available_next_to_the_script(server, analista_key, scratch, project, capfd) -> None:
    server.state.add_skill(
        "demo",
        "facturacion",
        {
            "scripts/uno.py": (
                "import os, pathlib\n"
                "root = pathlib.Path(os.environ['CLAUDE_SKILL_DIR'])\n"
                "here = pathlib.Path(__file__).resolve().parent.parent\n"
                "print('mismo-dir=' + str(root.samefile(here)))\n"
                "print((root / 'templates' / 'plantilla.txt').read_text().strip())\n"
                "print('bin=' + str(len((root / 'assets' / 'datos.bin').read_bytes())))\n"
            ),
            "templates/plantilla.txt": "PLANTILLA\n",
            "assets/datos.bin": bytes(range(256)),
        },
        roles=["analista"],
    )
    code = runner.run_script(mcp(server), analista_key, "skills/facturacion/scripts/uno.py", cwd=project)
    out = capfd.readouterr().out
    assert code == 0
    assert "mismo-dir=True" in out
    assert "PLANTILLA" in out
    assert "bin=256" in out
    assert list(scratch.iterdir()) == []


# --- ejecución local (sin red) -------------------------------------------------------------------


def test_execution_error_still_deletes(scratch, project, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("intérprete roto")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(OSError, match="intérprete roto"):
        runner.execute(package({"scripts/x.py": "print('x')\n"}), "scripts/x.py", [], cwd=project)
    assert list(scratch.iterdir()) == []


def test_temp_dir_is_private(scratch, project, capfd) -> None:
    content = "import os\nd = os.path.dirname(os.path.abspath(__file__))\nprint(oct(os.stat(d).st_mode & 0o777))\n"
    assert runner.execute(package({"scripts/p.py": content}), "scripts/p.py", [], cwd=project) == 0
    assert "0o700" in capfd.readouterr().out
    pkg_root_mode = "import os\nprint(oct(os.stat(os.environ['CLAUDE_SKILL_DIR']).st_mode & 0o777))\n"
    assert runner.execute(package({"scripts/q.py": pkg_root_mode}), "scripts/q.py", [], cwd=project) == 0
    assert "0o700" in capfd.readouterr().out


def test_cwd_is_the_given_one_and_not_the_temp_dir(scratch, project, capfd) -> None:
    pkg = package({"scripts/c.py": "import os\nprint('cwd=' + os.getcwd())\n"})
    assert runner.execute(pkg, "scripts/c.py", [], cwd=project) == 0
    assert f"cwd={project.resolve()}" in capfd.readouterr().out


def test_missing_cwd_is_a_config_error(scratch, project) -> None:
    with pytest.raises(ConfigError, match="directorio de trabajo"):
        runner.execute(package({"scripts/c.py": "pass\n"}), "scripts/c.py", [], cwd=project / "no-existe")
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize("code", [0, 1, 9, 10, 11, 137])
def test_script_exit_code_is_returned_whole(scratch, project, code) -> None:
    pkg = package({"scripts/e.py": f"import sys\nsys.exit({code})\n"})
    assert runner.execute(pkg, "scripts/e.py", [], cwd=project) == code


def test_script_killed_by_a_signal_returns_128_plus_signal(scratch, project) -> None:
    pkg = package({"scripts/k.py": "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"})
    assert runner.execute(pkg, "scripts/k.py", [], cwd=project) == 128 + signal.SIGKILL


@pytest.mark.skipif(shutil.which("bash") is None, reason="sin bash")
def test_sh_runs_with_bash_even_without_exec_bit(scratch, project, capfd) -> None:
    pkg = package({"scripts/s.sh": 'echo "bash=$BASH_VERSION" | cut -c1-5\necho "args=$1,$2"\n'})
    assert runner.execute(pkg, "scripts/s.sh", ["a", "b c"], cwd=project) == 0
    out = capfd.readouterr().out
    assert "bash=" in out
    assert "args=a,b c" in out


@pytest.mark.skipif(shutil.which("node") is None, reason="sin node")
def test_js_runs_with_node(scratch, project, capfd) -> None:
    pkg = package({"scripts/j.js": "console.log('node ' + process.argv.slice(2).join('|'));\n"})
    assert runner.execute(pkg, "scripts/j.js", ["x", "y"], cwd=project) == 0
    assert "node x|y" in capfd.readouterr().out


def test_shebang_decides_when_the_extension_is_unknown(scratch, project, capfd) -> None:
    pkg = package({"scripts/herramienta": "#!/bin/sh\necho por-shebang $1\n"})
    assert runner.execute(pkg, "scripts/herramienta", ["ok"], cwd=project) == 0
    assert "por-shebang ok" in capfd.readouterr().out


def test_shebang_through_env_is_resolved(scratch, project, capfd) -> None:
    pkg = package({"scripts/herramienta.x": "#!/usr/bin/env sh\necho via-env\n"})
    assert runner.execute(pkg, "scripts/herramienta.x", [], cwd=project) == 0
    assert "via-env" in capfd.readouterr().out


def test_python_shebang_uses_the_plugin_python(scratch, project, capfd) -> None:
    content = "#!/usr/bin/env python3\nimport sys\nprint('py=' + sys.executable)\n"
    assert runner.execute(package({"scripts/p.py": content}), "scripts/p.py", [], cwd=project) == 0
    assert sys.executable in capfd.readouterr().out


def test_declared_executable_without_shebang_runs_directly(scratch, project, capfd) -> None:
    pkg = package({"scripts/nativo": "#!/bin/sh\necho directo\n"}, executable=("scripts/nativo",))
    assert runner.execute(pkg, "scripts/nativo", [], cwd=project) == 0
    assert "directo" in capfd.readouterr().out


def test_requires_single_interpreter_chooses_unknown_extension(scratch, project, capfd) -> None:
    pkg = package(
        {"scripts/t.zz": "echo desde-requires\n"},
        metadata={"requires": {"interpreters": ["sh"]}},
    )
    assert runner.execute(pkg, "scripts/t.zz", [], cwd=project) == 0
    assert "desde-requires" in capfd.readouterr().out


def test_unknown_type_without_hint_is_unsupported(scratch, project) -> None:
    with pytest.raises(ConfigError, match="No se sabe cómo ejecutar"):
        runner.execute(package({"scripts/x.zz": "MZ"}), "scripts/x.zz", [], cwd=project)
    assert list(scratch.iterdir()) == []


def test_missing_interpreter_is_a_config_error(scratch, project) -> None:
    pkg = package({"scripts/x": "#!/usr/bin/env interprete-que-no-existe-xyz\n"})
    with pytest.raises(ConfigError, match="interprete-que-no-existe-xyz"):
        runner.execute(pkg, "scripts/x", [], cwd=project)


def test_requires_are_only_a_warning(scratch, project, capfd) -> None:
    pkg = package(
        {"scripts/r.py": "print('corre igual')\n"},
        metadata={"requires": {"binaries": ["binario-que-no-existe-xyz"], "interpreters": ["python3>=3.10"]}},
    )
    assert runner.execute(pkg, "scripts/r.py", [], cwd=project) == 0
    cap = capfd.readouterr()
    assert "corre igual" in cap.out
    assert "binario-que-no-existe-xyz" in cap.err
    assert "AVISO" in cap.err


def test_script_environment_has_no_secrets(scratch, project, capfd, monkeypatch) -> None:
    monkeypatch.setenv("BAAS_API_KEY", "baas_secreta")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Baas-Api-Key: baas_secreta")
    pkg = package(
        {"scripts/env.py": "import os\nprint(sorted(k for k in os.environ if 'BAAS' in k or 'CUSTOM' in k))\n"}
    )
    assert runner.execute(pkg, "scripts/env.py", [], cwd=project) == 0
    assert "['BAAS_SCRIPT']" in capfd.readouterr().out


def test_temp_tree_is_removed_on_sigterm(tmp_path: Path, server, analista_key, home, project) -> None:
    """SIGTERM al ejecutor: mata el script y borra el directorio temporal."""
    server.state.add_skill(
        "demo",
        "lenta",
        {"scripts/duerme.py": "import time\nprint('listo', flush=True)\ntime.sleep(60)\n"},
        roles=["analista"],
    )
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": analista_key, "BAAS_APP_URL": server.url})
    (project / ".claude").mkdir()  # el enlace se escribe a mano (sin `claude`)
    (project / ".claude/baas.local.json").write_text(json.dumps({"org": "demo", "mcp_url": mcp(server)}))
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    env = {**os.environ, "TMPDIR": str(tmpdir), "HOME": str(home)}
    proc = subprocess.Popen(
        [sys.executable, str(BAAS), "run-script", "skills/lenta/scripts/duerme.py", "--project", str(project)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "listo"
        assert any(p.name.startswith("baas-script-") for p in tmpdir.iterdir())
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    time.sleep(0.1)
    assert proc.returncode == 128 + signal.SIGTERM
    assert list(tmpdir.iterdir()) == []


# --- CLI -----------------------------------------------------------------------------------------


def bind(home: Path, project: Path, server: FakeBaasServer, email: str = "analista@demo.test") -> str:
    key = dev_api_key(email)
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": key, "BAAS_APP_URL": server.url})
    (project / ".claude").mkdir(exist_ok=True)
    (project / ".claude/baas.local.json").write_text(json.dumps({"org": "demo", "mcp_url": mcp(server)}))
    return key


def test_cli_run_script_uses_project_binding(home, project, server, scratch, capfd) -> None:
    key = bind(home, project, server)
    code = cli.main(["run-script", HELLO, "--project", str(project), "--", "--flag", "x"])
    out, err = capfd.readouterr()
    assert code == 0, err
    assert "args=--flag,x" in out
    assert key not in out + err
    assert "BAAS_ERROR=" not in err


def test_cli_run_script_cwd_option(home, project, server, scratch, capfd, tmp_path) -> None:
    bind(home, project, server)
    server.state.add_skill(
        "demo", "donde", {"scripts/pwd.py": "import os\nprint('cwd=' + os.getcwd())\n"}, roles=["analista"]
    )
    elsewhere = tmp_path / "otro-sitio"
    elsewhere.mkdir()
    code = cli.main(["run-script", "skills/donde/scripts/pwd.py", "--project", str(project), "--cwd", str(elsewhere)])
    assert code == 0
    assert f"cwd={elsewhere.resolve()}" in capfd.readouterr().out


def test_cli_run_script_requires_binding(home, project, scratch, capfd) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("analista@demo.test")})
    code = cli.main(["run-script", HELLO, "--project", str(project)])
    err = capfd.readouterr().err
    assert code == 2
    assert "/baas-init" in err
    assert "BAAS_ERROR=CONFIG_ERROR" in err


@pytest.mark.parametrize(
    ("path", "exit_code", "error"),
    [
        ("skills/alterada/scripts/alterado.py", 9, "SCRIPT_HASH_MISMATCH"),
        ("skills/revision/scripts/solo-revisor.py", 10, "AUTHORIZATION_DENIED"),
        ("skills/informe/scripts/no-existe.py", 2, "CONFIG_ERROR"),
    ],
)
def test_plugin_errors_carry_a_baas_error_line(home, project, server, scratch, capfd, path, exit_code, error) -> None:
    """Las líneas `BAAS_ERROR=` distinguen un error del plugin del código de salida del propio script."""
    bind(home, project, server)
    code = cli.main(["run-script", path, "--project", str(project)])
    err = capfd.readouterr().err.splitlines()
    assert code == exit_code
    assert f"BAAS_ERROR={error}" in err


def test_kill_switch_is_a_plugin_error_with_its_own_code(home, project, server, scratch, capfd) -> None:
    bind(home, project, server)
    server.state.orgs["demo"].paused = True
    code = cli.main(["run-script", HELLO, "--project", str(project)])
    err = capfd.readouterr().err
    assert code == 10
    assert "BAAS_ERROR=KILL_SWITCH_ACTIVE" in err.splitlines()


def test_script_exit_codes_9_and_10_have_no_baas_error_line(home, project, server, scratch, capfd) -> None:
    """Un script que sale con 9 o 10 no se confunde con un error del plugin."""
    bind(home, project, server)
    server.state.add_skill(
        "demo",
        "codigos",
        {"scripts/nueve.py": "import sys\nsys.exit(9)\n", "scripts/diez.py": "import sys\nsys.exit(10)\n"},
        roles=["analista"],
    )
    for script, expected in (("nueve", 9), ("diez", 10)):
        code = cli.main(["run-script", f"skills/codigos/scripts/{script}.py", "--project", str(project)])
        assert code == expected
        assert "BAAS_ERROR" not in capfd.readouterr().err


def test_sha256_option_checks_the_script_file(home, project, server, scratch, capfd) -> None:
    bind(home, project, server)
    good = server.state.scripts["demo"][HELLO].sha256
    assert cli.main(["run-script", HELLO, "--project", str(project), "--sha256", good]) == 0
    capfd.readouterr()
    assert cli.main(["run-script", HELLO, "--project", str(project), "--sha256", "b" * 64]) == 9
    assert "BAAS_ERROR=SCRIPT_HASH_MISMATCH" in capfd.readouterr().err


def test_hash_of_a_known_script_matches(server) -> None:
    sha = server.state.scripts["demo"][HELLO].sha256
    pkg = server.state.skills["demo"]["informe"].files["scripts/hola.py"]
    assert pkg.sha256 == sha == hashlib.sha256(pkg.content).hexdigest()
