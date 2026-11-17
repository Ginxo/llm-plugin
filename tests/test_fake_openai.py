"""OpenAI falso del spike S13 (`dev/fake_openai.py`): Responses y Chat Completions, con registro de cabeceras."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator

import pytest
from fake_openai import TEXT, FakeOpenAI


@pytest.fixture
def fake() -> Iterator[FakeOpenAI]:
    server = FakeOpenAI()
    yield server
    server.close()


def _post(url: str, payload: dict, headers: dict[str, str] | None = None) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def _events(raw: bytes) -> list[dict]:
    return [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]


def test_responses_streaming(fake: FakeOpenAI) -> None:
    status, ctype, body = _post(f"{fake.url}/v1/responses", {"model": "gpt-fake", "input": "hola", "stream": True})
    assert status == 200
    assert ctype.startswith("text/event-stream")
    events = _events(body)
    types = [e["type"] for e in events]
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    assert "response.output_text.delta" in types
    done = events[-1]["response"]
    assert done["status"] == "completed"
    assert done["output"][0]["content"][0]["text"] == TEXT
    assert done["usage"]["input_tokens"] > 0
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    assert b"event: response.completed\n" in body


def test_responses_json(fake: FakeOpenAI) -> None:
    status, ctype, body = _post(f"{fake.url}/v1/responses", {"model": "gpt-fake", "input": "hola"})
    data = json.loads(body)
    assert status == 200
    assert ctype.startswith("application/json")
    assert data["object"] == "response"
    assert data["output"][0]["content"][0]["text"] == TEXT


def test_chat_completions_json_and_stream(fake: FakeOpenAI) -> None:
    msg = {"model": "gpt-fake", "messages": [{"role": "user", "content": "hola"}]}
    _, _, body = _post(f"{fake.url}/v1/chat/completions", msg)
    assert json.loads(body)["choices"][0]["message"]["content"] == TEXT
    _, ctype, body = _post(f"{fake.url}/v1/chat/completions", {**msg, "stream": True})
    assert ctype.startswith("text/event-stream")
    assert body.rstrip().endswith(b"data: [DONE]")
    assert "".join(e["choices"][0]["delta"].get("content", "") for e in _events(body[: body.rindex(b"data: [DONE]")]))


def test_records_headers_without_secret_values(fake: FakeOpenAI) -> None:
    _post(
        f"{fake.url}/v1/responses",
        {"model": "gpt-fake", "input": "hola"},
        {"Authorization": "Bearer sk-fake-123456789", "X-Baas-Api-Key": "baas_secreto", "originator": "codex_exec"},
    )
    rec = fake.requests[-1]
    assert rec["method"] == "POST"
    assert rec["path"] == "/v1/responses"
    assert rec["x_baas_headers"] == ["x-baas-api-key"]
    assert rec["authorization"] == "Bearer sk-fak…"
    assert rec["headers"]["originator"] == "codex_exec"
    assert "baas_secreto" not in json.dumps(rec)
    assert "sk-fake-123456789" not in json.dumps(rec)


def test_models_and_unknown_path(fake: FakeOpenAI) -> None:
    with urllib.request.urlopen(f"{fake.url}/v1/models", timeout=5) as resp:
        assert json.loads(resp.read())["data"][0]["id"] == "gpt-fake"
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{fake.url}/v1/otra", {})
    assert exc.value.code == 404
