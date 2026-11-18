from __future__ import annotations

import os
from pathlib import Path

import pytest
from fake_baas import dev_api_key

ANALISTA = "analista@demo.test"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME temporal: nada de lo que hagan los tests toca la configuración real del usuario."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("BAAS_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("BAAS_CLAUDE_HOME", str(fake_home / ".claude"))  # `baas.py sync` nunca toca el real
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    return fake_home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proyecto"
    proj.mkdir()
    return proj


@pytest.fixture
def analista_key() -> str:
    return dev_api_key(ANALISTA)


def mode_of(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
