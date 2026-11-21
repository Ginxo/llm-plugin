"""`baas.py setup <herramienta>`: conecta una herramienta de IA que no es Claude Code con el MCP de la organización.

Cada herramienta es un dato (`ClientTarget`) en `TARGETS`: dónde está su `mcp_config.json`, cómo se llaman los campos
del servidor remoto y qué hay que avisar. Añadir una herramienta con el mismo formato es registrar otro
`ClientTarget`; el flujo (clave desde `~/baas-ai-ip/.env`, handshake, organización, comprobaciones de seguridad y
escritura atómica con 0600) es común. Un formato distinto (YAML, TOML) necesitará su propio escritor.

Criterio de seguridad (el mismo que `init`): la clave nunca se imprime ni viaja en argumentos; el fichero que la
contiene se añade a `.gitignore`, se niega a escribirse si está versionado y queda con permisos 0600.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import handshake, project
from .envfile import BaasPaths, atomic_write, load_credentials, read_env
from .errors import ConfigError, ProjectSafetyError

SERVER_NAME = "baas"
KEY_PLACEHOLDER = "<API key>"


@dataclass(frozen=True)
class ClientTarget:
    """Cómo se registra el MCP de la organización en una herramienta (solo datos)."""

    name: str  # subcomando: `setup <name>`
    label: str  # nombre para la persona
    config_path: tuple[str, ...]  # relativo a la raíz del proyecto; piezas, no una ruta POSIX
    url_key: str  # campo de la URL del servidor remoto
    servers_key: str = "mcpServers"
    headers_key: str = "headers"
    auth_scheme: str = "Bearer"
    # Campos de otro transporte (stdio, alias de la URL…): la entrada solo puede tener uno
    drop_keys: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()  # avisos de lo que esta conexión no cubre
    doc: str = "docs/antigravity.md"

    @property
    def config_entry(self) -> str:
        """La entrada de `.gitignore` (siempre con `/`, también en Windows)."""
        return "/".join(self.config_path)


ANTIGRAVITY = ClientTarget(
    name="antigravity",
    label="Antigravity (extensión de VS Code y CLI `agy`)",
    config_path=(".agents", "mcp_config.json"),
    url_key="serverUrl",
    drop_keys=("url", "httpUrl", "command", "args", "env", "cwd"),
    notes=(
        "Antigravity no pasa por el proxy de BaaS-AI-IP: solo conecta con el cerebro (el MCP). Sus conversaciones"
        " van a Google sin anonimizar (sin AI-IP) y no aparecen en la webapp.",
    ),
)

TARGETS: dict[str, ClientTarget] = {t.name: t for t in (ANTIGRAVITY,)}


# --- mcp_config.json -----------------------------------------------------------------------------


def _invalid(path: Path, why: str) -> ProjectSafetyError:
    return ProjectSafetyError(
        f"{path} {why}; no lo toco.",
        hint="Corrígelo (o muévelo) y vuelve a lanzar el comando: así no se pierde nada de lo que tienes ahí.",
    )


def read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        raise _invalid(path, "no es JSON válido") from None
    if not isinstance(data, dict):
        raise _invalid(path, "no contiene un objeto JSON")
    return data


def server_entry(target: ClientTarget, existing: Any, mcp_url: str, api_key: str) -> dict[str, Any]:
    """La entrada `baas`: la URL y la cabecera de autorización, más lo que la persona ya tuviera en la suya."""
    if existing is not None and not isinstance(existing, dict):
        raise ConfigError(f"La entrada `{SERVER_NAME}` existente no es un objeto JSON.")
    old = existing or {}
    old_headers = old.get(target.headers_key)
    if old_headers is not None and not isinstance(old_headers, dict):
        raise ConfigError(f"`{target.headers_key}` de la entrada `{SERVER_NAME}` no es un objeto JSON.")
    headers = {k: v for k, v in (old_headers or {}).items() if k.lower() != "authorization"}
    headers["Authorization"] = f"{target.auth_scheme} {api_key}"
    entry: dict[str, Any] = {target.url_key: mcp_url, target.headers_key: headers}
    for key, value in old.items():
        if key not in entry and key not in target.drop_keys:
            entry[key] = value
    return entry


def merged_config(
    target: ClientTarget, config: dict[str, Any], mcp_url: str, api_key: str, path: Path
) -> dict[str, Any]:
    servers = config.get(target.servers_key)
    if servers is not None and not isinstance(servers, dict):
        raise _invalid(path, f"tiene `{target.servers_key}` y no es un objeto")
    servers = dict(servers or {})
    try:
        servers[SERVER_NAME] = server_entry(target, servers.get(SERVER_NAME), mcp_url, api_key)
    except ConfigError as exc:
        raise _invalid(path, exc.message.removesuffix(".").lower()) from None
    return {**config, target.servers_key: servers}


def _render(config: dict[str, Any]) -> str:
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def _restrict(path: Path) -> bool:
    """Deja el fichero en 0600 si estaba más abierto (en Windows los permisos POSIX no se aplican)."""
    if os.name != "posix" or not path.exists():
        return False
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)
        return True
    return False


# --- el comando ------------------------------------------------------------------------------------


def _p(*lines: str) -> None:
    for line in lines:
        print(line)


def _linked_org(proj: Path) -> str | None:
    """La organización con la que `/baas-init` vinculó el proyecto, si lo hizo (si no, o si está dañado, ninguna)."""
    try:
        binding = project.read_binding(proj)
    except ProjectSafetyError:
        return None
    org = (binding or {}).get("org")
    return org if isinstance(org, str) else None


def cmd_setup(args: argparse.Namespace) -> int:
    from . import cli  # import diferido: cli registra este comando

    target: ClientTarget = TARGETS[args.tool]
    proj = (
        Path(args.project).expanduser().resolve() if args.project else project.resolve_project_dir(None, cwd=Path.cwd())
    )
    if not proj.is_dir():
        raise ConfigError(f"El proyecto {proj} no existe.", hint="Indica la carpeta del proyecto con --project.")

    paths = BaasPaths.default()
    creds = load_credentials(paths)  # sin clave: error de configuración antes de tocar nada
    values, _ = read_env(paths)
    app_url = cli._resolve_app_url(args.app_url, values)
    hs = cli._handshake(app_url, creds.api_key)
    org = handshake.select_organization(hs, requested=args.org, current=_linked_org(proj))

    config_file = proj.joinpath(*target.config_path)
    new_config = merged_config(target, read_config(config_file), org.mcp_url, creds.api_key, config_file)
    content = _render(new_config)

    if args.print_only:
        shown = merged_config(target, {}, org.mcp_url, KEY_PLACEHOLDER, config_file)
        shown[target.servers_key][SERVER_NAME][target.headers_key]["Authorization"] = (
            f"{target.auth_scheme} {KEY_PLACEHOLDER}"
        )
        _p(
            f"Lo que `setup {target.name}` escribiría en {config_file} (sin la clave):",
            "",
            _render(shown).rstrip("\n"),
            "",
            f"«{KEY_PLACEHOLDER}» se sustituye por tu API key, leída de {paths.env_file}; no se imprime ni se pasa",
            "como argumento. Se conservan los demás servidores y ajustes del fichero.",
        )
        return 0

    project.check_not_tracked(proj, (target.config_entry,), command=f"`setup {target.name}`")
    added = project.ensure_gitignore(proj, (target.config_entry,))
    config_file.parent.mkdir(parents=True, exist_ok=True)
    changed = not config_file.exists() or config_file.read_text(encoding="utf-8") != content
    if changed:
        atomic_write(config_file, content)
    tightened = _restrict(config_file)

    _p(
        f"{target.label}: proyecto {proj}, organización {org.describe()}.",
        f"MCP `{SERVER_NAME}`: {org.mcp_url}",
        f"{config_file}: {'actualizado' if changed else 'sin cambios'} (permisos 0600"
        f"{', corregidos' if tightened else ''}).",
    )
    if added:
        _p(f"Añadido a .gitignore: {', '.join(added)}.")
    for warning in handshake.kill_switch_warnings(hs, org):
        _p(f"AVISO: {warning}")
    _p(
        f"AVISO: tu API key queda en claro en {config_file}: {target.label.split(' (')[0]} no admite variables de"
        " entorno ni un comando para el secreto. No lo subas a git ni lo compartas; si se expone, revócala en la"
        " webapp (Mis API keys) y vuelve a lanzar este comando.",
        *(f"AVISO: {note}" for note in target.notes),
        f"Guía de uso y comprobación: {target.doc}",
    )
    return 0


def add_setup_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "setup", help="Conecta otra herramienta de IA con el MCP de la organización (p. ej. antigravity)"
    )
    tools = p.add_subparsers(dest="tool", required=True, metavar="<herramienta>")
    for target in TARGETS.values():
        t = tools.add_parser(target.name, help=target.label)
        t.add_argument("--project", help="Raíz del proyecto (por defecto, la raíz git o el directorio actual)")
        t.add_argument("--org", help="Slug de la organización (obligatorio si perteneces a varias)")
        t.add_argument("--app-url", help="URL de la webapp (por defecto, la guardada por /baas-init)")
        t.add_argument(
            "--print",
            dest="print_only",
            action="store_true",
            help="Imprime lo que escribiría (sin la clave) y no escribe nada",
        )
        t.set_defaults(func=cmd_setup)
