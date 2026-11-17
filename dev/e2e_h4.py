#!/usr/bin/env python3
"""Prueba de punta a punta del hito H4 contra una pila de BaaS-AI-IP con el proveedor falso con eco.

Por HTTP directo (sin Claude Code), con las claves deterministas del contrato §8 de `observador@observacion.test`,
`admin@baas.test` (PLATFORM_ADMIN), `orgadmin@demo.test` y `analista@demo.test`:

  0-4.  La webapp responde; `/_debug/requests` del proveedor falso responde y se vacía; *handshake* de
        `observador@observacion.test` (AI-IP activo, anonimizador apagado); sesiones; `observacion` con el
        anonimizador apagado.
  5-7.  `observacion` (anonimizador apagado), en Messages, Chat Completions y Responses, con y sin *streaming*: un DNI,
        un email y un IBAN llegan **en claro** al proveedor (el cuerpo sale tal como entró), el cliente recibe lo mismo,
        el turno sale `WARNING` con las coincidencias de las tres reglas y la webapp dice que el anonimizador estaba
        apagado. El PLATFORM_ADMIN no obtiene el contenido por la API (lista, detalle, turnos y revelado) ni por el
        WebSocket, tampoco con AI-IP apagado (`demo`).
  8.    El interruptor, en los dos sentidos: permisos y validaciones; una conversación que empezó con el anonimizador
        apagado sigue en claro aunque se encienda, una nueva sale tokenizada; la tokenizada sigue tokenizada (y
        restaurada) aunque se apague, una nueva sale en claro. El interruptor vuelve a su valor inicial.
  9.    Eliminar una organización: crear, bloquear y eliminar con confirmación del slug; la webapp, el proxy y el MCP
        dejan de servirla, el slug no se reutiliza, la auditoría lo recoge, el vault queda vacío, Redis sin sus
        claves y el directorio del MCP en cuarentena.

Uso (pila de `deploy` con el proveedor falso: `scripts/dev-up.sh --fake-upstream`):

    uv run python dev/e2e_h4.py --app-url http://app.localhost:8080 --fake-url http://127.0.0.1:18090

Los pasos 9.6 (vault, Redis y cuarentena) miran dentro de los contenedores con `podman`/`docker exec`
(`--inspect none` los omite). Sale con 0 si todo pasa, 1 si algún paso falla y 2 si los argumentos no valen. No se
distribuye con el plugin. `dev/fake_h4_stack.py` simula la pila para probar este script sin ella.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_h3 as h3
from e2e_h2 import DEV_PASSWORD, DEV_TOTP_SECRET, Http, Runner, StepError, check, dev_api_key

from baas_plugin.errors import McpToolError
from baas_plugin.mcp_client import McpClient

DNI, EMAIL, IBAN, VALUES = h3.DNI, h3.EMAIL, h3.IBAN, h3.VALUES
SENSITIVE_TEXT, TOKENS = h3.SENSITIVE_TEXT, h3.TOKENS
APIS, API_NAMES, GROUPS = h3.APIS, h3.API_NAMES, ("observacion", "interruptor", "eliminar")
META_ORG = "baas-ai-ip"
FORBIDDEN_TURN_KEYS = ("request_sent", "response_received", "request_original", "response_returned")
VALUE_KEYS = ("original_value", "value")
REASON = "Prueba de punta a punta de H4"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --- utilidades puras ------------------------------------------------------------------------------


def anonymizer_flag(item: Any) -> bool | None:
    """El anonimizador con el que empezó la conversación (o el de la organización) que expone la webapp."""
    if isinstance(item, dict):
        for key in ("anonymizer_enabled_at_start", "anonymizer_enabled"):
            if isinstance(item.get(key), bool):
                return item[key]
    return None


def segments_text(value: Any) -> str:
    """Texto de un campo de la vista de turno: segmentos `{"text"}` y `{"token"}` o una cadena."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(str(s.get("text") or s.get("token") or "") for s in value if isinstance(s, dict))
    return ""


def needles_for(nonce: str) -> list[str]:
    """Lo que no puede aparecer en lo que ve un PLATFORM_ADMIN: los valores, el texto del prompt y el eco."""
    return [DNI, EMAIL, IBAN, f"caso-{nonce}", "Eco:"]


def content_leaks(raw: str | bytes, needles: Iterable[str]) -> list[str]:
    """Qué `needles` aparecen en una respuesta, en bruto o en cualquiera de sus cadenas decodificadas."""
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    try:
        decoded = "\n".join(h3.strings_in(json.loads(text)))
    except ValueError:
        decoded = ""
    haystack = f"{text}\n{decoded}"
    return [f"«{needle}»" for needle in needles if needle in haystack]


def _keys_in(value: Any) -> set[str]:
    out: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            out |= {str(k) for k in item}
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def admin_turn_problems(turn: Any, *, metrics: bool = True) -> list[str]:
    """Un turno de la vista de verificación sin contenido: `content_available` falso, ningún campo de texto ni valor."""
    if not isinstance(turn, dict):
        return [f"el turno no es un objeto JSON: {turn!r}"]
    problems = []
    if turn.get("content_available") is not False:
        problems.append(f"content_available = {turn.get('content_available')!r} (se esperaba false)")
    present = [key for key in FORBIDDEN_TURN_KEYS if key in turn]
    if present:
        problems.append(f"el turno trae {', '.join(present)}")
    values = sorted(_keys_in(turn.get("replacements")) & set(VALUE_KEYS))
    if values:
        problems.append(f"los reemplazos traen valores ({', '.join(values)})")
    if metrics and not isinstance(turn.get("verification"), dict):
        problems.append("el turno no conserva las métricas de verificación")
    return problems


def observation_problems(verification: Any) -> list[str]:
    """Lo que impide dar por buena la verificación de un turno en modo observación (contrato §5.2)."""
    if not isinstance(verification, dict):
        return [f"el turno no tiene verificación ({verification!r})"]
    problems = []
    if verification.get("status") != "WARNING":
        problems.append(f"verification.status = {verification.get('status')} (se esperaba WARNING)")
    for name in ("request_replacements_by_type", "response_replacements_by_type"):
        if verification.get(name):
            problems.append(f"{name} = {verification.get(name)} (con el anonimizador apagado no se sustituye nada)")
    counts: dict[str, int] = {}
    for match in verification.get("residual_matches") or []:
        if isinstance(match, dict):
            kind = str(match.get("token_type"))
            counts[kind] = counts.get(kind, 0) + int(match.get("count") or 0)
    missing = [kind for kind in VALUES if counts.get(kind, 0) < 1]
    if missing:
        problems.append(f"residual_matches sin {', '.join(missing)}: {verification.get('residual_matches')}")
    for name in ("unresolved_response_tokens", "orphan_request_tokens"):
        if verification.get(name):
            problems.append(f"{name} = {verification.get(name)}")
    return problems


def case_model(nonce: str, group: str, api: str, mode: str) -> str:
    return f"e2e-h4-{nonce}-{group}-{api}-{mode}"


def new_slug(nonce: str) -> str:
    return f"e2e-{nonce}"


def org_data(body: Any) -> dict[str, Any]:
    """La organización de una respuesta: anidada en `organization` o con sus campos en la raíz."""
    if isinstance(body, dict):
        nested = body.get("organization")
        return nested if isinstance(nested, dict) else body
    return {}


# --- WebSocket mínimo (RFC 6455) con la biblioteca estándar --------------------------------------------


class WsClosedError(Exception):
    def __init__(self, code: int | None) -> None:
        super().__init__(f"el servidor cerró el WebSocket (código {code})")
        self.code = code


def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()  # noqa: S324 (RFC 6455)


