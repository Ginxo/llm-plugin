"""Cliente MCP mínimo sobre Streamable HTTP (solo lo que necesita el plugin: initialize + tools/call).

- Acepta respuestas `application/json` y `text/event-stream` (SSE) del mismo POST.
- Funciona con servidores con sesión (`Mcp-Session-Id`) y sin estado.
- Errores HTTP del contrato §7.1: 401 → clave no válida; 404 → organización inexistente o ajena;
  503 `UNAVAILABLE` → fallo cerrado. Errores de tool (`isError: true`) → `McpToolError`.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

from . import __version__, httpclient
from .errors import AuthError, BaasError, McpToolError, NetworkError

PROTOCOL_VERSION = "2025-06-18"  # era «legacy» del MCP de la Fase 1; se acepta la que negocie el servidor


def parse_sse(raw: bytes) -> Iterator[Any]:
    """Devuelve el JSON de cada evento SSE (une las líneas `data:` de un mismo evento)."""
    data: list[str] = []
    for line in [*raw.decode("utf-8", errors="replace").splitlines(), ""]:
        if line == "":
            if data:
                with contextlib.suppress(ValueError):
                    yield json.loads("\n".join(data))
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" ") if line.startswith("data: ") else line[5:])
    return


class McpClient:
    def __init__(self, url: str, api_key: str, *, timeout: float = 60) -> None:
        self.url = url
        self._api_key = api_key
        self.timeout = timeout
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._next_id = 0

    # --- ciclo de vida ------------------------------------------------------------------------
    def __enter__(self) -> McpClient:
        self.initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, message: dict[str, Any]) -> httpclient.Response:
        resp = httpclient.request(
            "POST", self.url, headers=self._headers(), body=json.dumps(message).encode(), timeout=self.timeout
        )
        if resp.status == 401:
            raise AuthError("El MCP rechazó la API key (no es válida o está revocada). Lanza /baas-init.")
        if resp.status == 404:
            raise McpToolError("NOT_FOUND", "La organización no existe o no tienes acceso a ella.")
        if resp.status >= 400:
            body = _safe_json(resp.body)
            code = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), str) else None
            if code:
                raise McpToolError(code, str(body.get("message") or f"HTTP {resp.status}"), body)
            raise NetworkError(f"El MCP respondió HTTP {resp.status}.")
        return resp

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        msg_id = self._next_id
        resp = self._post({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        ctype = (resp.header("Content-Type") or "").lower()
        candidates = parse_sse(resp.body) if "text/event-stream" in ctype else [resp.json()]
        for msg in candidates:
            batch = msg if isinstance(msg, list) else [msg]
            for item in batch:
                if isinstance(item, dict) and item.get("id") == msg_id and ("result" in item or "error" in item):
                    if "error" in item:
                        err = item["error"] or {}
                        raise BaasError(f"Error del MCP en {method}: {err.get('message') or err.get('code')}")
                    if method == "initialize":
                        sid = resp.header("Mcp-Session-Id")
                        self.session_id = sid or None
                    return item["result"]
        raise NetworkError(f"El MCP no devolvió respuesta a {method}.")

    def initialize(self) -> dict[str, Any]:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "baas-llm-plugin", "version": __version__},
            },
        )
        self.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def close(self) -> None:
        if self.session_id:
            with contextlib.suppress(NetworkError):
                httpclient.request("DELETE", self.url, headers=self._headers(), timeout=min(10, self.timeout))
            self.session_id = None

    # --- tools ------------------------------------------------------------------------------------
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        text = next(
            (c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"),
            "",
        )
        if result.get("isError"):
            payload = _safe_json(text.encode())
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                raise McpToolError(payload["error"], str(payload.get("message") or ""), payload)
            raise McpToolError("TOOL_ERROR", text or "error sin detalle")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        parsed = _safe_json(text.encode())
        return parsed if parsed is not None else {"text": text}


def _safe_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None
