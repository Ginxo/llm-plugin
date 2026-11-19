from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from baas_plugin import defaults, device
from baas_plugin.envfile import BaasPaths
from baas_plugin.errors import (
    ApprovalDeniedError,
    ApprovalExpiredError,
    ApprovalPendingError,
    ApprovalWaitingError,
    BaasError,
    NetworkError,
)

API_KEY = "baas_" + "A" * 43
CONTRACT_START = {
    "device_code": "dev-code-123",
    "user_code": "KQRT-7MWB",
    "verification_url": "http://localhost/connect?code=KQRT-7MWB",
    "expires_in": 600,
    "interval": 3,
}


class FakeDeviceServer:
    """Servidor HTTP falso (hilo) con las dos rutas públicas del contrato de dispositivo."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.start_response: tuple[int, Any] = (200, dict(CONTRACT_START))
        self.poll_responses: list[tuple[int, Any, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.requests.append({"path": self.path, "body": json.loads(body or b"null")})
                headers: dict[str, str] = {}
                if self.path.endswith("/device/start"):
                    status, payload = outer.start_response
                elif self.path.endswith("/device/poll"):
                    status, payload, headers = (
                        outer.poll_responses.pop(0) if outer.poll_responses else (200, {"status": "pending"}, {})
                    )
                else:
                    status, payload = 404, {}
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def __enter__(self) -> FakeDeviceServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


@pytest.fixture
def server() -> Iterator[FakeDeviceServer]:
    with FakeDeviceServer() as srv:
        yield srv


# --- start -------------------------------------------------------------------------------------


def test_start_parses_contract(server: FakeDeviceServer) -> None:
    flow = device.start(server.url, "mi-proyecto")
    assert flow == device.DeviceStart(
        device_code="dev-code-123",
        user_code="KQRT-7MWB",
        verification_url="http://localhost/connect?code=KQRT-7MWB",
        expires_in=600,
        interval=3,
    )
    assert server.requests == [{"path": "/api/v1/plugin/device/start", "body": {"client_name": "mi-proyecto"}}]


def test_start_without_server_is_network_error_in_plain_spanish() -> None:
    with pytest.raises(NetworkError) as exc:
        device.start("http://127.0.0.1:9", "x", timeout=2)
    text = exc.value.render()
    for forbidden in ("~/", ".env", "baas.py", "ANTHROPIC_BASE_URL", "BAAS_APP_URL"):
        assert forbidden not in text


@pytest.mark.parametrize("status", [429, 500, 503])
def test_start_server_errors_are_network_errors(server: FakeDeviceServer, status: int) -> None:
    server.start_response = (status, {})
    with pytest.raises(NetworkError):
        device.start(server.url, "x")


@pytest.mark.parametrize(
    "payload",
    [
        {"device_code": "d"},
        {**CONTRACT_START, "expires_in": "diez"},
        {**CONTRACT_START, "verification_url": "ftp://x/connect"},
        {**CONTRACT_START, "device_code": ""},
        ["no", "es", "un", "objeto"],
        b"<html>no es json</html>",
    ],
)
def test_start_rejects_responses_outside_the_contract(server: FakeDeviceServer, payload: Any) -> None:
    server.start_response = (200, payload)
    with pytest.raises(NetworkError):
        device.start(server.url, "x")


# --- poll --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "denied", "expired"])
def test_poll_maps_plain_states(server: FakeDeviceServer, status: str) -> None:
    server.poll_responses = [(200, {"status": status}, {})]
    result = device.poll(server.url, "dev-code-123")
    assert result == device.DevicePoll(status=status, api_key=None, email=None)
    assert server.requests[-1] == {"path": "/api/v1/plugin/device/poll", "body": {"device_code": "dev-code-123"}}


def test_poll_approved_returns_key_and_email(server: FakeDeviceServer) -> None:
    server.poll_responses = [(200, {"status": "approved", "api_key": API_KEY, "email": "ana@demo.test"}, {})]
    result = device.poll(server.url, "dev-code-123")
    assert result.status == "approved"
    assert result.api_key == API_KEY
    assert result.email == "ana@demo.test"
    assert API_KEY not in repr(result)


def test_poll_429_is_pending(server: FakeDeviceServer) -> None:
    server.poll_responses = [(429, {"detail": "slow down"}, {"Retry-After": "2"})]
    assert device.poll(server.url, "c").status == "pending"


@pytest.mark.parametrize(
    "response",
    [
        (500, {}, {}),
        (200, {"status": "raro"}, {}),
        (200, {"status": "approved"}, {}),
        (200, {"status": "approved", "api_key": API_KEY}, {}),
        (200, ["x"], {}),
    ],
)
def test_poll_unexpected_responses_are_network_errors(
    server: FakeDeviceServer, response: tuple[int, Any, dict[str, str]]
) -> None:
    server.poll_responses = [response]
    with pytest.raises(NetworkError) as exc:
        device.poll(server.url, "c")
    assert API_KEY not in exc.value.render()


def test_poll_without_server_is_network_error() -> None:
    with pytest.raises(NetworkError):
        device.poll("http://127.0.0.1:9", "c", timeout=2)


# --- wait --------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def test_wait_returns_approved_on_second_poll(server: FakeDeviceServer) -> None:
    server.poll_responses = [
        (200, {"status": "pending"}, {}),
        (200, {"status": "approved", "api_key": API_KEY, "email": "ana@demo.test"}, {}),
    ]
    clock = Clock()
    result = device.wait(server.url, "c", interval=3, max_seconds=60, sleep=clock.sleep, now=clock.now)
    assert result.status == "approved"
    assert result.api_key == API_KEY
    assert clock.sleeps == [3]
    assert len(server.requests) == 2


def test_wait_returns_pending_when_time_runs_out(server: FakeDeviceServer) -> None:
    clock = Clock()
    result = device.wait(server.url, "c", interval=3, max_seconds=10, sleep=clock.sleep, now=clock.now)
    assert result.status == "pending"
    assert sum(clock.sleeps) <= 10
    assert len(server.requests) >= 2


@pytest.mark.parametrize("status", ["denied", "expired"])
def test_wait_stops_on_terminal_states(server: FakeDeviceServer, status: str) -> None:
    server.poll_responses = [(200, {"status": status}, {})]
    clock = Clock()
    assert device.wait(server.url, "c", interval=3, max_seconds=60, sleep=clock.sleep, now=clock.now).status == status
    assert clock.sleeps == []


def test_wait_honors_retry_after_capped(server: FakeDeviceServer) -> None:
    server.poll_responses = [
        (429, {}, {"Retry-After": "5"}),
        (429, {}, {"Retry-After": "3600"}),
        (200, {"status": "denied"}, {}),
    ]
    clock = Clock()
    result = device.wait(server.url, "c", interval=1, max_seconds=600, sleep=clock.sleep, now=clock.now)
    assert result.status == "denied"
    assert clock.sleeps == [5, device.MAX_RETRY_AFTER]
    assert device.MAX_RETRY_AFTER <= 10


# --- estado pendiente --------------------------------------------------------------------------


def _flow(expires_at: float = 1000.0) -> device.PendingFlow:
    return device.PendingFlow(
        app_url="https://app.example.com",
        device_code="dev-code-123",
        user_code="KQRT-7MWB",
        verification_url="https://app.example.com/connect?code=KQRT-7MWB",
        expires_at=expires_at,
    )


@pytest.fixture
def paths(tmp_path: Path) -> BaasPaths:
    return BaasPaths(tmp_path / "baas-ai-ip")


def test_pending_roundtrip_is_private(paths: BaasPaths) -> None:
    device.save_pending(paths, _flow())
    pending_file = paths.root / "pending.json"
    assert pending_file.exists()
    assert paths.pending_file == pending_file
    if os.name == "posix":
        assert stat.S_IMODE(pending_file.stat().st_mode) == 0o600
    assert device.load_pending(paths, now=999.0) == _flow()
    assert list(paths.root.glob(".pending.json.*")) == []  # sin temporales


def test_pending_never_contains_the_api_key(paths: BaasPaths) -> None:
    device.save_pending(paths, _flow())
    text = paths.pending_file.read_text(encoding="utf-8")
    assert "api_key" not in text
    assert "baas_" not in text


def test_pending_expired_returns_none_and_deletes(paths: BaasPaths) -> None:
    device.save_pending(paths, _flow(expires_at=1000.0))
    assert device.load_pending(paths, now=1000.0) is None
    assert not paths.pending_file.exists()


@pytest.mark.parametrize(
    "content",
    ["{no es json", "[]", "{}", json.dumps({**_flow().__dict__, "expires_at": "mañana"}), json.dumps({"app_url": 3})],
)
def test_pending_damaged_returns_none_and_deletes(paths: BaasPaths, content: str) -> None:
    paths.root.mkdir(parents=True)
    paths.pending_file.write_text(content, encoding="utf-8")
    assert device.load_pending(paths, now=0.0) is None
    assert not paths.pending_file.exists()


def test_load_pending_without_file_is_none(paths: BaasPaths) -> None:
    assert device.load_pending(paths, now=0.0) is None


def test_clear_pending_is_idempotent(paths: BaasPaths) -> None:
    device.clear_pending(paths)
    device.save_pending(paths, _flow())
    device.clear_pending(paths)
    device.clear_pending(paths)
    assert not paths.pending_file.exists()


# --- resolve_app_url ---------------------------------------------------------------------------


def test_resolve_app_url_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(defaults, "DEFAULT_APP_URL", "https://default.example.com")
    env = {"BAAS_APP_URL": "https://env.example.com"}
    assert device.resolve_app_url("https://explicit.example.com", env, "https://saved.example.com") == (
        "https://explicit.example.com"
    )
    assert device.resolve_app_url(None, env, "https://saved.example.com") == "https://env.example.com"
    assert device.resolve_app_url(None, {}, "https://saved.example.com") == "https://saved.example.com"
    assert device.resolve_app_url(None, {}, None) == "https://default.example.com"
    assert device.resolve_app_url("", {"BAAS_APP_URL": "  "}, "") == "https://default.example.com"


def test_resolve_app_url_none_when_nothing_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(defaults, "DEFAULT_APP_URL", "")
    assert device.resolve_app_url(None, {}, None) is None


def test_default_app_url_is_empty_in_repo() -> None:
    assert defaults.DEFAULT_APP_URL == ""


# --- errores -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "code", "marker"),
    [
        (ApprovalPendingError, 20, "BAAS_APPROVAL_PENDING"),
        (ApprovalWaitingError, 21, "BAAS_APPROVAL_WAITING"),
        (ApprovalDeniedError, 22, "BAAS_APPROVAL_DENIED"),
        (ApprovalExpiredError, 23, "BAAS_APPROVAL_EXPIRED"),
    ],
)
def test_approval_errors_are_plain_and_stable(cls: type[BaasError], code: int, marker: str) -> None:
    err = cls()
    assert isinstance(err, BaasError)
    assert (err.exit_code, err.marker) == (code, marker)
    text = err.render()
    for forbidden in ("~/", ".env", "baas.py", "ANTHROPIC_BASE_URL", "exit", "proxy", "hook"):
        assert forbidden not in text


def test_pending_error_carries_the_link() -> None:
    err = ApprovalPendingError(verification_url="https://app.example.com/connect?code=KQRT-7MWB", user_code="KQRT-7MWB")
    assert "https://app.example.com/connect?code=KQRT-7MWB" in err.render()
    assert "KQRT-7MWB" in err.render()
