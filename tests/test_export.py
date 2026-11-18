"""`export`: la inversa de `publish`, con verificación de hashes y confinamiento de rutas."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from baas_plugin import export, publish
from baas_plugin.errors import ConfigError, McpToolError, ProjectSafetyError, ScriptIntegrityError
from baas_plugin.export import ExportOptions
from baas_plugin.publish import PublishOptions
from fake_publish_server import DirectClient, FakeStore
from skill_fixture import Brain, make_brain

SUGGESTED = ["facturacion.py", "render_doc.py", "md_to_doc.py", "gestor-facturas.py"]


@pytest.fixture
def brain(tmp_path: Path) -> Brain:
    return make_brain(tmp_path)


def published(brain: Brain, store: FakeStore | None = None) -> FakeStore:
    store = store or FakeStore()
    publish.run_publish(
        DirectClient(store),
        PublishOptions(skill_dir=brain.skill, roles=["facturacion"], scripts=[brain.scripts / n for n in SUGGESTED]),
    )
    return store


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("style", ["relative", "store"])
def test_export_returns_exactly_what_was_published(brain: Brain, tmp_path: Path, style: str) -> None:
    store = published(brain, FakeStore(path_style=style))
    dest = tmp_path / "salida" / "facturacion"
    result = export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest))
    exported = tree(dest)
    assert exported == {f"{k}": v for k, v in store.skill_files("facturacion").items()}
    assert {sha(v) for v in exported.values()} == {sha(v) for v in store.skill_files("facturacion").values()}
    assert (dest / "SKILL.md").read_bytes() == (brain.skill / "SKILL.md").read_bytes()
    assert (dest / "assets/logo.png").read_bytes() == (brain.skill / "assets/logo.png").read_bytes()
    assert (dest / "scripts/render_doc.py").read_bytes() == (brain.scripts / "render_doc.py").read_bytes()
    assert (dest / "baas.yaml").is_file()
    assert result.package_hash_ok is True
    assert result.package_sha256 == store._tool_baas_get_skill_package({"skill": "facturacion"})["package_sha256"]
    assert len(result.files) == len(exported)


def test_executable_bit_is_restored_from_the_manifest(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    dest = tmp_path / "out"
    export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest))
    assert os.access(dest / "scripts/facturacion.py", os.X_OK)
    assert os.access(dest / "scripts/render_doc.py", os.X_OK)  # tenía `#!` y no tenía bit: queda ejecutable
    assert not os.access(dest / "scripts/md_to_doc.py", os.X_OK)
    assert stat.S_IMODE((dest / "SKILL.md").stat().st_mode) == 0o644
    assert stat.S_IMODE((dest / "scripts/facturacion.py").stat().st_mode) == 0o755


def test_export_without_manifest_is_the_same_tree_as_the_original(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    dest = tmp_path / "out"
    export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest, include_manifest=False))
    assert not (dest / "baas.yaml").exists()
    for rel in ("SKILL.md", "references/normativa.md", "templates/informe.docx", ".env.example"):
        assert (dest / rel).read_bytes() == (brain.skill / rel).read_bytes()
    assert not (dest / ".env").exists()  # no se subió, no se exporta


def test_big_file_roundtrip(brain: Brain, tmp_path: Path) -> None:
    blob = os.urandom(26 * 1024 * 1024)
    (brain.skill / "assets" / "grande.bin").write_bytes(blob)
    store = published(brain)
    dest = tmp_path / "out"
    export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest))
    assert (dest / "assets/grande.bin").read_bytes() == blob


def test_reads_are_chunked(brain: Brain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = published(brain)
    monkeypatch.setattr(export, "CHUNK_FILES", 3)
    dest = tmp_path / "out"
    export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest))
    reads = [a for n, a in store.calls if n == "baas_get_skill_files"]
    assert len(reads) > 1
    assert all(len(a["paths"]) <= 3 for a in reads)
    assert tree(dest) == store.skill_files("facturacion")


# --- verificación --------------------


def test_hash_mismatch_writes_nothing(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    store.corrupt_reads["skills/facturacion/SKILL.md"] = "alterado en tránsito\n".encode()
    dest = tmp_path / "out"
    with pytest.raises(ScriptIntegrityError) as exc:
        export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest))
    assert "SKILL.md" in exc.value.message
    assert not dest.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".baas")] == []  # sin restos


# --- confinamiento de rutas --------------------


class EvilClient(DirectClient):
    """Un servidor hostil o roto que devuelve rutas peligrosas en el manifiesto del paquete."""

    def __init__(self, store: FakeStore, evil: str) -> None:
        super().__init__(store)
        self.evil = evil

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        out = super().call_tool(name, arguments)
        if name == "baas_get_skill_package":
            out["files"].append(
                {"path": self.evil, "sha256": sha(b"x"), "size": 1, "encoding": "utf8", "executable": False}
            )
        if name == "baas_get_skill_files":
            out["files"].append({"path": self.evil, "sha256": sha(b"x"), "size": 1, "encoding": "utf8", "content": "x"})
        return out


@pytest.mark.parametrize(
    "evil",
    [
        "../escape.txt",
        "a/../../escape.txt",
        "/tmp/escape.txt",
        "skills/facturacion/../../escape.txt",
        "a\\b.txt",
        "C:/windows/x.txt",
        "a//b.txt",
        "con\x00nul.txt",
        "",
        ".",
    ],
)
def test_unsafe_paths_are_refused_and_nothing_is_written(brain: Brain, tmp_path: Path, evil: str) -> None:
    store = published(brain)
    dest = tmp_path / "salida" / "out"
    with pytest.raises(ProjectSafetyError):
        export.run_export(EvilClient(store, evil), ExportOptions(skill="facturacion", dest=dest))
    assert not dest.exists()
    assert not (tmp_path / "salida" / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "salida").exists()


def test_existing_symlink_in_destination_is_never_followed(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    outside = tmp_path / "otro-sitio"
    outside.mkdir()
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "scripts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectSafetyError):
        export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest, force=True))
    assert list(outside.iterdir()) == []


# --- destino --------------------


def test_non_empty_destination_needs_force(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "mio.txt").write_text("mío\n")
    with pytest.raises(ConfigError) as exc:
        export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest))
    assert "--force" in (exc.value.hint or "")
    assert tree(dest) == {"mio.txt": "mío\n".encode()}
    export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest, force=True))
    assert (dest / "SKILL.md").is_file()
    assert (dest / "mio.txt").read_text() == "mío\n"  # lo que no es del paquete se respeta


def test_empty_existing_directory_is_fine(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    dest = tmp_path / "out"
    dest.mkdir()
    export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest))
    assert (dest / "SKILL.md").is_file()


def test_force_replaces_changed_files(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    dest = tmp_path / "out"
    export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest))
    (dest / "SKILL.md").write_text("editado a mano\n")
    export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest, force=True))
    assert (dest / "SKILL.md").read_bytes() == store.skill_files("facturacion")["SKILL.md"]


def test_destination_must_not_be_a_file(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    dest = tmp_path / "out"
    dest.write_text("soy un fichero")
    with pytest.raises(ConfigError):
        export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=dest, force=True))


# --- errores del MCP --------------------


def test_unknown_skill(tmp_path: Path) -> None:
    with pytest.raises(McpToolError) as exc:
        export.run_export(DirectClient(FakeStore()), ExportOptions(skill="nada", dest=tmp_path / "out"))
    assert exc.value.code == "NOT_FOUND"
    assert not (tmp_path / "out").exists()


def test_user_without_the_role_cannot_export(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    store.admin = False
    store.user_roles = {"analista"}
    with pytest.raises(McpToolError) as exc:
        export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=tmp_path / "out"))
    assert exc.value.code == "AUTHORIZATION_DENIED"
    assert not (tmp_path / "out").exists()


def test_user_with_the_role_can_export(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    store.admin = False
    store.user_roles = {"facturacion"}
    export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=tmp_path / "out"))
    assert (tmp_path / "out" / "SKILL.md").is_file()


def test_org_is_forwarded(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    store.calls.clear()
    export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=tmp_path / "out", org="demo"))
    assert store.calls
    assert all(a.get("org") == "demo" for _, a in store.calls)


def test_render_summary(brain: Brain, tmp_path: Path) -> None:
    store = published(brain)
    result = export.run_export(DirectClient(store), ExportOptions(skill="facturacion", dest=tmp_path / "out"))
    text = export.render_result(result)
    assert "facturacion" in text
    assert str(tmp_path / "out") in text
    assert "package_sha256" in text
    assert "scripts/facturacion.py" in text  # ejecutables
