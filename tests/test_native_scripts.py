"""Scripts y rutas de las skills nativas (T20 · #194): `sync --tool antigravity` reescribe el `SKILL.md`.

Las llamadas a los scripts de la skill pasan a `baas.py run-script` (que comprueba el SHA-256 y no deja el script en
disco), y los enlaces y citas a otras skills de la misma organización, a los nombres con prefijo. Todo contra el BaaS
falso, con `HOME` temporal.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fake_baas import FakeBaasServer, dev_api_key

from baas_plugin import cli, envfile, native_skills

SCRIPT = """\
import json, os, sys
with open(os.environ["OUT_FILE"], "w") as fh:
    json.dump({"args": sys.argv[1:], "cwd": os.getcwd(), "skill_dir": os.environ.get("CLAUDE_SKILL_DIR")}, fh)
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

Mención suelta: `${CLAUDE_SKILL_DIR}/scripts/hola.py` y una ruta relativa `python3 scripts/otro.py`.
Ejemplo ajeno en un bloque: ver `../facturacion/SKILL.md`.

```markdown
[no tocar](../facturacion/SKILL.md) y `/gestor`
```
"""
FACTURACION_MD = "---\nname: facturacion\ndescription: Facturas\n---\n\nVuelve a [`/gestor`](../gestor/SKILL.md).\n"


@dataclass
class Env:
    server: FakeBaasServer
    home: Path
    project: Path
    capsys: pytest.CaptureFixture[str]
    baas_py: Path

    @property
    def root(self) -> Path:
        return self.home / ".gemini" / "config" / "skills"

    def publish(self, name: str, md: str, org: str = "demo", **files: str | bytes):
        content = {"SKILL.md": md, **files}
        return self.server.state.add_skill(org, name, content, roles=["analista"])

    def run(self, *args: str) -> tuple[int, str]:
        code = cli.main(["sync", "--tool", "antigravity", "--project", str(self.project), *args])
        out = self.capsys.readouterr()
        return code, out.out + out.err

    def skill_md(self, name: str = "gestor", org: str = "demo") -> str:
        return (self.root / f"{org}-{name}" / "SKILL.md").read_text(encoding="utf-8")

    def control(self, org: str = "demo") -> dict:
        return json.loads((self.root / f".baas-skills-{org}.json").read_text(encoding="utf-8"))


@pytest.fixture
def env(home, project, capsys, monkeypatch, tmp_path) -> Iterator[Env]:
    with FakeBaasServer() as server:
        for skills in server.state.skills.values():
            skills.clear()
        key = dev_api_key("analista@demo.test")
        envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": key, "BAAS_APP_URL": server.url})
        baas_py = tmp_path / "plugin dir (x)" / "scripts" / "baas.py"  # con espacios y paréntesis
        monkeypatch.setattr(native_skills, "BAAS_PY", baas_py)
        monkeypatch.setattr(native_skills, "python_command", lambda: "python3")
        yield Env(server, home, project, capsys, baas_py)


def publish_pair(env: Env) -> None:
    env.publish("gestor", GESTOR_MD, **{"scripts/hola.py": SCRIPT})
    env.publish("facturacion", FACTURACION_MD)


def runner_line(env: Env, args: str) -> str:
    target = "skills/gestor/scripts/hola.py"
    return f'python3 "{env.baas_py}" run-script --tool antigravity --synced-org demo {target} -- {args}'


# --- scripts --------------------------------------------------------------------------------------------


def test_script_calls_become_run_script_and_the_script_is_still_not_copied(env: Env) -> None:
    publish_pair(env)
    code, out = env.run()
    assert code == 0, out
    md = env.skill_md()
    assert runner_line(env, 'list --name "Ana María" --year 2026') in md
    assert runner_line(env, "audit | head") in md
    assert (env.root / "demo-gestor/scripts").exists() is False, "el script no se copia a la carpeta de la skill"
    assert sorted(p.name for p in (env.root / "demo-gestor").iterdir()) == ["SKILL.md"]


def test_the_rewritten_command_really_runs_the_script_with_its_arguments(env: Env, monkeypatch) -> None:
    publish_pair(env)
    assert env.run()[0] == 0
    line = next(ln for ln in env.skill_md().splitlines() if "Ana María" in ln)
    tokens = shlex.split(line)
    assert tokens[:2] == ["python3", str(env.baas_py)]
    monkeypatch.chdir(env.project)
    monkeypatch.setenv("OUT_FILE", str(env.project / "salida.json"))
    code = cli.main(tokens[2:])  # lo que ejecutaría el agente tras `python3 <baas.py>`
    assert code == 0, env.capsys.readouterr()
    result = json.loads((env.project / "salida.json").read_text())
    assert result["args"] == ["list", "--name", "Ana María", "--year", "2026"]
    assert Path(result["cwd"]).resolve() == env.project.resolve()


def test_what_is_not_recognised_is_left_intact_and_counted_in_the_output(env: Env) -> None:
    publish_pair(env)
    code, out = env.run()
    assert code == 0, out
    md = env.skill_md()
    assert "`${CLAUDE_SKILL_DIR}/scripts/hola.py`" in md, "una mención suelta no es una orden"
    assert "`python3 scripts/otro.py`" in md
    assert "referencia(s) a `${CLAUDE_SKILL_DIR}` sin reescribir" in out
    assert "con ruta relativa" in out
    assert "«demo-gestor»" in out


def test_the_output_says_how_many_calls_were_rewritten(env: Env) -> None:
    publish_pair(env)
    code, out = env.run()
    assert code == 0, out
    assert "2 llamada(s) a scripts → run-script" in out


def test_a_call_to_a_script_that_is_not_in_the_package_stays_as_it_was(env: Env) -> None:
    md = GESTOR_MD.replace('scripts/hola.py" list', 'scripts/no-existe.py" list')
    env.publish("gestor", md, **{"scripts/hola.py": SCRIPT})
    code, out = env.run()
    assert code == 0, out
    assert 'python3 "${CLAUDE_SKILL_DIR}/scripts/no-existe.py" list' in env.skill_md()


def test_an_unsafe_baas_py_path_leaves_the_calls_alone_and_says_so(env: Env, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(native_skills, "BAAS_PY", tmp_path / 'raro"; id; "' / "baas.py")
    publish_pair(env)
    code, out = env.run()
    assert code == 0, out
    assert 'python3 "${CLAUDE_SKILL_DIR}/scripts/hola.py" list' in env.skill_md()
    assert "ruta de baas.py" in out


def test_the_original_and_the_adapted_hashes_are_recorded(env: Env) -> None:
    publish_pair(env)
    pkg = env.server.state.skills["demo"]["gestor"]
    assert env.run()[0] == 0
    entry = env.control()["skills"]["gestor"]
    written = (env.root / "demo-gestor/SKILL.md").read_bytes()
    assert entry["original_sha256"] == pkg.files["SKILL.md"].sha256
    assert {f["path"]: f["sha256"] for f in entry["files"]}["SKILL.md"] == hashlib.sha256(written).hexdigest()
    assert entry["original_sha256"] != hashlib.sha256(written).hexdigest()
    assert env.control()["baas_py"] == str(env.baas_py)


def test_the_note_of_a_skill_with_scripts_explains_the_executor(env: Env) -> None:
    publish_pair(env)
    assert env.run()[0] == 0
    note = env.skill_md().split("\n---\n", 1)[1].split("# Gestor")[0]
    assert "baas.py run-script" in note
    assert "no los copies" in note
    assert "knowledge" in note, "algunos scripts leen ficheros locales: hace falta el cerebro como carpeta de trabajo"
    env.publish("solo-texto", "---\nname: solo-texto\ndescription: x\n---\nhola\n")
    assert env.run()[0] == 0
    assert "run-script" not in env.skill_md("solo-texto")


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
    assert "+ demo-a" not in out
    assert "~ demo-a" not in out
    assert [c for c in env.server.state.tool_calls("baas_get_skill_package") if c[1]["skill"] == "a"] == []


# --- idempotencia, cambio de ruta y migración -------------------------------------------------------------------


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


def test_moving_the_plugin_changes_baas_py_and_the_next_sync_updates_the_skills_that_use_it(
    env: Env, monkeypatch, tmp_path
) -> None:
    publish_pair(env)
    env.publish("solo-texto", "---\nname: solo-texto\ndescription: x\n---\nhola\n")
    assert env.run()[0] == 0
    new_py = tmp_path / "otro sitio" / "scripts" / "baas.py"
    monkeypatch.setattr(native_skills, "BAAS_PY", new_py)
    env.server.state.requests.clear()
    code, out = env.run()
    assert code == 0, out
    assert "~ demo-gestor" in out
    assert f'python3 "{new_py}" run-script' in env.skill_md()
    assert str(env.baas_py) not in env.skill_md()
    assert env.control()["baas_py"] == str(new_py)
    assert "~ demo-solo-texto" not in out, "una skill sin llamadas a scripts no depende de la ruta"
    assert [c for c in env.server.state.tool_calls("baas_get_skill_package") if c[1]["skill"] == "solo-texto"] == []
    code, out = env.run()
    assert "Todo al día" in out


def test_a_control_file_from_before_this_change_is_migrated_by_the_next_sync(env: Env) -> None:
    publish_pair(env)
    assert env.run()[0] == 0
    control = env.root / ".baas-skills-demo.json"
    data = json.loads(control.read_text())
    for entry in data["skills"].values():
        for key in ("adapt_fp", "refs", "runner", "original_sha256"):
            entry.pop(key, None)
    data.pop("baas_py", None)
    data.pop("adapter", None)
    control.write_text(json.dumps(data))
    code, out = env.run()
    assert code == 0, out
    assert "~ demo-gestor" in out
    assert all("adapt_fp" in e for e in env.control()["skills"].values())


def test_a_stale_skill_shows_up_in_a_dry_run_without_writing(env: Env, monkeypatch, tmp_path) -> None:
    publish_pair(env)
    assert env.run()[0] == 0
    before = env.skill_md()
    monkeypatch.setattr(native_skills, "BAAS_PY", tmp_path / "x" / "baas.py")
    code, out = env.run("--dry-run")
    assert code == 0, out
    assert "~ demo-gestor" in out
    assert env.skill_md() == before


def test_a_user_edit_of_a_skill_that_needs_re_adapting_is_not_overwritten(env: Env, monkeypatch, tmp_path) -> None:
    publish_pair(env)
    assert env.run()[0] == 0
    (env.root / "demo-gestor/SKILL.md").write_text("editada por el usuario\n")
    monkeypatch.setattr(native_skills, "BAAS_PY", tmp_path / "x" / "baas.py")
    code, out = env.run()
    assert code == 0, out
    assert env.skill_md() == "editada por el usuario\n"
    assert "modificada" in out


def test_the_adapted_text_has_no_claude_skill_dir_in_what_was_recognised(env: Env) -> None:
    md = (
        "---\nname: gestor\ndescription: x\n---\n```bash\n"
        'python3 "${CLAUDE_SKILL_DIR}/scripts/hola.py" a\n"$CLAUDE_SKILL_DIR/scripts/hola.py" b\n```\n'
    )
    env.publish("gestor", md, **{"scripts/hola.py": SCRIPT})
    assert env.run()[0] == 0
    assert "CLAUDE_SKILL_DIR" not in env.skill_md()


def test_the_sync_never_downloads_the_scripts_only_the_executor_does(env: Env) -> None:
    publish_pair(env)
    assert env.run()[0] == 0
    requested = [
        p for _tool, args in env.server.state.tool_calls("baas_get_skill_files") for p in args.get("paths", [])
    ]
    assert requested, "la prueba no vería nada si no se pidiera ningún fichero"
    assert not [p for p in requested if "scripts/" in p]
