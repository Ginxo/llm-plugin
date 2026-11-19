from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from baas_plugin import project as pc
from baas_plugin.errors import ProjectSafetyError
from conftest import mode_of

PROXY = "https://proxy.example.com/org/demo/anthropic"


def settings(proj: Path) -> dict:
    return json.loads((proj / ".claude" / "settings.local.json").read_text())


def test_write_settings_creates_env_block(project: Path, analista_key: str) -> None:
    assert pc.write_settings_local(project, PROXY, analista_key) is True
    assert settings(project) == {
        "env": {
            "ANTHROPIC_BASE_URL": PROXY,
            "ANTHROPIC_CUSTOM_HEADERS": f"X-Baas-Api-Key: {analista_key}",
        }
    }


@pytest.mark.skipif(os.name != "posix", reason="permisos POSIX")
def test_settings_file_is_private(project: Path, analista_key: str) -> None:
    pc.write_settings_local(project, PROXY, analista_key)
    assert mode_of(project / ".claude" / "settings.local.json") == 0o600


def test_write_settings_preserves_user_content(project: Path, analista_key: str) -> None:
    (project / ".claude").mkdir()
    previous = {
        "permissions": {"allow": ["Bash(npm test)"]},
        "env": {
            "DISABLE_AUTO_COMPACT": "1",
            "ANTHROPIC_CUSTOM_HEADERS": "X-Otra: valor\nX-Baas-Api-Key: baas_vieja",
        },
        "model": "opus",
    }
    (project / ".claude" / "settings.local.json").write_text(json.dumps(previous))
    pc.write_settings_local(project, PROXY, analista_key)
    data = settings(project)
    assert data["permissions"] == previous["permissions"]
    assert data["model"] == "opus"
    assert data["env"]["DISABLE_AUTO_COMPACT"] == "1"
    assert data["env"]["ANTHROPIC_CUSTOM_HEADERS"] == f"X-Otra: valor\nX-Baas-Api-Key: {analista_key}"
    assert data["env"]["ANTHROPIC_BASE_URL"] == PROXY


def test_write_settings_is_idempotent(project: Path, analista_key: str) -> None:
    pc.write_settings_local(project, PROXY, analista_key)
    first = (project / ".claude" / "settings.local.json").read_bytes()
    assert pc.write_settings_local(project, PROXY, analista_key) is False
    assert (project / ".claude" / "settings.local.json").read_bytes() == first


def test_write_settings_refuses_invalid_json(project: Path, analista_key: str) -> None:
    (project / ".claude").mkdir()
    path = project / ".claude" / "settings.local.json"
    path.write_text("{ roto, ")
    with pytest.raises(ProjectSafetyError):
        pc.write_settings_local(project, PROXY, analista_key)
    assert path.read_text() == "{ roto, "


def test_merge_custom_headers_handles_case_and_blank_lines() -> None:
    merged = pc.merge_custom_headers("x-baas-api-key: vieja\n\nX-Otra: 1", "baas_nueva")
    assert merged == "X-Otra: 1\nX-Baas-Api-Key: baas_nueva"


def test_gitignore_created_with_both_entries(project: Path) -> None:
    added = pc.ensure_gitignore(project)
    assert added == [".claude/settings.local.json", ".claude/baas.local.json"]
    lines = (project / ".gitignore").read_text().splitlines()
    assert ".claude/settings.local.json" in lines
    assert ".claude/baas.local.json" in lines


def test_gitignore_preserves_content_and_does_not_duplicate(project: Path) -> None:
    (project / ".gitignore").write_text("node_modules/\n/.claude/settings.local.json")  # sin salto final
    assert pc.ensure_gitignore(project) == [".claude/baas.local.json"]
    assert pc.ensure_gitignore(project) == []
    text = (project / ".gitignore").read_text()
    assert text.startswith("node_modules/\n/.claude/settings.local.json\n")
    assert text.count("settings.local.json") == 1
    assert text.count(".claude/baas.local.json") == 1
    assert text.endswith("\n")


def _git(proj: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True)


def test_tracked_settings_file_is_refused(project: Path, analista_key: str) -> None:
    _git(project, "init", "-q")
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.local.json").write_text("{}")
    _git(project, "add", ".claude/settings.local.json")
    with pytest.raises(ProjectSafetyError) as exc:
        pc.check_not_tracked(project)
    assert "git rm --cached" in (exc.value.hint or "")


def test_untracked_files_pass_check(project: Path) -> None:
    _git(project, "init", "-q")
    pc.check_not_tracked(project)  # no lanza
    pc.check_not_tracked(project.parent / "no-es-repo-ni-existe")  # sin git: no lanza


def test_resolve_project_dir_prefers_explicit_then_git_root(project: Path) -> None:
    sub = project / "a" / "b"
    sub.mkdir(parents=True)
    assert pc.resolve_project_dir(str(project), cwd=sub) == project.resolve()
    assert pc.resolve_project_dir(None, cwd=sub) == sub.resolve()
    _git(project, "init", "-q")
    assert pc.resolve_project_dir(None, cwd=sub) == project.resolve()


def test_binding_roundtrip_has_no_secrets(project: Path, analista_key: str) -> None:
    binding = {"org": "demo", "mcp_url": "https://mcp.example.com/org/demo/mcp", "proxy_anthropic_url": PROXY}
    pc.write_binding(project, binding)
    assert pc.read_binding(project) == binding
    assert analista_key not in (project / ".claude" / "baas.local.json").read_text()
    assert pc.read_binding(project.parent) is None


# --- reparación de ficheros dañados -----------------------------------------------------------


def test_repair_moves_invalid_json_to_bak(project: Path) -> None:
    (project / ".claude").mkdir()
    path = project / ".claude" / "settings.local.json"
    path.write_text('{"env": {"X-Baas-Api-Key": "secreto", ')
    assert pc.repair_json_file(path) is True
    assert not path.exists()
    bak = project / ".claude" / "settings.local.json.bak"
    assert bak.read_text() == '{"env": {"X-Baas-Api-Key": "secreto", '
    if os.name == "posix":
        assert mode_of(bak) == 0o600


@pytest.mark.parametrize("content", ["[]", '"texto"', "   ", "no es json", "\xff\xfe"])
def test_repair_treats_non_objects_and_garbage_as_damaged(project: Path, content: str) -> None:
    (project / ".claude").mkdir()
    path = project / ".claude" / "baas.local.json"
    path.write_bytes(content.encode("latin-1"))
    assert pc.repair_json_file(path) is True
    assert (project / ".claude" / "baas.local.json.bak").exists()


@pytest.mark.parametrize("content", ["{}", '{"env": {}}', ""])
def test_repair_leaves_valid_files_alone(project: Path, content: str) -> None:
    (project / ".claude").mkdir()
    path = project / ".claude" / "settings.local.json"
    path.write_text(content)
    assert pc.repair_json_file(path) is False
    assert path.read_text() == content
    assert not (project / ".claude" / "settings.local.json.bak").exists()


def test_repair_missing_file_is_noop(project: Path) -> None:
    assert pc.repair_json_file(project / ".claude" / "settings.local.json") is False


def test_gitignore_can_cover_the_backups(project: Path) -> None:
    added = pc.ensure_gitignore(project, pc.GITIGNORE_ENTRIES + pc.BACKUP_ENTRIES)
    assert ".claude/settings.local.json.bak" in added
    assert ".claude/baas.local.json.bak" in added
    assert pc.ensure_gitignore(project, pc.GITIGNORE_ENTRIES + pc.BACKUP_ENTRIES) == []