def ws_frame(opcode: int, payload: bytes = b"", *, mask: bool = True, fin: bool = True) -> bytes:
    """Una trama; las del cliente van enmascaradas."""
    head = bytes([(0x80 if fin else 0) | opcode])
    length = len(payload)
    flag = 0x80 if mask else 0
    if length < 126:
        head += bytes([flag | length])
    elif length < 1 << 16:
        head += bytes([flag | 126]) + struct.pack(">H", length)
    else:
        head += bytes([flag | 127]) + struct.pack(">Q", length)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class WsClient:
    """Cliente de solo lectura de un WebSocket de texto (con cookie y `Origin`), sin dependencias."""

    def __init__(self, url: str, *, cookie: str, origin: str, timeout: float = 10) -> None:
        parts = urlsplit(url)
        host, port = parts.hostname or "localhost", parts.port or (443 if parts.scheme == "wss" else 80)
        if parts.scheme == "wss":
            raise StepError("wss no está soportado (la pila de desarrollo es HTTP)")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buffer = b""
        self.partial = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n"
            f"Cookie: baas_session={cookie}\r\nUser-Agent: {h3.USER_AGENT}\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        head = self._read_until(b"\r\n\r\n", timeout)
        lines = head.decode(errors="replace").split("\r\n")
        if " 101 " not in lines[0] + " ":
            self.close()
            raise StepError(f"WebSocket {url}: {lines[0]!r} (se esperaba 101)")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:]) if k}
        if headers.get("sec-websocket-accept") != ws_accept(key):
            self.close()
            raise StepError("WebSocket: Sec-WebSocket-Accept incorrecto")

    def _fill(self, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([self.sock], [], [], remaining)[0]:
            return False
        chunk = self.sock.recv(65536)
        if not chunk:
            raise WsClosedError(None)
        self.buffer += chunk
        return True

    def _read_until(self, marker: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while marker not in self.buffer:
            if not self._fill(deadline):
                raise StepError("el servidor no completó el handshake del WebSocket")
        head, _, self.buffer = self.buffer.partition(marker)
        return head

    def _need(self, count: int, deadline: float) -> bool:
        while len(self.buffer) < count:
            if not self._fill(deadline):
                return False
        return True

    def recv(self, timeout: float = 1.0) -> str | None:
        """Siguiente mensaje de texto o `None` si pasa `timeout` s. Responde `ping`; si cierra, `WsClosedError`."""
        deadline = time.monotonic() + timeout
        while True:
            if not self._need(2, deadline):
                return None
            first, second = self.buffer[0], self.buffer[1]
            length, offset = second & 0x7F, 2
            if length == 126:
                if not self._need(4, deadline):
                    return None
                length, offset = struct.unpack(">H", self.buffer[2:4])[0], 4
            elif length == 127:
                if not self._need(10, deadline):
                    return None
                length, offset = struct.unpack(">Q", self.buffer[2:10])[0], 10
            masked = bool(second & 0x80)
            if not self._need(offset + (4 if masked else 0) + length, deadline):
                return None
            key = self.buffer[offset : offset + 4] if masked else b""
            start = offset + (4 if masked else 0)
            payload = self.buffer[start : start + length]
            self.buffer = self.buffer[start + length :]
            if masked:
                payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
            opcode, fin = first & 0x0F, bool(first & 0x80)
            if opcode == 0x8:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
                raise WsClosedError(code)
            if opcode == 0x9:
                self.sock.sendall(ws_frame(0xA, payload))
                continue
            if opcode in (0x1, 0x0):  # texto y continuación
                self.partial += payload
                if fin:
                    text, self.partial = self.partial, b""
                    return text.decode(errors="replace")
            # 0xA (pong) y binarios: se ignoran

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.sendall(ws_frame(0x8, struct.pack(">H", 1000)))
        with contextlib.suppress(OSError):
            self.sock.close()


def ws_url(app_url: str, path: str) -> str:
    return re.sub(r"^http", "ws", app_url.rstrip("/"), count=1) + path


# --- lo que hay dentro de los contenedores (vault, Redis y cuarentena del MCP) ------------------------------


class Inspector(Protocol):
    def vault_rows(self, slug: str) -> int: ...
    def redis_keys(self, slug: str) -> list[str]: ...
    def mcp_org_dirs(self) -> list[str]: ...
    def mcp_quarantine(self) -> list[str]: ...


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class ContainerInspector:
    """Mira dentro de los contenedores de la pila de `compose.dev.yml` con `podman exec` o `docker exec`."""

    def __init__(self, engine: str, project: str, mcp_data_dir: str = "/data") -> None:
        self.engine, self.project, self.mcp_data_dir = engine, project, mcp_data_dir.rstrip("/")

    def container(self, service: str) -> str | None:
        out = self._run(["ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}",
                         "--filter", f"label=com.docker.compose.service={service}"], check_rc=False)  # fmt: skip
        ids = out.split()
        return ids[0] if ids else None

    def _run(self, argv: Sequence[str], *, check_rc: bool = True) -> str:
        try:
            done = subprocess.run([self.engine, *argv], capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StepError(f"{self.engine} {' '.join(argv[:3])}: {type(exc).__name__}") from None
        if check_rc and done.returncode != 0:
            raise StepError(
                f"{self.engine} {' '.join(argv[:3])} salió con {done.returncode}: {done.stderr.strip()[:200]}"
            )
        return done.stdout

    def _exec(self, service: str, *argv: str, check_rc: bool = True) -> str:
        cid = self.container(service)
        if cid is None:
            raise StepError(f"no hay contenedor «{service}» en el proyecto «{self.project}» ({self.engine})")
        return self._run(["exec", cid, *argv], check_rc=check_rc)

    @staticmethod
    def _slug(slug: str) -> str:
        if not _SLUG.fullmatch(slug):
            raise StepError(f"slug no válido: {slug!r}")
        return slug

    def vault_rows(self, slug: str) -> int:
        s = self._slug(slug)
        tables = ("vault_entries", "vault_counters", "conversation_state")
        sql = "SELECT " + " + ".join(f"(SELECT count(*) FROM {t} WHERE org_slug = '{s}')" for t in tables)  # noqa: S608
        out = self._exec("postgres", "psql", "-U", "postgres", "-d", "baas_vault", "-Atc", sql).strip()
        if not out.isdigit():
            raise StepError(f"psql no devolvió un número: {out[:100]!r}")
        return int(out)

    def redis_keys(self, slug: str) -> list[str]:
        s = self._slug(slug)
        keys: list[str] = []
        for pattern in (f"baas:policy:org:{s}", f"baas:member:*:{s}"):
            out = self._exec(
                "redis", "sh", "-c", 'REDISCLI_AUTH="$BAAS_REDIS_PASSWORD" exec redis-cli "$@"', "sh",
                "--scan", "--pattern", pattern,
            )  # fmt: skip
            keys += [line.strip() for line in out.splitlines() if line.strip()]
        return keys

    def mcp_org_dirs(self) -> list[str]:
        out = self._exec("mcp-server", "ls", "-1", f"{self.mcp_data_dir}/orgs", check_rc=False)
        return [line.strip() for line in out.splitlines() if line.strip()]

    def mcp_quarantine(self) -> list[str]:
        out = self._exec("mcp-server", "ls", "-1", f"{self.mcp_data_dir}/.quarantine", check_rc=False)
        return [line.strip() for line in out.splitlines() if line.strip()]


class MissingInspector:
    """No se encontró el motor de contenedores: cada consulta falla diciendo cómo seguir."""

    def __init__(self, why: str) -> None:
        self.why = why

    def _fail(self) -> Any:
        raise StepError(f"{self.why}. Usa --inspect podman|docker (y --compose-project) o --inspect none para omitirlo")

    def vault_rows(self, slug: str) -> int:
        return self._fail()

    def redis_keys(self, slug: str) -> list[str]:
        return self._fail()

    def mcp_org_dirs(self) -> list[str]:
        return self._fail()

    def mcp_quarantine(self) -> list[str]:
        return self._fail()


def build_inspector(args: argparse.Namespace) -> Inspector | None:
    if args.inspect == "none":
        return None
    engines = ("podman", "docker") if args.inspect == "auto" else (args.inspect,)
    tried = []
    for engine in engines:
        if shutil.which(engine) is None:
            tried.append(f"{engine} no está instalado")
            continue
        inspector = ContainerInspector(engine, args.compose_project, args.mcp_data_dir)
        try:
            found = inspector.container("postgres")
        except StepError as exc:
            tried.append(str(exc))
            continue
        if found:
            return inspector
        tried.append(f"{engine} no ve el proyecto «{args.compose_project}»")
    return MissingInspector("; ".join(tried))


# --- argumentos ------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    http_port = os.environ.get("BAAS_DEV_HTTP_PORT") or "8080"
    fake_port = os.environ.get("BAAS_DEV_FAKE_UPSTREAM_PORT") or "18090"
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--app-url", default=os.environ.get("BAAS_E2E_APP_URL") or f"http://app.localhost:{http_port}",
                   help="URL pública de la webapp (las del proxy y del MCP salen del handshake)")  # fmt: skip
    p.add_argument("--fake-url", default=os.environ.get("BAAS_E2E_FAKE_URL") or f"http://127.0.0.1:{fake_port}",
                   help="URL del proveedor falso publicada en el host (para /_debug/requests)")  # fmt: skip
    p.add_argument("--proxy-url", default=os.environ.get("BAAS_E2E_PROXY_URL") or None,
                   help="Sustituye el origen de las URLs del proxy del handshake (conserva /org/<slug>/…)")  # fmt: skip
    p.add_argument("--org", default="observacion", help="Organización con AI-IP activo y anonimizador apagado")
    p.add_argument("--email", default="observador@observacion.test", help="Usuario (USER) de esa organización")
    p.add_argument("--admin-email", default="admin@baas.test", help="PLATFORM_ADMIN")
    p.add_argument("--orgadmin-email", default="orgadmin@demo.test",
                   help="ORG_ADMIN de otra organización: primer miembro de la que se elimina")  # fmt: skip
    p.add_argument("--bypass-org", default="demo", help="Organización con AI-IP apagado")
    p.add_argument("--bypass-email", default="analista@demo.test", help="Usuario de la organización sin AI-IP")
    p.add_argument("--password", default=DEV_PASSWORD)
    p.add_argument("--totp-secret", default=DEV_TOTP_SECRET)
    p.add_argument("--only", type=h3._csv(GROUPS), default=list(GROUPS),
                   help=f"Partes que se ejecutan, separadas por comas: {', '.join(GROUPS)} (todas)")  # fmt: skip
    p.add_argument("--apis", type=h3._csv(APIS), default=list(APIS),
                   help=f"APIs que se prueban, separadas por comas: {', '.join(APIS)} (todas)")  # fmt: skip
    p.add_argument("--inspect", choices=("auto", "podman", "docker", "none"), default="auto",
                   help="Motor para ver el vault, Redis y la cuarentena del MCP («none» omite esos pasos)")  # fmt: skip
    p.add_argument("--compose-project", default=os.environ.get("COMPOSE_PROJECT_NAME") or "baas-dev",
                   help="Proyecto de compose de la pila (por defecto, $COMPOSE_PROJECT_NAME o baas-dev)")  # fmt: skip
    p.add_argument("--mcp-data-dir", default="/data", help="BAAS_DATA_DIR del contenedor del MCP")
    p.add_argument("--trace-timeout", type=float, default=30, help="Segundos de espera de cada traza o propagación")
    p.add_argument("--propagation-wait", type=float, default=1.5,
                   help="Segundos entre cambiar el anonimizador y el turno siguiente (Pub/Sub)")  # fmt: skip
    p.add_argument("--timeout", type=float, default=60, help="Tiempo máximo de cada petición HTTP")
    p.add_argument("--json", type=Path, help="Escribe aquí el informe en JSON")
    ns = p.parse_args(argv)
    ns.app_url, ns.fake_url = ns.app_url.rstrip("/"), ns.fake_url.rstrip("/")
    ns.proxy_url = ns.proxy_url.rstrip("/") if ns.proxy_url else None
    return ns


# --- la prueba ------------------------------------------------------------------------------------


@dataclass
class Conv:
    """Una conversación de la prueba del interruptor (una por API)."""

    case: h3.Case
    first_text: str
    answer: str = ""
    response_id: str | None = None
    follow: list[h3.Case] = field(default_factory=list)


class E2EH4:
    def __init__(self, args: argparse.Namespace, inspector: Inspector | None = None, nonce: str | None = None) -> None:
        self.args = args
        self.nonce = nonce or h3.new_nonce()
        self.inspector = inspector
        emails = {args.email, args.admin_email, args.orgadmin_email, args.bypass_email}
        self.keys = {email: dev_api_key(email) for email in emails}
        self.sessions: dict[str, str] = {}
        self.binding: dict[str, Any] = {}
        self.bypass_binding: dict[str, Any] = {}
        self.observed: list[h3.Case] = []  # casos con el anonimizador apagado que respondieron 200
        self.conversations: dict[str, dict[str, Conv]] = {"A": {}, "B": {}, "C": {}}
        self.slug = new_slug(self.nonce)
        self.member_binding: dict[str, Any] = {}  # handshake de orgadmin_email en la organización que se elimina

    # --- utilidades ------------------------------------------------------------------------------
    def case(self, group: str, api: str, stream: bool = False) -> h3.Case:
        mode = "stream" if stream else "json"
        return h3.Case(group, api, stream, h3.case_tag(self.nonce, group, api, mode),
                       case_model(self.nonce, group, api, mode))  # fmt: skip

    def key_of(self, email: str) -> str:
        return self.keys[email]

    def send(self, binding: dict[str, Any], email: str, api: str, payload: dict[str, Any]) -> tuple[bytes, Http]:
        headers = {"X-Baas-Api-Key": self.key_of(email)}
        if api == "messages":
            headers |= {"x-api-key": h3.FAKE_ANTHROPIC_KEY, "anthropic-version": "2023-06-01"}
        else:
            headers["Authorization"] = f"Bearer {h3.FAKE_OPENAI_KEY}"
        if payload.get("stream"):
            headers["Accept"] = "text/event-stream"
        raw = h3.encode(payload)
        return raw, h3.http("POST", h3.api_url(binding, api), headers=headers, data=raw, timeout=self.args.timeout)

    def webapp(self, method: str, path: str, who: str, payload: Any = None) -> Http:
        headers = {"Cookie": f"baas_session={self.sessions[who]}"}
        if method != "GET":
            headers["X-Requested-With"] = "baas-webapp"
        return h3.http(
            method, f"{self.args.app_url}{path}", headers=headers, payload=payload, timeout=self.args.timeout
        )

    def debug_entries(self) -> list[dict[str, Any]]:
        res = h3.http("GET", f"{self.args.fake_url}/_debug/requests", timeout=self.args.timeout)
        check(res.status == 200, f"GET {self.args.fake_url}/_debug/requests → HTTP {res.status}")
        data = res.json()
        check(
            isinstance(data, dict) and isinstance(data.get("requests"), list),
            f"forma inesperada: {h3.snippet(res.body)}",
        )
        return [e for e in data["requests"] if isinstance(e, dict)]

    def poll(self, fn: Callable[[], list[str]], timeout: float | None = None) -> list[str]:
        """Repite `fn` (devuelve problemas) hasta que no haya ninguno o se acabe el tiempo; devuelve los últimos."""
        deadline = time.monotonic() + (self.args.trace_timeout if timeout is None else timeout)
        while True:
            problems = fn()
            if not problems or time.monotonic() > deadline:
                return problems
            time.sleep(0.25)

    def list_conversations(self, who: str, org: str | None = None) -> list[dict[str, Any]]:
        res = self.webapp("GET", f"/api/v1/conversations?org={org or self.args.org}&q={self.nonce}&limit=200", who)
        check(res.status == 200, f"GET /api/v1/conversations ({who}) → HTTP {res.status}: {h3.snippet(res.body, 200)}")
        return [i for i in (res.json() or {}).get("items") or [] if isinstance(i, dict)]

    def wait_conversation(self, who: str, model: str, org: str | None = None, turns: int = 1) -> dict[str, Any]:
        deadline = time.monotonic() + self.args.trace_timeout
        while True:
            for item in self.list_conversations(who, org):
                if item.get("model") == model and (item.get("turn_count") or 0) >= turns:
                    return item
            if time.monotonic() > deadline:
                raise StepError(
                    f"la conversación ({model}) no aparece en GET /api/v1/conversations"
                    f" tras {self.args.trace_timeout:g} s"
                )
            time.sleep(0.25)

    def turn(self, who: str, conversation_id: str, index: int) -> Http:
        return self.webapp("GET", f"/api/v1/conversations/{conversation_id}/turns/{index}", who)

    def turn_indexes(self, who: str, conversation_id: str) -> list[int]:
        res = self.webapp("GET", f"/api/v1/conversations/{conversation_id}", who)
        check(res.status == 200, f"GET /api/v1/conversations/{conversation_id} ({who}) → HTTP {res.status}")
        turns = [t for t in (res.json() or {}).get("turns") or [] if isinstance(t, dict)]
        check(turns, f"la conversación {conversation_id} no tiene turnos")
        return sorted(int(t.get("turn_index") or 0) for t in turns)

    def provider_entries(self, tag: str) -> list[dict[str, Any]]:
        return h3.tagged(self.debug_entries(), tag)

    def path_problems(self, entries: list[dict[str, Any]], api: str) -> list[str]:
        problems = []
        for entry in entries:
            path = str(entry.get("path"))
            if not path.split("?")[0].endswith(h3.UPSTREAM_PATHS[api]):
                problems.append(f"el proveedor la recibió en {path} (se esperaba {h3.UPSTREAM_PATHS[api]})")
        return problems

    def clear_problems(self, tag: str, api: str, raw: bytes | None = None) -> list[str]:
        """Lo que recibió el proveedor para `tag`: los tres valores en claro, ningún token (y, con `raw`, idéntico)."""
        entries = self.provider_entries(tag)
        if not entries:
            return [f"el proveedor no recibió la petición (ninguna en /_debug/requests con «{tag}»)"]
        problems = self.path_problems(entries, api)
        for entry in entries:
            text = h3.debug_text(entry)
            missing = [f"{kind} «{value}»" for kind, value in VALUES.items() if value not in text]
            if missing:
                problems.append(f"el proveedor no recibió en claro: {', '.join(missing)}")
            if "[[" in text:
                problems.append(
                    f"el proveedor recibió tokens ({sorted(set(h3.TOKEN_RE.findall(text)))})"
                    " con el anonimizador apagado"
                )
            if raw is not None:
                same, _ = h3.same_payload(raw, entry.get("body"))
                if not same:
                    received = entry.get("body")
                    with contextlib.suppress(ValueError):
                        received = json.loads(received) if isinstance(received, str) else received
                    problems.append(
                        "el cuerpo que recibió el proveedor no es idéntico al enviado: "
                        f"{h3.first_difference(json.loads(raw), received)}"
                    )
        return problems

    def tokenized_problems(self, tag: str, api: str) -> list[str]:
        """Lo que recibió el proveedor para `tag`: los tres tokens y ningún valor."""
        entries = self.provider_entries(tag)
        if not entries:
            return [f"el proveedor no recibió la petición (ninguna en /_debug/requests con «{tag}»)"]
        problems = self.path_problems(entries, api)
        for entry in entries:
            text = h3.debug_text(entry)
            leaked = [f"{kind} «{value}»" for kind, value in VALUES.items() if value in text]
            if leaked:
                problems.append(f"el proveedor recibió en claro con el anonimizador encendido: {', '.join(leaked)}")
            missing = [token for token in TOKENS if token not in text]
            if missing:
                problems.append(f"el proveedor no recibió {', '.join(missing)}")
        return problems

    def reply_texts(self, api: str, res: Http, stream: bool, text: str | None = None) -> list[str]:
        """Problemas de lo que recibe el cliente: los tres valores en cada copia, sin `[[` y, con `text`, lo enviado."""
        problems = []
        ctype = next((v for k, v in res.headers if k.lower() == "content-type"), "")
        if stream and "text/event-stream" not in ctype:
            problems.append(f"Content-Type {ctype!r} en una respuesta en streaming")
        body = res.body.decode(errors="replace")
        if "[[" in body:
            at = body.index("[[")
            problems.append(f"el cuerpo de la respuesta contiene «[[»: {body[max(0, at - 20) : at + 30]!r}")
        reply = h3.extract_reply(api, res.body, stream=stream)
        if not reply.texts:
            problems.append("la respuesta no trae texto")
        for where, copy in reply.texts:
            problems += [f"{where}: {p}" for p in h3.restoration_problems(copy)]
            if text is not None and text not in copy:
                problems.append(f"{where}: no devuelve lo que se envió («{text[:60]}…»)")
        return problems

    # --- 0 a 4 -----------------------------------------------------------------------------------
    def webapp_up(self) -> str:
        res = h3.http("GET", f"{self.args.app_url}/healthz", timeout=self.args.timeout)
        check(res.status == 200, f"GET {self.args.app_url}/healthz → HTTP {res.status}")
        return f"{self.args.app_url} responde"

    def fake_debug(self) -> str:
        before = self.debug_entries()
        res = h3.http("DELETE", f"{self.args.fake_url}/_debug/requests", timeout=self.args.timeout)
        check(200 <= res.status < 300, f"DELETE {self.args.fake_url}/_debug/requests → HTTP {res.status}")
        check(not self.debug_entries(), "tras vaciarlo, /_debug/requests aún tiene peticiones")
        return f"{self.args.fake_url}/_debug/requests responde; vaciado ({len(before)} petición(es) previas)"

    def handshake(self, email: str, org: str, *, ai_ip: bool) -> dict[str, Any]:
        res = h3.http("POST", f"{self.args.app_url}/api/v1/plugin/handshake",
                      headers={"Authorization": f"Bearer {self.key_of(email)}"}, payload={},
                      timeout=self.args.timeout)  # fmt: skip
        check(res.status == 200, f"handshake de {email} → HTTP {res.status}: {h3.snippet(res.body, 200)}")
        orgs = {o.get("slug"): o for o in (res.json() or {}).get("organizations") or [] if isinstance(o, dict)}
        check(org in orgs, f"{email} no tiene la organización «{org}» (¿seed-dev de H4?): {sorted(map(str, orgs))}")
        binding = orgs[org]
        check(binding.get("ai_ip_enabled") is ai_ip,
              f"ai_ip_enabled = {binding.get('ai_ip_enabled')!r} en «{org}» (se esperaba {ai_ip})")  # fmt: skip
        check(binding.get("proxy_anthropic_url") and binding.get("proxy_openai_url"), f"sin URLs del proxy: {binding}")
        return h3.override_proxy(binding, self.args.proxy_url) if self.args.proxy_url else binding

    def handshake_observation(self) -> str:
        self.binding = self.handshake(self.args.email, self.args.org, ai_ip=True)
        return f"«{self.args.org}» con AI-IP activo · {self.binding['proxy_anthropic_url']}"

    def handshake_bypass(self) -> str:
        self.bypass_binding = self.handshake(self.args.bypass_email, self.args.bypass_org, ai_ip=False)
        return f"«{self.args.bypass_org}» con AI-IP apagado · {self.bypass_binding['proxy_anthropic_url']}"

    def login(self, *emails: str) -> str:
        a = self.args
        for email in dict.fromkeys(emails):
            self.sessions[email] = h3.webapp_login(a.app_url, email, a.password, a.totp_secret, a.timeout)
        return "sesión de " + ", ".join(dict.fromkeys(emails))

    def org_state(self) -> dict[str, Any]:
        res = self.webapp("GET", f"/api/v1/orgs/{self.args.org}", self.args.admin_email)
        check(res.status == 200, f"GET /api/v1/orgs/{self.args.org} → HTTP {res.status}: {h3.snippet(res.body, 200)}")
        data = org_data(res.json())
        check(
            "anonymizer_enabled" in data, f"GET /api/v1/orgs/{self.args.org} no trae anonymizer_enabled: {sorted(data)}"
        )
        return data

    def toggle(self, enabled: bool, who: str | None = None, reason: str | None = REASON) -> Http:
        body: dict[str, Any] = {"enabled": enabled}
        if reason is not None:
            body["reason"] = reason
        return self.webapp("PUT", f"/api/v1/orgs/{self.args.org}/anonymizer", who or self.args.admin_email, body)

    def switch(self, enabled: bool) -> str:
        """Cambia el anonimizador como PLATFORM_ADMIN, comprueba la respuesta y espera a que llegue al proxy."""
        res = self.toggle(enabled)
        check(
            res.status == 200,
            f"PUT …/anonymizer {{enabled: {str(enabled).lower()}}} → HTTP {res.status}: {h3.snippet(res.body, 200)}",
        )
        data = res.json() or {}
        propagated = data.get("propagated")
        check(isinstance(propagated, bool), f"la respuesta no trae «propagated»: {h3.snippet(res.body, 200)}")
        flag = org_data(data).get("anonymizer_enabled")
        check(flag is enabled, f"la respuesta dice anonymizer_enabled = {flag!r} (se esperaba {enabled})")
        check(self.org_state().get("anonymizer_enabled") is enabled, "GET de la organización no refleja el cambio")
        time.sleep(self.args.propagation_wait)  # ai_ip.changed llega al proxy por Pub/Sub, sin esperar respuesta
        return f"anonimizador {'encendido' if enabled else 'apagado'} (propagated: {str(propagated).lower()})"

    def ensure_off(self) -> str:
        state = self.org_state()
        check(
            state.get("ai_ip_enabled") is True,
            f"«{self.args.org}» tiene AI-IP apagado: no se puede probar el modo observación",
        )
        if state.get("anonymizer_enabled") is False:
            return f"«{self.args.org}» ya tiene el anonimizador apagado"
        return f"estaba encendido (¿una ejecución anterior?): {self.switch(False)}"

    # --- 5 a 7: anonimizador apagado y PLATFORM_ADMIN -----------------------------------------------
    def observe_case(self, api: str, stream: bool) -> str:
        case = self.case("observacion", api, stream)
        text = f"{case.tag}. {SENSITIVE_TEXT}."
        case.payload = h3.request_body(api, case.model, text, stream=stream)
        raw, res = self.send(self.binding, self.args.email, api, case.payload)
        check(res.status == 200, f"{API_NAMES[api]} → HTTP {res.status}: {h3.snippet(res.body)}")
        self.observed.append(case)
        problems = self.reply_texts(api, res, stream, text) + self.clear_problems(case.tag, api, raw)
        check(not problems, "\n".join(problems))
        return (
            "el proveedor recibió lo mismo que envió el cliente, con los tres valores en claro y sin tokens; "
            "el cliente recibió el eco completo"
        )

    def causes(self) -> str:
        """Como dueño: cada caso es una conversación sin sustituciones, con `WARNING` y la causa (anonimizador ✘)."""
        check(self.observed, "ningún caso con el anonimizador apagado llegó a responder 200")
        who = self.args.email
        deadline = time.monotonic() + self.args.trace_timeout
        while True:
            items = {i.get("model"): i for i in self.list_conversations(who)}
            ready = all(
                items.get(c.model, {}).get("turn_count") and items[c.model].get("verification_status")
                for c in self.observed
            )
            if ready or time.monotonic() > deadline:
                break
            time.sleep(0.25)
        problems: list[str] = []
        for case in self.observed:
            item = items.get(case.model)
            if item is None:
                problems.append(f"{case.label}: la conversación no aparece en GET /api/v1/conversations")
                continue
            if item.get("ai_ip_enabled") is not True:
                problems.append(f"{case.label}: ai_ip_enabled = {item.get('ai_ip_enabled')!r}")
            flag = anonymizer_flag(item)
            if flag is not False:
                problems.append(f"{case.label}: la causa no consta: anonymizer_enabled = {flag!r} (se esperaba false)")
            if item.get("verification_status") != "WARNING":
                problems.append(f"{case.label}: verificación {item.get('verification_status')} (se esperaba WARNING)")
            turns = self.turn_indexes(who, str(item["id"]))
            res = self.turn(who, str(item["id"]), turns[-1])
            check(res.status == 200, f"GET …/turns/{turns[-1]} ({who}) → HTTP {res.status}")
            body = res.json() or {}
            problems += [f"{case.label}: {p}" for p in observation_problems(body.get("verification"))]
            sent = segments_text(body.get("request_sent"))
            missing = [kind for kind, value in VALUES.items() if value not in sent]
            if missing:  # control positivo: el dueño sí lee su propio contenido
                problems.append(
                    f"{case.label}: el dueño no lee el texto enviado en claro (faltan {', '.join(missing)})"
                )
        check(not problems, "\n".join(problems))
        return (
            f"{len(self.observed)} turno(s) WARNING sin reemplazos, con DNI, EMAIL e IBAN en residual_matches, "
            "y la causa (anonimizador ✘)"
        )

    def admin_view(self, case: h3.Case, org: str | None = None, *, metrics: bool = True) -> list[str]:
        """Lo que ve el PLATFORM_ADMIN de una conversación: lista, detalle, turnos y revelado, sin contenido."""
        who, needles = self.args.admin_email, needles_for(self.nonce)
        res = self.webapp("GET", f"/api/v1/conversations?org={org or self.args.org}&q={self.nonce}&limit=200", who)
        if res.status != 200:
            return [f"{case.label}: GET /api/v1/conversations → HTTP {res.status}"]
        problems = [
            f"{case.label}: la lista de conversaciones contiene {leak}" for leak in content_leaks(res.body, needles)
        ]
        item = next((i for i in (res.json() or {}).get("items") or [] if i.get("model") == case.model), None)
        if item is None:
            return [*problems, f"{case.label}: el PLATFORM_ADMIN no ve la conversación en la lista"]
        cid = str(item["id"])
        if item.get("originals") not in (None, "LOCKED"):
            problems.append(f"{case.label}: originals = {item.get('originals')!r} (se esperaba LOCKED)")
        detail = self.webapp("GET", f"/api/v1/conversations/{cid}", who)
        if detail.status != 200:
            return [*problems, f"{case.label}: GET /api/v1/conversations/{cid} → HTTP {detail.status}"]
        problems += [
            f"{case.label}: el detalle de la conversación contiene {leak}"
            for leak in content_leaks(detail.body, needles)
        ]
        for index in sorted(int(t.get("turn_index") or 0) for t in (detail.json() or {}).get("turns") or []):
            turn = self.turn(who, cid, index)
            if turn.status != 200:
                problems.append(f"{case.label}: GET …/turns/{index} → HTTP {turn.status}")
                continue
            problems += [f"{case.label}: turno {index}: {p}" for p in admin_turn_problems(turn.json(), metrics=metrics)]
            problems += [
                f"{case.label}: el turno {index} contiene {leak}" for leak in content_leaks(turn.body, needles)
            ]
            reveal = self.webapp("POST", f"/api/v1/conversations/{cid}/turns/{index}/reveal", who, {"reason": REASON})
            problems += [
                f"{case.label}: el revelado del turno {index} contiene {leak}"
                for leak in content_leaks(reveal.body, needles)
            ]
            if reveal.status == 200:
                problems.append(
                    f"{case.label}: el PLATFORM_ADMIN puede revelar el turno {index} "
                    "de una organización ajena (HTTP 200)"
                )
        return problems

    def admin_without_content(self) -> str:
        check(self.observed, "ningún caso con el anonimizador apagado llegó a responder 200")
        problems: list[str] = []
        for case in self.observed:
            problems += self.poll(lambda case=case: self.admin_view(case))
        check(not problems, "\n".join(problems))
        return (
            f"{len(self.observed)} conversación(es): lista, detalle, turnos y revelado sin contenido ni valores "
            "(content_available: false)"
        )

    def admin_without_ai_ip(self) -> str:
        """Con AI-IP apagado (`demo`) el texto enviado es el original: tampoco lo ve el PLATFORM_ADMIN."""
        case = self.case("bypass", "messages")
        case.payload = h3.request_body("messages", case.model, f"{case.tag}. {SENSITIVE_TEXT}.")
        _, res = self.send(self.bypass_binding, self.args.bypass_email, "messages", case.payload)
        check(res.status == 200, f"turno en «{self.args.bypass_org}» → HTTP {res.status}: {h3.snippet(res.body)}")
        listing = lambda: self.list_conversations(self.args.bypass_email, self.args.bypass_org)  # noqa: E731
        deadline = time.monotonic() + self.args.trace_timeout
        while not any(i.get("model") == case.model for i in listing()):
            check(time.monotonic() < deadline, f"la conversación ({case.model}) no aparece para su dueño")
            time.sleep(0.25)
        problems = self.poll(
            lambda: self.admin_view(case, self.args.bypass_org, metrics=False)
        )  # AI-IP apagado: sin verificación
        check(not problems, "\n".join(problems))
        return f"conversación de «{self.args.bypass_org}» (AI-IP apagado) sin contenido para el PLATFORM_ADMIN"

    def websocket(self) -> str:
        a = self.args
        url = ws_url(a.app_url, f"/api/v1/ws/traces?org={a.org}")
        sockets = {who: WsClient(url, cookie=self.sessions[who], origin=a.app_url, timeout=a.timeout)
                   for who in (a.admin_email, a.email)}  # fmt: skip
        try:
            for who, ws in sockets.items():
                hello = ws.recv(5)
                check(hello is not None and '"hello"' in hello, f"{who}: el WebSocket no envió «hello»: {hello!r}")
            case = self.case("vivo", "messages")
            case.payload = h3.request_body("messages", case.model, f"{case.tag}. {SENSITIVE_TEXT}.")
            _, res = self.send(self.binding, a.email, "messages", case.payload)
            check(res.status == 200, f"turno en vivo → HTTP {res.status}: {h3.snippet(res.body)}")
            frames: dict[str, list[str]] = {who: [] for who in sockets}
            done: set[str] = set()
            deadline = time.monotonic() + a.trace_timeout
            while len(done) < len(sockets) and time.monotonic() < deadline:
                for who, ws in sockets.items():
                    try:
                        frame = ws.recv(0.2)
                    except WsClosedError as exc:
                        raise StepError(f"{who}: {exc}") from None
                    if frame is not None:
                        frames[who].append(frame)
                        if '"turn.completed"' in frame and case.model in frame:
                            done.add(who)
            time.sleep(0.3)  # lo que aún viaje
            for who, ws in sockets.items():
                while (frame := ws.recv(0.1)) is not None:
                    frames[who].append(frame)
        finally:
            for ws in sockets.values():
                ws.close()
        admin = frames[a.admin_email]
        about = [f for f in admin if case.model in f]
        problems = (
            [f"el PLATFORM_ADMIN no recibió ninguna trama de la conversación ({len(admin)} trama(s) en total)"]
            if not about
            else []
        )
        for frame in admin:
            problems += [
                f"trama del PLATFORM_ADMIN con {leak}: {frame[:100]!r}"
                for leak in content_leaks(frame, needles_for(self.nonce))
            ]
            parsed = None
            with contextlib.suppress(ValueError):
                parsed = json.loads(frame).get("turn")
            if isinstance(parsed, dict):
                problems += [f"trama del PLATFORM_ADMIN: {p}" for p in admin_turn_problems(parsed, metrics=False)]
        check(not problems, "\n".join(problems))
        owner_saw = any(content_leaks(f, [DNI, EMAIL, IBAN]) for f in frames[a.email])
        control = (
            "el dueño sí recibió el contenido (control positivo)"
            if owner_saw
            else "el dueño no recibió contenido por el WebSocket (sin control positivo)"
        )
        return f"el PLATFORM_ADMIN recibió {len(about)} trama(s) de la conversación sin contenido; {control}"

    # --- 8: el interruptor --------------------------------------------------------------------------
    @staticmethod
    def expect(problems: list[str], label: str, res: Http, status: int, code: str | None = None) -> None:
        """Anota un problema si `res` no es la respuesta `status` (y el `error` `code`) que se esperaba."""
        got = res.json().get("error") if isinstance(res.json(), dict) else None
        if res.status != status or (code and got != code):
            problems.append(
                f"{label}: HTTP {res.status} {got or ''} (se esperaba {status}{' ' + code if code else ''})"
            )

    def switch_rules(self) -> str:
        a, problems = self.args, []
        expect = lambda *args: self.expect(problems, *args)  # noqa: E731
        state = self.org_state().get("anonymizer_enabled")
        expect("sin motivo", self.toggle(not state, reason=None), 422, "REASON_REQUIRED")
        expect("motivo de 2 caracteres", self.toggle(not state, reason="ab"), 422, "REASON_REQUIRED")
        expect(f"{a.email} (USER de la organización)", self.toggle(not state, who=a.email), 403)
        expect(
            f"{a.orgadmin_email} (ORG_ADMIN de otra organización)", self.toggle(not state, who=a.orgadmin_email), 404
        )
        off = self.webapp(
            "PUT", f"/api/v1/orgs/{a.bypass_org}/anonymizer", a.admin_email, {"enabled": True, "reason": REASON}
        )
        expect(f"«{a.bypass_org}» con AI-IP apagado", off, 409, "AI_IP_DISABLED")
        after = self.org_state().get("anonymizer_enabled")
        if after != state:
            problems.append(f"una petición rechazada cambió el anonimizador de {state} a {after}")
        check(not problems, "\n".join(problems))
        return (
            "sin motivo → 422 REASON_REQUIRED; USER → 403; ORG_ADMIN ajeno → 404; "
            "AI-IP apagado → 409 AI_IP_DISABLED; nada cambió"
        )

    def first_turn(self, kind: str, api: str) -> str:
        """Primer turno de la conversación `kind` (A, B o C) en `api`, sin streaming."""
        case = self.case(f"i{kind.lower()}", api)
        first = f"{case.tag}. {SENSITIVE_TEXT}."
        case.payload = h3.request_body(api, case.model, first)
        raw, res = self.send(self.binding, self.args.email, api, case.payload)
        check(
            res.status == 200, f"{API_NAMES[api]}: primer turno de {kind} → HTTP {res.status}: {h3.snippet(res.body)}"
        )
        problems = self.reply_texts(api, res, False, first)
        problems += self.tokenized_problems(case.tag, api) if kind == "B" else self.clear_problems(case.tag, api, raw)
        reply = h3.extract_reply(api, res.body, stream=False)
        conv = Conv(case, first, reply.texts[0][1] if reply.texts else "", reply.response_id)
        if api == "responses" and not conv.response_id:
            problems.append("la respuesta de Responses no trae «id»")
        self.conversations[kind][api] = conv
        return "\n".join(f"{API_NAMES[api]}: {p}" for p in problems)

    def follow_turn(self, kind: str, api: str) -> str:
        """Segundo turno de la conversación `kind` con historial: sigue como empezó."""
        conv = self.conversations[kind][api]
        case = self.case(f"i{kind.lower()}-sigue", api)
        follow = f"{case.tag}. Sigue. {SENSITIVE_TEXT}."
        if api == "responses":
            case.payload = h3.request_body(api, conv.case.model, follow, previous_response_id=conv.response_id)
        else:
            past = [{"role": "user", "content": conv.first_text}, {"role": "assistant", "content": conv.answer}]
            case.payload = h3.request_body(api, conv.case.model, follow, history=past)
        raw, res = self.send(self.binding, self.args.email, api, case.payload)
        check(
            res.status == 200, f"{API_NAMES[api]}: segundo turno de {kind} → HTTP {res.status}: {h3.snippet(res.body)}"
        )
        problems = self.reply_texts(api, res, False, follow)
        problems += self.tokenized_problems(case.tag, api) if kind == "B" else self.clear_problems(case.tag, api, raw)
        conv.follow.append(case)
        return "\n".join(f"{API_NAMES[api]}: {p}" for p in problems)

    def phase(self, steps: Iterable[tuple[str, str]], what: str) -> str:
        problems = []
        for kind, api in steps:
            run = self.first_turn if what == "first" else self.follow_turn
            problems.append(run(kind, api))
        check(not any(problems), "\n".join(p for p in problems if p))
        return ""

    def apis(self) -> list[str]:
        return [api for api in APIS if api in self.args.apis]

    def api_names(self) -> str:
        return ", ".join(API_NAMES[api] for api in self.apis())

    def initial(self, kind: str) -> str:
        self.phase([(kind, api) for api in self.apis()], "first")
        what = {
            "A": "con el anonimizador apagado: los tres valores en claro hasta el proveedor",
            "B": "con él encendido: solo tokens hasta el proveedor y valores restaurados para el cliente",
            "C": "con él apagado otra vez: los tres valores en claro",
        }[kind]
        return f"conversación {kind} ({self.api_names()}) {what}"

    def continues(self, kind: str) -> str:
        self.phase([(kind, api) for api in self.apis()], "follow")
        what = "sigue en claro" if kind != "B" else "sigue tokenizada y restaurada"
        return f"la conversación {kind} {what} aunque el interruptor haya cambiado ({self.api_names()})"

    def switch_state(self) -> str:
        """La webapp: A y C empezaron con el anonimizador apagado (`WARNING`), B con él encendido (`CLEAN`)."""
        who, problems = self.args.email, []
        expected = {"A": (False, "WARNING"), "B": (True, "CLEAN"), "C": (False, "WARNING")}
        for kind, (flag, status) in expected.items():
            for api, conv in self.conversations[kind].items():
                item = self.wait_conversation(who, conv.case.model, turns=1 + len(conv.follow))
                label = f"{kind} · {API_NAMES[api]}"
                if anonymizer_flag(item) is not flag:
                    problems.append(f"{label}: anonymizer_enabled = {anonymizer_flag(item)!r} (se esperaba {flag})")
                if item.get("verification_status") != status:
                    problems.append(f"{label}: verificación {item.get('verification_status')} (se esperaba {status})")
        check(not problems, "\n".join(problems))
        return "A y C: anonimizador ✘ y WARNING; B: anonimizador ✔ y CLEAN"

    def back_to_off(self) -> str:
        state = self.org_state().get("anonymizer_enabled")
        if state is False:
            return f"«{self.args.org}» tiene el anonimizador apagado, como al empezar"
        return self.switch(False)

    # --- 9: eliminar una organización ------------------------------------------------------------------
    def admin_post(self, path: str, payload: Any = None, who: str | None = None, method: str = "POST") -> Http:
        return self.webapp(method, path, who or self.args.admin_email, payload)

    def member_chat(self) -> Http:
        """Un turno de Chat Completions de `orgadmin_email` por la ruta de la organización que se elimina."""
        tag = h3.case_tag(self.nonce, "eliminar", "chat", "json")
        payload = h3.request_body("chat", f"e2e-h4-{self.nonce}-eliminar-chat", f"{tag}. Hola.")
        return self.send(self.member_binding, self.args.orgadmin_email, "chat", payload)[1]

    def create_org(self) -> str:
        a, slug = self.args, self.slug
        res = self.admin_post("/api/v1/orgs", {"slug": slug, "name": f"E2E eliminar {self.nonce}"})
        check(res.status == 201, f"POST /api/v1/orgs → HTTP {res.status}: {h3.snippet(res.body, 200)}")
        res = self.admin_post(
            f"/api/v1/orgs/{slug}/members", {"email": a.orgadmin_email, "governance_role": "ORG_ADMIN"}
        )
        check(res.status == 201, f"invitar a {a.orgadmin_email} → HTTP {res.status}: {h3.snippet(res.body, 200)}")
        res = self.admin_post(f"/api/v1/orgs/{slug}/ai-ip", {"enabled": True, "reason": REASON}, method="PUT")
        check(res.status == 200, f"PUT …/ai-ip → HTTP {res.status}: {h3.snippet(res.body, 200)}")
        self.member_binding = self.handshake(a.orgadmin_email, slug, ai_ip=True)
        # Datos en el vault (un turno con valores) y en el MCP (un documento).
        case = self.case("eliminar", "messages")
        case.payload = h3.request_body("messages", case.model, f"{case.tag}. {SENSITIVE_TEXT}.")
        _, turn = self.send(self.member_binding, a.orgadmin_email, "messages", case.payload)
        check(turn.status == 200, f"turno en «{slug}» → HTTP {turn.status}: {h3.snippet(turn.body)}")
        problems = self.tokenized_problems(case.tag, "messages")
        check(not problems, "\n".join(problems))
        doc = f"knowledge/e2e/e2e-{self.nonce}.md"
        content = f"---\ntitle: Prueba {self.nonce}\ntype: knowledge\nroles: []\n---\nDocumento de la prueba de H4.\n"
        try:
            with McpClient(
                str(self.member_binding["mcp_url"]), self.key_of(a.orgadmin_email), timeout=a.timeout
            ) as mcp:
                written = mcp.call_tool("baas_write_document", {"path": doc, "content": content})
        except Exception as exc:
            raise StepError(f"baas_write_document en «{slug}»: {type(exc).__name__}: {exc}") from None
        check(written.get("path") == doc, f"baas_write_document no devolvió la ruta: {written}")
        return (
            f"organización «{slug}» con AI-IP, {a.orgadmin_email} como ORG_ADMIN, "
            "un turno con valores y un documento en el MCP"
        )

    def vault_before(self) -> str:
        """Antes de eliminar, el vault tiene entradas de la organización (si no, «vacío» después no probaría nada)."""
        insp, slug = self.inspector, self.slug
        assert insp is not None  # el paso solo se registra con inspector
        problems = self.poll(lambda: [] if insp.vault_rows(slug) > 0 else ["el vault no tiene entradas"], 5)
        check(not problems, f"antes de eliminar, el vault de «{slug}» debería tener entradas: {problems}")
        return f"el vault tiene {insp.vault_rows(slug)} fila(s) de «{slug}»"

    def delete_rules(self) -> str:
        a, slug, problems = self.args, self.slug, []
        expect = lambda *args: self.expect(problems, *args)  # noqa: E731
        body = {"reason": REASON, "confirm_slug": slug}
        expect("sin bloquear", self.admin_post(f"/api/v1/orgs/{slug}/delete", body), 409, "ORG_NOT_BLOCKED")
        expect(f"{a.orgadmin_email} (ORG_ADMIN de la organización)",
               self.admin_post(f"/api/v1/orgs/{slug}/delete", body, who=a.orgadmin_email), 403)  # fmt: skip
        expect(
            "la organización meta",
            self.admin_post(f"/api/v1/orgs/{META_ORG}/delete", {"reason": REASON, "confirm_slug": META_ORG}),
            403,
            "ORG_PROTECTED",
        )
        expect(
            "sin motivo", self.admin_post(f"/api/v1/orgs/{slug}/delete", {"confirm_slug": slug}), 422, "REASON_REQUIRED"
        )
        res = self.webapp("GET", f"/api/v1/orgs/{slug}", a.admin_email)
        if res.status != 200:
            problems.append(f"tras las peticiones rechazadas, GET /api/v1/orgs/{slug} → HTTP {res.status}")
        check(not problems, "\n".join(problems))
        return (
            "sin bloquear → 409 ORG_NOT_BLOCKED; ORG_ADMIN → 403; meta → 403 ORG_PROTECTED; sin motivo → 422; "
            "la organización sigue existiendo"
        )

    def block_org(self) -> str:
        a, slug = self.args, self.slug
        res = self.admin_post(f"/api/v1/orgs/{slug}/block", {"blocked": True, "reason": REASON}, method="PUT")
        check(res.status == 200, f"PUT …/block → HTTP {res.status}: {h3.snippet(res.body, 200)}")

        def blocked() -> list[str]:
            got = self.member_chat()
            etype, code, _ = h3.provider_error(got.body)
            ok = got.status == 403 and code == "baas_access_blocked"
            return (
                []
                if ok
                else [f"el proxy respondió HTTP {got.status} {etype} {code} (se esperaba 403 baas_access_blocked)"]
            )

        problems = self.poll(blocked)
        check(not problems, "\n".join(problems))
        res = self.admin_post(f"/api/v1/orgs/{slug}/delete", {"reason": REASON, "confirm_slug": f"{slug}-otro"})
        err = (res.json() or {}).get("error") if isinstance(res.json(), dict) else None
        check(
            res.status == 422 and err == "SLUG_MISMATCH",
            f"con otro slug: HTTP {res.status} {err} (se esperaba 422 SLUG_MISMATCH)",
        )
        return (
            f"bloqueada: el proxy responde 403 baas_access_blocked a {a.orgadmin_email}; "
            "con otro slug → 422 SLUG_MISMATCH"
        )

    def delete_org(self) -> str:
        slug = self.slug
        res = self.admin_post(f"/api/v1/orgs/{slug}/delete", {"reason": REASON, "confirm_slug": slug})
        check(res.status == 200, f"POST …/delete → HTTP {res.status}: {h3.snippet(res.body, 200)}")
        propagated = (res.json() or {}).get("propagated")
        check(isinstance(propagated, bool), f"la respuesta no trae «propagated»: {h3.snippet(res.body, 200)}")
        return f"organización «{slug}» eliminada (propagated: {str(propagated).lower()})"

    def gone(self) -> str:
        a, slug = self.args, self.slug
        problems: list[str] = []
        res = self.webapp("GET", f"/api/v1/orgs/{slug}", a.admin_email)
        if res.status != 404:
            problems.append(f"GET /api/v1/orgs/{slug} → HTTP {res.status} (se esperaba 404)")
        again = self.admin_post(f"/api/v1/orgs/{slug}/delete", {"reason": REASON, "confirm_slug": slug})
        if again.status != 404:
            problems.append(f"la segunda eliminación → HTTP {again.status} (se esperaba 404)")
        listing = self.webapp("GET", f"/api/v1/conversations?org={slug}", a.admin_email)
        if listing.status == 200 and (listing.json() or {}).get("items"):
            problems.append("la webapp aún lista conversaciones de la organización eliminada")

        def proxy_denied() -> list[str]:
            got = self.member_chat()
            etype, code, _ = h3.provider_error(got.body)
            ok = got.status == 403 and code == "baas_access_denied"
            return (
                []
                if ok
                else [f"el proxy respondió HTTP {got.status} {etype} {code} (se esperaba 403 baas_access_denied)"]
            )

        problems += self.poll(proxy_denied)
        try:
            with McpClient(
                str(self.member_binding["mcp_url"]), self.key_of(a.orgadmin_email), timeout=a.timeout
            ) as mcp:
                mcp.call_tool("baas_whoami", {})
            problems.append("el MCP sigue sirviendo la organización eliminada")
        except McpToolError as exc:
            if exc.code != "NOT_FOUND":
                problems.append(f"el MCP respondió {exc.code} (se esperaba 404 NOT_FOUND)")
        except Exception as exc:
            problems.append(f"el MCP: {type(exc).__name__}: {exc}")
        hs = h3.http("POST", f"{a.app_url}/api/v1/plugin/handshake",
                     headers={"Authorization": f"Bearer {self.key_of(a.orgadmin_email)}"}, payload={},
                     timeout=a.timeout)  # fmt: skip
        listed = {o.get("slug") for o in (hs.json() or {}).get("organizations") or [] if isinstance(o, dict)}
        if hs.status != 200:
            problems.append(
                f"el handshake de {a.orgadmin_email} → HTTP {hs.status} "
                "(los usuarios no se eliminan con su organización)"
            )
        elif slug in listed:
            problems.append("el handshake sigue listando la organización eliminada")
        check(not problems, "\n".join(problems))
        return (
            "webapp 404 (también la segunda eliminación); proxy 403 baas_access_denied; MCP 404; "
            "el handshake sigue funcionando sin ella"
        )

    def slug_not_reusable(self) -> str:
        a, slug = self.args, self.slug
        reuse = self.admin_post("/api/v1/orgs", {"slug": slug, "name": "Otra vez"})
        taken = self.admin_post("/api/v1/orgs", {"slug": a.bypass_org, "name": "Duplicada"})
        got = (reuse.json() or {}).get("error") if isinstance(reuse.json(), dict) else None
        want = (taken.json() or {}).get("error") if isinstance(taken.json(), dict) else None
        check(
            reuse.status == taken.status == 409,
            f"crear «{slug}» → HTTP {reuse.status}; crear «{a.bypass_org}» (en uso) → HTTP {taken.status}"
            " (se esperaba 409 en los dos)",
        )
        check(got == want, f"el slug eliminado da {got}, un slug en uso da {want}: deberían ser el mismo error")
        return f"crear «{slug}» de nuevo → 409 {got}, igual que un slug en uso"

    def audit_survives(self) -> str:
        res = self.webapp("GET", "/api/v1/audit?action=org.deleted&limit=200", self.args.admin_email)
        check(res.status == 200, f"GET /api/v1/audit → HTTP {res.status}: {h3.snippet(res.body, 200)}")
        items = [i for i in (res.json() or {}).get("items") or [] if isinstance(i, dict)]
        mine = [i for i in items if self.slug in h3.strings_in(i)]
        check(
            mine,
            f"la auditoría no tiene ninguna entrada org.deleted de «{self.slug}» ({len(items)} org.deleted en total)",
        )
        reason_seen = any(REASON in h3.strings_in(i) for i in mine)
        check(reason_seen, "la entrada de auditoría de la eliminación no lleva el motivo")
        return f"org.deleted de «{self.slug}» en la auditoría, con su motivo"

    def inside(self) -> str:
        insp = self.inspector
        assert insp is not None  # el paso solo se registra con inspector
        problems = self.poll(
            lambda: self.inside_problems(insp, self.slug)
        )  # el proxy y el MCP lo hacen al recibir org.deleted
        check(not problems, "\n".join(problems))
        return "vault vacío, Redis sin sus claves y el directorio del MCP en cuarentena (.quarantine/)"

    def inside_problems(self, insp: Inspector, slug: str) -> list[str]:
        problems = []
        rows = insp.vault_rows(slug)
        if rows:
            problems.append(f"el vault conserva {rows} fila(s) de la organización")
        keys = insp.redis_keys(slug)
        if keys:
            problems.append(f"Redis conserva claves de la organización: {', '.join(keys[:5])}")
        return problems + self.quarantine_problems(insp, slug)

    def quarantine_problems(self, insp: Inspector, slug: str) -> list[str]:
        problems = []
        if slug in insp.mcp_org_dirs():
            problems.append(f"el directorio del MCP «orgs/{slug}» sigue en su sitio")
        if not [name for name in insp.mcp_quarantine() if name.startswith(f"{slug}-")]:
            problems.append(f"no hay ninguna entrada «{slug}-<fecha>» en .quarantine/ del MCP")
        return problems

    # --- el recorrido ------------------------------------------------------------------------------
    def run(self, runner: Runner) -> None:
        a = self.args
        groups = set(a.only)
        step = runner.step
        s0, s1 = "0. webapp accesible", "1. proveedor falso: /_debug/requests"
        s2 = f"2. handshake de {a.email} ({a.org}, AI-IP activo)"
        s2b = f"2b. handshake de {a.bypass_email} ({a.bypass_org}, AI-IP apagado)"
        s3 = f"3. sesiones: {a.admin_email}, {a.email}"
        s4 = f"4. «{a.org}» con el anonimizador apagado"
        step(s0, self.webapp_up)
        step(s1, self.fake_debug)
        need_obs = bool(groups & {"observacion", "interruptor"})
        if need_obs:
            step(s2, self.handshake_observation, needs=[s0])
        if "observacion" in groups:
            step(s2b, self.handshake_bypass, needs=[s0])
        logins = (
            [a.admin_email, a.email]
            + ([a.bypass_email] if "observacion" in groups else [])
            + ([a.orgadmin_email] if groups & {"interruptor", "eliminar"} else [])
        )
        step(s3, lambda: self.login(*logins), needs=[s0])
        cases = [(api, stream) for api in APIS for stream in (False, True)]
        if groups & {"observacion", "interruptor"}:
            step(s4, self.ensure_off, needs=[s3])
        if "observacion" in groups:
            names = []
            for i, (api, stream) in enumerate(cases, 1):
                if api in a.apis:
                    name = f"5.{i} anonimizador apagado · {API_NAMES[api]} · {h3.mode_name(stream)}"
                    names.append(name)
                    step(name, lambda api=api, stream=stream: self.observe_case(api, stream), needs=[s1, s2, s4])
            step("6. WARNING y la causa en la webapp (como dueño)", self.causes, needs=[s3, *names])
            step(
                "7.1 el PLATFORM_ADMIN no obtiene el contenido por la API",
                self.admin_without_content,
                needs=[s3, *names],
            )
            step(f"7.2 lo mismo con AI-IP apagado («{a.bypass_org}»)", self.admin_without_ai_ip, needs=[s2b, s3])
            step("7.3 el PLATFORM_ADMIN no obtiene el contenido por el WebSocket", self.websocket, needs=[s2, s3, s4])
        if "interruptor" in groups:
            sw = "8.1 permisos y validaciones del interruptor"
            step(sw, self.switch_rules, needs=[s3, s4])
            base = [s1, s2, s3, s4, sw]
            a1 = "8.2 conversaciones A: primer turno con el anonimizador apagado"
            on = "8.3 encender el anonimizador"
            b1 = "8.4 con él encendido: B nueva sale tokenizada"
            a2 = "8.5 con él encendido: A sigue en claro"
            off = "8.6 apagar el anonimizador"
            b2 = "8.7 con él apagado: B sigue tokenizada y restaurada"
            c1 = "8.8 con él apagado: C nueva sale en claro"
            step(a1, lambda: self.initial("A"), needs=base)
            step(on, lambda: self.switch(True), needs=[a1])
            step(b1, lambda: self.initial("B"), needs=[on])
            step(a2, lambda: self.continues("A"), needs=[a1, on])
            step(off, lambda: self.switch(False), needs=[on])
            step(b2, lambda: self.continues("B"), needs=[b1, off])
            step(c1, lambda: self.initial("C"), needs=[off])
            step("8.9 la webapp: A y C ✘/WARNING, B ✔/CLEAN", self.switch_state, needs=[a2, b2, c1])
            step("8.10 el interruptor vuelve a su valor inicial (apagado)", self.back_to_off, needs=[s3])
        if "eliminar" in groups:
            created = "9.1 crear la organización con datos"
            step(created, self.create_org, needs=[s0, s1, s3])
            if self.inspector is not None:
                step(
                    "9.1b el vault tiene entradas de la organización antes de eliminar",
                    self.vault_before,
                    needs=[created],
                )
            rules = "9.2 condiciones de la eliminación"
            step(rules, self.delete_rules, needs=[created])
            blocked = "9.3 bloquear la organización"
            step(blocked, self.block_org, needs=[rules])
            deleted = "9.4 eliminar con motivo y confirmación del slug"
            step(deleted, self.delete_org, needs=[blocked])
            step("9.5 la organización ya no existe (webapp, proxy, MCP, handshake)", self.gone, needs=[deleted])
            step("9.6 el slug no se puede reutilizar", self.slug_not_reusable, needs=[deleted])
            step("9.7 la auditoría conserva org.deleted", self.audit_survives, needs=[deleted])
            if self.inspector is not None:
                step("9.8 vault vacío, Redis sin claves y MCP en cuarentena", self.inside, needs=[deleted])


def main(argv: list[str] | None = None, inspector: Inspector | None = None) -> int:
    args = parse_args(argv)
    runner = Runner()
    inspector = inspector if inspector is not None else build_inspector(args) if "eliminar" in args.only else None
    e2e = E2EH4(args, inspector)
    print(f"Prueba de punta a punta H4 · webapp {args.app_url} · proveedor falso {args.fake_url}"
          f" · organización {args.org} · ejecución {e2e.nonce}")  # fmt: skip
    e2e.run(runner)
    report = {**runner.report(), "app_url": args.app_url, "fake_url": args.fake_url, "nonce": e2e.nonce,
              "only": args.only, "apis": args.apis,
              "inspect": args.inspect if inspector is not None else "none"}  # fmt: skip
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    failed = [r.name for r in runner.results if r.status != "OK"]
    print("RESULTADO:", "todo OK" if not failed else f"{len(failed)} paso(s) sin éxito: {', '.join(failed)}")
    return runner.exit_code()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
    sys.exit(130)
