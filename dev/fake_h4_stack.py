#!/usr/bin/env python3
"""Pila H4 simulada para probar `dev/e2e_h4.py` sin la pila real: webapp, proxy, MCP y proveedor con eco.

Amplía la de H3 (`dev/fake_h3_stack.py`, que no se toca: se importan su proveedor, su vault y sus utilidades) con lo
que el hito H4 añade (contrato §4, §5.2, §6 y §8; plan, tareas W2 a W5, P2, P4, M3 y D1):

- **Proxy** con el anonimizador por conversación: con AI-IP activo y el anonimizador apagado en el primer turno, el
  cuerpo sale tal como entró, sin tokens, con `WARNING` y `residual_matches`; cada conversación conserva el valor con
  el que empezó en los dos sentidos. Una organización bloqueada responde `403 baas_access_blocked` y una eliminada,
  `403 baas_access_denied`.
- **Webapp**: `PUT /orgs/{slug}/anonymizer`, crear, bloquear, activar AI-IP, invitar miembros (usuarios nuevos con
  enlace de activación, `POST /auth/activation/{token}/password`, claves personales `/api-keys`, `GET /me`),
  `POST /orgs/{slug}/delete` (con sus errores), la auditoría, la vista de verificación del PLATFORM_ADMIN sin contenido
  (`content_available`) y el WebSocket `/api/v1/ws/traces` con el mismo filtrado.
- **Usuarios que se quedan sin organización** (contrato §6): `GET /orgs/{slug}/delete-preview`, `disable_orphans` al
  eliminar (desactiva, revoca claves personales, cierra sesiones, invalida enlaces y audita `user.disabled` sin email),
  `409 USER_DISABLED` para un ORG_ADMIN y la reactivación por el PLATFORM_ADMIN (`user.reactivated`, con la
  organización como `org_id` y `{org_slug, new_status}`); en la auditoría el ORG_ADMIN ve las filas de sus
  organizaciones (`user.reactivated`) y no las `user.disabled` (sin organización).
- **MCP** (`/org/<slug>/mcp`): `initialize` y `baas_write_document`; una organización eliminada pasa a
  `.quarantine/<slug>-<fecha>`.
- **Las dos instancias de Redis** (H4-02): la persistente (AOF) y la efímera (`redis-ephemeral`, sin ficheros). El proxy
  escribe en la efímera lo que lleva valores originales: la traza de cada turno (que la webapp consume en el acto) y
  la copia caliente del vault. Cada instancia guarda lo que tendría en memoria y en los ficheros de `/data`.
- `FakeInspector`: lo que `ContainerInspector` mira dentro de los contenedores (vault, Redis, discos y MCP).

`faults` inyecta los fallos que la prueba tiene que detectar (`FAULTS`).

Uso manual (imprime las URLs y el comando de la prueba):

    python3 dev/fake_h4_stack.py [--fault leak]

Solo biblioteca estándar. No es una implementación de referencia: es un doble de pruebas.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import queue
import re
import secrets
import select
import struct
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_h3_stack as h3s
from fake_baas import dev_api_key
from fake_h3_stack import (
    DEV_PASSWORD,
    ROUTES,
    EchoProvider,
    Rehydrator,
    Vault,
    _Handler,
    _Server,
    anonymize,
    encode,
    error_body,
    fingerprint,
    last_user_text,
    provider_handler,
    response_id_of,
)

META_ORG = "baas-ai-ip"
PERSISTENT, EPHEMERAL = "redis", "redis-ephemeral"  # los servicios de Redis (contrato §1, H4-02)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BLOCKED_MESSAGE = "Acceso bloqueado por BaaS-AI-IP: la organización está bloqueada por la plataforma"
DENIED_MESSAGE = "Acceso bloqueado por BaaS-AI-IP: la organización no existe o no eres miembro"
_SEVERITY = {None: 0, "CLEAN": 1, "WARNING": 2, "ERROR": 3}

FAULTS = {
    # modo observación y estado por conversación (proxy)
    "observe-rewrites": "con el anonimizador apagado, el proxy altera el cuerpo antes de reenviarlo",
    "observe-anonymizes": "con el anonimizador apagado, el proxy sustituye igualmente los valores",
    "observe-clean": "los turnos en observación salen CLEAN y sin coincidencias residuales",
    "state-follows-switch": "una conversación abierta adopta el valor actual del interruptor (no conserva el suyo)",
    "toggle-no-propagation": "el proxy no se entera de los cambios del interruptor",
    "no-ratchet": "una conversación abierta en claro no sube al encender el anonimizador (como antes de H4-08)",
    "ratchet-lowers": "apagar el anonimizador baja una conversación que ya estaba anonimizada",
    "no-protected-from": "la webapp no informa de protected_from_turn",
    "admin-sees-pre-raise": "el PLATFORM_ADMIN ve el texto de los turnos que salieron sin anonimizar",
    "admin-hides-raised": "el PLATFORM_ADMIN no ve los turnos que ya salieron anonimizados",
    # lo que expone la webapp
    "no-cause": "la webapp no dice con qué anonimizador empezó cada conversación",
    "admin-sees-content-api": "el PLATFORM_ADMIN recibe el texto enviado y recibido en los turnos",
    "admin-detail-leak": "el detalle de la conversación del PLATFORM_ADMIN trae el texto de sus turnos",
    "admin-reveal": "el PLATFORM_ADMIN puede revelar los originales de otra organización",
    "ws-leak": "el WebSocket entrega el contenido al PLATFORM_ADMIN",
    "ws-silent": "el WebSocket no entrega ninguna trama al PLATFORM_ADMIN",
    "admin-sees-bypass": "con AI-IP apagado, el PLATFORM_ADMIN ve el texto de la conversación",
    # interruptor
    "toggle-no-reason": "el interruptor acepta un cambio sin motivo",
    "toggle-user-allowed": "un USER puede cambiar el interruptor",
    "toggle-foreign-403": "un ORG_ADMIN ajeno recibe 403 en vez de 404 al cambiar el interruptor",
    "toggle-ai-ip-off-allowed": "con AI-IP apagado, el interruptor se cambia en vez de responder 409",
    "toggle-not-applied": "el interruptor responde 200 pero no cambia nada",
    # eliminar una organización
    "delete-no-block-check": "se puede eliminar una organización sin bloquear",
    "delete-no-slug-check": "se elimina aunque el slug de confirmación no coincida",
    "delete-no-reauth": "se elimina sin contraseña o con una incorrecta (sin reautenticar al PLATFORM_ADMIN)",
    "delete-meta-allowed": "se puede eliminar la organización meta",
    "delete-orgadmin-allowed": "un ORG_ADMIN puede eliminar su organización",
    "delete-twice-ok": "eliminar dos veces responde 200 la segunda",
    "slug-reusable": "el slug de una organización eliminada se puede volver a crear",
    "proxy-still-serves": "el proxy sigue sirviendo la organización eliminada",
    "mcp-still-serves": "el MCP sigue sirviendo la organización eliminada",
    "block-not-applied": "el proxy ignora el bloqueo de la organización",
    "vault-kept": "el vault conserva las entradas de la organización eliminada",
    "redis-kept": "Redis conserva las claves de la organización eliminada",
    "ephemeral-keys-kept": "la efímera conserva las claves de las conversaciones de la organización eliminada",
    # los originales y el disco de Redis (H4-02)
    "originals-in-aof": "el proxy escribe las trazas y la copia caliente del vault en la instancia persistente (AOF)",
    "traces-in-aof": "el proxy publica las trazas en la persistente (el vault caliente va bien a la efímera)",
    "ephemeral-persists": "la instancia efímera tiene appendonly yes y escribe ficheros en /data",
    "scanner-blind": "el escáner de disco no lee los ficheros binarios de la persistente (la base reescrita del AOF)",
    "no-ephemeral": "la pila no tiene redis-ephemeral (antigua): los originales siguen llegando al AOF",
    "no-quarantine": "el directorio del MCP no pasa a cuarentena",
    "audit-missing": "la eliminación no deja rastro en la auditoría",
    "delete-removes-users": "al eliminar una organización se elimina también a sus usuarios",
    # usuarios que se quedan sin organización
    "preview-lists-all": "la vista previa lista a todos los miembros, no solo a quien se quedaría sin organización",
    "preview-open": "la vista previa de la eliminación la puede pedir un ORG_ADMIN o un USER de la organización",
    "preview-needs-block": "la vista previa exige que la organización esté bloqueada (409 ORG_NOT_BLOCKED)",
    "orphans-kept": "al eliminar con disable_orphans: true no se desactiva a nadie",
    "orphans-ignore-flag": "con disable_orphans: false se desactiva igualmente",
    "orphans-default-off": "sin el campo disable_orphans no se desactiva a nadie (el valor por omisión es false)",
    "orphans-other-org": "también se desactiva a quien conserva otra organización",
    "orphans-invited-skipped": "los invitados que no activaron la cuenta no se desactivan",
    "orphans-keys-kept": "se desactiva al usuario pero sus claves personales siguen vigentes",
    "orphans-sessions-kept": "se desactiva al usuario pero sus sesiones siguen abiertas",
    "orphans-links-kept": "los enlaces de activación sin usar del usuario desactivado no se invalidan",
    "orphans-audit-missing": "desactivar a un usuario no deja ninguna fila user.disabled",
    "orphans-audit-email": "la fila user.disabled lleva el email y el nombre del usuario",
    "disabled-can-login": "un usuario desactivado puede iniciar sesión",
    "reactivate-orgadmin": "un ORG_ADMIN puede reactivar a un usuario desactivado (sin 409 USER_DISABLED)",
    "reactivate-no-audit": "reactivar a un usuario no deja ninguna fila user.reactivated",
    "reactivate-restores-keys": "al reactivar a un usuario vuelven a valer sus claves revocadas",
    "reactivate-audit-no-org": "la fila user.reactivated no lleva la organización a la que se invita (org_id nulo)",
    "audit-orgadmin-sees-disabled": "el ORG_ADMIN de la organización ve las filas user.disabled de sus miembros",
    "audit-orgadmin-blind": "el ORG_ADMIN no ve ninguna fila de la auditoría de su organización",
}


class FakeRedis:
    """Una instancia de Redis simulada: sus parámetros, lo que hay en memoria y los ficheros de `/data`.

    Con AOF cada escritura se anota en el fichero vivo (`incr`) y `rewrite` lo sustituye por una base con lo que hay en
    memoria, como `BGREWRITEAOF`: lo borrado desaparece de los ficheros y lo que sigue vivo se queda.
    """

    def __init__(self, appendonly: bool) -> None:
        self.settings = {"appendonly": "yes" if appendonly else "no", "save": "", "dir": "/data"}
        self.live: dict[str, str] = {}
        self.incr = ""
        self.base: str | None = "" if appendonly else None

    @property
    def persists(self) -> bool:
        return self.settings["appendonly"] == "yes"

    def set(self, key: str, value: str) -> None:
        self.live[key] = value
        if self.persists:
            self.incr += f"SET {key} {value}\r\n"

    def delete(self, key: str) -> None:
        if self.live.pop(key, None) is not None and self.persists:
            self.incr += f"DEL {key}\r\n"

    def rewrite(self) -> None:
        if self.persists:
            self.base = "".join(f"{key}\x00{value}\x00" for key, value in self.live.items())
            self.incr = ""

    def files(self) -> list[str]:
        if not self.persists:
            return []
        names = ["appendonly.aof.1.incr.aof", "appendonly.aof.manifest"] + (
            ["appendonly.aof.1.base.rdb"] if self.base is not None else []
        )
        return sorted(f"/data/appendonlydir/{name}" for name in names)

    def text(self, *, binary: bool = True) -> str:
        """Lo que hay en los ficheros; `binary=False` deja fuera la base (el preámbulo RDB)."""
        return (self.base or "" if binary else "") + self.incr


# --- modelo -----------------------------------------------------------------------------------------


@dataclass
class Org:
    slug: str
    name: str
    ai_ip: bool = False
    anonymizer: bool = True
    blocked: bool = False


@dataclass
class Key:
    """Una clave personal (`POST /api-keys`)."""

    id: str
    label: str
    secret: str
    revoked: bool = False


@dataclass
class Link:
    """Un enlace de activación: `USED` si ya se usó, `REVOKED` si se invalidó o su usuario está desactivado."""

    token: str
    used: bool = False
    revoked: bool = False


@dataclass
class User:
    email: str
    memberships: dict[str, str]  # slug → ORG_ADMIN · USER
    platform_admin: bool = False
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    status: str = "ACTIVE"  # ACTIVE · INVITED · DISABLED
    password: str | None = DEV_PASSWORD
    keys: list[Key] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = self.name or self.email.split("@")[0]


@dataclass
class Conv:
    id: str
    org: str
    user: User
    api: str
    model: str
    ai_ip: bool
    anonymizer: bool
    vault: Vault = field(default_factory=Vault)
    turns: list[dict[str, Any]] = field(default_factory=list)
    verification_status: str | None = None
    # H4-08: `ai_ip` y `anonymizer` son los del INICIO; los `now_*` son los del proxy ahora (solo suben).
    protected_from_turn: int | None = None
    now_ai_ip: bool | None = None
    now_anonymizer: bool | None = None

    def __post_init__(self) -> None:
        if self.now_ai_ip is None:
            self.now_ai_ip = self.ai_ip
        if self.now_anonymizer is None:
            self.now_anonymizer = self.anonymizer


@dataclass
class Subscriber:
    user: User
    org: str | None
    messages: queue.Queue[str] = field(default_factory=queue.Queue)


def segments(text: str) -> list[dict[str, str]]:
    """Texto partido en trozos de texto y tokens (como la vista de turno de la webapp)."""
    out, pos = [], 0
    for match in h3s.TOKEN_RE.finditer(text):
        if match.start() > pos:
            out.append({"text": text[pos : match.start()]})
        out.append({"token": match.group(0)})
        pos = match.end()
    if pos < len(text):
        out.append({"text": text[pos:]})
    return out


def ws_frame(opcode: int, payload: bytes = b"") -> bytes:
    """Trama del servidor (sin enmascarar)."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    head += (
        bytes([n])
        if n < 126
        else bytes([126]) + struct.pack(">H", n)
        if n < 1 << 16
        else bytes([127]) + struct.pack(">Q", n)
    )
    return head + payload


