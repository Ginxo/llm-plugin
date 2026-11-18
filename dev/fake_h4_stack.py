#!/usr/bin/env python3
"""Pila H4 simulada para probar `dev/e2e_h4.py` sin la pila real: webapp, proxy, MCP y proveedor con eco.

Amplía la de H3 (`dev/fake_h3_stack.py`, que no se toca: se importan su proveedor, su vault y sus utilidades) con lo
que el hito H4 añade (contrato §4, §5.2, §6 y §8; plan, tareas W2 a W5, P2, P4, M3 y D1):

- **Proxy** con el anonimizador por conversación: con AI-IP activo y el anonimizador apagado en el primer turno, el
  cuerpo sale tal como entró, sin tokens, con `WARNING` y `residual_matches`; cada conversación conserva el valor con
  el que empezó en los dos sentidos. Una organización bloqueada responde `403 baas_access_blocked` y una eliminada,
  `403 baas_access_denied`.
- **Webapp**: `PUT /orgs/{slug}/anonymizer`, crear, bloquear, activar AI-IP, invitar miembros,
  `POST /orgs/{slug}/delete` (con sus errores), la auditoría, la vista de verificación del PLATFORM_ADMIN sin contenido
  (`content_available`) y el WebSocket `/api/v1/ws/traces` con el mismo filtrado.
- **MCP** (`/org/<slug>/mcp`): `initialize` y `baas_write_document`; una organización eliminada pasa a
  `.quarantine/<slug>-<fecha>`.
- `FakeInspector`: lo que `ContainerInspector` mira dentro de los contenedores (vault, claves de Redis y MCP).

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
    "no-quarantine": "el directorio del MCP no pasa a cuarentena",
    "audit-missing": "la eliminación no deja rastro en la auditoría",
    "delete-removes-users": "al eliminar una organización se elimina también a sus usuarios",
}


# --- modelo -----------------------------------------------------------------------------------------


@dataclass
class Org:
    slug: str
    name: str
    ai_ip: bool = False
    anonymizer: bool = True
    blocked: bool = False


@dataclass
class User:
    email: str
    memberships: dict[str, str]  # slug → ORG_ADMIN · USER
    platform_admin: bool = False
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


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
        }
        users = [
            User("admin@baas.test", {META_ORG: "ORG_ADMIN"}, platform_admin=True),
            User("orgadmin@demo.test", {"demo": "ORG_ADMIN"}),
            User("analista@demo.test", {"demo": "USER"}),
            User("observador@observacion.test", {"observacion": "USER"}),
        ]
        self.users = {u.email: u for u in users}
        self.by_key = {dev_api_key(u.email): u for u in users}
        self.sessions: dict[str, User] = {}
        self.deleted_slugs: set[str] = set()
        self.ghosts: dict[str, Org] = {}  # lo que el proxy o el MCP siguen viendo con un fallo inyectado
        self.proxy_anonymizer: dict[str, bool] = {s: o.anonymizer for s, o in self.orgs.items()}
        self.convs: dict[str, Conv] = {}
        self.fingerprints: dict[str, str] = {}
        self.response_ids: dict[str, str] = {}
        self.subscribers: list[Subscriber] = []
        self.audit: list[dict[str, Any]] = []
        self.redis_keys: set[str] = set()
        for user in users:
            for slug in user.memberships:
                self.redis_keys.add(f"baas:member:{user.id}:{slug}")
        self.redis_keys |= {f"baas:policy:org:{slug}" for slug in self.orgs}
        self.mcp_dirs: dict[str, dict[str, str]] = {}
        self.quarantine: list[str] = []

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
            self.audit.append({"action": "org.created", "org_id": str(uuid.uuid4()), "actor": actor.email,
                               "target_type": "organization", "target_id": slug, "metadata": {}})  # fmt: skip
        return 201, {"organization": self.org_json(org), "member": None, "activation_url": None, "propagated": True}

    def invite(self, slug: str, email: str, role: str) -> tuple[int, dict[str, Any]]:
        with self.lock:
            user = self.users.get(email)
            if user is None:
                return 404, {"error": "NOT_FOUND", "message": "User not found"}
            user.memberships[slug] = role
            self.redis_keys.add(f"baas:member:{user.id}:{slug}")
        return 201, {"member": {"email": email, "governance_role": role}, "activation_url": None, "propagated": True}

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
            self.audit.append({"action": "org.anonymizer.changed", "org_id": slug, "actor": viewer.email,
                               "target_type": "organization", "target_id": slug,
                               "metadata": {"after": enabled, "reason": reason}})  # fmt: skip
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
            if "no-quarantine" not in self.faults and slug in self.mcp_dirs:
                self.quarantine.append(f"{slug}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
                del self.mcp_dirs[slug]
            if "audit-missing" not in self.faults:
                self.audit.append({"action": "org.deleted", "org_id": None, "actor": viewer.email,
                                   "target_type": "organization", "target_id": slug,
                                   "metadata": {"slug": slug, "reason": reason, "conversations": len(conversations),
                                                "members": len(members)}})  # fmt: skip
        return 200, {"propagated": True}

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

    def redis_keys(self, slug: str) -> list[str]:
        with self.state.lock:
            return sorted(k for k in self.state.redis_keys if k == f"baas:policy:org:{slug}" or k.endswith(f":{slug}"))

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
            viewer = self._viewer()
            if viewer is None:
                return self._json(401, {"error": "UNAUTHENTICATED", "message": "Not authenticated"})
            if url.path == "/api/v1/ws/traces":
                return self._websocket(viewer, parse_qs(url.query))
            if m := _ORG.fullmatch(url.path):
                org = state.orgs.get(m.group(1))
                if org is None or not state.can_see_org(viewer, org.slug):
                    return self._json(404, {"error": "NOT_FOUND", "message": "Organization not found"})
                return self._json(200, state.org_json(org))
            if url.path == "/api/v1/audit":
                if not viewer.platform_admin:
                    return self._json(
                        403, {"error": "FORBIDDEN", "message": "Only administrators can see the audit log"}
                    )
                action = (parse_qs(url.query).get("action") or [None])[0]
                items = [a for a in state.audit if not action or a["action"] == action]
                return self._json(200, {"items": items, "total": len(items)})
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
            if self.path == "/api/v1/orgs":
                if not viewer.platform_admin:
                    return self._json(403, {"error": "FORBIDDEN", "message": "Forbidden"})
                return self._json(*state.create_org(viewer, str(body.get("slug")), str(body.get("name"))))
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
                if slug not in state.orgs or not state.can_see_org(viewer, slug):
                    return self._json(404, {"error": "NOT_FOUND", "message": "Organization not found"})
                return self._json(
                    *state.invite(slug, str(body.get("email")), str(body.get("governance_role") or "USER"))
                )
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
