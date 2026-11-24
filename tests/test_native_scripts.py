"""Scripts y rutas de las skills nativas (T20 y T22): `sync --tool antigravity` copia los scripts verificados y
reescribe el `SKILL.md` para usarlos desde la carpeta de la skill.

Como Claude Code, los scripts se verifican (SHA-256) al sincronizar y se ejecutan desde la carpeta de la skill. Las
llamadas del `SKILL.md` (`${CLAUDE_SKILL_DIR}/scripts/x`, `python3 scripts/x` «desde la carpeta de la skill») pasan a la
ruta absoluta de esa carpeta, bien entrecomillada, y los enlaces y citas a otras skills de la organización, al nombre
con prefijo. Todo contra el BaaS falso, con `HOME` temporal; las órdenes reescritas se ejecutan de verdad.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fake_baas import FakeBaasServer, dev_api_key

from baas_plugin import cli, envfile, native_skills

SCRIPT = """\
import json, os, sys
with open(os.environ["OUT_FILE"], "w") as fh:
    json.dump({"args": sys.argv[1:], "cwd": os.getcwd(), "file": __file__}, fh)
"""
GESTOR_MD = """---
name: gestor
description: Gestor fiscal
allowed-tools: Bash(python3 *)
---

# Gestor

Trabaja con [`/facturacion`](../facturacion/SKILL.md) y lee [el libro](../../../knowledge/fiscal/ledger.json).
Para el resto, la skill `/gestor` y la `/otra-que-no-existe`.

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/hola.py" list --name "Ana María" --year 2026
python3 "${CLAUDE_SKILL_DIR}/scripts/hola.py" audit | head
```

Mención: `${CLAUDE_SKILL_DIR}/scripts/hola.py` y una ruta relativa `python3 scripts/otro.py`.
Ejemplo ajeno en un bloque: ver `../facturacion/SKILL.md`.

```markdown
[no tocar](../facturacion/SKILL.md) y `/gestor`
```
"""
FACTURACION_MD = "---\nname: facturacion\ndescription: Facturas\n---\n\nVuelve a [`/gestor`](../gestor/SKILL.md).\n"
CONTADOR_MD = (
    "---\nname: contador\ndescription: Cuenta\n---\n\n# Contador\n\n"
    "Ejecuta el script desde la carpeta de la skill: `python3 scripts/hola.py --inc`\n"
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

    def publish(self, name: str, md: str, org: str = "demo", **files: str | bytes):
        return self.server.state.add_skill(org, name, {"SKILL.md": md, **files}, roles=["analista"])

    def run(self, *args: str) -> tuple[int, str]:
        code = cli.main(["sync", "--tool", "antigravity", "--project", str(self.project), *args])
        out = self.capsys.readouterr()
        return code, out.out + out.err

    def skill_md(self, name: str = "gestor", org: str = "demo") -> str:
        return (self.root / f"{org}-{name}" / "SKILL.md").read_text(encoding="utf-8")

    def skill_dir(self, name: str = "gestor", org: str = "demo") -> str:
        return str(self.root / f"{org}-{name}")

    def control(self, org: str = "demo") -> dict:
        return json.loads((self.root / f".baas-skills-{org}.json").read_text(encoding="utf-8"))

    def execute(self, command: str, monkeypatch) -> dict:
        """Ejecuta de verdad una orden reescrita (con el intérprete de la prueba) y devuelve lo que anotó el script."""
        tokens = shlex.split(command)
        assert tokens[0] in ("python3", "python"), command
        out = self.project / "salida.json"
        out.unlink(missing_ok=True)
        subprocess.run(
            [sys.executable, *tokens[1:]], cwd=self.project, env={**os.environ, "OUT_FILE": str(out)}, check=True
        )
        return json.loads(out.read_text())


def make_env(home: Path, project: Path, capsys, server: FakeBaasServer) -> Env:
    for skills in server.state.skills.values():
        skills.clear()
    key = dev_api_key("analista@demo.test")
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": key, "BAAS_APP_URL": server.url})
    return Env(server, home, project, capsys)


@pytest.fixture
def env(home, project, capsys) -> Iterator[Env]:
    with FakeBaasServer() as server:
        yield make_env(home, project, capsys, server)


def publish_pair(env: Env) -> None:
    env.publish("gestor", GESTOR_MD, **{"scripts/hola.py": SCRIPT})
    env.publish("facturacion", FACTURACION_MD)


def first_command(md: str, containing: str) -> str:
    return next(ln for ln in md.splitlines() if containing in ln)


# --- scripts: copiados, verificados y llamados desde la carpeta de la skill ----------------------------------


def test_the_scripts_are_copied_and_the_calls_use_the_absolute_folder_of_the_skill(env: Env) -> None:
    publish_pair(env)
    code, out = env.run()
    assert code == 0, out
    folder = env.skill_dir()
    md = env.skill_md()
    assert f'python3 "{folder}/scripts/hola.py" list --name "Ana María" --year 2026' in md
    assert f'python3 "{folder}/scripts/hola.py" audit | head' in md
    assert (env.root / "demo-gestor/scripts/hola.py").read_text() == SCRIPT, "el script llega tal cual, ya verificado"
    assert "${CLAUDE_SKILL_DIR}" not in md.replace("Mención:", "").split("Mención")[0]


def test_the_rewritten_command_really_runs_the_script_with_its_arguments(env: Env, monkeypatch) -> None:
    publish_pair(env)
    assert env.run()[0] == 0
    result = env.execute(first_command(env.skill_md(), "Ana María"), monkeypatch)
    assert result["args"] == ["list", "--name", "Ana María", "--year", "2026"]
    assert Path(result["cwd"]).resolve() == env.project.resolve()
    assert Path(result["file"]).resolve() == (env.root / "demo-gestor/scripts/hola.py").resolve()


def test_a_home_with_spaces_and_parentheses_still_gives_working_commands(
    tmp_path, project, capsys, monkeypatch
) -> None:
    home = tmp_path / "mi casa (x)"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BAAS_CLAUDE_HOME", str(home / ".claude"))
    with FakeBaasServer() as server:
        env = make_env(home, project, capsys, server)
        md = GESTOR_MD.replace(
            'python3 "${CLAUDE_SKILL_DIR}/scripts/hola.py" audit', "python3 ${CLAUDE_SKILL_DIR}/scripts/hola.py audit"
        )
        env.publish("gestor", md, **{"scripts/hola.py": SCRIPT})
        env.publish("facturacion", FACTURACION_MD)
        code, out = env.run()
        assert code == 0, out
        text = env.skill_md()
        assert f'python3 "{env.skill_dir()}/scripts/hola.py" audit | head' in text, "sin comillas originales: se añaden"
        result = env.execute(first_command(text, "Ana María"), monkeypatch)
        assert result["args"][0] == "list"
        assert result["file"].endswith("mi casa (x)/.gemini/config/skills/demo-gestor/scripts/hola.py")


def test_a_relative_call_from_the_skill_folder_like_the_one_in_contador_is_resolved(env: Env, monkeypatch) -> None:
    env.publish("contador", CONTADOR_MD, **{"scripts/hola.py": SCRIPT})
    code, out = env.run()
    assert code == 0, out
    md = env.skill_md("contador")
    span = re.search(r"`(python3 [^`]+)`", md)
    assert span, md
    assert span.group(1) == f'python3 "{env.skill_dir("contador")}/scripts/hola.py" --inc'
    assert env.execute(span.group(1), monkeypatch)["args"] == ["--inc"]
    assert "ruta relativa" not in out, "ya no queda nada sin resolver"


def test_cd_to_the_skill_folder_makes_the_relative_call_certain(env: Env) -> None:
    md = '---\nname: c\ndescription: x\n---\n\n```bash\ncd "${CLAUDE_SKILL_DIR}" && python3 scripts/hola.py a\n```\n'
    env.publish("c", md, **{"scripts/hola.py": SCRIPT})
    assert env.run()[0] == 0
    folder = env.skill_dir("c")
    assert f'cd {folder} && python3 "{folder}/scripts/hola.py" a' in env.skill_md("c") or (
        f'cd "{folder}" && python3 "{folder}/scripts/hola.py" a' in env.skill_md("c")
    )


def test_what_is_not_recognised_is_left_intact_and_counted_in_the_output(env: Env) -> None:
    md = (
        "---\nname: gestor\ndescription: x\n---\n\n"
        "Sin pista: `python3 scripts/hola.py x`.\n\n```bash\npython3 '${CLAUDE_SKILL_DIR}/scripts/hola.py' y\n```\n"
    )
    env.publish("gestor", md, **{"scripts/hola.py": SCRIPT})
    code, out = env.run()
    assert code == 0, out
    text = env.skill_md()
    assert "`python3 scripts/hola.py x`" in text
    assert "'${CLAUDE_SKILL_DIR}/scripts/hola.py'" in text
    assert "1 llamada(s) a scripts con ruta relativa" in out
    assert "1 referencia(s) a `${CLAUDE_SKILL_DIR}` sin reescribir" in out
    assert "«demo-gestor»" in out


def test_the_output_says_how_many_calls_were_resolved(env: Env) -> None:
    publish_pair(env)
    code, out = env.run()
    assert code == 0, out
    assert "2 llamada(s) a scripts → carpeta de la skill" in out
    assert "no se copian" not in out
    assert "run-script" not in out


def test_the_note_of_a_skill_with_scripts_says_where_they_are_and_forbids_running_copies(env: Env) -> None:
    publish_pair(env)
    env.publish("solo-texto", "---\nname: solo-texto\ndescription: x\n---\nhola\n")
    assert env.run()[0] == 0
    note = env.skill_md().split("\n---\n", 1)[1].split("# Gestor")[0]
    assert f"`{env.skill_dir()}`" in note
    assert "verificados con su SHA-256 al sincronizar" in note
    assert "No copies el contenido de `baas_get_script`" in note
    assert "baas.py sync --tool antigravity" in note
    assert "knowledge" in note, "algunos scripts leen ficheros locales: hace falta el cerebro como carpeta de trabajo"
    assert "run-script" not in note
    assert "scripts de esta skill" not in env.skill_md("solo-texto")


def test_a_skill_whose_text_has_no_recognised_call_still_gets_the_scripts_note(env: Env) -> None:
    env.publish("lector", "---\nname: lector\ndescription: x\n---\nsin llamadas\n", **{"scripts/hola.py": SCRIPT})
    assert env.run()[0] == 0
    assert "Los scripts de esta skill están en su carpeta" in env.skill_md("lector")
    assert (env.root / "demo-lector/scripts/hola.py").exists()


def test_the_original_and_the_adapted_hashes_and_the_calls_are_recorded(env: Env) -> None:
    publish_pair(env)
    pkg = env.server.state.skills["demo"]["gestor"]
    assert env.run()[0] == 0
    entry = env.control()["skills"]["gestor"]
    written = (env.root / "demo-gestor/SKILL.md").read_bytes()
    import hashlib

    assert entry["original_sha256"] == pkg.files["SKILL.md"].sha256
    assert {f["path"]: f["sha256"] for f in entry["files"]}["SKILL.md"] == hashlib.sha256(written).hexdigest()
    assert entry["original_sha256"] != hashlib.sha256(written).hexdigest()
    assert entry["calls"] == [["python3", "scripts/hola.py"], ["python3", "scripts/hola.py"]]
    assert entry["needs_dir"] is True


def test_windows_gets_python_instead_of_python3_in_the_recognised_calls(env: Env, monkeypatch) -> None:
    monkeypatch.setattr(native_skills, "_windows", lambda: True)
    publish_pair(env)
    assert env.run()[0] == 0
    md = env.skill_md()
    assert f'python "{env.skill_dir()}/scripts/hola.py" list' in md
    assert env.control()["skills"]["gestor"]["calls"][0] == ["python", "scripts/hola.py"]


def test_an_unsafe_home_leaves_the_variable_alone_and_says_so(tmp_path, project, capsys, monkeypatch) -> None:
    home = tmp_path / "a$b"  # un `$` en la ruta: un shell lo reinterpretaría
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BAAS_CLAUDE_HOME", str(home / ".claude"))
    with FakeBaasServer() as server:
        env = make_env(home, project, capsys, server)
        publish_pair(env)
        code, out = env.run()
        assert code == 0, out
        assert 'python3 "${CLAUDE_SKILL_DIR}/scripts/hola.py" list' in env.skill_md()
        assert "caracteres que un shell reinterpretaría" in out


# --- enlaces y citas ----------------------------------------------------------------------------------------


def test_links_and_citations_to_synced_skills_get_the_prefix_and_the_rest_is_untouched(env: Env) -> None:
    publish_pair(env)
    assert env.run()[0] == 0
    md = env.skill_md()
    assert "[`/demo-facturacion`](../demo-facturacion/SKILL.md)" in md
    assert "la skill `/demo-gestor` y la `/otra-que-no-existe`" in md
    assert "(../../../knowledge/fiscal/ledger.json)" in md, "los ficheros del cerebro no se reescriben"
    assert "ver `../facturacion/SKILL.md`" in md, "dentro de un trozo de código es un ejemplo"
    assert "[no tocar](../facturacion/SKILL.md) y `/gestor`" in md, "ni dentro de un bloque de código"
    assert "../demo-gestor/SKILL.md" in env.skill_md("facturacion")


def test_links_to_a_skill_that_is_not_synced_are_left_alone_until_it_is(env: Env) -> None:
    env.publish("gestor", GESTOR_MD, **{"scripts/hola.py": SCRIPT})
    code, out = env.run()
    assert code == 0, out
    assert "(../facturacion/SKILL.md)" in env.skill_md()
    env.publish("facturacion", FACTURACION_MD)  # ahora el usuario también tiene `facturacion`
    code, out = env.run()
    assert code == 0, out
    assert "~ demo-gestor" in out, "`gestor` se vuelve a adaptar porque ya puede enlazar a `facturacion`"
    assert "[`/demo-facturacion`](../demo-facturacion/SKILL.md)" in env.skill_md()


def test_a_skill_that_does_not_mention_the_new_skill_is_not_downloaded_again(env: Env) -> None:
    env.publish("a", "---\nname: a\ndescription: x\n---\nsin enlaces\n")
    assert env.run()[0] == 0
    env.publish("b", "---\nname: b\ndescription: x\n---\nhola\n")
    env.server.state.requests.clear()
    code, out = env.run()
    assert code == 0, out
    assert "+ demo-b" in out
    assert "~ demo-a" not in out
    assert [c for c in env.server.state.tool_calls("baas_get_skill_package") if c[1]["skill"] == "a"] == []


# --- idempotencia, cambio de carpeta y migración -------------------------------------------------------------------


def test_a_second_sync_changes_nothing_and_downloads_nothing(env: Env) -> None:
    publish_pair(env)
    assert env.run()[0] == 0
    files = {p: p.read_bytes() for p in env.root.rglob("*") if p.is_file()}
    control = env.root / ".baas-skills-demo.json"
    stamp = (control.stat().st_ino, control.stat().st_mtime_ns)
    env.server.state.requests.clear()
    code, out = env.run()
    assert code == 0, out
    assert "Todo al día" in out
    assert {p: p.read_bytes() for p in env.root.rglob("*") if p.is_file()} == files
    assert (control.stat().st_ino, control.stat().st_mtime_ns) == stamp
    assert env.server.state.tool_calls("baas_get_skill_package") == []


@pytest.mark.skipif(os.name != "posix", reason="enlaces simbólicos")
def test_moving_the_skills_folder_changes_the_absolute_paths_and_the_next_sync_updates_only_those_that_use_them(
    env: Env, tmp_path
) -> None:
    publish_pair(env)
    env.publish("solo-texto", "---\nname: solo-texto\ndescription: x\n---\nhola\n")
    env.publish("lector", "---\nname: lector\ndescription: x\n---\nsin llamadas\n", **{"scripts/hola.py": SCRIPT})
    real1 = tmp_path / "dotfiles1" / "skills"
    real1.parent.mkdir()
    env.root.parent.mkdir(parents=True)
    real1.mkdir()
    env.root.symlink_to(real1, target_is_directory=True)
    assert env.run()[0] == 0
    assert str(real1) in env.skill_md()
    real2 = tmp_path / "dotfiles2" / "skills"  # el usuario mueve sus dotfiles
    real2.parent.mkdir()
    real1.rename(real2)
    env.root.unlink()
    env.root.symlink_to(real2, target_is_directory=True)
    env.server.state.requests.clear()
    code, out = env.run()
    assert code == 0, out
    assert "~ demo-gestor" in out
    assert str(real2) in env.skill_md()
    assert str(real1) not in env.skill_md()
    assert "~ demo-solo-texto" not in out
    assert "~ demo-lector" in out, "trae scripts: su nota lleva la ruta de la carpeta aunque el texto no la use"
    assert str(real2) in env.skill_md("lector")
    assert [c for c in env.server.state.tool_calls("baas_get_skill_package") if c[1]["skill"] == "solo-texto"] == []
    assert "Todo al día" in env.run()[1]


T20_RUN_SCRIPT = (
    'python3 "/viejo/plugin/scripts/baas.py" run-script --tool antigravity --synced-org demo '
    "skills/gestor/scripts/hola.py -- list"
)


def t20_state(env: Env) -> None:
    """El estado que dejaban T19 y T20: sin scripts en disco, con la orden `run-script` y un control sin `calls`."""
    import hashlib

    skill = env.root / "demo-gestor"
    skill.mkdir(parents=True)
    md = f"---\nname: demo-gestor\ndescription: Gestor fiscal\n---\n\n> nota vieja\n\n```bash\n{T20_RUN_SCRIPT}\n```\n"
    (skill / "SKILL.md").write_text(md)
    pkg = env.server.state.skills["demo"]["gestor"]
    control = {
        "managed_by": "baas-llm-plugin",
        "format": 1,
        "org": "demo",
        "server": f"{env.server.url}/org/demo/mcp",
        "synced_at": "2026-11-24T10:00:00Z",
        "adapter": 1,
        "baas_py": "/viejo/plugin/scripts/baas.py",
        "skills": {
            "gestor": {
                "package_sha256": pkg.package_sha256,
                "depends_on": [],
                "files": [{"path": "SKILL.md", "sha256": hashlib.sha256(md.encode()).hexdigest()}],
                "omitted": [{"path": "scripts/hola.py", "reason": "carpeta scripts/"}],
                "original_sha256": pkg.files["SKILL.md"].sha256,
                "refs": [],
                "runner": True,
                "adapt_fp": "0123456789abcdef",
            }
        },
        "warnings": [],
    }
    (env.root / ".baas-skills-demo.json").write_text(json.dumps(control))


def test_a_control_file_from_t20_is_migrated_the_scripts_are_downloaded_and_the_text_is_rewritten(env: Env) -> None:
    env.publish("gestor", GESTOR_MD, **{"scripts/hola.py": SCRIPT})
    t20_state(env)
    code, out = env.run()
    assert code == 0, out
    assert "~ demo-gestor" in out
    assert (env.root / "demo-gestor/scripts/hola.py").read_text() == SCRIPT
    assert "run-script" not in env.skill_md()
    assert f'python3 "{env.skill_dir()}/scripts/hola.py" list' in env.skill_md()
    control = env.control()
    assert "baas_py" not in control
    assert control["adapter"] == native_skills.ADAPTER_VERSION
    entry = control["skills"]["gestor"]
    assert "omitted" not in entry
    assert "runner" not in entry
    assert "scripts/hola.py" in {f["path"] for f in entry["files"]}


def test_a_skill_the_user_edited_after_t20_is_not_migrated_over_their_changes_without_force(env: Env) -> None:
    env.publish("gestor", GESTOR_MD, **{"scripts/hola.py": SCRIPT})
    t20_state(env)
    (env.root / "demo-gestor/SKILL.md").write_text("editada por el usuario\n")
    code, out = env.run()
    assert code == 0, out
    assert env.skill_md() == "editada por el usuario\n"
    assert "modificada" in out
    assert not (env.root / "demo-gestor/scripts").exists()
    assert env.run("--force")[0] == 0
    assert (env.root / "demo-gestor/scripts/hola.py").exists()


def test_a_stale_skill_shows_up_in_a_dry_run_without_writing(env: Env) -> None:
    env.publish("gestor", GESTOR_MD, **{"scripts/hola.py": SCRIPT})
    t20_state(env)
    before = env.skill_md()
    code, out = env.run("--dry-run")
    assert code == 0, out
    assert "~ demo-gestor" in out
    assert env.skill_md() == before
    assert not (env.root / "demo-gestor/scripts").exists()


def test_the_adapted_text_has_no_claude_skill_dir_in_what_was_recognised(env: Env) -> None:
    md = (
        "---\nname: gestor\ndescription: x\n---\n```bash\n"
        'python3 "${CLAUDE_SKILL_DIR}/scripts/hola.py" a\n"$CLAUDE_SKILL_DIR/scripts/hola.sh" b\n```\n'
    )
    env.publish("gestor", md, **{"scripts/hola.py": SCRIPT, "scripts/hola.sh": "#!/bin/sh\n"})
    assert env.run()[0] == 0
    assert "CLAUDE_SKILL_DIR" not in env.skill_md()
