"""Subida y bajada de ficheros grandes por trozos (contrato §7.4, hito H5, D64).

Un fichero de decenas de MiB no viaja bien en un mensaje del MCP: el servidor mantiene a la vez el JSON, el base64
y los bytes (medido con `mcp-server/scripts/bench_memory.py`: subir pesa 6,3 veces el fichero y bajar 5 veces; con
500 MiB, 3,3 GB) y un proxy intermedio puede limitar el cuerpo de una petición. Por eso lo que pasa de
`CHUNK_THRESHOLD` va por trozos de pocos MiB: la memoria del servidor (y la de este cliente) no crece con el tamaño
del fichero. El resto sigue yendo inline, en el lote de siempre.

- Subir: `baas_begin_upload` (ruta, tamaño y `sha256`), `baas_put_chunk` × n (en orden, con el `sha256` de cada trozo)
  y, para colocarlo, `files[].upload_id` en `baas_write_files`: el lote sigue siendo atómico y el servidor verifica
  el `sha256` de extremo a extremo.
- Bajar: `baas_read_chunk` × n, con el `sha256` del manifiesto como `expected_sha256` (si el fichero cambia a mitad,
  el servidor lo dice) y verificación del `sha256` del conjunto.

Solo biblioteca estándar.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, BinaryIO, TypeVar

from .errors import McpToolError, NetworkError, PackageError

MIB = 1024 * 1024
CHUNK_THRESHOLD = 4 * MIB  # un fichero de más de esto va por trozos; el resto, inline como siempre
INLINE_BUDGET = 8 * MIB  # lo que suman, como mucho, los ficheros inline de un mismo mensaje
FALLBACK_CHUNK = 4 * MIB  # el trozo, si el servidor no recomienda uno
RETRIES = 3  # reintentos de un trozo ante un fallo de red o un trozo corrupto
SHA256_LEN = 64

T = TypeVar("T")


def is_large(size: int) -> bool:
    return size > CHUNK_THRESHOLD


def split_by_budget(items: Sequence[T], size_of: Callable[[T], int]) -> tuple[list[T], list[T]]:
    """`(inline, por_trozos)`: lo grande va por trozos y, de lo pequeño, lo que no cabe en el presupuesto del mensaje.

    Así el mensaje que confirma un lote pesa como mucho `INLINE_BUDGET` aunque el paquete tenga cientos de ficheros.
    """
    inline: list[T] = []
    chunked: list[T] = []
    used = 0
    for item in items:
        size = size_of(item)
        if not is_large(size) and used + size <= INLINE_BUDGET:
            inline.append(item)
            used += size
        else:
            chunked.append(item)
    return inline, chunked


def _args(org: str | None, **kw: Any) -> dict[str, Any]:
    out = {k: v for k, v in kw.items() if v is not None}
    if org:
        out["org"] = org
    return out


# --- subir -----------------------------------------------------------------------------------------------


def upload(
    client: Any,
    path: str,
    source: bytes | Path,
    sha256: str,
    *,
    size: int | None = None,
    org: str | None = None,
) -> str:
    """Sube un fichero por trozos y devuelve su `upload_id` (para `files[].upload_id` de `baas_write_files`).

    `source` son los bytes o la ruta de un fichero local (se lee por trozos). Los trozos se envían en orden; si la
    respuesta de uno se pierde, el servidor dice hasta dónde llegó (`received`) y se continúa desde ahí. Si algo falla
    se descarta la carga (`baas_abort_upload`).
    """
    total = size if size is not None else (len(source) if isinstance(source, bytes) else source.stat().st_size)
    begun = client.call_tool("baas_begin_upload", _args(org, path=path, size=total, sha256=sha256))
    upload_id = begun["upload_id"]
    step = _chunk_size(begun)
    try:
        with _Reader(source) as fh:
            offset = 0
            stalls = 0
            while offset < total:
                fh.seek(offset)
                data = fh.read(min(step, total - offset))
                if not data:
                    raise PackageError(f"El fichero {path} cambió mientras se subía: se esperaban {total} bytes.")
                received = _put_chunk(client, upload_id, offset, data, org)
                stalls = 0 if received > offset else stalls + 1
                if stalls > RETRIES or received > total:
                    raise PackageError(f"El servidor no avanza al recibir {path} (recibido {received} de {total}).")
                offset = received
    except BaseException:
        with contextlib.suppress(Exception):
            client.call_tool("baas_abort_upload", _args(org, upload_id=upload_id))
        raise
    return str(upload_id)


def abort(client: Any, upload_ids: Sequence[str], org: str | None = None) -> None:
    """Descarta cargas ya subidas (si el lote no llegó a confirmarse). Nunca falla."""
    for upload_id in upload_ids:
        with contextlib.suppress(Exception):
            client.call_tool("baas_abort_upload", _args(org, upload_id=upload_id))


def _chunk_size(begun: dict[str, Any]) -> int:
    recommended = begun.get("chunk_size")
    maximum = begun.get("max_chunk_size")
    step = recommended if isinstance(recommended, int) and recommended > 0 else FALLBACK_CHUNK
    if isinstance(maximum, int) and maximum > 0:
        step = min(step, maximum)
    return step


class _Reader:
    """Un lector con `seek`/`read` sobre bytes o sobre un fichero (que se cierra al salir)."""

    def __init__(self, source: bytes | Path) -> None:
        self._source = source
        self._fh: BinaryIO | None = None

    def __enter__(self) -> _Reader:
        if isinstance(self._source, bytes):
            self._view = memoryview(self._source)
            self._pos = 0
        else:
            self._fh = self._source.open("rb")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fh is not None:
            self._fh.close()

    def seek(self, offset: int) -> None:
        if self._fh is not None:
            self._fh.seek(offset)
        else:
            self._pos = offset

    def read(self, n: int) -> bytes:
        if self._fh is not None:
            return self._fh.read(n)
        data = bytes(self._view[self._pos : self._pos + n])
        self._pos += len(data)
        return data


def _put_chunk(client: Any, upload_id: str, offset: int, data: bytes, org: str | None) -> int:
    """Envía un trozo y devuelve los bytes que el servidor lleva recibidos. Reintenta lo transitorio."""
    args = _args(
        org,
        upload_id=upload_id,
        offset=offset,
        content=base64.b64encode(data).decode("ascii"),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(min(2.0, 0.25 * 2**attempt))
        try:
            out = client.call_tool("baas_put_chunk", args)
        except NetworkError as exc:  # un corte: el trozo pudo llegar o no; el reintento lo aclara con `received`
            last = exc
            continue
        except McpToolError as exc:
            code = exc.payload.get("code")
            received = exc.payload.get("received")
            if exc.code == "INVALID_ARGUMENT" and code == "upload.offset" and isinstance(received, int):
                return received  # el trozo ya había llegado (se perdió la respuesta), o va por detrás: se continúa
            if exc.code == "INVALID_ARGUMENT" and code == "chunk.sha256_mismatch":
                last = exc  # corrompido en tránsito: se vuelve a enviar
                continue
            raise
        received = out.get("received")
        if not isinstance(received, int):
            raise PackageError("Respuesta de baas_put_chunk incompleta (falta `received`).")
        return received
    assert last is not None
    raise last


# --- bajar -------------------------------------------------------------------------------------------------


def download(
    client: Any,
    path: str,
    sink: BinaryIO,
    *,
    size: int,
    sha256: str,
    org: str | None = None,
) -> str:
    """Baja un fichero por trozos a `sink` y devuelve el `sha256` de lo escrito (el que llama lo verifica).

    Comprueba que cada trozo es de este fichero (`expected_sha256`: si cambió en el servidor entre dos trozos, no se
    mezclan versiones) y que no se escribe nada más allá del tamaño del manifiesto.
    """
    digest = hashlib.sha256()
    offset = 0
    while True:
        out = _read_chunk(client, path, offset, sha256, org)
        try:
            data = base64.b64decode(str(out.get("content") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PackageError(f"baas_read_chunk devolvió base64 no válido para {path}.") from exc
        if out.get("offset") != offset or out.get("encoding") not in (None, "base64"):
            raise PackageError(f"baas_read_chunk devolvió un trozo que no es el pedido ({path}, desde {offset}).")
        if offset + len(data) > size:
            raise PackageError(f"El servidor devuelve más bytes de los que declara el manifiesto para {path}.")
        sink.write(data)
        digest.update(data)
        offset += len(data)
        if out.get("eof"):
            break
        if not data:
            raise PackageError(f"El servidor no avanza al enviar {path} (en el byte {offset} de {size}).")
    if offset != size:
        raise PackageError(f"{path} llegó con {offset} bytes y el manifiesto declara {size}.")
    return digest.hexdigest()


def _read_chunk(client: Any, path: str, offset: int, sha256: str, org: str | None) -> dict[str, Any]:
    args = _args(org, path=path, offset=offset, expected_sha256=sha256)
    last: Exception | None = None
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(min(2.0, 0.25 * 2**attempt))
        try:
            out = client.call_tool("baas_read_chunk", args)
        except NetworkError as exc:
            last = exc
            continue
        except McpToolError as exc:
            if exc.code == "CONFLICT":
                raise PackageError(
                    f"{path} cambió en el servidor mientras se descargaba.", hint="Vuelve a lanzar el comando."
                ) from exc
            raise
        if not isinstance(out, dict):
            raise PackageError("Respuesta de baas_read_chunk incompleta.")
        return out
    assert last is not None
    raise last
