"""Descarga, verificación y escritura de paquetes de skills (contrato §7.4, hito H5).

Lo comparten el ejecutor bajo demanda (`runner`) y la sincronización (`sync`):

1. `fetch_manifest`: `baas_get_skill_package` → rutas, hashes y bit de ejecución de cada fichero.
2. `fetch_contents`: `baas_get_skill_files` por lotes (lo pequeño) y `baas_read_chunk` por trozos (lo que pasa de
   `transfer.CHUNK_THRESHOLD`). **Cada fichero se verifica** con el `sha256` del manifiesto; el servidor no puede
   añadir ficheros que no se pidieron.
3. `materialize`: escribe el árbol en un directorio propio, con el bit de ejecución solo donde el
   manifiesto lo declara. Nunca crea enlaces simbólicos ni sale del directorio de destino.
   `fetch_to_dir` hace 2 y 3 a la vez **sin guardar los ficheros grandes en memoria**: los escribe a disco según
   llegan (lo usan `sync` y `export`).

Las rutas del manifiesto pueden venir como `skills/<skill>/<ruta>` (las del almacén) o relativas a la skill;
aquí se normalizan a relativas y se pide al servidor cada una tal como la declaró. `baas.yaml` es el
manifiesto del MCP, no parte de la skill instalada: no se descarga.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import transfer
from .errors import PackageError, ScriptIntegrityError

MANIFEST_FILE = "baas.yaml"
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BATCH_BYTES = 8 * 1024 * 1024  # bytes (del manifiesto) que se piden en cada `baas_get_skill_files`
MAX_PATH_BYTES = 1024  # límites del sistema de ficheros, no de producto (diseño §5.1)
MAX_SEGMENT_BYTES = 255
DIR_MODE = 0o700
FILE_MODE = 0o600
EXEC_MODE = 0o700


def valid_skill_name(name: object) -> bool:
    return isinstance(name, str) and bool(SKILL_NAME_RE.match(name))


def safe_rel_path(path: object) -> str:
    """Valida una ruta relativa de un paquete: sin `..`, absolutas, `\\`, controles ni segmentos vacíos."""
    if not isinstance(path, str) or not path:
        raise PackageError(f"Ruta de fichero no válida en el paquete: {path!r}.")
    shown = path.encode("unicode_escape").decode()
    if "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise PackageError(f"Ruta no válida en el paquete (barra invertida o caracteres de control): {shown}.")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise PackageError(f"Ruta absoluta no admitida en el paquete: {shown}.")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise PackageError(f"Ruta no admitida en el paquete (segmento vacío, `.` o `..`): {shown}.")
    if len(path.encode()) > MAX_PATH_BYTES or any(len(p.encode()) > MAX_SEGMENT_BYTES for p in parts):
        raise PackageError(f"Ruta demasiado larga en el paquete: {shown[:80]}….")
    return path


def check_tree(paths: Iterable[str]) -> None:
    """Un mismo nombre no puede ser fichero y directorio ni repetirse salvo por mayúsculas.

    Se compara sin distinguir mayúsculas: en macOS y Windows `A.py` y `a.py` serían el mismo fichero.
    """
    seen: dict[str, str] = {}
    dirs: set[str] = set()
    for path in paths:
        folded = path.casefold()
        if folded in seen and seen[folded] != path:
            raise PackageError(f"Dos rutas del paquete solo difieren en mayúsculas: {seen[folded]} y {path}.")
        seen[folded] = path
        parts = folded.split("/")
        dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
    clash = sorted(set(seen) & dirs)
    if clash:
        raise PackageError(f"«{seen[clash[0]]}» es a la vez fichero y directorio en el paquete.")


@dataclass(frozen=True)
class PackageFile:
    path: str  # relativa a la skill y validada
    server_path: str  # tal como la declaró el servidor (así se pide)
    sha256: str
    size: int
    executable: bool


@dataclass(frozen=True)
class Dependency:
    name: str
    accessible: bool
    package_sha256: str | None = None


@dataclass
class Manifest:
    skill: str
    status: str
    package_sha256: str
    files: list[PackageFile]  # sin `baas.yaml`
    metadata: dict[str, Any] = field(default_factory=dict)
    dependencies: list[Dependency] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def file(self, rel: str) -> PackageFile | None:
        return next((f for f in self.files if f.path == rel), None)

    def executable_paths(self) -> set[str]:
        return {f.path for f in self.files if f.executable}

    @property
    def requires(self) -> dict[str, Any]:
        req = self.metadata.get("requires")
        return req if isinstance(req, dict) else {}

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)


@dataclass
class Package:
    """Un manifiesto y el contenido (bytes) de todos sus ficheros, ya verificado."""

    manifest: Manifest
    contents: dict[str, bytes] = field(repr=False)

    @classmethod
    def build(
        cls,
        skill: str,
        files: dict[str, str | bytes],
        *,
        executable: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> Package:
        """Un paquete local (sin servidor), p. ej. para ejecutar un script ya verificado."""
        exec_set = set(executable)
        contents = {p: (c.encode() if isinstance(c, str) else c) for p, c in files.items()}
        entries = [
            PackageFile(p, p, hashlib.sha256(c).hexdigest(), len(c), p in exec_set) for p, c in sorted(contents.items())
        ]
        manifest = Manifest(skill, "active", "", entries, dict(metadata or {}))
        return cls(manifest, contents)


# --- manifiesto ----------------------------------------------------------------------------------


def _strip_store_prefix(skill: str, paths: list[str]) -> list[str]:
    """`skills/<skill>/<ruta>` → `<ruta>` si TODAS las rutas lo llevan (el `SKILL.md` es obligatorio)."""
    prefix = f"skills/{skill}/"
    if paths and all(p.startswith(prefix) for p in paths):
        return [p[len(prefix) :] for p in paths]
    return paths


def _args(skill: str, org: str | None, **extra: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {"skill": skill, **extra}
    if org:
        arguments["org"] = org
    return arguments


def parse_manifest(skill: str, data: Any) -> Manifest:
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise PackageError(f"Respuesta de baas_get_skill_package incompleta para «{skill}» (falta `files`).")
    raw_files = [f for f in data["files"] if isinstance(f, dict)]
    if len(raw_files) != len(data["files"]):
        raise PackageError(f"Manifiesto de «{skill}» mal formado: un fichero no es un objeto.")
    server_paths = [f.get("path") for f in raw_files]
    if not all(isinstance(p, str) for p in server_paths):
        raise PackageError(f"Manifiesto de «{skill}» mal formado: un fichero sin `path`.")
    rels = _strip_store_prefix(skill, server_paths)
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    declared_exec = metadata.get("executable")
    declared_exec = {str(p) for p in declared_exec} if isinstance(declared_exec, list) else set()
    files: list[PackageFile] = []
    for raw, server_path, rel in zip(raw_files, server_paths, rels, strict=True):
        rel = safe_rel_path(rel)
        sha = str(raw.get("sha256") or "").lower()
        if not SHA256_RE.match(sha):
            raise PackageError(f"Manifiesto de «{skill}»: el `sha256` de {rel} no es válido.")
        if rel == MANIFEST_FILE:
            continue
        executable = raw["executable"] if isinstance(raw.get("executable"), bool) else rel in declared_exec
        size = raw.get("size")
        files.append(PackageFile(rel, server_path, sha, size if isinstance(size, int) and size >= 0 else 0, executable))
    check_tree(f.path for f in files)
    if len({f.path for f in files}) != len(files):
        raise PackageError(f"Manifiesto de «{skill}» mal formado: rutas repetidas.")
    deps_raw = (data.get("depends_on") or {}).get("skills") if isinstance(data.get("depends_on"), dict) else []
    dependencies = []
    for dep in deps_raw or []:
        if isinstance(dep, str):
            dependencies.append(Dependency(dep, True))
        elif isinstance(dep, dict) and isinstance(dep.get("name"), str):
            dependencies.append(Dependency(dep["name"], bool(dep.get("accessible")), dep.get("package_sha256")))
    warnings = [
        str(w.get("message") or w.get("code")) if isinstance(w, dict) else str(w) for w in data.get("warnings") or []
    ]
    return Manifest(
        skill=skill,
        status=str(data.get("status") or "active"),
        package_sha256=str(data.get("package_sha256") or ""),
        files=sorted(files, key=lambda f: f.path),
        metadata=metadata,
        dependencies=dependencies,
        warnings=warnings,
    )


def fetch_manifest(client: Any, skill: str, org: str | None = None) -> Manifest:
    return parse_manifest(skill, client.call_tool("baas_get_skill_package", _args(skill, org)))


# --- contenido -----------------------------------------------------------------------------------


def _decode(item: dict[str, Any], rel: str) -> bytes:
    content = item.get("content")
    if not isinstance(content, str):
        raise PackageError(f"Respuesta de baas_get_skill_files: {rel} sin `content`.")
    encoding = str(item.get("encoding") or "utf8").lower()
    if encoding == "utf8":
        return content.encode("utf-8")
    if encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PackageError(f"Respuesta de baas_get_skill_files: {rel} no es base64 válido.") from exc
    raise PackageError(f"Respuesta de baas_get_skill_files: codificación desconocida «{encoding}» en {rel}.")


def verify_bytes(skill: str, rel: str, data: bytes, expected_sha256: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256.lower()):
        raise ScriptIntegrityError(
            f"El SHA-256 de {skill}/{rel} no coincide con el que declara el MCP; no se usa.",
            hint=f"Declarado {expected_sha256}, calculado {actual}. Informa al administrador de la organización.",
        )


def _batches(files: list[PackageFile]) -> Iterable[list[PackageFile]]:
    batch: list[PackageFile] = []
    total = 0
    for f in files:
        if batch and total + f.size > BATCH_BYTES:
            yield batch
            batch, total = [], 0
        batch.append(f)
        total += f.size
    if batch:
        yield batch


def _store_path(manifest: Manifest, f: PackageFile) -> str:
    """La ruta completa en el almacén (`skills/<skill>/<ruta>`): la que entiende `baas_read_chunk`."""
    return f"skills/{manifest.skill}/{f.path}"


def _split_large(files: list[PackageFile]) -> tuple[list[PackageFile], list[PackageFile]]:
    """`(pequeños, grandes)`: lo grande se descarga por trozos; lo pequeño, inline por lotes."""
    return [f for f in files if not transfer.is_large(f.size)], [f for f in files if transfer.is_large(f.size)]


def _wanted(manifest: Manifest, paths: Iterable[str] | None) -> list[PackageFile]:
    wanted = list(manifest.files)
    if paths is not None:
        names = set(paths)
        wanted = [f for f in wanted if f.path in names]
    return wanted


def _verify_download(manifest: Manifest, f: PackageFile, digest: str) -> None:
    if not hmac.compare_digest(digest, f.sha256.lower()):
        raise ScriptIntegrityError(
            f"El SHA-256 de {manifest.skill}/{f.path} no coincide con el que declara el MCP; no se usa.",
            hint=f"Declarado {f.sha256}, calculado {digest}. Informa al administrador de la organización.",
        )


def _fetch_inline(
    client: Any, manifest: Manifest, wanted: list[PackageFile], org: str | None
) -> Iterable[tuple[PackageFile, bytes]]:
    """Los ficheros pequeños, por lotes de `BATCH_BYTES`, verificados; cada lote se entrega antes de pedir otro."""
    for batch in _batches(wanted):
        by_server = {f.server_path: f for f in batch}
        data = client.call_tool(
            "baas_get_skill_files", _args(manifest.skill, org, paths=[f.server_path for f in batch])
        )
        items = data.get("files") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise PackageError(f"Respuesta de baas_get_skill_files incompleta para «{manifest.skill}».")
        seen: dict[str, bytes] = {}
        for item in items:
            entry = by_server.get(item.get("path")) if isinstance(item, dict) else None
            if entry is None:
                raise PackageError(f"baas_get_skill_files devolvió un fichero no pedido: {item.get('path')!r}.")
            if entry.path in seen:
                raise PackageError(f"baas_get_skill_files devolvió {entry.path} dos veces.")
            body = _decode(item, entry.path)
            verify_bytes(manifest.skill, entry.path, body, entry.sha256)
            reported = item.get("sha256")
            if isinstance(reported, str) and reported.lower() != entry.sha256:
                verify_bytes(manifest.skill, entry.path, body, reported)  # el servidor se contradice
            seen[entry.path] = body
        missing = sorted(f.path for f in batch if f.path not in seen)
        if missing:
            raise PackageError(f"baas_get_skill_files no devolvió {missing[0]} (y {len(missing) - 1} más).")
        for f in batch:
            yield f, seen[f.path]


def fetch_contents(
    client: Any, manifest: Manifest, paths: Iterable[str] | None = None, org: str | None = None
) -> dict[str, bytes]:
    """Descarga y verifica los ficheros (por omisión, todos los del manifiesto). `{ruta: bytes}` en memoria.

    Lo grande se pide por trozos (`baas_read_chunk`), nunca inline; pero aquí acaba entero en memoria: para escribir
    un árbol a disco sin cargarlo, `fetch_to_dir`.
    """
    small, large = _split_large(_wanted(manifest, paths))
    contents: dict[str, bytes] = {}
    for f, body in _fetch_inline(client, manifest, small, org):
        contents[f.path] = body
    for f in large:
        sink = io.BytesIO()
        digest = transfer.download(client, _store_path(manifest, f), sink, size=f.size, sha256=f.sha256, org=org)
        _verify_download(manifest, f, digest)
        contents[f.path] = sink.getvalue()
    return contents


def fetch_to_dir(client: Any, manifest: Manifest, dest: Path, org: str | None = None) -> None:
    """Descarga el paquete y lo escribe en `dest` (se crea, 0700), verificando cada fichero.

    Los ficheros pequeños se piden inline por lotes; los grandes, por trozos y se escriben a disco según llegan: la
    memoria no crece con su tamaño. Si algo falla (red, hash) se lanza la excepción y **el que llama** descarta
    `dest` (es un directorio temporal). Mismas garantías que `materialize`: rutas confinadas, sin enlaces
    simbólicos, bit de ejecución solo donde lo declara el manifiesto.
    """
    for f in manifest.files:
        safe_rel_path(f.path)
    check_tree(f.path for f in manifest.files)
    _make_root(dest)
    executable = manifest.executable_paths()
    small, large = _split_large(list(manifest.files))
    for f, body in _fetch_inline(client, manifest, small, org):
        fd = _open_new(dest, f.path)
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        os.chmod(dest.joinpath(*f.path.split("/")), EXEC_MODE if f.path in executable else FILE_MODE)
    for f in large:
        target = dest.joinpath(*f.path.split("/"))
        with os.fdopen(_open_new(dest, f.path), "wb") as fh:
            digest = transfer.download(client, _store_path(manifest, f), fh, size=f.size, sha256=f.sha256, org=org)
        _verify_download(manifest, f, digest)
        os.chmod(target, EXEC_MODE if f.path in executable else FILE_MODE)


def fetch_package(client: Any, skill: str, org: str | None = None) -> Package:
    manifest = fetch_manifest(client, skill, org)
    return Package(manifest, fetch_contents(client, manifest, org=org))


# --- escritura -----------------------------------------------------------------------------------


def _make_root(root: Path) -> None:
    root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(root, DIR_MODE)


def _open_new(root: Path, rel: str) -> int:
    """Crea (sin seguir enlaces, sin pisar) el fichero `rel` bajo `root` y devuelve su descriptor de escritura."""
    target = root.joinpath(*rel.split("/"))
    for parent in reversed(target.relative_to(root).parents):
        if parent != Path("."):
            directory = root / parent
            directory.mkdir(mode=DIR_MODE, exist_ok=True)
            os.chmod(directory, DIR_MODE)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.open(target, flags, FILE_MODE)


def materialize(root: Path, contents: dict[str, bytes], executable: Iterable[str] = ()) -> None:
    """Escribe el árbol en `root` (se crea, 0700). Solo `executable` lleva el bit de ejecución."""
    exec_set = set(executable)
    for rel in contents:
        safe_rel_path(rel)
    check_tree(contents)
    _make_root(root)
    for rel in sorted(contents):
        with os.fdopen(_open_new(root, rel), "wb") as fh:
            fh.write(contents[rel])
        os.chmod(root.joinpath(*rel.split("/")), EXEC_MODE if rel in exec_set else FILE_MODE)
