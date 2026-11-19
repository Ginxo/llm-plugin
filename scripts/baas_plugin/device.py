"""Cliente del flujo de autorización de dispositivo (la persona aprueba en el navegador) y estado pendiente.

La API key solo viaja en la respuesta del último `poll`; aquí nunca se escribe en disco ni en mensajes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import defaults, httpclient
from .envfile import BaasPaths, atomic_write, ensure_private_dir, normalize_app_url
from .errors import NetworkError

START_PATH = "/api/v1/plugin/device/start"
POLL_PATH = "/api/v1/plugin/device/poll"
MAX_RETRY_AFTER = 5  # segundos: tope a lo que se respeta un `Retry-After` del servidor

_UNREACHABLE = "No he podido conectar con BaaS-AI-IP."
_UNREACHABLE_HINT = "Comprueba tu conexión a internet e inténtalo de nuevo en unos minutos."
_UNEXPECTED = "BaaS-AI-IP ha respondido algo que no esperaba."
_UNEXPECTED_HINT = "Inténtalo de nuevo en unos minutos; si sigue pasando, avisa a quien administra BaaS-AI-IP."


@dataclass(frozen=True)
class DeviceStart:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class DevicePoll:
    status: str
    api_key: str | None = field(default=None, repr=False)
    email: str | None = None


def _unexpected() -> NetworkError:
    return NetworkError(_UNEXPECTED, hint=_UNEXPECTED_HINT)


def _post(url: str, payload: dict[str, Any], timeout: float) -> httpclient.Response:
    try:
        return httpclient.post_json(url, payload, timeout=timeout)
    except NetworkError as exc:
        raise NetworkError(_UNREACHABLE, hint=_UNREACHABLE_HINT) from exc


def _json_object(resp: httpclient.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except NetworkError as exc:
        raise _unexpected() from exc
    if not isinstance(body, dict):
        raise _unexpected()
    return body


def _nonempty_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def start(app_url: str, client_name: str, *, timeout: float = 15) -> DeviceStart:
    """Pide un código de aprobación. `NetworkError` si no hay servidor o la respuesta no es del contrato."""
    resp = _post(normalize_app_url(app_url) + START_PATH, {"client_name": client_name}, timeout)
    if resp.status == 429:
        raise NetworkError(
            "Se han pedido demasiadas conexiones seguidas a BaaS-AI-IP.", hint="Espera un minuto e inténtalo de nuevo."
        )
    if resp.status != 200:
        raise NetworkError("BaaS-AI-IP no está disponible ahora mismo.", hint=_UNREACHABLE_HINT)
    body = _json_object(resp)
    device_code = _nonempty_str(body.get("device_code"))
    user_code = _nonempty_str(body.get("user_code"))
    url = _nonempty_str(body.get("verification_url"))
    expires_in = _positive_int(body.get("expires_in"))
    interval = _positive_int(body.get("interval"))
    if not (device_code and user_code and url and expires_in and interval) or urlsplit(url).scheme not in (
        "http",
        "https",
    ):
        raise _unexpected()
    return DeviceStart(device_code, user_code, url, expires_in, interval)


def _retry_after(resp: httpclient.Response) -> float:
    try:
        return min(max(float(resp.header("Retry-After") or 0), 0.0), MAX_RETRY_AFTER)
    except ValueError:
        return 0.0


def _poll(app_url: str, device_code: str, timeout: float) -> tuple[DevicePoll, float]:
    """Un sondeo. Devuelve también los segundos que el servidor pide esperar (solo con 429)."""
    resp = _post(normalize_app_url(app_url) + POLL_PATH, {"device_code": device_code}, timeout)
    if resp.status == 429:
        return DevicePoll("pending"), _retry_after(resp)
    if resp.status != 200:
        raise NetworkError("BaaS-AI-IP no está disponible ahora mismo.", hint=_UNREACHABLE_HINT)
    body = _json_object(resp)
    status = body.get("status")
    if status in ("pending", "denied", "expired"):
        return DevicePoll(status), 0.0
    if status == "approved":
        api_key = _nonempty_str(body.get("api_key"))
        email = _nonempty_str(body.get("email"))
        if not (api_key and email):
            raise _unexpected()
        return DevicePoll("approved", api_key, email), 0.0
    raise _unexpected()


def poll(app_url: str, device_code: str, *, timeout: float = 15) -> DevicePoll:
    """Un sondeo del estado de la aprobación; un `429` cuenta como «pending»."""
    return _poll(app_url, device_code, timeout)[0]


def wait(
    app_url: str,
    device_code: str,
    *,
    interval: int,
    max_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> DevicePoll:
    """Sondea hasta que el estado deje de ser `pending`; si se agota `max_seconds` devuelve `pending`."""
    started = now()
    while True:
        result, retry_after = _poll(app_url, device_code, 15)
        if result.status != "pending":
            return result
        delay = max(float(interval), retry_after)
        if now() - started + delay > max_seconds:
            return result
        sleep(delay)


# --- estado pendiente: una conexión empezada y aún sin aprobar ---------------------------------


@dataclass(frozen=True)
class PendingFlow:
    app_url: str
    device_code: str
    user_code: str
    verification_url: str
    expires_at: float  # epoch (time.time()): tiene que sobrevivir al proceso


def save_pending(paths: BaasPaths, flow: PendingFlow) -> None:
    ensure_private_dir(paths.root)
    atomic_write(paths.pending_file, json.dumps(flow.__dict__, ensure_ascii=False) + "\n")


def clear_pending(paths: BaasPaths) -> None:
    paths.pending_file.unlink(missing_ok=True)


def _parse_pending(raw: str) -> PendingFlow | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    texts = [_nonempty_str(data.get(k)) for k in ("app_url", "device_code", "user_code", "verification_url")]
    expires_at = data.get("expires_at")
    if not all(texts) or isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return None
    app_url, device_code, user_code, verification_url = texts
    return PendingFlow(app_url, device_code, user_code, verification_url, float(expires_at))  # type: ignore[arg-type]


def load_pending(paths: BaasPaths, *, now: float) -> PendingFlow | None:
    """El flujo pendiente, o `None` (y borra el fichero) si caducó o está dañado."""
    path: Path = paths.pending_file
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        clear_pending(paths)
        return None
    flow = _parse_pending(raw)
    if flow is None or flow.expires_at <= now:
        clear_pending(paths)
        return None
    return flow


def resolve_app_url(explicit: str | None, env: dict[str, str], saved: str | None) -> str | None:
    """URL de la webapp: explícita > `BAAS_APP_URL` del entorno > guardada > por defecto del plugin > `None`."""
    for candidate in (explicit, env.get("BAAS_APP_URL"), saved, defaults.DEFAULT_APP_URL):
        if candidate and candidate.strip():
            return candidate.strip()
    return None
