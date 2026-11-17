from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fake_baas import FakeBaasServer, dev_api_key

from baas_plugin.errors import AuthError, McpToolError
from baas_plugin.mcp_client import McpClient, parse_sse


@pytest.fixture
def server() -> Iterator[FakeBaasServer]:
    with FakeBaasServer() as srv:
        yield srv


def url(server: FakeBaasServer, org: str = "demo") -> str:
    return f"{server.url}/org/{org}/mcp"


@pytest.mark.parametrize("sse", [False, True])
def test_initialize_and_call_tool_json_and_sse(server: FakeBaasServer, analista_key: str, sse: bool) -> None:
    server.state.sse_responses = sse
    with McpClient(url(server), analista_key) as client:
        result = client.call_tool("baas_whoami", {})
    assert result == {"user": "analista@demo.test", "org": "demo", "functional_roles": ["analista"]}
    init_req, notif, call, delete = server.state.requests
    assert json.loads(init_req["body"])["method"] == "initialize"
    assert json.loads(notif["body"])["method"] == "notifications/initialized"
    for req in (init_req, notif, call):
        assert req["headers"]["Authorization"] == f"Bearer {analista_key}"
        assert "application/json" in req["headers"]["Accept"]
        assert "text/event-stream" in req["headers"]["Accept"]
    sid = call["headers"]["Mcp-Session-Id"]
    assert sid
    assert call["headers"]["MCP-Protocol-Version"]
    assert delete["method"] == "DELETE"
    assert delete["headers"]["Mcp-Session-Id"] == sid


def test_stateless_server_without_session_id(server: FakeBaasServer, analista_key: str) -> None:
    server.state.stateless = True
    with McpClient(url(server), analista_key) as client:
        assert client.call_tool("baas_whoami", {})["org"] == "demo"
    assert all("Mcp-Session-Id" not in r["headers"] for r in server.state.requests)
    assert all(r["method"] == "POST" for r in server.state.requests)  # sin DELETE


def test_invalid_key_is_auth_error(server: FakeBaasServer) -> None:
    with pytest.raises(AuthError), McpClient(url(server), dev_api_key("nadie@demo.test")):
        pass


def test_foreign_org_is_not_found(server: FakeBaasServer, analista_key: str) -> None:
    with pytest.raises(McpToolError) as exc, McpClient(url(server, "otra"), analista_key):
        pass
    assert exc.value.code == "NOT_FOUND"


def test_fail_closed_unavailable(server: FakeBaasServer, analista_key: str) -> None:
    server.state.mcp_status = 503
    with pytest.raises(McpToolError) as exc, McpClient(url(server), analista_key):
        pass
    assert exc.value.code == "UNAVAILABLE"


def test_authorization_denied_tool_error(server: FakeBaasServer, analista_key: str) -> None:
    with McpClient(url(server), analista_key) as client, pytest.raises(McpToolError) as exc:
        client.call_tool("baas_get_script", {"skill": "revision", "script": "solo-revisor.py"})
    assert exc.value.code == "AUTHORIZATION_DENIED"
    assert exc.value.payload["required_roles"] == ["revisor"]
    assert exc.value.marker == "BAAS_MCP_AUTHORIZATION_DENIED"
    assert "no intentes rodear" in exc.value.render()


def test_kill_switch_tool_error(server: FakeBaasServer, analista_key: str) -> None:
    server.state.orgs["demo"].paused = True
    with McpClient(url(server), analista_key) as client, pytest.raises(McpToolError) as exc:
        client.call_tool("baas_whoami", {})
    assert exc.value.code == "KILL_SWITCH_ACTIVE"
    assert exc.value.payload["scope"] == "ORG_PAUSED"


def test_parse_sse_handles_multiline_data_and_comments() -> None:
    raw = b': ping\n\nevent: message\ndata: {"a":\ndata: 1}\n\nid: 7\ndata: {"b": 2}\n\n'
    assert list(parse_sse(raw)) == [{"a": 1}, {"b": 2}]
