"""`docs/antigravity.md` (T3 · #165): lo que documenta coincide con lo que escribe `setup` y no deja ninguna clave."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "antigravity.md"


def text() -> str:
    return DOC.read_text(encoding="utf-8")


def blocks(lang: str) -> list[str]:
    return re.findall(rf"```{lang}\n(.*?)```", text(), flags=re.S)


def test_the_readme_links_the_guide() -> None:
    assert "docs/antigravity.md" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_documented_config_has_the_shape_that_setup_writes() -> None:
    configs = [json.loads(b) for b in blocks("json")]
    config = next(c for c in configs if "baas" in c.get("mcpServers", {}))
    server = config["mcpServers"]["baas"]
    assert set(server) == {"serverUrl", "headers"}
    assert server["headers"] == {"Authorization": "Bearer <API key>"}
    assert server["serverUrl"].startswith("https://")


def test_the_key_is_never_written_in_the_doc() -> None:
    # una API key es `baas_` + 43 caracteres base64url (contrato §3)
    assert not re.search(r"baas_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", text()), "hay una API key literal"


def test_the_doc_gives_the_commands_for_the_extension_and_the_cli() -> None:
    doc = text()
    commands = "\n".join(blocks("bash"))
    assert "baas.py" in commands
    assert "setup antigravity" in commands
    assert "--print" in commands
    for needle in ("Google.google-antigravity", "agy", "/mcp", ".agents/mcp_config.json", ".agents/rules/baas.md"):
        assert needle in doc, needle
    for needle in ("--scope project", "--replace"):
        assert needle in commands, needle


def test_the_doc_explains_why_the_default_scope_is_global_and_what_was_verified() -> None:
    doc = text()
    for needle in ("lee **solo** el global", "una sola organización", "1.13.0", "1.5.15", "2026-11-23"):
        assert needle in doc, needle


def test_the_doc_tells_how_to_get_the_repository_without_claude_code() -> None:
    assert "git clone https://github.com/baas-ai-ip/llm-plugin" in "\n".join(blocks("bash"))


def test_the_doc_says_what_antigravity_does_not_cover() -> None:
    doc = text()
    for needle in ("sin anonimizar", "proxy", "AI-IP", "en claro", "~/.gemini/config/mcp_config.json", "24 000"):
        assert needle in doc, needle


def test_the_doc_lists_the_manual_checks_and_the_open_questions() -> None:
    doc = text()
    assert "Lista de comprobación" in doc
    assert "- [ ]" in doc
    for needle in ("carpeta sin confianza", "instructions", "skills nativas", ".agents/skills/"):
        assert needle in doc, needle
    assert "¿La extensión lee el mismo" not in doc, "esa pregunta ya está respondida: la extensión solo lee el global"


def test_the_doc_explains_the_native_skills_command_and_its_limits() -> None:
    doc = text()
    commands = "\n".join(blocks("bash"))
    assert "sync --tool antigravity" in commands
    assert "--dry-run" in commands
    for needle in (
        "~/.gemini/config/skills/",
        "/skills reload",
        "run-script",
        "disable-model-invocation",
        "prefijo obligatorio",
        "<slug>-<skill>",
        ".baas-skills-<organización>.json",
        "--force",
        "no la toco",
        "SHA-256",
    ):
        assert needle in doc, needle


def test_the_doc_no_longer_lists_native_skills_as_a_follow_up_and_asks_to_check_them() -> None:
    doc = text()
    assert "Llegarán como" not in doc
    for needle in ("`/skills`", "`/<skill>`", "extensión ofrece las skills"):
        assert needle in doc, needle


def test_the_readme_mentions_the_native_skills_command() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "sync --tool antigravity" in readme


def test_the_doc_states_the_multi_organization_limitation_and_recommends_an_option() -> None:
    doc = text()
    for needle in (
        "Varias organizaciones, un solo MCP",
        "baas-<slug>",
        "actuaría sobre",
        "baas_whoami",
        "no documenta",
        "`:`",
        "Recomendada",
    ):
        assert needle in doc, needle


def test_the_doc_explains_the_script_rewrite_its_limits_and_what_the_user_must_check() -> None:
    doc = text()
    commands = "\n".join(blocks("bash"))
    assert "run-script --tool antigravity --synced-org" in commands
    for needle in (
        "${CLAUDE_SKILL_DIR}",
        "--synced-org",
        "sin reescribir",
        "knowledge/",
        "carpeta de trabajo",
        "ruta de `baas.py`",
        "solo funciona completa",
        "no se amplía",
        "dicta",
    ):
        assert needle in doc, needle
    assert "proyecto de cerebro" in doc, "la lista manual dice qué probar con `gestor`"
