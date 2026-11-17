#!/usr/bin/env python3
"""Lint del plugin: manifiestos, skills y comandos (cabecera válida y campos obligatorios).

Uso: uv run python tools/lint_skills.py [--root DIR] [--expect-version X.Y.Z]
Es complementario a `claude plugin validate`, que no está disponible en CI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
SCRIPT_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+)")
SKILL_FIELDS = {
    "name", "description", "when_to_use", "argument-hint", "arguments", "disable-model-invocation",
    "user-invocable", "allowed-tools", "disallowed-tools", "model", "effort", "context", "agent",
    "background", "hooks", "paths", "shell", "metadata", "license", "compatibility",
}  # fmt: skip
BOOL_FIELDS = {"disable-model-invocation", "user-invocable", "background"}
MAX_DESCRIPTION = 1536


def frontmatter(path: Path, errors: list[str]) -> tuple[dict[str, Any] | None, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        errors.append(f"{path}: la cabecera YAML debe empezar en la primera línea con ---")
        return None, text
    end = text.find("\n---", 4)
    if end == -1:
        errors.append(f"{path}: cabecera YAML sin cierre ---")
        return None, text
    try:
        data = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        errors.append(f"{path}: YAML no válido: {exc}")
        return None, text
    if not isinstance(data, dict):
        errors.append(f"{path}: la cabecera YAML debe ser un mapa")
        return None, text
    return data, text[end + 4 :]


def check_refs(root: Path, path: Path, text: str, errors: list[str]) -> None:
    for ref in sorted(set(SCRIPT_REF.findall(text))):
        if not (root / ref).exists():
            errors.append(f"{path}: referencia a ${{CLAUDE_PLUGIN_ROOT}}/{ref}, que no existe")


def lint_skill(root: Path, skill_md: Path, errors: list[str]) -> None:
    data, _ = frontmatter(skill_md, errors)
    if data is None:
        return
    dirname = skill_md.parent.name
    name = data.get("name")
    if name != dirname:
        errors.append(f"{skill_md}: `name` ({name!r}) debe coincidir con el directorio ({dirname!r})")
    if not KEBAB.match(dirname):
        errors.append(f"{skill_md}: el nombre {dirname!r} debe estar en kebab-case")
    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errors.append(f"{skill_md}: falta `description`")
    elif len(desc) + len(str(data.get("when_to_use") or "")) > MAX_DESCRIPTION:
        errors.append(f"{skill_md}: `description` + `when_to_use` supera {MAX_DESCRIPTION} caracteres")
    for key in sorted(set(data) - SKILL_FIELDS):
        errors.append(f"{skill_md}: campo desconocido `{key}` (Claude Code lo ignoraría)")
    for key in sorted(BOOL_FIELDS & set(data)):
        if not isinstance(data[key], bool):
            errors.append(f"{skill_md}: `{key}` debe ser booleano")
    check_refs(root, skill_md, skill_md.read_text(encoding="utf-8"), errors)


def lint_command(root: Path, cmd: Path, errors: list[str]) -> None:
    data, _ = frontmatter(cmd, errors)
    if data is None:
        return
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        errors.append(f"{cmd}: falta `description`")
    for key in sorted(set(data) - (SKILL_FIELDS - {"name", "paths"})):
        errors.append(f"{cmd}: campo desconocido `{key}`")
    check_refs(root, cmd, cmd.read_text(encoding="utf-8"), errors)


def load_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"{path}: {exc}")
        return {}
    if not isinstance(data, dict):
        errors.append(f"{path}: debe ser un objeto JSON")
        return {}
    return data


def lint_manifests(root: Path, errors: list[str], expect_version: str | None) -> None:
    plugin = load_json(root / ".claude-plugin" / "plugin.json", errors)
    market = load_json(root / ".claude-plugin" / "marketplace.json", errors)
    name = plugin.get("name")
    if not isinstance(name, str) or not KEBAB.match(name):
        errors.append("plugin.json: `name` obligatorio y en kebab-case")
    version = plugin.get("version")
    if not isinstance(version, str) or not SEMVER.match(version):
        errors.append("plugin.json: `version` obligatoria con formato X.Y.Z")
    elif expect_version and version != expect_version:
        errors.append(f"plugin.json: la versión {version} no coincide con la etiqueta {expect_version}")
    if not plugin.get("description"):
        errors.append("plugin.json: falta `description`")
    if not isinstance(plugin.get("author"), dict) or not plugin["author"].get("name"):
        errors.append("plugin.json: falta `author.name`")
    if not market.get("name") or not isinstance(market.get("owner"), dict) or not market["owner"].get("name"):
        errors.append("marketplace.json: `name` y `owner.name` son obligatorios")
    entries = market.get("plugins")
    if not isinstance(entries, list) or not entries:
        errors.append("marketplace.json: `plugins` debe ser una lista no vacía")
        return
    entry = next((e for e in entries if isinstance(e, dict) and e.get("name") == name), None)
    if entry is None:
        errors.append(f"marketplace.json: no hay entrada para el plugin {name!r} (los nombres deben coincidir)")
    elif entry.get("source") not in ("./", "."):
        errors.append('marketplace.json: el plugin está en la raíz del repositorio; `source` debe ser "./"')
    elif "version" in entry:
        errors.append("marketplace.json: no repitas `version` en la entrada (manda la de plugin.json)")


def lint(root: Path, expect_version: str | None = None) -> list[str]:
    errors: list[str] = []
    lint_manifests(root, errors, expect_version)
    skills = sorted((root / "skills").glob("*/SKILL.md"))
    if not skills:
        errors.append("skills/: no hay ninguna skill")
    for skill in skills:
        lint_skill(root, skill, errors)
    for cmd in sorted((root / "commands").glob("*.md")):
        lint_command(root, cmd, errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--expect-version", help="Versión esperada en plugin.json (p. ej. la de la etiqueta v*)")
    args = parser.parse_args(argv)
    errors = lint(Path(args.root), args.expect_version)
    for error in errors:
        print(f"ERROR {error}")
    print(f"lint de skills: {'OK' if not errors else f'{len(errors)} errores'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
