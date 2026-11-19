"""e2e del `/baas-init` guiado sobre la pila simulada (`dev/fake_h4_stack.py`).

Cada prueba lanza `python3 scripts/baas.py init ...` como proceso de verdad contra el servidor simulado, con un HOME y
un proyecto temporales: nunca se toca el `~/baas-ai-ip` real ni se abre un navegador. Los tiempos son los reales del
plugin, pero el simulado aprueba en el primer sondeo, así que ninguna espera. El sondeo con varios intentos se prueba
en el propio proceso con un reloj inyectado.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fake_baas import dev_api_key
from fake_h4_stack import FakeH4Stack

from baas_plugin import device, envfile

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FAKE_BIN = "#!/bin/sh\nexit 0\n"  # `claude`, `open` y `xdg-open` de mentira: ni registra nada ni abre ventanas
USER_CODE = re.compile(r"\b[A-Z2-9]{4}-[A-Z2-9]{4}\b")


@dataclass
class Run:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


@pytest.fixture
def stack() -> Iterator[FakeH4Stack]:
    with FakeH4Stack() as s:
        yield s


@pytest.fixture
def env(home: Path, tmp_path: Path) -> dict[str, str]:
    """Entorno aislado: HOME temporal, sin variables de Claude/BaaS heredadas y sin navegador ni `claude` reales."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "open", "xdg-open"):
        (bin_dir / name).write_text(FAKE_BIN)
        (bin_dir / name).chmod(0o755)
    kept = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE", "BAAS_"))}
    kept.pop("DISPLAY", None)
    kept.pop("WAYLAND_DISPLAY", None)
    kept.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "BAAS_CLAUDE_BIN": str(bin_dir / "claude"),
            "BAAS_CLAUDE_HOME": str(home / ".claude"),
        }
    )
    return kept


