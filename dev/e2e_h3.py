#!/usr/bin/env python3
"""Prueba de punta a punta de AI-IP (hito H3) contra una pila de BaaS-AI-IP con el proveedor falso con eco.

Por HTTP directo al proxy (sin Claude Code), con la clave determinista de `analista@privada.test` (contrato §8):

  0-3. La webapp responde; `/_debug/requests` del proveedor falso responde y se vacía; *handshake* de
       `analista@privada.test` (AI-IP activo) y de `analista@demo.test` (AI-IP apagado); sesión en la webapp
       como `orgadmin@privada.test`.
  4.   Messages, Chat Completions y Responses, con y sin *streaming*: el texto
       `DNI 12345678Z, email ana.perez@privada.test, IBAN ES91 2100 0418 4502 0005 1332` vuelve con los tres
       valores y sin `[[`, y el proveedor solo recibió `[[DNI_001]]`, `[[EMAIL_001]]` e `[[IBAN_001]]`.
  5.   Lo mismo pidiendo una herramienta (`usa la herramienta`): los argumentos de la llamada vuelven restaurados
       y son JSON válido en todas sus copias.
  6.   Cada turno aparece en `GET /api/v1/conversations` con la verificación `CLEAN`.
  7.   Con `analista@demo.test` (AI-IP apagado), lo que recibe el proveedor es idéntico a lo que envía el cliente.
  8.   Tras borrar la conversación como `orgadmin@privada.test`, el siguiente turno con historial recibe `403`
       (`baas_conversation_deleted`), no llega al proveedor y la conversación no reaparece.
  9.   El mismo primer mensaje, sin historial, abre una conversación nueva.
  10.  Ninguna petición que recibió el proveedor (salvo las de bypass) contiene los valores.

Uso (pila de `deploy` con el proveedor falso con eco: `scripts/dev-up.sh --fake-upstream`):

    uv run python dev/e2e_h3.py --app-url http://app.localhost:8080 --fake-url http://127.0.0.1:18090

Sale con 0 si todo pasa, 1 si algún paso falla y 2 si los argumentos no son válidos. No se distribuye con el
plugin. `dev/fake_h3_stack.py` simula la pila para probar este script sin ella.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import string
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_h2 import (  # utilidades de la prueba de H2
    DEV_PASSWORD,
    DEV_TOTP_SECRET,
    Http,
    Runner,
    StepError,
    check,
    cookies_from,
    dev_api_key,
    totp,
)

__all__ = ["Http", "Runner", "StepError", "check", "cookies_from", "dev_api_key", "totp"]

USER_AGENT = "baas-e2e-h3/1"
FAKE_ANTHROPIC_KEY = "sk-ant-api03-FAKE-e2e-h3-provider-key"  # credenciales de proveedor ficticias
FAKE_OPENAI_KEY = "sk-FAKE-e2e-h3-provider-key"

DNI, EMAIL, IBAN = "12345678Z", "ana.perez@privada.test", "ES91 2100 0418 4502 0005 1332"
VALUES = {"DNI": DNI, "EMAIL": EMAIL, "IBAN": IBAN}
SENSITIVE_TEXT = f"DNI {DNI}, email {EMAIL}, IBAN {IBAN}"
TOKENS = ("[[DNI_001]]", "[[EMAIL_001]]", "[[IBAN_001]]")
TOKEN_RE = re.compile(r"\[\[[A-Z_]+_[0-9]{3,}\]\]")
TOOL_TRIGGER = "usa la herramienta"
TOOL_NAME = "echo"
SYSTEM = "Eres el asistente de la prueba de punta a punta de AI-IP."

APIS = ("messages", "chat", "responses")
API_NAMES = {"messages": "Messages", "chat": "Chat Completions", "responses": "Responses"}
UPSTREAM_PATHS = {"messages": "/v1/messages", "chat": "/v1/chat/completions", "responses": "/v1/responses"}
GROUPS = ("aiip", "bypass", "borrado")
MODES = ("json", "stream")


# --- utilidades puras ------------------------------------------------------------------------------


def encode(payload: Any) -> bytes:
    """Cuerpo JSON tal como lo envía el cliente (UTF-8, sin escapes ASCII)."""
    return json.dumps(payload, ensure_ascii=False).encode()


def canonical(value: Any) -> str:
    """JSON canónico para comparar sin confundir `true` con `1` ni `1` con `1.0`."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def strings_in(value: Any) -> list[str]:
    """Todas las cadenas (claves incluidas) de un valor JSON."""
    out: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for k, v in item.items():
                out.append(str(k))
                stack.append(v)
        elif isinstance(item, list):
            stack.extend(item)
    return out


