from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

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


# --- skill /baas-init: guiada, en llano y con permisos mínimos -----------------------------------------------

INIT_SKILL = REPO / "skills" / "baas-init" / "SKILL.md"
STATUS_SKILL = REPO / "skills" / "baas-status" / "SKILL.md"
# Lo que Claude le dice a la persona va entre comillas angulares «…»; ahí no puede haber rutas ni jerga.
SHOWN = re.compile(r"«([^»]*)»")
FORBIDDEN_IN_SHOWN = (
    "~/baas-ai-ip", ".env", ".claude", "baas.py", "set-key", "settings", "proxy", "handshake", "hook",
    "api key", "apikey", "token", "git ", "--", "exit", "código de salida", "kill switch", "headers",
)  # fmt: skip


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text[4 : text.find("\n---", 4)])


def _shown_texts(path: Path) -> list[str]:
    return SHOWN.findall(path.read_text(encoding="utf-8"))


def _allowed_tools(path: Path) -> list[str]:
    raw = _frontmatter(path).get("allowed-tools", [])
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return re.findall(r"Bash\(.*?\)(?=\s+Bash\(|\s*$)", str(raw))


def test_init_skill_covers_every_approval_and_error_code() -> None:
    text = INIT_SKILL.read_text(encoding="utf-8")
    for marker in (
        "BAAS_APPROVAL_PENDING", "BAAS_APPROVAL_WAITING", "BAAS_APPROVAL_DENIED", "BAAS_APPROVAL_EXPIRED",
        "BAAS_APP_URL_MISSING", "BAAS_ORG_SELECTION_REQUIRED", "BAAS_NO_ORGANIZATIONS", "BAAS_NETWORK_ERROR",
        "BAAS_PROJECT_UNSAFE", "BAAS_ORG_NOT_FOUND",
    ):  # fmt: skip
        assert marker in text, marker
    for code in ("`20`", "`21`", "`22`", "`23`"):
        assert code in text, code
    assert "init --wait" in text
    assert "--app-url" in text
    assert "AskUserQuestion" in text


def test_init_skill_never_uses_set_key_or_local_secrets() -> None:
    text = INIT_SKILL.read_text(encoding="utf-8")
    assert "set-key" not in text
    assert "terminal aparte" not in text
    assert "BAAS_KEY_MISSING" not in text  # init ya renueva la clave sola
    assert "BAAS_AUTH_ERROR" not in text


@pytest.mark.parametrize("path", [INIT_SKILL, STATUS_SKILL], ids=["baas-init", "baas-status"])
def test_text_shown_to_the_user_is_plain(path: Path) -> None:
    shown = _shown_texts(path)
    assert shown, "la skill debe dar a Claude frases literales para la persona"
    for sentence in shown:
        low = sentence.lower()
        for word in FORBIDDEN_IN_SHOWN:
            assert word not in low, f"{path.parent.name}: «{sentence}» contiene {word!r}"


def test_init_skill_has_narrow_allowed_tools() -> None:
    tools = _allowed_tools(INIT_SKILL)
    assert tools[0] == 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" init *)'
    # Lo único más que puede ejecutar: sacar del índice de git los dos ficheros locales, uno a uno.
    assert sorted(tools[1:]) == [
        "Bash(git rm --cached .claude/baas.local.json)",
        "Bash(git rm --cached .claude/settings.local.json)",
    ]
    assert _frontmatter(INIT_SKILL)["disable-model-invocation"] is True
    assert "argument-hint" in _frontmatter(INIT_SKILL)


@pytest.mark.parametrize("skill", sorted((REPO / "skills").glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_no_skill_allows_a_bare_or_wildcard_tool(skill: Path) -> None:
    for tool in _allowed_tools(skill):
        assert tool.startswith("Bash("), tool
        assert tool.endswith(")"), tool
        command = tool[len("Bash(") : -1]
        assert command.strip("* :") != "", tool
        assert command.split()[0] in {"python3", "git"}, tool
        assert "*" not in command.removesuffix(" *"), tool  # el comodín, solo al final


def test_status_skill_is_plain_and_points_to_init() -> None:
    text = STATUS_SKILL.read_text(encoding="utf-8")
    assert "set-key" not in text
    assert "/baas-init" in text
    assert "todavía no está protegida" in text.lower() or "aún no está protegida" in text.lower()


def test_readme_documents_first_use_and_server_address() -> None:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    for needle in ("DEFAULT_APP_URL", "BAAS_APP_URL", "/baas-init", "set-key"):
        assert needle in text, needle
    first_use = re.split(r"\n#{2,3} ", text.split("## Primer uso", 1)[1], maxsplit=1)[0]
    assert "/baas-init" in first_use
    assert "set-key" not in first_use  # el primer uso son tres pasos, sin claves ni terminales
