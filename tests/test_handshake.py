from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fake_baas import FakeBaasServer, dev_api_key

from baas_plugin import handshake
from baas_plugin.errors import (
    AuthError,
    BaasError,
    NetworkError,
    NoOrganizationsError,
    OrgNotFoundError,
    SelectionRequiredError,
)


@pytest.fixture
def server() -> Iterator[FakeBaasServer]:
    with FakeBaasServer() as srv:
        yield srv


def test_handshake_sends_bearer_and_parses_contract(server: FakeBaasServer, analista_key: str) -> None:
    hs = handshake.perform_handshake(server.url, analista_key)
    req = server.state.requests[-1]
    assert req["method"] == "POST"
    assert req["path"] == "/api/v1/plugin/handshake"
    assert req["headers"]["Authorization"] == f"Bearer {analista_key}"
    assert json.loads(req["body"]) == {}
    assert hs.email == "analista@demo.test"
    assert hs.platform_role == "USER"
    assert hs.global_kill_switch is False
    [org] = hs.organizations
    assert org.slug == "demo"
    assert org.functional_roles == ("analista",)
    assert org.mcp_url == f"{server.url}/org/demo/mcp"
    assert org.proxy_anthropic_url == f"{server.url}/proxy/org/demo/anthropic"


def test_handshake_invalid_key_is_auth_error(server: FakeBaasServer) -> None:
    with pytest.raises(AuthError) as exc:
        handshake.perform_handshake(server.url, dev_api_key("nadie@demo.test"))
    assert "ya no vale" in exc.value.message
    assert "/baas-init" in exc.value.render()
    assert "set-key" not in exc.value.render()


def test_handshake_without_network_is_network_error() -> None:
    with pytest.raises(NetworkError):
        handshake.perform_handshake("http://127.0.0.1:9", dev_api_key("a@b.test"), timeout=2)


@pytest.mark.parametrize(
    ("status", "fragment"), [(429, "demasiadas conexiones"), (503, "no está disponible"), (500, "no está disponible")]
)
def test_handshake_server_errors(server: FakeBaasServer, analista_key: str, status: int, fragment: str) -> None:
    server.state.handshake_status = status
    with pytest.raises(NetworkError) as exc:
        handshake.perform_handshake(server.url, analista_key)
    assert fragment in exc.value.message
    assert "HTTP" not in exc.value.render()


def test_parse_rejects_insecure_service_urls() -> None:
    body = {
        "user": {"id": "u", "email": "e@x.test", "platform_role": "USER"},
        "global_kill_switch": False,
        "organizations": [
            {
                "slug": "demo",
                "name": "Demo",
                "governance_role": "USER",
                "functional_roles": [],
                "paused": False,
                "blocked": False,
                "ai_ip_enabled": False,
                "mcp_url": "http://mcp.example.com/org/demo/mcp",
                "proxy_anthropic_url": "https://proxy.example.com/org/demo/anthropic",
                "proxy_openai_url": "https://proxy.example.com/org/demo/openai/v1",
            }
        ],
    }
    with pytest.raises(BaasError):
        handshake.parse_handshake(body)


def test_parse_rejects_malformed_response() -> None:
    with pytest.raises(BaasError):
        handshake.parse_handshake({"user": {}})


def _hs(server: FakeBaasServer, email: str) -> handshake.Handshake:
    return handshake.perform_handshake(server.url, dev_api_key(email))


def test_select_zero_orgs_warns(server: FakeBaasServer) -> None:
    with pytest.raises(NoOrganizationsError):
        handshake.select_organization(_hs(server, "sinorg@demo.test"), requested=None, current=None)


def test_select_single_org_is_automatic(server: FakeBaasServer) -> None:
    org = handshake.select_organization(_hs(server, "analista@demo.test"), requested=None, current=None)
    assert org.slug == "demo"


def test_select_many_orgs_requires_choice_and_lists_them(server: FakeBaasServer) -> None:
    hs = _hs(server, "multi@demo.test")
    with pytest.raises(SelectionRequiredError) as exc:
        handshake.select_organization(hs, requested=None, current=None)
    assert "demo" in exc.value.message
    assert "otra" in exc.value.message


def test_select_many_orgs_keeps_current_binding(server: FakeBaasServer) -> None:
    org = handshake.select_organization(_hs(server, "multi@demo.test"), requested=None, current="otra")
    assert org.slug == "otra"


def test_select_requested_org_wins_over_current(server: FakeBaasServer) -> None:
    org = handshake.select_organization(_hs(server, "multi@demo.test"), requested="demo", current="otra")
    assert org.slug == "demo"


def test_select_unknown_requested_org(server: FakeBaasServer) -> None:
    with pytest.raises(OrgNotFoundError):
        handshake.select_organization(_hs(server, "multi@demo.test"), requested="ajena", current=None)


def test_select_current_binding_lost_with_many_orgs_requires_choice(server: FakeBaasServer) -> None:
    with pytest.raises(SelectionRequiredError):
        handshake.select_organization(_hs(server, "multi@demo.test"), requested=None, current="perdida")


def test_kill_switch_warnings(server: FakeBaasServer) -> None:
    server.state.global_kill_switch = True
    server.state.orgs["demo"].blocked = True
    server.state.orgs["demo"].paused = True
    hs = _hs(server, "analista@demo.test")
    warnings = handshake.kill_switch_warnings(hs, hs.organizations[0])
    text = "\n".join(warnings)
    assert "plataforma está detenida temporalmente" in text
    assert "bloqueada por la plataforma" in text
    assert "pausada por su administrador" in text


def test_no_kill_switch_warnings_when_all_clear(server: FakeBaasServer) -> None:
    hs = _hs(server, "analista@demo.test")
    assert handshake.kill_switch_warnings(hs, hs.organizations[0]) == []
