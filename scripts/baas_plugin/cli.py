"""CLI del plugin: `set-key`, `init`, `status`, `desktop-config`, `run-script`, `sync`, `inspect`, `publish` y `export`. Mensajes en español.

Códigos de salida (las skills los interpretan): 0 ok · 2 configuración (falta la clave o la URL)
· 3 hay que elegir organización · 4 sin organizaciones · 5 clave no válida · 6 red o servidor
· 7 proyecto inseguro · 8 organización ajena · 9 hash de un fichero no coincide · 10 error del MCP
· 11 paquete inválido (ruta insegura) · 12 sincronización incompleta · 13 directorio ajeno.

`run-script` es la excepción: devuelve **íntegro** el código de salida del script, que puede ser
cualquiera (también 9 o 10). Sus errores propios salen, además, con una línea `BAAS_ERROR=<código>` en
stderr (p. ej. `SCRIPT_HASH_MISMATCH`, `AUTHORIZATION_DENIED`, `KILL_SWITCH_ACTIVE`): sin esa línea, el
código de salida es el del script.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import baas_guard

from . import __version__, desktop_config, handshake, mcp_config, project, sync
from .claude_home import ClaudePaths
from .envfile import (
    BaasPaths,
    load_credentials,
    missing_key_error,
    normalize_app_url,
    read_env,
    update_env,
    validate_api_key,
)
from .errors import BaasError, ConfigError, MissingAppUrlError


def _p(*lines: str) -> None:
    for line in lines:
        print(line)


def key_prefix(key: str) -> str:
    return key[:12] + "…"


# --- set-key ---------------------------------------------------------------------------------


ANTHROPIC_KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$")


def validate_anthropic_key(value: str) -> str:
    key = value.strip()
    if not ANTHROPIC_KEY_RE.match(key):
        raise ConfigError(
            "La clave no tiene el formato de una API key de Anthropic Console (`sk-ant-…`).",
            hint="Créala en console.anthropic.com (API keys). La suscripción de claude.ai no sirve en modo gateway.",
        )
    return key


def cmd_set_key(args: argparse.Namespace) -> int:
    paths = BaasPaths.default()
    label = "API key de Anthropic Console" if args.anthropic else "API key de BaaS-AI-IP"
    if args.stdin:
        raw = sys.stdin.readline()
    elif sys.stdin is not None and sys.stdin.isatty():
        raw = getpass.getpass(f"{label} (no se mostrará): ")
    else:
        raise ConfigError(
            "`set-key` necesita una terminal interactiva para leer la clave sin mostrarla.",
            hint="Ejecútalo en una terminal aparte, o pásala por tubería con --stdin "
            "(p. ej. desde tu gestor de contraseñas).",
        )
    if args.anthropic:
        key = validate_anthropic_key(raw)
        warnings = update_env(paths, {"ANTHROPIC_API_KEY": key})
        _p(*warnings, f"{label} {key[:10]}… guardada en {paths.env_file} (permisos 0600).")
        _p("La usa solo el helper de Claude Desktop en modo gateway (ver `desktop-config`).")
        return 0
    key = validate_api_key(raw)
    updates = {"BAAS_API_KEY": key}
    if args.app_url:
        updates["BAAS_APP_URL"] = normalize_app_url(args.app_url)
    warnings = update_env(paths, updates)
    _p(*warnings, f"API key {key_prefix(key)} guardada en {paths.env_file} (permisos 0600).")
    if args.app_url:
        _p(f"URL de la webapp: {updates['BAAS_APP_URL']}")
    _p("Vuelve a Claude Code y lanza /baas-init.")
    return 0


# --- init ------------------------------------------------------------------------------------


def _project_dir(args: argparse.Namespace) -> Path:
    return project.resolve_project_dir(args.project or os.environ.get("CLAUDE_PROJECT_DIR"), cwd=Path.cwd())


def cmd_init(args: argparse.Namespace) -> int:
    paths = BaasPaths.default()
    if args.app_url:
        update_env(paths, {"BAAS_APP_URL": normalize_app_url(args.app_url)})
    if not paths.env_file.exists():
        raise missing_key_error(paths)
    creds = load_credentials(paths)
    if not creds.app_url:
        raise MissingAppUrlError(
            "Falta la URL de la webapp de BaaS-AI-IP (BAAS_APP_URL en ~/baas-ai-ip/.env).",
            hint="Lanza de nuevo init con --app-url https://app.<dominio> (no es un secreto).",
        )
    proj = _project_dir(args)
    project.check_not_tracked(proj)
    hs = handshake.perform_handshake(creds.app_url, creds.api_key)
    current = (project.read_binding(proj) or {}).get("org")
    org = handshake.select_organization(hs, requested=args.org, current=current)

    project.write_settings_local(proj, org.proxy_anthropic_url, creds.api_key)
    project.write_binding(
        proj,
        {
            "org": org.slug,
            "org_name": org.name,
            "user_email": hs.email,
            "app_url": creds.app_url,
            "mcp_url": org.mcp_url,
            "proxy_anthropic_url": org.proxy_anthropic_url,
            "proxy_openai_url": org.proxy_openai_url,
            "bound_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if current != org.slug
            else (project.read_binding(proj) or {}).get("bound_at"),
            "plugin_version": __version__,
        },
    )
    added = project.ensure_gitignore(proj)
    helper = mcp_config.install_headers_helper(paths)
    reg = mcp_config.register_mcp_server(proj, org.mcp_url, mcp_config.helper_command(helper))

    action = "vinculado" if current != org.slug else "actualizado"
    _p(
        f"Proyecto {proj} {action}: organización {org.describe()}.",
        f"Usuario: {hs.email} ({hs.platform_role}).",
        f"Proxy de Anthropic: {org.proxy_anthropic_url} (en .claude/settings.local.json).",
        f"MCP `baas`: {org.mcp_url}.",
    )
    if added:
        _p(f"Añadido a .gitignore: {', '.join(added)}.")
    if reg.registered:
        _p("Servidor MCP `baas` registrado en Claude Code (ámbito local de este proyecto).")
    else:
        _p(
            f"AVISO: no se pudo registrar el MCP automáticamente ({reg.detail}).",
            "Ejecuta en una terminal (no contiene la API key):",
            f"  {reg.manual_command}",
        )
    for warning in handshake.kill_switch_warnings(hs, org):
        _p(f"AVISO: {warning}")
    _p(
        "Siguiente paso: reinicia Claude Code en este proyecto (acepta la confianza en la carpeta si la pide)"
        " para cargar el MCP `baas` y el proxy; comprueba con /baas-status.",
        "Después, /baas-sync trae las skills de tu organización (también se sincronizan solas al iniciar y"
        " cerrar la sesión).",
    )
    return 0


# --- status ----------------------------------------------------------------------------------


def _yes(flag: bool) -> str:
    return "ACTIVO" if flag else "no"


def cmd_status(args: argparse.Namespace) -> int:
    paths = BaasPaths.default()
    if not paths.env_file.exists():
        raise missing_key_error(paths)
    creds = load_credentials(paths)
    proj = _project_dir(args)
    binding = project.read_binding(proj)
    _p(f"API key: {key_prefix(creds.api_key)} en {paths.env_file} · webapp: {creds.app_url or 'sin configurar'}")
    if not creds.app_url:
        raise MissingAppUrlError("Falta BAAS_APP_URL en ~/baas-ai-ip/.env; lanza /baas-init con --app-url.")
    hs = handshake.perform_handshake(creds.app_url, creds.api_key)
    _p(
        f"Usuario: {hs.email} · rol de plataforma: {hs.platform_role}",
        f"Kill switch global: {_yes(hs.global_kill_switch)}",
    )
    if binding is None:
        _p(f"El proyecto {proj} no está vinculado a ninguna organización: lanza /baas-init.")
        _p("Tus organizaciones:", *(f"  - {o.describe()}" for o in hs.organizations))
        return 0
    org = hs.find(str(binding.get("org")))
    if org is None:
        _p(
            f"AVISO: el proyecto está vinculado a «{binding.get('org')}», pero ya no tienes acceso a esa"
            " organización. Lanza /baas-init para elegir otra.",
        )
        return 0
    roles = ", ".join(org.functional_roles) or "ninguno"
    _p(
        f"Organización del proyecto: {org.slug} · {org.name}",
        f"Rol de gobierno: {org.governance_role} · roles funcionales: {roles}",
        f"Bloqueo de plataforma: {_yes(org.blocked)} · pausa de la organización: {_yes(org.paused)}"
        + (" (pausada por su administrador)" if org.paused else ""),
        f"AI-IP: {'activado' if org.ai_ip_enabled else 'desactivado (bypass)'}",
    )
    for warning in handshake.kill_switch_warnings(hs, org):
        _p(f"AVISO: {warning}")
    _check_local_config(proj, org)
    claude = sync.claude_paths(args)
    if args.no_mcp:
        _p(*sync.status_lines(binding, proj, claude))
    else:
        _check_mcp(creds.api_key, org.mcp_url, binding, proj, claude)
    return 0


def _check_local_config(proj: Path, org: handshake.Organization) -> None:
    env = project.read_settings_env(proj)
    if env.get("ANTHROPIC_BASE_URL") != org.proxy_anthropic_url or project.HEADER_NAME not in str(
        env.get("ANTHROPIC_CUSTOM_HEADERS", "")
    ):
        _p("AVISO: .claude/settings.local.json no coincide con la organización; lanza /baas-init.")
    if os.environ.get("CLAUDECODE") == "1":
        # Mismo diagnóstico que la guardia de los hooks (scripts/baas_guard.py).
        problem = baas_guard.diagnose(project.read_binding(proj), dict(os.environ))
        if problem:
            _p(f"AVISO: {problem.removeprefix('BaaS-AI-IP: ')}")
        else:
            _p("Proxy activo en esta sesión: sí.")


# --- desktop-config --------------------------------------------------------------------------


def cmd_desktop_config(args: argparse.Namespace) -> int:
    paths = BaasPaths.default()
    if not paths.env_file.exists():
        raise missing_key_error(paths)
    creds = load_credentials(paths)
    if not creds.app_url:
        raise MissingAppUrlError("Falta BAAS_APP_URL en ~/baas-ai-ip/.env; lanza /baas-init con --app-url.")
    proj = _project_dir(args)
    current = (project.read_binding(proj) or {}).get("org")
    hs = handshake.perform_handshake(creds.app_url, creds.api_key)
    org = handshake.select_organization(hs, requested=args.org, current=current)
    helper = desktop_config.install_credentials_helper(paths)
    config = desktop_config.gateway_config(org.proxy_anthropic_url, helper)
    values, _ = read_env(paths)
    _p(
        f"Claude Desktop en modo gateway hacia el proxy de la organización {org.describe()}.",
        "",
        "Ten en cuenta:",
        "  - Es GLOBAL a Claude Desktop: todas sus sesiones pasarán por esta organización.",
        "  - Desktop deja de usar tu suscripción de claude.ai: el proxy reenvía a Anthropic la API key"
        " de Anthropic Console guardada en ~/baas-ai-ip/.env (ANTHROPIC_API_KEY).",
        "  - El soporte del modo gateway en Desktop para Linux no está confirmado.",
        "",
        "Pasos:",
        "  1. Claude Desktop → Help → Troubleshooting → Enable Developer Mode.",
        "  2. Developer → Configure Third-Party Inference… e introduce estos valores"
        " (o distribúyelos por MDM / fichero de configuración):",
        "",
        json.dumps(config, indent=2, ensure_ascii=False),
        "",
        "  3. Reinicia Claude Desktop y elige el modo de terceros (no «Sign in to Claude.ai»).",
        "  4. En un proyecto vinculado, /baas-status debe decir «Proxy activo en esta sesión: sí».",
        "",
        f"Helper de credenciales instalado en {helper} (lee las claves de {paths.env_file}; no contiene secretos).",
    )
    if not values.get("ANTHROPIC_API_KEY"):
        _p(
            "AVISO: falta ANTHROPIC_API_KEY en ~/baas-ai-ip/.env. Guárdala en una terminal, sin pasar por el chat:",
            f"  python3 {Path(__file__).resolve().parent.parent / 'baas.py'} set-key --anthropic",
        )
    for warning in handshake.kill_switch_warnings(hs, org):
        _p(f"AVISO: {warning}")
    return 0


def _check_mcp(
    api_key: str, mcp_url: str, binding: dict | None = None, proj: Path | None = None, claude: ClaudePaths | None = None
) -> None:
    from .mcp_client import McpClient  # import diferido: status sin MCP no lo necesita

    client = None
    try:
        with McpClient(mcp_url, api_key) as client:
            try:
                client.call_tool("baas_whoami", {})
                _p("MCP `baas`: accesible.")
            except BaasError as exc:
                _p(f"MCP `baas`: ERROR · {exc.message}")
            if proj is not None and claude is not None:  # skills sincronizadas, desfasadas y revocadas (H5)
                _p(*sync.status_lines(binding, proj, claude, client))
    except BaasError as exc:
        _p(f"MCP `baas`: ERROR · {exc.message}")
        if proj is not None and claude is not None and client is None:
            _p(*sync.status_lines(binding, proj, claude))


# --- main ------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baas", description="Plugin de BaaS-AI-IP para Claude Code")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    from .skillcmds import add_skill_package_parsers  # inspect, publish y export (H5)

    add_skill_package_parsers(sub)

    p = sub.add_parser("set-key", help="Guarda la API key en ~/baas-ai-ip/.env sin mostrarla")
    p.add_argument("--stdin", action="store_true", help="Lee la clave de la entrada estándar (tubería)")
    p.add_argument("--app-url", help="URL de la webapp, p. ej. https://app.<dominio>")
    p.add_argument(
        "--anthropic",
        action="store_true",
        help="Guarda la API key de Anthropic Console (ANTHROPIC_API_KEY) para Claude Desktop en modo gateway",
    )
    p.set_defaults(func=cmd_set_key)

    p = sub.add_parser("init", help="Handshake y vinculación del proyecto a una organización")
    p.add_argument("--project", help="Raíz del proyecto (por defecto CLAUDE_PROJECT_DIR o la raíz git)")
    p.add_argument("--org", help="Slug de la organización (obligatorio si perteneces a varias)")
    p.add_argument("--app-url", help="URL de la webapp (se guarda en ~/baas-ai-ip/.env)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="Usuario, organización, roles, kill switch, AI-IP y skills sincronizadas")
    p.add_argument("--project")
    p.add_argument("--no-mcp", action="store_true", help="No comprobar la conexión con el MCP")
    sync.add_claude_home_args(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("desktop-config", help="Configuración de Claude Desktop en modo gateway hacia el proxy")
    p.add_argument("--project", help="Proyecto cuya organización se toma por defecto")
    p.add_argument("--org", help="Slug de la organización (obligatorio si perteneces a varias y no hay proyecto)")
    p.set_defaults(func=cmd_desktop_config)

    from .runner import add_run_script_parser  # el ejecutor vive en su propio módulo

    add_run_script_parser(sub)
    sync.add_sync_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    script_args: list[str] = []
    if "--" in argv:  # lo que sigue a `--` es para el script de run-script
        cut = argv.index("--")
        argv, script_args = argv[:cut], argv[cut + 1 :]
    args = build_parser().parse_args(argv)
    args.script_args = script_args
    try:
        return int(args.func(args))
    except BaasError as exc:
        if args.command == "run-script":
            # Un script puede salir con cualquier código (también 9 o 10): esta línea marca un error del plugin.
            print(f"BAAS_ERROR={exc.error_code}", file=sys.stderr)
        print(exc.render(), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("Cancelado.", file=sys.stderr)
        return 130