def load_json(raw: str | bytes, where: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
        raise StepError(f"{where}: no es JSON ({exc}): {text[:200]!r}") from None


def parse_sse(raw: str) -> list[tuple[str | None, str]]:
    """Eventos SSE como (`event` o None, `data` con las líneas unidas por `\\n`)."""
    events: list[tuple[str | None, str]] = []
    name: str | None = None
    data: list[str] = []
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line:
            if data:
                events.append((name, "\n".join(data)))
            name, data = None, []
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if key == "event":
            name = value
        elif key == "data":
            data.append(value)
    if data:
        events.append((name, "\n".join(data)))
    return events


@dataclass
class ToolCall:
    name: str
    arguments: Any  # texto JSON (o el objeto `input` de Anthropic sin streaming)
    where: str


@dataclass
class Reply:
    """Lo que el cliente reconstruye de una respuesta: cada copia del texto y de cada llamada a herramienta."""

    texts: list[tuple[str, str]] = field(default_factory=list)  # (dónde, texto)
    tool_calls: list[ToolCall] = field(default_factory=list)
    response_id: str | None = None


def _fail_on_error_event(event: Any, where: str) -> None:
    if isinstance(event, dict) and (event.get("type") in ("error", "response.failed") or "error" in event):
        raise StepError(f"{where}: el stream trae un error: {json.dumps(event, ensure_ascii=False)[:300]}")


def _messages_reply(body: bytes, stream: bool) -> Reply:
    reply = Reply()
    if not stream:
        data = load_json(body, "respuesta de Messages")
        for i, block in enumerate(data.get("content") or []):
            if block.get("type") == "text":
                reply.texts.append((f"content[{i}].text", str(block.get("text") or "")))
            elif block.get("type") in ("tool_use", "server_tool_use"):
                reply.tool_calls.append(ToolCall(str(block.get("name")), block.get("input"), f"content[{i}].input"))
        return reply
    blocks: dict[int, dict[str, Any]] = {}
    for _, raw in parse_sse(body.decode(errors="replace")):
        event = load_json(raw, "evento de Messages")
        _fail_on_error_event(event, "Messages")
        kind, index = event.get("type"), event.get("index", 0)
        if kind == "content_block_start":
            start = event.get("content_block") or {}
            blocks[index] = {"start": start, "text": str(start.get("text") or ""), "json": []}
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            slot = blocks.setdefault(index, {"start": {"type": "text"}, "text": "", "json": []})
            if delta.get("type") == "text_delta":
                slot["text"] += str(delta.get("text") or "")
            elif delta.get("type") == "input_json_delta":
                slot["json"].append(str(delta.get("partial_json") or ""))
    for index in sorted(blocks):
        slot, start = blocks[index], blocks[index]["start"]
        if start.get("type") == "text":
            reply.texts.append((f"bloque {index} (text_delta)", slot["text"]))
        elif start.get("type") in ("tool_use", "server_tool_use"):
            args = "".join(slot["json"]) if slot["json"] else json.dumps(start.get("input") or {})
            reply.tool_calls.append(ToolCall(str(start.get("name")), args, f"bloque {index} (input_json_delta)"))
    return reply


def _chat_reply(body: bytes, stream: bool) -> Reply:
    reply = Reply()
    if not stream:
        data = load_json(body, "respuesta de Chat Completions")
        for ci, choice in enumerate(data.get("choices") or []):
            message = choice.get("message") or {}
            if isinstance(message.get("content"), str) and message["content"]:
                reply.texts.append((f"choices[{ci}].message.content", message["content"]))
            for ti, call in enumerate(message.get("tool_calls") or []):
                fn = call.get("function") or {}
                where = f"choices[{ci}].message.tool_calls[{ti}].function.arguments"
                reply.tool_calls.append(ToolCall(str(fn.get("name")), fn.get("arguments"), where))
        return reply
    content: dict[int, str] = {}
    tools: dict[tuple[int, int], dict[str, str]] = {}
    for _, raw in parse_sse(body.decode(errors="replace")):
        if raw.strip() == "[DONE]":
            continue
        chunk = load_json(raw, "fragmento de Chat Completions")
        _fail_on_error_event(chunk, "Chat Completions")
        for choice in chunk.get("choices") or []:
            ci = int(choice.get("index") or 0)
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content[ci] = content.get(ci, "") + delta["content"]
            for call in delta.get("tool_calls") or []:
                slot = tools.setdefault((ci, int(call.get("index") or 0)), {"name": "", "args": ""})
                fn = call.get("function") or {}
                slot["name"] = slot["name"] or str(fn.get("name") or "")
                slot["args"] += str(fn.get("arguments") or "")
    for ci in sorted(content):
        if content[ci]:
            reply.texts.append((f"choices[{ci}].delta.content", content[ci]))
    for (ci, ti), slot in sorted(tools.items()):
        where = f"choices[{ci}].delta.tool_calls[{ti}].function.arguments"
        reply.tool_calls.append(ToolCall(slot["name"], slot["args"], where))
    return reply


def _responses_items(items: Any, where: str, reply: Reply) -> None:
    for i, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for j, part in enumerate(item.get("content") or []):
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    reply.texts.append((f"{where}[{i}].content[{j}].text", part["text"]))
        elif item.get("type") == "function_call":
            reply.tool_calls.append(ToolCall(str(item.get("name")), item.get("arguments"), f"{where}[{i}].arguments"))


def _responses_reply(body: bytes, stream: bool) -> Reply:
    reply = Reply()
    if not stream:
        data = load_json(body, "respuesta de Responses")
        reply.response_id = data.get("id")
        _responses_items(data.get("output"), "output", reply)
        return reply
    copies = Reply()  # las copias completas, después de los deltas
    text_deltas: dict[Any, str] = {}
    arg_deltas: dict[Any, str] = {}
    names: dict[Any, str] = {}
    for _, raw in parse_sse(body.decode(errors="replace")):
        event = load_json(raw, "evento de Responses")
        _fail_on_error_event(event, "Responses")
        kind = event.get("type")
        key = event.get("item_id", event.get("output_index"))
        if kind in ("response.created", "response.in_progress", "response.completed"):
            response = event.get("response") or {}
            reply.response_id = response.get("id") or reply.response_id
            if kind == "response.completed":
                _responses_items(response.get("output"), "response.completed.output", copies)
        elif kind == "response.output_text.delta":
            text_key = (key, event.get("content_index", 0))
            text_deltas[text_key] = text_deltas.get(text_key, "") + str(event.get("delta") or "")
        elif kind == "response.output_text.done":
            copies.texts.append(("response.output_text.done", str(event.get("text") or "")))
        elif kind == "response.content_part.done":
            part = event.get("part") or {}
            if isinstance(part.get("text"), str):
                copies.texts.append(("response.content_part.done", part["text"]))
        elif kind == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                names[item.get("id")] = names[event.get("output_index")] = str(item.get("name") or "")
        elif kind == "response.function_call_arguments.delta":
            arg_deltas[key] = arg_deltas.get(key, "") + str(event.get("delta") or "")
        elif kind == "response.function_call_arguments.done":
            name = names.get(event.get("item_id")) or names.get(event.get("output_index")) or str(event.get("name"))
            where = "response.function_call_arguments.done"
            copies.tool_calls.append(ToolCall(name, event.get("arguments"), where))
        elif kind == "response.output_item.done":
            _responses_items([event.get("item")], "response.output_item.done", copies)
    for text in text_deltas.values():
        reply.texts.append(("deltas de response.output_text.delta", text))
    for key, args in arg_deltas.items():
        reply.tool_calls.append(ToolCall(names.get(key, ""), args, "deltas de response.function_call_arguments.delta"))
    reply.texts += copies.texts
    reply.tool_calls += copies.tool_calls
    return reply


def extract_reply(api: str, body: bytes, *, stream: bool) -> Reply:
    """Textos y llamadas a herramienta de una respuesta de `api`, con todas sus copias."""
    parsers: dict[str, Callable[[bytes, bool], Reply]] = {
        "messages": _messages_reply,
        "chat": _chat_reply,
        "responses": _responses_reply,
    }
    return parsers[api](body, stream)


def restoration_problems(text: str) -> list[str]:
    """Qué le falta a `text` para estar restaurado: los tres valores y ningún `[[`."""
    problems = [f"falta el {kind} original «{value}»" for kind, value in VALUES.items() if value not in text]
    if "[[" in text:
        found = sorted(set(TOKEN_RE.findall(text)))
        if found:
            problems.append(f"quedan tokens sin restaurar: {', '.join(found)}")
        else:
            at = text.index("[[")
            problems.append(f"queda «[[» (token partido o sin restaurar): {text[max(0, at - 10) : at + 20]!r}")
    return problems


def tool_problems(arguments: Any) -> list[str]:
    """Los argumentos de una llamada son un objeto JSON válido con los tres valores y sin `[[`."""
    value = arguments
    if isinstance(arguments, str):
        try:
            value = json.loads(arguments)
        except ValueError as exc:
            return [f"JSON inválido ({exc}): {arguments[:120]!r}"]
    if not isinstance(value, dict):
        return [f"los argumentos no son un objeto JSON ({type(value).__name__}): {str(arguments)[:120]!r}"]
    return restoration_problems("\n".join(strings_in(value)))


def debug_text(entry: dict[str, Any]) -> str:
    """Texto buscable de una petición de `/_debug/requests`: el cuerpo tal cual y sus cadenas decodificadas."""
    body = entry.get("body")
    parsed: Any = body
    if isinstance(body, str):
        raw = body
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
    else:
        raw = json.dumps(body, ensure_ascii=False)
    return "\n".join([raw, *strings_in(parsed)])


def tagged(entries: Iterable[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    return [e for e in entries if tag in debug_text(e)]


def same_payload(sent: bytes, received: Any) -> tuple[bool, bool]:
    """(Idéntico como JSON, idéntico byte a byte) entre lo enviado y el `body` que guardó el proveedor."""
    expected = canonical(json.loads(sent))
    if isinstance(received, str):
        try:
            parsed = json.loads(received)
        except ValueError:
            return False, False
        same = canonical(parsed) == expected
        return same, same and received.encode() == sent
    return canonical(received) == expected, False


def provider_error(body: bytes) -> tuple[str | None, str | None, str]:
    """(`error.type`, `error.code`, `error.message`) de un error de Anthropic o de OpenAI."""
    try:
        data = json.loads(body)
    except ValueError:
        return None, None, ""
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None, None, ""
    return err.get("type"), err.get("code"), str(err.get("message") or "")


def new_nonce(length: int = 10) -> str:
    """Identificador de la ejecución, solo con letras: ninguna regla de AI-IP lo reconoce."""
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(length))


def case_tag(nonce: str, group: str, api: str, mode: str) -> str:
    return f"caso-{nonce}-{group}-{api}-{mode}"


def case_model(nonce: str, group: str, api: str, mode: str) -> str:
    return f"e2e-h3-{nonce}-{group}-{api}-{mode}"


_TOOL_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "Texto que se devuelve tal cual"}},
    "required": ["text"],
}
_TOOL_DESCRIPTION = "Devuelve el texto que recibe."


