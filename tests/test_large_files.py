"""Ficheros grandes en `publish`, `export` y `package_fetch` (contrato §7.4, hito H5, D64).

Lo que pasa del umbral va por trozos (`transfer`); lo pequeño sigue en el lote inline de siempre. El servidor simulado
(`FakeStore`) falla si recibe o devuelve un fichero grande inline (`inline_write_limit` / `inline_read_limit`): así
una prueba en verde demuestra que el fichero grande no viajó en un mensaje.
"""

from __future__ import annotations

import hashlib
import os
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from baas_plugin import export, package_fetch, publish, transfer
from baas_plugin.errors import McpToolError, ScriptIntegrityError
from baas_plugin.export import ExportOptions
from baas_plugin.publish import PublishOptions
from fake_publish_server import DirectClient, FakeStore

MIB = 1024 * 1024
BIG = 3000  # el umbral de las pruebas (bytes)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def small_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer, "CHUNK_THRESHOLD", BIG)
    monkeypatch.setattr(transfer, "INLINE_BUDGET", 5000)
    monkeypatch.setattr(transfer.time, "sleep", lambda _s: None)


@pytest.fixture
def store() -> FakeStore:
    s = FakeStore()
    s.chunk_size, s.max_chunk = 1000, 2000
    s.inline_write_limit = BIG  # ningún fichero grande puede viajar inline
    s.inline_read_limit = BIG
    return s


def make_skill(tmp_path: Path, blob: bytes, extra: dict[str, bytes] | None = None) -> Path:
    skill = tmp_path / "grande"
    (skill / "data").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: grande\ndescription: Con un fichero grande\n---\nUsa data/blob.bin\n")
    (skill / "data" / "blob.bin").write_bytes(blob)
    for rel, data in (extra or {}).items():
        target = skill / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return skill


def options(skill: Path, **kw: Any) -> PublishOptions:
    kw.setdefault("roles", ["facturacion"])
    return PublishOptions(skill_dir=skill, **kw)


def names(store: FakeStore, tool: str) -> list[dict[str, Any]]:
    return [a for n, a in store.calls if n == tool]


def committed_write(store: FakeStore) -> dict[str, Any]:
    [call] = [a for a in names(store, "baas_write_files") if not a.get("dry_run")]
    return call


# -- publish -----------------------------------------------------------------------------------------------


def test_publish_sends_the_big_file_by_chunks_and_the_rest_inline(store: FakeStore, tmp_path: Path) -> None:
    blob = os.urandom(10_500)
    result = publish.run_publish(DirectClient(store), options(make_skill(tmp_path, blob)))
    assert result.verified is True
    assert store.files["skills/grande/data/blob.bin"] == blob
    entries = {e["path"]: e for e in committed_write(store)["files"]}
    assert set(entries["skills/grande/data/blob.bin"]) == {"path", "upload_id", "expected_sha256"}
    assert entries["skills/grande/data/blob.bin"]["expected_sha256"] == "none"
    assert "content" in entries["skills/grande/SKILL.md"]
    assert "upload_id" not in entries["skills/grande/SKILL.md"]
    assert len(names(store, "baas_put_chunk")) == 11  # trozos de 1000 bytes
    assert store.uploads == {}  # consumida al confirmar el lote


def test_it_is_still_one_atomic_batch(store: FakeStore, tmp_path: Path) -> None:
    publish.run_publish(DirectClient(store), options(make_skill(tmp_path, os.urandom(8000))))
    assert len([1 for a in names(store, "baas_write_files") if not a.get("dry_run")]) == 1


def test_small_packages_are_unchanged_no_uploads(store: FakeStore, tmp_path: Path) -> None:
    publish.run_publish(DirectClient(store), options(make_skill(tmp_path, os.urandom(500))))
    assert names(store, "baas_begin_upload") == []
    assert names(store, "baas_put_chunk") == []
    assert all("upload_id" not in e for e in committed_write(store)["files"])


