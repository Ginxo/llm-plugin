"""Registro de los subcomandos de skills como paquetes (H5): `inspect`, `publish` y `export`."""

from __future__ import annotations

import argparse


def add_skill_package_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    from .inspect import add_inspect_parser

    add_inspect_parser(sub)
