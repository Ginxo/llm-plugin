"""`docs/codex.md` (tarea L2): el perfil de Codex que documenta es válido y no deja la clave en ningún fichero."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

try:
    import tomllib
except ImportError:  # Python 3.10
    tomllib = None

DOC = Path(__file__).resolve().parents[1] / "docs" / "codex.md"


def text() -> str:
    return DOC.read_text()


def blocks(lang: str) -> list[str]:
    return re.findall(rf"```{lang}\n(.*?)```", text(), flags=re.S)


@pytest.mark.skipif(tomllib is None, reason="tomllib llega con Python 3.11")
def test_profile_blocks_are_valid_toml_with_the_documented_provider() -> None:
    profiles = [tomllib.loads(b) for b in blocks("toml")]
    assert profiles, "docs/codex.md no trae ningún bloque toml"
    profile = next(p for p in profiles if p.get("model_provider") == "baas")
    provider = profile["model_providers"]["baas"]
    assert provider["wire_api"] == "responses", "Codex 0.169 ya no admite wire_api = chat"
    assert provider["env_http_headers"] == {"X-Baas-Api-Key": "BAAS_API_KEY"}
    assert provider["env_key"] == "OPENAI_API_KEY"
    assert provider["base_url"].startswith("http")
    assert provider["base_url"].rstrip("/").endswith("/openai/v1")


def test_the_key_is_never_written_in_a_file() -> None:
    doc = text()
    # una API key es `baas_` + 43 caracteres base64url (contrato §3)
    assert not re.search(r"baas_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", doc), "hay una API key literal en el documento"
    for block in blocks("toml"):
        assert "http_headers" not in block.replace("env_http_headers", ""), "http_headers dejaría la clave en claro"
        assert "baas_" not in block


def test_the_launcher_reads_only_the_key_and_runs_codex_with_the_profile() -> None:
    launchers = [b for b in blocks("bash") if "codex" in b and "exec" in b]
    assert launchers, "falta el lanzador de Codex"
    launcher = launchers[0]
    assert "BAAS_API_KEY" in launcher
    assert "codex -p baas" in launcher
    assert ".env" in launcher
    assert "export BAAS_API_KEY" not in launcher, "la clave no se exporta al shell del usuario"


def test_the_doc_states_what_was_and_was_not_verified() -> None:
    doc = text()
    for needle in ("0.169", "spike-s13.md", "No se ha comprobado", "snapshots", "AI-IP"):
        assert needle in doc, needle
