#!/usr/bin/env python3
"""API de OpenAI falsa para el spike S13 (Codex CLI a través del proxy de BaaS-AI-IP).

Responde a la API **Responses** (`POST /v1/responses`, en JSON o en *streaming* SSE), a **Chat Completions**
(`POST /v1/chat/completions`, ídem) y a `GET /v1/models`. Registra cada petición (método, ruta, nombres de
cabecera, qué cabeceras `X-Baas-*` llegan y un prefijo de `Authorization`) **sin** guardar valores secretos,
para comprobar que el proxy quita `X-Baas-*` y reenvía intacta la credencial del proveedor (contrato §3).

El proveedor falso de `deploy` (`dev/fake-upstream.caddy`) solo simula `/v1/messages` y `/v1/chat/completions`;
Codex CLI usa Responses, así que el spike apunta el proxy a este con `BAAS_DEV_OPENAI_UPSTREAM`.

Uso: `python3 dev/fake_openai.py --host 0.0.0.0 --port 8091` (registro en JSON Lines por la salida estándar).
Solo biblioteca estándar.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TEXT = "Respuesta del OpenAI falso de BaaS-AI-IP."
MODEL = "gpt-fake"
USAGE = {"input_tokens": 12, "output_tokens": 9, "total_tokens": 21}
_SAFE_HEADERS = {"user-agent", "originator", "content-type", "accept", "openai-beta", "session_id", "version"}


def _message_item(status: str, text: str) -> dict[str, Any]:
    content = [{"type": "output_text", "text": text, "annotations": []}] if text or status == "completed" else []
    return {"id": "msg_fake_1", "type": "message", "status": status, "role": "assistant", "content": content}


def _response(status: str, model: str, output: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "resp_fake_1",
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": output,
        "usage": {
            **USAGE,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
        if status == "completed"
        else None,
    }


def responses_events(model: str = MODEL, text: str = TEXT) -> list[dict[str, Any]]:
    part = {"type": "output_text", "text": "", "annotations": []}
    base = {"item_id": "msg_fake_1", "output_index": 0}
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": _response("in_progress", model, [])},
        {"type": "response.in_progress", "response": _response("in_progress", model, [])},
        {"type": "response.output_item.added", "output_index": 0, "item": _message_item("in_progress", "")},
        {"type": "response.content_part.added", **base, "content_index": 0, "part": part},
        {"type": "response.output_text.delta", **base, "content_index": 0, "delta": text},
        {"type": "response.output_text.done", **base, "content_index": 0, "text": text},
        {"type": "response.content_part.done", **base, "content_index": 0, "part": {**part, "text": text}},
        {"type": "response.output_item.done", "output_index": 0, "item": _message_item("completed", text)},
        {"type": "response.completed", "response": _response("completed", model, [_message_item("completed", text)])},
    ]
    for i, event in enumerate(events):
        event["sequence_number"] = i
    return events


def chat_completion(model: str = MODEL, text: str = TEXT) -> dict[str, Any]:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21},
    }


def chat_chunks(model: str = MODEL, text: str = TEXT) -> list[dict[str, Any]]:
    def chunk(delta: dict[str, Any], finish: str | None) -> dict[str, Any]:
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    last = chunk({}, "stop")
    last["usage"] = {"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21}
    return [chunk({"role": "assistant", "content": ""}, None), chunk({"content": text}, None), last]


def describe(method: str, path: str, headers: Any, body: bytes) -> dict[str, Any]:
    """Registro de una petición sin valores secretos."""
    names = sorted({k.lower() for k in headers})
    auth = headers.get("Authorization") or ""
    try:
        parsed = json.loads(body or b"{}")
    except ValueError:
        parsed = {}
    return {
        "method": method,
        "path": path,
        "header_names": names,
        "x_baas_headers": [n for n in names if n.startswith("x-baas-")],
        "authorization": (auth[:13] + "…") if auth else None,
        "headers": {n: headers.get(n) for n in names if n in _SAFE_HEADERS},
        "model": parsed.get("model") if isinstance(parsed, dict) else None,
        "stream": bool(parsed.get("stream")) if isinstance(parsed, dict) else False,
    }


class FakeOpenAI:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, log: Any = None) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                return

            def _send(self, status: int, body: bytes, ctype: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, body: bytes) -> dict[str, Any]:
                rec = describe(self.command, self.path, self.headers, body)
                outer.requests.append(rec)
                if log is not None:
                    log.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    log.flush()
                return rec

            def do_GET(self) -> None:
                self._record(b"")
                if self.path.split("?")[0] == "/v1/models":
                    data = {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "baas"}]}
                    return self._send(200, json.dumps(data).encode(), "application/json")
                return self._not_found()

            def _not_found(self) -> None:
                err = {"error": {"message": "fake openai: ruta no simulada", "type": "invalid_request_error",
                                 "param": None, "code": "not_found"}}  # fmt: skip
                self._send(404, json.dumps(err).encode(), "application/json")

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                rec = self._record(body)
                path = self.path.split("?")[0]
                model = rec["model"] or MODEL
                if path == "/v1/responses":
                    if rec["stream"]:
                        sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in responses_events(model))
                        return self._send(200, sse.encode(), "text/event-stream")
                    out = _response("completed", model, [_message_item("completed", TEXT)])
                    return self._send(200, json.dumps(out).encode(), "application/json")
                if path == "/v1/chat/completions":
                    if rec["stream"]:
                        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chat_chunks(model)) + "data: [DONE]\n\n"
                        return self._send(200, sse.encode(), "text/event-stream")
                    return self._send(200, json.dumps(chat_completion(model)).encode(), "application/json")
                return self._not_found()

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://{host}:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> int:
    p = argparse.ArgumentParser(description="API de OpenAI falsa (Responses y Chat Completions)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8091)
    ns = p.parse_args()
    server = FakeOpenAI(ns.host, ns.port, log=sys.stdout)
    print(json.dumps({"listening": server.url}), flush=True)
    try:
        server._thread.join()
    except KeyboardInterrupt:
        server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
