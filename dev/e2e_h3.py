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
import contextlib
import json
import os
import re
import secrets
import string
import sys
import time
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
    p.add_argument("--purge-wait", type=float, default=1,
                   help="Segundos entre el borrado y el turno siguiente (purga por Pub/Sub)")  # fmt: skip
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


def first_difference(expected: Any, received: Any, path: str = "$") -> str | None:
    """Primera diferencia entre dos valores JSON (`None` si son idénticos, sin confundir tipos)."""
    if type(expected) is not type(received):
        return f"{path}: {canonical(expected)[:80]} ≠ {canonical(received)[:80]}"
    if isinstance(expected, dict):
        for key in sorted(expected.keys() | received.keys()):
            if key not in received:
                return f"{path}.{key}: falta en lo recibido"
            if key not in expected:
                return f"{path}.{key}: sobra en lo recibido ({canonical(received[key])[:80]})"
            diff = first_difference(expected[key], received[key], f"{path}.{key}")
            if diff:
                return diff
        return None
    if isinstance(expected, list):
        if len(expected) != len(received):
            return f"{path}: {len(expected)} elementos ≠ {len(received)}"
        for i, (a, b) in enumerate(zip(expected, received, strict=True)):
            diff = first_difference(a, b, f"{path}[{i}]")
            if diff:
                return diff
        return None
    return None if expected == received else f"{path}: {canonical(expected)[:80]} ≠ {canonical(received)[:80]}"


def verification_problems(verification: Any) -> list[str]:
    """Lo que impide dar por buena la verificación de un turno con los tres valores (contrato §5.2)."""
    if not isinstance(verification, dict):
        return [f"el turno no tiene verificación ({verification!r})"]
    problems = []
    if verification.get("status") != "CLEAN":
        problems.append(f"verification.status = {verification.get('status')} (se esperaba CLEAN)")
    for name in ("request_replacements_by_type", "response_replacements_by_type"):
        counts = verification.get(name) or {}
        missing = [kind for kind in VALUES if not (isinstance(counts, dict) and counts.get(kind))]
        if missing:
            problems.append(f"{name} sin {', '.join(missing)}: {counts}")
    for name in ("unresolved_response_tokens", "orphan_request_tokens"):
        if verification.get(name):
            problems.append(f"{name} = {verification.get(name)}")
    if verification.get("residual_matches"):
        problems.append(f"residual_matches = {verification.get('residual_matches')}")
    return problems


def snippet(body: bytes, limit: int = 300) -> str:
    return repr(body[:limit].decode(errors="replace"))


# --- la prueba ------------------------------------------------------------------------------------


@dataclass
class Case:
    group: str  # texto · herramienta · bypass · borrado
    api: str
    stream: bool
    tag: str
    model: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{API_NAMES[self.api]} · {mode_name(self.stream)} · {self.group}"


def mode_name(stream: bool) -> str:
    return "streaming" if stream else "sin streaming"


def webapp_login(app_url: str, email: str, password: str, totp_secret: str, timeout: float) -> str:
    """Cookie `baas_session`: contraseña y, si la webapp lo pide, TOTP (como en `dev/e2e_h2.py`)."""
    hdrs = {"X-Requested-With": "baas-webapp"}
    res = http("POST", f"{app_url}/api/v1/auth/login", headers=hdrs,
               payload={"email": email, "password": password}, timeout=timeout)  # fmt: skip
    check(res.status == 200, f"login de {email} → HTTP {res.status}: {snippet(res.body, 200)}")
    cookies = cookies_from(res.headers)
    if "baas_session" in cookies:  # BAAS_DEV_SKIP_TOTP=1
        return cookies["baas_session"]
    check("baas_mfa" in cookies, f"login sin cookie de MFA ni de sesión: {snippet(res.body, 200)}")
    for attempt in range(2):
        r = http("POST", f"{app_url}/api/v1/auth/totp", headers={**hdrs, "Cookie": f"baas_mfa={cookies['baas_mfa']}"},
                 payload={"code": totp(totp_secret)}, timeout=timeout)  # fmt: skip
        session = cookies_from(r.headers).get("baas_session")
        if r.status == 200 and session:
            return session
        if attempt == 0:  # el código ya se usó en esta ventana: se espera a la siguiente
            time.sleep(31 - time.time() % 30)
    raise StepError(f"TOTP rechazado: HTTP {r.status} {snippet(r.body, 200)}")


