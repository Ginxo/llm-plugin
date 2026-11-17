"""HTTP mínimo con urllib. Nunca registra cabeceras ni cuerpos (pueden contener la API key)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .errors import NetworkError

USER_AGENT = f"baas-llm-plugin/{__version__}"


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes = field(repr=False)

    def header(self, name: str) -> str | None:
        lname = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == lname), None)

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError as exc:
            raise NetworkError(f"Respuesta no JSON del servidor (HTTP {self.status}).") from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Una redirección podría reenviar la cabecera Authorization a otro host: no se siguen.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def request(
    method: str, url: str, *, headers: dict[str, str] | None = None, body: bytes | None = None, timeout: float = 15
) -> Response:
    req = urllib.request.Request(url, data=body, method=method)  # noqa: S310 (URL validada por quien llama)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return Response(resp.status, dict(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read() or b"")
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise NetworkError(
            f"No se pudo conectar con {url.split('?')[0]}: {reason}.",
            hint="Comprueba tu conexión y la URL (BAAS_APP_URL en ~/baas-ai-ip/.env).",
        ) from exc


def post_json(url: str, payload: Any, *, headers: dict[str, str] | None = None, timeout: float = 15) -> Response:
    hdrs = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
    return request("POST", url, headers=hdrs, body=json.dumps(payload).encode(), timeout=timeout)