class StackState:
    def __init__(self, faults: set[str], provider_url: str) -> None:
        self.faults = faults
        self.provider_url = provider_url
        self.proxy_url = self.app_url = ""
        self.lock = threading.RLock()
        self.orgs: dict[str, Org] = {
            META_ORG: Org(META_ORG, "BaaS-AI-IP (meta)"),
            "demo": Org("demo", "Demo Clínica"),
            "observacion": Org("observacion", "Demo Observación", ai_ip=True, anonymizer=False),
            "privada": Org("privada", "Demo Privacidad", ai_ip=True, anonymizer=True),
        }
        users = [
            User("admin@baas.test", {META_ORG: "ORG_ADMIN"}, platform_admin=True),
            User("orgadmin@demo.test", {"demo": "ORG_ADMIN"}),
            User("analista@demo.test", {"demo": "USER"}),
            User("observador@observacion.test", {"observacion": "USER"}),
            User("analista@privada.test", {"privada": "USER"}),
        ]
        self.users = {u.email: u for u in users}
        self.users_by_id = {u.id: u for u in users}
        self.by_key = {dev_api_key(u.email): u for u in users}
        self.sessions: dict[str, User] = {}
        self.links: dict[str, tuple[User, Link]] = {}  # token → (usuario, enlace de activación)
        self.deleted_slugs: set[str] = set()
        self.ghosts: dict[str, Org] = {}  # lo que el proxy o el MCP siguen viendo con un fallo inyectado
        self.proxy_anonymizer: dict[str, bool] = {s: o.anonymizer for s, o in self.orgs.items()}
        self.convs: dict[str, Conv] = {}
        self.fingerprints: dict[str, str] = {}
        self.response_ids: dict[str, str] = {}
        self.subscribers: list[Subscriber] = []
        self.audit: list[dict[str, Any]] = []
        self.redis_keys: set[str] = set()  # instancia persistente: política y membresías
        self.redis = {PERSISTENT: FakeRedis(True), EPHEMERAL: FakeRedis("ephemeral-persists" in faults)}
        for user in users:
            for slug in user.memberships:
                self.redis_keys.add(f"baas:member:{user.id}:{slug}")
        self.redis_keys |= {f"baas:policy:org:{slug}" for slug in self.orgs}
        self.mcp_dirs: dict[str, dict[str, str]] = {}
        self.quarantine: list[str] = []

    @property
    def ephemeral_keys(self) -> set[str]:
        """Las claves de conversación de la instancia efímera (la copia caliente del vault)."""
        return {k for k in self.redis[EPHEMERAL].live if k.startswith("baas:conv:")}

    def originals_target(self, *, trace: bool = False) -> FakeRedis:
        """Dónde escribe el proxy lo que lleva valores originales (la copia caliente del vault y, con `trace`, la traza
        del turno): la efímera, salvo que falte o se escriba mal."""
        leak = {"originals-in-aof", "no-ephemeral"} | ({"traces-in-aof"} if trace else set())
        return self.redis[PERSISTENT if leak & self.faults else EPHEMERAL]

    # --- auditoría ---------------------------------------------------------------------------------
    def record(self, action: str, actor: User | None, *, org: str | None = None, target_type: str | None = None,
               target_id: str | None = None, reason: str | None = None,
               metadata: dict[str, Any] | None = None) -> None:  # fmt: skip
        """Una fila de auditoría con la forma de `GET /api/v1/audit` de la webapp: `target_label` de un usuario es su
        email (la webapp lo resuelve al leer, no se guarda en la fila)."""
        target = self.users_by_id.get(target_id or "") if target_type == "user" else None
        self.audit.append({
            "id": str(uuid.uuid4()), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": {"id": actor.id, "email": actor.email, "name": actor.name} if actor else None,
            "org_slug": org, "action": action, "source": "WEBAPP", "target_type": target_type,
            "target_id": target_id, "target_label": target.email if target else target_id, "reason": reason,
            "count": 1, "sensitive": False, "metadata": metadata or {},
        })  # fmt: skip

    # --- permisos ----------------------------------------------------------------------------------
    def can_see_org(self, user: User, slug: str) -> bool:
        return user.platform_admin or slug in user.memberships

    def can_view(self, viewer: User, conv: Conv) -> bool:
        return viewer.platform_admin or viewer.memberships.get(conv.org) == "ORG_ADMIN" or conv.user.id == viewer.id

    def content_mode(self, viewer: User, conv: Conv) -> str:
        """`OWN`: lee el contenido; `VERIFY`: vista de verificación (PLATFORM_ADMIN ajeno); `LOCKED`: sin contenido."""
        if conv.user.id == viewer.id or viewer.memberships.get(conv.org) == "ORG_ADMIN":
            return "OWN"
        return "VERIFY" if viewer.platform_admin and conv.org not in viewer.memberships else "LOCKED"

    def admin_content(self, conv: Conv) -> bool:
        """De la conversación: ¿hay algún turno que un PLATFORM_ADMIN ajeno pueda ver? Empezó anonimizada o ya subió."""
        if "admin-sees-content-api" in self.faults:
            return True
        if not conv.ai_ip and "admin-sees-bypass" in self.faults:
            return True
        return (conv.ai_ip and conv.anonymizer) or conv.protected_from_turn is not None

    def admin_content_turn(self, conv: Conv, turn: dict[str, Any]) -> bool:
        """De un turno (H4-08): solo si salió con AI-IP y anonimizador encendidos."""
        if "admin-sees-content-api" in self.faults:
            return True
        if not turn["ai_ip"] and "admin-sees-bypass" in self.faults:
            return True
        protected = bool(turn["ai_ip"] and turn["anonymizer"])
        if protected and "admin-hides-raised" in self.faults and conv.protected_from_turn is not None:
            return False
        if not protected and "admin-sees-pre-raise" in self.faults and conv.protected_from_turn is not None:
            return True
        return protected

    # --- organizaciones ------------------------------------------------------------------------------
    def org_json(self, org: Org) -> dict[str, Any]:
        return {"slug": org.slug, "name": org.name, "blocked": org.blocked, "paused": False,
                "ai_ip_enabled": org.ai_ip, "anonymizer_enabled": org.anonymizer, "retention_days": 30}  # fmt: skip

    def handshake(self, user: User) -> dict[str, Any]:
        orgs = []
        for slug, role in sorted(user.memberships.items()):
            org = self.orgs.get(slug)
            if org is None:
                continue
            orgs.append({
                "slug": slug, "name": org.name, "governance_role": role, "functional_roles": [],
                "paused": False, "blocked": org.blocked, "ai_ip_enabled": org.ai_ip,
                "mcp_url": f"{self.app_url}/org/{slug}/mcp",
                "proxy_anthropic_url": f"{self.proxy_url}/org/{slug}/anthropic",
                "proxy_openai_url": f"{self.proxy_url}/org/{slug}/openai/v1",
            })  # fmt: skip
        user_json = {
            "id": user.id,
            "email": user.email,
            "platform_role": "PLATFORM_ADMIN" if user.platform_admin else "USER",
        }
        return {"user": user_json, "global_kill_switch": False, "organizations": orgs}

    def create_org(self, actor: User, slug: str, name: str) -> tuple[int, dict[str, Any]]:
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?", slug):
            return 422, {"error": "INVALID_SLUG", "message": "Invalid slug"}
        with self.lock:
            reused = slug in self.deleted_slugs and "slug-reusable" in self.faults
            if (slug in self.orgs or slug in self.deleted_slugs) and not reused:
                return 409, {"error": "SLUG_TAKEN", "message": "That identifier is already in use"}
            self.deleted_slugs.discard(slug)
            org = Org(slug, name)
            self.orgs[slug] = org
            self.proxy_anonymizer[slug] = org.anonymizer
            self.redis_keys.add(f"baas:policy:org:{slug}")
            self.record("org.created", actor, org=slug, target_type="organization", target_id=slug)
        return 201, {"organization": self.org_json(org), "member": None, "activation_url": None, "propagated": True}

    def member_json(self, user: User, slug: str) -> dict[str, Any]:
        others = bool(set(user.memberships) - {slug})
        return {"user_id": user.id, "email": user.email, "name": user.name, "status": user.status,
                "governance_role": user.memberships.get(slug), "functional_roles": [],
                "is_platform_admin": user.platform_admin, "other_orgs": others}  # fmt: skip

    def new_link(self, user: User) -> str:
        token = secrets.token_urlsafe(24)
        link = Link(token)
        user.links.append(link)
        self.links[token] = (user, link)
        return token

    def invite(self, viewer: User, slug: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """`POST /orgs/{slug}/members`: un usuario nuevo nace `INVITED` con enlace; uno `DISABLED` solo lo reactiva un
        PLATFORM_ADMIN (`ACTIVE` si ya tenía contraseña, `INVITED` con otro enlace si no)."""
        email = str(body.get("email") or "").strip().lower()
        role = str(body.get("governance_role") or "USER")
        with self.lock:
            if slug not in self.orgs or not self.can_see_org(viewer, slug):
                return 404, {"error": "NOT_FOUND", "message": "Organization not found"}
            if not (viewer.platform_admin or viewer.memberships.get(slug) == "ORG_ADMIN"):
                return 403, {"error": "FORBIDDEN", "message": "Forbidden"}
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                return 422, {"error": "VALIDATION_ERROR", "message": "Invalid email", "field": "email"}
            user, previous = self.users.get(email), None
            if user is None:
                name = str(body.get("name") or "").strip() or email.split("@")[0]
                user = User(email, {}, name=name, status="INVITED", password=None)
                self.users[email] = user
                self.users_by_id[user.id] = user
            else:
                if slug in user.memberships:
                    return 409, {"error": "ALREADY_MEMBER", "message": "This user is already a member"}
                if user.status == "DISABLED":
                    if not viewer.platform_admin and "reactivate-orgadmin" not in self.faults:
                        return 409, {"error": "USER_DISABLED", "message": "This user is disabled"}
                    previous, user.status = "DISABLED", "ACTIVE" if user.password else "INVITED"
                    if "reactivate-restores-keys" in self.faults:
                        for key in user.keys:
                            key.revoked = False
                            self.by_key[key.secret] = user
                elif user.status == "INVITED" and user.memberships:
                    return 409, {"error": "USER_NOT_ACTIVE", "message": "The user has not activated the account"}
            user.memberships[slug] = role
            self.redis_keys.add(f"baas:member:{user.id}:{slug}")
            token = self.new_link(user) if user.status == "INVITED" else None
            self.record("member.invited", viewer, org=slug, target_type="user", target_id=user.id,
                        metadata={"email": email, "governance_role": role, "functional_roles": [],
                                  "new_user": previous is None and token is not None,
                                  "activation_link_issued": token is not None})  # fmt: skip
            if previous and "reactivate-no-audit" not in self.faults:
                # `org_id` = la organización a la que se invita: su ORG_ADMIN ve la fila
                org_of_row = None if "reactivate-audit-no-org" in self.faults else slug
                self.record("user.reactivated", viewer, org=org_of_row, target_type="user", target_id=user.id,
                            reason=str(body.get("reason") or "") or None,
                            metadata={"org_slug": slug, "new_status": user.status})  # fmt: skip
            url = f"{self.app_url}/activate/{token}" if token else None
            return 201, {"member": self.member_json(user, slug), "activation_url": url, "propagated": True}

    # --- usuarios que se quedan sin organización ---------------------------------------------------------
    def orphan_candidates(self, slug: str, *, preview: bool = False) -> list[User]:
        """Miembros que no son PLATFORM_ADMIN, no tienen otra membresía y no están `DISABLED` (en orden de `id`)."""
        out = []
        for user in sorted(self.users.values(), key=lambda u: u.id):
            if slug not in user.memberships:
                continue
            if preview and "preview-lists-all" in self.faults:
                out.append(user)
                continue
            if user.platform_admin or user.status == "DISABLED":
                continue
            if set(user.memberships) - {slug} and (preview or "orphans-other-org" not in self.faults):
                continue
            if user.status == "INVITED" and "orphans-invited-skipped" in self.faults:
                continue
            out.append(user)
        return out

    def delete_preview(self, viewer: User, slug: str) -> tuple[int, dict[str, Any]]:
        with self.lock:
            org = self.orgs.get(slug)
            if org is None or not self.can_see_org(viewer, slug):
                return 404, {"error": "NOT_FOUND", "message": "Organization not found"}
            if not viewer.platform_admin and "preview-open" not in self.faults:
                return 403, {"error": "FORBIDDEN", "message": "Only the platform administrator can see this"}
            if "preview-needs-block" in self.faults and not org.blocked:
                return 409, {"error": "ORG_NOT_BLOCKED", "message": "Block the organization first"}
            orphans = [{"id": u.id, "email": u.email, "name": u.name, "status": u.status,
                        "personal_keys": len([k for k in u.keys if not k.revoked])}
                       for u in self.orphan_candidates(slug, preview=True)]  # fmt: skip
            return 200, {"members": len([u for u in self.users.values() if slug in u.memberships]),
                         "conversations": len([c for c in self.convs.values() if c.org == slug]),
                         "orphans": orphans}  # fmt: skip

    def disable_user(self, user: User, actor: User, reason: str, slug: str) -> None:
        """Lo que la eliminación hace con cada usuario que se queda sin organización."""
        previous, revoked = user.status, 0
        user.status = "DISABLED"
        if "orphans-keys-kept" not in self.faults:
            for key in user.keys:
                if not key.revoked:
                    key.revoked, revoked = True, revoked + 1
                    self.by_key.pop(key.secret, None)
        if "orphans-sessions-kept" not in self.faults:
            self.sessions = {t: u for t, u in self.sessions.items() if u is not user}
        if "orphans-links-kept" not in self.faults:
            for link in user.links:
                link.revoked = True
        if "orphans-audit-missing" in self.faults:
            return
        meta: dict[str, Any] = {"cause": "org.deleted", "org_slug": slug, "previous_status": previous,
                                "keys_revoked": revoked}  # fmt: skip
        if "orphans-audit-email" in self.faults:
            meta |= {"email": user.email, "name": user.name}
        self.record("user.disabled", actor, target_type="user", target_id=user.id, reason=reason, metadata=meta)

    # --- sesiones, claves y enlaces ---------------------------------------------------------------------
    def me_json(self, user: User) -> dict[str, Any]:
        memberships = [{"org_slug": slug, "governance_role": role} for slug, role in sorted(user.memberships.items())]
        return {"id": user.id, "email": user.email, "name": user.name, "status": user.status,
                "is_platform_admin": user.platform_admin, "memberships": memberships}  # fmt: skip

    def login(self, email: str, password: Any) -> User | None:
        user = self.users.get(email.strip().lower())
        if user is None or not user.password or password != user.password:
            return None
        if user.status != "ACTIVE" and not (user.status == "DISABLED" and "disabled-can-login" in self.faults):
            return None
        return user

    def create_key(self, user: User, label: str) -> tuple[int, dict[str, Any]]:
        if not 1 <= len(label.strip()) <= 100:
            return 422, {"error": "VALIDATION_ERROR", "message": "Label must have 1-100 characters", "field": "label"}
        with self.lock:
            key = Key(str(uuid.uuid4()), label.strip(), "baas_" + secrets.token_urlsafe(32))
            user.keys.append(key)
            self.by_key[key.secret] = user
        return 201, {"key": self.key_json(key, user), "secret": key.secret}

    @staticmethod
    def key_json(key: Key, user: User) -> dict[str, Any]:
        return {"id": key.id, "label": key.label, "kind": "PERSONAL", "org_slug": None,
                "revoked_at": "2026-11-18T00:00:00Z" if key.revoked else None,
                "user": {"id": user.id, "email": user.email, "name": user.name}}  # fmt: skip

    def link_state(self, token: str) -> tuple[str, User | None, Link | None]:
        found = self.links.get(token)
        if found is None:
            return "NOT_FOUND", None, None
        user, link = found
        if link.used:
            return "USED", user, link
        if link.revoked or user.status == "DISABLED":
            return "REVOKED", user, link
        return "VALID", user, link

    def activate(self, token: str, password: str) -> tuple[int, dict[str, Any], str | None]:
        """`POST /auth/activation/{token}/password` con `BAAS_DEV_SKIP_TOTP=1`: `200 {"next": "DONE"}` y sesión."""
        with self.lock:
            status, user, link = self.link_state(token)
            if status != "VALID" or user is None or link is None:
                reason = "REVOKED" if status == "NOT_FOUND" else status
                return 410, {"error": "LINK_INVALID", "message": "This link is no longer valid", "reason": reason}, None
            user.password, user.status, link.used = password, "ACTIVE", True
            session = secrets.token_urlsafe(16)
            self.sessions[session] = user
        return 200, {"next": "DONE"}, session

    def audit_visible(self, viewer: User, row: dict[str, Any]) -> bool:
        """W10: el PLATFORM_ADMIN ve todo; un ORG_ADMIN, las filas de sus organizaciones (las `user.disabled` no tienen
        organización: no las ve)."""
        if viewer.platform_admin:
            return True
        if "audit-orgadmin-blind" in self.faults:
            return False
        admin_orgs = {s for s, role in viewer.memberships.items() if role == "ORG_ADMIN"}
        if row["org_slug"] in admin_orgs:
            return True
        target = self.users_by_id.get(row["target_id"] or "") if row["action"] == "user.disabled" else None
        return bool(target and "audit-orgadmin-sees-disabled" in self.faults and admin_orgs & set(target.memberships))

    def audit_items(self, viewer: User, action: str | None, limit: int) -> dict[str, Any]:
        with self.lock:
            rows = [
                r for r in reversed(self.audit)
                if (not action or str(r["action"]).startswith(action)) and self.audit_visible(viewer, r)
            ]  # fmt: skip
        return {"items": rows[:limit], "total": len(rows), "limit": limit, "offset": 0}

    def set_anonymizer(self, viewer: User, slug: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            org = self.orgs.get(slug)
            role = viewer.memberships.get(slug)
            if org is None or not (viewer.platform_admin or role):
                if org is not None and "toggle-foreign-403" in self.faults:
                    return 403, {"error": "FORBIDDEN", "message": "Forbidden"}
                return 404, {"error": "NOT_FOUND", "message": "Organization not found"}
            if not viewer.platform_admin and role != "ORG_ADMIN" and "toggle-user-allowed" not in self.faults:
                return 403, {"error": "FORBIDDEN", "message": "Only an ORG_ADMIN can change the anonymizer"}
            reason = str(body.get("reason") or "").strip()
            if not 3 <= len(reason) <= 500 and "toggle-no-reason" not in self.faults:
                return 422, {"error": "REASON_REQUIRED", "message": "A reason of 3 to 500 characters is required"}
            if not org.ai_ip and "toggle-ai-ip-off-allowed" not in self.faults:
                return 409, {"error": "AI_IP_DISABLED", "message": "AI-IP is disabled in this organization"}
            enabled = bool(body.get("enabled"))
            if "toggle-not-applied" not in self.faults:
                org.anonymizer = enabled
            if "toggle-no-propagation" not in self.faults:
                self.proxy_anonymizer[slug] = org.anonymizer
            self.record("org.anonymizer.changed", viewer, org=slug, target_type="organization", target_id=slug,
                        reason=reason, metadata={"after": enabled, "reason": reason})  # fmt: skip
        return 200, {"organization": self.org_json(org), "propagated": True}

    def set_flag(self, viewer: User, slug: str, field_name: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            org = self.orgs.get(slug)
            if org is None or not self.can_see_org(viewer, slug):
                return 404, {"error": "NOT_FOUND", "message": "Organization not found"}
            if field_name == "blocked" and not viewer.platform_admin:
                return 403, {"error": "FORBIDDEN", "message": "Only the platform administrator can block"}
            if not viewer.platform_admin and viewer.memberships.get(slug) != "ORG_ADMIN":
                return 403, {"error": "FORBIDDEN", "message": "Forbidden"}
            if len(str(body.get("reason") or "").strip()) < 3:
                return 422, {"error": "REASON_REQUIRED", "message": "A reason is required"}
            setattr(
                org, "blocked" if field_name == "blocked" else "ai_ip", bool(body.get("blocked", body.get("enabled")))
            )
        return 200, {"organization": self.org_json(org), "propagated": True}

    def delete_org(self, viewer: User, slug: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            org = self.orgs.get(slug)
            if org is None or not self.can_see_org(viewer, slug):
                if org is None and slug in self.deleted_slugs and "delete-twice-ok" in self.faults:
                    return 200, {"propagated": True}
                return 404, {"error": "NOT_FOUND", "message": "Organization not found"}
            if not viewer.platform_admin and "delete-orgadmin-allowed" not in self.faults:
                return 403, {"error": "FORBIDDEN", "message": "Only the platform administrator can delete"}
            if slug == META_ORG and "delete-meta-allowed" not in self.faults:
                return 403, {"error": "ORG_PROTECTED", "message": "The platform organization cannot be deleted"}
            if viewer.platform_admin and "delete-no-reauth" not in self.faults:  # H4-17: contraseña (dev sin TOTP)
                password = body.get("current_password")
                if not password:
                    return 422, {
                        "error": "VALIDATION_ERROR",
                        "message": "Your password is required",
                        "field": "current_password",
                    }
                if password != DEV_PASSWORD:
                    return 401, {"error": "INVALID_CREDENTIALS", "message": "Invalid credentials"}
            reason = str(body.get("reason") or "").strip()
            if not 3 <= len(reason) <= 500:
                return 422, {"error": "REASON_REQUIRED", "message": "A reason of 3 to 500 characters is required"}
            if not org.blocked and "delete-no-block-check" not in self.faults:
                return 409, {"error": "ORG_NOT_BLOCKED", "message": "Block the organization first"}
            if body.get("confirm_slug") != slug and "delete-no-slug-check" not in self.faults:
                return 422, {"error": "SLUG_MISMATCH", "message": "The confirmation does not match the slug"}
            members = [u for u in self.users.values() if slug in u.memberships]
            conversations = [c for c in self.convs.values() if c.org == slug]
            disable = body.get("disable_orphans", "orphans-default-off" not in self.faults)
            if "orphans-ignore-flag" in self.faults:
                disable = True
            if "orphans-kept" in self.faults:
                disable = False
            disabled = self.orphan_candidates(slug) if disable else []
            for user in disabled:  # antes de borrar las membresías: es lo que decide quién se queda sin organización
                self.disable_user(user, viewer, reason, slug)
            self.deleted_slugs.add(slug)
            del self.orgs[slug]
            if {"proxy-still-serves", "mcp-still-serves"} & self.faults:
                self.ghosts[slug] = org
            for user in members:
                if "delete-removes-users" in self.faults and not user.platform_admin:
                    self.by_key = {k: u for k, u in self.by_key.items() if u is not user}
                if "proxy-still-serves" not in self.faults:
                    del user.memberships[slug]
            if "vault-kept" not in self.faults:
                for conv in conversations:
                    del self.convs[conv.id]
                self.fingerprints = {k: v for k, v in self.fingerprints.items() if v in self.convs}
                self.response_ids = {k: v for k, v in self.response_ids.items() if v in self.convs}
            if "redis-kept" not in self.faults:
                self.redis_keys = {
                    k for k in self.redis_keys if not (k == f"baas:policy:org:{slug}" or k.endswith(f":{slug}"))
                }
            for name, instance in self.redis.items():
                if name == EPHEMERAL and "ephemeral-keys-kept" in self.faults:
                    continue
                for conv in conversations:
                    instance.delete(f"baas:conv:{conv.id}:vault")
            if "no-quarantine" not in self.faults and slug in self.mcp_dirs:
                self.quarantine.append(f"{slug}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
                del self.mcp_dirs[slug]
            if "audit-missing" not in self.faults:
                self.record("org.deleted", viewer, target_type="organization", target_id=slug, reason=reason,
                            metadata={"slug": slug, "reason": reason, "conversations": len(conversations),
                                      "members": len(members)})  # fmt: skip
        return 200, {"propagated": True, "disabled_users": len(disabled)}

    # --- lo que miraría el inspector ---------------------------------------------------------------------
    def vault_rows(self, slug: str) -> int:
        with self.lock:
            return sum(len(c.vault.tokens) + len(c.vault.counters) + 1 for c in self.convs.values() if c.org == slug)

    # --- proxy ---------------------------------------------------------------------------------------------
    def proxy_org(self, slug: str) -> Org | None:
        return self.orgs.get(slug) or (self.ghosts.get(slug) if "proxy-still-serves" in self.faults else None)

    def _resolve(self, api: str, org: Org, user: User, payload: dict[str, Any]) -> Conv:
        fp = fingerprint(api, org.slug, payload)
        prev = payload.get("previous_response_id")
        conv_id = self.response_ids.get(prev) if prev else self.fingerprints.get(fp)
        conv = self.convs.get(conv_id or "")
        if conv is None:
            conv = Conv(str(uuid.uuid4()), org.slug, user, api, str(payload.get("model")), org.ai_ip,
                        self.proxy_anonymizer.get(org.slug, org.anonymizer))  # fmt: skip
            self.convs[conv.id] = conv
        elif "state-follows-switch" in self.faults:
            conv.ai_ip, conv.anonymizer = org.ai_ip, self.proxy_anonymizer.get(org.slug, org.anonymizer)
            conv.now_ai_ip, conv.now_anonymizer = conv.ai_ip, conv.anonymizer
        else:
            policy_anonymizes = org.ai_ip and self.proxy_anonymizer.get(org.slug, org.anonymizer)
            anonymized = bool(conv.now_ai_ip and conv.now_anonymizer)
            if policy_anonymizes and not anonymized and "no-ratchet" not in self.faults:
                conv.now_ai_ip = conv.now_anonymizer = True  # H4-08: sube en su siguiente turno
            elif not policy_anonymizes and anonymized and "ratchet-lowers" in self.faults:
                conv.now_ai_ip = org.ai_ip
                conv.now_anonymizer = self.proxy_anonymizer.get(org.slug, org.anonymizer)
        self.fingerprints.setdefault(fp, conv.id)
        return conv

    def proxy_turn(self, slug: str, route: str, headers: Any, raw: bytes) -> tuple[int, str, bytes]:
        api, upstream = ROUTES[route]
        user = self.by_key.get(headers.get("X-Baas-Api-Key") or "")
        if user is None:
            etype = "authentication_error" if api == "messages" else "invalid_request_error"
            return 401, "application/json", error_body(api, etype, "baas_invalid_api_key", "API key no válida")
        with self.lock:
            org = self.proxy_org(slug)
            if org is None or slug not in user.memberships:
                return (
                    403,
                    "application/json",
                    error_body(api, "permission_error", "baas_access_denied", DENIED_MESSAGE),
                )
            if org.blocked and "block-not-applied" not in self.faults:
                return (
                    403,
                    "application/json",
                    error_body(api, "permission_error", "baas_access_blocked", BLOCKED_MESSAGE),
                )
            payload = json.loads(raw)
            conv = self._resolve(api, org, user, payload)
            mode = "bypass" if not conv.now_ai_ip else "anonymize" if conv.now_anonymizer else "observe"
            if mode == "observe" and "observe-anonymizes" in self.faults:
                mode = "anonymize"
        request_counts: dict[str, int] = {}
        sent = raw
        if mode == "anonymize":
            with self.lock:
                sent = encode(anonymize(payload, conv.vault, request_counts))
                hot = json.dumps(conv.vault.tokens, ensure_ascii=False)  # la copia caliente del vault (H4-02)
                self.originals_target().set(f"baas:conv:{conv.id}:vault", hot)
        elif mode == "observe" and "observe-rewrites" in self.faults:
            sent = encode({**payload, "baas_e2e": True})
        status, ctype, body = self._forward(upstream, sent)
        response_counts: dict[str, int] = {}
        if mode == "anonymize" and status == 200:
            rehydrator = Rehydrator(conv.vault, response_counts, bad_tool_json=False, skip_copies=False)
            body = rehydrator.stream(api, body) if "event-stream" in ctype else rehydrator.json_body(body)
        if api == "responses" and status == 200:
            rid = response_id_of(body, bool(payload.get("stream")))
            if rid:
                with self.lock:
                    self.response_ids[rid] = conv.id
        verification = self._verification(mode, payload, request_counts, response_counts, body)
        self._record(conv, mode, api, payload, sent, verification)
        return status, ctype, body

    def _verification(
        self, mode: str, payload: dict[str, Any], req: dict[str, int], resp: dict[str, int], body: bytes
    ) -> Any:
        if mode == "bypass":
            return None
        if mode == "observe":
            seen: dict[str, int] = {}
            anonymize(payload, Vault(), seen)  # solo para contar lo que las reglas reconocen
            residual = [] if "observe-clean" in self.faults else [
                {"rule_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, kind)), "token_type": kind, "count": count}
                for kind, count in sorted(seen.items())
            ]  # fmt: skip
            return {"status": "WARNING" if residual else "CLEAN", "request_replacements_by_type": {},
                    "response_replacements_by_type": {}, "unresolved_response_tokens": 0,
                    "orphan_request_tokens": 0, "residual_matches": residual}  # fmt: skip
        unresolved = len(h3s.TOKEN_RE.findall(body.decode(errors="replace")))
        return {"status": "WARNING" if unresolved else "CLEAN", "request_replacements_by_type": req,
                "response_replacements_by_type": resp, "unresolved_response_tokens": unresolved,
                "orphan_request_tokens": 0, "residual_matches": []}  # fmt: skip

    def _forward(self, path: str, body: bytes) -> tuple[int, str, bytes]:
        return h3s.StackState._forward(self, path, body)  # type: ignore[arg-type]

    def _record(self, conv: Conv, mode: str, api: str, payload: dict[str, Any], sent: bytes, verification: Any) -> None:
        """Lo que haría la webapp al consumir los eventos del turno, y la difusión en vivo."""
        original = last_user_text(api, payload)
        sent_text = last_user_text(api, json.loads(sent))
        with self.lock:
            turn = {
                "turn_index": len(conv.turns) + 1, "status": "COMPLETED", "verification": verification,
                "ai_ip": bool(conv.now_ai_ip), "anonymizer": bool(conv.now_anonymizer),
                "verification_status": verification["status"] if verification else None,
                "original": original, "request_sent": sent_text, "response_received": f"Eco: {sent_text}",
            }  # fmt: skip
            conv.turns.append(turn)
            event = {"event_id": uuid.uuid4().hex, "conversation_id": conv.id, "request_original": original,
                     "request_sent": sent_text, "response_returned": f"Eco: {original}"}  # fmt: skip
            trace = self.originals_target(trace=True)  # XADD baas:traces y, en el acto, XACK + XDEL de la webapp
            trace.set(f"baas:traces:{event['event_id']}", json.dumps(event, ensure_ascii=False))
            trace.delete(f"baas:traces:{event['event_id']}")
            started_protected = conv.ai_ip and conv.anonymizer
            if not started_protected and conv.protected_from_turn is None and turn["ai_ip"] and turn["anonymizer"]:
                conv.protected_from_turn = turn["turn_index"]
            if _SEVERITY[turn["verification_status"]] > _SEVERITY[conv.verification_status]:
                conv.verification_status = turn["verification_status"]
            for sub in list(self.subscribers):
                if sub.org in (None, conv.org) and self.can_view(sub.user, conv):
                    message = {
                        "type": "turn.completed", "conversation_id": conv.id, "org_slug": conv.org,
                        "conversation": self.conv_json(conv, sub.user), "turn_index": turn["turn_index"],
                        "turn": self.turn_json(conv, turn, sub.user, live=True),
                    }  # fmt: skip
                    if "ws-silent" in self.faults and sub.user.platform_admin:
                        continue
                    sub.messages.put(json.dumps(message))

    # --- webapp: conversaciones --------------------------------------------------------------------------------
    def conv_json(self, conv: Conv, viewer: User) -> dict[str, Any]:
        data = {
            "id": conv.id, "org_slug": conv.org, "user": {"id": conv.user.id, "email": conv.user.email},
            "provider": "ANTHROPIC" if conv.api == "messages" else "OPENAI",
            "api_flavor": h3s.FLAVORS[conv.api], "model": conv.model, "ai_ip_enabled": conv.ai_ip,
            "status": "ACTIVE", "turn_count": len(conv.turns), "verification_status": conv.verification_status,
            "originals": "OWN" if self.content_mode(viewer, conv) == "OWN" else "LOCKED",
        }  # fmt: skip
        data["content_available"] = self.content_mode(viewer, conv) == "OWN" or self.admin_content(conv)
        if "no-cause" not in self.faults:
            data["anonymizer_enabled"] = conv.anonymizer
        if "no-protected-from" not in self.faults:
            data["protected_from_turn"] = conv.protected_from_turn
        return data

    def turn_json(self, conv: Conv, turn: dict[str, Any], viewer: User, *, live: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {"turn_index": turn["turn_index"], "status": turn["status"],
                                "verification_status": turn["verification_status"],
                                "ai_ip_enabled": turn["ai_ip"], "anonymizer_enabled": turn["anonymizer"],
                                "verification": turn["verification"], "replacements": []}  # fmt: skip
        mode = self.content_mode(viewer, conv)
        visible = self.admin_content_turn(conv, turn)
        if mode == "OWN" or (mode == "VERIFY" and (visible or (live and "ws-leak" in self.faults))):
            data["request_sent"] = segments(turn["request_sent"])
            data["response_received"] = segments(turn["response_received"])
            if mode == "VERIFY":
                data["content_available"] = True
        elif mode == "VERIFY":
            data["content_available"] = False
        return data

    def detail_json(self, conv: Conv, viewer: User) -> dict[str, Any]:
        turns = [{"turn_index": t["turn_index"], "status": t["status"],
                  "verification_status": t["verification_status"]} for t in conv.turns]  # fmt: skip
        if "admin-detail-leak" in self.faults and viewer.platform_admin:
            turns = [{**s, "request_sent": segments(t["request_sent"])} for s, t in zip(turns, conv.turns, strict=True)]
        return {**self.conv_json(conv, viewer), "turns": turns}

    # --- MCP ---------------------------------------------------------------------------------------------------
    def mcp(self, slug: str, headers: Any, message: dict[str, Any]) -> tuple[int, Any]:
        auth = headers.get("Authorization") or ""
        user = self.by_key.get(auth.removeprefix("Bearer ").strip()) if auth.startswith("Bearer ") else None
        if user is None:
            return 401, {"error": "UNAUTHENTICATED", "message": "Invalid API key"}
        with self.lock:
            org = self.orgs.get(slug)
            ghost = org is None and "mcp-still-serves" in self.faults and slug in self.ghosts
            if not ghost and (org is None or not (user.platform_admin or slug in user.memberships)):
                return 404, {"error": "NOT_FOUND"}
            method, mid = message.get("method"), message.get("id")
            if method == "initialize":
                result: Any = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                               "serverInfo": {"name": "fake-baas-mcp", "version": "h4"}}  # fmt: skip
            elif method == "tools/call":
                params = message.get("params") or {}
                args = params.get("arguments") or {}
                role = user.memberships.get(slug)
                if params.get("name") == "baas_write_document":
                    if not (user.platform_admin or role == "ORG_ADMIN"):
                        err = {"error": "AUTHORIZATION_DENIED", "message": "denied", "required_roles": ["ORG_ADMIN"]}
                        result = {"isError": True, "content": [{"type": "text", "text": json.dumps(err)}]}
                    else:
                        path, content = str(args.get("path")), str(args.get("content"))
                        self.mcp_dirs.setdefault(slug, {})[path] = content
                        data = {"path": path, "sha256": hashlib.sha256(content.encode()).hexdigest()}
                        result = {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}
                else:
                    data = {"user": user.email, "organization": slug, "role": role}
                    result = {"content": [{"type": "text", "text": json.dumps(data)}], "structuredContent": data}
            else:
                return 202, None
        return 200, {"jsonrpc": "2.0", "id": mid, "result": result}


class FakeInspector:
    """Lo que `ContainerInspector` mira dentro de los contenedores, leído de la pila simulada."""

    def __init__(self, state: StackState) -> None:
        self.state = state

    def vault_rows(self, slug: str) -> int:
        return self.state.vault_rows(slug)

    def conversation_ids(self, slug: str) -> list[str]:
        with self.state.lock:
            return sorted(c.id for c in self.state.convs.values() if c.org == slug)

    def redis_services(self) -> list[str]:
        return [PERSISTENT] if "no-ephemeral" in self.state.faults else [PERSISTENT, EPHEMERAL]

    def redis_keys(self, slug: str, conversation_ids: Sequence[str] = ()) -> list[str]:
        with self.state.lock:
            keys = sorted(k for k in self.state.redis_keys if k == f"baas:policy:org:{slug}" or k.endswith(f":{slug}"))
            found = [f"[{PERSISTENT}] {k}" for k in keys]
            if EPHEMERAL in self.redis_services():
                for conv in conversation_ids:
                    held = sorted(k for k in self.state.ephemeral_keys if k.startswith(f"baas:conv:{conv}:"))
                    found += [f"[{EPHEMERAL}] {k}" for k in held]
            return found

    def redis_setting(self, service: str, name: str) -> str:
        with self.state.lock:
            return self.state.redis[service].settings[name]

    def disk_files(self, service: str) -> list[str]:
        with self.state.lock:
            return self.state.redis[service].files()

    def force_aof_rewrite(self, service: str) -> None:
        with self.state.lock:
            self.state.redis[service].rewrite()

    def disk_contains(self, service: str, needles: Sequence[str]) -> list[str]:
        with self.state.lock:
            blind = "scanner-blind" in self.state.faults and service == PERSISTENT
            text = self.state.redis[service].text(binary=not blind)
            return [needle for needle in needles if needle in text]

    def redis_set_temp(self, service: str, key: str, value: str, ttl: int) -> None:
        with self.state.lock:
            self.state.redis[service].set(key, value)

    def redis_delete(self, service: str, key: str) -> None:
        with self.state.lock:
            self.state.redis[service].delete(key)

    def mcp_org_dirs(self) -> list[str]:
        with self.state.lock:
            return sorted(self.state.mcp_dirs)

    def mcp_quarantine(self) -> list[str]:
        with self.state.lock:
            return list(self.state.quarantine)


# --- servidores HTTP -------------------------------------------------------------------------------------


class _Base(_Handler, BaseHTTPRequestHandler):
    """Los manejadores de la pila H3 con la base estándar a la vista (los `do_*` son nombres de la biblioteca)."""


_PROXY_PATH = re.compile(r"/org/([a-z0-9-]+)/(" + "|".join(re.escape(r) for r in ROUTES) + r")")
_MCP_PATH = re.compile(r"/org/([a-z0-9-]+)/mcp")


def proxy_handler(state: StackState) -> type[BaseHTTPRequestHandler]:
    class Handler(_Base):
        def do_POST(self) -> None:
            raw = self._read()
            m = _PROXY_PATH.fullmatch(self.path)
            if m is None:
                return self._json(404, {"error": {"type": "not_found_error", "message": "ruta no simulada"}})
            status, ctype, body = state.proxy_turn(m.group(1), m.group(2), self.headers, raw)
            return self._send(status, ctype, body)

    return Handler


_ORG = re.compile(r"/api/v1/orgs/([a-z0-9-]+)")
_ORG_SUB = re.compile(r"/api/v1/orgs/([a-z0-9-]+)/(anonymizer|block|ai-ip|members|delete)")
_ORG_GET = re.compile(r"/api/v1/orgs/([a-z0-9-]+)/(members|delete-preview)")
_ACTIVATION = re.compile(r"/api/v1/auth/activation/([A-Za-z0-9_-]+)(/password)?")
_CONV = re.compile(r"/api/v1/conversations/([0-9a-f-]+)")
_TURN = re.compile(r"/api/v1/conversations/([0-9a-f-]+)/turns/(\d+)")
_REVEAL = re.compile(r"/api/v1/conversations/([0-9a-f-]+)/turns/(\d+)/reveal")
_CSRF = {"error": "CSRF_FAILED", "message": "X-Requested-With: baas-webapp required"}


def webapp_handler(state: StackState) -> type[BaseHTTPRequestHandler]:
    class Handler(_Base):
        def _viewer(self) -> User | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "baas_session":
                    return state.sessions.get(value)
            return None

        def _visible(self, viewer: User) -> list[Conv]:
            with state.lock:
                return [c for c in state.convs.values() if state.can_view(viewer, c)]

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path == "/healthz":
                return self._json(200, {"status": "ok", "version": "simulada"})
            if (m := _ACTIVATION.fullmatch(url.path)) and not m.group(2):
                status, user, _ = state.link_state(m.group(1))
                if status != "VALID" or user is None:
                    return self._json(200, {"status": status})
                return self._json(200, {"status": "VALID", "purpose": "ACTIVATION", "email": user.email,
                                        "name": user.name, "org_names": sorted(user.memberships)})  # fmt: skip
            viewer = self._viewer()
            if viewer is None:
                return self._json(401, {"error": "UNAUTHENTICATED", "message": "Not authenticated"})
            if url.path == "/api/v1/ws/traces":
                return self._websocket(viewer, parse_qs(url.query))
            if url.path == "/api/v1/me":
                return self._json(200, state.me_json(viewer))
            if url.path == "/api/v1/api-keys":
                return self._json(200, {"items": [state.key_json(k, viewer) for k in viewer.keys]})
            if m := _ORG_GET.fullmatch(url.path):
                slug = m.group(1)
                if m.group(2) == "delete-preview":
                    return self._json(*state.delete_preview(viewer, slug))
                if slug not in state.orgs or not state.can_see_org(viewer, slug):
                    return self._json(404, {"error": "NOT_FOUND", "message": "Organization not found"})
                if not (viewer.platform_admin or viewer.memberships.get(slug) == "ORG_ADMIN"):
                    return self._json(403, {"error": "FORBIDDEN", "message": "Forbidden"})
                with state.lock:
                    members = [state.member_json(u, slug) for u in state.users.values() if slug in u.memberships]
                return self._json(200, {"members": members})
            if m := _ORG.fullmatch(url.path):
                org = state.orgs.get(m.group(1))
                if org is None or not state.can_see_org(viewer, org.slug):
                    return self._json(404, {"error": "NOT_FOUND", "message": "Organization not found"})
                return self._json(200, state.org_json(org))
            if url.path == "/api/v1/audit":
                if not viewer.platform_admin and "ORG_ADMIN" not in viewer.memberships.values():
                    return self._json(
                        403, {"error": "FORBIDDEN", "message": "Only administrators can see the audit log"}
                    )
                query = parse_qs(url.query)
                action = (query.get("action") or [None])[0]
                return self._json(200, state.audit_items(viewer, action, int((query.get("limit") or ["50"])[0])))
            if url.path == "/api/v1/conversations":
                query = parse_qs(url.query)
                org = (query.get("org") or [None])[0]
                term = ((query.get("q") or [""])[0]).lower()
                items = [
                    state.conv_json(c, viewer) for c in self._visible(viewer)
                    if (not org or c.org == org) and term in f"{c.model} {c.user.email} {c.id}".lower()
                ]  # fmt: skip
                limit = int((query.get("limit") or ["50"])[0])
                return self._json(200, {"items": items[:limit], "total": len(items), "limit": limit, "offset": 0})
            by_id = {c.id: c for c in self._visible(viewer)}
            if m := _TURN.fullmatch(url.path):
                conv = by_id.get(m.group(1))
                turn = next((t for t in conv.turns if t["turn_index"] == int(m.group(2))), None) if conv else None
                if conv is None or turn is None:
                    return self._json(404, {"error": "NOT_FOUND", "message": "Turn not found"})
                return self._json(200, state.turn_json(conv, turn, viewer))
            if (m := _CONV.fullmatch(url.path)) and m.group(1) in by_id:
                return self._json(200, state.detail_json(by_id[m.group(1)], viewer))
            return self._json(404, {"error": "NOT_FOUND", "message": "Not found"})

        def do_POST(self) -> None:
            raw = self._read()
            if m := _MCP_PATH.fullmatch(self.path):
                status, data = state.mcp(m.group(1), self.headers, json.loads(raw or b"{}"))
                return self._json(status, data) if data is not None else self._send(status, "application/json", b"")
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
                user = state.login(str(body.get("email")), body.get("password"))
                if user is None:
                    return self._json(401, {"error": "INVALID_CREDENTIALS", "message": "Invalid credentials"})
                token = secrets.token_urlsafe(16)
                state.sessions[token] = user
                cookie = f"baas_session={token}; HttpOnly; Path=/; SameSite=lax"
                return self._json(200, {"next": "DONE"}, {"Set-Cookie": cookie})
            if (m := _ACTIVATION.fullmatch(self.path)) and m.group(2):
                status, data, session = state.activate(m.group(1), str(body.get("password") or ""))
                set_cookie = (
                    {"Set-Cookie": f"baas_session={session}; HttpOnly; Path=/; SameSite=lax"} if session else None
                )
                return self._json(status, data, set_cookie)
            viewer = self._viewer()
            if viewer is None:
                return self._json(401, {"error": "UNAUTHENTICATED", "message": "Not authenticated"})
            if self.path == "/api/v1/orgs":
                if not viewer.platform_admin:
                    return self._json(403, {"error": "FORBIDDEN", "message": "Forbidden"})
                return self._json(*state.create_org(viewer, str(body.get("slug")), str(body.get("name"))))
            if self.path == "/api/v1/api-keys":
                return self._json(*state.create_key(viewer, str(body.get("label") or "")))
            if m := _REVEAL.fullmatch(self.path):
                conv = next((c for c in self._visible(viewer) if c.id == m.group(1)), None)
                if conv is None:
                    return self._json(404, {"error": "NOT_FOUND", "message": "Not found"})
                if state.content_mode(viewer, conv) != "OWN" and "admin-reveal" not in state.faults:
                    return self._json(403, {"error": "FORBIDDEN", "message": "Originals are locked"})
                turn = next((t for t in conv.turns if t["turn_index"] == int(m.group(2))), None)
                return self._json(200, {"originals": {"original": (turn or {}).get("original")}})
            if (m := _ORG_SUB.fullmatch(self.path)) and m.group(2) in ("delete", "members"):
                slug = m.group(1)
                if m.group(2) == "delete":
                    return self._json(*state.delete_org(viewer, slug, body))
                return self._json(*state.invite(viewer, slug, body))
            return self._json(404, {"error": "NOT_FOUND", "message": "Not found"})

        def do_PUT(self) -> None:
            raw = self._read()
            if self.headers.get("X-Requested-With") != "baas-webapp":
                return self._json(403, _CSRF)
            viewer = self._viewer()
            if viewer is None:
                return self._json(401, {"error": "UNAUTHENTICATED", "message": "Not authenticated"})
            m = _ORG_SUB.fullmatch(self.path)
            if m is None or m.group(2) not in ("anonymizer", "block", "ai-ip"):
                return self._json(404, {"error": "NOT_FOUND", "message": "Not found"})
            body = json.loads(raw or b"{}")
            slug, what = m.group(1), m.group(2)
            if what == "anonymizer":
                return self._json(*state.set_anonymizer(viewer, slug, body))
            return self._json(*state.set_flag(viewer, slug, "blocked" if what == "block" else "ai_ip", body))

        def _websocket(self, viewer: User, query: dict[str, list[str]]) -> None:
            if self.headers.get("Origin") != state.app_url:
                return self._json(403, {"error": "CSRF_FAILED", "message": "Origin not allowed"})
            slug = (query.get("org") or [None])[0]
            if slug and (slug not in state.orgs or not state.can_see_org(viewer, slug)):
                return self._json(404, {"error": "NOT_FOUND", "message": "Organization not found"})
            key = self.headers.get("Sec-WebSocket-Key") or ""
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()  # noqa: S324
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.close_connection = True
            sub = Subscriber(viewer, slug)
            with state.lock:
                state.subscribers.append(sub)
            try:
                self.wfile.write(ws_frame(1, json.dumps({"type": "hello", "org_slug": slug}).encode()))
                self.wfile.flush()
                while True:
                    try:
                        text = sub.messages.get(timeout=0.1)
                        self.wfile.write(ws_frame(1, text.encode()))
                        self.wfile.flush()
                    except queue.Empty:
                        pass
                    if select.select([self.connection], [], [], 0)[0]:
                        data = self.connection.recv(4096)
                        if not data or data[0] & 0x0F == 0x8:
                            break
            except OSError:
                pass
            finally:
                with state.lock, contextlib.suppress(ValueError):
                    state.subscribers.remove(sub)

    return Handler


class FakeH4Stack:
    """Las tres piezas en puertos efímeros de `host`. `faults` ⊆ `FAULTS`; `debug_body` = `json` o `text`."""

    def __init__(self, faults: Any = (), *, debug_body: str = "json", host: str = "127.0.0.1") -> None:
        chosen = set(faults)
        unknown = chosen - FAULTS.keys()
        if unknown:
            raise ValueError(f"fallos desconocidos: {', '.join(sorted(unknown))}")
        self.provider = EchoProvider(debug_body)
        self._servers = [_Server(provider_handler(self.provider), host)]
        self.state = StackState(chosen, self._servers[0].url)
        self._servers += [_Server(proxy_handler(self.state), host), _Server(webapp_handler(self.state), host)]
        self.state.proxy_url, self.state.app_url = self._servers[1].url, self._servers[2].url
        self.inspector = FakeInspector(self.state)

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

    def __enter__(self) -> FakeH4Stack:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pila H4 simulada para probar dev/e2e_h4.py")
    p.add_argument("--fault", action="append", choices=sorted(FAULTS), default=[], help="Fallo que se inyecta")
    p.add_argument("--debug-body", choices=("json", "text"), default="json", help="Cómo guarda los cuerpos")
    p.add_argument("--host", default="127.0.0.1")
    ns = p.parse_args(argv)
    with FakeH4Stack(ns.fault, debug_body=ns.debug_body, host=ns.host) as stack:
        print(f"webapp     {stack.app_url}\nproxy      {stack.proxy_url}\nproveedor  {stack.fake_url}")
        for fault in ns.fault:
            print(f"fallo      {fault}: {FAULTS[fault]}")
        print(
            f"\nuv run python dev/e2e_h4.py --app-url {stack.app_url} --fake-url {stack.fake_url} --inspect none",
            flush=True,
        )
        with contextlib.suppress(KeyboardInterrupt):
            threading.Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
