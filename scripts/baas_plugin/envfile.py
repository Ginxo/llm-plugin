"""`~/baas-ai-ip/.env`: la API key de BaaS vive solo aquí (contrato §10), con permisos 0600."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigError, MissingApiKeyError

API_KEY_RE = re.compile(r"^baas_[A-Za-z0-9_-]{43}$")
DIR_MODE = 0o700
FILE_MODE = 0o600
_POSIX = os.name == "posix"


@dataclass(frozen=True)
class BaasPaths:
    root: Path

    @classmethod
    def default(cls) -> BaasPaths:
        return cls(Path(os.path.expanduser("~")) / "baas-ai-ip")

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def pending_file(self) -> Path:
        """Estado de una conexión a medias (código de dispositivo); nunca contiene la API key."""
        return self.root / "pending.json"

    @property
    def bin_dir(self) -> Path:
        return self.root / "bin"


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    app_url: str | None

    def __str__(self) -> str:
        return f"Credentials(app_url={self.app_url!r})"


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _tighten(path: Path, mode: int) -> bool:
    """Ajusta los permisos si son más abiertos que `mode`. Devuelve True si los cambió."""
    if not _POSIX or not path.exists():
        return False
    current = path.stat().st_mode & 0o777
    if current & ~mode:
        path.chmod(mode)
        return True
    return False


def ensure_private_dir(path: Path) -> list[str]:
    warnings: list[str] = []
    if not path.exists():
        path.mkdir(mode=DIR_MODE, parents=True)
    if _tighten(path, DIR_MODE):
        warnings.append(f"Permisos de {path} corregidos a 0700.")
    return warnings


def read_env(paths: BaasPaths) -> tuple[dict[str, str], list[str]]:
    if not paths.env_file.exists():
        return {}, []
    warnings: list[str] = []
    if _tighten(paths.root, DIR_MODE):
        warnings.append(f"Permisos de {paths.root} corregidos a 0700.")
    if _tighten(paths.env_file, FILE_MODE):
        warnings.append(f"Permisos de {paths.env_file} corregidos a 0600 (estaban más abiertos).")
    return parse_env(paths.env_file.read_text(encoding="utf-8")), warnings


def atomic_write(path: Path, content: str, mode: int = FILE_MODE) -> None:
    """Escribe en un temporal del mismo directorio (creado ya con `mode`) y lo renombra."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        if _POSIX:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update_env(paths: BaasPaths, updates: dict[str, str]) -> list[str]:
    """Actualiza claves conservando comentarios y el resto de líneas; no duplica."""
    warnings = ensure_private_dir(paths.root)
    lines = paths.env_file.read_text(encoding="utf-8").splitlines() if paths.env_file.exists() else []
    pending = dict(updates)
    out: list[str] = []
    for raw in lines:
        key = raw.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in raw else None
        if key in pending and not raw.lstrip().startswith("#"):
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(raw)
    out.extend(f"{k}={v}" for k, v in pending.items())
    atomic_write(paths.env_file, "\n".join(out) + "\n")
    return warnings


def remove_env_keys(paths: BaasPaths, keys: Iterable[str]) -> bool:
    """Quita de `.env` las líneas de `keys` (conserva comentarios y el resto). `True` si cambió algo."""
    if not paths.env_file.exists():
        return False
    drop = set(keys)
    lines = paths.env_file.read_text(encoding="utf-8").splitlines()
    kept = [
        raw
        for raw in lines
        if raw.lstrip().startswith("#")
        or "=" not in raw
        or raw.split("=", 1)[0].strip().removeprefix("export ").strip() not in drop
    ]
    if kept == lines:
        return False
    atomic_write(paths.env_file, "\n".join(kept) + "\n" if kept else "")
    return True


def validate_api_key(value: str) -> str:
    key = value.strip()
    if not API_KEY_RE.match(key):
        raise ConfigError(
            "La API key no tiene el formato de BaaS-AI-IP (`baas_` + 43 caracteres base64url).",
            hint="Copia de nuevo la clave desde la webapp (Mis API keys).",
        )
    return key


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def normalize_app_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or parts.scheme not in ("http", "https"):
        raise ConfigError(f"URL de la webapp no válida: {value!r}. Ejemplo: https://app.<dominio>")
    is_local = host in _LOCAL_HOSTS or host.endswith((".localhost", ".test"))
    if parts.scheme == "http" and not is_local:
        raise ConfigError(
            f"La URL {url} usa HTTP sin cifrar: la API key viajaría en claro. Usa https:// "
            "(HTTP solo se admite en localhost o dominios .localhost/.test de desarrollo)."
        )
    return url


def missing_key_error(paths: BaasPaths) -> MissingApiKeyError:
    return MissingApiKeyError(
        f"No hay API key de BaaS-AI-IP en {paths.env_file} (ruta ~/baas-ai-ip/.env).",
        hint="Guárdala sin pasar por el chat: ejecuta `set-key` en una terminal o edita ese fichero "
        "(línea BAAS_API_KEY=...). Nunca la pegues en la conversación.",
    )


def load_credentials(paths: BaasPaths) -> Credentials:
    values, _ = read_env(paths)
    key = values.get("BAAS_API_KEY", "").strip()
    if not key:
        raise missing_key_error(paths)
    app_url = values.get("BAAS_APP_URL", "").strip()
    return Credentials(api_key=validate_api_key(key), app_url=normalize_app_url(app_url) if app_url else None)
