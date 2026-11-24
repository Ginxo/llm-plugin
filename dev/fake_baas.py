"""Servidor BaaS falso para tests y pruebas manuales (handshake + MCP Streamable HTTP).

Implementa lo justo de los contratos de la Fase 1 para probar el plugin sin el stack real:

- `POST /api/v1/plugin/handshake` (arquitectura §3.1, contrato §6), con los datos de
  desarrollo del contrato §8 y sus API keys deterministas.
- `POST /org/<slug>/mcp` (fase-1-mvp §2.2, contrato §7.1): `initialize`,
  `notifications/initialized`, `tools/list` y `tools/call` de `baas_whoami`, `baas_get_script` y,
  desde el hito H5 (contrato §7.4), `baas_list_skills` ampliada, `baas_get_skill_package` y
  `baas_get_skill_files`, con sesión `Mcp-Session-Id` y respuestas en JSON o en SSE.

Skills como paquetes (H5): `FakeBaas.add_skill(org, nombre, {ruta: contenido})` publica un paquete;
`state.skills[org][nombre]` es un `SkillPackage` editable. Interruptores de prueba: `package_paths`
(rutas del manifiesto como `skills/<skill>/<ruta>` o relativas), `mcp_delay` (servidor lento),
`list_filter` (la lista omite skills accesibles) y `tool_calls(nombre)` (llamadas recibidas).

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
import time
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


@dataclass
class PkgFile:
    """Un fichero de un paquete de skill (bytes). `sha256_override` hace que el manifiesto declare otro hash."""

    content: bytes
    executable: bool = False
    sha256_override: str | None = None

    @property
    def sha256(self) -> str:
        return self.sha256_override or hashlib.sha256(self.content).hexdigest()

    @property
    def real_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    def encoding(self) -> str:
        try:
            self.content.decode("utf-8")
        except UnicodeDecodeError:
            return "base64"
        return "base64" if b"\x00" in self.content else "utf8"

    def encoded(self) -> str:
        return self.content.decode("utf-8") if self.encoding() == "utf8" else base64.b64encode(self.content).decode()


@dataclass
class SkillPackage:
    """Paquete de una skill (contrato §7.4): ficheros relativos a la skill, roles y `baas.yaml` sintetizado."""

    name: str
    files: dict[str, PkgFile]
    roles: list[str] = field(default_factory=list)  # ["*"] = cualquier miembro
    status: str = "active"  # active | deprecated
    depends_on: list[str] = field(default_factory=list)
    knowledge: list[str] = field(default_factory=list)
    requires: dict[str, Any] = field(default_factory=dict)

    @property
    def executable(self) -> list[str]:
        return sorted(p for p, f in self.files.items() if f.executable)

    def metadata(self) -> dict[str, Any]:
        meta: dict[str, Any] = {"version": 1, "roles": self.roles, "status": self.status}
        if self.requires:
            meta["requires"] = self.requires
        if self.depends_on or self.knowledge:
            meta["depends_on"] = {"skills": self.depends_on, "knowledge": self.knowledge}
        if self.executable:
            meta["executable"] = self.executable
        return meta

    def all_files(self) -> dict[str, PkgFile]:
        """Los ficheros del paquete incluido `baas.yaml` (JSON es YAML válido)."""
        yaml = PkgFile(json.dumps(self.metadata(), ensure_ascii=False).encode() + b"\n")
        return {**self.files, "baas.yaml": yaml}

    @property
    def package_sha256(self) -> str:
        """SHA-256 de las líneas `<sha256>  <ruta>\\n`, ordenadas por ruta (contrato §7.4)."""
        files = self.all_files()
        text = "".join(f"{files[p].sha256}  {p}\n" for p in sorted(files, key=lambda p: p.encode()))
        return hashlib.sha256(text.encode()).hexdigest()

    @property
    def size(self) -> int:
        return sum(len(f.content) for f in self.all_files().values())


def _as_pkg_file(value: bytes | str | PkgFile) -> PkgFile:
    if isinstance(value, PkgFile):
        return value
    return PkgFile(value.encode() if isinstance(value, str) else value)


def skill_md(name: str, description: str = "Skill de ejemplo") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n"


def default_skills(scripts: dict[str, dict[str, Script]]) -> dict[str, dict[str, SkillPackage]]:
    """Un paquete por skill con los scripts de `default_scripts()` bajo `scripts/` (compatibilidad con H2).

    El script con hash alterado va en su propia skill (`alterada`): un paquete entero se descarga y verifica
    de una vez, y uno corrupto no debe estropear a los demás scripts de `informe`.
    """
    skills: dict[str, dict[str, SkillPackage]] = {}
    for org, by_path in scripts.items():
        for path, script in by_path.items():
            _, skill, _, fname = path.split("/", 3)
            if script.sha256_override:
                skill = "alterada"
            pkg = skills.setdefault(org, {}).setdefault(
                skill, SkillPackage(skill, {"SKILL.md": PkgFile(skill_md(skill).encode())}, roles=list(script.roles))
            )
            pkg.files[f"scripts/{fname}"] = PkgFile(script.content.encode(), sha256_override=script.sha256_override)
    return skills


class FakeBaas:
    """Estado del servidor falso; los tests lo manipulan directamente."""

    def __init__(self) -> None:
        self.orgs = default_orgs()
        self.users = default_users()
        self.scripts = default_scripts()
        self.skills = default_skills(self.scripts)  # paquetes (hito H5): org → nombre → SkillPackage
        self.package_paths = "store"  # rutas del manifiesto: "store" (skills/<skill>/<ruta>) o "relative"
        self.chunk_size = 8 * 1024 * 1024  # bytes que devuelve `baas_read_chunk` por omisión (contrato §7.4)
        self.mcp_delay = 0.0  # segundos que tarda el MCP en responder (simula un servidor lento)
        self.list_filter: set[str] | None = None  # si se fija, baas_list_skills solo lista estas skills
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

    # --- paquetes de skills (hito H5) ---------------------------------------------
    def add_skill(
        self,
        org: str,
        name: str,
        files: dict[str, bytes | str | PkgFile],
        *,
        roles: list[str] | None = None,
        executable: tuple[str, ...] | list[str] = (),
        **kwargs: Any,
    ) -> SkillPackage:
        """Publica (o reemplaza) el paquete de una skill. `SKILL.md` se crea si falta."""
        pkg_files = {path: _as_pkg_file(content) for path, content in files.items()}
        pkg_files.setdefault("SKILL.md", PkgFile(skill_md(name).encode()))
        for path in executable:
            pkg_files[path].executable = True
        pkg = SkillPackage(name, pkg_files, roles=list(roles or []), **kwargs)
        self.skills.setdefault(org, {})[name] = pkg
        return pkg

    def tool_calls(self, name: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        """Las llamadas `tools/call` recibidas: lista de (tool, argumentos)."""
        calls = []
        for req in self.requests:
            if req["method"] != "POST" or not req["body"]:
                continue
            try:
                msg = json.loads(req["body"])
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("method") == "tools/call":
                params = msg.get("params") or {}
                if name is None or params.get("name") == name:
                    calls.append((params.get("name"), params.get("arguments") or {}))
        return calls

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
            if state.mcp_delay:
                time.sleep(state.mcp_delay)
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
            if name == "baas_list_skills":
                return self._list_skills(user, target, args)
            if name in ("baas_get_skill_package", "baas_get_skill_files"):
                return self._package_tool(user, target, name, args)
            if name == "baas_read_chunk":
                return self._read_chunk(user, target, args)
            return _tool_error("INVALID_ARGUMENT", "Tool desconocida", field="name")

        # --- skills como paquetes (contrato §7.4) -----------------------------------------
        def _is_admin(self, user: UserState, slug: str) -> bool:
            member = state.membership(user, slug)
            return user.platform_admin or (member is not None and member.governance_role == "ORG_ADMIN")

        def _can_read(self, user: UserState, slug: str, pkg: SkillPackage) -> bool:
            member = state.membership(user, slug)
            if self._is_admin(user, slug):
                return True
            roles = set(member.functional_roles) if member else set()
            return ("*" in pkg.roles and member is not None) or bool(roles & set(pkg.roles))

        def _store_path(self, skill: str, rel: str) -> str:
            return f"skills/{skill}/{rel}" if state.package_paths == "store" else rel

        def _list_skills(self, user: UserState, slug: str, args: dict[str, Any]) -> dict[str, Any]:
            rows = []
            for name, pkg in sorted(state.skills.get(slug, {}).items()):
                if pkg.status != "active" or not self._can_read(user, slug, pkg):
                    continue
                if state.list_filter is not None and name not in state.list_filter:
                    continue
                skill_md_file = pkg.files.get("SKILL.md")  # un paquete de prueba puede no tenerlo
                scripts = [
                    {"name": p.removeprefix("scripts/"), "path": f"skills/{name}/{p}", "sha256": f.sha256,
                     "size": len(f.content)}
                    for p, f in sorted(pkg.files.items())
                    if p.startswith("scripts/")
                ]  # fmt: skip
                rows.append(
                    {
                        "name": name,
                        "title": name,
                        "description": "Skill de ejemplo",
                        "path": f"skills/{name}/SKILL.md",
                        "roles": pkg.roles,
                        "sha256": skill_md_file.sha256 if skill_md_file else "",
                        "scripts": scripts,
                        "package_sha256": pkg.package_sha256,
                        "status": pkg.status,
                        "files": len(pkg.all_files()),
                        "size": pkg.size,
                        "depends_on": {"skills": pkg.depends_on, "knowledge": pkg.knowledge},
                        "executable": pkg.executable,
                        "warnings": [],
                    }
                )
            return _tool_ok({"organization": slug, "skills": rows})

        def _dependency(self, user: UserState, slug: str, name: str) -> dict[str, Any]:
            dep = state.skills.get(slug, {}).get(name)
            ok = dep is not None and dep.status == "active" and self._can_read(user, slug, dep)
            info: dict[str, Any] = {"name": name, "accessible": ok}
            if ok:
                info["package_sha256"] = dep.package_sha256
            return info

        def _read_chunk(self, user: UserState, slug: str, args: dict[str, Any]) -> dict[str, Any]:
            """`baas_read_chunk` (contrato §7.4): un trozo de un fichero de una skill, en base64."""
            path = args.get("path")
            parts = path.split("/", 2) if isinstance(path, str) else []
            if len(parts) != 3 or parts[0] != "skills":
                return _tool_error("NOT_FOUND", "No existe el fichero", resource=str(path))
            pkg = state.skills.get(slug, {}).get(parts[1])
            if pkg is None or (pkg.status != "active" and not self._is_admin(user, slug)):
                return _tool_error("NOT_FOUND", "La skill no existe", resource=f"skills/{parts[1]}")
            if not self._can_read(user, slug, pkg):
                return _tool_error(
                    "AUTHORIZATION_DENIED", f"No tienes acceso a «{path}».", resource=path, required_roles=pkg.roles
                )
            f = pkg.all_files().get(parts[2])
            if f is None:
                return _tool_error("NOT_FOUND", "No existe el fichero", resource=path)
            expected = args.get("expected_sha256")
            if expected and expected != f.sha256:
                return _tool_error("CONFLICT", "El fichero cambió", resource=path, current_sha256=f.sha256)
            offset = int(args.get("offset") or 0)
            length = int(args.get("length") or state.chunk_size)
            if offset > len(f.content):
                return _tool_error("INVALID_ARGUMENT", "Desplazamiento fuera del fichero", field="offset")
            piece = f.content[offset : offset + length]
            return _tool_ok(
                {
                    "path": path,
                    "size": len(f.content),
                    "sha256": f.sha256,
                    "offset": offset,
                    "length": len(piece),
                    "eof": offset + len(piece) >= len(f.content),
                    "encoding": "base64",
                    "content": base64.b64encode(piece).decode(),
                }
            )

        def _package_tool(self, user: UserState, slug: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            allowed = {"skill", "org"} | ({"paths"} if tool == "baas_get_skill_files" else set())
            unknown = set(args) - allowed
            if unknown or not isinstance(args.get("skill"), str):
                field = sorted(unknown)[0] if unknown else "skill"
                return _tool_error("INVALID_ARGUMENT", f"Argumento no válido «{field}»", field=field)
            name = args["skill"]
            pkg = state.skills.get(slug, {}).get(name)
            resource = f"skills/{name}"
            if pkg is None or (pkg.status != "active" and not self._is_admin(user, slug)):
                return _tool_error("NOT_FOUND", "La skill no existe", resource=resource)
            if not self._can_read(user, slug, pkg):
                return _tool_error(
                    "AUTHORIZATION_DENIED", f"No tienes acceso a «{resource}».", resource=resource,
                    required_roles=pkg.roles,
                )  # fmt: skip
            files = pkg.all_files()
            if tool == "baas_get_skill_package":
                entries = [
                    {
                        "path": self._store_path(name, p),
                        "sha256": f.sha256,
                        "size": len(f.content),
                        "encoding": f.encoding(),
                        "executable": f.executable,
                    }
                    for p, f in sorted(files.items())
                ]
                return _tool_ok(
                    {
                        "skill": name,
                        "name": name,
                        "status": pkg.status,
                        "roles": pkg.roles,
                        "package_sha256": pkg.package_sha256,
                        "frontmatter": {"name": name, "description": "Skill de ejemplo"},
                        "metadata": pkg.metadata(),
                        "files": entries,
                        "depends_on": {
                            "skills": [self._dependency(user, slug, d) for d in pkg.depends_on],
                            "knowledge": pkg.knowledge,
                        },
                        "warnings": [],
                    }
                )
            by_server_path = {self._store_path(name, p): p for p in files}
            wanted = args.get("paths")
            if wanted is None:
                wanted = list(by_server_path)
            out = []
            for path in wanted:
                rel = by_server_path.get(path)
                if rel is None:
                    return _tool_error("NOT_FOUND", "El fichero no pertenece a la skill", resource=str(path))
                f = files[rel]
                out.append(
                    {
                        "path": path,
                        "sha256": f.real_sha256,
                        "size": len(f.content),
                        "encoding": f.encoding(),
                        "content": f.encoded(),
                    }
                )
            return _tool_ok({"skill": name, "files": out})

    return Handler


TOOLS = [
    "baas_whoami",
    "baas_list_skills",
    "baas_get_skill",
    "baas_get_script",
    "baas_get_skill_package",
    "baas_get_skill_files",
    "baas_read_chunk",
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
