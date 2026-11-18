"""Las skills `baas-publish` y `baas-export`: existen, pasan el lint y citan órdenes y opciones reales."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import lint_skills
from baas_plugin import cli

REPO = Path(__file__).resolve().parents[1]
SKILLS = ("baas-publish", "baas-export")


def parser_flags() -> dict[str, set[str]]:
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    return {
        name: {opt for action in p._actions for opt in action.option_strings}
        for name, p in sub.choices.items()
        if name in ("inspect", "publish", "export")
    }


def body_of(name: str) -> tuple[dict, str]:
    text = (REPO / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    _, header, body = text.split("---\n", 2)
    return yaml.safe_load(header), body


@pytest.mark.parametrize("name", SKILLS)
def test_skill_passes_the_repository_lint(name: str) -> None:
    assert lint_skills.lint(REPO) == []
    meta, _ = body_of(name)
    assert meta["name"] == name
    assert meta["disable-model-invocation"] is True  # tienen efectos: solo si el usuario las lanza
    assert len(meta["description"]) < 1536


@pytest.mark.parametrize("name", SKILLS)
def test_skill_tells_the_agent_to_stop_on_denials(name: str) -> None:
    _, body = body_of(name)
    assert "AUTHORIZATION_DENIED" in body
    assert "KILL_SWITCH_ACTIVE" in body
    assert "detente" in body.lower()


@pytest.mark.parametrize("name", SKILLS)
def test_every_option_the_skill_cites_exists(name: str) -> None:
    flags = parser_flags()
    known = set().union(*flags.values())
    _, body = body_of(name)
    cited = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", body))
    assert cited - known == set(), f"opciones citadas que no existen: {cited - known}"


def test_publish_skill_follows_the_interview() -> None:
    meta, body = body_of("baas-publish")
    lowered = body.lower()
    for needle in (
        "inspect",
        "--dry-run",
        "--yes",
        "--include-scripts",
        "--knowledge",
        "--roles",
        "--rewrite-paths",
        "baas_whoami",
        "baas_list_files",
        "baas_search_knowledge",
        "baas_list_skills",
        "¿publico?",
        "lote",
    ):
        assert needle in lowered, needle
    # el ensayo se puede lanzar sin pedir permiso; publicar de verdad, no
    assert "publish * --dry-run" in meta["allowed-tools"]
    assert "--yes" not in meta["allowed-tools"]


def test_export_skill_uses_the_export_command() -> None:
    meta, body = body_of("baas-export")
    assert 'baas.py" export' in body
    assert "export *" in meta["allowed-tools"]


def test_skills_are_written_in_spanish_and_concise() -> None:
    for name in SKILLS:
        _, body = body_of(name)
        assert len(body.splitlines()) < 120
        assert "Si el MCP dice que no" in body