def test_a_dry_run_uploads_nothing_and_says_what_it_skipped(store: FakeStore, tmp_path: Path) -> None:
    skill = make_skill(tmp_path, os.urandom(9000))
    result = publish.run_publish(DirectClient(store), options(skill, dry_run=True))
    assert names(store, "baas_begin_upload") == []
    [call] = names(store, "baas_write_files")
    assert call["dry_run"] is True
    assert {e["path"] for e in call["files"]} == {"skills/grande/SKILL.md", "skills/grande/baas.yaml"}
    assert result.server["chunked"] == [{"path": "skills/grande/data/blob.bin", "size": 9000}]
    text = publish.render_plan(result)
    assert "por trozos" in text
    assert "skills/grande/data/blob.bin" in text
    assert "skills/grande/data/blob.bin" not in store.files


def test_the_interactive_preview_then_the_real_publication_uploads_once(store: FakeStore, tmp_path: Path) -> None:
    skill = make_skill(tmp_path, os.urandom(9000))
    seen: list[publish.PublishResult] = []

    def confirm(preview: publish.PublishResult) -> bool:
        seen.append(preview)
        return True

    publish.run_publish(DirectClient(store), options(skill), confirm=confirm)
    assert seen
    assert seen[0].dry_run
    assert len(names(store, "baas_begin_upload")) == 1


def test_an_unchanged_big_file_is_not_uploaded_again(store: FakeStore, tmp_path: Path) -> None:
    skill = make_skill(tmp_path, os.urandom(9000))
    publish.run_publish(DirectClient(store), options(skill))
    store.calls.clear()
    result = publish.run_publish(DirectClient(store), options(skill))
    assert names(store, "baas_begin_upload") == []
    assert names(store, "baas_write_files") == []
    assert result.plan.counts == {"new": 0, "changed": 0, "unchanged": 3}


def test_a_changed_big_file_is_guarded_with_the_previous_hash(store: FakeStore, tmp_path: Path) -> None:
    skill = make_skill(tmp_path, os.urandom(9000))
    publish.run_publish(DirectClient(store), options(skill))
    old = store.files["skills/grande/data/blob.bin"]
    store.calls.clear()
    new = os.urandom(9500)
    (skill / "data" / "blob.bin").write_bytes(new)
    publish.run_publish(DirectClient(store), options(skill))
    entry = next(e for e in committed_write(store)["files"] if e["path"].endswith("blob.bin"))
    assert entry["expected_sha256"] == sha(old)
    assert store.files["skills/grande/data/blob.bin"] == new
    assert [p for p, _, _ in store.versions if p.endswith("blob.bin")].count("skills/grande/data/blob.bin") == 2


def test_a_conflict_on_the_big_file_aborts_its_upload(store: FakeStore, tmp_path: Path) -> None:
    skill = make_skill(tmp_path, os.urandom(9000))
    client = DirectClient(store)

    class Racy:
        """Otro administrador publica el fichero justo después de que se planificara."""

        def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            if name == "baas_begin_upload":
                store.put("skills/grande/data/blob.bin", b"de otro")
            return client.call_tool(name, args)

    with pytest.raises(McpToolError) as exc:
        publish.run_publish(Racy(), options(skill))
    assert exc.value.code == "CONFLICT"
    assert store.uploads == {}  # no queda ninguna carga a medias
    assert store.files["skills/grande/data/blob.bin"] == b"de otro"


def test_a_rejected_batch_aborts_every_upload(store: FakeStore, tmp_path: Path) -> None:
    skill = make_skill(tmp_path, os.urandom(9000))
    store.org_roles = {"analista"}  # el rol del lote no existe: el servidor rechaza el lote
    with pytest.raises(McpToolError):
        publish.run_publish(DirectClient(store), options(skill, roles=["facturacion"]))
    assert store.uploads == {}
    assert store.files == {}


def test_small_files_beyond_the_inline_budget_go_by_chunks_too(store: FakeStore, tmp_path: Path) -> None:
    extra = {f"data/pequeño{i}.bin": bytes([i]) * 1000 for i in range(10)}  # 10 000 bytes: más que el presupuesto
    skill = make_skill(tmp_path, b"\x01" * 100, extra)
    publish.run_publish(DirectClient(store), options(skill))
    entries = committed_write(store)["files"]
    inline = [e for e in entries if "content" in e]
    staged = [e for e in entries if "upload_id" in e]
    assert sum(len(e["content"]) for e in inline) <= 5000 * 4 // 3 + 200  # el mensaje inline sigue acotado
    assert staged, "lo que no cabe en el presupuesto del mensaje va por trozos"
    for rel, data in extra.items():
        assert store.files[f"skills/grande/{rel}"] == data


