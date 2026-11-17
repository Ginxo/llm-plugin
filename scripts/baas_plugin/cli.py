"""CLI del plugin: `set-key`, `init`, `status` y `run-script`. Mensajes en español.

Códigos de salida (las skills los interpretan): 0 ok · 2 configuración (falta la clave o la URL)
· 3 hay que elegir organización · 4 sin organizaciones · 5 clave no válida · 6 red o servidor
· 7 proyecto inseguro · 8 organización ajena · 9 hash del script no coincide · 10 error del MCP.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, handshake, mcp_config, project
from .envfile import BaasPaths, load_credentials, missing_key_error, normalize_app_url, update_env, validate_api_key
from .errors import BaasError, ConfigError, MissingAppUrlError


def _p(*lines: str) -> None:
    for line in lines:
        print(line)


def key_prefix(key: str) -> str:
    return key[:12] + "…"


# --- set-key ---------------------------------------------------------------------------------


def cmd_set_key(args: argparse.Namespace) -> int:
    paths = BaasPaths.default()
    if args.stdin:
        raw = sys.stdin.readline()
    elif sys.stdin is not None and sys.stdin.isatty():
        raw = getpass.getpass("API key de BaaS-AI-IP (no se mostrará): ")
    else:
        raise ConfigError(
            "`set-key` necesita una terminal interactiva para leer la clave sin mostrarla.",
            hint="Ejecútalo en una terminal aparte, o pásala por tubería con --stdin "
            "(p. ej. desde tu gestor de contraseñas).",
        )
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
    if not args.no_mcp:
        _check_mcp(creds.api_key, org.mcp_url)
    return 0


def _check_local_config(proj: Path, org: handshake.Organization) -> None:
    env = project.read_settings_env(proj)
    if env.get("ANTHROPIC_BASE_URL") != org.proxy_anthropic_url or project.HEADER_NAME not in str(
        env.get("ANTHROPIC_CUSTOM_HEADERS", "")
    ):
        _p("AVISO: .claude/settings.local.json no coincide con la organización; lanza /baas-init.")
    if os.environ.get("CLAUDECODE") == "1":
        current = os.environ.get("ANTHROPIC_BASE_URL")
        if current != org.proxy_anthropic_url:
            _p(
                "AVISO: esta sesión de Claude Code no pasa por el proxy"
                f" (ANTHROPIC_BASE_URL={current or 'sin definir'}). Reinicia la sesión y acepta la confianza"
                " en la carpeta. En la pestaña Code de Claude Desktop"
                " el proxy por proyecto no se aplica (ver README).",
            )
        else:
            _p("Proxy activo en esta sesión: sí.")


def _check_mcp(api_key: str, mcp_url: str) -> None:
    from .mcp_client import McpClient  # import diferido: status sin MCP no lo necesita

    try:
        with McpClient(mcp_url, api_key) as client:
            client.call_tool("baas_whoami", {})
        _p("MCP `baas`: accesible.")
    except BaasError as exc:
        _p(f"MCP `baas`: ERROR · {exc.message}")


# --- main ------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baas", description="Plugin de BaaS-AI-IP para Claude Code")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("set-key", help="Guarda la API key en ~/baas-ai-ip/.env sin mostrarla")
    p.add_argument("--stdin", action="store_true", help="Lee la clave de la entrada estándar (tubería)")
    p.add_argument("--app-url", help="URL de la webapp, p. ej. https://app.<dominio>")
    p.set_defaults(func=cmd_set_key)

    p = sub.add_parser("init", help="Handshake y vinculación del proyecto a una organización")
    p.add_argument("--project", help="Raíz del proyecto (por defecto CLAUDE_PROJECT_DIR o la raíz git)")
    p.add_argument("--org", help="Slug de la organización (obligatorio si perteneces a varias)")
    p.add_argument("--app-url", help="URL de la webapp (se guarda en ~/baas-ai-ip/.env)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="Usuario, organización, roles, kill switch y AI-IP")
    p.add_argument("--project")
    p.add_argument("--no-mcp", action="store_true", help="No comprobar la conexión con el MCP")
    p.set_defaults(func=cmd_status)

    from .runner import add_run_script_parser  # el ejecutor vive en su propio módulo

    add_run_script_parser(sub)
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
        print(exc.render(), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("Cancelado.", file=sys.stderr)
        return 130
