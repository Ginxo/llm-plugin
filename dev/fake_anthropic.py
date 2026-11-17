"""API de Anthropic falsa para el spike S1: registra cabeceras y cuerpos y responde un mensaje fijo."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fake_baas import Headers

TEXT = "RESPUESTA-FALSA"


def _sse() -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": "claude-fake", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": TEXT}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]  # fmt: skip
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


class FakeAnthropic:
    def __init__(self) -> None:
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

            def do_GET(self) -> None:
                outer.requests.append({"method": "GET", "path": self.path, "headers": Headers(self.headers.items())})
                self._send(200, b'{"data": []}', "application/json")

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append(
                    {"method": "POST", "path": self.path, "headers": Headers(self.headers.items()), "body": body}
                )
                if "count_tokens" in self.path:
                    return self._send(200, b'{"input_tokens": 1}', "application/json")
                try:
                    stream = json.loads(body or b"{}").get("stream", False)
                except ValueError:
                    stream = False
                if stream:
                    return self._send(200, _sse(), "text/event-stream")
                msg = {"id": "msg_fake", "type": "message", "role": "assistant", "model": "claude-fake",
                       "content": [{"type": "text", "text": TEXT}], "stop_reason": "end_turn",
                       "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}  # fmt: skip
                self._send(200, json.dumps(msg).encode(), "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def messages(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST" and "/v1/messages" in r["path"]]