def request_body(
    api: str,
    model: str,
    text: str,
    *,
    stream: bool = False,
    tool: bool = False,
    history: Sequence[dict[str, Any]] = (),
    previous_response_id: str | None = None,
) -> dict[str, Any]:
    """Cuerpo de la petición del cliente para `api` (el último mensaje del usuario es `text`)."""
    if api == "messages":
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 1024,
            "system": SYSTEM,
            "messages": [*history, {"role": "user", "content": text}],
        }
        if tool:
            body["tools"] = [{"name": TOOL_NAME, "description": _TOOL_DESCRIPTION, "input_schema": _TOOL_SCHEMA}]
    elif api == "chat":
        body = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM}, *history, {"role": "user", "content": text}],
        }
        if tool:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": TOOL_NAME, "description": _TOOL_DESCRIPTION, "parameters": _TOOL_SCHEMA},
                }
            ]
    elif api == "responses":
        message = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
        body = {"model": model, "instructions": SYSTEM, "input": [*history, message]}
        if previous_response_id:
            body["previous_response_id"] = previous_response_id
        if tool:
            body["tools"] = [
                {"type": "function", "name": TOOL_NAME, "description": _TOOL_DESCRIPTION, "parameters": _TOOL_SCHEMA}
            ]
    else:
        raise ValueError(f"API desconocida: {api}")
    body["stream"] = stream
    return body


