#!/usr/bin/env python3
"""Pila H3 simulada para probar `dev/e2e_h3.py` sin la pila real: webapp, proxy con AI-IP y proveedor con eco.

Implementa lo justo de los contratos del hito H3 (contrato §3, §5, §6, §7.2 y §8; plan, tarea D1):

- **Proveedor falso con eco** (como el de `deploy`): `POST /v1/messages`, `/v1/chat/completions` y
  `/v1/responses`, con y sin *streaming*. Responde `Eco: <último mensaje del usuario>` troceado en fragmentos de
  1 a 7 caracteres con una semilla fija; si el mensaje contiene `usa la herramienta`, llama a la herramienta `echo`
  con `{"text": "<eco>"}`. `GET /_debug/requests` → `{"requests": [{"path", "body"}]}` (50 últimas; `body` como
  objeto JSON o como texto, según `debug_body`) y `DELETE` para vaciarlo.
- **Proxy**: `/org/<slug>/anthropic/v1/messages` y `/org/<slug>/openai/v1/{chat/completions,responses}` con
  `X-Baas-Api-Key`. Con AI-IP activo sustituye email, IBAN y DNI por `[[TIPO_NNN]]` (vault por conversación) y los
  restaura en la respuesta, también cuando un token llega partido entre eventos SSE y en todas las copias completas
  de Responses. Conversaciones por huella (*system prompt* y primer mensaje) y por `previous_response_id`; las
  borradas dejan una lápida: un turno con historial recibe `403` (`baas_conversation_deleted`).
- **Webapp**: `/healthz`, *handshake*, inicio de sesión sin TOTP (`BAAS_DEV_SKIP_TOTP=1`), conversaciones con la
  verificación de cada turno y borrado (`POST /api/v1/conversations/{id}/delete`).

`faults` inyecta los fallos que la prueba tiene que detectar (`FAULTS`).

Uso manual (imprime las URLs y el comando de la prueba):

    python3 dev/fake_h3_stack.py [--fault leak] [--debug-body text]

Solo biblioteca estándar. No es una implementación de referencia de AI-IP: es un doble de pruebas.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import itertools
import json
import random
import re
import secrets
import sys
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_baas import dev_api_key  # API keys deterministas del contrato §8

DEV_PASSWORD = "baas-dev-password"  # noqa: S105 (contrato §8: valor de prueba de desarrollo)
TOOL_TRIGGER = "usa la herramienta"
TOKEN_RE = re.compile(r"\[\[[A-Z_]+_[0-9]{3,}\]\]")
_TOKEN_PREFIX = re.compile(r"\[(?:\[[A-Z_0-9]*\]?)?\Z")  # comienzo de un token que aún no ha llegado entero

# Reglas del catálogo global (contrato §11) que usa la prueba, por prioridad. Sin validadores: es un doble.
RULES = (
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    ("IBAN", re.compile(r"(?i)\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")),
    ("DNI", re.compile(r"(?i)\b(?:\d{8}|[XYZ]\d{7})-?[A-Z]\b")),
)
# Campos estructurales que el proxy no anonimiza (diseño §5.1.4).
_STRUCTURAL = {"model", "type", "role", "name", "id", "call_id", "previous_response_id", "tool_choice", "tools"}

ROUTES = {
    "anthropic/v1/messages": ("messages", "/v1/messages"),
    "openai/v1/chat/completions": ("chat", "/v1/chat/completions"),
    "openai/v1/responses": ("responses", "/v1/responses"),
}
FLAVORS = {"messages": "MESSAGES", "chat": "CHAT_COMPLETIONS", "responses": "RESPONSES"}
DELETED_MESSAGE = (
    "Acceso bloqueado por BaaS-AI-IP: la conversación fue eliminada por un administrador; empieza una nueva"
)
DENIED_MESSAGE = "Acceso bloqueado por BaaS-AI-IP: la organización no existe o no eres miembro"

FAULTS = {
    "leak": "el proxy reenvía el cuerpo original: los valores llegan en claro al proveedor",
    "no-rehydrate": "el proxy no restaura los tokens de la respuesta",
    "responses-done-unrestored": "en Responses con streaming solo restaura los deltas, no las copias completas",
    "bad-tool-json": "restaura los argumentos de las herramientas sin escapar (JSON inválido)",
    "warning": "los turnos con AI-IP se registran con verificación WARNING",
    "no-trace": "el proxy no emite eventos: los turnos no llegan a la webapp",
    "aiip-off": "la organización privada tiene AI-IP apagado",
    "bypass-mutates": "con AI-IP apagado, el proxy altera el cuerpo antes de reenviarlo",
    "delete-not-blocking": "tras borrar, el proxy sigue reenviando los turnos con historial",
    "delete-wrong-code": "tras borrar, el proxy responde 403 con el código de acceso denegado",
    "delete-not-removed": "la webapp responde al borrado pero no borra la conversación",
    "blocked-emits-events": "el turno rechazado de una conversación borrada vuelve a crearla en la webapp",
    "fresh-blocked": "tras borrar, también se bloquea una conversación nueva sin historial",
}


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode()


def sse_event(data: Any, name: str | None = None) -> str:
    head = f"event: {name}\n" if name else ""
    return f"{head}data: {json.dumps(data, ensure_ascii=False)}\n\n"


def parse_sse(raw: str) -> list[tuple[str | None, str]]:
    """Lector SSE mínimo para lo que emite el proveedor simulado (`event:` opcional y una línea `data:`)."""
    events = []
    for block in raw.split("\n\n"):
        name, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if data is not None:
            events.append((name, data))
    return events


# --- proveedor falso con eco ---------------------------------------------------------------------


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        kinds = ("text", "input_text", "output_text")
        return "".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") in kinds)
    return ""


def last_user_text(api: str, body: dict[str, Any]) -> str:
    if api == "responses":
        items = body.get("input")
        if isinstance(items, str):
            return items
        users = [i for i in items or [] if isinstance(i, dict) and i.get("role") == "user"]
    else:
        users = [m for m in body.get("messages") or [] if isinstance(m, dict) and m.get("role") == "user"]
    return _text_of(users[-1].get("content")) if users else ""


def pieces(text: str, seed: int = 7) -> list[str]:
    """Fragmentos de 1 a 7 caracteres, reproducibles, para que los tokens queden partidos entre eventos."""
    rng = random.Random(seed)  # noqa: S311 (troceado reproducible, no criptográfico)
    out, i = [], 0
    while i < len(text):
        n = rng.randint(1, 7)
        out.append(text[i : i + n])
        i += n
    return out


class EchoProvider:
    def __init__(self, debug_body: str = "json") -> None:
        self.debug_body = debug_body
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.counter = itertools.count(1)

    def record(self, path: str, raw: bytes) -> None:
        body: Any = raw.decode() if self.debug_body == "text" else json.loads(raw)
        with self.lock:
            self.requests.append({"path": path, "body": body})
            del self.requests[:-50]

    def respond(self, api: str, body: dict[str, Any]) -> tuple[str, bytes]:
        text = last_user_text(api, body)
        args = (next(self.counter), str(body.get("model") or "fake"), f"Eco: {text}", TOOL_TRIGGER in text)
        stream = bool(body.get("stream"))
        handler = {"messages": self._messages, "chat": self._chat, "responses": self._responses}[api]
        return handler(*args, stream)

    @staticmethod
    def _messages(n: int, model: str, eco: str, tool: bool, stream: bool) -> tuple[str, bytes]:
        args = json.dumps({"text": eco}, ensure_ascii=False)
        block: dict[str, Any] = {"type": "text", "text": eco}
        if tool:
            block = {"type": "tool_use", "id": f"toolu_fake_{n}", "name": "echo", "input": {"text": eco}}
        stop = "tool_use" if tool else "end_turn"
        usage = {"input_tokens": 12, "output_tokens": 9}
        message = {"id": f"msg_fake_{n}", "type": "message", "role": "assistant", "model": model,
                   "content": [block], "stop_reason": stop, "stop_sequence": None, "usage": usage}  # fmt: skip
        if not stream:
            return "application/json", encode(message)
        start = {**block, "input": {}} if tool else {"type": "text", "text": ""}

        def delta(part: str) -> dict[str, Any]:
            if tool:
                return {"type": "input_json_delta", "partial_json": part}
            return {"type": "text_delta", "text": part}

        events = [
            {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
            {"type": "content_block_start", "index": 0, "content_block": start},
            *({"type": "content_block_delta", "index": 0, "delta": delta(p)} for p in pieces(args if tool else eco)),
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 9},
            },
            {"type": "message_stop"},
        ]
        return "text/event-stream", "".join(sse_event(e, e["type"]) for e in events).encode()

    @staticmethod
    def _chat(n: int, model: str, eco: str, tool: bool, stream: bool) -> tuple[str, bytes]:
        args = json.dumps({"text": eco}, ensure_ascii=False)
        call = {"id": f"call_fake_{n}", "type": "function", "function": {"name": "echo", "arguments": args}}
        base = {"id": f"chatcmpl-fake-{n}", "created": 0, "model": model}
        finish = "tool_calls" if tool else "stop"
        if not stream:
            message: dict[str, Any] = {"role": "assistant", "content": eco}
            if tool:
                message = {"role": "assistant", "content": None, "tool_calls": [call]}
            choice = {"index": 0, "message": message, "finish_reason": finish}
            usage = {"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21}
            return "application/json", encode(
                {**base, "object": "chat.completion", "choices": [choice], "usage": usage}
            )

        def chunk(delta: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
            return {**base, "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}  # fmt: skip

        if tool:
            head = {"index": 0, "id": call["id"], "type": "function", "function": {"name": "echo", "arguments": ""}}
            first = chunk({"role": "assistant", "content": None, "tool_calls": [head]})
            parts = [chunk({"tool_calls": [{"index": 0, "function": {"arguments": p}}]}) for p in pieces(args)]
        else:
            first = chunk({"role": "assistant", "content": ""})
            parts = [chunk({"content": p}) for p in pieces(eco)]
        chunks = [first, *parts, chunk({}, finish)]
        return "text/event-stream", ("".join(sse_event(c) for c in chunks) + "data: [DONE]\n\n").encode()

    @staticmethod
    def _responses(n: int, model: str, eco: str, tool: bool, stream: bool) -> tuple[str, bytes]:
        args = json.dumps({"text": eco}, ensure_ascii=False)
        item_id = f"fc_fake_{n}" if tool else f"msg_fake_{n}"
        part = {"type": "output_text", "text": eco, "annotations": []}
        item: dict[str, Any] = {"id": item_id, "type": "message", "status": "completed", "role": "assistant",
                                "content": [part]}  # fmt: skip
        if tool:
            item = {"id": item_id, "type": "function_call", "status": "completed", "call_id": f"call_fake_{n}",
                    "name": "echo", "arguments": args}  # fmt: skip

        def response(status: str, output: list[Any]) -> dict[str, Any]:
            usage = {"input_tokens": 12, "output_tokens": 9, "total_tokens": 21} if status == "completed" else None
            return {"id": f"resp_fake_{n}", "object": "response", "status": status, "model": model, "output": output,
                    "usage": usage}  # fmt: skip

        if not stream:
            return "application/json", encode(response("completed", [item]))
        events: list[dict[str, Any]] = [
            {"type": "response.created", "response": response("in_progress", [])},
            {"type": "response.in_progress", "response": response("in_progress", [])},
        ]
        if tool:
            ref = {"item_id": item_id, "output_index": 0}
            events += [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "status": "in_progress", "arguments": ""},
                },
                *({"type": "response.function_call_arguments.delta", **ref, "delta": p} for p in pieces(args)),
                {"type": "response.function_call_arguments.done", **ref, "arguments": args},
            ]
        else:
            ref = {"item_id": item_id, "output_index": 0, "content_index": 0}
            events += [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "status": "in_progress", "content": []},
                },
                {"type": "response.content_part.added", **ref, "part": {**part, "text": ""}},
                *({"type": "response.output_text.delta", **ref, "delta": p} for p in pieces(eco)),
                {"type": "response.output_text.done", **ref, "text": eco},
                {"type": "response.content_part.done", **ref, "part": part},
            ]
        events += [
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response("completed", [item])},
        ]
        for i, event in enumerate(events):
            event["sequence_number"] = i
        return "text/event-stream", "".join(sse_event(e, e["type"]) for e in events).encode()


# --- vault y rehidratación del proxy -------------------------------------------------------------


@dataclass
class Vault:
    tokens: dict[str, str] = field(default_factory=dict)  # token → valor
    by_value: dict[tuple[str, str], str] = field(default_factory=dict)  # (tipo, valor) → token
    counters: dict[str, int] = field(default_factory=dict)

    def token(self, kind: str, value: str) -> str:
        if (kind, value) not in self.by_value:
            self.counters[kind] = self.counters.get(kind, 0) + 1
            tok = f"[[{kind}_{self.counters[kind]:03d}]]"
            self.by_value[(kind, value)] = tok
            self.tokens[tok] = value
        return self.by_value[(kind, value)]


def _count(counts: dict[str, int], kind: str) -> None:
    counts[kind] = counts.get(kind, 0) + 1


def anonymize(value: Any, vault: Vault, counts: dict[str, int], key: str | None = None) -> Any:
    if key in _STRUCTURAL:
        return value
    if isinstance(value, str):
        for kind, rx in RULES:

            def sub(m: re.Match[str], kind: str = kind) -> str:
                _count(counts, kind)
                return vault.token(kind, m.group(0))

            value = rx.sub(sub, value)
        return value
    if isinstance(value, dict):
        return {k: anonymize(v, vault, counts, k) for k, v in value.items()}
    if isinstance(value, list):
        return [anonymize(v, vault, counts) for v in value]
    return value


class Rehydrator:
    """Restaura los tokens del vault de la conversación; retiene lo que puede ser el comienzo de un token."""

    def __init__(self, vault: Vault, counts: dict[str, int], *, bad_tool_json: bool, skip_copies: bool) -> None:
        self.vault, self.counts = vault, counts
        self.bad_tool_json, self.skip_copies = bad_tool_json, skip_copies
        self.pending: dict[Any, str] = {}
        self.tool_blocks: dict[int, bool] = {}

    def restore(self, text: str, *, tool: bool = False) -> str:
        def sub(m: re.Match[str]) -> str:
            value = self.vault.tokens.get(m.group(0))
            if value is None:
                return m.group(0)
            _count(self.counts, m.group(0)[2:].rsplit("_", 1)[0])
            return value + '"' if tool and self.bad_tool_json else value  # el fallo: sin escapar

        return TOKEN_RE.sub(sub, text)

    def walk(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, str):
            return self.restore(value, tool=key in ("arguments", "partial_json"))
        if isinstance(value, dict):
            return {k: self.walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.walk(v, key) for v in value]
        return value

    def feed(self, key: Any, text: str, *, tool: bool = False) -> str:
        buf = self.pending.pop(key, "") + text
        cut = len(buf)
        for i in range(max(0, len(buf) - 32), len(buf)):
            if buf[i] == "[" and _TOKEN_PREFIX.match(buf, i):
                cut = i
                break
        if cut < len(buf):
            self.pending[key] = buf[cut:]
        return self.restore(buf[:cut], tool=tool)

    def flush(self, key: Any, *, tool: bool = False) -> str:
        return self.restore(self.pending.pop(key, ""), tool=tool)

    def json_body(self, body: bytes) -> bytes:
        return encode(self.walk(json.loads(body)))

    def stream(self, api: str, body: bytes) -> bytes:
        handler: Callable[[str | None, dict[str, Any]], list[tuple[str | None, Any]]] = {
            "messages": self._messages,
            "chat": self._chat,
            "responses": self._responses,
        }[api]
        out = []
        for name, data in parse_sse(body.decode()):
            if data.strip() == "[DONE]":
                out.append("data: [DONE]\n\n")
                continue
            out += [sse_event(ev, ev_name) for ev_name, ev in handler(name, json.loads(data))]
        return "".join(out).encode()

    def _messages(self, name: str | None, ev: dict[str, Any]) -> list[tuple[str | None, Any]]:
        kind, index = ev.get("type"), ev.get("index", 0)
        if kind == "content_block_start":
            self.tool_blocks[index] = (ev.get("content_block") or {}).get("type") in ("tool_use", "server_tool_use")
        elif kind == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                delta["text"] = self.feed(("m", index), delta["text"])
            elif delta.get("type") == "input_json_delta":
                delta["partial_json"] = self.feed(("m", index), delta["partial_json"], tool=True)
            return [(name, ev)]
        elif kind == "content_block_stop":
            tool = self.tool_blocks.get(index, False)
            rest = self.flush(("m", index), tool=tool)
            if not rest:
                return [(name, ev)]
            delta = {"type": "input_json_delta", "partial_json": rest} if tool else {"type": "text_delta", "text": rest}
            return [
                ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}),
                (name, ev),
            ]
        return [(name, self.walk(ev))]

    def _chat(self, name: str | None, ev: dict[str, Any]) -> list[tuple[str | None, Any]]:
        out: list[tuple[str | None, Any]] = []
        here: set[Any] = set()
        for choice in ev.get("choices") or []:
            ci, delta = choice.get("index", 0), choice.get("delta") or {}
            last = bool(choice.get("finish_reason"))
            if isinstance(delta.get("content"), str):
                key = ("c", ci)
                here.add(key)
                delta["content"] = self.feed(key, delta["content"]) + (self.flush(key) if last else "")
            for call in delta.get("tool_calls") or []:
                fn, key = call.get("function") or {}, ("t", ci, call.get("index", 0))
                if isinstance(fn.get("arguments"), str):
                    here.add(key)
                    fn["arguments"] = self.feed(key, fn["arguments"], tool=True) + (
                        self.flush(key, tool=True) if last else ""
                    )
            if last:  # lo retenido de otros campos de esta elección sale antes del fragmento final
                for key in [k for k in self.pending if k[1] == ci and k not in here]:
                    tool = key[0] == "t"
                    rest = self.flush(key, tool=tool)
                    extra = (
                        {"tool_calls": [{"index": key[2], "function": {"arguments": rest}}]}
                        if tool
                        else {"content": rest}
                    )
                    meta = {k: v for k, v in ev.items() if k != "choices"}
                    out.append((None, {**meta, "choices": [{"index": ci, "delta": extra, "finish_reason": None}]}))
        out.append((name, ev))
        return out

    def _responses(self, name: str | None, ev: dict[str, Any]) -> list[tuple[str | None, Any]]:
        kind = ev.get("type")
        item = ev.get("item_id", ev.get("output_index"))
        ref = {k: ev[k] for k in ("item_id", "output_index", "content_index") if k in ev}
        if kind == "response.output_text.delta":
            ev["delta"] = self.feed(("t", item, ev.get("content_index", 0)), ev["delta"])
            return [(name, ev)]
        if kind == "response.function_call_arguments.delta":
            ev["delta"] = self.feed(("a", item), ev["delta"], tool=True)
            return [(name, ev)]
        out: list[tuple[str | None, Any]] = []
        if kind == "response.output_text.done":
            rest = self.flush(("t", item, ev.get("content_index", 0)))
            if rest:
                out.append(("response.output_text.delta", {"type": "response.output_text.delta", **ref, "delta": rest}))
        elif kind == "response.function_call_arguments.done":
            rest = self.flush(("a", item), tool=True)
            if rest:
                delta_ev = {"type": "response.function_call_arguments.delta", **ref, "delta": rest}
                out.append(("response.function_call_arguments.delta", delta_ev))
        copies = (
            "response.output_text.done",
            "response.function_call_arguments.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        )
        out.append((name, ev if self.skip_copies and kind in copies else self.walk(ev)))
        return out


# --- estado compartido de proxy y webapp ---------------------------------------------------------


@dataclass
class Org:
    slug: str
    name: str
    ai_ip_enabled: bool


@dataclass
class User:
    email: str
    memberships: dict[str, str]  # slug → ORG_ADMIN · USER
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class Conversation:
    id: str
    org: Org
    user: User
    api: str
    model: str
    ai_ip: bool
    vault: Vault = field(default_factory=Vault)


def fingerprint(api: str, org: str, payload: dict[str, Any]) -> str:
    """Huella de la conversación: API, organización, *system prompt* y primer mensaje del usuario."""
    messages: list[Any]
    if api == "responses":
        system = payload.get("instructions")
        items = payload.get("input")
        messages = [{"role": "user", "content": items}] if isinstance(items, str) else list(items or [])
    else:
        messages = [m for m in payload.get("messages") or [] if isinstance(m, dict)]
        system = payload.get("system") if api == "messages" else [m for m in messages if m.get("role") == "system"]
    first = next((m.get("content") for m in messages if isinstance(m, dict) and m.get("role") == "user"), None)
    raw = json.dumps([api, org, system, first], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def has_history(api: str, payload: dict[str, Any]) -> bool:
    if api == "responses":
        items = payload.get("input") if isinstance(payload.get("input"), list) else []
        return bool(payload.get("previous_response_id")) or any(
            isinstance(i, dict) and (i.get("role") == "assistant" or i.get("type") in ("function_call",)) for i in items
        )
    return any(isinstance(m, dict) and m.get("role") == "assistant" for m in payload.get("messages") or [])


def response_id_of(body: bytes, stream: bool) -> str | None:
    try:
        if not stream:
            return json.loads(body).get("id")
        for _, data in parse_sse(body.decode()):
            event = json.loads(data)
            if event.get("type") in ("response.created", "response.completed"):
                return (event.get("response") or {}).get("id")
    except (ValueError, AttributeError):
        return None
    return None


def error_body(api: str, etype: str, code: str, message: str) -> bytes:
    if api == "messages":
        return encode({"type": "error", "error": {"type": etype, "message": message}})
    return encode({"error": {"message": message, "type": etype, "param": None, "code": code}})


_SEVERITY = {None: 0, "CLEAN": 1, "WARNING": 2, "ERROR": 3}


class StackState:
    def __init__(self, faults: set[str], provider_url: str) -> None:
        self.faults = faults
        self.provider_url = provider_url
        self.proxy_url = self.app_url = ""
        self.lock = threading.RLock()
        self.orgs = {
            "demo": Org("demo", "Demo Clínica", ai_ip_enabled=False),
            "privada": Org("privada", "Demo Privacidad", ai_ip_enabled="aiip-off" not in faults),
        }
        users = [
            User("analista@demo.test", {"demo": "USER"}),
            User("orgadmin@demo.test", {"demo": "ORG_ADMIN"}),
            User("analista@privada.test", {"privada": "USER"}),
            User("orgadmin@privada.test", {"privada": "ORG_ADMIN"}),
        ]
        self.users = {u.email: u for u in users}
        self.by_key = {dev_api_key(u.email): u for u in users}
        self.sessions: dict[str, User] = {}
        self.conversations: dict[str, Conversation] = {}  # proxy: conversaciones conocidas
        self.fingerprints: dict[str, str] = {}  # proxy: huella → conversación
        self.response_ids: dict[str, str] = {}  # proxy: id de respuesta → conversación
        self.tombstones: set[str] = set()  # proxy: conversaciones borradas
        self.visible: dict[str, dict[str, Any]] = {}  # webapp: conversaciones con sus turnos

    # proxy
    def handshake(self, user: User) -> dict[str, Any]:
        orgs = []
        for slug, role in user.memberships.items():
            org = self.orgs[slug]
            orgs.append({
                "slug": slug, "name": org.name, "governance_role": role, "functional_roles": [],
                "paused": False, "blocked": False, "ai_ip_enabled": org.ai_ip_enabled,
                "mcp_url": f"{self.app_url}/org/{slug}/mcp",
                "proxy_anthropic_url": f"{self.proxy_url}/org/{slug}/anthropic",
                "proxy_openai_url": f"{self.proxy_url}/org/{slug}/openai/v1",
            })  # fmt: skip
        user_json = {"id": user.id, "email": user.email, "platform_role": "USER"}
        return {"user": user_json, "global_kill_switch": False, "organizations": orgs}

    def _resolve(self, api: str, org: Org, user: User, payload: dict[str, Any]) -> tuple[Conversation, bool]:
        """(Conversación, rechazada por su lápida)."""
        fp = fingerprint(api, org.slug, payload)
        prev = payload.get("previous_response_id")
        conv_id = self.response_ids.get(prev) if prev else self.fingerprints.get(fp)
        if conv_id in self.tombstones and "delete-not-blocking" not in self.faults:
            if has_history(api, payload) or "fresh-blocked" in self.faults:
                return self.conversations[conv_id], True
            conv_id = None  # sin historial: conversación nueva aunque la huella coincida
        conv = self.conversations.get(conv_id or "")
        if conv is None:
            conv = Conversation(conv_id or str(uuid.uuid4()), org, user, api, str(payload.get("model")),
                                ai_ip=org.ai_ip_enabled)  # fmt: skip
            self.conversations[conv.id] = conv
        self.fingerprints.setdefault(fp, conv.id)
        if self.fingerprints[fp] in self.tombstones:
            self.fingerprints[fp] = conv.id
        return conv, False

    def proxy_turn(self, slug: str, route: str, headers: Any, raw: bytes) -> tuple[int, str, bytes]:
        api, upstream = ROUTES[route]
        user = self.by_key.get(headers.get("X-Baas-Api-Key") or "")
        if user is None:
            etype = "authentication_error" if api == "messages" else "invalid_request_error"
            return 401, "application/json", error_body(api, etype, "baas_invalid_api_key", "API key no válida")
        org = self.orgs.get(slug)
        if org is None or slug not in user.memberships:
            return 403, "application/json", error_body(api, "permission_error", "baas_access_denied", DENIED_MESSAGE)
        payload = json.loads(raw)
        with self.lock:
            conv, blocked = self._resolve(api, org, user, payload)
        if blocked:
            if "blocked-emits-events" in self.faults:
                self._record(conv, None, blocked=True)
            if "delete-wrong-code" in self.faults:
                return (
                    403,
                    "application/json",
                    error_body(api, "permission_error", "baas_access_denied", DENIED_MESSAGE),
                )
            body = error_body(api, "permission_error", "baas_conversation_deleted", DELETED_MESSAGE)
            return 403, "application/json", body
        stream = bool(payload.get("stream"))
        request_counts: dict[str, int] = {}
        sent = raw
        if conv.ai_ip and "leak" not in self.faults:
            with self.lock:
                sent = encode(anonymize(payload, conv.vault, request_counts))
        elif not conv.ai_ip and "bypass-mutates" in self.faults:
            sent = encode({**payload, "baas_e2e": True})
        status, ctype, body = self._forward(upstream, sent)
        response_counts: dict[str, int] = {}
        if conv.ai_ip and status == 200 and "no-rehydrate" not in self.faults:
            rehydrator = Rehydrator(conv.vault, response_counts, bad_tool_json="bad-tool-json" in self.faults,
                                    skip_copies="responses-done-unrestored" in self.faults)  # fmt: skip
            body = rehydrator.stream(api, body) if "event-stream" in ctype else rehydrator.json_body(body)
        if api == "responses" and status == 200:
            rid = response_id_of(body, stream)
            if rid:
                with self.lock:
                    self.response_ids[rid] = conv.id
        verification = None
        if conv.ai_ip:
            unresolved = len(TOKEN_RE.findall(body.decode(errors="replace")))
            warning = "warning" in self.faults
            verification = {
                "status": "WARNING" if unresolved or warning else "CLEAN",
                "request_replacements_by_type": request_counts,
                "response_replacements_by_type": response_counts,
                "unresolved_response_tokens": max(unresolved, int(warning)),
                "orphan_request_tokens": 0,
                "residual_matches": [],
            }
        self._record(conv, verification)
        return status, ctype, body

    def _forward(self, path: str, body: bytes) -> tuple[int, str, bytes]:
        req = urllib.request.Request(self.provider_url + path, data=body, method="POST")  # noqa: S310
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (proveedor simulado local)
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", "") if exc.headers else "", exc.read()

    def _record(self, conv: Conversation, verification: dict[str, Any] | None, *, blocked: bool = False) -> None:
        """Lo que haría la webapp al consumir los eventos del turno."""
        if "no-trace" in self.faults:
            return
        with self.lock:
            rec = self.visible.setdefault(conv.id, {
                "id": conv.id, "org_slug": conv.org.slug,
                "user": {"id": conv.user.id, "email": conv.user.email, "name": conv.user.email},
                "provider": "ANTHROPIC" if conv.api == "messages" else "OPENAI", "api_flavor": FLAVORS[conv.api],
                "model": conv.model, "ai_ip_enabled": conv.ai_ip, "status": "ACTIVE", "turn_count": 0,
                "verification_status": None, "turns": [],
            })  # fmt: skip
            status = verification["status"] if verification else None
            rec["turn_count"] += 1
            rec["turns"].append({
                "turn_index": rec["turn_count"], "status": "BLOCKED" if blocked else "COMPLETED",
                "verification_status": status, "verification": verification,
            })  # fmt: skip
            if _SEVERITY[status] > _SEVERITY[rec["verification_status"]]:
                rec["verification_status"] = status

    # webapp
    def can_view(self, viewer: User, rec: dict[str, Any]) -> bool:
        return viewer.memberships.get(rec["org_slug"]) == "ORG_ADMIN" or rec["user"]["email"] == viewer.email

    def delete(self, viewer: User, conv_id: str) -> tuple[int, dict[str, Any]]:
        with self.lock:
            rec = self.visible.get(conv_id)
            if rec is None or not self.can_view(viewer, rec):
                return 404, {"error": "NOT_FOUND", "message": "Conversation not found"}
            if viewer.memberships.get(rec["org_slug"]) != "ORG_ADMIN":
                return 403, {"error": "FORBIDDEN", "message": "Only an ORG_ADMIN can delete conversations"}
            if "delete-not-removed" not in self.faults:
                del self.visible[conv_id]
            self.tombstones.add(conv_id)  # conversation.purge → lápida en el proxy
            if conv_id in self.conversations:
                self.conversations[conv_id].vault = Vault()
        return 200, {"propagated": True}


# --- servidores HTTP -----------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # silencioso
        return

    def _send(self, status: int, ctype: str, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        if body or status != 204:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
        self._send(status, "application/json", encode(data), headers)

    def _read(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))


def provider_handler(provider: EchoProvider) -> type[BaseHTTPRequestHandler]:
    class Handler(_Handler):
        def do_GET(self) -> None:
            if self.path == "/_debug/requests":
                with provider.lock:
                    return self._json(200, {"requests": list(provider.requests)})
            return self._json(404, {"error": "no simulado"})

        def do_DELETE(self) -> None:
            if self.path == "/_debug/requests":
                with provider.lock:
                    provider.requests.clear()
                return self._send(204, "")
            return self._json(404, {"error": "no simulado"})

        def do_POST(self) -> None:
            raw = self._read()
            api = {path: api for api, path in ROUTES.values()}.get(self.path)
            if api is None:
                return self._json(404, {"error": "no simulado"})
            provider.record(self.path, raw)
            ctype, body = provider.respond(api, json.loads(raw))
            return self._send(200, ctype, body)

    return Handler


_PROXY_PATH = re.compile(r"/org/([a-z0-9-]+)/(" + "|".join(re.escape(r) for r in ROUTES) + r")")


def proxy_handler(state: StackState) -> type[BaseHTTPRequestHandler]:
    class Handler(_Handler):
        def do_POST(self) -> None:
            raw = self._read()
            m = _PROXY_PATH.fullmatch(self.path)
            if m is None:
                return self._json(404, {"error": {"type": "not_found_error", "message": "ruta no simulada"}})
            status, ctype, body = state.proxy_turn(m.group(1), m.group(2), self.headers, raw)
            return self._send(status, ctype, body)

    return Handler


_CONV = re.compile(r"/api/v1/conversations/([0-9a-f-]+)")
_TURN = re.compile(r"/api/v1/conversations/([0-9a-f-]+)/turns/(\d+)")
_DELETE = re.compile(r"/api/v1/conversations/([0-9a-f-]+)/delete")
_CSRF = {"error": "CSRF_FAILED", "message": "X-Requested-With: baas-webapp required"}


def webapp_handler(state: StackState) -> type[BaseHTTPRequestHandler]:
    class Handler(_Handler):
        def _viewer(self) -> User | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "baas_session":
                    return state.sessions.get(value)
            return None

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == "/healthz":
                return self._json(200, {"status": "ok", "version": "simulada"})
            viewer = self._viewer()
            if viewer is None:
                return self._json(401, {"error": "UNAUTHENTICATED", "message": "Not authenticated"})
            with state.lock:
                visible = [r for r in state.visible.values() if state.can_view(viewer, r)]
            if url.path == "/api/v1/conversations":
                query = parse_qs(url.query)
                org = (query.get("org") or [None])[0]
                term = ((query.get("q") or [""])[0]).lower()
                items = [
                    {k: v for k, v in r.items() if k != "turns"}
                    for r in visible
                    if (not org or r["org_slug"] == org)
                    and term in f"{r['model']} {r['user']['email']} {r['id']}".lower()
                ]
                limit = int((query.get("limit") or ["50"])[0])
                return self._json(200, {"items": items[:limit], "total": len(items), "limit": limit, "offset": 0})
            by_id = {r["id"]: r for r in visible}
            if m := _TURN.fullmatch(url.path):
                turns = (by_id.get(m.group(1)) or {}).get("turns") or []
                turn = next((t for t in turns if t["turn_index"] == int(m.group(2))), None)
                if turn is None:
                    return self._json(404, {"error": "NOT_FOUND", "message": "Turn not found"})
                return self._json(200, turn)
            if (m := _CONV.fullmatch(url.path)) and m.group(1) in by_id:
                rec = by_id[m.group(1)]
                summaries = [{k: v for k, v in t.items() if k != "verification"} for t in rec["turns"]]
                return self._json(200, {**rec, "turns": summaries})
            return self._json(404, {"error": "NOT_FOUND", "message": "Not found"})

        def do_POST(self) -> None:
            raw = self._read()
            if self.path == "/api/v1/plugin/handshake":
                auth = self.headers.get("Authorization") or ""
                user = state.by_key.get(auth.removeprefix("Bearer ").strip()) if auth.startswith("Bearer ") else None
                if user is None:
                    return self._json(401, {"error": "UNAUTHENTICATED", "message": "Invalid API key"})
                return self._json(200, state.handshake(user))
            if self.headers.get("X-Requested-With") != "baas-webapp":
                return self._json(403, _CSRF)
            body = json.loads(raw or b"{}")
            if self.path == "/api/v1/auth/login":
                user = state.users.get(str(body.get("email")))
                if user is None or body.get("password") != DEV_PASSWORD:
                    return self._json(401, {"error": "INVALID_CREDENTIALS", "message": "Invalid credentials"})
                token = secrets.token_urlsafe(16)
                state.sessions[token] = user
                cookie = f"baas_session={token}; HttpOnly; Path=/; SameSite=lax"
                return self._json(200, {"user": {"email": user.email}}, {"Set-Cookie": cookie})
            viewer = self._viewer()
            if viewer is None:
                return self._json(401, {"error": "UNAUTHENTICATED", "message": "Not authenticated"})
            if m := _DELETE.fullmatch(self.path):
                reason = str(body.get("reason") or "").strip()
                if not 3 <= len(reason) <= 500:
                    return self._json(422, {"error": "REASON_REQUIRED", "message": "A reason is required"})
                status, data = state.delete(viewer, m.group(1))
                return self._json(status, data)
            return self._json(404, {"error": "NOT_FOUND", "message": "Not found"})

    return Handler


class _Server:
    def __init__(self, handler: type[BaseHTTPRequestHandler], host: str, port: int = 0) -> None:
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.url = f"http://{host}:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeH3Stack:
    """Las tres piezas en puertos efímeros de `host`. `faults` ⊆ `FAULTS`; `debug_body` = `json` o `text`."""

    def __init__(self, faults: Iterable[str] = (), *, debug_body: str = "json", host: str = "127.0.0.1") -> None:
        chosen = set(faults)
        unknown = chosen - FAULTS.keys()
        if unknown:
            raise ValueError(f"fallos desconocidos: {', '.join(sorted(unknown))}")
        if debug_body not in ("json", "text"):
            raise ValueError("debug_body debe ser «json» o «text»")
        self.provider = EchoProvider(debug_body)
        self._servers = [_Server(provider_handler(self.provider), host)]
        self.state = StackState(chosen, self._servers[0].url)
        self._servers += [_Server(proxy_handler(self.state), host), _Server(webapp_handler(self.state), host)]
        self.state.proxy_url, self.state.app_url = self._servers[1].url, self._servers[2].url

    @property
    def fake_url(self) -> str:
        return self._servers[0].url

    @property
    def proxy_url(self) -> str:
        return self._servers[1].url

    @property
    def app_url(self) -> str:
        return self._servers[2].url

    def close(self) -> None:
        for server in self._servers:
            server.close()

    def __enter__(self) -> FakeH3Stack:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pila H3 simulada para probar dev/e2e_h3.py")
    p.add_argument("--fault", action="append", choices=sorted(FAULTS), default=[], help="Fallo que se inyecta")
    p.add_argument("--debug-body", choices=("json", "text"), default="json", help="Cómo guarda los cuerpos")
    p.add_argument("--host", default="127.0.0.1")
    ns = p.parse_args(argv)
    with FakeH3Stack(ns.fault, debug_body=ns.debug_body, host=ns.host) as stack:
        print(f"webapp   {stack.app_url}\nproxy    {stack.proxy_url}\nproveedor {stack.fake_url}")
        for fault in ns.fault:
            print(f"fallo    {fault}: {FAULTS[fault]}")
        print(f"\nuv run python dev/e2e_h3.py --app-url {stack.app_url} --fake-url {stack.fake_url}", flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
