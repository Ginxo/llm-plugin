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
    """Las entradas de `allowed-tools` (lista YAML o texto separado por espacios; los paréntesis agrupan)."""
    raw = _frontmatter(path).get("allowed-tools", [])
    if isinstance(raw, list):
        return [str(item) for item in raw]
    entries: list[str] = []
    depth, current = 0, ""
    for char in str(raw):
        if char.isspace() and depth == 0:
            if current:
                entries.append(current)
            current = ""
            continue
        depth += char == "("
        depth -= char == ")"
        current += char
    if current:
        entries.append(current)
    assert depth == 0, f"paréntesis sin cerrar en allowed-tools de {path}"
    return entries


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
        'Bash(git -C "${CLAUDE_PROJECT_DIR}" rm --cached .claude/baas.local.json)',
        'Bash(git -C "${CLAUDE_PROJECT_DIR}" rm --cached .claude/settings.local.json)',
    ]
    assert len(tools) == 3
    assert _frontmatter(INIT_SKILL)["disable-model-invocation"] is True
    assert "argument-hint" in _frontmatter(INIT_SKILL)


@pytest.mark.parametrize("skill", sorted((REPO / "skills").glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_no_skill_allows_a_bare_or_wildcard_tool(skill: Path) -> None:
    tools = _allowed_tools(skill)
    if "allowed-tools" not in _frontmatter(skill):
        assert tools == []  # sin permisos propios (p. ej. baas-brain, que solo orienta)
        return
    assert tools, "si declara `allowed-tools`, el lint tiene que entender sus entradas"
    for tool in tools:
        if tool.startswith("mcp__"):
            assert tool.startswith("mcp__baas__baas_"), tool  # solo las tools del propio MCP, una a una
            assert "*" not in tool, tool
            continue
        assert tool.startswith("Bash("), tool
        assert tool.endswith(")"), tool
        command = tool[len("Bash(") : -1].split()
        assert command[0] in {"python3", "git"}, tool
        assert len(command) > 2, tool  # nunca `Bash(python3 *)` ni `Bash(*)`
        if command[0] == "python3":
            assert command[1] == '"${CLAUDE_PLUGIN_ROOT}/scripts/baas.py"', tool
        for word in command:
            assert "*" not in word or word == "*", tool  # el comodín, solo como palabra suelta
        assert command[2] != "*", tool  # el subcomando nunca es un comodín


def test_connection_failure_sentence_does_not_show_the_address() -> None:
    text = INIT_SKILL.read_text(encoding="utf-8")
    assert not [s for s in _shown_texts(INIT_SKILL) if "<dirección>" in s and "No encuentro" in s]
    assert "No encuentro el servidor de BaaS-AI-IP." in text


def test_init_skill_never_mentions_local_secret_files() -> None:
    text = INIT_SKILL.read_text(encoding="utf-8")
    assert ".env" not in text
    assert "~/baas-ai-ip" not in text


def test_init_skill_validates_and_single_quotes_what_it_passes_to_the_shell() -> None:
    text = INIT_SKILL.read_text(encoding="utf-8")
    assert "^https?://[A-Za-z0-9.:/_-]+$" in text
    assert "^[a-z0-9][a-z0-9-]{0,63}$" in text
    # Todo valor que sigue a --org o --app-url en un comando va entre comillas simples.
    for match in re.finditer(r"--(?:org|app-url)[ \t]+(\S)", text):
        assert match.group(1) == "'", text[match.start() : match.end() + 20]
    assert 'init --project "${CLAUDE_PROJECT_DIR}" [--org' in text
    # Los comandos de git se ejecutan desde la raíz del proyecto.
    for match in re.finditer(r"`git [^`]*`", text):
        assert match.group(0).startswith('`git -C "${CLAUDE_PROJECT_DIR}" rm --cached'), match.group(0)


def test_init_skill_keys_on_result_markers_and_covers_new_rows() -> None:
    text = INIT_SKILL.read_text(encoding="utf-8")
    for needle in (
        "[BAAS_RESULT linked]", "[BAAS_RESULT unchanged]", "[BAAS_APP_URL", "BAAS_CONFIG_ERROR",
        "ya no es válida", "He renovado tu conexión anterior", "Hay demasiados intentos seguidos",
        "No he podido hacerlo; pide ayuda a quien administra BaaS-AI-IP", "no digas la frase del caso",
        "Esa dirección no parece válida; tiene que empezar por https://",
    ):  # fmt: skip
        assert needle in text, needle
    assert "primera línea con" not in text
    # Fila de cierre: cualquier salida que no esté en la tabla tampoco deja a la persona sin siguiente paso.
    assert "Cualquier otra salida" in text
    assert "Algo no ha ido como esperaba. Vuelve a intentarlo con /baas-init" in text
    assert "no es la esperada" in text
    assert "respuesta inesperada" in text
    assert "Ahora mismo no puedo conectar" in text  # ya no se deduce de «vinculado»/«actualizado»


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
    troubleshooting = text.split("## Solución de problemas", 1)[1].split("\n## ", 1)[0]
    assert "set-key" not in troubleshooting  # `set-key` es solo para administradores y CI