def api_url(binding: dict[str, Any], api: str) -> str:
    if api == "messages":
        return str(binding["proxy_anthropic_url"]).rstrip("/") + "/v1/messages"
    suffix = {"chat": "/chat/completions", "responses": "/responses"}[api]
    return str(binding["proxy_openai_url"]).rstrip("/") + suffix


def override_proxy(binding: dict[str, Any], proxy_url: str) -> dict[str, Any]:
    """Las URLs del proxy con otro origen y las rutas del contrato (`/org/<slug>/anthropic` y `/openai/v1`)."""
    base, slug = proxy_url.rstrip("/"), binding["slug"]
    return {
        **binding,
        "proxy_anthropic_url": f"{base}/org/{slug}/anthropic",
        "proxy_openai_url": f"{base}/org/{slug}/openai/v1",
    }


# --- argumentos ------------------------------------------------------------------------------------


def _csv(choices: Sequence[str]) -> Callable[[str], list[str]]:
    def parse(raw: str) -> list[str]:
        chosen = [x.strip() for x in raw.split(",") if x.strip()]
        unknown = [x for x in chosen if x not in choices]
        if unknown or not chosen:
            raise argparse.ArgumentTypeError(f"valores admitidos: {', '.join(choices)} (separados por comas)")
        return [c for c in choices if c in chosen]

    return parse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    http_port = os.environ.get("BAAS_DEV_HTTP_PORT") or "8080"
    fake_port = os.environ.get("BAAS_DEV_FAKE_UPSTREAM_PORT") or "18090"
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--app-url", default=os.environ.get("BAAS_E2E_APP_URL") or f"http://app.localhost:{http_port}",
                   help="URL pública de la webapp (las del proxy salen del handshake)")  # fmt: skip
    p.add_argument("--fake-url", default=os.environ.get("BAAS_E2E_FAKE_URL") or f"http://127.0.0.1:{fake_port}",
                   help="URL del proveedor falso publicada en el host (para /_debug/requests)")  # fmt: skip
    p.add_argument("--proxy-url", default=os.environ.get("BAAS_E2E_PROXY_URL") or None,
                   help="Sustituye el origen de las URLs del proxy del handshake (conserva /org/<slug>/…)")  # fmt: skip
    p.add_argument("--org", default="privada", help="Organización con AI-IP activo")
    p.add_argument("--email", default="analista@privada.test", help="Usuario de la organización con AI-IP")
    p.add_argument("--admin-email", default="orgadmin@privada.test", help="ORG_ADMIN que ve las trazas y borra")
    p.add_argument("--bypass-org", default="demo", help="Organización con AI-IP apagado")
    p.add_argument("--bypass-email", default="analista@demo.test", help="Usuario de la organización sin AI-IP")
    p.add_argument("--password", default=DEV_PASSWORD)
    p.add_argument("--totp-secret", default=DEV_TOTP_SECRET)
    p.add_argument("--only", type=_csv(GROUPS), default=list(GROUPS),
                   help=f"Partes que se ejecutan, separadas por comas: {', '.join(GROUPS)} (todas)")  # fmt: skip
    p.add_argument("--apis", type=_csv(APIS), default=list(APIS),
                   help=f"APIs que se prueban, separadas por comas: {', '.join(APIS)} (todas)")  # fmt: skip
    p.add_argument("--trace-timeout", type=float, default=30, help="Segundos de espera de cada traza en la webapp")
    p.add_argument("--settle", type=float, default=2, help="Segundos de espera antes de comprobar que nada reaparece")
    p.add_argument("--timeout", type=float, default=60, help="Tiempo máximo de cada petición HTTP")
    p.add_argument("--json", type=Path, help="Escribe aquí el informe en JSON")
    ns = p.parse_args(argv)
    ns.app_url, ns.fake_url = ns.app_url.rstrip("/"), ns.fake_url.rstrip("/")
    ns.proxy_url = ns.proxy_url.rstrip("/") if ns.proxy_url else None
    return ns


# --- HTTP ----------------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


# Sin proxies del entorno (la pila es local) ni redirecciones.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: Any = None,
    data: bytes | None = None,
    timeout: float = 60,
) -> Http:
    body = data if data is not None else (None if payload is None else encode(payload))
    req = urllib.request.Request(url, data=body, method=method)  # noqa: S310 (URL de la pila de desarrollo)
    req.add_header("User-Agent", USER_AGENT)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return Http(resp.status, list(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as exc:
        return Http(exc.code, list(exc.headers.items()) if exc.headers else [], exc.read() or b"")
