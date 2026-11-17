"""Handshake con la webapp (arquitectura §3.1, contrato §6 y §10) y elección de organización."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import httpclient
from .envfile import normalize_app_url
from .errors import (
    AuthError,
    BaasError,
    ConfigError,
    NetworkError,
    NoOrganizationsError,
    OrgNotFoundError,
    SelectionRequiredError,
)

HANDSHAKE_PATH = "/api/v1/plugin/handshake"


@dataclass(frozen=True)
class Organization:
    slug: str
    name: str
    governance_role: str
    functional_roles: tuple[str, ...]
    paused: bool
    blocked: bool
    ai_ip_enabled: bool
    mcp_url: str
    proxy_anthropic_url: str
    proxy_openai_url: str

    def describe(self) -> str:
        roles = ", ".join(self.functional_roles) or "sin roles funcionales"
        return f"{self.slug} · {self.name} ({self.governance_role}; {roles})"


@dataclass(frozen=True)
class Handshake:
    user_id: str
    email: str
    platform_role: str
    global_kill_switch: bool
    organizations: tuple[Organization, ...]

    def find(self, slug: str) -> Organization | None:
        return next((o for o in self.organizations if o.slug == slug), None)


def _service_url(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise BaasError(f"Respuesta del handshake sin `{name}`.")
    try:
        return normalize_app_url(value)
    except ConfigError as exc:
        raise BaasError(f"El handshake devolvió una URL no segura en `{name}`: {exc.message}") from exc


def parse_handshake(body: Any) -> Handshake:
    try:
        user = body["user"]
        orgs = [
            Organization(
                slug=str(o["slug"]),
                name=str(o.get("name") or o["slug"]),
                governance_role=str(o.get("governance_role") or "USER"),
                functional_roles=tuple(str(r) for r in o.get("functional_roles") or ()),
                paused=bool(o.get("paused")),
                blocked=bool(o.get("blocked")),
                ai_ip_enabled=bool(o.get("ai_ip_enabled")),
                mcp_url=_service_url(o.get("mcp_url"), "mcp_url"),
                proxy_anthropic_url=_service_url(o.get("proxy_anthropic_url"), "proxy_anthropic_url"),
                proxy_openai_url=_service_url(o.get("proxy_openai_url"), "proxy_openai_url"),
            )
            for o in body.get("organizations") or []
        ]
        return Handshake(
            user_id=str(user["id"]),
            email=str(user["email"]),
            platform_role=str(user.get("platform_role") or "USER"),
            global_kill_switch=bool(body.get("global_kill_switch")),
            organizations=tuple(orgs),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise BaasError(f"Respuesta del handshake inesperada (falta {exc}).") from exc


def perform_handshake(app_url: str, api_key: str, *, timeout: float = 15) -> Handshake:
    url = normalize_app_url(app_url) + HANDSHAKE_PATH
    resp = httpclient.post_json(url, {}, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
    if resp.status in (401, 403):
        raise AuthError(
            "La API key no es válida o está revocada.",
            hint="Crea una nueva en la webapp (Mis API keys) y guárdala con `set-key`.",
        )
    if resp.status == 429:
        retry = resp.header("Retry-After")
        raise NetworkError(
            "El handshake ha superado el límite de tasa." + (f" Reintenta en {retry} s." if retry else "")
        )
    if resp.status != 200:
        raise NetworkError(f"La webapp respondió HTTP {resp.status} al handshake. Inténtalo más tarde.")
    return parse_handshake(resp.json())


def _org_list(hs: Handshake) -> str:
    return "\n".join(f"  - {o.describe()}" for o in hs.organizations)


def select_organization(hs: Handshake, *, requested: str | None, current: str | None) -> Organization:
    if not hs.organizations:
        raise NoOrganizationsError(
            f"El usuario {hs.email} no pertenece a ninguna organización.",
            hint="Pide a un administrador que te añada a una organización y vuelve a lanzar /baas-init.",
        )
    if requested:
        org = hs.find(requested)
        if org is None:
            raise OrgNotFoundError(f"No perteneces a la organización «{requested}». Disponibles:\n{_org_list(hs)}")
        return org
    if current and hs.find(current):
        return hs.find(current)  # type: ignore[return-value]
    if len(hs.organizations) == 1:
        return hs.organizations[0]
    raise SelectionRequiredError(
        f"Perteneces a {len(hs.organizations)} organizaciones; elige una para este proyecto:\n{_org_list(hs)}",
        hint="Vuelve a lanzar init con --org <slug>.",
    )


def kill_switch_warnings(hs: Handshake, org: Organization) -> list[str]:
    warnings = []
    if hs.global_kill_switch:
        warnings.append("Kill switch global ACTIVO: la plataforma está detenida temporalmente.")
    if org.blocked:
        warnings.append(f"La organización {org.slug} está bloqueada por la plataforma.")
    if org.paused:
        warnings.append(f"La organización {org.slug} está pausada por su administrador.")
    return warnings
