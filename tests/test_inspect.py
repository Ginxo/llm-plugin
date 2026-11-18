"""`baas.py inspect`: informe mecánico de una skill. Usa una copia sintética de los patrones de `facturacion`."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from baas_plugin import cli
from baas_plugin import inspect as skill_inspect
from baas_plugin.errors import ConfigError
from skill_fixture import CIF, EMAIL, FAKE_GH_TOKEN, IBAN, IBAN_COMPACT, NIF, Brain, make_brain


@pytest.fixture
def brain(tmp_path: Path) -> Brain:
    return make_brain(tmp_path)


@pytest.fixture
def report(brain: Brain) -> dict:
    return skill_inspect.inspect_skill(brain.skill, scripts=[brain.scripts], knowledge=[brain.knowledge])


def paths(items: list[dict], key: str = "file") -> set[str]:
    return {i[key] for i in items}


def snapshot(root: Path) -> dict[str, tuple[bytes, float, int]]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            out[str(p)] = (p.read_bytes(), st.st_mtime, st.st_mode)
    return out


# --- ficheros -----------------------------------------------------------------------------------


def test_file_types_sizes_and_areas(report: dict, brain: Brain) -> None:
    files = {f["path"]: f for f in report["files"]}
    assert files["SKILL.md"]["area"] == "skill"
    assert files["scripts/facturacion.py"]["area"] == "scripts"
    assert files["knowledge/ledger-facturas.json"]["area"] == "knowledge"
    assert files["assets/logo.png"]["text"] is False
    assert files["templates/informe.docx"]["text"] is False
    assert files["templates/informe.typ"]["text"] is True
    assert files["references/normativa.md"]["text"] is True
    assert files["scripts/facturacion.py"]["exec"] is True
    assert files["scripts/md_to_doc.py"]["exec"] is False
    assert files["scripts/facturacion.py"]["size"] == (brain.scripts / "facturacion.py").stat().st_size
    summary = report["summary"]
    assert summary["files"] == len(report["files"])
    assert summary["bytes"] == sum(f["size"] for f in report["files"])
    assert summary["by_ext"][".md"]["count"] >= 3
    assert summary["binary_files"] >= 1


def test_hidden_symlinks_and_ignored_are_reported(report: dict) -> None:
    assert ".env.example" in report["hidden"]
    assert "knowledge/facturas/2026/.gitkeep" in report["hidden"]
    links = {s["path"]: s for s in report["symlinks"]}
    assert links["refs-enlace"]["action"] == "followed"
    assert links["fuera.txt"]["action"] == "skipped"
    assert report["ignored"] >= 1  # __pycache__
    assert "scripts/__pycache__/facturacion.cpython-313.pyc" not in {f["path"] for f in report["files"]}
    # lo seguido aparece como fichero; lo omitido, nunca
    paths_ = {f["path"] for f in report["files"]}
    assert "refs-enlace/normativa.md" in paths_
    assert "fuera.txt" not in paths_


# --- cabecera ---------------------------------------------------------------------------------


def test_frontmatter_hooks_and_allowed_tools(report: dict) -> None:
    fm = report["frontmatter"]
    assert fm["present"] is True
    assert fm["valid_yaml"] is True
    assert fm["name"] == "facturacion"
    assert "invoice" in fm["description"]
    assert fm["hooks"] is True
    assert fm["allowed_tools"] == "Bash(python3 scripts/facturacion.py *) Read"
    assert {"name", "description", "allowed-tools", "hooks"} <= set(fm["keys"])
    assert any("hooks" in w for w in report["warnings"])
    assert any("allowed-tools" in w for w in report["warnings"])


def test_lenient_header_is_reported(tmp_path: Path) -> None:
    skill = tmp_path / "x"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: x\ndescription: Use when: el usuario pide algo\n---\ncuerpo\n")
    rep = skill_inspect.inspect_skill(skill)
    assert rep["frontmatter"]["valid_yaml"] is False
    assert rep["frontmatter"]["description"] == "Use when: el usuario pide algo"
    assert any("HEADER_LENIENT" in w for w in rep["warnings"])


def test_name_differing_from_directory_is_warned(tmp_path: Path) -> None:
    skill = tmp_path / "directorio"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: otro-nombre\ndescription: d\n---\n")
    rep = skill_inspect.inspect_skill(skill)
    assert rep["skill"]["name"] == "directorio"
    assert any("otro-nombre" in w and "directorio" in w for w in rep["warnings"])


def test_directory_without_skill_md_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"SKILL\.md"):
        skill_inspect.inspect_skill(tmp_path)


# --- enlaces ------------------------------------------------------------------------------------


def test_links_to_other_skills(report: dict) -> None:
    skills = {s["name"]: s for s in report["links"]["skills"]}
    assert set(skills) == {"gestor", "md-to-doc"}
    assert skills["gestor"]["from"] == "SKILL.md"
    assert skills["gestor"]["line"] > 1


def test_links_to_scripts_and_commands(report: dict) -> None:
    targets = {s["target"]: s for s in report["links"]["scripts"]}
    assert targets["scripts/gestor-facturas.py"]["kind"] == "link"
    assert targets["scripts/gestor-facturas.py"]["exists"] is True
    assert targets["scripts/facturacion.py"]["kind"] == "command"
    assert targets["scripts/render_doc.py"]["exists"] is True


def test_links_to_knowledge(report: dict) -> None:
    targets = {k["target"]: k for k in report["links"]["knowledge"]}
    assert targets["knowledge/fiscal/ledger-facturas.json"]["exists_locally"] is True
    assert "knowledge/fiscal/perfil-fiscal-y-regimen.md" in targets
    assert "knowledge/fiscal/templates/factura-pro.typ" in targets
    # los que cita un script también cuentan (rutas calculadas desde `__file__`)
    assert "knowledge/fiscal/facturas" in targets


def test_broken_links_are_reported(report: dict) -> None:
    assert [b["target"] for b in report["links"]["broken"]] == ["references/no-existe.md"]


def test_missing_scripts_when_no_scripts_dir_is_given(brain: Brain) -> None:
    rep = skill_inspect.inspect_skill(brain.skill)
    missing = set(rep["scripts"]["missing"])
    assert {"scripts/facturacion.py", "scripts/render_doc.py", "scripts/gestor-facturas.py"} <= missing
    assert rep["scripts"]["suggested"] == []


# --- scripts: imports, llamadas y cierre ---------------------------------------------------------------


def test_imports_between_own_scripts(report: dict) -> None:
    edges = {(e["from"], e["to"]) for e in report["scripts"]["imports"]}
    assert ("scripts/facturacion.py", "scripts/render_doc.py") in edges
    third = {(t["file"], t["module"]) for t in report["scripts"]["third_party"]}
    assert ("scripts/facturacion.py", "yaml") in third
    stdlib_noise = {m for _, m in third}
    assert not stdlib_noise & {"json", "os", "sys", "pathlib", "subprocess", "smtplib"}


def test_calls_between_scripts_by_subprocess_and_shell(report: dict) -> None:
    edges = {(c["from"], c["to"]) for c in report["scripts"]["calls"]}
    assert ("scripts/facturacion.py", "scripts/md_to_doc.py") in edges
    assert ("scripts/md_to_doc.py", "scripts/render_doc.py") in edges
    assert ("scripts/auditor.sh", "scripts/md_to_doc.py") in edges


def test_suggested_scripts_are_the_closure_of_what_the_skill_cites(report: dict) -> None:
    assert set(report["scripts"]["suggested"]) == {
        "scripts/facturacion.py",
        "scripts/render_doc.py",
        "scripts/md_to_doc.py",
        "scripts/gestor-facturas.py",
    }
    assert set(report["scripts"]["other"]) == {
        "scripts/auditor.sh",
        "scripts/browser-scraper.js",
        "scripts/tool.js",
        "scripts/unrelated_tool.py",
    }
    assert report["scripts"]["missing"] == []


# --- rutas calculadas desde el propio fichero ----------------------------------------------------------


def escapes(report: dict) -> dict[tuple[str, str], dict]:
    return {(e["file"], e["name"]): e for e in report["path_escapes"]}


def test_file_relative_paths_that_leave_the_scripts_directory(report: dict) -> None:
    esc = escapes(report)
    root = esc[("scripts/facturacion.py", "REPO_ROOT")]
    assert root["kind"] == "__file__"
    assert root["levels_up"] == 2
    assert root["escapes"] is True
    assert root["line"] > 1
    assert "knowledge/fiscal/ledger-facturas.json" in root["derived"]
    assert esc[("scripts/render_doc.py", "REPO_ROOT")]["escapes"] is True
    # `ROOT = dirname(HERE)` se resuelve a través de la variable intermedia
    assert esc[("scripts/gestor-facturas.py", "ROOT")]["levels_up"] == 2
    assert esc[("scripts/gestor-facturas.py", "ROOT")]["escapes"] is True
    # la carpeta del propio script no sale de scripts/
    assert esc[("scripts/md_to_doc.py", "SCRIPTS_DIR")]["escapes"] is False
    assert esc[("scripts/gestor-facturas.py", "HERE")]["escapes"] is False


def test_shell_and_js_escapes(report: dict) -> None:
    esc = escapes(report)
    sh = esc[("scripts/auditor.sh", "ROOT")]
    assert sh["kind"] == "BASH_SOURCE"
    assert sh["levels_up"] == 2
    assert sh["escapes"] is True
    js = esc[("scripts/tool.js", "root")]
    assert js["kind"] == "__dirname"
    assert js["escapes"] is True


# --- binarios, intérpretes y estado ------------------------------------------------------------------


def test_external_binaries_and_interpreters(report: dict) -> None:
    external = {b["name"]: b for b in report["binaries"]["external"]}
    assert {"typst", "mmdc"} <= set(external)
    assert any("scripts/render_doc.py" in w for w in external["typst"]["where"])
    assert any("scripts/auditor.sh" in w for w in external["typst"]["where"])
    assert any(w.startswith("SKILL.md") for w in external["typst"]["where"])
    assert set(report["binaries"]["interpreters"]) == {"python3", "bash", "node"}


def test_browser_snippet_is_not_node(report: dict) -> None:
    kinds = {(n["file"], n["kind"]) for n in report["nonportable"]}
    assert ("scripts/browser-scraper.js", "browser_snippet") in kinds
    assert ("scripts/tool.js", "browser_snippet") not in kinds


def test_mutable_state_written_under_the_package(report: dict) -> None:
    state = report["mutable_state"]
    by_file: dict[str, list[dict]] = {}
    for s in state:
        by_file.setdefault(s["file"], []).append(s)
    ledger = [
        s for s in by_file["scripts/facturacion.py"] if s.get("path_hint") == "knowledge/fiscal/ledger-facturas.json"
    ]
    assert ledger
    assert ledger[0]["derived_from_file"] is True
    assert any(
        s.get("path_hint", "") and s["path_hint"].startswith("knowledge/fiscal/facturas")
        for s in by_file["scripts/facturacion.py"]
    )
    assert "scripts/gestor-facturas.py" in by_file
    assert any(s["op"].startswith("mkdir") or "mkdir" in s["op"] for s in by_file["scripts/auditor.sh"])
    assert any(">" in s["op"] or "redirect" in s["op"] for s in by_file["scripts/auditor.sh"])
    assert "scripts/tool.js" in by_file
    assert "scripts/render_doc.py" not in by_file  # solo ejecuta typst: no escribe en el paquete


# --- datos sensibles ---------------------------------------------------------------------------------


def test_sensitive_data_is_detected_and_masked(report: dict) -> None:
    items = report["sensitive"]["items"]
    kinds = {(i["kind"], i["file"]) for i in items}
    assert ("nif", "SKILL.md") in kinds
    assert ("cif", "SKILL.md") in kinds
    assert ("iban", "SKILL.md") in kinds
    assert ("email", "SKILL.md") in kinds
    assert ("nif", "knowledge/ledger-facturas.json") in kinds
    assert ("iban", "knowledge/ledger-facturas.json") in kinds
    assert ("iban", "knowledge/perfil-fiscal-y-regimen.md") in kinds  # sin espacios también
    assert ("password", "scripts/facturacion.py") in kinds
    assert ("token", "references/claves.md") in kinds
    assert ("private_key", "references/claves.md") in kinds
    dumped = json.dumps(report)
    for secret in (NIF, CIF, IBAN, IBAN_COMPACT, EMAIL, FAKE_GH_TOKEN, "hunter2-ejemplo-9"):
        assert secret not in dumped
    assert all("sample" in i and "line" in i for i in items)


def test_sensitive_files_by_name(report: dict) -> None:
    files = {f["file"]: f["reason"] for f in report["sensitive"]["files"]}
    assert ".env" in files
    assert ".env.example" not in files


def test_invalid_look_alikes_are_not_flagged(tmp_path: Path) -> None:
    skill = tmp_path / "s"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\nDNI 12345678A (letra mala), IBAN ES00 0000 0000 0000 0000 0000, "
        "version 1.2.3, pedido 98765432101.\n"
    )
    rep = skill_inspect.inspect_skill(skill)
    assert rep["sensitive"]["items"] == []


# --- no portable ------------------------------------------------------------------------------------


def test_absolute_paths_and_native_binaries_are_flagged(brain: Brain) -> None:
    (brain.skill / "assets" / "tool-linux").write_bytes(b"\x7fELF\x02\x01\x01" + bytes(100))
    (brain.skill / "assets" / "instalar.ps1").write_text("Write-Host hola\n")
    rep = skill_inspect.inspect_skill(brain.skill, scripts=[brain.scripts], knowledge=[brain.knowledge])
    kinds = {(n["file"], n["kind"]) for n in rep["nonportable"]}
    assert ("SKILL.md", "absolute_path") in kinds
    assert ("assets/tool-linux", "native_binary") in kinds
    assert ("assets/instalar.ps1", "windows_only") in kinds
    detail = next(n["detail"] for n in rep["nonportable"] if n["kind"] == "absolute_path")
    assert "/home/ana/.local/bin/typst" in detail


# --- reescrituras --------------------------------------------------------------------------------------


def test_rewrites_are_proposed_for_markdown_links_and_commands(report: dict) -> None:
    rewrites = report["rewrites"]
    link = next(r for r in rewrites if r["kind"] == "md_script_link" and r["before"].endswith("gestor-facturas.py)"))
    assert link["file"] == "SKILL.md"
    assert (
        link["after"] == "[`gestor-facturas.py`](scripts/gestor-facturas.py)"
        or "(scripts/gestor-facturas.py)" in link["after"]
    )
    cmds = [r for r in rewrites if r["kind"] == "md_script_command"]
    assert any("${CLAUDE_SKILL_DIR}/scripts/facturacion.py" in r["after"] for r in cmds)
    assert all(r["file"] == "SKILL.md" for r in cmds)


def test_rewrites_are_proposed_for_file_based_roots(report: dict) -> None:
    by_file = {(r["file"], r["kind"]) for r in report["rewrites"]}
    assert ("scripts/facturacion.py", "py_root") in by_file
    assert ("scripts/render_doc.py", "py_root") in by_file
    assert ("scripts/gestor-facturas.py", "py_root") in by_file
    assert ("scripts/auditor.sh", "sh_root") in by_file
    assert ("scripts/md_to_doc.py", "py_root") not in by_file  # SCRIPTS_DIR no sale de scripts/
    root = next(r for r in report["rewrites"] if r["file"] == "scripts/facturacion.py")
    assert "CLAUDE_PROJECT_DIR" in root["after"]
    assert root["before"].startswith("REPO_ROOT = Path(__file__)")


def test_apply_rewrites_gives_valid_code_and_never_touches_the_original(brain: Brain) -> None:
    before = snapshot(brain.root)
    rep = skill_inspect.inspect_skill(brain.skill, scripts=[brain.scripts], knowledge=[brain.knowledge])
    assert snapshot(brain.root) == before  # inspect es de solo lectura
    src = (brain.scripts / "facturacion.py").read_text()
    new = skill_inspect.apply_rewrites(src, [r for r in rep["rewrites"] if r["file"] == "scripts/facturacion.py"])
    ast.parse(new)
    assert "CLAUDE_PROJECT_DIR" in new
    assert "Path(__file__).resolve().parent.parent" not in new
    assert "sys.path.insert(0, str(Path(__file__).resolve().parent))" in new  # lo que no sale de scripts/ se conserva
    gestor = (brain.scripts / "gestor-facturas.py").read_text()
    new_gestor = skill_inspect.apply_rewrites(
        gestor, [r for r in rep["rewrites"] if r["file"] == "scripts/gestor-facturas.py"]
    )
    ast.parse(new_gestor)
    assert new_gestor.count("CLAUDE_PROJECT_DIR") >= 2  # ROOT (os.path) y REPO_ROOT (Path)
    sh = (brain.scripts / "auditor.sh").read_text()
    new_sh = skill_inspect.apply_rewrites(sh, [r for r in rep["rewrites"] if r["file"] == "scripts/auditor.sh"])
    assert 'ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"' in new_sh
    assert "BASH_SOURCE" not in new_sh


def test_apply_rewrites_in_skill_md(brain: Brain) -> None:
    rep = skill_inspect.inspect_skill(brain.skill, scripts=[brain.scripts], knowledge=[brain.knowledge])
    md = (brain.skill / "SKILL.md").read_text()
    new = skill_inspect.apply_rewrites(md, [r for r in rep["rewrites"] if r["file"] == "SKILL.md"])
    assert "](scripts/gestor-facturas.py)" in new
    assert '"${CLAUDE_SKILL_DIR}/scripts/facturacion.py" preflight' in new
    assert "](../gestor/SKILL.md)" in new  # los enlaces entre skills siguen siendo válidos
    assert "knowledge/fiscal/templates/factura-pro.typ" in new  # el conocimiento no es del paquete: no se toca


def test_rewrites_only_for_scripts_that_are_included(brain: Brain) -> None:
    rep = skill_inspect.inspect_skill(brain.skill, scripts=[brain.scripts], knowledge=[brain.knowledge])
    only = skill_inspect.filter_rewrites(rep["rewrites"], included={"SKILL.md", "scripts/facturacion.py"})
    cmd_targets = [r["after"] for r in only if r["kind"] == "md_script_command"]
    assert any("facturacion.py" in c for c in cmd_targets)
    assert not any("render_doc.py" in c for c in cmd_targets)
    assert not any(r["file"] == "scripts/auditor.sh" for r in only)


# --- sugerencias para el manifiesto -----------------------------------------------------------------------


def test_manifest_hint(report: dict) -> None:
    hint = report["manifest"]
    assert hint["depends_on"]["skills"] == ["gestor", "md-to-doc"]
    assert hint["requires"]["binaries"] == ["mmdc", "typst"]
    assert hint["requires"]["interpreters"] == ["bash", "node", "python3"]
    assert {"scripts/facturacion.py", "scripts/render_doc.py", "scripts/gestor-facturas.py"} <= set(hint["executable"])
    assert "scripts/browser-scraper.js" not in hint["executable"]


# --- CLI ---------------------------------------------------------------------------------------------------


def test_cli_json_output(brain: Brain, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(
        ["inspect", str(brain.skill), "--scripts", str(brain.scripts), "--knowledge", str(brain.knowledge), "--json"]
    )
    out = capsys.readouterr().out
    assert code == 0
    data = json.loads(out)
    assert data["skill"]["name"] == "facturacion"
    assert data["scripts"]["suggested"]


def test_cli_text_report_in_spanish(brain: Brain, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["inspect", str(brain.skill), "--scripts", str(brain.scripts), "--knowledge", str(brain.knowledge)])
    out = capsys.readouterr().out
    assert code == 0
    for heading in (
        "Ficheros",
        "Scripts",
        "Rutas calculadas",
        "Enlaces",
        "Binarios externos",
        "Estado mutable",
        "Datos sensibles",
        "Cabecera",
        "No portable",
        "Reescrituras propuestas",
    ):
        assert heading in out
    for secret in (NIF, IBAN, IBAN_COMPACT, EMAIL, FAKE_GH_TOKEN):
        assert secret not in out
    assert "typst" in out
    assert "gestor" in out


def test_cli_rejects_a_directory_without_skill_md(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["inspect", str(tmp_path)]) == 2
    assert "SKILL.md" in capsys.readouterr().err


def test_inspect_does_not_read_beyond_the_tree(brain: Brain) -> None:
    rep = skill_inspect.inspect_skill(brain.skill, scripts=[brain.scripts], knowledge=[brain.knowledge])
    assert "no debe subirse" not in json.dumps(rep)


def test_knowledge_with_a_known_prefix_is_shown_under_its_store_path(brain: Brain) -> None:
    rep = skill_inspect.inspect_skill(brain.skill, scripts=[brain.scripts], knowledge=[(brain.knowledge, "fiscal")])
    assert "knowledge/fiscal/ledger-facturas.json" in {f["path"] for f in rep["files"]}
    targets = {k["target"]: k for k in rep["links"]["knowledge"]}
    assert targets["knowledge/fiscal/ledger-facturas.json"]["exists_locally"] is True
    assert targets["knowledge/fiscal/templates/factura-pro.typ"]["exists_locally"] is True
    assert any(i["file"] == "knowledge/fiscal/perfil-fiscal-y-regimen.md" for i in rep["sensitive"]["items"])


def test_rewrites_never_repeat_sensitive_data_from_the_surrounding_line(report: dict) -> None:
    cmds = [r for r in report["rewrites"] if r["kind"] == "md_script_command"]
    # la línea `scripts/facturacion.py client-add --nif … --email …` solo aporta el trozo que cambia
    assert any(r["before"] == " scripts/facturacion.py " for r in cmds)
    dumped = json.dumps(report["rewrites"])
    for secret in (NIF, EMAIL):
        assert secret not in dumped
    assert all(len(r["before"]) < 60 for r in cmds)


def test_two_scripts_on_one_line_are_rewritten_independently(tmp_path: Path, brain: Brain) -> None:
    skill = brain.skill
    (skill / "SKILL.md").write_text(
        "---\nname: facturacion\ndescription: d\n---\n\n```bash\n"
        "python3 scripts/facturacion.py x && python3 scripts/render_doc.py y\n```\n"
    )
    rep = skill_inspect.inspect_skill(skill, scripts=[brain.scripts])
    mine = [r for r in rep["rewrites"] if r["file"] == "SKILL.md"]
    new = skill_inspect.apply_rewrites((skill / "SKILL.md").read_text(), mine)
    expected = 'python3 "${CLAUDE_SKILL_DIR}/scripts/facturacion.py" x && '
    expected += 'python3 "${CLAUDE_SKILL_DIR}/scripts/render_doc.py" y'
    assert expected in new


def test_name_is_the_one_given_even_through_a_symlink(brain: Brain, tmp_path: Path) -> None:
    import os

    link = tmp_path / "enlace-a-facturacion"
    os.symlink(brain.skill, link)
    rep = skill_inspect.inspect_skill(link)
    assert rep["skill"]["name"] == "enlace-a-facturacion"


def test_big_text_files_are_still_analysed_without_being_kept_in_memory(brain: Brain) -> None:
    big = brain.skill / "references" / "grande.txt"
    big.write_text("relleno\n" * 200_000 + f"DNI {NIF}\n")  # ~1,6 MiB: por encima de lo que se guarda
    rep = skill_inspect.inspect_skill(brain.skill)
    assert any(i["file"] == "references/grande.txt" and i["kind"] == "nif" for i in rep["sensitive"]["items"])
    entry = next(f for f in rep["files"] if f["path"] == "references/grande.txt")
    assert entry["text"] is True
    assert entry["size"] > 1024 * 1024
