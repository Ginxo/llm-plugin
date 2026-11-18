"""Descarga y materialización de paquetes de skills (contrato §7.4): común al ejecutor y a la sincronización."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_baas import FakeBaasServer, PkgFile

from baas_plugin import package_fetch
from baas_plugin.errors import McpToolError, PackageError, ScriptIntegrityError
from baas_plugin.mcp_client import McpClient

BINARY = bytes(range(256)) * 3  # no es UTF-8 válido


@pytest.fixture
def server() -> Iterator[FakeBaasServer]:
    with FakeBaasServer() as srv:
        srv.state.skills["demo"].clear()
        yield srv


def client(server: FakeBaasServer, key: str) -> McpClient:
    return McpClient(f"{server.url}/org/demo/mcp", key)


def publish(server: FakeBaasServer, **kwargs):
    files = {
        "SKILL.md": "---\nname: facturacion\ndescription: x\n---\n",
        "scripts/run.sh": "#!/bin/sh\necho hola\n",
        "scripts/lib/util.py": "X = 1\n",
        "templates/informe.docx": BINARY,
        ".env.example": "A=1\n",
    }
    return server.state.add_skill(
        "demo", "facturacion", files, roles=["analista"], executable=["scripts/run.sh"], **kwargs
    )


# --- rutas ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["SKILL.md", "scripts/a.py", "a/b/c/d.txt", ".env.example", "ñandú/x.md", "a b/c.md"])
def test_safe_rel_path_accepts_free_tree(path: str) -> None:
    assert package_fetch.safe_rel_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../x",
        "a/../b",
        "a/..",
        "/etc/passwd",
        "a//b",
        "a/./b",
        "./a",
        "a/",
        "a\\b",
        "C:/x",
        "a\x00b",
        "a\nb",
        "a/" + "x" * 256,
        "x/" * 600,
    ],
)
def test_safe_rel_path_rejects_threats(path: str) -> None:
    with pytest.raises(PackageError):
        package_fetch.safe_rel_path(path)


@pytest.mark.parametrize("name", ["facturacion", "md-to-doc", "a.b_c", "A1"])
def test_skill_name_ok(name: str) -> None:
    assert package_fetch.valid_skill_name(name)


@pytest.mark.parametrize("name", ["", ".oculta", "a:b", "a/b", "a b", "-x", "x" * 129, "..", "a\\b"])
def test_skill_name_rejected(name: str) -> None:
    assert not package_fetch.valid_skill_name(name)


def test_tree_conflicts_are_rejected() -> None:
    with pytest.raises(PackageError, match="fichero y directorio"):
        package_fetch.check_tree(["a", "a/b"])
    with pytest.raises(PackageError, match="mayúsculas"):
        package_fetch.check_tree(["Scripts/a.py", "scripts/A.py"])
    with pytest.raises(PackageError, match="fichero y directorio"):
        package_fetch.check_tree(["Foo", "foo/x"])
    package_fetch.check_tree(["a/b", "a/c", "d", "Scripts/a.py", "scripts/b.py"])


# --- manifiesto y ficheros -----------------------------------------------------------------------


@pytest.mark.parametrize("paths", ["store", "relative"])
def test_fetch_package_verifies_and_returns_relative_paths(server, analista_key, paths) -> None:
    server.state.package_paths = paths
    publish(server)
    with client(server, analista_key) as c:
        pkg = package_fetch.fetch_package(c, "facturacion")
    assert set(pkg.contents) == {
        "SKILL.md",
        "scripts/run.sh",
        "scripts/lib/util.py",
        "templates/informe.docx",
        ".env.example",
    }  # sin baas.yaml
    assert pkg.contents["templates/informe.docx"] == BINARY
    assert pkg.manifest.package_sha256 == server.state.skills["demo"]["facturacion"].package_sha256
    assert pkg.manifest.executable_paths() == {"scripts/run.sh"}
    for rel, data in pkg.contents.items():
        assert hashlib.sha256(data).hexdigest() == pkg.manifest.file(rel).sha256
    # Se pide al servidor la ruta tal como la declaró su manifiesto
    asked = server.state.tool_calls("baas_get_skill_files")[0][1]["paths"]
    prefix = "skills/facturacion/" if paths == "store" else ""
    assert prefix + "scripts/run.sh" in asked
    assert not any(p.endswith("baas.yaml") for p in asked)


def test_manifest_metadata_requires_and_dependencies(server, analista_key) -> None:
    server.state.add_skill("demo", "base", {"x.txt": "1"}, roles=["analista"])
    server.state.add_skill("demo", "oculta", {"x.txt": "1"}, roles=["revisor"])
    publish(server, depends_on=["base", "oculta", "no-existe"], requires={"binaries": ["typst"]})
    with client(server, analista_key) as c:
        manifest = package_fetch.fetch_manifest(c, "facturacion")
    assert manifest.requires == {"binaries": ["typst"]}
    assert {d.name: d.accessible for d in manifest.dependencies} == {
        "base": True,
        "oculta": False,
        "no-existe": False,
    }


def test_wrong_hash_raises_integrity_error(server, analista_key) -> None:
    pkg = publish(server)
    pkg.files["scripts/lib/util.py"] = PkgFile(b"X = 1\n", sha256_override="0" * 64)
    with client(server, analista_key) as c, pytest.raises(ScriptIntegrityError, match=r"scripts/lib/util\.py"):
        package_fetch.fetch_package(c, "facturacion")


def test_unsafe_manifest_path_is_rejected_before_downloading(server, analista_key) -> None:
    publish(server).files["../fuera.txt"] = PkgFile(b"x")
    with client(server, analista_key) as c, pytest.raises(PackageError, match=r"\.\./fuera.txt"):
        package_fetch.fetch_package(c, "facturacion")
    assert server.state.tool_calls("baas_get_skill_files") == []


def test_server_cannot_add_files_nobody_asked_for(server, analista_key, monkeypatch) -> None:
    publish(server)
    with client(server, analista_key) as c:
        manifest = package_fetch.fetch_manifest(c, "facturacion")
        real = c.call_tool

        def extra(name, arguments):
            data = real(name, arguments)
            if name == "baas_get_skill_files":
                data["files"].append({"path": "../../etc/x", "sha256": "0" * 64, "encoding": "utf8", "content": "x"})
            return data

        monkeypatch.setattr(c, "call_tool", extra)
        with pytest.raises(PackageError, match="no pedido"):
            package_fetch.fetch_contents(c, manifest)


def test_large_packages_are_downloaded_in_batches(server, analista_key, monkeypatch) -> None:
    publish(server)
    monkeypatch.setattr(package_fetch, "BATCH_BYTES", 10)
    with client(server, analista_key) as c:
        pkg = package_fetch.fetch_package(c, "facturacion")
    calls = server.state.tool_calls("baas_get_skill_files")
    assert len(calls) >= 4
    assert sum(len(a["paths"]) for _, a in calls) == len(pkg.contents)


def test_authorization_denied_and_not_found(server, analista_key) -> None:
    server.state.add_skill("demo", "solo-revisor", {"a": "1"}, roles=["revisor"])
    with client(server, analista_key) as c:
        with pytest.raises(McpToolError) as exc:
            package_fetch.fetch_manifest(c, "solo-revisor")
        assert exc.value.code == "AUTHORIZATION_DENIED"
        with pytest.raises(McpToolError) as exc:
            package_fetch.fetch_manifest(c, "no-existe")
        assert exc.value.code == "NOT_FOUND"


def test_org_argument_is_forwarded(server, analista_key) -> None:
    publish(server)
    with client(server, analista_key) as c:
        package_fetch.fetch_manifest(c, "facturacion", org="demo")
    assert server.state.tool_calls("baas_get_skill_package")[0][1] == {"skill": "facturacion", "org": "demo"}


# --- materialización -----------------------------------------------------------------------------


def test_materialize_writes_tree_and_only_declared_exec_bit(server, analista_key, tmp_path: Path) -> None:
    publish(server)
    with client(server, analista_key) as c:
        pkg = package_fetch.fetch_package(c, "facturacion")
    dest = tmp_path / "skill"
    package_fetch.materialize(dest, pkg.contents, pkg.manifest.executable_paths())
    assert (dest / "templates/informe.docx").read_bytes() == BINARY
    assert (dest / ".env.example").read_text() == "A=1\n"
    assert os.access(dest / "scripts/run.sh", os.X_OK)
    assert not os.access(dest / "scripts/lib/util.py", os.X_OK)
    assert not os.access(dest / "SKILL.md", os.X_OK)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert stat.S_IMODE((dest / "scripts").stat().st_mode) == 0o700


def test_materialize_never_writes_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    with pytest.raises(PackageError):
        package_fetch.materialize(root, {"../fuera.txt": b"x"}, set())
    assert not (tmp_path / "fuera.txt").exists()
