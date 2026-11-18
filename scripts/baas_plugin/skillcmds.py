"""Registro de los subcomandos de skills como paquetes (H5): `inspect`, `publish` y `export`."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from . import project
from .envfile import BaasPaths, load_credentials
from .errors import ConfigError


def connection(project_arg: str | None) -> tuple[str, str]:
    """`(URL del MCP, API key)` del proyecto vinculado (`/baas-init`)."""
    creds = load_credentials(BaasPaths.default())
    proj = project.resolve_project_dir(project_arg or os.environ.get("CLAUDE_PROJECT_DIR"), cwd=Path.cwd())
    binding = project.read_binding(proj)
    if not binding or not binding.get("mcp_url"):
        raise ConfigError(f"El proyecto {proj} no está vinculado a BaaS-AI-IP: lanza /baas-init.")
    return str(binding["mcp_url"]), creds.api_key


def add_skill_package_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    from .inspect import add_inspect_parser
    from .publish import add_publish_parser

    add_inspect_parser(sub)
    add_publish_parser(sub)