class E2E:
    def __init__(self, args: argparse.Namespace, nonce: str | None = None) -> None:
        self.args = args
        self.nonce = nonce or new_nonce()
        self.key = dev_api_key(args.email)
        self.bypass_key = dev_api_key(args.bypass_email)
        self.binding: dict[str, Any] = {}
        self.bypass_binding: dict[str, Any] = {}
        self.session: str | None = None
        self.sent: list[Case] = []  # casos de AI-IP que el proxy respondió con 200
        self.deleted: dict[str, Case] = {}  # conversación borrada → su primer turno
        self.delete_cases: dict[str, tuple[Case, str]] = {}  # API → (primer turno, conversación borrada)

    # utilidades
    def case(self, group: str, api: str, stream: bool) -> Case:
        mode = "stream" if stream else "json"
        return Case(
            group, api, stream, case_tag(self.nonce, group, api, mode), case_model(self.nonce, group, api, mode)
        )

    def send(self, binding: dict[str, Any], key: str, api: str, payload: dict[str, Any]) -> tuple[bytes, Http]:
        headers = {"X-Baas-Api-Key": key}
        if api == "messages":
            headers |= {"x-api-key": FAKE_ANTHROPIC_KEY, "anthropic-version": "2023-06-01"}
        else:
            headers["Authorization"] = f"Bearer {FAKE_OPENAI_KEY}"
        if payload.get("stream"):
            headers["Accept"] = "text/event-stream"
        raw = encode(payload)
        return raw, http("POST", api_url(binding, api), headers=headers, data=raw, timeout=self.args.timeout)

    def debug_entries(self) -> list[dict[str, Any]]:
        res = http("GET", f"{self.args.fake_url}/_debug/requests", timeout=self.args.timeout)
        check(res.status == 200, f"GET {self.args.fake_url}/_debug/requests → HTTP {res.status}")
        data = res.json()
        check(
            isinstance(data, dict) and isinstance(data.get("requests"), list), f"forma inesperada: {snippet(res.body)}"
        )
        return [e for e in data["requests"] if isinstance(e, dict)]

    def webapp(self, path: str) -> Http:
        return http("GET", f"{self.args.app_url}{path}", headers={"Cookie": f"baas_session={self.session}"},
                    timeout=self.args.timeout)  # fmt: skip

    def conversations(self) -> list[dict[str, Any]]:
        res = self.webapp(f"/api/v1/conversations?org={self.args.org}&q={self.nonce}&limit=200")
        check(res.status == 200, f"GET /api/v1/conversations → HTTP {res.status}: {snippet(res.body, 200)}")
        return [i for i in (res.json() or {}).get("items") or [] if isinstance(i, dict)]

    def wait_conversation(self, model: str, *, exclude: Iterable[str] = ()) -> dict[str, Any]:
        """La conversación de `model` con al menos un turno (y que no esté en `exclude`)."""
        skip = set(exclude)
        deadline = time.monotonic() + self.args.trace_timeout
        while True:
            for item in self.conversations():
                if item.get("model") == model and (item.get("turn_count") or 0) >= 1 and item.get("id") not in skip:
                    return item
            if time.monotonic() > deadline:
                raise StepError(f"la conversación ({model}) no aparece en GET /api/v1/conversations"
                                f" tras {self.args.trace_timeout:g} s")  # fmt: skip
            time.sleep(0.25)

    def provider_problems(self, case: Case) -> list[str]:
        """Lo que recibió el proveedor para `case`: solo tokens, ningún valor."""
        entries = tagged(self.debug_entries(), case.tag)
        if not entries:
            return [f"el proveedor no recibió la petición (ninguna en /_debug/requests con «{case.tag}»)"]
        problems = []
        for entry in entries:
            text, path = debug_text(entry), str(entry.get("path"))
            if not path.split("?")[0].endswith(UPSTREAM_PATHS[case.api]):
                problems.append(f"el proveedor la recibió en {path} (se esperaba {UPSTREAM_PATHS[case.api]})")
            leaked = [f"{kind} «{value}»" for kind, value in VALUES.items() if value in text]
            if leaked:
                problems.append(f"el proveedor recibió en claro: {', '.join(leaked)}")
            missing = [token for token in TOKENS if token not in text]
            if missing:
                problems.append(f"el proveedor no recibió {', '.join(missing)}")
        return problems

    # 0
    def webapp_up(self) -> str:
        res = http("GET", f"{self.args.app_url}/healthz", timeout=self.args.timeout)
        check(res.status == 200, f"GET {self.args.app_url}/healthz → HTTP {res.status}")
        return f"{self.args.app_url} responde"

    # 1
    def fake_debug(self) -> str:
        before = self.debug_entries()
        res = http("DELETE", f"{self.args.fake_url}/_debug/requests", timeout=self.args.timeout)
        check(200 <= res.status < 300, f"DELETE {self.args.fake_url}/_debug/requests → HTTP {res.status}")
        after = self.debug_entries()
        check(not after, f"tras vaciarlo, /_debug/requests aún tiene {len(after)} petición(es)")
        return f"{self.args.fake_url}/_debug/requests responde; vaciado ({len(before)} petición(es) previas)"

    # 2a y 2b
    def handshake(self, email: str, key: str, org: str, *, ai_ip: bool) -> dict[str, Any]:
        res = http("POST", f"{self.args.app_url}/api/v1/plugin/handshake", headers={"Authorization": f"Bearer {key}"},
                   timeout=self.args.timeout)  # fmt: skip
        check(res.status == 200, f"handshake de {email} → HTTP {res.status}: {snippet(res.body, 200)}")
        orgs = {o.get("slug"): o for o in (res.json() or {}).get("organizations") or [] if isinstance(o, dict)}
        check(org in orgs, f"{email} no tiene la organización «{org}» (¿seed-dev de H3?): {sorted(map(str, orgs))}")
        binding = orgs[org]
        check(binding.get("ai_ip_enabled") is ai_ip,
              f"ai_ip_enabled = {binding.get('ai_ip_enabled')!r} en «{org}» (se esperaba {ai_ip})")  # fmt: skip
        check(binding.get("proxy_anthropic_url") and binding.get("proxy_openai_url"), f"sin URLs del proxy: {binding}")
        return override_proxy(binding, self.args.proxy_url) if self.args.proxy_url else binding

    def handshake_privacy(self) -> str:
        a = self.args
        self.binding = self.handshake(a.email, self.key, a.org, ai_ip=True)
        return (
            f"«{a.org}» con AI-IP activo · {self.binding['proxy_anthropic_url']} · {self.binding['proxy_openai_url']}"
        )

    def handshake_bypass(self) -> str:
        a = self.args
        self.bypass_binding = self.handshake(a.bypass_email, self.bypass_key, a.bypass_org, ai_ip=False)
        b = self.bypass_binding
        return f"«{a.bypass_org}» con AI-IP apagado · {b['proxy_anthropic_url']} · {b['proxy_openai_url']}"

    # 3
    def login(self) -> str:
        a = self.args
        self.session = webapp_login(a.app_url, a.admin_email, a.password, a.totp_secret, a.timeout)
        return f"sesión de {a.admin_email} iniciada"

    # 4 y 5
    def aiip_case(self, api: str, stream: bool, tool: bool) -> str:
        case = self.case("herramienta" if tool else "texto", api, stream)
        text = f"{case.tag}. {SENSITIVE_TEXT}." + (f" Por favor, {TOOL_TRIGGER} {TOOL_NAME}." if tool else "")
        case.payload = request_body(api, case.model, text, stream=stream, tool=tool)
        _, res = self.send(self.binding, self.key, api, case.payload)
        check(res.status == 200, f"{API_NAMES[api]} → HTTP {res.status}: {snippet(res.body)}")
        self.sent.append(case)
        problems: list[str] = []
        ctype = next((v for k, v in res.headers if k.lower() == "content-type"), "")
        if stream and "text/event-stream" not in ctype:
            problems.append(f"Content-Type {ctype!r} en una respuesta en streaming")
        body = res.body.decode(errors="replace")
        if "[[" in body:
            at = body.index("[[")
            problems.append(f"el cuerpo de la respuesta contiene «[[»: {body[max(0, at - 20) : at + 30]!r}")
        reply = extract_reply(api, res.body, stream=stream)
        if tool:
            calls = [c for c in reply.tool_calls if c.name == TOOL_NAME]
            if not calls:
                problems.append(f"no hay llamada a la herramienta {TOOL_NAME}: {[c.name for c in reply.tool_calls]}")
            problems += [f"{c.where}: {p}" for c in calls for p in tool_problems(c.arguments)]
            problems += [f"{where}: queda «[[» en el texto" for where, t in reply.texts if "[[" in t]
            copies = [c.where for c in calls]
        else:
            if not reply.texts:
                problems.append("la respuesta no trae texto")
            problems += [f"{where}: {p}" for where, t in reply.texts for p in restoration_problems(t)]
            copies = [where for where, _ in reply.texts]
        problems += self.provider_problems(case)
        check(not problems, "\n".join(problems))
        what = "argumentos de la herramienta restaurados y JSON válido" if tool else "texto restaurado"
        return (
            f"{what} en {len(copies)} copia(s): {', '.join(copies)}\n"
            f"el proveedor recibió {', '.join(TOKENS)} y ninguno de los valores"
        )

    # 6
    def verification(self) -> str:
        check(self.sent, "ningún caso de AI-IP llegó a responder 200")
        ready = lambda item: item and (item.get("turn_count") or 0) >= 1 and item.get("verification_status")  # noqa: E731
        deadline = time.monotonic() + self.args.trace_timeout
        while True:
            items = {i.get("model"): i for i in self.conversations()}
            if all(ready(items.get(c.model)) for c in self.sent) or time.monotonic() > deadline:
                break
            time.sleep(0.25)
        problems: list[str] = []
        for case in self.sent:
            item = items.get(case.model)
            if item is None:
                problems.append(f"{case.label}: la conversación no aparece en GET /api/v1/conversations")
                continue
            if item.get("ai_ip_enabled") is not True:
                problems.append(f"{case.label}: ai_ip_enabled = {item.get('ai_ip_enabled')!r}")
            if item.get("verification_status") != "CLEAN":
                problems.append(f"{case.label}: verificación {item.get('verification_status')} (se esperaba CLEAN)")
            problems += [f"{case.label}: {p}" for p in verification_problems(self.last_turn(str(item.get("id"))))]
        check(not problems, "\n".join(problems))
        return f"{len(self.sent)} turno(s) CLEAN con reemplazos de DNI, EMAIL e IBAN en la ida y en la vuelta"

    def last_turn(self, conversation_id: str) -> Any:
        res = self.webapp(f"/api/v1/conversations/{conversation_id}")
        check(res.status == 200, f"GET /api/v1/conversations/{conversation_id} → HTTP {res.status}")
        turns = [t for t in (res.json() or {}).get("turns") or [] if isinstance(t, dict)]
        check(turns, f"la conversación {conversation_id} no tiene turnos")
        index = max(int(t.get("turn_index") or 0) for t in turns)
        res = self.webapp(f"/api/v1/conversations/{conversation_id}/turns/{index}")
        check(res.status == 200, f"GET …/{conversation_id}/turns/{index} → HTTP {res.status}")
        return (res.json() or {}).get("verification")

    # 7
    def bypass_case(self, api: str, stream: bool) -> str:
        case = self.case("bypass", api, stream)
        case.payload = request_body(api, case.model, f"{case.tag}. {SENSITIVE_TEXT}.", stream=stream)
        raw, res = self.send(self.bypass_binding, self.bypass_key, api, case.payload)
        check(res.status == 200, f"{API_NAMES[api]} → HTTP {res.status}: {snippet(res.body)}")
        entries = tagged(self.debug_entries(), case.tag)
        check(entries, f"el proveedor no recibió la petición (ninguna en /_debug/requests con «{case.tag}»)")
        problems, exact = [], True
        for entry in entries:
            same, byte_same = same_payload(raw, entry.get("body"))
            exact = exact and byte_same
            if not same:
                received = entry.get("body")
                with contextlib.suppress(ValueError):
                    received = json.loads(received) if isinstance(received, str) else received
                diff = first_difference(json.loads(raw), received)
                problems.append(f"lo que recibió el proveedor no es idéntico a lo enviado: {diff}")
        reply = extract_reply(api, res.body, stream=stream)
        if not any(all(v in t for v in VALUES.values()) for _, t in reply.texts):
            problems.append("la respuesta no trae el eco con los valores")
        check(not problems, "\n".join(problems))
        how = "byte a byte" if exact else "como JSON (el proveedor no guarda los bytes exactos)"
        return f"el proveedor recibió exactamente lo que envió el cliente ({how})"

    # 8
    def deletion(self, api: str) -> str:
        case = self.case("borrado", api, False)
        first = f"{case.tag}. {SENSITIVE_TEXT}."
        case.payload = request_body(api, case.model, first)
        _, res = self.send(self.binding, self.key, api, case.payload)
        check(res.status == 200, f"primer turno → HTTP {res.status}: {snippet(res.body)}")
        reply = extract_reply(api, res.body, stream=False)
        answer = reply.texts[0][1] if reply.texts else ""
        check(answer, f"el primer turno no trae texto: {snippet(res.body)}")
        conversation_id = str(self.wait_conversation(case.model)["id"])

        deleted = http("POST", f"{self.args.app_url}/api/v1/conversations/{conversation_id}/delete",
                       headers={"Cookie": f"baas_session={self.session}", "X-Requested-With": "baas-webapp"},
                       payload={"reason": "Prueba de punta a punta de AI-IP (H3): borrado"},
                       timeout=self.args.timeout)  # fmt: skip
        check(
            deleted.status == 200, f"POST …/{conversation_id}/delete → HTTP {deleted.status}: {snippet(deleted.body)}"
        )
        propagated = (deleted.json() or {}).get("propagated")
        check(isinstance(propagated, bool), f"la respuesta del borrado no trae «propagated»: {snippet(deleted.body)}")
        gone = self.webapp(f"/api/v1/conversations/{conversation_id}")
        check(gone.status == 404, f"tras borrarla, GET /api/v1/conversations/{conversation_id} → HTTP {gone.status}"
                                  " (se esperaba 404)")  # fmt: skip
        self.deleted[conversation_id] = case
        self.delete_cases[api] = (case, conversation_id)
        time.sleep(self.args.purge_wait)  # conversation.purge llega al proxy por Pub/Sub, sin esperar respuesta

        tag2 = case_tag(self.nonce, "continua", api, "json")
        follow = f"{tag2}. Sigue con la conversación."
        if api == "responses":
            check(reply.response_id, f"la respuesta de Responses no trae «id»: {snippet(res.body)}")
            payload = request_body(api, case.model, follow, previous_response_id=reply.response_id)
            history = f"previous_response_id {reply.response_id}"
        else:
            past = [{"role": "user", "content": first}, {"role": "assistant", "content": answer}]
            payload = request_body(api, case.model, follow, history=past)
            history = "historial con la respuesta anterior"
        _, blocked = self.send(self.binding, self.key, api, payload)
        etype, code, message = provider_error(blocked.body)
        problems = []
        if blocked.status != 403:
            hint = f" (si el proxy aún no había aplicado la purga, sube --purge-wait de {self.args.purge_wait:g} s)"
            problems.append(
                f"el turno con historial → HTTP {blocked.status} (se esperaba 403): {snippet(blocked.body, 200)}"
                + (hint if blocked.status == 200 else "")
            )
        if etype != "permission_error":
            problems.append(f"error.type = {etype!r} (se esperaba permission_error)")
        if api == "messages" and "eliminada" not in message:
            problems.append(f"el mensaje de error no dice que la conversación fue eliminada: {message!r}")
        if api != "messages" and code != "baas_conversation_deleted":
            problems.append(f"error.code = {code!r} (se esperaba baas_conversation_deleted)")
        if tagged(self.debug_entries(), tag2):
            problems.append("el proveedor recibió el turno de la conversación borrada")
        check(not problems, "\n".join(problems))
        shown = f"{etype} · {code}" if code else f"{etype} «{message}»"
        return (
            f"conversación {conversation_id} borrada (propagated: {str(propagated).lower()}) y 404 en la webapp\n"
            f"turno siguiente ({history}) → 403 {shown}; no llegó al proveedor"
        )

    def nothing_reappears(self) -> str:
        time.sleep(self.args.settle)
        listed = {str(i.get("id")) for i in self.conversations()}
        problems = []
        for conversation_id in self.deleted:
            res = self.webapp(f"/api/v1/conversations/{conversation_id}")
            if res.status != 404 or conversation_id in listed:
                problems.append(f"la conversación borrada {conversation_id} reaparece en la webapp (HTTP {res.status})")
        check(not problems, "\n".join(problems))
        return f"{len(self.deleted)} conversación(es) borrada(s) siguen sin aparecer {self.args.settle:g} s después"

    # 9
    def fresh_after_delete(self, api: str) -> str:
        case, deleted_id = self.delete_cases[api]
        _, res = self.send(self.binding, self.key, api, case.payload)
        check(res.status == 200, f"el primer mensaje sin historial → HTTP {res.status}"
                                 f" (una conversación nueva no se bloquea): {snippet(res.body, 200)}")  # fmt: skip
        reply = extract_reply(api, res.body, stream=False)
        problems = [f"{where}: {p}" for where, t in reply.texts for p in restoration_problems(t)]
        check(reply.texts and not problems, "\n".join(problems) or "la respuesta no trae texto")
        item = self.wait_conversation(case.model, exclude=[deleted_id])
        return f"200 con los valores restaurados; conversación nueva {item['id']} (la borrada era {deleted_id})"

    # 10
    def no_values_in_clear(self) -> str:
        entries = self.debug_entries()
        marker = f"caso-{self.nonce}-bypass-"
        checked = [e for e in entries if marker not in debug_text(e)]
        leaks = []
        for entry in checked:
            found = [f"{kind} «{value}»" for kind, value in VALUES.items() if value in debug_text(entry)]
            if found:
                leaks.append(f"{entry.get('path')}: {', '.join(found)} en claro")
        check(not leaks, "\n".join(leaks))
        apart = len(entries) - len(checked)
        return f"{len(checked)} petición(es) revisada(s) sin valores en claro ({apart} de bypass aparte)"

    def run(self, runner: Runner) -> None:
        a = self.args
        groups = set(a.only)
        privacy = bool(groups & {"aiip", "borrado"})
        s0, s1 = "0. webapp accesible", "1. proveedor falso: /_debug/requests"
        s2a = f"2a. handshake de {a.email} ({a.org}, AI-IP activo)"
        s2b = f"2b. handshake de {a.bypass_email} ({a.bypass_org}, AI-IP apagado)"
        s3 = f"3. sesión en la webapp como {a.admin_email}"
        runner.step(s0, self.webapp_up)
        runner.step(s1, self.fake_debug)
        if privacy:
            runner.step(s2a, self.handshake_privacy, needs=[s0])
        if "bypass" in groups:
            runner.step(s2b, self.handshake_bypass, needs=[s0])
        if privacy:
            runner.step(s3, self.login, needs=[s0])
        cases = [(api, stream) for api in APIS for stream in (False, True)]
        if "aiip" in groups:
            for number, tool in ((4, False), (5, True)):
                kind = "AI-IP con herramienta" if tool else "AI-IP texto"
                for i, (api, stream) in enumerate(cases, 1):
                    if api in a.apis:
                        name = f"{number}.{i} {kind} · {API_NAMES[api]} · {mode_name(stream)}"
                        run = lambda api=api, stream=stream, tool=tool: self.aiip_case(api, stream, tool)  # noqa: E731
                        runner.step(name, run, needs=[s1, s2a])
            runner.step("6. verificación CLEAN en la webapp", self.verification, needs=[s2a, s3])
        if "bypass" in groups:
            for i, (api, stream) in enumerate(cases, 1):
                if api in a.apis:
                    run = lambda api=api, stream=stream: self.bypass_case(api, stream)  # noqa: E731
                    runner.step(f"7.{i} bypass · {API_NAMES[api]} · {mode_name(stream)}", run, needs=[s1, s2b])
        if "borrado" in groups:
            names = {api: f"8.{i} borrado · {API_NAMES[api]}" for i, api in enumerate(APIS, 1) if api in a.apis}
            for api, name in names.items():
                runner.step(name, lambda api=api: self.deletion(api), needs=[s1, s2a, s3])
            needs = [s3] if self.deleted else list(names.values())
            runner.step("8.4 nada reaparece en la webapp tras el borrado", self.nothing_reappears, needs=needs)
            api = next(iter(self.delete_cases), a.apis[0])
            runner.step(f"9. conversación nueva tras el borrado ({API_NAMES[api]})",
                        lambda: self.fresh_after_delete(api), needs=[names[api]])  # fmt: skip
        if privacy:
            runner.step("10. ningún valor en claro en /_debug/requests", self.no_values_in_clear, needs=[s1, s2a])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runner = Runner()
    e2e = E2E(args)
    print(f"Prueba de punta a punta H3 (AI-IP) · webapp {args.app_url} · proveedor falso {args.fake_url}"
          f" · organización {args.org} · ejecución {e2e.nonce}")  # fmt: skip
    e2e.run(runner)
    report = {**runner.report(), "app_url": args.app_url, "fake_url": args.fake_url, "nonce": e2e.nonce,
              "only": args.only, "apis": args.apis}  # fmt: skip
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    failed = [r.name for r in runner.results if r.status != "OK"]
    print("RESULTADO:", "todo OK" if not failed else f"{len(failed)} paso(s) sin éxito: {', '.join(failed)}")
    return runner.exit_code()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
    sys.exit(130)
