"""Recorrido de un directorio de skill (o de sus scripts y conocimiento) y codificación de sus ficheros.

Solo lectura. Reglas (diseño H5 §5 y §9):
- Cualquier extensión y profundidad (paridad con un directorio de skill); los ficheros ocultos se incluyen.
- Se ignoran los residuos habituales (`.git`, `__pycache__`, `*.pyc`, `.DS_Store`, `node_modules`, `.venv`…)
  y lo que indique el usuario con `excludes`.
- Un enlace simbólico que apunta DENTRO del árbol se sigue; uno que apunta fuera (o roto) se omite y se avisa:
  nunca se sube nada de fuera del árbol.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_FILE_BYTES = 500 * 1024 * 1024  # único tope del MCP, por fichero (D64, contrato §7.4): se rechaza al planificar
LARGE_FILE_BYTES = 25 * 1024 * 1024  # umbral de aviso LARGE_FILE (contrato §7.4)
LARGE_PACKAGE_BYTES = 100 * 1024 * 1024  # umbral de aviso LARGE_PACKAGE

DEFAULT_EXCLUDES = (
    ".git/",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "Thumbs.db",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "node_modules/",
    ".venv/",
)
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class LocalFile:
    """Un fichero local. `rel` es relativo a su raíz de escaneo (siempre con `/`)."""

    rel: str
    path: Path  # fichero real a leer (tras seguir un enlace interno)
    size: int
    exec_bit: bool
    via_symlink: bool = False
    hidden: bool = False
    _sha256: str | None = field(default=None, repr=False)

    @property
    def sha256(self) -> str:
        if self._sha256 is None:
            self._sha256 = sha256_file(self.path)
        return self._sha256

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()


@dataclass(frozen=True)
class Skipped:
    rel: str
    reason: str  # symlink_outside | symlink_broken | special | unreadable
    target: str | None = None


@dataclass(frozen=True)
class LinkInfo:
    """Un enlace simbólico encontrado: `followed` (apunta dentro del árbol) o `skipped` (fuera o roto)."""

    rel: str
    target: str
    action: str
    reason: str | None = None


@dataclass
class ScanResult:
    root: Path
    files: list[LocalFile] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    links: list[LinkInfo] = field(default_factory=list)
    ignored: int = 0


def _excluded(rel: str, name: str, is_dir: bool, patterns: tuple[str, ...]) -> bool:
    for pat in patterns:
        only_dir = pat.endswith("/")
        pat = pat.rstrip("/")
        if only_dir and not is_dir:
            continue
        if fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(rel, pat):
            return True
    return False


def scan_tree(root: Path, *, excludes: list[str] | tuple[str, ...] = (), default_excludes: bool = True) -> ScanResult:
    """Lista los ficheros de `root` (un directorio, o un único fichero). No lee su contenido."""
    root = Path(root)
    real_root = Path(os.path.realpath(root))
    result = ScanResult(root=real_root)
    if real_root.is_file():
        st = real_root.stat()
        result.files.append(
            LocalFile(
                real_root.name,
                real_root,
                st.st_size,
                bool(st.st_mode & 0o111),
                hidden=real_root.name.startswith("."),
            )
        )
        return result
    patterns = (*(DEFAULT_EXCLUDES if default_excludes else ()), *excludes)

    def walk(directory: Path, prefix: str, via_link: bool, ancestors: frozenset[str]) -> None:
        real = os.path.realpath(directory)
        if real in ancestors:
            return  # un enlace que vuelve a un directorio padre: ciclo
        ancestors = ancestors | {real}
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            result.skipped.append(Skipped(prefix.rstrip("/") or ".", "unreadable"))
            return
        for entry in entries:
            rel = prefix + entry.name
            is_link = entry.is_symlink()
            target_real = os.path.realpath(entry.path) if is_link else entry.path
            is_dir = entry.is_dir(follow_symlinks=True)
            if _excluded(rel, entry.name, is_dir, patterns):
                result.ignored += 1
                continue
            if is_link:
                if not os.path.exists(entry.path):
                    result.skipped.append(Skipped(rel, "symlink_broken", os.readlink(entry.path)))
                    result.links.append(LinkInfo(rel, os.readlink(entry.path), "skipped", "symlink_broken"))
                    continue
                if not _inside(target_real, str(real_root)):
                    result.skipped.append(Skipped(rel, "symlink_outside", target_real))
                    result.links.append(LinkInfo(rel, target_real, "skipped", "symlink_outside"))
                    continue
                result.links.append(LinkInfo(rel, os.path.relpath(target_real, real_root), "followed"))
            if is_dir:
                # el recorrido sigue la ruta lógica (`rel`), no la del destino del enlace
                walk(Path(entry.path), rel + "/", via_link or is_link, ancestors)
            elif entry.is_file(follow_symlinks=True):
                st = os.stat(entry.path)
                result.files.append(
                    LocalFile(
                        rel,
                        Path(target_real if is_link else entry.path),
                        st.st_size,
                        bool(st.st_mode & 0o111),
                        via_symlink=via_link or is_link,
                        hidden=any(part.startswith(".") for part in rel.split("/")),
                    )
                )
            else:
                result.skipped.append(Skipped(rel, "special"))

    walk(real_root, "", False, frozenset())
    return result


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


# --- codificación y rutas del almacén ------------------------------------------------------------


def encode_content(data: bytes) -> tuple[str, str]:
    """`(content, encoding)`: `utf8` si son bytes de texto UTF-8 válido sin NUL; `base64` en otro caso."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return base64.b64encode(data).decode("ascii"), "base64"
    if "\x00" in text:
        return base64.b64encode(data).decode("ascii"), "base64"
    return text, "utf8"


def decode_content(content: str, encoding: str) -> bytes:
    if encoding == "base64":
        return base64.b64decode(content, validate=True)
    return content.encode("utf-8")


def is_text(data: bytes) -> bool:
    return encode_content(data)[1] == "utf8"


def check_store_path(path: str) -> str | None:
    """Reglas de ruta del almacén (contrato §7.4). Devuelve el motivo del rechazo o `None` si vale."""
    if not path or path.startswith("/") or "\\" in path or _CONTROL.search(path):
        return "ruta vacía, absoluta o con `\\` o caracteres de control"
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return "el nombre no es UTF-8 válido"
    if len(path.encode("utf-8")) > 1024:
        return "más de 1024 bytes"
    for seg in path.split("/"):
        if seg in ("", ".", ".."):
            return "segmento vacío, `.` o `..`"
        if len(seg.encode("utf-8")) > 255:
            return "segmento de más de 255 bytes"
    return None


def package_sha256(files: dict[str, str]) -> str:
    """SHA-256 del texto de líneas `"<sha256>  <ruta>\\n"` ordenadas por ruta en bytes UTF-8 (contrato §7.4)."""
    lines = (f"{sha}  {path}\n" for path, sha in sorted(files.items(), key=lambda kv: kv[0].encode("utf-8")))
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def posix_rel(path: str) -> str:
    return PurePosixPath(path).as_posix()
