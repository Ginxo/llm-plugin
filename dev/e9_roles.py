#!/usr/bin/env python3
"""Prueba de E9 (hito H4): los roles funcionales aíslan el contenido, aunque una skill lo cite.

Contra una pila real de BaaS-AI-IP (la de `deploy`, con `BAAS_DEV_SKIP_TOTP=1`), crea lo que falte y comprueba:

  1. Organización `e9-prueba` («E9 Prueba») con dos roles funcionales distintos, `recepcion` y `facturacion`, y cuatro
     usuarios nuevos (`@e9.test`): un ORG_ADMIN, un USER solo con `recepcion`, un USER solo con `facturacion` y un
     USER sin ningún rol funcional. Los activa por la API (contraseña de desarrollo) y les crea una API key.
  2. Contenido escrito por MCP como ORG_ADMIN, con el procedimiento de las skills `componer-conocimiento` y
     `componer-skill` de la organización meta (buscar antes de crear, cabecera válida, `expected_sha256`, verificar
     con la búsqueda): un documento y una skill con script para cada rol, y un documento sin roles. La skill de cada
     rol **cita la ruta del documento del otro**.
  3. Con la clave de cada usuario: el del rol A busca, lee y obtiene lo suyo; la búsqueda no devuelve ni deja ver
     trozos de lo del rol B; leer lo del rol B, por su ruta o siguiendo la cita de la skill, da `AUTHORIZATION_DENIED`
     con los `required_roles` correctos; y lo simétrico con el rol B. Un USER sin rol no ve ni lee nada; un usuario de
     otra organización no ve nada de `e9-prueba` (`NOT_FOUND`).

Es idempotente: una segunda ejecución no crea nada nuevo (las API keys `e9-roles-*` se renuevan y se revocan las
anteriores). Guarda la evidencia (llamada → respuesta, sin claves ni enlaces) con `--evidence`.

Uso (pila con `scripts/dev-up.sh`; las URLs del MCP salen del *handshake*):

    uv run python dev/e9_roles.py --app-url http://app.localhost:28080 --evidence e9-evidencia.json

Sale con 0 si todo pasa, 1 si algún paso falla y 2 si los argumentos no son válidos. No se distribuye con el plugin.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_h2 import DEV_PASSWORD, Runner, StepError, check, cookies_from, dev_api_key, http

from baas_plugin.errors import AuthError, McpToolError
from baas_plugin.mcp_client import McpClient

ORG, ORG_NAME = "e9-prueba", "E9 Prueba"
ROLE_A, ROLE_B = "recepcion", "facturacion"
ROLE_DESCRIPTIONS = {
    ROLE_A: ("Recepción", "Atiende el mostrador y gestiona las citas"),
    ROLE_B: ("Facturación", "Emite y revisa las facturas"),
}
# etiqueta → (email, rol de gobierno, roles funcionales)
USERS: dict[str, tuple[str, str, list[str]]] = {
    "admin": ("e9-admin@e9.test", "ORG_ADMIN", []),
    "a": ("e9-recepcion@e9.test", "USER", [ROLE_A]),
    "b": ("e9-facturacion@e9.test", "USER", [ROLE_B]),
    "c": ("e9-sinrol@e9.test", "USER", []),
}
OTHER_ORG_USER, OTHER_ORG = "user@otra.test", "otra"  # usuario de desarrollo (contrato §8): solo es miembro de «otra»
KEY_LABEL_PREFIX = "e9-roles-"
ADMIN_ROLES = ["ORG_ADMIN", "PLATFORM_ADMIN"]

DOC_A = "knowledge/recepcion/protocolo-de-citas.md"
DOC_B = "knowledge/facturacion/criterios-de-facturacion.md"
DOC_ADMIN = "knowledge/direccion/reservado.md"
SKILL_A, SKILL_B = "gestion-de-citas", "emision-de-facturas"
SCRIPT_A, SCRIPT_B = "resumen_citas.py", "resumen_facturas.py"
SKILL_A_PATH, SKILL_B_PATH = f"skills/{SKILL_A}/SKILL.md", f"skills/{SKILL_B}/SKILL.md"
SCRIPT_A_PATH, SCRIPT_B_PATH = f"skills/{SKILL_A}/scripts/{SCRIPT_A}", f"skills/{SKILL_B}/scripts/{SCRIPT_B}"

MARK_A, MARK_B, MARK_ADMIN = "E9-RECEPCION-7731", "E9-FACTURACION-4159", "E9-RESERVADO-9026"
# Frases que solo están en el cuerpo de cada documento (para comprobar que la búsqueda no deja ver trozos)
BODY_A, BODY_B = "confirman por teléfono con 24 horas", "plazo de cinco días laborables"
TITLE_A, TITLE_B = "Protocolo de citas de recepción", "Criterios de facturación"


# --- contenido (ficticio; ningún dato personal) ------------------------------------------------------


def doc_a() -> str:
    return (
        "---\n"
        f"title: {TITLE_A}\n"
        "type: knowledge\n"
        f"roles: [{ROLE_A}]\n"
        "status: active\n"
        "---\n"
        f"# {TITLE_A}\n\n"
        "## Para qué sirve\n\n"
        f"Fija cómo se gestionan las citas en el mostrador. Marca de la prueba: {MARK_A}.\n\n"
        "## Procedimiento\n\n"
        f"1. Las citas se {BODY_A} de antelación.\n"
        "2. Si la persona no responde, se anota la incidencia y se ofrece otro hueco.\n"
    )


def doc_b() -> str:
    return (
        "---\n"
        f"title: {TITLE_B}\n"
        "type: knowledge\n"
        f"roles: [{ROLE_B}]\n"
        "status: active\n"
        "---\n"
        f"# {TITLE_B}\n\n"
        "## Para qué sirve\n\n"
        f"Fija cuándo y cómo se factura una visita. Marca de la prueba: {MARK_B}.\n\n"
        "## Procedimiento\n\n"
        f"1. La factura se emite en un {BODY_B} tras la visita.\n"
        "2. La visita de revisión se factura con la tarifa de revisión del año en curso.\n"
    )


def doc_admin() -> str:
    return (
        "---\n"
        "title: Documento reservado a la administración\n"
        "type: knowledge\n"
        "roles: []\n"
        "status: active\n"
        "---\n"
        "# Documento reservado a la administración\n\n"
        f"Sin roles: solo lo ven los administradores (denegación por defecto). Marca de la prueba: {MARK_ADMIN}.\n"
    )


def skill_a() -> str:
    return (
        "---\n"
        "title: Gestión de citas\n"
        "type: skill\n"
        f"roles: [{ROLE_A}]\n"
        "status: active\n"
        "description: Resume las citas del día para recepción y recuerda el protocolo.\n"
        "---\n"
        "# Gestión de citas\n\n"
        "Úsala cuando recepción pida un resumen de las citas del día.\n\n"
        "## Pasos\n\n"
        f"1. Lee el protocolo con `baas_read_document` (`{DOC_A}`).\n"
        f"2. Ejecuta `scripts/{SCRIPT_A}` con las citas en CSV (`estado,hora`) por la entrada estándar.\n"
        f"3. Antes de cerrar el día, comprueba cómo se factura en `{DOC_B}`.\n\n"
        "Si recibes AUTHORIZATION_DENIED o KILL_SWITCH_ACTIVE, detente e informa al usuario.\n"
    )


def skill_b() -> str:
    return (
        "---\n"
        "title: Emisión de facturas\n"
        "type: skill\n"
        f"roles: [{ROLE_B}]\n"
        "status: active\n"
        "description: Resume las facturas pendientes para facturación y recuerda los criterios.\n"
        "---\n"
        "# Emisión de facturas\n\n"
        "Úsala cuando facturación pida un resumen de las facturas pendientes.\n\n"
        "## Pasos\n\n"
        f"1. Lee los criterios con `baas_read_document` (`{DOC_B}`).\n"
        f"2. Ejecuta `scripts/{SCRIPT_B}` con las facturas en CSV (`estado,importe`) por la entrada estándar.\n"
        f"3. Si la visita tuvo cita, consulta el protocolo de recepción en `{DOC_A}`.\n\n"
        "Si recibes AUTHORIZATION_DENIED o KILL_SWITCH_ACTIVE, detente e informa al usuario.\n"
    )


def script_for(column: str, what: str) -> str:
    return (
        f'"""Cuenta {what} por {column} a partir de un CSV por la entrada estándar (solo biblioteca estándar)."""\n\n'
        "import csv\n"
        "import sys\n"
        "from collections import Counter\n\n\n"
        "def main() -> int:\n"
        "    cuenta = Counter(fila[0] for fila in csv.reader(sys.stdin) if fila)\n"
        "    for clave, n in sorted(cuenta.items()):\n"
        '        print(f"{clave}: {n}")\n'
        "    return 0\n\n\n"
        'if __name__ == "__main__":\n'
        "    sys.exit(main())\n"
    )


CONTENT: dict[str, str] = {
    DOC_A: doc_a(),
    DOC_B: doc_b(),
    DOC_ADMIN: doc_admin(),
    SKILL_A_PATH: skill_a(),
    SCRIPT_A_PATH: script_for("estado", "las citas"),
    SKILL_B_PATH: skill_b(),
    SCRIPT_B_PATH: script_for("estado", "las facturas"),
}


@dataclass(frozen=True)
class Side:
    """Un lado de la prueba: el rol, su documento, su skill con script y lo que solo él debe ver."""

    role: str
    doc: str
    skill: str
    script: str
    mark: str
    body: str
    title: str

    @property
    def skill_path(self) -> str:
        return f"skills/{self.skill}/SKILL.md"

    @property
    def script_path(self) -> str:
        return f"skills/{self.skill}/scripts/{self.script}"

    @property
    def paths(self) -> set[str]:
        return {self.doc, self.skill_path, self.script_path}


SIDE_A = Side(ROLE_A, DOC_A, SKILL_A, SCRIPT_A, MARK_A, BODY_A, TITLE_A)
SIDE_B = Side(ROLE_B, DOC_B, SKILL_B, SCRIPT_B, MARK_B, BODY_B, TITLE_B)

B_ONLY = SIDE_B.paths  # lo que el rol A no debe ver
A_ONLY = SIDE_A.paths  # lo que el rol B no debe ver
RESERVED = {DOC_ADMIN}  # sin roles: solo administradores


# --- utilidades puras ------------------------------------------------------------------------------

ROLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SECRET_RE = re.compile(r"baas_[A-Za-z0-9_-]{30,}|/activate/[A-Za-z0-9_-]{16,}|baas_session=[A-Za-z0-9_-]{16,}")
CITED_RE = re.compile(r"knowledge/[A-Za-z0-9_./-]+\.md")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def header_errors(text: str, kind: str) -> list[str]:
    """Lo mismo que `validar_cabecera.py` de la skill `componer-skill` (y algo más para el conocimiento)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ["falta la cabecera: la primera línea debe ser ---"]
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return ["la cabecera no está cerrada con ---"]
    header: dict[str, Any] = {}
    for line in lines[1:end]:
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            header[key.strip()] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        else:
            header[key.strip()] = value
    errors = [f"falta «{k}»" for k in ("title", "type", "status") if not header.get(k)]
    if kind == "skill" and not header.get("description"):
        errors.append("falta «description»")
    if "roles" not in header:
        errors.append("falta «roles» (sin roles solo lo verían los administradores)")
    if header.get("type") not in (None, kind):
        errors.append(f"«type» debe ser {kind}")
    if header.get("status") not in (None, "active", "deprecated"):
        errors.append("«status» debe ser active o deprecated")
    roles = header.get("roles", [])
    if not isinstance(roles, list):
        errors.append("«roles» debe ser una lista: [rol1, rol2]")
    else:
        errors += [f"rol no válido: {r}" for r in roles if not ROLE_RE.match(r)]
    # YAML: un valor con «: » dentro (por ejemplo un título) sin comillas rompe la cabecera en el servidor
    errors += [f"«{k}» lleva «: » sin comillas" for k, v in header.items() if isinstance(v, str) and ": " in v]
    return errors


