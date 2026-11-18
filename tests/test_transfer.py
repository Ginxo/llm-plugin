"""`transfer`: subida y bajada de ficheros grandes por trozos (contrato §7.4, hito H5)."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from baas_plugin import transfer
from baas_plugin.errors import McpToolError, NetworkError, PackageError
from fake_publish_server import DirectClient, FakeStore

MIB = 1024 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer.time, "sleep", lambda _s: None)


@pytest.fixture
def store() -> FakeStore:
    s = FakeStore()
    s.chunk_size, s.max_chunk = 1000, 2000
    return s


def calls(store: FakeStore, name: str) -> list[dict[str, Any]]:
    return [a for n, a in store.calls if n == name]


# -- política ---------------------------------------------------------------------------------------


def test_thresholds_are_the_documented_ones() -> None:
    assert transfer.CHUNK_THRESHOLD == 4 * MIB
    assert transfer.INLINE_BUDGET == 8 * MIB
    assert not transfer.is_large(4 * MIB)
    assert transfer.is_large(4 * MIB + 1)


def test_split_by_budget_sends_big_files_by_chunks_and_caps_the_inline_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer, "CHUNK_THRESHOLD", 100)
    monkeypatch.setattr(transfer, "INLINE_BUDGET", 250)
    sizes = {"a": 50, "grande": 101, "b": 100, "c": 90, "d": 10, "e": 100}
    inline, chunked = transfer.split_by_budget(list(sizes), sizes.__getitem__)
    assert sum(sizes[k] for k in inline) <= 250
    assert set(inline) | set(chunked) == set(sizes)
    assert not set(inline) & set(chunked)
    assert inline == ["a", "b", "c", "d"]
    assert chunked == ["grande", "e"]


def test_everything_small_stays_inline() -> None:
    items = [("x", 10), ("y", 20)]
    assert transfer.split_by_budget(items, lambda i: i[1]) == (items, [])


# -- subir --------------------------------------------------------------------------------------------


def test_upload_bytes_in_the_servers_chunks(store: FakeStore) -> None:
    data = os.urandom(3500)
    upload_id = transfer.upload(DirectClient(store), "skills/s/data/a.bin", data, sha(data))
    assert bytes(store.uploads[upload_id]["data"]) == data
    puts = calls(store, "baas_put_chunk")
    assert [p["offset"] for p in puts] == [0, 1000, 2000, 3000]
    assert all(p["sha256"] == sha(base64.b64decode(p["content"])) for p in puts)
    begun = calls(store, "baas_begin_upload")[0]
    assert begun == {"path": "skills/s/data/a.bin", "size": 3500, "sha256": sha(data)}


def test_upload_from_a_file_reads_it_in_pieces(store: FakeStore, tmp_path: Path) -> None:
    data = os.urandom(2500)
    source = tmp_path / "a.bin"
    source.write_bytes(data)
    upload_id = transfer.upload(DirectClient(store), "knowledge/a/a.bin", source, sha(data), org="demo")
    assert bytes(store.uploads[upload_id]["data"]) == data
    assert all(a["org"] == "demo" for n, a in store.calls)


def test_upload_never_exceeds_the_servers_maximum_chunk(store: FakeStore) -> None:
    store.chunk_size, store.max_chunk = 5000, 1500  # una recomendación por encima del máximo (no debería pasar)
    data = os.urandom(4000)
    transfer.upload(DirectClient(store), "knowledge/a/a.bin", data, sha(data))
    assert all(len(base64.b64decode(a["content"])) <= 1500 for a in calls(store, "baas_put_chunk"))


def test_an_empty_file_needs_no_chunks(store: FakeStore) -> None:
    upload_id = transfer.upload(DirectClient(store), "skills/s/.gitkeep", b"", sha(b""))
    assert calls(store, "baas_put_chunk") == []
    assert store.uploads[upload_id]["size"] == 0


def test_a_lost_response_resumes_where_the_server_is(store: FakeStore) -> None:
    store.lose_put_responses = 1  # el servidor aplica el primer trozo y la respuesta no llega
    data = os.urandom(3500)
    upload_id = transfer.upload(DirectClient(store), "knowledge/a/a.bin", data, sha(data))
    assert bytes(store.uploads[upload_id]["data"]) == data
    puts = calls(store, "baas_put_chunk")
    assert [p["offset"] for p in puts] == [0, 0, 1000, 2000, 3000]  # el reintento del primero recibió `received`


def test_a_corrupt_chunk_is_sent_again(store: FakeStore) -> None:
    store.corrupt_chunk_once = 1
    data = os.urandom(1500)
    upload_id = transfer.upload(DirectClient(store), "knowledge/a/a.bin", data, sha(data))
    assert bytes(store.uploads[upload_id]["data"]) == data


def test_network_failures_give_up_after_the_retries_and_abort_the_upload(store: FakeStore) -> None:
    client = DirectClient(store)
    puts = {"n": 0}

    class Down:
        def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            if name == "baas_put_chunk":
                puts["n"] += 1
                raise NetworkError("sin conexión")
            return client.call_tool(name, args)

    data = os.urandom(1500)
    with pytest.raises(NetworkError):
        transfer.upload(Down(), "knowledge/a/a.bin", data, sha(data))
    assert puts["n"] == transfer.RETRIES + 1
    assert store.uploads == {}  # se descartó


def test_a_refusal_aborts_the_upload_and_propagates(store: FakeStore) -> None:
    store.kill_scope = None
    client = DirectClient(store)
    data = os.urandom(2500)

    class Flaky:
        def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            if name == "baas_put_chunk" and args["offset"] == 1000:
                store.kill_scope = "ORG_PAUSED"
            return client.call_tool(name, args)

    with pytest.raises(McpToolError) as exc:
        transfer.upload(Flaky(), "knowledge/a/a.bin", data, sha(data))
    assert exc.value.code == "KILL_SWITCH_ACTIVE"
    store.kill_scope = None
    assert store.uploads == {} or all(len(u["data"]) < 2500 for u in store.uploads.values())


def test_a_file_that_shrinks_while_uploading_is_an_error(store: FakeStore, tmp_path: Path) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(os.urandom(1200))
    with pytest.raises(PackageError, match="cambió mientras se subía"):
        transfer.upload(DirectClient(store), "knowledge/a/a.bin", source, "0" * 64, size=3000)
    assert store.uploads == {}


def test_a_server_that_does_not_advance_is_an_error() -> None:
    class Stuck:
        def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            if name == "baas_begin_upload":
                return {"upload_id": "u" * 24, "chunk_size": 100, "max_chunk_size": 100}
            if name == "baas_put_chunk":
                return {"upload_id": "u" * 24, "received": 0, "size": 500, "complete": False}
            return {"ok": True}

    with pytest.raises(PackageError, match="no avanza"):
        transfer.upload(Stuck(), "knowledge/a/a.bin", b"x" * 500, sha(b"x" * 500))


def test_abort_never_fails(store: FakeStore) -> None:
    transfer.abort(DirectClient(store), ["no-existe"])
    store.kill_scope = "GLOBAL"
    transfer.abort(DirectClient(store), ["otra"])  # ni siquiera con el kill switch


# -- bajar --------------------------------------------------------------------------------------------


def test_download_assembles_the_file_and_returns_its_sha256(store: FakeStore) -> None:
    data = os.urandom(4500)
    store.put("skills/s/data/a.bin", data)
    store.put("skills/s/SKILL.md", "---\nroles: ['*']\n---\nx")
    sink = io.BytesIO()
    digest = transfer.download(DirectClient(store), "skills/s/data/a.bin", sink, size=len(data), sha256=sha(data))
    assert sink.getvalue() == data
    assert digest == sha(data)
    reads = calls(store, "baas_read_chunk")
    assert [r["offset"] for r in reads] == [0, 1000, 2000, 3000, 4000]
    assert all(r["expected_sha256"] == sha(data) and "length" not in r for r in reads)


def test_download_of_an_empty_file(store: FakeStore) -> None:
    store.put("skills/s/.gitkeep", b"")
    store.put("skills/s/SKILL.md", "---\nroles: ['*']\n---\nx")
    sink = io.BytesIO()
    assert transfer.download(DirectClient(store), "skills/s/.gitkeep", sink, size=0, sha256=sha(b"")) == sha(b"")
    assert sink.getvalue() == b""


def test_download_returns_the_digest_of_what_arrived_so_corruption_is_visible(store: FakeStore) -> None:
    data = os.urandom(2500)
    store.put("skills/s/data/a.bin", data)
    store.put("skills/s/SKILL.md", "---\nroles: ['*']\n---\nx")
    store.corrupt_reads["skills/s/data/a.bin"] = bytes([data[0] ^ 1]) + data[1:]
    sink = io.BytesIO()
    digest = transfer.download(DirectClient(store), "skills/s/data/a.bin", sink, size=len(data), sha256=sha(data))
    assert digest != sha(data)  # el que llama compara y se niega a usarlo


def test_a_file_replaced_between_chunks_is_not_mixed(store: FakeStore) -> None:
    data = os.urandom(3000)
    store.put("skills/s/data/a.bin", data)
    store.put("skills/s/SKILL.md", "---\nroles: ['*']\n---\nx")
    client = DirectClient(store)

    class Replacer:
        def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            out = client.call_tool(name, args)
            if name == "baas_read_chunk" and args["offset"] == 0:
                store.put("skills/s/data/a.bin", os.urandom(3000))
            return out

    with pytest.raises(PackageError, match="cambió en el servidor"):
        transfer.download(Replacer(), "skills/s/data/a.bin", io.BytesIO(), size=3000, sha256=sha(data))


def test_download_refuses_more_bytes_than_the_manifest_declares(store: FakeStore) -> None:
    data = os.urandom(2500)
    store.put("skills/s/data/a.bin", data)
    store.put("skills/s/SKILL.md", "---\nroles: ['*']\n---\nx")
    with pytest.raises(PackageError, match="más bytes"):
        transfer.download(DirectClient(store), "skills/s/data/a.bin", io.BytesIO(), size=2000, sha256=sha(data))
    with pytest.raises(PackageError, match="declara 3000"):
        transfer.download(DirectClient(store), "skills/s/data/a.bin", io.BytesIO(), size=3000, sha256=sha(data))


def test_download_retries_network_failures_and_propagates_refusals(store: FakeStore) -> None:
    data = os.urandom(1500)
    store.put("skills/s/data/a.bin", data)
    store.put("skills/s/SKILL.md", "---\nroles: [facturacion]\n---\nx")
    client = DirectClient(store)
    failures = {"n": 2}

    class Flaky:
        def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            if failures["n"] > 0:
                failures["n"] -= 1
                raise NetworkError("corte")
            return client.call_tool(name, args)

    sink = io.BytesIO()
    transfer.download(Flaky(), "skills/s/data/a.bin", sink, size=len(data), sha256=sha(data))
    assert sink.getvalue() == data
    store.admin, store.user_roles = False, {"otro"}
    with pytest.raises(McpToolError) as exc:
        transfer.download(client, "skills/s/data/a.bin", io.BytesIO(), size=len(data), sha256=sha(data))
    assert exc.value.code == "AUTHORIZATION_DENIED"


# -- memoria del cliente ---------------------------------------------------------------------------------


class DiskServer:
    """Un servidor que guarda en disco, para medir la memoria del cliente sin contar la del servidor."""

    def __init__(self, root: Path, chunk: int) -> None:
        self.root, self.chunk = root, chunk
        self.size = 0

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        part = self.root / "up.part"
        if name == "baas_begin_upload":
            part.write_bytes(b"")
            self.size = args["size"]
            return {"upload_id": "u" * 24, "chunk_size": self.chunk, "max_chunk_size": self.chunk}
        if name == "baas_put_chunk":
            with part.open("ab") as fh:
                fh.write(base64.b64decode(args["content"]))
            return {"upload_id": "u" * 24, "received": part.stat().st_size, "size": self.size, "complete": False}
        if name == "baas_read_chunk":
            with part.open("rb") as fh:
                fh.seek(args["offset"])
                piece = fh.read(self.chunk)
            return {
                "offset": args["offset"],
                "eof": args["offset"] + len(piece) >= part.stat().st_size,
                "encoding": "base64",
                "content": base64.b64encode(piece).decode(),
            }
        raise AssertionError(name)


def test_client_memory_does_not_grow_with_the_file(tmp_path: Path) -> None:
    """Subir y bajar 64 MiB con trozos de 2 MiB: el pico de Python es de pocos MiB, no de 64+."""
    source = tmp_path / "grande.bin"
    block = os.urandom(MIB)
    digest = hashlib.sha256()
    with source.open("wb") as fh:
        for _ in range(64):
            fh.write(block)
            digest.update(block)
    total = 64 * MIB
    server = DiskServer(tmp_path, 2 * MIB)
    sink_path = tmp_path / "bajado.bin"
    tracemalloc.start()
    try:
        transfer.upload(server, "skills/s/data/grande.bin", source, digest.hexdigest())
        with sink_path.open("wb") as sink:
            got = transfer.download(server, "skills/s/data/grande.bin", sink, size=total, sha256=digest.hexdigest())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got == digest.hexdigest()
    assert sink_path.stat().st_size == total
    assert peak < 24 * MIB, f"pico de {peak / MIB:.1f} MiB para un fichero de 64 MiB"
