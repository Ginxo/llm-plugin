"""Servidor BaaS falso para tests y pruebas manuales (handshake + MCP Streamable HTTP).

Implementa lo justo de los contratos de la Fase 1 para probar el plugin sin el stack real:

- `POST /api/v1/plugin/handshake` (arquitectura §3.1, contrato §6), con los datos de
  desarrollo del contrato §8 y sus API keys deterministas.
- `POST /org/<slug>/mcp` (fase-1-mvp §2.2, contrato §7.1): `initialize`,
  `notifications/initialized`, `tools/list` y `tools/call` de `baas_whoami` y
  `baas_get_script`, con sesión `Mcp-Session-Id` y respuestas en JSON o en SSE.

Uso manual: `python3 dev/fake_baas.py --port 8765` (imprime las claves de prueba).
Solo biblioteca estándar.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def dev_api_key(email: str) -> str:
    """API key determinista de desarrollo (contrato §8)."""
    digest = hashlib.sha256(f"baas-dev-key:{email}".encode()).digest()
    return "baas_" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@dataclass
class Membership:
    slug: str
    governance_role: str
    functional_roles: list[str]


@dataclass
class OrgState:
    slug: str
    name: str
    paused: bool = False
    blocked: bool = False
    ai_ip_enabled: bool = False


@dataclass
class UserState:
    email: str
    platform_admin: bool
    memberships: list[Membership]
    user_id: str = field(default_factory=lambda: "0b9e0000-0000-4000-8000-" + secrets.token_hex(6))


class Headers(dict):
    """Cabeceras con búsqueda sin distinguir mayúsculas (urllib las envía capitalizadas)."""

    def __init__(self, items: Any) -> None:
        super().__init__((k.lower(), v) for k, v in items)

    def __getitem__(self, key: str) -> str:
        return super().__getitem__(key.lower())

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and super().__contains__(key.lower())

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        return super().get(key.lower(), default)


def default_orgs() -> dict[str, OrgState]:
    return {
        "baas-ai-ip": OrgState("baas-ai-ip", "BaaS-AI-IP (meta)"),
        "demo": OrgState("demo", "Demo Clínica"),
        "otra": OrgState("otra", "Otra Organización"),
    }


def default_users() -> list[UserState]:
    return [
        UserState("admin@baas.test", True, [Membership("baas-ai-ip", "ORG_ADMIN", ["plataforma"])]),
        UserState("orgadmin@demo.test", False, [Membership("demo", "ORG_ADMIN", [])]),
        UserState("analista@demo.test", False, [Membership("demo", "USER", ["analista"])]),
        UserState("revisor@demo.test", False, [Membership("demo", "USER", ["revisor"])]),
        UserState("user@otra.test", False, [Membership("otra", "USER", ["analista"])]),
        # Casos adicionales para probar 0 y N organizaciones
        UserState("sinorg@demo.test", False, []),
        UserState(
            "multi@demo.test",
            False,
            [Membership("demo", "USER", ["analista"]), Membership("otra", "ORG_ADMIN", [])],
        ),
    ]


@dataclass
class Script:
    path: str
    content: str
    roles: list[str]
    sha256_override: str | None = None  # para simular un hash que no coincide

    @property
    def sha256(self) -> str:
        return self.sha256_override or hashlib.sha256(self.content.encode()).hexdigest()


def default_scripts() -> dict[str, dict[str, Script]]:
    hello = (
        "import os, sys\n"
        "print('hola desde', os.path.basename(__file__), 'args=' + ','.join(sys.argv[1:]))\n"
        "print('cabeceras-en-entorno=' + str('ANTHROPIC_CUSTOM_HEADERS' in os.environ))\n"
    )
    return {
        "demo": {
            "skills/informe/scripts/hola.py": Script("skills/informe/scripts/hola.py", hello, ["analista"]),
            "skills/informe/scripts/falla.py": Script(
                "skills/informe/scripts/falla.py", "import sys\nprint('voy a fallar')\nsys.exit(3)\n", ["analista"]
            ),
            "skills/informe/scripts/alterado.py": Script(
                "skills/informe/scripts/alterado.py", "print('no debería ejecutarse')\n", ["analista"], "0" * 64
            ),
            "skills/revision/scripts/solo-revisor.py": Script(
                "skills/revision/scripts/solo-revisor.py", "print('secreto')\n", ["revisor"]
            ),
        }
    }


class FakeBaas:
    """Estado del servidor falso; los tests lo manipulan directamente."""

    def __init__(self) -> None:
        self.orgs = default_orgs()
        self.users = default_users()
        self.scripts = default_scripts()
        self.global_kill_switch = False
        self.base_url = ""  # se rellena al arrancar
        self.requests: list[dict[str, Any]] = []
        self.sessions: set[str] = set()
        self.sse_responses = False  # responder a las peticiones MCP en SSE en lugar de JSON
        self.handshake_status: int | None = None  # forzar un código HTTP en el handshake
        self.mcp_status: int | None = None  # forzar un código HTTP en el MCP (p. ej. 503 UNAVAILABLE)
        self.proxy_base = ""  # base de las URLs del proxy (por defecto, <base_url>/proxy)
        self.stateless = False  # MCP sin estado: sin Mcp-Session-Id (como el MCP real de la Fase 1)
        self.lock = threading.Lock()

    # --- utilidades ---------------------------------------------------------------
    def user_for_key(self, key: str | None) -> UserState | None:
        if not key:
            return None
        for user in self.users:
            if secrets.compare_digest(dev_api_key(user.email), key):
                return user
        return None

    def membership(self, user: UserState, slug: str) -> Membership | None:
        return next((m for m in user.memberships if m.slug == slug), None)

    def handshake_body(self, user: UserState) -> dict[str, Any]:
        orgs = []
        proxy = self.proxy_base or f"{self.base_url}/proxy"
        for m in user.memberships:
            org = self.orgs[m.slug]
            orgs.append(
                {
                    "slug": org.slug,
                    "name": org.name,
                    "governance_role": m.governance_role,
                    "functional_roles": m.functional_roles,
                    "paused": org.paused,
                    "blocked": org.blocked,
                    "ai_ip_enabled": org.ai_ip_enabled,
                    "mcp_url": f"{self.base_url}/org/{org.slug}/mcp",
                    "proxy_anthropic_url": f"{proxy}/org/{org.slug}/anthropic",
                    "proxy_openai_url": f"{proxy}/org/{org.slug}/openai/v1",
                }
            )
        return {
            "user": {
                "id": user.user_id,
                "email": user.email,
                "platform_role": "PLATFORM_ADMIN" if user.platform_admin else "USER",
            },
            "global_kill_switch": self.global_kill_switch,
            "organizations": orgs,
        }


def _tool_error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload = {"error": code, "message": message, **extra}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": True}


def _tool_ok(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "structuredContent": data,
        "isError": False,
    }


def make_handler(state: FakeBaas) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # silencioso
            return

        # --- respuesta -----------------------------------------------------------------
        def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
            body = json.dumps(data, ensure_ascii=False).encode()
            self._send(status, body, {"Content-Type": "application/json", **(headers or {})})

        def _bearer(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            return auth[len("Bearer ") :] if auth.startswith("Bearer ") else None

        def _record(self, body: bytes) -> None:
            with state.lock:
                state.requests.append(
                    {"method": self.command, "path": self.path, "headers": Headers(self.headers.items()), "body": body}
                )

        # --- rutas -----------------------------------------------------------------------
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            self._record(body)
            if self.path == "/api/v1/plugin/handshake":
                return self._handshake()
            parts = self.path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "org" and parts[2] == "mcp":
                return self._mcp(parts[1], body)
            self._json(404, {"error": "NOT_FOUND"})

        def do_DELETE(self) -> None:
            self._record(b"")
            sid = self.headers.get("Mcp-Session-Id")
            with state.lock:
                state.sessions.discard(sid or "")
            self._send(204)

        def do_GET(self) -> None:
            self._record(b"")
            if self.path == "/healthz":
                return self._json(200, {"status": "ok", "version": "fake"})
            self._send(405)

        def _handshake(self) -> None:
            if state.handshake_status:
                return self._json(state.handshake_status, {"error": "FORCED"})
            user = state.user_for_key(self._bearer())
            if user is None:
                return self._json(401, {"error": "UNAUTHENTICATED", "message": "API key no válida"})
            self._json(200, state.handshake_body(user))

        def _mcp(self, slug: str, body: bytes) -> None:
            user = state.user_for_key(self._bearer())
            if user is None:
                return self._json(401, {"error": "UNAUTHENTICATED", "message": "API key no válida"})
            if slug not in state.orgs or (state.membership(user, slug) is None and not user.platform_admin):
                return self._json(404, {"error": "NOT_FOUND"})
            if state.mcp_status:
                return self._json(state.mcp_status, {"error": "UNAVAILABLE", "message": "Sin política disponible"})
            try:
                msg = json.loads(body)
            except ValueError:
                return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse"}})
            method = msg.get("method")
            if "id" not in msg:  # notificación
                return self._send(202)
            headers: dict[str, str] = {}
            if method == "initialize":
                if not state.stateless:
                    sid = secrets.token_hex(8)
                    with state.lock:
                        state.sessions.add(sid)
                    headers["Mcp-Session-Id"] = sid
                result: dict[str, Any] = {
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}, "prompts": {}},
                    "serverInfo": {"name": "fake-baas-mcp", "version": "0.0.0"},
                }
            else:
                if not state.stateless and self.headers.get("Mcp-Session-Id") not in state.sessions:
                    return self._json(404, {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32001}})
                if method == "tools/list":
                    result = {"tools": [{"name": n, "inputSchema": {"type": "object"}} for n in TOOLS]}
                elif method == "prompts/list":
                    result = {"prompts": [{"name": "informe", "description": "Skill de ejemplo"}]}
                elif method == "prompts/get":
                    result = {"messages": [{"role": "user", "content": {"type": "text", "text": "PROMPT-INFORME"}}]}
                elif method == "tools/call":
                    result = self._call_tool(user, slug, msg.get("params", {}))
                else:
                    return self._json(200, {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601}})
            response = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
            if state.sse_responses:
                # Un evento de progreso previo (otra notificación) y luego la respuesta
                sse = (
                    "event: message\n"
                    'data: {"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info","data":"x"}}\n\n'
                    "event: message\n"
                    f"data: {json.dumps(response, ensure_ascii=False)}\n\n"
                ).encode()
                return self._send(200, sse, {"Content-Type": "text/event-stream", **headers})
            self._json(200, response, headers)

        def _call_tool(self, user: UserState, slug: str, params: dict[str, Any]) -> dict[str, Any]:
            name = params.get("name")
            args = params.get("arguments") or {}
            target = args.get("org") or slug
            org = state.orgs.get(target)
            if org is None or (state.membership(user, target) is None and not user.platform_admin):
                return _tool_error("NOT_FOUND", "La organización no existe", resource=f"org:{target}")
            if state.global_kill_switch:
                return _tool_error("KILL_SWITCH_ACTIVE", "La plataforma está detenida temporalmente", scope="GLOBAL")
            if org.blocked:
                return _tool_error(
                    "KILL_SWITCH_ACTIVE", "Organización bloqueada por la plataforma", scope="ORG_BLOCKED"
                )
            if org.paused:
                return _tool_error(
                    "KILL_SWITCH_ACTIVE", "Organización pausada por su administrador", scope="ORG_PAUSED"
                )
            member = state.membership(user, target)
            roles = member.functional_roles if member else []
            if name == "baas_whoami":
                return _tool_ok({"user": user.email, "org": target, "functional_roles": roles})
            if name == "baas_get_script":
                # Misma firma que el mcp-server real: {skill, script, org?}; «path» no existe.
                unknown = set(args) - {"skill", "script", "org"}
                if unknown or not args.get("skill") or not args.get("script"):
                    field = sorted(unknown)[0] if unknown else "skill"
                    return _tool_error("INVALID_ARGUMENT", f"Argumento no válido «{field}»", field=field)
                path = f"skills/{args['skill']}/scripts/{args['script']}"
                script = state.scripts.get(target, {}).get(path)
                if script is None:
                    return _tool_error("NOT_FOUND", "El script no existe", resource=path)
                is_admin = user.platform_admin or (member is not None and member.governance_role == "ORG_ADMIN")
                if not is_admin and not set(roles) & set(script.roles):
                    return _tool_error(
                        "AUTHORIZATION_DENIED",
                        f"No tienes acceso a «{path}».",
                        resource=path,
                        required_roles=script.roles,
                    )
                return _tool_ok({"path": script.path, "sha256": script.sha256, "content": script.content})
            return _tool_error("INVALID_ARGUMENT", "Tool desconocida", field="name")

    return Handler


TOOLS = [
    "baas_whoami",
    "baas_list_skills",
    "baas_get_skill",
    "baas_get_script",
    "baas_search_knowledge",
    "baas_read_document",
    "baas_write_document",
]


class FakeBaasServer:
    """Arranca el servidor falso en un hilo, en un puerto efímero."""

    def __init__(self, state: FakeBaas | None = None, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = state or FakeBaas()
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.state))
        self.httpd.daemon_threads = True
        self.state.base_url = f"http://{host}:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return self.state.base_url

    def __enter__(self) -> FakeBaasServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = FakeBaasServer(host=args.host, port=args.port)
    print(f"BaaS falso en {server.url}")
    for user in server.state.users:
        print(f"  {user.email:22} {dev_api_key(user.email)}")
    with contextlib.suppress(KeyboardInterrupt):
        server.httpd.serve_forever()


if __name__ == "__main__":
    main()
