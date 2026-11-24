"""`sync --tool antigravity --verify` y la sugerencia de permisos de `agy` (T22 · #196).

`--verify` recomprueba los scripts (y todo lo copiado) contra el SHA-256 del fichero de control, sin red ni clave y sin
modificar nada. La sugerencia de permisos se imprime, nunca se escribe en `~/.gemini/antigravity-cli/settings.json`.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fake_baas import FakeBaasServer, dev_api_key

from baas_plugin import cli, envfile

posix_only = pytest.mark.skipif(os.name != "posix", reason="los permisos POSIX solo se comprueban en POSIX")
SCRIPT = "print('v1')\n"
MD = (
    "---\nname: {n}\ndescription: x\n---\n\n```bash\n"
    'python3 "${{CLAUDE_SKILL_DIR}}/scripts/hola.py" list\npython3 "${{CLAUDE_SKILL_DIR}}/scripts/hola.py" audit\n'
    '"${{CLAUDE_SKILL_DIR}}/scripts/run.sh" init\n```\n'
)


@dataclass
class Env:
    server: FakeBaasServer
    home: Path
    project: Path
    capsys: pytest.CaptureFixture[str]

    @property
    def root(self) -> Path:
        return self.home / ".gemini" / "config" / "skills"

    def publish(self, name: str = "gestor", org: str = "demo", **files: str | bytes):
        files.setdefault("scripts/hola.py", SCRIPT)
        files.setdefault("scripts/run.sh", "#!/bin/sh\necho hola\n")
        return self.server.state.add_skill(
            org,
            name,
            {"SKILL.md": MD.format(n=name), **files},
            roles=["analista"],
            executable=["scripts/run.sh"],
        )

    def main(self, *args: str) -> tuple[int, str]:
        code = cli.main(["sync", "--tool", "antigravity", "--project", str(self.project), *args])
        out = self.capsys.readouterr()
        return code, out.out + out.err

    def tree(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}


@pytest.fixture
def env(home, project, capsys) -> Iterator[Env]:
    with FakeBaasServer() as server:
        for skills in server.state.skills.values():
            skills.clear()
        key = dev_api_key("analista@demo.test")
        envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": key, "BAAS_APP_URL": server.url})
        yield Env(server, home, project, capsys)


# --- --verify ---------------------------------------------------------------------------------------------


def test_verify_a_clean_install_passes_without_network_or_key(env: Env) -> None:
    env.publish()
    env.publish("facturacion")
    assert env.main()[0] == 0
    envfile.remove_env_keys(envfile.BaasPaths.default(), ["BAAS_API_KEY", "BAAS_APP_URL"])
    env.server.state.requests.clear()
    code, out = env.main("--verify")
    assert code == 0, out
    assert "✓ demo-gestor (3 ficheros)" in out
    assert "✓ demo-facturacion" in out
    assert "✗" not in out
    assert env.server.state.requests == [], "la verificación es local"


def test_verify_does_not_modify_anything(env: Env) -> None:
    env.publish()
    assert env.main()[0] == 0
    (env.root / "demo-gestor/scripts/hola.py").write_text("print('cambiado')\n")
    before = env.tree()
    stamps = {p: p.stat().st_mtime_ns for p in env.root.rglob("*") if p.is_file()}
    code, _ = env.main("--verify")
    assert code == 9
    assert env.tree() == before
    assert {p: p.stat().st_mtime_ns for p in env.root.rglob("*") if p.is_file()} == stamps


def test_verify_reports_an_edited_script_with_exit_code_9(env: Env) -> None:
    env.publish()
    assert env.main()[0] == 0
    (env.root / "demo-gestor/scripts/hola.py").write_text("print('malicioso')\n")
    code, out = env.main("--verify")
    assert code == 9, out
    assert "✗ demo-gestor: has editado scripts/hola.py" in out
    assert "BAAS_SCRIPT_HASH_MISMATCH" in out
    assert "--force" in out


def test_verify_reports_a_missing_file_an_added_file_a_missing_folder_and_a_symlink(env: Env, tmp_path) -> None:
    env.publish("uno")
    env.publish("dos")
    env.publish("tres")
    env.publish("cuatro")
    assert env.main()[0] == 0
    (env.root / "demo-uno/scripts/hola.py").unlink()
    (env.root / "demo-dos/scripts/extra.sh").write_text("echo hola\n")
    import shutil

    shutil.rmtree(env.root / "demo-tres")
    if os.name == "posix":
        (env.root / "demo-cuatro/enlace").symlink_to(tmp_path)
    code, out = env.main("--verify")
    assert code == 9, out
    assert "demo-uno: falta scripts/hola.py" in out
    assert "demo-dos: has añadido scripts/extra.sh" in out
    assert "demo-tres: falta la carpeta" in out
    if os.name == "posix":
        assert "demo-cuatro: contiene un enlace simbólico (enlace)" in out
    assert f"{4 if os.name == 'posix' else 3} skill(s) no coinciden" in out


@posix_only
def test_verify_checks_the_executable_bit_and_the_write_permissions(env: Env) -> None:
    env.publish("uno")
    env.publish("dos")
    env.publish("tres")
    assert env.main()[0] == 0
    (env.root / "demo-uno/scripts/hola.py").chmod(0o700)  # un script que no era ejecutable pasa a serlo
    (env.root / "demo-dos/scripts/run.sh").chmod(0o600)  # y uno que lo era, deja de serlo
    (env.root / "demo-tres/scripts/hola.py").chmod(0o666)  # escribible por todos
    code, out = env.main("--verify")
    assert code == 9, out
    assert "scripts/hola.py es ejecutable sin serlo en el manifiesto" in out
    assert "scripts/run.sh ya no es ejecutable en el manifiesto" in out
    assert "scripts/hola.py es escribible por el grupo o por otros" in out
    # nada de esto es una «edición» para el sync: restaurar los permisos deja la verificación limpia
    (env.root / "demo-uno/scripts/hola.py").chmod(0o600)
    (env.root / "demo-dos/scripts/run.sh").chmod(0o700)
    (env.root / "demo-tres/scripts/hola.py").chmod(0o600)
    assert env.main("--verify")[0] == 0


def test_verify_with_org_checks_only_that_organization_and_without_it_all_of_them(env: Env) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("multi@demo.test")})
    env.publish("a", org="demo")
    env.publish("b", org="otra")
    assert env.main("--org", "demo")[0] == 0
    assert env.main("--org", "otra")[0] == 0
    (env.root / "otra-b/scripts/hola.py").write_text("print('x')\n")
    code, out = env.main("--verify", "--org", "demo")
    assert code == 0, out
    assert "demo-a" in out
    assert "otra-b" not in out
    code, out = env.main("--verify")
    assert code == 9, out
    assert "✓ demo-a" in out
    assert "✗ otra-b" in out


def test_verify_without_anything_synced_says_so_and_succeeds(env: Env) -> None:
    code, out = env.main("--verify")
    assert code == 0, out
    assert "nada que verificar" in out


def test_verify_refuses_a_corrupt_control_file(env: Env) -> None:
    env.publish()
    assert env.main()[0] == 0
    (env.root / ".baas-skills-demo.json").write_text("{no es json")
    code, out = env.main("--verify")
    assert code == 13, out


def test_verify_fails_if_the_skills_folder_content_was_replaced_by_a_foreign_file(env: Env) -> None:
    env.publish()
    assert env.main()[0] == 0
    skill = env.root / "demo-gestor"
    (skill / "SKILL.md").write_text("otra cosa\n")
    code, out = env.main("--verify")
    assert code == 9, out
    assert "has editado SKILL.md" in out


# --- sugerencia de permisos de `agy` -------------------------------------------------------------------------


def suggestion_rules(out: str) -> list[str]:
    block = re.search(r"^  (\{\n(?:  .*\n)+?  \})$", out, flags=re.M)
    assert block, out
    text = "\n".join(line.removeprefix("  ") for line in block.group(1).splitlines())
    return json.loads(text)["permissions"]["allow"]


def test_sync_prints_the_permission_rule_for_the_scripts_it_installed_and_writes_nothing(env: Env) -> None:
    env.publish()
    settings = env.home / ".gemini" / "antigravity-cli" / "settings.json"
    code, out = env.main()
    assert code == 0, out
    folder = env.root / "demo-gestor"
    assert suggestion_rules(out) == [
        f"command(python3 {folder}/scripts/hola.py)",
        f"command({folder}/scripts/run.sh)",
    ], "una regla por llamada distinta, sin repetir; la directa sin intérprete"
    assert "NO he escrito nada en ~/.gemini/antigravity-cli/settings.json" in out
    assert "sandbox" in out
    assert "NO comprobada con `agy` real" in out
    assert not settings.exists(), "son permisos de la persona: nunca se escriben"


def test_an_existing_settings_file_is_never_touched(env: Env) -> None:
    env.publish()
    settings = env.home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions": {"deny": ["command(sudo *)"]}}\n')
    before = (settings.read_bytes(), settings.stat().st_mtime_ns)
    assert env.main()[0] == 0
    assert env.main("--permissions")[0] == 0
    assert (settings.read_bytes(), settings.stat().st_mtime_ns) == before


def test_an_unchanged_sync_does_not_repeat_the_suggestion_but_permissions_prints_it_without_network(env: Env) -> None:
    env.publish()
    assert env.main()[0] == 0
    code, out = env.main()
    assert code == 0, out
    assert "Permisos de `agy`" not in out
    envfile.remove_env_keys(envfile.BaasPaths.default(), ["BAAS_API_KEY", "BAAS_APP_URL"])
    env.server.state.requests.clear()
    code, out = env.main("--permissions")
    assert code == 0, out
    assert len(suggestion_rules(out)) == 2
    assert env.server.state.requests == []


def test_no_suggestion_when_no_skill_calls_its_scripts_or_in_dry_run_or_quiet(env: Env) -> None:
    env.server.state.add_skill(
        "demo", "texto", {"SKILL.md": "---\nname: texto\ndescription: x\n---\nhola\n"}, roles=["analista"]
    )
    code, out = env.main()
    assert code == 0, out
    assert "Permisos de `agy`" not in out
    assert "nada que permitir" in env.main("--permissions")[1]
    env.publish()
    code, out = env.main("--dry-run")
    assert "Permisos de `agy`" not in out
    code, out = env.main("--quiet")
    assert out == ""


def test_the_suggestion_warns_when_the_path_has_spaces(tmp_path, project, capsys, monkeypatch) -> None:
    home = tmp_path / "mi casa"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BAAS_CLAUDE_HOME", str(home / ".claude"))
    with FakeBaasServer() as server:
        for skills in server.state.skills.values():
            skills.clear()
        envfile.update_env(
            envfile.BaasPaths.default(),
            {"BAAS_API_KEY": dev_api_key("analista@demo.test"), "BAAS_APP_URL": server.url},
        )
        env = Env(server, home, project, capsys)
        env.publish()
        code, out = env.main()
        assert code == 0, out
        assert "la ruta tiene espacios" in out


def test_each_organization_gets_only_its_own_rules(env: Env) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("multi@demo.test")})
    env.publish("a", org="demo")
    env.publish("b", org="otra")
    assert env.main("--org", "demo")[0] == 0
    code, out = env.main("--org", "otra")
    assert code == 0, out
    rules = suggestion_rules(out)
    assert all("otra-b" in r for r in rules)
    assert stat.S_IMODE((env.root / ".baas-skills-otra.json").stat().st_mode) in (0o600, 0o666 & ~0o077)


def test_a_dry_run_with_changes_pending_never_prints_the_suggestion(env: Env) -> None:
    env.publish("a")
    assert env.main()[0] == 0
    env.publish("b")  # algo nuevo que instalar, con un control ya existente
    code, out = env.main("--dry-run")
    assert code == 0, out
    assert "+ demo-b" in out
    assert "Permisos de `agy`" not in out


def test_permissions_with_org_prints_only_that_organization(env: Env) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("multi@demo.test")})
    env.publish("a", org="demo")
    env.publish("b", org="otra")
    assert env.main("--org", "demo")[0] == 0
    assert env.main("--org", "otra")[0] == 0
    code, out = env.main("--permissions", "--org", "demo")
    assert code == 0, out
    assert all("demo-a" in r for r in suggestion_rules(out))
    assert "otra-b" not in out
    code, out = env.main("--permissions")
    assert "demo-a" in out
    assert "otra-b" in out
