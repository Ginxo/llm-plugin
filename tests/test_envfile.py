from __future__ import annotations

import os
from pathlib import Path

import pytest

from baas_plugin import envfile
from baas_plugin.errors import ConfigError, MissingApiKeyError
from conftest import mode_of

posix_only = pytest.mark.skipif(os.name != "posix", reason="permisos POSIX")


def test_baas_paths_default_lives_under_home(home: Path) -> None:
    paths = envfile.BaasPaths.default()
    assert paths.root == home / "baas-ai-ip"
    assert paths.env_file == home / "baas-ai-ip" / ".env"
    assert paths.bin_dir == home / "baas-ai-ip" / "bin"


def test_parse_env_handles_comments_quotes_export_and_equals() -> None:
    text = "\n".join(
        [
            "# comentario",
            "",
            "BAAS_API_KEY=baas_abc",
            'BAAS_APP_URL="https://app.example.com"',
            "export OTRA='con = igual'",
            "SIN_VALOR=",
            "linea basura sin igual",
        ]
    )
    assert envfile.parse_env(text) == {
        "BAAS_API_KEY": "baas_abc",
        "BAAS_APP_URL": "https://app.example.com",
        "OTRA": "con = igual",
        "SIN_VALOR": "",
    }


@posix_only
def test_update_env_creates_private_dir_and_file(home: Path) -> None:
    paths = envfile.BaasPaths.default()
    envfile.update_env(paths, {"BAAS_API_KEY": "baas_x"})
    assert paths.env_file.read_text() == "BAAS_API_KEY=baas_x\n"
    assert mode_of(paths.root) == 0o700
    assert mode_of(paths.env_file) == 0o600


def test_update_env_preserves_other_lines_and_replaces_in_place(home: Path) -> None:
    paths = envfile.BaasPaths.default()
    paths.root.mkdir(mode=0o700)
    paths.env_file.write_text("# mis notas\nBAAS_APP_URL=https://app.example.com\nBAAS_API_KEY=viejo\nOTRA=1\n")
    envfile.update_env(paths, {"BAAS_API_KEY": "baas_nuevo"})
    envfile.update_env(paths, {"BAAS_API_KEY": "baas_nuevo"})  # idempotente
    assert paths.env_file.read_text() == (
        "# mis notas\nBAAS_APP_URL=https://app.example.com\nBAAS_API_KEY=baas_nuevo\nOTRA=1\n"
    )


def test_update_env_appends_missing_keys_without_duplicates(home: Path) -> None:
    paths = envfile.BaasPaths.default()
    envfile.update_env(paths, {"BAAS_API_KEY": "baas_a"})
    envfile.update_env(paths, {"BAAS_APP_URL": "https://app.example.com"})
    envfile.update_env(paths, {"BAAS_APP_URL": "https://app2.example.com"})
    assert paths.env_file.read_text() == "BAAS_API_KEY=baas_a\nBAAS_APP_URL=https://app2.example.com\n"


def test_update_env_leaves_no_temporary_files(home: Path) -> None:
    paths = envfile.BaasPaths.default()
    envfile.update_env(paths, {"BAAS_API_KEY": "baas_a"})
    assert sorted(p.name for p in paths.root.iterdir()) == [".env"]


@posix_only
def test_update_env_tightens_existing_permissions(home: Path) -> None:
    paths = envfile.BaasPaths.default()
    paths.root.mkdir(mode=0o755)
    paths.root.chmod(0o755)
    paths.env_file.write_text("BAAS_API_KEY=baas_a\n")
    paths.env_file.chmod(0o644)
    envfile.update_env(paths, {"BAAS_APP_URL": "https://app.example.com"})
    assert mode_of(paths.root) == 0o700
    assert mode_of(paths.env_file) == 0o600


@posix_only
def test_read_env_fixes_open_permissions_and_warns(home: Path) -> None:
    paths = envfile.BaasPaths.default()
    paths.root.mkdir(mode=0o700)
    paths.env_file.write_text("BAAS_API_KEY=baas_a\n")
    paths.env_file.chmod(0o644)
    values, warnings = envfile.read_env(paths)
    assert values == {"BAAS_API_KEY": "baas_a"}
    assert mode_of(paths.env_file) == 0o600
    assert any("0600" in w for w in warnings)


def test_read_env_missing_file_returns_empty(home: Path) -> None:
    values, warnings = envfile.read_env(envfile.BaasPaths.default())
    assert values == {}
    assert warnings == []


def test_validate_api_key_accepts_contract_format(analista_key: str) -> None:
    assert len(analista_key) == 48
    assert envfile.validate_api_key(f"  {analista_key}\n") == analista_key


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "sk-ant-api03-xxxx",
        "baas_" + "a" * 42,  # corta
        "baas_" + "a" * 44,  # larga
        "baas_" + "a" * 42 + "=",  # con relleno
        "baas_" + "a" * 42 + "+",  # base64 estándar, no url-safe
    ],
)
def test_validate_api_key_rejects_other_formats(bad: str) -> None:
    with pytest.raises(ConfigError):
        envfile.validate_api_key(bad)


def test_load_credentials_without_file_asks_for_key(home: Path) -> None:
    with pytest.raises(MissingApiKeyError) as exc:
        envfile.load_credentials(envfile.BaasPaths.default())
    # El mensaje indica dónde guardarla, sin pedirla en el chat
    assert "baas-ai-ip/.env" in str(exc.value)


def test_load_credentials_with_empty_key_asks_for_key(home: Path) -> None:
    paths = envfile.BaasPaths.default()
    envfile.update_env(paths, {"BAAS_API_KEY": ""})
    with pytest.raises(MissingApiKeyError):
        envfile.load_credentials(paths)


def test_load_credentials_returns_key_and_normalized_url(home: Path, analista_key: str) -> None:
    paths = envfile.BaasPaths.default()
    envfile.update_env(paths, {"BAAS_API_KEY": analista_key, "BAAS_APP_URL": "https://app.example.com/"})
    creds = envfile.load_credentials(paths)
    assert creds.api_key == analista_key
    assert creds.app_url == "https://app.example.com"


def test_credentials_repr_never_shows_the_key(home: Path, analista_key: str) -> None:
    creds = envfile.Credentials(api_key=analista_key, app_url=None)
    assert analista_key not in repr(creds)
    assert analista_key not in str(creds)


@pytest.mark.parametrize(
    "url",
    [
        "https://app.example.com",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "http://app.localhost",
        "http://app.baas.test:8080",
    ],
)
def test_normalize_app_url_accepts_https_and_local_http(url: str) -> None:
    assert envfile.normalize_app_url(url + "/") == url


@pytest.mark.parametrize("url", ["http://app.example.com", "ftp://app.example.com", "app.example.com", "https://"])
def test_normalize_app_url_rejects_insecure_or_invalid(url: str) -> None:
    with pytest.raises(ConfigError):
        envfile.normalize_app_url(url)
