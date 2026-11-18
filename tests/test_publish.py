"""`publish`: plan, lote único, `expected_sha256`, `--prune`, `--dry-run`, reescrituras y verificación."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from baas_plugin import publish
from baas_plugin.errors import ConfigError, McpToolError, ScriptIntegrityError
from baas_plugin.publish import PublishOptions
from fake_publish_server import DirectClient, FakeStore
from skill_fixture import NIF, Brain, make_brain

SKILL_PATH = "skills/facturacion"
SUGGESTED = ["facturacion.py", "render_doc.py", "md_to_doc.py", "gestor-facturas.py"]


@pytest.fixture
def brain(tmp_path: Path) -> Brain:
    return make_brain(tmp_path)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def opts(brain: Brain, **kw: Any) -> PublishOptions:
    base: dict[str, Any] = {
        "skill_dir": brain.skill,
        "roles": ["facturacion"],
        "scripts": [brain.scripts / n for n in SUGGESTED],
    }
    base.update(kw)
    return PublishOptions(**base)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def writes(store: FakeStore, *, dry: bool | None = None) -> list[dict[str, Any]]:
    calls = [a for n, a in store.calls if n == "baas_write_files"]
    return calls if dry is None else [a for a in calls if bool(a.get("dry_run")) is dry]


def tree_snapshot(root: Path) -> dict[str, bytes]:
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


# --- un lote, bytes exactos ---------------------------------------------------------------------------


def test_publish_a_whole_skill_in_one_atomic_batch(brain: Brain, store: FakeStore) -> None:
    before = tree_snapshot(brain.root)
    result = publish.run_publish(DirectClient(store), opts(brain))
    assert len(writes(store)) == 1  # un solo lote
    assert writes(store)[0]["dry_run"] is False
    assert tree_snapshot(brain.root) == before  # no toca el original
    files = store.skill_files("facturacion")
    assert files["SKILL.md"] == (brain.skill / "SKILL.md").read_bytes()
    assert files["assets/logo.png"] == (brain.skill / "assets/logo.png").read_bytes()
    assert files["templates/informe.docx"] == (brain.skill / "templates/informe.docx").read_bytes()
    assert files["scripts/facturacion.py"] == (brain.scripts / "facturacion.py").read_bytes()
    assert files["references/normativa.md"].startswith(b"# Normativa")
    assert result.verified is True
    assert result.package_sha256 == store._tool_baas_get_skill_package({"skill": "facturacion"})["package_sha256"]


def test_what_is_not_uploaded(brain: Brain, store: FakeStore) -> None:
    publish.run_publish(DirectClient(store), opts(brain))
    paths = set(store.skill_files("facturacion"))
    assert "fuera.txt" not in paths  # enlace simbólico hacia fuera del árbol
    assert ".env" not in paths  # credenciales
    assert ".env.example" in paths  # los ocultos que no son secretos sí
    assert not any("__pycache__" in p for p in paths)
    assert "refs-enlace/normativa.md" in paths  # enlace interno: se sigue
    assert "scripts/unrelated_tool.py" not in paths  # solo los scripts que se indican
    assert b"no debe subirse" not in b"".join(store.files.values())


def test_encoding_is_utf8_for_text_and_base64_for_binaries(brain: Brain, store: FakeStore) -> None:
    publish.run_publish(DirectClient(store), opts(brain, dry_run=True))
    sent = {f["path"]: f for f in writes(store)[0]["files"]}
    assert sent[f"{SKILL_PATH}/SKILL.md"]["encoding"] == "utf8"
    logo = sent[f"{SKILL_PATH}/assets/logo.png"]
    assert logo["encoding"] == "base64"
    assert base64.b64decode(logo["content"]) == (brain.skill / "assets/logo.png").read_bytes()


def test_manifest_is_generated(brain: Brain, store: FakeStore) -> None:
    publish.run_publish(DirectClient(store), opts(brain))
    manifest = yaml.safe_load(store.files[f"{SKILL_PATH}/baas.yaml"].decode())
    assert manifest["version"] == 1
    assert manifest["roles"] == ["facturacion"]
    assert manifest["requires"]["binaries"] == ["mmdc", "typst"]
    assert "python3" in manifest["requires"]["interpreters"]
    assert manifest["depends_on"]["skills"] == ["gestor", "md-to-doc"]
    assert {"scripts/facturacion.py", "scripts/render_doc.py", "scripts/gestor-facturas.py"} <= set(
        manifest["executable"]
    )
    assert "scripts/md_to_doc.py" not in manifest["executable"]  # sin bit ni #!
    assert "assets/logo.png" not in manifest["executable"]


def test_executable_follows_the_exec_bit_and_shebang(brain: Brain, store: FakeStore) -> None:
    (brain.skill / "assets" / "tool.sh").write_text("#!/bin/sh\necho x\n")
    (brain.skill / "assets" / "run").write_text("sin shebang\n")
    os.chmod(brain.skill / "assets" / "run", 0o755)  # noqa: S103
    publish.run_publish(DirectClient(store), opts(brain))
    executable = set(yaml.safe_load(store.files[f"{SKILL_PATH}/baas.yaml"].decode())["executable"])
    assert "assets/tool.sh" in executable  # `#!` sin bit
    assert "assets/run" in executable  # bit sin `#!`
    assert "references/normativa.md" not in executable


def test_wildcard_roles(brain: Brain, store: FakeStore) -> None:
    publish.run_publish(DirectClient(store), opts(brain, roles=["*"]))
    assert yaml.safe_load(store.files[f"{SKILL_PATH}/baas.yaml"].decode())["roles"] == ["*"]


def test_knowledge_is_uploaded_with_prefix_and_acl(brain: Brain, store: FakeStore) -> None:
    result = publish.run_publish(
        DirectClient(store),
        opts(
            brain,
            knowledge=[
                (brain.knowledge / "perfil-fiscal-y-regimen.md", "fiscal"),
                (brain.knowledge / "templates", "fiscal/templates"),
            ],
            knowledge_roles=["facturacion"],
        ),
    )
    assert (
        store.files["knowledge/fiscal/perfil-fiscal-y-regimen.md"]
        == (brain.knowledge / "perfil-fiscal-y-regimen.md").read_bytes()
    )
    assert "knowledge/fiscal/templates/factura-pro.typ" in store.files
    assert yaml.safe_load(store.files["knowledge/fiscal/templates/_acl.yaml"].decode()) == {"roles": ["facturacion"]}
    assert "knowledge/fiscal/_acl.yaml" not in store.files  # un fichero suelto no cambia los roles de su carpeta
    assert result.verified is True
    manifest = yaml.safe_load(store.files[f"{SKILL_PATH}/baas.yaml"].decode())
    assert "knowledge/fiscal/templates/factura-pro.typ" in manifest["depends_on"]["knowledge"]
    assert "knowledge/fiscal/perfil-fiscal-y-regimen.md" in manifest["depends_on"]["knowledge"]


def test_knowledge_directory_keeps_relative_tree_and_skips_acl_if_no_roles(brain: Brain, store: FakeStore) -> None:
    publish.run_publish(DirectClient(store), opts(brain, knowledge=[(brain.knowledge, "knowledge/fiscal")]))
    assert "knowledge/fiscal/ledger-facturas.json" in store.files
    assert "knowledge/fiscal/facturas/2026/.gitkeep" in store.files
    assert "knowledge/fiscal/_acl.yaml" not in store.files
    assert not any(p.startswith("knowledge/knowledge") for p in store.files)


@pytest.mark.parametrize(
    ("raw", "prefix"),
    [
        ("/tmp/x:fiscal", "fiscal"),
        ("/tmp/x:knowledge/fiscal/", "fiscal"),
        ("/tmp/x:/a/b/", "a/b"),
        ("/tmp/x:knowledge", ""),
    ],
)
def test_knowledge_argument_parsing(raw: str, prefix: str) -> None:
    path, got = publish.parse_knowledge_arg(raw)
    assert path == Path("/tmp/x")
    assert got == prefix


def test_knowledge_argument_needs_a_prefix_separator() -> None:
    with pytest.raises(ConfigError, match="DIR:PREFIJO"):
        publish.parse_knowledge_arg("/tmp/x")


# --- dry-run ---------------------------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_reports(brain: Brain, store: FakeStore) -> None:
    result = publish.run_publish(DirectClient(store), opts(brain, dry_run=True, roles=None))
    assert store.files == {}
    assert len(writes(store)) == 1
    assert writes(store)[0]["dry_run"] is True
    assert result.dry_run is True
    assert result.verified is None
    assert result.plan.counts["new"] == len(result.plan.files)
    codes = {w["code"] for w in result.server["warnings"]}
    assert {"NO_ROLES", "FRONTMATTER_HOOKS", "FRONTMATTER_ALLOWED_TOOLS"} <= codes
    assert result.server["summary"]["files"] == len(writes(store)[0]["files"])
    text = publish.render_plan(result)
    assert "facturacion" in text
    assert "NO_ROLES" in text
    assert "dry-run" in text.lower()


def test_dry_run_reports_estimated_traffic(brain: Brain, store: FakeStore) -> None:
    result = publish.run_publish(DirectClient(store), opts(brain, dry_run=True))
    assert result.plan.total_bytes == sum(f.size for f in result.plan.files)
    assert result.plan.upload_bytes >= result.plan.total_bytes  # base64 y JSON
    assert "tráfico" in publish.render_plan(result).lower()


# --- actualizar sin pisar -------------------------------------------------------------------------------


def test_republish_sends_only_what_changed(brain: Brain, store: FakeStore) -> None:
    client = DirectClient(store)
    publish.run_publish(client, opts(brain))
    (brain.skill / "references" / "normativa.md").write_text("# Normativa 2\n")
    result = publish.run_publish(client, opts(brain))
    sent = [f["path"] for f in writes(store)[-1]["files"]]
    assert sent == [f"{SKILL_PATH}/references/normativa.md", f"{SKILL_PATH}/refs-enlace/normativa.md"]
    assert result.plan.counts["changed"] == 2
    assert result.plan.counts["unchanged"] == len(result.plan.files) - 2
    prev = writes(store)[-1]["files"][0]["expected_sha256"]
    assert prev == sha(b"# Normativa\n\nTexto de referencia.\n")  # expected_sha256 = lo que había


def test_new_files_are_guarded_with_none(brain: Brain, store: FakeStore) -> None:
    publish.run_publish(DirectClient(store), opts(brain, dry_run=True))
    assert {f["expected_sha256"] for f in writes(store)[0]["files"]} == {"none"}


def test_nothing_to_publish_makes_no_write(brain: Brain, store: FakeStore) -> None:
    client = DirectClient(store)
    publish.run_publish(client, opts(brain))
    n = len(writes(store))
    result = publish.run_publish(client, opts(brain))
    assert len(writes(store)) == n
    assert result.written is False
    assert result.verified is True
    assert "sin cambios" in publish.render_plan(result).lower()


def test_concurrent_change_is_a_conflict_and_nothing_is_written(brain: Brain, store: FakeStore) -> None:
    client = DirectClient(store)
    publish.run_publish(client, opts(brain))
    (brain.skill / "references" / "normativa.md").write_text("# mi versión\n")
    plan = publish.build_plan(client, opts(brain))
    store.put(
        f"{SKILL_PATH}/references/normativa.md", "# la de otro administrador\n"
    )  # llega entre el plan y la escritura
    before = dict(store.files)
    with pytest.raises(McpToolError) as exc:
        publish.write_plan(client, plan, dry_run=False)
    assert exc.value.code == "CONFLICT"
    assert store.files == before
    assert "references/normativa.md" in publish.describe_tool_error(exc.value)


def test_new_file_created_meanwhile_conflicts(brain: Brain, store: FakeStore) -> None:
    client = DirectClient(store)
    plan = publish.build_plan(client, opts(brain))
    store.put(f"{SKILL_PATH}/SKILL.md", "# otro\n")
    with pytest.raises(McpToolError) as exc:
        publish.write_plan(client, plan, dry_run=False)
    assert exc.value.code == "CONFLICT"
    assert len(exc.value.payload["conflicts"]) >= 1


def test_invalid_roles_fail_everything_and_write_nothing(brain: Brain, store: FakeStore) -> None:
    with pytest.raises(McpToolError) as exc:
        publish.run_publish(DirectClient(store), opts(brain, roles=["no-existe"]))
    assert exc.value.code == "INVALID_ARGUMENT"
    assert store.files == {}
    message = publish.describe_tool_error(exc.value)
    assert "roles" in message
    assert "no-existe" in message


def test_republish_keeps_published_roles_when_none_given(brain: Brain, store: FakeStore) -> None:
    client = DirectClient(store)
    publish.run_publish(client, opts(brain, roles=["analista", "revisor"]))
    (brain.skill / "references" / "normativa.md").write_text("cambio\n")
    publish.run_publish(client, opts(brain, roles=None))
    assert yaml.safe_load(store.files[f"{SKILL_PATH}/baas.yaml"].decode())["roles"] == ["analista", "revisor"]


def test_local_baas_yaml_is_merged_and_never_modified(brain: Brain, store: FakeStore) -> None:
    local = "version: 1\nroles: [analista]\nowner: equipo-fiscal\ndepends_on:\n  skills: [gestor]\n"
    (brain.skill / "baas.yaml").write_text(local)
    publish.run_publish(DirectClient(store), opts(brain, roles=None))
    manifest = yaml.safe_load(store.files[f"{SKILL_PATH}/baas.yaml"].decode())
    assert manifest["roles"] == ["analista"]
    assert manifest["owner"] == "equipo-fiscal"
    assert manifest["depends_on"]["skills"] == ["gestor"]  # lo curado manda sobre lo detectado
    assert (brain.skill / "baas.yaml").read_text() == local


def test_invalid_local_baas_yaml_is_refused(brain: Brain, store: FakeStore) -> None:
    (brain.skill / "baas.yaml").write_text("roles: [a\n")
    with pytest.raises(ConfigError, match=r"baas\.yaml"):
        publish.run_publish(DirectClient(store), opts(brain))
    assert writes(store) == []


# --- prune ----------------------------------------------------------------------------------------------


def test_prune_retires_what_is_no_longer_local(brain: Brain, store: FakeStore) -> None:
    client = DirectClient(store)
    publish.run_publish(client, opts(brain))
    (brain.skill / "references" / "claves.md").unlink()
    result = publish.run_publish(client, opts(brain, prune=True))
    assert f"{SKILL_PATH}/references/claves.md" not in store.files
    gone = [f"{SKILL_PATH}/references/claves.md", f"{SKILL_PATH}/refs-enlace/claves.md"]  # el enlace interno la repetía
    assert writes(store)[-1]["delete"] == gone
    assert result.plan.delete == gone
    assert any(
        p == f"{SKILL_PATH}/references/claves.md" and kind == "delete" for p, _, kind in store.versions
    )  # queda versión


def test_without_prune_nothing_is_deleted_but_it_is_reported(brain: Brain, store: FakeStore) -> None:
    client = DirectClient(store)
    publish.run_publish(client, opts(brain))
    (brain.skill / "references" / "claves.md").unlink()
    result = publish.run_publish(client, opts(brain))
    assert f"{SKILL_PATH}/references/claves.md" in store.files
    assert any("--prune" in w for w in result.plan.warnings)


def test_prune_never_touches_other_skills_or_knowledge(brain: Brain, store: FakeStore) -> None:
    store.put("skills/otra/SKILL.md", "---\nname: otra\n---\n")
    store.put("knowledge/fiscal/x.md", "x")
    publish.run_publish(DirectClient(store), opts(brain, prune=True))
    assert "skills/otra/SKILL.md" in store.files
    assert "knowledge/fiscal/x.md" in store.files


# --- reescrituras -----------------------------------------------------------------------------------------


def test_rewrites_apply_only_to_the_uploaded_copy_and_only_when_asked(brain: Brain, store: FakeStore) -> None:
    original = (brain.scripts / "facturacion.py").read_bytes()
    publish.run_publish(DirectClient(store), opts(brain))
    assert store.skill_files("facturacion")["scripts/facturacion.py"] == original  # por defecto, nada se reescribe

    store2 = FakeStore()
    result = publish.run_publish(DirectClient(store2), opts(brain, rewrite_paths=True))
    sent = store2.skill_files("facturacion")
    assert b"CLAUDE_PROJECT_DIR" in sent["scripts/facturacion.py"]
    assert b"${CLAUDE_SKILL_DIR}/scripts/facturacion.py" in sent["SKILL.md"]
    assert b"](scripts/gestor-facturas.py)" in sent["SKILL.md"]
    assert (brain.scripts / "facturacion.py").read_bytes() == original  # el original, intacto
    assert b"CLAUDE_PROJECT_DIR" not in (brain.skill / "SKILL.md").read_bytes()
    assert any(f.rewritten for f in result.plan.files)
    assert "scripts/auditor.sh" not in sent  # no se incluyó: no se reescribe
    assert result.verified is True  # el hash verificado es el de lo subido, no el del original


def test_rewrites_are_listed_in_the_plan(brain: Brain, store: FakeStore) -> None:
    result = publish.run_publish(DirectClient(store), opts(brain, rewrite_paths=True, dry_run=True))
    text = publish.render_plan(result)
    assert "reescrit" in text.lower()
    assert any(f.rewritten for f in result.plan.files)


# --- seguridad y avisos --------------------------------------------------------------------------------------


def test_secret_files_are_omitted_unless_asked(brain: Brain, store: FakeStore) -> None:
    result = publish.run_publish(DirectClient(store), opts(brain, dry_run=True))
    assert any(o["path"] == ".env" for o in result.plan.omitted)
    assert any(".env" in w and "--include-secrets" in w for w in result.plan.warnings)
    result2 = publish.run_publish(DirectClient(FakeStore()), opts(brain, dry_run=True, include_secrets=True))
    assert not any(o["path"] == ".env" for o in result2.plan.omitted)
    assert any(f.path.endswith("/.env") for f in result2.plan.files)


def test_sensitive_data_is_warned_without_values(brain: Brain, store: FakeStore) -> None:
    result = publish.run_publish(DirectClient(store), opts(brain, dry_run=True))
    joined = "\n".join(result.plan.warnings)
    assert "nif" in joined.lower()
    assert "iban" in joined.lower()
    assert NIF not in joined
    assert NIF not in publish.render_plan(result)


def test_large_files_warn_but_are_uploaded(brain: Brain, store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish, "LARGE_FILE_BYTES", 1000)
    monkeypatch.setattr(publish, "LARGE_PACKAGE_BYTES", 3000)
    result = publish.run_publish(DirectClient(store), opts(brain))
    codes = {w.split(":")[0] for w in result.plan.warnings}
    assert {"LARGE_FILE", "LARGE_PACKAGE"} <= codes
    assert f"{SKILL_PATH}/assets/logo.png" in store.files


def test_a_file_of_tens_of_megabytes_roundtrips(brain: Brain, store: FakeStore) -> None:
    blob = os.urandom(26 * 1024 * 1024)
    (brain.skill / "assets" / "grande.bin").write_bytes(blob)
    result = publish.run_publish(DirectClient(store), opts(brain))
    assert store.skill_files("facturacion")["assets/grande.bin"] == blob
    assert result.verified is True
    assert "LARGE_FILE" in {w["code"] for w in result.server["warnings"]}


# --- errores del servidor -------------------------------------------------------------------------------------


def test_non_admin_is_denied_and_told_to_stop(brain: Brain) -> None:
    store = FakeStore(admin=False, user_roles=("facturacion",))
    with pytest.raises(McpToolError) as exc:
        publish.run_publish(DirectClient(store), opts(brain))
    assert exc.value.code == "AUTHORIZATION_DENIED"
    assert store.files == {}
    assert "Detente" in (exc.value.hint or "")


def test_kill_switch_stops_everything(brain: Brain) -> None:
    store = FakeStore()
    store.kill_scope = "ORG_PAUSED"
    with pytest.raises(McpToolError) as exc:
        publish.run_publish(DirectClient(store), opts(brain))
    assert exc.value.code == "KILL_SWITCH_ACTIVE"
    assert store.files == {}
    assert [n for n, _ in store.calls if n == "baas_write_files"] == []


def test_org_is_forwarded(brain: Brain, store: FakeStore) -> None:
    publish.run_publish(DirectClient(store), opts(brain, org="otra-org", dry_run=True))
    assert all(a.get("org") == "otra-org" for _, a in store.calls)


# --- verificación -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["relative", "store"])
def test_verification_works_with_both_path_styles(brain: Brain, style: str) -> None:
    store = FakeStore(path_style=style)
    result = publish.run_publish(
        DirectClient(store), opts(brain, knowledge=[(brain.knowledge / "templates", "fiscal/templates")])
    )
    assert result.verified is True
    assert store.skill_files("facturacion")["SKILL.md"]


class TamperingClient(DirectClient):
    """Tras escribir, el almacén «cambia» un fichero: la verificación debe darse cuenta."""

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        out = super().call_tool(name, arguments)
        if name == "baas_write_files" and not arguments.get("dry_run"):
            self.store.put(f"{SKILL_PATH}/SKILL.md", "alterado en el servidor\n")
        return out


def test_verification_failure_is_reported(brain: Brain, store: FakeStore) -> None:
    with pytest.raises(ScriptIntegrityError) as exc:
        publish.run_publish(TamperingClient(store), opts(brain))
    assert "SKILL.md" in exc.value.message


# --- validaciones locales ------------------------------------------------------------------------------------------


def test_directory_must_be_a_skill(tmp_path: Path, store: FakeStore) -> None:
    with pytest.raises(ConfigError, match=r"SKILL\.md"):
        publish.run_publish(DirectClient(store), PublishOptions(skill_dir=tmp_path))


def test_skill_name_must_be_valid_and_can_be_overridden(brain: Brain, store: FakeStore, tmp_path: Path) -> None:
    odd = tmp_path / "con espacio"
    odd.mkdir()
    (odd / "SKILL.md").write_text("---\nname: x\ndescription: d\n---\n")
    with pytest.raises(ConfigError, match="nombre"):
        publish.run_publish(DirectClient(store), PublishOptions(skill_dir=odd, roles=["*"]))
    publish.run_publish(DirectClient(store), PublishOptions(skill_dir=odd, roles=["*"], name="bien"))
    assert "skills/bien/SKILL.md" in store.files


def test_conflicting_script_paths_are_refused(brain: Brain, store: FakeStore) -> None:
    (brain.skill / "scripts").mkdir()
    (brain.skill / "scripts" / "render_doc.py").write_text("# otra versión\n")
    with pytest.raises(ConfigError, match=r"scripts/render_doc\.py"):
        publish.run_publish(DirectClient(store), opts(brain))


def test_identical_duplicates_are_merged(brain: Brain, store: FakeStore) -> None:
    (brain.skill / "scripts").mkdir()
    (brain.skill / "scripts" / "render_doc.py").write_bytes((brain.scripts / "render_doc.py").read_bytes())
    publish.run_publish(DirectClient(store), opts(brain))
    assert "scripts/render_doc.py" in store.skill_files("facturacion")


def test_json_summary_has_everything_the_skill_needs(brain: Brain, store: FakeStore) -> None:
    result = publish.run_publish(DirectClient(store), opts(brain, dry_run=True))
    data = publish.result_to_json(result)
    assert data["skill"] == "facturacion"
    assert data["dry_run"] is True
    assert {"path", "status", "size", "sha256"} <= set(data["files"][0])
    assert data["summary"]["files"] == len(data["files"])
    assert "warnings" in data
    assert "omitted" in data


def test_single_markdown_gets_roles_in_its_uploaded_header_only(brain: Brain, store: FakeStore) -> None:
    doc = brain.knowledge / "nota.md"
    doc.write_text("# Nota sin cabecera\n")
    original = doc.read_bytes()
    result = publish.run_publish(
        DirectClient(store), opts(brain, knowledge=[(doc, "fiscal")], knowledge_roles=["facturacion", "revisor"])
    )
    sent = store.files["knowledge/fiscal/nota.md"].decode()
    assert sent.startswith("---\nroles: [facturacion, revisor]\n---\n# Nota sin cabecera")
    assert doc.read_bytes() == original
    assert "knowledge/fiscal/_acl.yaml" not in store.files
    assert any(f.note and "cabecera" in f.note for f in result.plan.files)


def test_single_markdown_with_roles_keeps_them_and_warns(brain: Brain, store: FakeStore) -> None:
    perfil = brain.knowledge / "perfil-fiscal-y-regimen.md"
    result = publish.run_publish(
        DirectClient(store), opts(brain, knowledge=[(perfil, "fiscal")], knowledge_roles=["revisor"], dry_run=True)
    )
    assert any("ya declara roles" in w for w in result.plan.warnings)
    sent = next(f for f in result.plan.files if f.path.endswith("perfil-fiscal-y-regimen.md"))
    assert sent.data == perfil.read_bytes()


def test_single_non_markdown_cannot_carry_roles(brain: Brain, store: FakeStore) -> None:
    result = publish.run_publish(
        DirectClient(store),
        opts(brain, knowledge=[(brain.knowledge / "ledger-facturas.json", "fiscal")], knowledge_roles=["revisor"]),
    )
    assert any("solo un .md puede llevar roles" in w for w in result.plan.warnings)
    assert "knowledge/fiscal/ledger-facturas.json" in store.files
    assert not any(p.endswith("_acl.yaml") for p in store.files)


def test_directory_mapping_warns_that_acl_covers_the_whole_directory(brain: Brain, store: FakeStore) -> None:
    result = publish.run_publish(
        DirectClient(store),
        opts(brain, knowledge=[(brain.knowledge / "templates", "fiscal/templates")], knowledge_roles=["revisor"]),
    )
    assert any("_acl.yaml" in w and "todo ese directorio" in w for w in result.plan.warnings)
