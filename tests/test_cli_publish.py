"""`baas.py publish|export|inspect` de punta a punta: CLI → McpClient → MCP simulado por HTTP."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from baas_plugin import cli, envfile
from baas_plugin import project as project_mod
from fake_publish_server import FakePublishServer, FakeStore
from skill_fixture import Brain, make_brain

KEY = "baas_" + "A" * 43
SUGGESTED = ["facturacion.py", "render_doc.py", "md_to_doc.py", "gestor-facturas.py"]


@pytest.fixture
def brain(tmp_path: Path) -> Brain:
    return make_brain(tmp_path)


@pytest.fixture
def served(home: Path, project: Path) -> Iterator[tuple[FakeStore, FakePublishServer]]:
    store = FakeStore()
    with FakePublishServer(store, api_key=KEY) as server:
        envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": KEY})
        (project / ".claude").mkdir()
        project_mod.write_binding(project, {"org": "demo", "mcp_url": server.mcp_url})
        yield store, server


def publish_args(brain: Brain, project: Path, *extra: str) -> list[str]:
    args = ["publish", str(brain.skill), "--project", str(project), "--roles", "facturacion"]
    for name in SUGGESTED:
        args += ["--include-scripts", str(brain.scripts / name)]
    return [*args, *extra]


def test_dry_run_shows_the_plan_and_writes_nothing(brain, project, served, capfd) -> None:
    store, _ = served
    assert cli.main(publish_args(brain, project, "--dry-run")) == 0
    out = capfd.readouterr().out
    assert store.files == {}
    assert "ENSAYO" in out
    assert "skills/facturacion/SKILL.md" in out
    assert "Tráfico estimado" in out
    assert "FRONTMATTER_HOOKS" in out
    assert KEY not in out


def test_publish_with_yes_publishes_and_verifies(brain, project, served, capfd) -> None:
    store, _ = served
    assert cli.main(publish_args(brain, project, "--yes")) == 0
    out = capfd.readouterr().out
    assert "PUBLICADA" in out
    assert "Verificado" in out
    assert store.skill_files("facturacion")["SKILL.md"] == (brain.skill / "SKILL.md").read_bytes()
    assert len([1 for n, a in store.calls if n == "baas_write_files" and not a.get("dry_run")]) == 1


def test_publish_without_confirmation_and_without_a_terminal_refuses(
    brain, project, served, capfd, monkeypatch
) -> None:
    store, _ = served
    monkeypatch.setattr("sys.stdin", open(os.devnull))  # noqa: SIM115
    assert cli.main(publish_args(brain, project)) == 2
    captured = capfd.readouterr()
    assert "--yes" in captured.err
    assert store.files == {}


@pytest.mark.parametrize(("answer", "code", "published"), [("s", 0, True), ("n", 1, False), ("", 1, False)])
def test_interactive_confirmation(brain, project, served, capfd, monkeypatch, answer, code, published) -> None:
    store, _ = served
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert cli.main(publish_args(brain, project)) == code
    out = capfd.readouterr().out
    assert bool(store.files) is published
    assert ("Cancelado" in out) is (not published)


def test_json_output(brain, project, served, capfd) -> None:
    assert cli.main(publish_args(brain, project, "--dry-run", "--json")) == 0
    data = json.loads(capfd.readouterr().out)
    assert data["skill"] == "facturacion"
    assert data["dry_run"] is True
    assert any(f["path"] == "skills/facturacion/baas.yaml" for f in data["files"])
    assert data["manifest"]["roles"] == ["facturacion"]


def test_json_needs_yes_or_dry_run(brain, project, served, capfd) -> None:
    assert cli.main(publish_args(brain, project, "--json")) == 2


def test_roles_star_must_be_alone(brain, project, served, capfd) -> None:
    code = cli.main(["publish", str(brain.skill), "--project", str(project), "--roles", "*,a", "--dry-run"])
    assert code == 2
    assert "solo" in capfd.readouterr().err


def test_knowledge_and_roles_from_the_command_line(brain, project, served) -> None:
    store, _ = served
    args = publish_args(
        brain,
        project,
        "--yes",
        "--knowledge",
        f"{brain.knowledge / 'templates'}:fiscal/templates",
        "--knowledge-roles",
        "facturacion",
    )
    assert cli.main(args) == 0
    assert "knowledge/fiscal/templates/factura-pro.typ" in store.files
    assert yaml.safe_load(store.files["knowledge/fiscal/templates/_acl.yaml"].decode()) == {"roles": ["facturacion"]}


def test_invalid_role_reports_every_failure_and_exits_10(brain, project, served, capfd) -> None:
    store, _ = served
    code = cli.main(
        [
            "publish",
            str(brain.skill),
            "--project",
            str(project),
            "--roles",
            "nada",
            "--yes",
            "--include-scripts",
            str(brain.scripts / "render_doc.py"),
        ]
    )
    err = capfd.readouterr().err
    assert code == 10
    assert "BAAS_MCP_INVALID_ARGUMENT" in err
    assert "nada" in err
    assert store.files == {}


def test_authorization_denied_tells_the_agent_to_stop(brain, project, served, capfd) -> None:
    store, _ = served
    store.admin = False
    store.user_roles = {"facturacion"}
    code = cli.main(publish_args(brain, project, "--yes"))
    err = capfd.readouterr().err
    assert code == 10
    assert "BAAS_MCP_AUTHORIZATION_DENIED" in err
    assert "Detente" in err
    assert store.files == {}


def test_kill_switch_stops_publication(brain, project, served, capfd) -> None:
    store, _ = served
    store.kill_scope = "GLOBAL"
    code = cli.main(publish_args(brain, project, "--yes"))
    err = capfd.readouterr().err
    assert code == 10
    assert "BAAS_MCP_KILL_SWITCH_ACTIVE" in err
    assert store.files == {}


def test_unbound_project_asks_for_init(brain, home, project, capfd) -> None:
    envfile.update_env(envfile.BaasPaths.default(), {"BAAS_API_KEY": KEY})
    assert cli.main(publish_args(brain, project, "--dry-run")) == 2
    assert "/baas-init" in capfd.readouterr().err


def test_publish_then_export_gives_the_same_tree(brain, project, served, tmp_path, capfd) -> None:
    store, _ = served
    assert cli.main(publish_args(brain, project, "--yes")) == 0
    dest = tmp_path / "exportada"
    assert cli.main(["export", "facturacion", str(dest), "--project", str(project)]) == 0
    out = capfd.readouterr().out
    assert "exportada" in out
    for rel, data in store.skill_files("facturacion").items():
        assert (dest / rel).read_bytes() == data
    assert (dest / "baas.yaml").is_file()
    assert os.access(dest / "scripts/facturacion.py", os.X_OK)
    # y sin baas.yaml sigue siendo una skill válida con el mismo contenido que el original
    again = tmp_path / "sin-manifiesto"
    assert cli.main(["export", "facturacion", str(again), "--project", str(project), "--no-manifest"]) == 0
    assert (again / "SKILL.md").read_bytes() == (brain.skill / "SKILL.md").read_bytes()
    assert not (again / "baas.yaml").exists()


def test_export_json_and_errors(brain, project, served, tmp_path, capfd) -> None:
    assert cli.main(publish_args(brain, project, "--yes")) == 0
    capfd.readouterr()
    assert cli.main(["export", "facturacion", str(tmp_path / "a"), "--project", str(project), "--json"]) == 0
    data = json.loads(capfd.readouterr().out)
    assert data["skill"] == "facturacion"
    assert data["package_hash_ok"] is True
    assert cli.main(["export", "no-existe", str(tmp_path / "b"), "--project", str(project)]) == 10
    assert "BAAS_MCP_NOT_FOUND" in capfd.readouterr().err


def test_tens_of_megabytes_go_up_and_come_down_over_http(brain, project, served, tmp_path, capfd) -> None:
    store, _ = served
    blob = os.urandom(30 * 1024 * 1024)
    (brain.skill / "assets" / "grande.bin").write_bytes(blob)
    assert cli.main(publish_args(brain, project, "--yes")) == 0
    out = capfd.readouterr().out
    assert "LARGE_FILE" in out
    dest = tmp_path / "out"
    assert cli.main(["export", "facturacion", str(dest), "--project", str(project)]) == 0
    assert (dest / "assets/grande.bin").read_bytes() == blob
    assert store.skill_files("facturacion")["assets/grande.bin"] == blob


def test_help_lists_the_new_commands(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for cmd in ("inspect", "publish", "export"):
        assert cmd in out
