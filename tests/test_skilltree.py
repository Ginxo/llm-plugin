from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from baas_plugin import skilltree
from baas_plugin.skilltree import check_store_path, encode_content, package_sha256, scan_tree


def write(root: Path, rel: str, data: bytes | str = "x\n", mode: int | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    if mode is not None:
        path.chmod(mode)
    return path


def rels(scan) -> list[str]:
    return [f.rel for f in scan.files]


def test_scan_lists_every_file_sorted_with_any_extension_and_depth(tmp_path: Path) -> None:
    write(tmp_path, "SKILL.md")
    write(tmp_path, "scripts/a/b/c/deep.go")
    write(tmp_path, "templates/informe.docx", b"PK\x03\x04\x00")
    write(tmp_path, "references/x.unknownext")
    scan = scan_tree(tmp_path)
    assert rels(scan) == [
        "SKILL.md",
        "references/x.unknownext",
        "scripts/a/b/c/deep.go",
        "templates/informe.docx",
    ]
    assert all(f.sha256 == hashlib.sha256(f.read_bytes()).hexdigest() for f in scan.files)
    assert {f.rel: f.size for f in scan.files}["templates/informe.docx"] == 5


def test_hidden_files_are_included_and_flagged(tmp_path: Path) -> None:
    write(tmp_path, "SKILL.md")
    write(tmp_path, ".gitkeep", "")
    write(tmp_path, "assets/.env.example", "A=1\n")
    write(tmp_path, ".oculta/dentro.txt")
    scan = scan_tree(tmp_path)
    assert {f.rel: f.hidden for f in scan.files} == {
        ".gitkeep": True,
        ".oculta/dentro.txt": True,
        "SKILL.md": False,
        "assets/.env.example": True,
    }


def test_default_excludes_and_custom_excludes(tmp_path: Path) -> None:
    write(tmp_path, "SKILL.md")
    write(tmp_path, "scripts/x.py")
    write(tmp_path, "scripts/__pycache__/x.cpython-313.pyc")
    write(tmp_path, "scripts/y.pyc")
    write(tmp_path, ".git/config")
    write(tmp_path, ".DS_Store")
    write(tmp_path, "node_modules/m/i.js")
    write(tmp_path, "logs/run.log")
    write(tmp_path, "build/out.bin")
    scan = scan_tree(tmp_path, excludes=["*.log", "build/"])
    assert rels(scan) == ["SKILL.md", "scripts/x.py"]
    assert scan.ignored >= 6
    everything = scan_tree(tmp_path, default_excludes=False)
    assert ".git/config" in rels(everything)
    assert "scripts/__pycache__/x.cpython-313.pyc" in rels(everything)


def test_exec_bit_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "scripts/run.sh", "#!/bin/sh\n", mode=0o755)
    write(tmp_path, "scripts/mod.py", "x = 1\n", mode=0o644)
    scan = {f.rel: f for f in scan_tree(tmp_path).files}
    assert scan["scripts/run.sh"].exec_bit is True
    assert scan["scripts/mod.py"].exec_bit is False


def test_symlinks_inside_the_tree_are_followed_and_outside_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    write(root, "SKILL.md")
    write(root, "real/data.txt", "datos\n")
    os.symlink("real/data.txt", root / "enlace.txt")
    os.symlink("real", root / "dir-enlace")
    outside = write(tmp_path, "secreto.txt", "no subir\n")
    os.symlink(outside, root / "fuera.txt")
    os.symlink("/etc", root / "etc-link")
    os.symlink("no-existe", root / "roto")
    scan = scan_tree(root)
    assert rels(scan) == ["SKILL.md", "dir-enlace/data.txt", "enlace.txt", "real/data.txt"]
    assert all(f.via_symlink for f in scan.files if f.rel in ("enlace.txt", "dir-enlace/data.txt"))
    reasons = {s.rel: s.reason for s in scan.skipped}
    assert reasons == {"fuera.txt": "symlink_outside", "etc-link": "symlink_outside", "roto": "symlink_broken"}
    assert "no subir" not in b"".join(f.read_bytes() for f in scan.files).decode()


def test_symlink_cycles_do_not_hang(tmp_path: Path) -> None:
    root = tmp_path / "skill"
    write(root, "SKILL.md")
    os.symlink(".", root / "bucle")
    scan = scan_tree(root)
    assert rels(scan) == ["SKILL.md"]


def test_special_files_are_skipped(tmp_path: Path) -> None:
    write(tmp_path, "SKILL.md")
    os.mkfifo(tmp_path / "tuberia")
    scan = scan_tree(tmp_path)
    assert rels(scan) == ["SKILL.md"]
    assert [(s.rel, s.reason) for s in scan.skipped] == [("tuberia", "special")]


def test_scan_of_a_single_file(tmp_path: Path) -> None:
    f = write(tmp_path, "solo.py", "print(1)\n")
    scan = scan_tree(f)
    assert rels(scan) == ["solo.py"]


@pytest.mark.parametrize(
    ("data", "encoding"),
    [
        (b"hola \xc3\xa1\n", "utf8"),
        (b"", "utf8"),
        (b"\xef\xbb\xbfcon BOM\n", "utf8"),
        (b"\xff\xfe binario", "base64"),
        (b"con \x00 nul", "base64"),
        (b"PK\x03\x04\x14\x00", "base64"),
        (b"latin1 \xe9\n", "base64"),
    ],
)
def test_encoding_is_utf8_only_for_valid_text_without_nul(data: bytes, encoding: str) -> None:
    content, enc = encode_content(data)
    assert enc == encoding
    if enc == "utf8":
        assert content.encode() == data
    else:
        import base64

        assert base64.b64decode(content) == data


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("skills/facturacion/SKILL.md", True),
        ("skills/f/.gitkeep", True),
        ("skills/f/a b/ñ.txt", True),
        ("skills/f/../x", False),
        ("/etc/passwd", False),
        ("skills/f//x", False),
        ("skills\\f\\x", False),
        ("skills/f/x\x00y", False),
        ("skills/f/x\ny", False),
        ("skills/f/./x", False),
        ("skills/f/\udcff.txt", False),  # nombre que no es UTF-8 válido
        ("", False),
        ("skills/f/" + "a" * 256, False),
        ("skills/" + "/".join(["abcdefgh"] * 200), False),
    ],
)
def test_store_path_rules_mirror_the_server(path: str, ok: bool) -> None:
    assert (check_store_path(path) is None) is ok


def test_package_sha256_is_the_hash_of_sorted_lines() -> None:
    files = {"b.txt": "2" * 64, "a/z.txt": "1" * 64, "A.md": "3" * 64}
    expected = hashlib.sha256(f"{'3' * 64}  A.md\n{'1' * 64}  a/z.txt\n{'2' * 64}  b.txt\n".encode()).hexdigest()
    assert package_sha256(files) == expected


def test_package_sha256_orders_by_utf8_bytes() -> None:
    files = {"é.txt": "1" * 64, "z.txt": "2" * 64}  # «é» (0xC3…) va después de «z» en bytes UTF-8
    text = f"{'2' * 64}  z.txt\n{'1' * 64}  é.txt\n"
    assert package_sha256(files) == hashlib.sha256(text.encode()).hexdigest()


def test_module_exposes_thresholds_from_the_contract() -> None:
    assert skilltree.LARGE_FILE_BYTES == 25 * 1024 * 1024
    assert skilltree.LARGE_PACKAGE_BYTES == 100 * 1024 * 1024
