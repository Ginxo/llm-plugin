"""`baas.py sync --tool antigravity` (T19 · #192): las skills de la organización como skills nativas de Antigravity.

Todo contra el BaaS falso (`dev/fake_baas.py`), con `HOME` temporal y claves ficticias: nunca se toca el `~/.gemini`
real ni se ejecuta `agy`.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from fake_baas import FakeBaasServer, PkgFile, dev_api_key

from baas_plugin import cli, envfile

CONTROL = ".baas-skills.json"
posix_only = pytest.mark.skipif(os.name != "posix", reason="los permisos POSIX solo se comprueban en POSIX")
BINARY = bytes(range(256)) * 4  # no es UTF-8 válido


@dataclass
class Env:
    server: FakeBaasServer
    home: Path
    project: Path
    capsys: pytest.CaptureFixture[str]
    key: str

    @property
    def root(self) -> Path:
        return self.home / ".gemini" / "config" / "skills"

    def run(self, *args: str) -> tuple[int, str]:
        code = cli.main(["sync", "--tool", "antigravity", "--project", str(self.project), *args])
        out = self.capsys.readouterr()
        return code, out.out + out.err

    def publish(self, name: str = "facturacion", org: str = "demo", **kwargs):
        files = kwargs.pop("files", {"SKILL.md": f"---\nname: {name}\ndescription: Emite facturas\n---\n\n# {name}\n"})
        kwargs.setdefault("roles", ["analista"])
        return self.server.state.add_skill(org, name, files, **kwargs)

    def control(self) -> dict:
        return json.loads((self.root / CONTROL).read_text(encoding="utf-8"))

    def tree(self, base: Path | None = None) -> dict[str, bytes]:
        base = base or self.root
        out: dict[str, bytes] = {}
        for dirpath, _dirs, files in os.walk(base):
            for f in files:
                p = Path(dirpath) / f
                out[str(p.relative_to(base))] = p.read_bytes() if not p.is_symlink() else b"<enlace>"
        return out


@pytest.fixture
def env(home, project, capsys) -> Iterator[Env]:
    with FakeBaasServer() as server:
        for skills in server.state.skills.values():
            skills.clear()  # el servidor falso trae skills de ejemplo: aquí cada test publica las suyas
        key = dev_api_key("analista@demo.test")
        envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": key, "BAAS_APP_URL": server.url})
        yield Env(server, home, project, capsys, key)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frontmatter(text: str) -> dict:
    assert text.startswith("---\n")
    return yaml.safe_load(text[4 : text.index("\n---", 4)])


# --- alta, actualización y retirada -----------------------------------------------------------


def test_new_skill_is_installed_as_a_native_skill_with_a_control_file(env: Env) -> None:
    pkg = env.publish()
    code, out = env.run()
    assert code == 0, out
    skill = env.root / "facturacion" / "SKILL.md"
    meta = frontmatter(skill.read_text(encoding="utf-8"))
    assert meta == {"name": "facturacion", "description": "Emite facturas"}
    control = env.control()
    assert control["managed_by"] == "baas-llm-plugin"
    assert control["org"] == "demo"
    assert control["skills"]["facturacion"]["package_sha256"] == pkg.package_sha256
    recorded = {f["path"]: f["sha256"] for f in control["skills"]["facturacion"]["files"]}
    assert recorded == {"SKILL.md": sha(skill.read_bytes())}, "se registra lo escrito (ya adaptado), no lo del servidor"
    assert env.key not in json.dumps(control)
    assert "+ facturacion" in out
    assert "/skills reload" in out
    assert [p.name for p in env.root.iterdir() if p.name.startswith(".baas-tmp")] == []


def test_the_command_is_the_folder_name_and_other_files_of_the_skill_are_copied(env: Env) -> None:
    env.publish(
        files={
            "SKILL.md": "---\nname: facturacion\ndescription: x\n---\n# t\n",
            "references/normativa.md": "# Normativa\n",
            "templates/informe.docx": BINARY,
            ".env.example": "A=1\n",
        }
    )
    code, out = env.run()
    assert code == 0, out
    base = env.root / "facturacion"
    assert (base / "references/normativa.md").read_text() == "# Normativa\n"
    assert (base / "templates/informe.docx").read_bytes() == BINARY
    assert (base / ".env.example").read_text() == "A=1\n"


def test_unchanged_skills_are_not_downloaded_again(env: Env) -> None:
    env.publish()
    assert env.run()[0] == 0
    before = env.tree()
    env.server.state.requests.clear()
    code, out = env.run()
    assert code == 0, out
    assert "Todo al día" in out
    assert env.tree() == before
    assert env.server.state.tool_calls("baas_get_skill_package") == []


def test_a_changed_skill_is_updated(env: Env) -> None:
    pkg = env.publish(files={"SKILL.md": "---\nname: facturacion\ndescription: v1\n---\n# a\n", "old.md": "x"})
    assert env.run()[0] == 0
    pkg.files["SKILL.md"] = PkgFile(b"---\nname: facturacion\ndescription: v2\n---\n# b\n")
    del pkg.files["old.md"]
    code, out = env.run()
    assert code == 0, out
    assert "~ facturacion" in out
    assert frontmatter((env.root / "facturacion/SKILL.md").read_text())["description"] == "v2"
    assert not (env.root / "facturacion/old.md").exists()
    assert env.control()["skills"]["facturacion"]["package_sha256"] == pkg.package_sha256


def test_a_revoked_skill_is_removed_and_the_users_own_skills_are_left_alone(env: Env) -> None:
    env.publish("facturacion")
    env.publish("gestor")
    mine = env.root / "mia"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\ndescription: mia\n---\nmia\n")
    (env.root / "notas.txt").write_text("mis notas")
    assert env.run()[0] == 0
    env.server.state.skills["demo"]["gestor"].roles = ["otro-rol"]  # el usuario pierde el acceso
    code, out = env.run()
    assert code == 0, out
    assert "- gestor" in out
    assert not (env.root / "gestor").exists()
    assert (env.root / "facturacion/SKILL.md").exists()
    assert (mine / "SKILL.md").read_text() == "---\ndescription: mia\n---\nmia\n"
    assert (env.root / "notas.txt").read_text() == "mis notas"
    assert sorted(env.control()["skills"]) == ["facturacion"]


def test_a_deprecated_or_deleted_skill_is_removed(env: Env) -> None:
    env.publish("a")
    env.publish("b")
    assert env.run()[0] == 0
    env.server.state.skills["demo"]["a"].status = "deprecated"
    del env.server.state.skills["demo"]["b"]
    code, out = env.run()
    assert code == 0, out
    assert [p.name for p in env.root.iterdir() if not p.name.startswith(".")] == []


def test_kill_switch_removes_every_managed_skill(env: Env) -> None:
    env.publish("a")
    env.publish("b")
    mine = env.root / "mia"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\ndescription: mia\n---\n")
    assert env.run()[0] == 0
    env.server.state.global_kill_switch = True
    code, out = env.run()
    assert code == 0, out
    assert "KILL_SWITCH_ACTIVE" in out
    assert sorted(p.name for p in env.root.iterdir() if not p.name.startswith(".")) == ["mia"]


# --- adaptación del SKILL.md ------------------------------------------------------------------


def test_skill_md_keeps_only_name_and_description_and_the_body_byte_for_byte(env: Env) -> None:
    body = "\n# Skill: facturas\n\nUsa `scripts/x.py`. Acentos: ñ é — «sí».\n\n```bash\necho 1\n```\n"
    original = (
        "---\n"
        "name: otro-nombre\n"
        "description: >-\n  Use this skill to invoice.\n  Second line: with colon.\n"
        "when_to_use: Cuando pidan una factura\n"
        "allowed-tools: Bash(python3 scripts/x.py *) Read\n"
        "hooks:\n  PreToolUse:\n    - matcher: Bash\n"
        "argument-hint: '[cliente]'\n"
        "---" + body
    )
    env.publish(files={"SKILL.md": original})
    code, out = env.run()
    assert code == 0, out
    written = (env.root / "facturacion/SKILL.md").read_text(encoding="utf-8")
    meta = frontmatter(written)
    assert set(meta) == {"name", "description"}
    assert meta["name"] == "facturacion", "el nombre es el de la carpeta, que es el del comando"
    assert "Use this skill to invoice. Second line: with colon." in meta["description"]
    assert "Cuando pidan una factura" in meta["description"]
    assert written.endswith("---" + body)
    for dropped in ("allowed-tools", "hooks", "argument-hint"):
        assert dropped in out, f"se avisa de que se quita {dropped}"


@pytest.mark.parametrize(
    "content",
    ["# sin cabecera\n", "---\nname: x\n---\n# sin description\n", "---\nname: x\ndescription: ''\n---\n"],
)
def test_a_skill_md_without_description_is_not_installed(env: Env, content: str) -> None:
    env.publish(files={"SKILL.md": content})
    env.publish("buena")
    code, out = env.run()
    assert code == 12, out
    assert "description" in out
    assert not (env.root / "facturacion").exists()
    assert (env.root / "buena/SKILL.md").exists(), "el resto sí se instala"


def test_a_skill_that_asks_for_manual_invocation_only_is_not_installed(env: Env) -> None:
    env.publish(files={"SKILL.md": "---\nname: facturacion\ndescription: x\ndisable-model-invocation: true\n---\n#\n"})
    code, out = env.run()
    assert code == 0, out
    assert not (env.root / "facturacion").exists()
    assert "disable-model-invocation" in out
    assert "facturacion" not in env.control()["skills"] if (env.root / CONTROL).exists() else True


def test_a_managed_skill_that_becomes_manual_only_is_removed(env: Env) -> None:
    pkg = env.publish()
    assert env.run()[0] == 0
    pkg.files["SKILL.md"] = PkgFile(b"---\nname: facturacion\ndescription: x\ndisable-model-invocation: true\n---\n#\n")
    code, out = env.run()
    assert code == 0, out
    assert not (env.root / "facturacion").exists()


# --- scripts: nunca listos para ejecutarse ----------------------------------------------------


def test_scripts_and_executables_are_not_copied_and_the_user_is_told(env: Env) -> None:
    env.publish(
        files={
            "SKILL.md": "---\nname: facturacion\ndescription: x\n---\n# t\n",
            "scripts/run.sh": "#!/bin/sh\necho hola\n",
            "scripts/lib/util.py": "X = 1\n",
            "scripts/LEEME.md": "# datos dentro de scripts/\n",
            "otro.py": "print(1)\n",
            "bin/herramienta": "binario\n",
            "doc/disfrazado.txt": "#!/usr/bin/env python3\nprint(1)\n",
            "instalar.bat": "echo hola\n",
            "references/a.md": "# a\n",
            "templates/informe.docx": BINARY,
        },
        executable=["bin/herramienta"],
    )
    code, out = env.run()
    assert code == 0, out
    files = env.tree(env.root / "facturacion")
    assert sorted(files) == ["SKILL.md", "references/a.md", "templates/informe.docx"]
    for rel in (
        "scripts/run.sh",
        "scripts/lib/util.py",
        "scripts/LEEME.md",
        "otro.py",
        "bin/herramienta",
        "doc/disfrazado.txt",
        "instalar.bat",
    ):
        assert rel in out, f"{rel} debe figurar como omitido"
    assert "no funcionará completa" in out
    omitted = {o["path"] for o in env.control()["skills"]["facturacion"]["omitted"]}
    assert omitted == {
        "scripts/run.sh",
        "scripts/lib/util.py",
        "scripts/LEEME.md",
        "otro.py",
        "bin/herramienta",
        "doc/disfrazado.txt",
        "instalar.bat",
    }


@posix_only
def test_nothing_written_is_executable_and_modes_are_private(env: Env) -> None:
    env.publish(files={"SKILL.md": "---\nname: facturacion\ndescription: x\n---\n#\n", "references/a.md": "a"})
    assert env.run()[0] == 0
    for dirpath, dirs, files in os.walk(env.root / "facturacion"):
        for d in dirs:
            assert stat.S_IMODE((Path(dirpath) / d).stat().st_mode) == 0o700
        for f in files:
            assert stat.S_IMODE((Path(dirpath) / f).stat().st_mode) == 0o600
    assert stat.S_IMODE((env.root / "facturacion").stat().st_mode) == 0o700
    assert stat.S_IMODE((env.root / CONTROL).stat().st_mode) == 0o600
    assert stat.S_IMODE(env.root.stat().st_mode) == 0o700  # la creó el plugin


# --- nombres ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Mi_Skill", "con", "mcp", "skills", "a.b", "Upper"])
def test_names_antigravity_cannot_use_are_skipped_and_reported(env: Env, name: str) -> None:
    env.publish(name)
    env.publish("buena")
    code, out = env.run()
    assert code == 0, out
    assert name in out
    assert not (env.root / name).exists()
    assert (env.root / "buena/SKILL.md").exists()


def test_no_organization_prefix_so_the_skills_own_links_keep_working(env: Env) -> None:
    env.publish(
        "facturacion", files={"SKILL.md": "---\nname: facturacion\ndescription: x\n---\n[g](../gestor/SKILL.md)\n"}
    )
    env.publish("gestor")
    assert env.run()[0] == 0
    assert (env.root / "facturacion/../gestor/SKILL.md").exists()
    assert ":" not in "".join(p.name for p in env.root.iterdir()), "ningún nombre lleva `:` (Windows)"


# --- choques con lo del usuario -----------------------------------------------------------------


def test_a_users_skill_with_the_same_name_is_never_touched(env: Env) -> None:
    env.publish("facturacion")
    env.publish("gestor")
    mine = env.root / "facturacion"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("---\ndescription: la mia\n---\n")
    (mine / "extra.md").write_text("mio")
    before = env.tree(mine)
    code, out = env.run()
    assert code == 0, out
    assert env.tree(mine) == before
    assert "facturacion" in out
    assert "no la toco" in out
    assert (env.root / "gestor/SKILL.md").exists()
    assert "facturacion" not in env.control()["skills"], "lo ajeno no se registra como gestionado"
    again, out2 = env.run()
    assert again == 0, out2
    assert env.tree(mine) == before


@posix_only
def test_a_users_symlinked_skill_is_not_followed(env: Env, tmp_path: Path) -> None:
    env.publish("facturacion")
    outside = tmp_path / "fuera"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\ndescription: ajena\n---\n")
    env.root.mkdir(parents=True)
    (env.root / "facturacion").symlink_to(outside, target_is_directory=True)
    code, out = env.run()
    assert code == 0, out
    assert (env.root / "facturacion").is_symlink()
    assert sorted(p.name for p in outside.iterdir()) == ["SKILL.md"]
    assert (outside / "SKILL.md").read_text() == "---\ndescription: ajena\n---\n"


@posix_only
def test_a_symlinked_skills_root_is_written_through(env: Env, tmp_path: Path) -> None:
    env.publish()
    real = tmp_path / "dotfiles" / "skills"
    real.mkdir(parents=True)
    env.root.parent.mkdir(parents=True)
    env.root.symlink_to(real, target_is_directory=True)
    code, out = env.run()
    assert code == 0, out
    assert env.root.is_symlink()
    assert (real / "facturacion/SKILL.md").exists()


def test_a_managed_skill_edited_by_the_user_is_not_overwritten_or_removed_without_force(env: Env) -> None:
    pkg = env.publish()
    assert env.run()[0] == 0
    skill = env.root / "facturacion"
    (skill / "SKILL.md").write_text("---\ndescription: editada\n---\nmi version\n")
    (skill / "mio.md").write_text("mio")
    pkg.files["SKILL.md"] = PkgFile(b"---\nname: facturacion\ndescription: nueva\n---\n#\n")
    code, out = env.run()
    assert code == 0, out
    assert "modificada" in out
    assert (skill / "SKILL.md").read_text() == "---\ndescription: editada\n---\nmi version\n"
    assert (skill / "mio.md").exists()
    env.server.state.skills["demo"]["facturacion"].roles = ["otro-rol"]
    code, out = env.run()
    assert code == 0, out
    assert (skill / "mio.md").exists(), "tampoco se retira si el usuario la modificó"
    code, out = env.run("--force")
    assert code == 0, out
    assert not skill.exists()


def test_force_overwrites_a_modified_managed_skill_but_never_a_foreign_one(env: Env) -> None:
    pkg = env.publish()
    env.publish("gestor")
    assert env.run()[0] == 0
    (env.root / "facturacion/SKILL.md").write_text("editada")
    mine = env.root / "propia"
    mine.mkdir()
    (mine / "SKILL.md").write_text("propia")
    env.publish("propia")
    pkg.files["SKILL.md"] = PkgFile(b"---\nname: facturacion\ndescription: nueva\n---\n#\n")
    code, out = env.run("--force")
    assert code == 0, out
    assert frontmatter((env.root / "facturacion/SKILL.md").read_text())["description"] == "nueva"
    assert (mine / "SKILL.md").read_text() == "propia"


@posix_only
def test_a_symlink_inside_a_managed_skill_counts_as_a_modification_and_is_never_followed(
    env: Env, tmp_path: Path
) -> None:
    pkg = env.publish()
    assert env.run()[0] == 0
    outside = tmp_path / "secreto.txt"
    outside.write_text("no tocar")
    (env.root / "facturacion/enlace").symlink_to(outside)
    pkg.files["SKILL.md"] = PkgFile(b"---\nname: facturacion\ndescription: nueva\n---\n#\n")
    code, out = env.run()
    assert code == 0, out
    assert (env.root / "facturacion/enlace").is_symlink()
    code, out = env.run("--force")
    assert code == 0, out
    assert not (env.root / "facturacion/enlace").exists()
    assert outside.read_text() == "no tocar"


# --- integridad y confinamiento -----------------------------------------------------------------


@pytest.mark.parametrize("bad", ["../escape.txt", "a/../../escape.txt", "/etc/passwd", "a\\b", "a//b", "./x"])
def test_a_path_outside_the_skill_in_the_manifest_installs_nothing_of_it(env: Env, bad: str, tmp_path: Path) -> None:
    pkg = env.publish()
    pkg.files[bad] = PkgFile(b"x")
    env.publish("buena")
    code, out = env.run()
    assert code == 12, out
    assert "facturacion" in out
    assert not (env.root / "facturacion").exists()
    assert not (env.root.parent / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert (env.root / "buena/SKILL.md").exists()


def test_a_wrong_hash_keeps_the_previous_version(env: Env) -> None:
    pkg = env.publish(files={"SKILL.md": "---\nname: facturacion\ndescription: v1\n---\n#\n", "a.md": "a"})
    assert env.run()[0] == 0
    before = env.tree()
    pkg.files["a.md"] = PkgFile(b"cambiado", sha256_override="0" * 64)
    code, out = env.run()
    assert code == 12, out
    assert "SHA-256" in out
    assert env.tree() == before
    assert [p.name for p in env.root.iterdir() if p.name.startswith(".baas-tmp")] == []


def test_a_wrong_hash_on_a_new_skill_leaves_nothing(env: Env) -> None:
    pkg = env.publish()
    pkg.files["SKILL.md"] = PkgFile(b"hola", sha256_override="1" * 64)
    code, out = env.run()
    assert code == 12, out
    assert not (env.root / "facturacion").exists()


def test_a_package_without_skill_md_is_not_installed(env: Env) -> None:
    pkg = env.publish()
    del pkg.files["SKILL.md"]
    pkg.files["notas.md"] = PkgFile(b"x")
    code, out = env.run()
    assert code == 12, out
    assert "SKILL.md" in out
    assert not (env.root / "facturacion").exists()


def test_a_corrupt_control_file_aborts_without_touching_anything(env: Env) -> None:
    env.publish()
    env.root.mkdir(parents=True)
    (env.root / CONTROL).write_text("{no es json")
    (env.root / "facturacion").mkdir()
    (env.root / "facturacion/SKILL.md").write_text("mia")
    code, out = env.run()
    assert code == 13, out
    assert (env.root / CONTROL).read_text() == "{no es json"
    assert (env.root / "facturacion/SKILL.md").read_text() == "mia"


# --- dependencias -------------------------------------------------------------------------------


def test_dependencies_the_user_cannot_read_are_warned_and_not_granted(env: Env) -> None:
    env.publish("principal", depends_on=["secreta"])
    env.publish("secreta", roles=["otro-rol"])
    code, out = env.run()
    assert code == 0, out
    assert "secreta" in out
    assert (env.root / "principal/SKILL.md").exists()
    assert not (env.root / "secreta").exists()


# --- modo de ensayo, red y organización ----------------------------------------------------------


def test_dry_run_writes_nothing_and_shows_what_would_happen(env: Env) -> None:
    env.publish(files={"SKILL.md": "---\nname: facturacion\ndescription: x\n---\n#\n", "scripts/a.sh": "echo"})
    code, out = env.run("--dry-run")
    assert code == 0, out
    assert "Simulación" in out
    assert "+ facturacion" in out
    assert not (env.home / ".gemini").exists()


def test_quiet_fails_open_when_the_mcp_is_down_and_touches_nothing(env: Env) -> None:
    env.publish()
    assert env.run()[0] == 0
    before = env.tree()
    env.server.state.mcp_status = 503
    code, _ = env.run("--quiet")
    assert code == 0
    assert env.tree() == before
    code, _ = env.run()
    assert code != 0
    assert env.tree() == before


def test_several_organizations_need_an_explicit_choice_and_nothing_is_written(env: Env) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("multi@demo.test")})
    env.publish("de-demo")
    env.publish("de-otra", org="otra")
    code, out = env.run()
    assert code == 3, out
    assert "--org" in out
    assert not (env.home / ".gemini").exists()
    code, out = env.run("--org", "otra")
    assert code == 0, out
    assert (env.root / "de-otra/SKILL.md").exists()
    assert not (env.root / "de-demo").exists()
    assert env.control()["org"] == "otra"


def test_the_organization_of_the_global_mcp_entry_is_the_default(env: Env) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("multi@demo.test")})
    env.publish("de-demo")
    env.publish("de-otra", org="otra")
    config = env.home / ".gemini" / "config" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"baas": {"serverUrl": f"{env.server.url}/org/otra/mcp"}}}))
    code, out = env.run()
    assert code == 0, out
    assert (env.root / "de-otra/SKILL.md").exists()


def test_switching_organization_retires_the_previous_organizations_skills(env: Env) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("multi@demo.test")})
    env.publish("de-demo")
    env.publish("de-otra", org="otra")
    assert env.run("--org", "demo")[0] == 0
    code, out = env.run("--org", "otra")
    assert code == 0, out
    assert not (env.root / "de-demo").exists()
    assert (env.root / "de-otra/SKILL.md").exists()


def test_an_explicit_organization_different_from_the_global_mcp_entry_is_warned(env: Env) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": dev_api_key("multi@demo.test")})
    env.publish("de-otra", org="otra")
    config = env.home / ".gemini" / "config" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"baas": {"serverUrl": f"{env.server.url}/org/demo/mcp"}}}))
    code, out = env.run("--org", "otra")
    assert code == 0, out
    assert "AVISO" in out
    assert "MCP global" in out


def test_without_key_nothing_is_written(env: Env) -> None:
    envfile.remove_env_keys(envfile.BaasPaths.default(), ["BAAS_API_KEY"])
    code, out = env.run()
    assert code == 2, out
    assert not (env.home / ".gemini").exists()


# --- lo único que toca ----------------------------------------------------------------------------


def test_only_the_skills_folder_is_written_and_the_key_is_never_printed(env: Env) -> None:
    env.publish(files={"SKILL.md": "---\nname: facturacion\ndescription: x\n---\n#\n", "scripts/a.sh": "echo"})
    snapshot = env.tree(env.project)
    code, out = env.run()
    assert code == 0, out
    assert env.key not in out
    assert env.tree(env.project) == snapshot, "no escribe nada en el proyecto"
    touched = {p.name for p in env.home.iterdir()}
    assert touched <= {".gemini", "baas-ai-ip"}
    gemini = sorted(str(p.relative_to(env.home / ".gemini")) for p in (env.home / ".gemini").rglob("*"))
    assert all(g.startswith("config") for g in gemini)
    assert not (env.home / ".gemini/config/mcp_config.json").exists(), "las skills no tocan la configuración del MCP"
    for p in (env.home / ".gemini").rglob("*"):
        if p.is_file():
            assert env.key.encode() not in p.read_bytes()


def test_the_claude_code_sync_is_unaffected(env: Env) -> None:
    code = cli.main(["sync", "--project", str(env.project)])
    out = env.capsys.readouterr()
    assert code == 2, "sin vincular sigue pidiendo /baas-init"
    assert "baas-init" in out.out + out.err


def test_an_unknown_tool_is_rejected_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        cli.main(["sync", "--tool", "no-existe"])


def test_setup_antigravity_points_to_the_skills_command(env: Env, capsys) -> None:
    code = cli.main(["setup", "antigravity", "--project", str(env.project)])
    out = capsys.readouterr()
    assert code == 0
    assert "sync --tool antigravity" in out.out


@posix_only
def test_a_managed_skill_replaced_by_a_symlink_is_not_followed_or_replaced(env: Env, tmp_path: Path) -> None:
    pkg = env.publish()
    assert env.run()[0] == 0
    moved = tmp_path / "movida"
    (env.root / "facturacion").rename(moved)
    (env.root / "facturacion").symlink_to(moved, target_is_directory=True)  # mismo contenido, pero es un enlace
    before = env.tree(moved)
    pkg.files["SKILL.md"] = PkgFile(b"---\nname: facturacion\ndescription: nueva\n---\n#\n")
    code, out = env.run()
    assert code == 0, out
    assert "enlace simbólico" in out
    assert (env.root / "facturacion").is_symlink()
    assert env.tree(moved) == before