def cited_paths(text: str) -> list[str]:
    """Rutas de conocimiento que cita un texto (la skill del rol A cita la del documento B)."""
    return list(dict.fromkeys(CITED_RE.findall(text)))


def leaks(response: Any, forbidden_paths: set[str], forbidden_text: list[str]) -> list[str]:
    """Qué de lo prohibido aparece en una respuesta de búsqueda o de listado: rutas de resultados (`path`) y
    trozos del cuerpo o del título (`title`, `excerpt`, `description`). Citar una ruta dentro del texto de una skill
    propia no cuenta: es lo que prueba el caso de la cita."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            path = node.get("path")
            if isinstance(path, str) and path in forbidden_paths:
                found.append(f"ruta {path}")
            for key in ("title", "excerpt", "description", "content"):
                value = node.get(key)
                if isinstance(value, str):
                    found.extend(f"«{t}» en {key}" for t in forbidden_text if t.lower() in value.lower())
            for key, value in node.items():
                if key != "content":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(response)
    return found


def redact(value: Any, limit: int = 600) -> Any:
    """Copia de una respuesta para la evidencia: textos largos recortados y nada que parezca un secreto."""
    if isinstance(value, dict):
        return {k: redact(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, limit) for v in value]
    if isinstance(value, str):
        text = SECRET_RE.sub("[oculto]", value)
        return text if len(text) <= limit else text[:limit] + f"… ({len(value)} caracteres)"
    return value


@dataclass
class Evidence:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, who: str, call: str, arguments: dict[str, Any], *, response: Any = None, error: Any = None) -> None:
        entry: dict[str, Any] = {"as": who, "call": call, "arguments": redact(arguments)}
        entry["error" if error is not None else "response"] = redact(error if error is not None else response)
        self.calls.append(entry)


class McpSession:
    """Una sesión MCP con la clave de un usuario. `call` devuelve (éxito, respuesta o error) y lo anota."""

    def __init__(self, who: str, url: str, key: str, evidence: Evidence, timeout: float) -> None:
        self.who, self.evidence = who, evidence
        self.client = McpClient(url, key, timeout=timeout)

    def __enter__(self) -> McpSession:
        self.client.initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.close()

    def call(self, tool: str, **arguments: Any) -> tuple[bool, dict[str, Any]]:
        try:
            result = self.client.call_tool(tool, arguments)
        except McpToolError as exc:
            payload = exc.payload or {"error": exc.code, "message": exc.message}
            self.evidence.add(self.who, tool, arguments, error=payload)
            return False, payload
        self.evidence.add(self.who, tool, arguments, response=result)
        return True, result


def error_code(payload: dict[str, Any]) -> str:
    return str(payload.get("error", ""))


# --- la prueba ------------------------------------------------------------------------------------


class E9:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.evidence = Evidence()
        self.sessions: dict[str, str] = {}  # email → cookie de sesión de la webapp (no se escribe en la evidencia)
        self.keys: dict[str, str] = {}  # etiqueta → API key (solo en memoria)
        self.mcp_urls: dict[str, str] = {}  # organización → URL del MCP, del handshake
        self.created: list[str] = []
        self.pending_links: dict[str, str] = {}  # email → enlace de activación recién emitido (nunca se escribe)

    # utilidades
    def webapp(self, method: str, path: str, who: str, payload: Any = None) -> Any:
        headers = {"Cookie": f"baas_session={self.sessions[who]}"}
        if method != "GET":
            headers["X-Requested-With"] = "baas-webapp"
        return http(method, f"{self.args.app_url}/api/v1{path}", headers=headers, payload=payload)

    def login(self, email: str) -> None:
        """Inicia sesión con la contraseña de desarrollo. Con `RATE_LIMITED` (20 por minuto y IP, 10 por usuario)
        espera lo que dice `retry_after_seconds` y repite: dos ejecuciones seguidas pasan de ese límite."""
        for attempt in range(3):
            res = http(
                "POST",
                f"{self.args.app_url}/api/v1/auth/login",
                headers={"X-Requested-With": "baas-webapp"},
                payload={"email": email, "password": self.args.password},
            )
            if res.status != 429 or attempt == 2:
                break
            wait = min(int((res.json() or {}).get("retry_after_seconds") or 30), 65)
            print(f"          (RATE_LIMITED al iniciar sesión como {email}: espero {wait} s)")
            time.sleep(wait + 1)
        check(res.status == 200, f"inicio de sesión de {email} → HTTP {res.status}: {(res.json() or {}).get('error')}")
        cookie = cookies_from(res.headers).get("baas_session")
        check(cookie, f"{email}: sin sesión (¿la pila tiene BAAS_DEV_SKIP_TOTP=1?): {(res.json() or {}).get('next')}")
        self.sessions[email] = str(cookie)

    def key_of(self, label: str) -> str:
        return self.keys[label]

    def session(self, label: str, org: str = ORG) -> McpSession:
        return McpSession(label, self.mcp_urls[org], self.key_of(label), self.evidence, self.args.timeout)

    # 0
    def stack_up(self) -> str:
        res = http("GET", f"{self.args.app_url}/healthz")
        check(res.status == 200, f"GET {self.args.app_url}/healthz → HTTP {res.status}")
        return f"{self.args.app_url} responde"

    # 1-2
    def admin_login(self) -> str:
        self.login(self.args.admin_email)
        me = self.webapp("GET", "/me", self.args.admin_email).json() or {}
        check(me.get("is_platform_admin") is True, f"{self.args.admin_email} no es PLATFORM_ADMIN")
        return f"sesión de {self.args.admin_email} (PLATFORM_ADMIN)"

    def ensure_org(self) -> str:
        admin = self.args.admin_email
        res = self.webapp("GET", f"/orgs/{ORG}", admin)
        if res.status == 200:
            return f"«{ORG}» ya existía"
        check(res.status == 404, f"GET /orgs/{ORG} → HTTP {res.status}")
        email = USERS["admin"][0]
        res = self.webapp("POST", "/orgs", admin, {"slug": ORG, "name": ORG_NAME, "first_admin_email": email})
        check(res.status == 201, f"POST /orgs → HTTP {res.status}: {res.body[:200]!r}")
        self.created.append(f"organización {ORG}")
        body = res.json() or {}
        self.pending_links[email] = str(body.get("activation_url") or "")
        return f"«{ORG}» creada con {email} como primer ORG_ADMIN"

    # 3
    def ensure_roles(self) -> str:
        admin, done = self.args.admin_email, []
        listed = self.webapp("GET", f"/orgs/{ORG}/roles?include_deleted=1", admin).json() or {}
        have = {r["slug"]: r for r in listed.get("roles", [])}
        for slug, (name, description) in ROLE_DESCRIPTIONS.items():
            role = have.get(slug)
            if role is None:
                res = self.webapp(
                    "POST", f"/orgs/{ORG}/roles", admin, {"slug": slug, "name": name, "description": description}
                )
                check(res.status == 201, f"crear el rol {slug} → HTTP {res.status}: {res.body[:200]!r}")
                done.append(f"{slug} creado")
                self.created.append(f"rol {slug}")
            elif role.get("deleted_at"):
                res = self.webapp("POST", f"/orgs/{ORG}/roles/{slug}/restore", admin)
                check(res.status in (200, 204), f"restaurar el rol {slug} → HTTP {res.status}")
                done.append(f"{slug} restaurado")
            else:
                done.append(f"{slug} existía")
        return ", ".join(done)

    # 4
    def ensure_members(self) -> str:
        admin, out = self.args.admin_email, []
        links = self.pending_links
        for label, (email, governance, roles) in USERS.items():
            members = {
                m["email"]: m
                for m in (self.webapp("GET", f"/orgs/{ORG}/members", admin).json() or {}).get("members", [])
            }
            member = members.get(email)
            link = links.get(email, "")
            if member is None:
                payload = {
                    "email": email,
                    "name": email.split("@")[0],
                    "governance_role": governance,
                    "functional_roles": roles,
                }
                res = self.webapp("POST", f"/orgs/{ORG}/members", admin, payload)
                check(res.status == 201, f"invitar a {email} → HTTP {res.status}: {res.body[:200]!r}")
                body = res.json() or {}
                member, link = body["member"], str(body.get("activation_url") or "")
                self.created.append(f"miembro {email}")
                out.append(f"{label}: invitado")
            elif (member["governance_role"], sorted(member["functional_roles"])) != (governance, sorted(roles)):
                res = self.webapp(
                    "PATCH",
                    f"/orgs/{ORG}/members/{member['user_id']}",
                    admin,
                    {"governance_role": governance, "functional_roles": roles},
                )
                check(res.status == 200, f"corregir los roles de {email} → HTTP {res.status}: {res.body[:200]!r}")
                out.append(f"{label}: roles corregidos")
            else:
                out.append(f"{label}: ya estaba")
            if member["status"] == "INVITED":
                if not link:
                    res = self.webapp("POST", f"/orgs/{ORG}/members/{member['user_id']}/activation-link", admin)
                    check(res.status in (200, 201), f"enlace de {email} → HTTP {res.status}")
                    link = str((res.json() or {}).get("activation_url") or "")
                self.activate(email, link)
                out[-1] += " y activado"
        return "; ".join(out)

    def activate(self, email: str, link: str) -> None:
        token = link.rstrip("/").rsplit("/", 1)[-1]
        check(token, f"sin enlace de activación para {email}")
        res = http(
            "POST",
            f"{self.args.app_url}/api/v1/auth/activation/{token}/password",
            headers={"X-Requested-With": "baas-webapp"},
            payload={"password": self.args.password, "locale": "es"},
        )
        body = res.json() or {}
        check(
            res.status == 200 and body.get("next") == "DONE",
            f"activar a {email} → HTTP {res.status} {body.get('next') or body.get('error')} "
            "(esta prueba necesita BAAS_DEV_SKIP_TOTP=1)",
        )

    # 5
    def make_keys(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        label = f"{KEY_LABEL_PREFIX}{stamp}"
        revoked = 0
        for who, (email, _, _) in USERS.items():
            self.login(email)
            res = self.webapp("POST", "/api-keys", email, {"label": label})
            check(res.status == 201, f"crear la API key de {email} → HTTP {res.status}")
            self.keys[who] = str((res.json() or {}).get("secret") or "")
            check(self.keys[who].startswith("baas_"), f"la API key de {email} no tiene la forma esperada")
            listed = (self.webapp("GET", "/api-keys?scope=mine", email).json() or {}).get("items", [])
            for item in listed:
                if (
                    str(item["label"]).startswith(KEY_LABEL_PREFIX)
                    and item["label"] != label
                    and not item["revoked_at"]
                ):
                    res = self.webapp(
                        "POST", f"/api-keys/{item['id']}/revoke", email, {"reason": "Renovación de la prueba E9"}
                    )
                    check(res.status in (200, 204), f"revocar una clave anterior de {email} → HTTP {res.status}")
                    revoked += 1
        self.keys["admin-platform"] = dev_api_key(self.args.admin_email)
        self.keys["other"] = dev_api_key(OTHER_ORG_USER)
        for who in ("a", "b", "c", "admin"):  # el handshake da las URL del MCP, como en /baas-init
            res = http(
                "POST",
                f"{self.args.app_url}/api/v1/plugin/handshake",
                headers={"Authorization": f"Bearer {self.keys[who]}"},
                payload={},
            )
            check(res.status == 200, f"handshake de {USERS[who][0]} → HTTP {res.status}")
            orgs = {o["slug"]: o for o in (res.json() or {}).get("organizations", [])}
            check(list(orgs) == [ORG], f"{USERS[who][0]} debería ver solo «{ORG}»: {sorted(orgs)}")
            self.mcp_urls[ORG] = str(orgs[ORG]["mcp_url"])
            if who == "a":
                check(orgs[ORG]["functional_roles"] == [ROLE_A], f"handshake de A: {orgs[ORG]['functional_roles']}")
        res = http(
            "POST",
            f"{self.args.app_url}/api/v1/plugin/handshake",
            headers={"Authorization": f"Bearer {self.keys['other']}"},
            payload={},
        )
        check(res.status == 200, f"handshake de {OTHER_ORG_USER} → HTTP {res.status}")
        other = {o["slug"]: o for o in (res.json() or {}).get("organizations", [])}
        check(
            OTHER_ORG in other and ORG not in other,
            f"{OTHER_ORG_USER} debería ver «{OTHER_ORG}» y no «{ORG}»: {sorted(other)}",
        )
        self.mcp_urls[OTHER_ORG] = str(other[OTHER_ORG]["mcp_url"])
        return (
            f"claves «{label}» para {len(USERS)} usuarios ({revoked} anterior(es) revocada(s)); "
            "URL del MCP por handshake"
        )

    # 6
    def compose(self) -> str:
        out: list[str] = []
        for _ in range(20):  # la membresía y la clave recién escritas llegan al MCP por Redis
            try:
                with self.session("admin") as admin:
                    ok, who = admin.call("baas_whoami")
                if ok and who["organization"]["governance_role"] == "ORG_ADMIN":
                    break
            except (AuthError, McpToolError):
                pass
            time.sleep(0.5)
        else:
            raise StepError("el MCP no ve a e9-admin como ORG_ADMIN tras 10 s")
        with self.session("admin") as admin:
            ok, listed = admin.call("baas_list_skills")  # componer-skill §2: no duplicar skills
            check(ok, f"baas_list_skills → {listed}")
            for path, text in CONTENT.items():
                kind = "skill" if path.endswith("SKILL.md") else "knowledge"
                if path.endswith(".md"):
                    errors = header_errors(text, kind)
                    check(not errors, f"{path}: cabecera no válida: {'; '.join(errors)}")
                out.append(self.ensure_file(admin, path, text))
            # componer-conocimiento §4: se comprueba con la búsqueda que aparece
            for path, mark in ((DOC_A, MARK_A), (DOC_B, MARK_B), (DOC_ADMIN, MARK_ADMIN)):
                ok, found = admin.call("baas_search_knowledge", query=mark)
                check(
                    ok and path in {r["path"] for r in found["results"]}, f"{path} no aparece en la búsqueda: {found}"
                )
            ok, skills = admin.call("baas_list_skills")
            names = {s["name"] for s in skills["skills"]}
            check({SKILL_A, SKILL_B} <= names, f"faltan skills en baas_list_skills: {sorted(names)}")
        return "; ".join(out)

    def ensure_file(self, admin: McpSession, path: str, text: str) -> str:
        ok, current = admin.call("baas_read_document", path=path)
        if ok:
            if current["content"] == text:
                return f"{path}: sin cambios"
            ok, wrote = admin.call("baas_write_document", path=path, content=text, expected_sha256=current["sha256"])
            action = "actualizado"
        else:
            check(error_code(current) == "NOT_FOUND", f"leer {path} → {current}")
            if path.endswith(".md") and not path.endswith(
                "SKILL.md"
            ):  # componer-conocimiento §2: buscar antes de crear
                title = re.search(r"^title: (.+)$", text, re.MULTILINE)
                ok, found = admin.call(
                    "baas_search_knowledge", query=title.group(1) if title else path, type="knowledge"
                )
                same = [
                    r["path"]
                    for r in (found.get("results") or [])
                    if r["title"] == (title.group(1) if title else "") and r["path"] != path
                ]
                check(not same, f"ya hay un documento con el mismo título: {same}")
            ok, wrote = admin.call("baas_write_document", path=path, content=text)
            action = "creado"
            self.created.append(path)
        check(ok, f"escribir {path} → {wrote}")
        check(wrote["sha256"] == sha256(text), f"{path}: el sha256 escrito no coincide con el enviado")
        return f"{path}: {action}"

    # 7: controles positivos
    def positive(self) -> str:
        every = A_ONLY | B_ONLY | RESERVED
        for label in ("admin", "admin-platform"):
            with self.session(label) as s:
                for path in sorted(every):
                    ok, doc = s.call("baas_read_document", path=path)
                    check(ok and doc["sha256"] == sha256(CONTENT[path]), f"{label} no lee bien {path}: {doc}")
                ok, listed = s.call("baas_list_skills")
                check({SKILL_A, SKILL_B} <= {x["name"] for x in listed["skills"]}, f"{label}: faltan skills")
                ok, found = s.call("baas_search_knowledge", query=MARK_B)
                check(DOC_B in {r["path"] for r in found["results"]}, f"{label} no encuentra {DOC_B}")
        return "ORG_ADMIN y PLATFORM_ADMIN leen los 7 ficheros, ven las dos skills y encuentran ambos documentos"

    # 8-9: un rol frente al otro
    def role_checks(self, label: str, me: Side, other: Side) -> str:
        email = USERS[label][0]
        problems: list[str] = []
        forbidden_text = [other.mark, other.body, other.title]

        def need(condition: object, message: str) -> None:
            if not condition:
                problems.append(message)

        with self.session(label) as s:
            ok, who = s.call("baas_whoami")
            need(ok and who["organization"]["functional_roles"] == [me.role], f"whoami: {who.get('organization')}")
            need(ok and who["organization"]["governance_role"] == "USER", "no es USER")
            # lo suyo: busca, lee y obtiene la skill y su script
            ok, found = s.call("baas_search_knowledge", query=me.mark)
            need(ok and me.doc in {r["path"] for r in found["results"]}, f"su búsqueda no encuentra {me.doc}")
            ok, doc = s.call("baas_read_document", path=me.doc)
            need(ok and me.mark in doc["content"] and doc["sha256"] == sha256(CONTENT[me.doc]), f"no lee {me.doc}")
            ok, listed = s.call("baas_list_skills")
            names = {x["name"] for x in listed["skills"]} if ok else set()
            need(me.skill in names and other.skill not in names, f"baas_list_skills: {sorted(names)}")
            need(not leaks(listed, other.paths, forbidden_text), f"baas_list_skills deja ver lo del otro rol: {listed}")
            ok, skill = s.call("baas_get_skill", skill=me.skill)
            need(ok and skill["roles"] == [me.role], f"baas_get_skill de lo suyo: {skill}")
            script_sha = next((x["sha256"] for x in (skill.get("scripts") or []) if x["name"] == me.script), None)
            ok, script = s.call("baas_get_script", skill=me.skill, script=me.script)
            need(ok and script["sha256"] == script_sha == sha256(CONTENT[me.script_path]), "su script no coincide")
            # la búsqueda no devuelve ni deja ver trozos de lo del otro rol, con varias consultas y filtros
            queries: list[dict[str, Any]] = [
                {"query": other.mark},
                {"query": other.body},
                {"query": other.title},
                {"query": other.doc},
                {"query": f"{other.mark} {other.body}", "limit": 50},
                {"query": "factura citas protocolo criterios", "limit": 50},
                {"query": other.mark, "type": "skill"},
                {"query": other.title, "area": other.doc.split("/")[1]},
                {"query": f"{other.skill} {other.script}", "type": "script"},
            ]
            for q in queries:
                ok, res = s.call("baas_search_knowledge", **q)
                need(ok, f"búsqueda {q} falló: {res}")
                bad = leaks(res, other.paths, forbidden_text)
                need(not bad, f"la búsqueda {q} deja ver lo del otro rol: {bad}")
            # lo del otro rol, por su ruta: AUTHORIZATION_DENIED con los roles que hacen falta
            denied: list[tuple[str, dict[str, Any], str, list[str]]] = [
                ("baas_read_document", {"path": other.doc}, other.doc, [other.role]),
                ("baas_get_skill", {"skill": other.skill}, other.skill_path, [other.role]),
                (
                    "baas_get_script",
                    {"skill": other.skill, "script": other.script},
                    other.script_path,
                    [other.role],
                ),
                ("baas_read_document", {"path": DOC_ADMIN}, DOC_ADMIN, []),
            ]
            for tool, args, resource, roles in denied:
                ok, err = s.call(tool, **args)
                need(not ok and error_code(err) == "AUTHORIZATION_DENIED", f"{tool} {resource}: {err}")
                need(
                    err.get("resource") == resource and err.get("required_roles") == roles,
                    f"{tool}: resource/required_roles = {err.get('resource')}/{err.get('required_roles')}",
                )
                need(not leaks(err, other.paths, forbidden_text), f"el error de {tool} deja ver contenido: {err}")
            # seguir la cita de su skill: la ruta está en el texto y aun así se deniega
            cites = cited_paths(skill.get("content", "")) if isinstance(skill, dict) else []
            need(other.doc in cites, f"la skill {me.skill} no cita {other.doc}: {cites}")
            ok, err = s.call("baas_read_document", path=other.doc)
            need(
                not ok and error_code(err) == "AUTHORIZATION_DENIED" and err.get("required_roles") == [other.role],
                f"seguir la cita a {other.doc}: {err}",
            )
            # escribir: no es administrador, y nada cambia
            attempt = "---\ntitle: Pisado\ntype: knowledge\nroles: []\nstatus: active\n---\nNo debería escribirse.\n"
            ok, err = s.call("baas_write_document", path=me.doc, content=attempt)
            need(
                not ok and error_code(err) == "AUTHORIZATION_DENIED" and err.get("required_roles") == ADMIN_ROLES,
                f"escribir como USER: {err}",
            )
        with self.session("admin") as admin:
            ok, doc = admin.call("baas_read_document", path=me.doc)
            need(ok and doc["sha256"] == sha256(CONTENT[me.doc]), "el intento de escritura cambió el documento")
        check(not problems, f"{email} (rol {me.role}):\n  " + "\n  ".join(problems))
        return (
            f"{email}: busca, lee y obtiene lo suyo; {len(queries)} búsquedas sin rastro del rol {other.role}; "
            f"{len(denied)} lecturas y la cita → AUTHORIZATION_DENIED (required_roles [{other.role}]); no escribe"
        )

    def role_a(self) -> str:
        return self.role_checks("a", SIDE_A, SIDE_B)

    def role_b(self) -> str:
        return self.role_checks("b", SIDE_B, SIDE_A)

    # 10
    def no_role(self) -> str:
        problems: list[str] = []
        everything = A_ONLY | B_ONLY | RESERVED
        forbidden_text = [MARK_A, MARK_B, MARK_ADMIN, BODY_A, BODY_B, TITLE_A, TITLE_B]
        with self.session("c") as s:
            ok, who = s.call("baas_whoami")
            if not (
                ok
                and who["organization"]["functional_roles"] == []
                and who["organization"]["governance_role"] == "USER"
            ):
                problems.append(f"whoami: {who.get('organization')}")
            ok, listed = s.call("baas_list_skills")
            if not ok or listed["skills"]:
                problems.append(f"baas_list_skills debería estar vacío: {listed}")
            for q in (MARK_A, MARK_B, MARK_ADMIN, BODY_A, BODY_B, "citas facturación protocolo criterios reservado"):
                ok, res = s.call("baas_search_knowledge", query=q, limit=50)
                if not ok or res["results"] or leaks(res, everything, forbidden_text):
                    problems.append(f"la búsqueda «{q}» debería estar vacía: {res}")
            for path in sorted(everything):
                ok, err = s.call("baas_read_document", path=path)
                if ok or error_code(err) != "AUTHORIZATION_DENIED":
                    problems.append(f"leer {path}: {err}")
            for skill in (SKILL_A, SKILL_B):
                ok, err = s.call("baas_get_skill", skill=skill)
                if ok or error_code(err) != "AUTHORIZATION_DENIED":
                    problems.append(f"baas_get_skill {skill}: {err}")
        check(not problems, "USER sin rol funcional:\n  " + "\n  ".join(problems))
        return (
            f"{USERS['c'][0]}: lista de skills vacía, 6 búsquedas vacías, "
            f"{len(everything)} lecturas y 2 skills → AUTHORIZATION_DENIED"
        )

    # 11
    def other_org(self) -> str:
        problems: list[str] = []
        key = self.keys["other"]
        # por la ruta de la organización: HTTP 404 NOT_FOUND, indistinguible de «no existe»
        url = self.mcp_urls[OTHER_ORG].replace(f"/org/{OTHER_ORG}/", f"/org/{ORG}/")
        try:
            with McpClient(url, key, timeout=self.args.timeout):
                problems.append("el MCP aceptó la conexión a /org/e9-prueba/mcp")
            self.evidence.add(OTHER_ORG_USER, "initialize", {"url": f"/org/{ORG}/mcp"}, response="aceptado")
        except McpToolError as exc:
            self.evidence.add(
                OTHER_ORG_USER, "initialize", {"url": f"/org/{ORG}/mcp"}, error={"http": 404, "error": exc.code}
            )
            if exc.code != "NOT_FOUND":
                problems.append(f"conectar a la ruta de {ORG}: {exc.code}")
        # con el parámetro `org` desde su propia organización: NOT_FOUND, no AUTHORIZATION_DENIED
        with McpSession(OTHER_ORG_USER, self.mcp_urls[OTHER_ORG], key, self.evidence, self.args.timeout) as s:
            for tool, args in (
                ("baas_read_document", {"path": DOC_A}),
                ("baas_search_knowledge", {"query": MARK_A}),
                ("baas_list_skills", {}),
                ("baas_get_skill", {"skill": SKILL_A}),
                ("baas_whoami", {}),
            ):
                ok, err = s.call(tool, org=ORG, **args)
                if ok or error_code(err) != "NOT_FOUND" or err.get("resource") != f"org:{ORG}":
                    problems.append(f"{tool} con org={ORG}: {err}")
            ok, res = s.call("baas_search_knowledge", query=MARK_A)  # en su organización no hay nada de e9-prueba
            if not ok or leaks(res, A_ONLY | B_ONLY | RESERVED, [MARK_A, MARK_B]):
                problems.append(f"su búsqueda en «{OTHER_ORG}»: {res}")
        check(not problems, "usuario de otra organización:\n  " + "\n  ".join(problems))
        return (
            f"{OTHER_ORG_USER}: la ruta de {ORG} → 404 NOT_FOUND; con org={ORG} → NOT_FOUND (org:{ORG}) "
            "en 5 tools; nada en su búsqueda"
        )

    def run(self, runner: Runner) -> None:
        runner.step("0. la pila responde", self.stack_up)
        s1 = "1. sesión del PLATFORM_ADMIN"
        runner.step(s1, self.admin_login, needs=["0. la pila responde"])
        s2, s3, s4, s5, s6 = (
            "2. organización e9-prueba",
            "3. roles funcionales recepcion y facturacion",
            "4. miembros y activación",
            "5. API keys y handshake",
            "6. contenido por MCP (componer-*)",
        )
        runner.step(s2, self.ensure_org, needs=[s1])
        runner.step(s3, self.ensure_roles, needs=[s2])
        runner.step(s4, self.ensure_members, needs=[s3])
        runner.step(s5, self.make_keys, needs=[s4])
        runner.step(s6, self.compose, needs=[s5])
        runner.step("7. ORG_ADMIN y PLATFORM_ADMIN ven todo", self.positive, needs=[s6])
        runner.step("8. rol recepcion: lo suyo sí, lo de facturacion no", self.role_a, needs=[s6])
        runner.step("9. rol facturacion: lo suyo sí, lo de recepcion no", self.role_b, needs=[s6])
        runner.step("10. USER sin rol funcional: nada", self.no_role, needs=[s6])
        runner.step("11. usuario de otra organización: nada", self.other_org, needs=[s6])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--app-url",
        default=os.environ.get("BAAS_E2E_APP_URL", "http://app.localhost:28080"),
        help="URL pública de la webapp (la del MCP sale del handshake)",
    )
    p.add_argument("--admin-email", default="admin@baas.test", help="PLATFORM_ADMIN de desarrollo (contrato §8)")
    p.add_argument("--password", default=DEV_PASSWORD, help="Contraseña de los usuarios de desarrollo y de los nuevos")
    p.add_argument("--timeout", type=float, default=30, help="Segundos de espera de cada llamada MCP")
    p.add_argument("--evidence", type=Path, help="Escribe aquí la evidencia (llamada → respuesta) en JSON")
    p.add_argument("--json", type=Path, help="Escribe aquí el informe de pasos en JSON")
    ns = p.parse_args(argv)
    ns.app_url = ns.app_url.rstrip("/")
    return ns


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runner, e9 = Runner(), E9(args)
    print(f"Prueba E9 de roles funcionales · webapp {args.app_url} · organización {ORG}")
    e9.run(runner)
    report = {**runner.report(), "app_url": args.app_url, "created": e9.created}
    if args.evidence:
        text = json.dumps({**report, "calls": e9.evidence.calls}, indent=2, ensure_ascii=False) + "\n"
        check(not SECRET_RE.search(text), "la evidencia contiene algo que parece un secreto: no se escribe")
        args.evidence.write_text(text)
        print(f"Evidencia: {args.evidence} ({len(e9.evidence.calls)} llamadas)")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    failed = [r.name for r in runner.results if r.status != "OK"]
    print("Creado en esta ejecución:", ", ".join(e9.created) if e9.created else "nada (todo existía)")
    print("RESULTADO:", "todo OK" if not failed else f"{len(failed)} paso(s) sin éxito: {', '.join(failed)}")
    return runner.exit_code()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
    sys.exit(130)
