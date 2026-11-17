from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import lint_skills

REPO = Path(__file__).resolve().parents[1]


def test_repository_passes_lint() -> None:
    assert lint_skills.lint(REPO) == []


def test_expected_version_must_match() -> None:
    assert any("no coincide" in e for e in lint_skills.lint(REPO, expect_version="9.9.9"))


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    dst = tmp_path / "plugin"
    shutil.copytree(REPO / ".claude-plugin", dst / ".claude-plugin")
    shutil.copytree(REPO / "skills", dst / "skills")
    shutil.copytree(REPO / "scripts", dst / "scripts")
    return dst


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("sin cabecera\n", "primera línea"),
        ("---\nname: rota\n", "sin cierre"),
        ("---\nname: [rota\n---\n", "YAML no válido"),
        ("---\nname: otra\ndescription: x\n---\n", "debe coincidir"),
        ("---\nname: mala\n---\n", "falta `description`"),
        ("---\nname: mala\ndescription: x\ninventado: 1\n---\n", "campo desconocido"),
        ("---\nname: mala\ndescription: x\nuser-invocable: quizá\n---\n", "booleano"),
        ("---\nname: mala\ndescription: x\n---\n${CLAUDE_PLUGIN_ROOT}/scripts/no.py\n", "no existe"),
    ],
)
def test_bad_skills_are_reported(copy: Path, content: str, expected: str) -> None:
    (copy / "skills" / "mala").mkdir()
    (copy / "skills" / "mala" / "SKILL.md").write_text(content)
    errors = lint_skills.lint(copy)
    assert any(expected in e for e in errors), errors


def test_marketplace_entry_must_match_plugin_name(copy: Path) -> None:
    path = copy / ".claude-plugin" / "marketplace.json"
    path.write_text(path.read_text().replace('"name": "baas",', '"name": "otro",'))
    assert any("no hay entrada" in e for e in lint_skills.lint(copy))


def test_cli_exit_codes(copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert lint_skills.main(["--root", str(copy)]) == 0
    (copy / "skills" / "baas-brain" / "SKILL.md").write_text("roto")
    assert lint_skills.main(["--root", str(copy)]) == 1
    assert "ERROR" in capsys.readouterr().out