def test_the_plan_does_not_load_big_files_into_memory(store: FakeStore, tmp_path: Path, monkeypatch) -> None:
    """El plan de un fichero grande no guarda sus bytes y su memoria no crece con el tamaño (la inspección lee, como
    mucho, 16 MiB de cada fichero)."""
    monkeypatch.setattr(transfer, "CHUNK_THRESHOLD", 4 * MIB)
    block = os.urandom(MIB)
    store.inline_write_limit = None

    def plan_peak(megabytes: int) -> tuple[int, publish.PlanFile]:
        skill = make_skill(tmp_path / f"s{megabytes}", b"")
        with (skill / "data" / "blob.bin").open("wb") as fh:
            for _ in range(megabytes):
                fh.write(block)
        tracemalloc.start()
        try:
            plan = publish.build_plan(DirectClient(store), options(skill))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return peak, next(f for f in plan.files if f.path.endswith("blob.bin"))

    small_peak, small = plan_peak(40)
    big_peak, big = plan_peak(120)
    assert big.size == 120 * MIB
    assert big.data == b""
    assert big.source is not None
    assert small.data == b""
    assert small.size == 40 * MIB
    assert big_peak < 100 * MIB, f"el plan de un fichero de 120 MiB pesó {big_peak / MIB:.1f} MiB"
    assert abs(big_peak - small_peak) < 8 * MIB  # no crece con el tamaño del fichero


def test_a_file_over_the_limit_is_still_refused_before_uploading(store: FakeStore, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(publish, "MAX_FILE_BYTES", 5000)
    with pytest.raises(Exception, match="no admite ficheros de más de") as exc:
        publish.run_publish(DirectClient(store), options(make_skill(tmp_path, os.urandom(6000))))
    assert "MiB" in str(exc.value)
    assert names(store, "baas_begin_upload") == []


def test_the_large_file_warning_is_still_given(store: FakeStore, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(publish, "LARGE_FILE_BYTES", 5000)
    result = publish.run_publish(DirectClient(store), options(make_skill(tmp_path, os.urandom(9000))))
    assert any(w.startswith("LARGE_FILE: skills/grande/data/blob.bin") for w in result.plan.warnings)


# -- export ----------------------------------------------------------------------------------------------


def published(store: FakeStore, tmp_path: Path, blob: bytes) -> Path:
    skill = make_skill(tmp_path / "origen", blob)
    publish.run_publish(DirectClient(store), options(skill))
    return skill


def test_export_reads_the_big_file_by_chunks_and_the_rest_inline(store: FakeStore, tmp_path: Path) -> None:
    blob = os.urandom(10_500)
    published(store, tmp_path, blob)
    store.calls.clear()
    dest = tmp_path / "salida"
    result = export.run_export(DirectClient(store), ExportOptions(skill="grande", dest=dest))
    assert (dest / "data" / "blob.bin").read_bytes() == blob
    assert result.package_hash_ok is True
    asked = [p for a in names(store, "baas_get_skill_files") for p in a["paths"]]
    assert "skills/grande/data/blob.bin" not in asked
    assert "skills/grande/SKILL.md" in asked
    assert {a["path"] for a in names(store, "baas_read_chunk")} == {"skills/grande/data/blob.bin"}
    assert sorted(f.rel for f in result.files) == ["SKILL.md", "baas.yaml", "data/blob.bin"]


def test_export_verifies_the_big_file_and_writes_nothing_if_it_is_corrupt(store: FakeStore, tmp_path: Path) -> None:
    blob = os.urandom(8000)
    published(store, tmp_path, blob)
    store.corrupt_reads["skills/grande/data/blob.bin"] = bytes([blob[0] ^ 1]) + blob[1:]
    dest = tmp_path / "salida"
    with pytest.raises(ScriptIntegrityError, match="no coincide"):
        export.run_export(DirectClient(store), ExportOptions(skill="grande", dest=dest))
    assert not dest.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".baas-export-")]


