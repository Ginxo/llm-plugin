"""`baas.py run-script --tool antigravity --synced-org <org>` (T20 · #194): el ejecutor para las skills nativas.

Las skills nativas de Antigravity no tienen una vinculación de Claude Code en el proyecto: el MCP de la
organización sale del fichero de control que escribe `sync --tool antigravity` (nunca de un argumento) y solo vale
para las skills sincronizadas de esa organización. Todo contra el BaaS falso, con `HOME` temporal.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fake_baas import FakeBaasServer, PkgFile, dev_api_key

from baas_plugin import cli, envfile

SCRIPT = """\
import json, os, sys
out = {"args": sys.argv[1:], "cwd": os.getcwd(), "org": "{ORG}",
       "env": sorted(k for k in os.environ if "BAAS_API_KEY" in k or "CUSTOM_HEADERS" in k),
       "baas_script": os.environ.get("BAAS_SCRIPT"), "skill_dir": os.environ.get("CLAUDE_SKILL_DIR")}
with open(os.environ.get("OUT_FILE", "salida.json"), "w") as fh:
    json.dump(out, fh)
sys.exit(int(os.environ.get("EXIT", "0")))
"""
OWN_PATH = "skills/gestor/scripts/hola.py"


@dataclass
class Env:
    server: FakeBaasServer
    home: Path
    project: Path
    key: str
    capsys: pytest.CaptureFixture[str]

    @property
    def root(self) -> Path:
        return self.home / ".gemini" / "config" / "skills"

    def publish(self, org: str = "demo", name: str = "gestor", **files: str | bytes):
        content = {
            "SKILL.md": f"---\nname: {name}\ndescription: x\n---\n# {name}\n",
            "scripts/hola.py": SCRIPT.replace("{ORG}", org),
            **files,
        }
        return self.server.state.add_skill(org, name, content, roles=["analista"])

    def sync(self, org: str = "demo") -> None:
        code = cli.main(["sync", "--tool", "antigravity", "--project", str(self.project), "--org", org])
        assert code == 0, self.capsys.readouterr()
        self.capsys.readouterr()

    def run(self, *args: str, path: str = OWN_PATH, org: str = "demo") -> tuple[int, str]:
        argv = ["run-script", "--tool", "antigravity", "--synced-org", org, "--cwd", str(self.project), path]
        code = cli.main([*argv, "--", *args] if args else argv)
        out = self.capsys.readouterr()
        return code, out.out + out.err

    def output(self) -> dict:
        return json.loads((self.project / "salida.json").read_text())


@pytest.fixture
def env(home, project, capsys, monkeypatch) -> Iterator[Env]:
    with FakeBaasServer() as server:
        for skills in server.state.skills.values():
            skills.clear()
        key = dev_api_key("analista@demo.test")
        envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": key, "BAAS_APP_URL": server.url})
        monkeypatch.setenv("OUT_FILE", str(project / "salida.json"))
        yield Env(server, home, project, key, capsys)


def test_it_runs_a_script_of_a_synced_skill_without_a_claude_binding(env: Env, tmp_path, monkeypatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    env.publish()
    env.sync()
    assert not (env.project / ".claude").exists(), "no hay vinculación de Claude Code en el proyecto"
    monkeypatch.setenv("BAAS_API_KEY", env.key)
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", f"X-Baas-Api-Key: {env.key}")
    code, out = env.run("list", "--name", "Ana María", "--year", "2026")
    assert code == 0, out
    result = env.output()
    assert result["args"] == ["list", "--name", "Ana María", "--year", "2026"], "los argumentos llegan intactos"
    assert Path(result["cwd"]).resolve() == env.project.resolve(), "el script corre en el directorio de trabajo"
    assert result["env"] == [], "la clave no llega al entorno del script"
    assert result["baas_script"] == "1"
    assert list(scratch.iterdir()) == [], "el temporal se borra siempre"
    assert env.key not in out


def test_the_script_is_never_copied_to_the_skill_folder(env: Env) -> None:
    env.publish()
    env.sync()
    assert env.run()[0] == 0
    assert sorted(p.name for p in (env.root / "demo-gestor").iterdir()) == ["SKILL.md"]


def test_the_exit_code_of_the_script_is_returned_unchanged(env: Env, monkeypatch) -> None:
    env.publish()
    env.sync()
    monkeypatch.setenv("EXIT", "7")
    code, _ = env.run()
    assert code == 7


def test_the_mcp_is_the_one_of_the_synced_organization_even_if_the_project_is_bound_to_another(env: Env) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("multi@demo.test")})
    env.publish("demo")
    env.publish("otra")
    env.sync("demo")
    env.sync("otra")
    (env.project / ".claude").mkdir()
    binding = {"org": "otra", "mcp_url": f"{env.server.url}/org/otra/mcp"}
    (env.project / ".claude/baas.local.json").write_text(json.dumps(binding))
    assert env.run(org="demo")[0] == 0
    assert env.output()["org"] == "demo"
    assert env.run(org="otra")[0] == 0
    assert env.output()["org"] == "otra"


def test_a_skill_that_is_not_synced_for_that_organization_is_refused(env: Env) -> None:
    env.publish()
    env.publish("secreta")
    env.sync()
    (env.root / ".baas-skills-demo.json").write_text(
        json.dumps({**json.loads((env.root / ".baas-skills-demo.json").read_text()), "skills": {}})
    )
    code, out = env.run()
    assert code == 2, out
    assert "no figura entre las skills sincronizadas" in out
    assert not (env.project / "salida.json").exists()


def test_without_any_sync_it_says_so(env: Env) -> None:
    env.publish()
    code, out = env.run()
    assert code == 2, out
    assert "sync --tool antigravity" in out


def test_the_other_organizations_skill_is_not_reachable_through_this_organization(env: Env) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("multi@demo.test")})
    env.publish("otra")  # solo existe en `otra`
    env.sync("otra")
    code, out = env.run(org="demo")  # `gestor` figura en el control de `otra`, no en el de `demo`
    assert code == 2, out
    assert not (env.project / "salida.json").exists()


def test_a_wrong_hash_is_not_executed(env: Env) -> None:
    pkg = env.publish()
    env.sync()
    pkg.files["scripts/hola.py"] = PkgFile(b"print('malicioso')\n", sha256_override="0" * 64)
    code, out = env.run()
    assert code == 9, out
    assert "SHA-256" in out
    assert not (env.project / "salida.json").exists()


@pytest.mark.parametrize(
    "path",
    ["skills/gestor/scripts/../../x.py", "skills/gestor/../otra/scripts/hola.py", "skills//gestor/scripts/hola.py"],
)
def test_a_path_outside_the_skill_is_refused(env: Env, path: str) -> None:
    env.publish()
    env.sync()
    code, out = env.run(path=path)
    assert code == 2, out
    assert not (env.project / "salida.json").exists()


def test_a_script_that_is_not_in_the_package_is_refused(env: Env) -> None:
    env.publish()
    env.sync()
    code, out = env.run(path="skills/gestor/scripts/no-existe.py")
    assert code == 2, out
    assert "no está en el paquete" in out


def test_tool_and_synced_org_go_together_and_exclude_org(env: Env, project: Path) -> None:
    env.publish()
    env.sync()
    for argv in (
        ["run-script", "--tool", "antigravity", OWN_PATH],
        ["run-script", "--synced-org", "demo", OWN_PATH],
        ["run-script", "--tool", "antigravity", "--synced-org", "demo", "--org", "otra", OWN_PATH],
        ["run-script", "--tool", "antigravity", "--synced-org", "../x", OWN_PATH],
        ["run-script", "--tool", "antigravity", "--synced-org", "Demo", OWN_PATH],
    ):
        code = cli.main(argv)
        out = env.capsys.readouterr()
        assert code == 2, (argv, out)
        if "--org" not in argv and ("--tool" not in argv or "--synced-org" not in argv):
            assert "van juntas" in out.err, "debe decir que las dos opciones van juntas"


def test_an_unknown_tool_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        cli.main(["run-script", "--tool", "no-existe", "--synced-org", "demo", OWN_PATH])


def test_an_insecure_server_in_the_control_file_never_receives_the_key(env: Env) -> None:
    env.publish()
    env.sync()
    control = env.root / ".baas-skills-demo.json"
    data = json.loads(control.read_text())
    data["server"] = "http://servidor-ajeno.example.com/org/demo/mcp"
    control.write_text(json.dumps(data))
    env.server.state.requests.clear()
    code, out = env.run()
    assert code == 2, out
    assert env.server.state.requests == []
    assert env.key not in out


def test_a_corrupt_control_file_is_refused(env: Env) -> None:
    env.publish()
    env.sync()
    (env.root / ".baas-skills-demo.json").write_text("{no es json")
    code, out = env.run()
    assert code == 13, out
