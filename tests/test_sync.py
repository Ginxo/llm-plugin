"""Sincronización de las skills de la organización (H5, T7): `baas.py sync` y el plugin `~/.claude/skills/<slug>/`."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fake_baas import FakeBaasServer, PkgFile, dev_api_key

from baas_plugin import cli, envfile, transfer

BINARY = bytes(range(256)) * 4  # no es UTF-8 válido: viaja en base64
KEY = "analista@demo.test"
SKILLS_KEY = "demo@skills-dir"


@dataclass
class Env:
    server: FakeBaasServer
    project: Path
    claude: Path  # ~/.claude de pruebas
    capsys: pytest.CaptureFixture[str]

    @property
    def plugin(self) -> Path:
        return self.claude / "skills" / "demo"

    @property
    def skills(self) -> Path:
        return self.plugin / "skills"

    def run(self, *args: str, project: Path | None = None) -> tuple[int, str]:
        code = cli.main(["sync", "--project", str(project or self.project), *args])
        out = self.capsys.readouterr()
        return code, out.out + out.err

    def publish(self, name: str = "facturacion", **kwargs):
        files = kwargs.pop(
            "files",
            {
                "SKILL.md": f"---\nname: {name}\ndescription: x\n---\n# {name}\n",
                "scripts/run.sh": "#!/bin/sh\necho hola\n",
                "scripts/lib/util.py": "X = 1\n",
                "templates/informe.docx": BINARY,
                ".env.example": "A=1\n",
            },
        )
        kwargs.setdefault("roles", ["analista"])
        kwargs.setdefault("executable", ["scripts/run.sh"])
        return self.server.state.add_skill("demo", name, files, **kwargs)

    def user_settings(self) -> dict:
        return json.loads((self.claude / "settings.json").read_text())

    def project_settings(self) -> dict:
        return json.loads((self.project / ".claude/settings.local.json").read_text())

    def state(self) -> dict:
        return json.loads((self.plugin / ".baas-managed").read_text())

    def tree(self, root: Path | None = None) -> dict[str, bytes]:
        root = root or self.skills
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}

    def fingerprint(self) -> dict[str, tuple[int, int]]:
        """(inodo, mtime) de todo el plugin salvo `.baas-managed`: detecta reescrituras."""
        return {
            str(p.relative_to(self.plugin)): (p.stat().st_ino, p.stat().st_mtime_ns)
            for p in sorted(self.plugin.rglob("*"))
            if p.is_file() and p.name != ".baas-managed"
        }

    def set_roles(self, email: str, roles: list[str]) -> None:
        user = next(u for u in self.server.state.users if u.email == email)
        user.memberships[0].functional_roles = roles


@pytest.fixture
def server() -> Iterator[FakeBaasServer]:
    with FakeBaasServer() as srv:
        srv.state.skills["demo"].clear()
        yield srv


@pytest.fixture
def env(home, project, server, monkeypatch, capsys) -> Env:
    claude = home / ".claude"
    monkeypatch.setenv("BAAS_CLAUDE_HOME", str(claude))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key(KEY), "BAAS_APP_URL": server.url})
    (project / ".claude").mkdir()
    binding = {"org": "demo", "org_name": "Demo Clínica", "mcp_url": f"{server.url}/org/demo/mcp"}
    (project / ".claude/baas.local.json").write_text(json.dumps(binding))
    return Env(server, project, claude, capsys)


# --- primera sincronización ----------------------------------------------------------------------


def test_first_sync_writes_the_org_plugin(env: Env) -> None:
    env.publish()
    code, out = env.run()
    assert code == 0, out
    assert env.tree() == {
        "facturacion/SKILL.md": b"---\nname: facturacion\ndescription: x\n---\n# facturacion\n",
        "facturacion/scripts/run.sh": b"#!/bin/sh\necho hola\n",
        "facturacion/scripts/lib/util.py": b"X = 1\n",
        "facturacion/templates/informe.docx": BINARY,
        "facturacion/.env.example": b"A=1\n",
    }  # sin baas.yaml, y el binario llega byte a byte
    pkg = env.server.state.skills["demo"]["facturacion"]
    plugin = json.loads((env.plugin / ".claude-plugin/plugin.json").read_text())
    assert plugin["name"] == "demo"
    assert plugin["description"]
    assert len(plugin["version"]) == 12
    state = env.state()
    assert state["server"] == f"{env.server.url}/org/demo/mcp"
    assert state["org"] == "demo"
    assert state["synced_at"].endswith("Z")
    assert state["version"] == plugin["version"]
    entry = state["skills"]["facturacion"]
    assert entry["package_sha256"] == pkg.package_sha256
    files = {f["path"]: f for f in entry["files"]}
    assert files["scripts/run.sh"] == {
        "path": "scripts/run.sh",
        "sha256": hashlib.sha256(b"#!/bin/sh\necho hola\n").hexdigest(),
        "executable": True,
    }
    assert files["templates/informe.docx"]["sha256"] == hashlib.sha256(BINARY).hexdigest()
    assert files["templates/informe.docx"]["executable"] is False
    assert "baas.yaml" not in files
    assert "/reload-plugins" in out
    assert "/demo:facturacion" in out


def test_execute_bit_only_where_declared(env: Env) -> None:
    files = {"SKILL.md": "x", "scripts/a.sh": "#!/bin/sh\n", "scripts/b.sh": "#!/bin/sh\n", "c.py": "1"}
    env.publish(files=files, executable=["scripts/a.sh"])
    assert env.run()[0] == 0
    base = env.skills / "facturacion"
    assert os.access(base / "scripts/a.sh", os.X_OK)
    for rel in ("scripts/b.sh", "c.py", "SKILL.md"):
        assert not os.access(base / rel, os.X_OK), rel
    assert stat.S_IMODE((base / "SKILL.md").stat().st_mode) == 0o600


def test_manifest_paths_relative_to_the_skill_also_work(env: Env) -> None:
    env.server.state.package_paths = "relative"
    env.publish()
    assert env.run()[0] == 0
    assert (env.skills / "facturacion/scripts/lib/util.py").read_text() == "X = 1\n"


def test_org_name_comes_from_the_binding_and_slug_names_the_plugin(env: Env) -> None:
    env.publish()
    env.run()
    assert "Demo Clínica" in json.loads((env.plugin / ".claude-plugin/plugin.json").read_text())["description"]


# --- sin cambios ---------------------------------------------------------------------------------


def test_second_sync_without_changes_rewrites_nothing(env: Env) -> None:
    env.publish()
    env.publish("gestor")
    assert env.run()[0] == 0
    before = env.fingerprint()
    settings = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in (env.claude / "settings.json",)}
    local = env.project / ".claude/settings.local.json"
    local_before = (local.stat().st_ino, local.stat().st_mtime_ns)
    env.server.state.requests.clear()
    code, out = env.run()
    assert code == 0, out
    assert env.fingerprint() == before
    assert {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in settings} == settings
    assert (local.stat().st_ino, local.stat().st_mtime_ns) == local_before
    assert env.server.state.tool_calls("baas_get_skill_files") == []
    assert env.server.state.tool_calls("baas_get_skill_package") == []  # el hash de la lista basta
    assert "/reload-plugins" not in out
    assert "sin cambios" in out


def test_changing_one_file_only_updates_that_skill(env: Env) -> None:
    env.publish()
    env.publish("gestor")
    env.run()
    before = env.fingerprint()
    old_version = env.state()["version"]
    pkg = env.server.state.skills["demo"]["facturacion"]
    pkg.files["scripts/lib/util.py"] = PkgFile(b"X = 2\n")
    del pkg.files[".env.example"]  # un fichero que desaparece del paquete
    pkg.files["nuevo/dir/n.txt"] = PkgFile(b"n")
    env.server.state.requests.clear()
    code, out = env.run()
    assert code == 0, out
    assert (env.skills / "facturacion/scripts/lib/util.py").read_text() == "X = 2\n"
    assert not (env.skills / "facturacion/.env.example").exists()
    assert (env.skills / "facturacion/nuevo/dir/n.txt").read_text() == "n"
    after = env.fingerprint()
    assert {k: v for k, v in after.items() if "gestor" in k} == {k: v for k, v in before.items() if "gestor" in k}
    assert env.state()["skills"]["facturacion"]["package_sha256"] == pkg.package_sha256
    assert env.state()["version"] != old_version
    assert json.loads((env.plugin / ".claude-plugin/plugin.json").read_text())["version"] == env.state()["version"]
    assert "/reload-plugins" in out
    called = {a["skill"] for _, a in env.server.state.tool_calls("baas_get_skill_files")}
    assert called == {"facturacion"}


def test_force_reinstalls_everything(env: Env) -> None:
    env.publish()
    env.run()
    env.server.state.requests.clear()
    assert env.run("--force")[0] == 0
    assert len(env.server.state.tool_calls("baas_get_skill_files")) == 1


# --- revocación ----------------------------------------------------------------------------------


def test_role_removed_deletes_the_skill(env: Env) -> None:
    env.publish()
    env.publish("gestor", roles=["analista", "revisor"])
    env.publish("contabilidad", roles=["contable"])  # nunca la ve
    env.run()
    assert sorted(p.name for p in env.skills.iterdir()) == ["facturacion", "gestor"]
    env.set_roles(KEY, ["revisor"])
    code, out = env.run()
    assert code == 0, out
    assert sorted(p.name for p in env.skills.iterdir()) == ["gestor"]
    assert set(env.state()["skills"]) == {"gestor"}
    assert "facturacion" in out
    assert "/reload-plugins" in out


def test_wildcard_role_reaches_any_member(env: Env) -> None:
    env.publish("para-todos", roles=["*"])
    env.set_roles(KEY, [])
    assert env.run()[0] == 0
    assert (env.skills / "para-todos/SKILL.md").exists()


def test_retired_skill_is_deleted(env: Env) -> None:
    env.publish()
    env.publish("gestor")
    env.run()
    env.server.state.skills["demo"]["facturacion"].status = "deprecated"
    assert env.run()[0] == 0
    assert [p.name for p in env.skills.iterdir()] == ["gestor"]
    del env.server.state.skills["demo"]["gestor"]  # y una que ya no existe
    assert env.run()[0] == 0
    assert list(env.skills.iterdir()) == []
    assert env.state()["skills"] == {}


def test_anything_unauthorized_inside_the_plugin_is_removed(env: Env) -> None:
    """La carpeta de la organización es toda gestionada: lo no autorizado desaparece aunque no figure en el estado."""
    env.publish()
    env.run()
    (env.skills / "colada").mkdir()
    (env.skills / "colada/SKILL.md").write_text("de otra época")
    (env.skills / "suelto.txt").write_text("x")
    assert env.run()[0] == 0
    assert [p.name for p in env.skills.iterdir()] == ["facturacion"]


@pytest.mark.parametrize(
    ("setup", "scope"),
    [
        (lambda s: setattr(s.state.orgs["demo"], "blocked", True), "ORG_BLOCKED"),
        (lambda s: setattr(s.state.orgs["demo"], "paused", True), "ORG_PAUSED"),
        (lambda s: setattr(s.state, "global_kill_switch", True), "GLOBAL"),
    ],
)
def test_kill_switch_removes_all_skills(env: Env, setup, scope) -> None:
    env.publish()
    env.publish("gestor")
    env.run()
    setup(env.server)
    code, out = env.run()
    assert code == 0, out
    assert list(env.skills.iterdir()) == []
    assert env.state()["skills"] == {}
    assert (env.plugin / ".baas-managed").exists()  # el directorio sigue siendo gestionado
    assert "KILL_SWITCH_ACTIVE" in out
    assert scope in out
    assert "/reload-plugins" in out
    # Al levantar el interruptor vuelven
    env.server.state.global_kill_switch = False
    env.server.state.orgs["demo"].blocked = env.server.state.orgs["demo"].paused = False
    assert env.run()[0] == 0
    assert sorted(p.name for p in env.skills.iterdir()) == ["facturacion", "gestor"]


def test_kill_switch_without_a_previous_sync_creates_nothing(env: Env) -> None:
    env.publish()
    env.server.state.global_kill_switch = True
    code, out = env.run()
    assert code == 0
    assert not env.claude.exists()
    assert "KILL_SWITCH_ACTIVE" in out


# --- solo toca lo suyo ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["dir", "file", "empty-dir", "symlink"])
def test_foreign_directory_with_the_same_name_aborts(env: Env, tmp_path: Path, kind: str) -> None:
    env.publish()
    target = env.claude / "skills" / "demo"
    target.parent.mkdir(parents=True)
    outside = tmp_path / "otro-sitio"
    outside.mkdir()
    if kind == "dir":
        (target / "skills/mia").mkdir(parents=True)
        (target / "skills/mia/SKILL.md").write_text("mi skill")
        (target / ".claude-plugin").mkdir()
        (target / ".claude-plugin/plugin.json").write_text('{"name": "demo", "version": "1.0.0"}')
    elif kind == "file":
        target.write_text("un fichero")
    elif kind == "empty-dir":
        target.mkdir()
    else:
        target.symlink_to(outside, target_is_directory=True)
    before = sorted(str(p.relative_to(env.claude)) for p in env.claude.rglob("*"))
    code, out = env.run()
    assert code == 13, out
    assert "BAAS_SYNC_FOREIGN_DIR" in out
    assert str(target) in out
    assert sorted(str(p.relative_to(env.claude)) for p in env.claude.rglob("*")) == before
    assert list(outside.iterdir()) == []
    assert not (env.claude / "settings.json").exists()  # tampoco toca la configuración
    assert not (env.project / ".claude/settings.local.json").exists()
    if kind == "dir":
        assert (target / "skills/mia/SKILL.md").read_text() == "mi skill"
    assert env.server.state.tool_calls("baas_get_skill_files") == []


def test_other_skills_of_the_user_are_never_touched(env: Env) -> None:
    env.publish()
    mine = env.claude / "skills" / "demo-notas"  # un nombre parecido, de otro origen
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mía")
    other = env.claude / "skills" / "otra-org"
    (other / "skills/x").mkdir(parents=True)
    (other / ".baas-managed").write_text(json.dumps({"org": "otra-org", "skills": {}}))
    (other / "skills/x/SKILL.md").write_text("de otra organización")
    assert env.run()[0] == 0
    assert (mine / "SKILL.md").read_text() == "mía"
    assert (other / "skills/x/SKILL.md").read_text() == "de otra organización"


def test_symlinked_skill_dir_is_replaced_without_following_it(env: Env, tmp_path: Path) -> None:
    env.publish()
    env.run()
    outside = tmp_path / "fuera"
    outside.mkdir()
    (outside / "precioso.txt").write_text("no me toques")
    (env.skills / "gestor").symlink_to(outside, target_is_directory=True)
    env.publish("gestor")
    assert env.run()[0] == 0
    assert not (env.skills / "gestor").is_symlink()
    assert (env.skills / "gestor/SKILL.md").exists()
    assert (outside / "precioso.txt").read_text() == "no me toques"
    assert [p.name for p in outside.iterdir()] == ["precioso.txt"]


def test_removing_a_symlinked_entry_does_not_follow_it(env: Env, tmp_path: Path) -> None:
    env.publish()
    env.run()
    outside = tmp_path / "fuera"
    outside.mkdir()
    (outside / "precioso.txt").write_text("no me toques")
    (env.skills / "vieja").symlink_to(outside, target_is_directory=True)
    assert env.run()[0] == 0
    assert not (env.skills / "vieja").exists()
    assert (outside / "precioso.txt").read_text() == "no me toques"


@pytest.mark.parametrize("fixed", [".claude-plugin", "skills"])
def test_symlinked_plugin_internals_abort(env: Env, tmp_path: Path, fixed: str) -> None:
    env.publish()
    env.run()
    outside = tmp_path / "fuera"
    outside.mkdir()
    internal = env.plugin / fixed
    shutil.rmtree(internal)
    internal.symlink_to(outside, target_is_directory=True)
    env.publish("otra")
    code, out = env.run()
    assert code == 13, out
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("bad", ["../escape.txt", "a/../../escape.txt", "/etc/passwd", "a\\b", "a//b", "./x"])
def test_unsafe_paths_are_rejected_and_the_skill_is_not_installed(env: Env, tmp_path: Path, bad: str) -> None:
    env.publish("buena")
    env.publish("mala").files[bad] = PkgFile(b"pwned")
    env.server.state.package_paths = "relative"
    code, out = env.run()
    assert code == 12, out  # sincronización incompleta
    assert "BAAS_SYNC_INCOMPLETE" in out
    assert "mala" in out
    assert not (env.skills / "mala").exists()
    assert (env.skills / "buena/SKILL.md").exists()  # el resto sí se aplica
    assert "mala" not in env.state()["skills"]
    assert not (env.claude / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert not Path("/etc/passwd").read_bytes().startswith(b"pwned")


def test_wrong_hash_is_not_installed_and_keeps_the_previous_version(env: Env) -> None:
    env.publish()
    env.publish("gestor")
    env.run()
    pkg = env.server.state.skills["demo"]["facturacion"]
    pkg.files["scripts/lib/util.py"] = PkgFile(b"X = 99\n", sha256_override="0" * 64)
    env.publish("gestor-2")
    code, out = env.run()
    assert code == 12, out
    assert "scripts/lib/util.py" in out
    assert (env.skills / "facturacion/scripts/lib/util.py").read_text() == "X = 1\n"  # intacta
    assert env.state()["skills"]["facturacion"]["package_sha256"] != pkg.package_sha256
    assert (env.skills / "gestor-2/SKILL.md").exists()  # las demás sí
    assert not any(p.name.startswith(".baas-tmp") for p in env.plugin.iterdir())


def test_unsafe_manifest_on_an_update_keeps_the_previous_version(env: Env) -> None:
    env.publish()
    env.run()
    env.server.state.skills["demo"]["facturacion"].files["../escape.txt"] = PkgFile(b"x")
    code, out = env.run()
    assert code == 12, out
    assert (env.skills / "facturacion/SKILL.md").exists()  # no se retira por un manifiesto roto
    assert "facturacion" in env.state()["skills"]


def test_transient_server_error_aborts_without_removing_anything(env: Env, monkeypatch) -> None:
    from baas_plugin import package_fetch
    from baas_plugin.errors import McpToolError

    env.publish()
    env.publish("gestor")
    env.run()
    before = env.fingerprint()
    env.server.state.skills["demo"]["gestor"].files["x.txt"] = PkgFile(b"1")  # fuerza pedir su manifiesto
    real = package_fetch.fetch_manifest

    def flaky(client, skill, org=None):
        if skill == "gestor":
            raise McpToolError("UNAVAILABLE", "sin política")
        return real(client, skill, org)

    monkeypatch.setattr(package_fetch, "fetch_manifest", flaky)
    code, out = env.run()
    assert code == 10, out
    assert env.fingerprint() == before
    assert sorted(p.name for p in env.skills.iterdir()) == ["facturacion", "gestor"]


def test_wrong_hash_on_a_new_skill_installs_nothing_of_it(env: Env) -> None:
    pkg = env.publish()
    pkg.files["SKILL.md"] = PkgFile(b"hola", sha256_override="1" * 64)
    code, _ = env.run()
    assert code == 12
    assert not (env.skills / "facturacion").exists()


# --- dependencias --------------------------------------------------------------------------------


def test_dependencies_are_installed_transitively(env: Env) -> None:
    env.publish("a", depends_on=["b"])
    env.publish("b", depends_on=["c"])
    env.publish("c")
    env.publish("suelta")
    env.server.state.list_filter = {"a"}  # la lista solo trae «a»: «b» y «c» llegan por sus dependencias
    code, out = env.run()
    assert code == 0, out
    assert sorted(p.name for p in env.skills.iterdir()) == ["a", "b", "c"]
    assert env.state()["skills"]["a"]["depends_on"] == ["b"]


def test_dependency_cycle_terminates(env: Env) -> None:
    env.publish("a", depends_on=["b"])
    env.publish("b", depends_on=["a"])
    env.server.state.list_filter = {"a"}
    code, out = env.run()
    assert code == 0, out
    assert sorted(p.name for p in env.skills.iterdir()) == ["a", "b"]


def test_inaccessible_dependency_is_reported_and_never_granted(env: Env) -> None:
    env.publish("a", depends_on=["b", "secreta", "inexistente"])
    env.publish("b", depends_on=["secreta"])
    env.publish("secreta", roles=["contable"])
    code, out = env.run()
    assert code == 0, out
    assert sorted(p.name for p in env.skills.iterdir()) == ["a", "b"]
    assert "«a» depende de «secreta»" in out
    assert "«b» depende de «secreta»" in out
    assert "«a» depende de «inexistente»" in out
    assert any("secreta" in w for w in env.state()["warnings"])
    # Ni siquiera se pide el paquete de una dependencia que el manifiesto declara inaccesible
    asked = [a["skill"] for _, a in env.server.state.tool_calls("baas_get_skill_package")]
    assert "secreta" not in asked
    assert "inexistente" not in asked


def test_dependency_that_became_accessible_after_a_sync_without_changes(env: Env) -> None:
    env.publish("a", depends_on=["b"])
    env.publish("b", roles=["contable"])
    env.run()
    assert not (env.skills / "b").exists()
    env.set_roles(KEY, ["analista", "contable"])
    assert env.run()[0] == 0
    assert (env.skills / "b/SKILL.md").exists()
    # «a» no cambió: se sabe por el estado local, sin volver a pedir sus ficheros
    assert [a["skill"] for _, a in env.server.state.tool_calls("baas_get_skill_files")] == ["a", "b"]


# --- activación por proyecto ---------------------------------------------------------------------


def test_plugin_is_off_for_the_user_and_on_for_the_project(env: Env) -> None:
    env.publish()
    code, out = env.run()
    assert code == 0, out
    assert env.user_settings() == {"enabledPlugins": {SKILLS_KEY: False}}
    assert env.project_settings() == {"enabledPlugins": {SKILLS_KEY: True}}
    gitignore = (env.project / ".gitignore").read_text()
    assert ".claude/settings.local.json" in gitignore
    assert ".claude/baas.local.json" in gitignore
    assert stat.S_IMODE((env.project / ".claude/settings.local.json").stat().st_mode) == 0o600


def test_activation_keeps_the_rest_of_both_settings_files(env: Env) -> None:
    env.publish()
    (env.claude).mkdir(parents=True)
    (env.claude / "settings.json").write_text(
        json.dumps({"model": "opus", "enabledPlugins": {"otro@mkt": True}, "permissions": {"allow": ["Bash(ls)"]}})
    )
    (env.project / ".claude/settings.local.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://x"}, "enabledPlugins": {"mio@skills-dir": True}})
    )
    assert env.run()[0] == 0
    assert env.user_settings() == {
        "model": "opus",
        "enabledPlugins": {"otro@mkt": True, SKILLS_KEY: False},
        "permissions": {"allow": ["Bash(ls)"]},
    }
    assert env.project_settings() == {
        "env": {"ANTHROPIC_BASE_URL": "http://x"},
        "enabledPlugins": {"mio@skills-dir": True, SKILLS_KEY: True},
    }


def test_user_level_activation_is_turned_off(env: Env) -> None:
    env.publish()
    env.claude.mkdir(parents=True)
    (env.claude / "settings.json").write_text(json.dumps({"enabledPlugins": {SKILLS_KEY: True}}))
    code, out = env.run()
    assert code == 0
    assert env.user_settings()["enabledPlugins"][SKILLS_KEY] is False
    assert "nivel de usuario" in out


def test_settings_are_resolved_through_symlinks(env: Env, tmp_path: Path) -> None:
    """Un settings.json enlazado (dotfiles) sigue siendo un enlace: se escribe el destino."""
    env.publish()
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"model": "opus"}))
    env.claude.mkdir(parents=True)
    (env.claude / "settings.json").symlink_to(real)
    assert env.run()[0] == 0
    assert (env.claude / "settings.json").is_symlink()
    assert json.loads(real.read_text()) == {"model": "opus", "enabledPlugins": {SKILLS_KEY: False}}


def test_unreadable_user_settings_block_installs_but_not_removals(env: Env) -> None:
    env.publish()
    env.run()
    (env.claude / "settings.json").write_text("{ esto no es json")
    env.publish("nueva")
    env.server.state.skills["demo"]["facturacion"].status = "deprecated"
    code, out = env.run()
    assert code == 0, out
    assert not (env.skills / "nueva").exists()  # no se instala sin poder desactivarlo a nivel de usuario
    assert not (env.skills / "facturacion").exists()  # pero la retirada sí se aplica
    assert "settings.json" in out
    assert (env.claude / "settings.json").read_text() == "{ esto no es json"


def test_project_settings_tracked_by_git_are_not_touched(env: Env) -> None:
    env.publish()
    subprocess.run(["git", "-C", str(env.project), "init", "-q"], check=True)
    path = env.project / ".claude/settings.local.json"
    path.write_text("{}\n")
    subprocess.run(["git", "-C", str(env.project), "add", "-f", ".claude/settings.local.json"], check=True)
    code, out = env.run()
    assert code == 0, out
    assert path.read_text() == "{}\n"
    assert "versionado" in out
    assert (env.skills / "facturacion/SKILL.md").exists()  # la skill se instala igualmente


def test_switching_the_project_org_deactivates_the_previous_one(env: Env) -> None:
    env.publish()
    env.run()
    # Otro plugin gestionado (de una organización anterior del proyecto) y uno ajeno
    other = env.claude / "skills" / "otra"
    other.mkdir(parents=True)
    (other / ".baas-managed").write_text("{}")
    settings = env.project / ".claude/settings.local.json"
    data = json.loads(settings.read_text())
    data["enabledPlugins"].update({"otra@skills-dir": True, "personal@skills-dir": True})
    settings.write_text(json.dumps(data))
    assert env.run()[0] == 0
    assert env.project_settings()["enabledPlugins"] == {SKILLS_KEY: True, "personal@skills-dir": True}


# --- MCP caído, lento o sin acceso ----------------------------------------------------------------


def test_mcp_down_touches_nothing(env: Env, tmp_path: Path) -> None:
    env.publish()
    env.run()
    before = env.fingerprint()
    state = (env.plugin / ".baas-managed").read_bytes()
    binding = json.loads((env.project / ".claude/baas.local.json").read_text())
    binding["mcp_url"] = "http://127.0.0.1:9/org/demo/mcp"
    (env.project / ".claude/baas.local.json").write_text(json.dumps(binding))
    code, out = env.run()
    assert code == 6, out
    assert env.fingerprint() == before
    assert (env.plugin / ".baas-managed").read_bytes() == state


def test_slow_mcp_is_given_up_quickly_and_touches_nothing(env: Env) -> None:
    env.publish()
    env.server.state.mcp_delay = 3.0
    start = time.monotonic()
    code, out = env.run("--timeout", "0.5")
    elapsed = time.monotonic() - start
    assert code == 6, out
    assert elapsed < 2.0
    assert not env.claude.exists()


def test_quiet_mode_fails_open(env: Env) -> None:
    env.publish()
    env.server.state.mcp_status = 503
    code, out = env.run("--quiet")
    assert code == 0
    assert "UNAVAILABLE" in out  # queda en el registro del hook, pero no falla
    assert not env.claude.exists()


def test_quiet_mode_prints_nothing_on_success(env: Env) -> None:
    env.publish()
    code, out = env.run("--quiet")
    assert code == 0
    assert out == ""
    assert (env.skills / "facturacion/SKILL.md").exists()


def test_invalid_key_touches_nothing(env: Env) -> None:
    env.publish()
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("nadie@demo.test")})
    code, _ = env.run()
    assert code == 5
    assert not env.claude.exists()


@pytest.mark.parametrize("how", ["unknown-key", "not-a-member"])
def test_losing_the_org_is_an_error_not_a_role_revocation(env: Env, how: str) -> None:
    """Un 401 o un 404 es un error del MCP: no borra nada (solo la retirada, el rol o el kill switch lo hacen)."""
    env.publish()
    env.run()
    before = env.fingerprint()
    if how == "unknown-key":
        del env.server.state.users[:]
    else:
        next(u for u in env.server.state.users if u.email == KEY).memberships = []
    code, _ = env.run()
    assert code != 0
    assert env.fingerprint() == before


# --- dry-run, vinculación y varios ---------------------------------------------------------------


def test_dry_run_writes_nothing(env: Env) -> None:
    env.publish()
    code, out = env.run("--dry-run")
    assert code == 0, out
    assert not env.claude.exists()
    assert not (env.project / ".claude/settings.local.json").exists()
    assert not (envfile.BaasPaths.default().root / "sync.lock").exists()
    assert "facturacion" in out
    assert "dry-run" in out.lower()
    assert env.server.state.tool_calls("baas_get_skill_files") == []


def test_dry_run_reports_updates_and_removals(env: Env) -> None:
    env.publish()
    env.publish("gestor")
    env.run()
    before = env.fingerprint()
    env.server.state.skills["demo"]["facturacion"].files["x.txt"] = PkgFile(b"1")
    env.server.state.skills["demo"]["gestor"].status = "deprecated"
    env.publish("nueva")
    code, out = env.run("--dry-run")
    assert code == 0
    assert env.fingerprint() == before
    for expected in ("facturacion", "gestor", "nueva"):
        assert expected in out


def test_project_without_binding_is_a_config_error(env: Env, tmp_path: Path) -> None:
    loose = tmp_path / "suelto"
    loose.mkdir()
    code, out = env.run(project=loose)
    assert code == 2
    assert "/baas-init" in out
    assert not env.claude.exists()


def test_sync_without_key_is_a_config_error(env: Env) -> None:
    (envfile.BaasPaths.default().env_file).unlink()
    code, out = env.run()
    assert code == 2
    assert "BAAS_KEY_MISSING" in out


@pytest.mark.parametrize("slug", ["../x", "A b", "", "-x", "x/y"])
def test_binding_slug_must_be_a_plain_kebab_name(env: Env, slug: str) -> None:
    binding = json.loads((env.project / ".claude/baas.local.json").read_text())
    binding["org"] = slug
    (env.project / ".claude/baas.local.json").write_text(json.dumps(binding))
    code, _ = env.run()
    assert code == 2
    assert not env.claude.exists()


def test_skills_root_option_moves_the_plugin_dir(env: Env, tmp_path: Path) -> None:
    env.publish()
    root = tmp_path / "otras-skills"
    assert env.run("--skills-root", str(root))[0] == 0
    assert (root / "demo/skills/facturacion/SKILL.md").exists()
    assert not (env.claude / "skills").exists()
    assert env.user_settings()["enabledPlugins"][SKILLS_KEY] is False  # la configuración sigue en BAAS_CLAUDE_HOME


def test_claude_home_option_overrides_the_environment(env: Env, tmp_path: Path) -> None:
    env.publish()
    other = tmp_path / "otro-claude"
    assert env.run("--claude-home", str(other))[0] == 0
    assert (other / "skills/demo/skills/facturacion/SKILL.md").exists()
    assert not env.claude.exists()


def test_claude_config_dir_is_honoured_when_no_baas_variable(env: Env, tmp_path: Path, monkeypatch) -> None:
    env.publish()
    monkeypatch.delenv("BAAS_CLAUDE_HOME")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert env.run()[0] == 0
    assert (tmp_path / "cfg/skills/demo/skills/facturacion/SKILL.md").exists()


def test_another_sync_in_progress_is_not_disturbed(env: Env) -> None:
    fcntl = pytest.importorskip("fcntl")
    env.publish()
    paths = envfile.BaasPaths.default()
    envfile.ensure_private_dir(paths.root)
    fd = os.open(paths.root / "sync.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, out = env.run()
    finally:
        os.close(fd)
    assert code == 0
    assert "en curso" in out
    assert not env.claude.exists()
    assert env.run()[0] == 0  # liberado el bloqueo, sincroniza


def test_stale_temp_dirs_are_cleaned(env: Env) -> None:
    env.publish()
    env.run()
    stale = env.plugin / ".baas-tmp-1234-viejo"
    (stale / "new-x").mkdir(parents=True)
    (stale / "new-x/f").write_text("x")
    assert env.run()[0] == 0
    assert not stale.exists()


def test_corrupt_state_file_is_recovered(env: Env) -> None:
    env.publish()
    env.run()
    (env.plugin / ".baas-managed").write_text("{ roto")
    (env.skills / "colada").mkdir()
    assert env.run()[0] == 0
    assert [p.name for p in env.skills.iterdir()] == ["facturacion"]
    assert env.state()["skills"]["facturacion"]["package_sha256"]


# --- estado (/baas-status) -----------------------------------------------------------------------


def status(env: Env, *args: str) -> str:
    code = cli.main(["status", "--project", str(env.project), *args])
    out = env.capsys.readouterr()
    assert code == 0, out
    return out.out + out.err


def test_status_without_sync_says_so(env: Env) -> None:
    env.publish()
    out = status(env)
    assert "Skills sincronizadas: ninguna" in out
    assert "/baas-sync" in out
    assert "pendientes de instalar: facturacion" in out


def test_status_reports_synced_outdated_and_revoked(env: Env) -> None:
    env.publish()
    env.publish("gestor")
    env.publish("vieja")
    env.run()
    env.server.state.skills["demo"]["gestor"].files["x.txt"] = PkgFile(b"1")  # desfasada
    env.server.state.skills["demo"]["vieja"].status = "deprecated"  # revocada o retirada
    env.publish("nueva")
    out = status(env)
    assert "al día: facturacion" in out
    assert "desfasadas: gestor" in out
    assert "revocadas o retiradas: vieja" in out
    assert "pendientes de instalar: nueva" in out
    assert "Plugin activo en este proyecto: sí" in out
    assert (env.skills / "vieja").exists()  # status no cambia nada


def test_status_without_mcp_shows_the_local_state(env: Env) -> None:
    env.publish()
    env.run()
    out = status(env, "--no-mcp")
    assert "Skills sincronizadas: 1" in out
    assert "facturacion" in out
    assert "Última sincronización" in out


def test_status_shows_warnings_of_the_last_sync(env: Env) -> None:
    env.publish("a", depends_on=["secreta"])
    env.publish("secreta", roles=["contable"])
    env.run()
    assert "«a» depende de «secreta»" in status(env, "--no-mcp")


def test_status_with_kill_switch_announces_the_removal(env: Env) -> None:
    env.publish()
    env.run()
    env.server.state.orgs["demo"].paused = True
    out = status(env)
    assert "KILL_SWITCH_ACTIVE" in out
    assert "se retirarán" in out


# --- hooks ---------------------------------------------------------------------------------------

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
HOOK = SCRIPTS / "baas_sync_hook.py"
PLUGIN_ROOT = SCRIPTS.parent


def run_hook(env: Env, event: str, project: Path | None = None, **extra_env: str) -> subprocess.CompletedProcess[str]:
    child_env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE"))}
    child_env.update({"HOME": str(envfile.BaasPaths.default().root.parent), "BAAS_CLAUDE_HOME": str(env.claude)})
    child_env.update(extra_env)
    stdin = json.dumps({"hook_event_name": event, "cwd": str(project or env.project)})
    return subprocess.run(
        [sys.executable, str(HOOK), "--event", event],
        input=stdin,
        capture_output=True,
        text=True,
        env=child_env,
        check=False,
        timeout=20,
    )


def wait_for(path: Path, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("event", ["session-start", "session-end"])
def test_hook_syncs_in_the_background_and_returns_at_once(env: Env, event: str) -> None:
    env.publish()
    env.server.state.mcp_delay = 0.0
    start = time.monotonic()
    res = run_hook(env, event)
    elapsed = time.monotonic() - start
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""  # un hook de SessionStart añade su stdout al contexto del agente: nada
    assert elapsed < 3.0
    assert wait_for(env.skills / "facturacion/SKILL.md")


def test_hook_does_not_wait_for_a_slow_mcp(env: Env) -> None:
    env.publish()
    env.server.state.mcp_delay = 5.0
    start = time.monotonic()
    res = run_hook(env, "session-start", BAAS_SYNC_TIMEOUT="0.5")
    assert time.monotonic() - start < 2.0
    assert res.returncode == 0
    assert res.stdout == ""
    time.sleep(0.3)
    assert not env.claude.exists()  # nada escrito mientras el MCP no responde


def test_hook_is_silent_in_unbound_projects(env: Env, tmp_path: Path) -> None:
    loose = tmp_path / "suelto"
    loose.mkdir()
    res = run_hook(env, "session-start", project=loose)
    assert (res.returncode, res.stdout, res.stderr) == (0, "", "")
    time.sleep(0.3)
    assert env.server.state.requests == []


def test_hook_survives_garbage_input(env: Env) -> None:
    res = subprocess.run(
        [sys.executable, str(HOOK), "--event", "nope"], input="no json", capture_output=True, text=True, check=False
    )
    assert res.returncode == 0
    assert res.stdout == ""


def test_hooks_manifest_registers_start_and_end() -> None:
    manifest = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    start = [h for g in manifest["SessionStart"] for h in g["hooks"]]
    guard = [h for h in start if "baas_guard.py" in h["command"]]
    launch = [h for h in start if "baas_sync_hook.py" in h["command"]]
    assert len(guard) == 1  # la guardia de Desktop sigue donde estaba
    assert len(launch) == 1
    assert "--event session-start" in launch[0]["command"]
    assert launch[0]["timeout"] <= 5
    end = [h for g in manifest["SessionEnd"] for h in g["hooks"]]
    assert len(end) == 1
    assert "baas_sync_hook.py" in end[0]["command"]
    assert "--event session-end" in end[0]["command"]
    assert end[0]["timeout"] <= 5
    assert "UserPromptSubmit" in manifest


# --- ficheros grandes (D64): por trozos y directo a disco --------------------------------------------


def big_skill(env: Env, big: bytes, monkeypatch: pytest.MonkeyPatch, **extra) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(transfer, "CHUNK_THRESHOLD", 3000)
    monkeypatch.setattr(transfer.time, "sleep", lambda _s: None)
    env.server.state.chunk_size = 1000
    files = {
        "SKILL.md": "---\nname: grande\ndescription: x\n---\nUsa data/blob.bin\n",
        "scripts/run.sh": "#!/bin/sh\necho hola\n",
        "data/blob.bin": big,
    }
    env.publish("grande", files=files, **extra)


def test_a_big_file_is_synced_by_chunks_and_never_inline(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    big = os.urandom(10_500)
    big_skill(env, big, monkeypatch)
    code, out = env.run()
    assert code == 0, out
    assert env.tree()["grande/data/blob.bin"] == big
    assert (env.skills / "grande/scripts/run.sh").stat().st_mode & 0o111  # el bit de ejecución sigue donde toca
    asked = [p for _, a in env.server.state.tool_calls("baas_get_skill_files") for p in a["paths"]]
    assert "skills/grande/data/blob.bin" not in asked
    assert "skills/grande/SKILL.md" in asked
    reads = [a for _, a in env.server.state.tool_calls("baas_read_chunk")]
    assert [r["offset"] for r in reads] == list(range(0, 10_500, 1000))
    assert {r["path"] for r in reads} == {"skills/grande/data/blob.bin"}
    entry = {f["path"]: f for f in env.state()["skills"]["grande"]["files"]}["data/blob.bin"]
    assert entry["sha256"] == hashlib.sha256(big).hexdigest()


def test_a_corrupt_big_file_is_not_installed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    big = os.urandom(8000)
    big_skill(env, big, monkeypatch)
    pkg = env.server.state.skills["demo"]["grande"]
    pkg.files["data/blob.bin"] = PkgFile(big, sha256_override="0" * 64)  # declara un hash y sirve otros bytes
    code, out = env.run()
    assert code == 12, out
    assert "data/blob.bin" in out
    assert not (env.skills / "grande").exists()
    assert not any(p.name.startswith(".baas-tmp") for p in env.plugin.iterdir())


def test_a_big_file_that_changes_on_the_server_while_syncing_is_not_mixed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = os.urandom(8000)
    big_skill(env, big, monkeypatch)
    pkg = env.server.state.skills["demo"]["grande"]
    real = pkg.files["data/blob.bin"]
    # el manifiesto se lee con el hash viejo y, al pedir los trozos, el servidor ya tiene otro fichero
    original = type(real).sha256.fget  # type: ignore[attr-defined]
    calls = {"n": 0}

    def drifting(self):  # type: ignore[no-untyped-def]
        if self is real:
            calls["n"] += 1
            return original(self) if calls["n"] <= 3 else "f" * 64
        return original(self)

    monkeypatch.setattr(type(real), "sha256", property(drifting))
    code, out = env.run()
    assert code == 12
    assert "cambió en el servidor" in out
    assert not (env.skills / "grande").exists()