def test_export_of_a_big_executable_keeps_its_bit(store: FakeStore, tmp_path: Path) -> None:
    script = b"#!/bin/sh\n" + b"# relleno\n" * 600
    skill = make_skill(tmp_path / "origen", b"x", {"scripts/grande.sh": script})
    publish.run_publish(DirectClient(store), options(skill))
    dest = tmp_path / "salida"
    export.run_export(DirectClient(store), ExportOptions(skill="grande", dest=dest))
    assert (dest / "scripts" / "grande.sh").read_bytes() == script
    assert os.access(dest / "scripts" / "grande.sh", os.X_OK)


# -- package_fetch -----------------------------------------------------------------------------------------


def test_fetch_contents_reads_big_files_by_chunks(store: FakeStore, tmp_path: Path) -> None:
    blob = os.urandom(9000)
    published(store, tmp_path, blob)
    store.calls.clear()
    client = DirectClient(store)
    manifest = package_fetch.fetch_manifest(client, "grande")
    contents = package_fetch.fetch_contents(client, manifest)
    assert contents["data/blob.bin"] == blob
    assert "SKILL.md" in contents
    assert all("skills/grande/data/blob.bin" not in a["paths"] for a in names(store, "baas_get_skill_files"))


def test_fetch_to_dir_writes_the_tree_with_modes_and_verifies_hashes(store: FakeStore, tmp_path: Path) -> None:
    blob = os.urandom(9000)
    skill = make_skill(tmp_path / "origen", blob, {"scripts/run.sh": b"#!/bin/sh\necho hola\n"})
    publish.run_publish(DirectClient(store), options(skill))
    client = DirectClient(store)
    manifest = package_fetch.fetch_manifest(client, "grande")
    dest = tmp_path / "destino"
    package_fetch.fetch_to_dir(client, manifest, dest)
    assert (dest / "data" / "blob.bin").read_bytes() == blob
    assert (dest / "scripts" / "run.sh").read_bytes() == b"#!/bin/sh\necho hola\n"
    assert os.stat(dest / "data" / "blob.bin").st_mode & 0o777 == 0o600
    assert os.access(dest / "scripts" / "run.sh", os.X_OK)
    assert not (dest / "baas.yaml").exists()  # el manifiesto del MCP no es parte de la skill instalada


def test_fetch_to_dir_refuses_a_corrupt_big_file(store: FakeStore, tmp_path: Path) -> None:
    blob = os.urandom(9000)
    published(store, tmp_path, blob)
    store.corrupt_reads["skills/grande/data/blob.bin"] = blob[::-1]
    client = DirectClient(store)
    manifest = package_fetch.fetch_manifest(client, "grande")
    with pytest.raises(ScriptIntegrityError, match="no coincide"):
        package_fetch.fetch_to_dir(client, manifest, tmp_path / "destino")


def test_fetch_to_dir_uses_no_memory_for_big_files(store: FakeStore, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(transfer, "CHUNK_THRESHOLD", 4 * MIB)
    block = os.urandom(MIB)
    blob_path = tmp_path / "blob.bin"
    digest = hashlib.sha256()
    with blob_path.open("wb") as fh:
        for _ in range(48):
            fh.write(block)
            digest.update(block)
    total = 48 * MIB
    size_chunk = 2 * MIB

    class DiskServer:
        """El manifiesto y los trozos de un fichero de 48 MiB que vive en disco."""

        def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
            import base64

            if name == "baas_get_skill_package":
                return {
                    "skill": "grande",
                    "status": "active",
                    "package_sha256": "x",
                    "metadata": {},
                    "files": [{"path": "skills/grande/blob.bin", "sha256": digest.hexdigest(), "size": total}],
                }
            assert name == "baas_read_chunk", name
            with blob_path.open("rb") as fh:
                fh.seek(args["offset"])
                piece = fh.read(size_chunk)
            return {
                "offset": args["offset"],
                "eof": args["offset"] + len(piece) >= total,
                "encoding": "base64",
                "content": base64.b64encode(piece).decode(),
            }

    manifest = package_fetch.fetch_manifest(DiskServer(), "grande")
    tracemalloc.start()
    try:
        package_fetch.fetch_to_dir(DiskServer(), manifest, tmp_path / "destino")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (tmp_path / "destino" / "blob.bin").stat().st_size == total
    assert sha((tmp_path / "destino" / "blob.bin").read_bytes()) == digest.hexdigest()
    assert peak < 24 * MIB, f"se descargaron 48 MiB con un pico de {peak / MIB:.1f} MiB"