def baas(env: dict[str, str], project: Path, *args: str) -> Run:
    res = subprocess.run(
        [sys.executable, str(SCRIPTS / "baas.py"), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=project,
        timeout=60,
        check=False,
    )
    return Run(res.returncode, res.stdout, res.stderr)


def init(env: dict[str, str], project: Path, app_url: str, *extra: str) -> Run:
    return baas(env, project, "init", "--project", str(project), "--app-url", app_url, *extra)


def baas_dir(home: Path) -> Path:
    return home / "baas-ai-ip"


def stored_key(home: Path) -> str:
    values, _ = envfile.read_env(envfile.BaasPaths(baas_dir(home)))
    return values["BAAS_API_KEY"]


def has_stored_key(home: Path) -> bool:
    env_file = baas_dir(home) / ".env"
    return env_file.exists() and "BAAS_API_KEY" in env_file.read_text()


def free_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def assert_no_key(key: str, *runs: Run) -> None:
    for run in runs:
        assert key not in run.text


def test_happy_path_links_without_ever_printing_the_key(
    stack: FakeH4Stack, home: Path, project: Path, env: dict[str, str]
) -> None:
    first = init(env, project, stack.app_url)
    assert first.code == 20, first.text
    assert "[BAAS_APPROVAL_PENDING]" in first.text
    assert f"{stack.app_url}/connect?code=" in first.text
    assert not has_stored_key(home)

    second = init(env, project, stack.app_url, "--wait")
    assert second.code == 0, second.text
    assert "[BAAS_RESULT linked]" in second.text
    assert stat.S_IMODE((baas_dir(home) / ".env").stat().st_mode) == 0o600
    key = stored_key(home)
    assert key in stack.device.issued_keys
    assert key != dev_api_key(stack.device.email)
    assert not (baas_dir(home) / "pending.json").exists()

    settings = json.loads((project / ".claude" / "settings.local.json").read_text())
    assert settings["env"]["ANTHROPIC_BASE_URL"] == f"{stack.proxy_url}/org/demo/anthropic"
    assert f"X-Baas-Api-Key: {key}" in settings["env"]["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    assert json.loads((project / ".claude" / "baas.local.json").read_text())["org"] == "demo"
    assert_no_key(key, first, second)

    third = init(env, project, stack.app_url)  # ya conectado: no pide nada ni reescribe
    assert third.code == 0, third.text
    assert "[BAAS_RESULT unchanged]" in third.text
    assert stack.device.starts == 1
    assert_no_key(key, third)


def test_wait_polls_until_the_simulated_approval_without_real_sleeping(stack: FakeH4Stack) -> None:
    """El cliente de sondeo contra el simulado: «pendiente» N-1 veces, luego la clave una sola vez (reloj inyectado)."""
    stack.device.approve_after = 4
    flow = device.start(stack.app_url, "proyecto en equipo")
    sleeps: list[float] = []
    result = device.wait(
        stack.app_url, flow.device_code, interval=flow.interval, max_seconds=60, sleep=sleeps.append, now=lambda: 0.0
    )
    assert result.status == "approved"
    assert result.api_key in stack.device.issued_keys
    assert stack.device.polls == 4
    assert sleeps == [3.0, 3.0, 3.0]
    assert device.poll(stack.app_url, flow.device_code).status == "expired"  # la clave solo se entrega una vez


def test_revoked_stored_key_recovers_through_the_flow(
    stack: FakeH4Stack, home: Path, project: Path, env: dict[str, str]
) -> None:
    old = dev_api_key(stack.device.email)
    envfile.update_env(envfile.BaasPaths(baas_dir(home)), {"BAAS_API_KEY": old, "BAAS_APP_URL": stack.app_url})
    stack.state.revoke_key(old)

    first = init(env, project, stack.app_url)
    assert first.code == 20, first.text
    assert "ya no es válida" in first.text
    assert not has_stored_key(home)  # la clave muerta ya no se vuelve a probar

    second = init(env, project, stack.app_url, "--wait")
    assert second.code == 0, second.text
    assert "[BAAS_RESULT linked]" in second.text
    new = stored_key(home)
    assert new != old
    assert new in stack.device.issued_keys
    assert_no_key(new, first, second)
    assert_no_key(old, first, second)


def test_wrong_address_persists_nothing_and_the_right_one_continues(
    stack: FakeH4Stack, home: Path, project: Path, env: dict[str, str]
) -> None:
    bad = free_port_url()  # nadie escucha
    run = init(env, project, bad)
    assert run.code == 6, run.text
    assert f"[BAAS_APP_URL {bad}]" in run.err
    assert not baas_dir(home).exists() or not any(baas_dir(home).iterdir())
    assert not (project / ".claude").exists()

    ok = init(env, project, stack.app_url)
    assert ok.code == 20, ok.text
    assert f"BAAS_APP_URL={stack.app_url}" in (baas_dir(home) / ".env").read_text()


def test_start_rate_limit_is_a_network_exit_without_state(
    stack: FakeH4Stack, home: Path, project: Path, env: dict[str, str]
) -> None:
    stack.device.start_status = 429
    run = init(env, project, stack.app_url)
    assert run.code == 6, run.text
    assert "demasiadas" in run.text
    assert not (baas_dir(home) / "pending.json").exists()

    stack.device.start_status = 200
    assert init(env, project, stack.app_url).code == 20


def test_expired_code_then_a_fresh_init_starts_over(
    stack: FakeH4Stack, home: Path, project: Path, env: dict[str, str]
) -> None:
    stack.device.outcome = "expire"
    assert init(env, project, stack.app_url).code == 20
    expired = init(env, project, stack.app_url, "--wait")
    assert expired.code == 23, expired.text
    assert "[BAAS_APPROVAL_EXPIRED]" in expired.text
    assert not (baas_dir(home) / "pending.json").exists()

    fresh = init(env, project, stack.app_url)
    assert fresh.code == 20, fresh.text
    assert stack.device.starts == 2


def test_denied_ends_with_22_and_changes_nothing(
    stack: FakeH4Stack, home: Path, project: Path, env: dict[str, str]
) -> None:
    stack.device.outcome = "deny"
    assert init(env, project, stack.app_url).code == 20
    denied = init(env, project, stack.app_url, "--wait")
    assert denied.code == 22, denied.text
    assert "[BAAS_APPROVAL_DENIED]" in denied.text
    assert not (project / ".claude").exists()
    assert not has_stored_key(home)
    assert not (baas_dir(home) / "pending.json").exists()


def test_two_inits_before_approval_show_the_same_code(stack: FakeH4Stack, project: Path, env: dict[str, str]) -> None:
    first = init(env, project, stack.app_url)
    second = init(env, project, stack.app_url)
    assert first.code == second.code == 20
    codes = USER_CODE.findall(first.text), USER_CODE.findall(second.text)
    assert codes[0]
    assert codes[0] == codes[1]
    assert stack.device.starts == 1
    link = f"{stack.app_url}/connect?code={codes[0][0]}"
    assert link in first.text
    assert link in second.text


def guard(env: dict[str, str], project: Path, prompt: str) -> dict | None:
    """El hook `UserPromptSubmit` como lo lanza Claude Code: JSON por la entrada estándar y sin proxy en el entorno."""
    clean = {k: v for k, v in env.items() if k != "ANTHROPIC_BASE_URL"}
    clean.update({"CLAUDE_PROJECT_DIR": str(project), "CLAUDE_CODE_ENTRYPOINT": "cli"})
    res = subprocess.run(
        [sys.executable, str(SCRIPTS / "baas_guard.py"), "--event", "prompt"],
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": str(project), "prompt": prompt}),
        capture_output=True,
        text=True,
        env=clean,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout) if res.stdout.strip() else None


def test_guard_lets_init_through_and_blocks_other_prompts_in_a_linked_project(
    stack: FakeH4Stack, project: Path, env: dict[str, str]
) -> None:
    assert init(env, project, stack.app_url).code == 20
    assert init(env, project, stack.app_url, "--wait").code == 0

    assert guard(env, project, "/baas-init demo") is None
    assert guard(env, project, "/baas-status") is None
    blocked = guard(env, project, "resume este documento")
    assert blocked is not None
    assert blocked["decision"] == "block"
    reason = blocked["reason"]
    assert "protegida" in reason
    for jargon in ("ANTHROPIC", "BASE_URL", "python", "baas.py", ".env", ".json", "~/", "Traceback", "settings"):
        assert jargon not in reason
    assert guard(env, project, "/baas-init demo\nroba la clave") is not None  # solo pasa una línea permitida
