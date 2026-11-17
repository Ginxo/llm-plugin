"""Prueba de punta a punta de AI-IP (`dev/e2e_h3.py`).

La pila real de H3 se integra al final; aquí se prueban las utilidades puras y el recorrido completo contra la pila
simulada de `dev/fake_h3_stack.py`, también con cada fallo que la prueba tiene que detectar. Con `unittest` (se
ejecutan con `pytest` en la CI y con `python -m unittest discover tests`).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))

import e2e_h3

DNI, EMAIL, IBAN = "12345678Z", "ana.perez@privada.test", "ES91 2100 0418 4502 0005 1332"
RESTORED = f"Eco: DNI {DNI}, email {EMAIL}, IBAN {IBAN}"
TOKENIZED = "Eco: DNI [[DNI_001]], email [[EMAIL_001]], IBAN [[IBAN_001]]"


def sse(events: list[dict], *, named: bool = True) -> bytes:
    out = []
    for event in events:
        head = f"event: {event['type']}\n" if named and "type" in event else ""
        out.append(f"{head}data: {json.dumps(event)}\n\n")
    return "".join(out).encode()


def chunks(text: str, size: int = 3) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class ParseSseTest(unittest.TestCase):
    def test_events_comments_multiline_and_crlf(self) -> None:
        raw = ': comentario\r\nevent: uno\r\ndata: {"a":\r\ndata: 1}\r\n\r\nid: 7\nretry: 10\ndata: [DONE]\n\n'
        self.assertEqual(e2e_h3.parse_sse(raw), [("uno", '{"a":\n1}'), (None, "[DONE]")])

    def test_last_event_without_blank_line(self) -> None:
        self.assertEqual(e2e_h3.parse_sse("data: x"), [(None, "x")])


class ExtractReplyTest(unittest.TestCase):
    def test_messages_json_text_and_tool(self) -> None:
        body = {
            "content": [
                {"type": "text", "text": RESTORED},
                {"type": "tool_use", "id": "t", "name": "echo", "input": {"text": RESTORED}},
            ]
        }
        reply = e2e_h3.extract_reply("messages", json.dumps(body).encode(), stream=False)
        self.assertEqual([t for _, t in reply.texts], [RESTORED])
        self.assertEqual(reply.tool_calls[0].name, "echo")
        self.assertEqual(reply.tool_calls[0].arguments, {"text": RESTORED})

    def test_messages_stream_assembles_split_deltas(self) -> None:
        args = json.dumps({"text": RESTORED})
        events = [
            {"type": "message_start", "message": {"content": []}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            *(
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": c}}
                for c in chunks(RESTORED)
            ),
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "t", "name": "echo", "input": {}},
            },
            *(
                {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": c}}
                for c in chunks(args, 5)
            ),
            {"type": "content_block_stop", "index": 1},
            {"type": "message_stop"},
        ]
        reply = e2e_h3.extract_reply("messages", sse(events), stream=True)
        self.assertEqual([t for _, t in reply.texts], [RESTORED])
        self.assertEqual([(c.name, c.arguments) for c in reply.tool_calls], [("echo", args)])

    def test_chat_json_and_stream(self) -> None:
        args = json.dumps({"text": RESTORED})
        body = {
            "choices": [
                {
                    "message": {
                        "content": RESTORED,
                        "tool_calls": [{"id": "c", "function": {"name": "echo", "arguments": args}}],
                    }
                }
            ]
        }
        reply = e2e_h3.extract_reply("chat", json.dumps(body).encode(), stream=False)
        self.assertEqual([t for _, t in reply.texts], [RESTORED])
        self.assertEqual([(c.name, c.arguments) for c in reply.tool_calls], [("echo", args)])

        first = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {"index": 0, "id": "c", "type": "function", "function": {"name": "echo", "arguments": ""}}
                        ],
                    },
                }
            ]
        }
        parts = [
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": c}}]}}]}
            for c in chunks(args, 4)
        ]
        text = [{"choices": [{"index": 0, "delta": {"content": c}}]} for c in chunks(RESTORED, 2)]
        last = {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
        raw = sse([first, *text, *parts, last], named=False) + b"data: [DONE]\n\n"
        reply = e2e_h3.extract_reply("chat", raw, stream=True)
        self.assertEqual([t for _, t in reply.texts], [RESTORED])
        self.assertEqual([(c.name, c.arguments) for c in reply.tool_calls], [("echo", args)])

    def test_responses_json(self) -> None:
        args = json.dumps({"text": RESTORED})
        body = {
            "id": "resp_1",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": RESTORED}]},
                {"type": "function_call", "name": "echo", "arguments": args},
            ],
        }
        reply = e2e_h3.extract_reply("responses", json.dumps(body).encode(), stream=False)
        self.assertEqual(reply.response_id, "resp_1")
        self.assertEqual([t for _, t in reply.texts], [RESTORED])
        self.assertEqual([(c.name, c.arguments) for c in reply.tool_calls], [("echo", args)])

    def test_responses_stream_collects_every_copy(self) -> None:
        msg = {"type": "message", "id": "m", "content": [{"type": "output_text", "text": "copia completa"}]}
        events = [
            {"type": "response.created", "response": {"id": "resp_9", "output": []}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "message", "id": "m", "content": []},
            },
            *(
                {
                    "type": "response.output_text.delta",
                    "item_id": "m",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": c,
                }
                for c in chunks(RESTORED)
            ),
            {"type": "response.output_text.done", "item_id": "m", "content_index": 0, "text": "texto done"},
            {
                "type": "response.content_part.done",
                "item_id": "m",
                "content_index": 0,
                "part": {"type": "output_text", "text": "parte done"},
            },
            {"type": "response.output_item.done", "output_index": 0, "item": msg},
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"type": "function_call", "id": "f", "name": "echo", "arguments": ""},
            },
            {"type": "response.function_call_arguments.delta", "item_id": "f", "output_index": 1, "delta": '{"te'},
            {"type": "response.function_call_arguments.delta", "item_id": "f", "output_index": 1, "delta": 'xt": 1}'},
            {"type": "response.function_call_arguments.done", "item_id": "f", "output_index": 1, "arguments": "{}"},
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {"type": "function_call", "id": "f", "name": "echo", "arguments": "[]"},
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_9",
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": "completed"}]},
                        {"type": "function_call", "name": "echo", "arguments": '{"a": 2}'},
                    ],
                },
            },
        ]
        reply = e2e_h3.extract_reply("responses", sse(events), stream=True)
        self.assertEqual(reply.response_id, "resp_9")
        self.assertEqual(
            [t for _, t in reply.texts], [RESTORED, "texto done", "parte done", "copia completa", "completed"]
        )
        self.assertEqual(
            [(c.name, c.arguments) for c in reply.tool_calls],
            [("echo", '{"text": 1}'), ("echo", "{}"), ("echo", "[]"), ("echo", '{"a": 2}')],
        )
        self.assertTrue(all(where for where, _ in reply.texts))

    def test_invalid_json_body_raises_step_error(self) -> None:
        with self.assertRaises(e2e_h3.StepError):
            e2e_h3.extract_reply("chat", b"<html>", stream=False)


class ChecksTest(unittest.TestCase):
    def test_restoration_problems(self) -> None:
        self.assertEqual(e2e_h3.restoration_problems(RESTORED), [])
        problems = e2e_h3.restoration_problems(TOKENIZED)
        self.assertTrue(any("[[" in p for p in problems))
        self.assertTrue(any(DNI in p for p in problems))
        self.assertTrue(any("[[" in p for p in e2e_h3.restoration_problems(RESTORED + " [[X")))

    def test_tool_problems(self) -> None:
        self.assertEqual(e2e_h3.tool_problems(json.dumps({"text": RESTORED})), [])
        self.assertEqual(e2e_h3.tool_problems({"text": RESTORED}), [])
        self.assertIn("JSON inválido", " ".join(e2e_h3.tool_problems('{"text": "a"b"}')))
        self.assertIn("objeto", " ".join(e2e_h3.tool_problems('"texto"')))
        self.assertTrue(e2e_h3.tool_problems(json.dumps({"text": TOKENIZED})))
        nested = json.dumps({"datos": {"lista": [RESTORED]}})
        self.assertEqual(e2e_h3.tool_problems(nested), [])

    def test_debug_text_body_as_object_or_text(self) -> None:
        payload = {"messages": [{"role": "user", "content": "caso-x [[DNI_001]] anaé"}]}
        as_object = {"path": "/v1/messages", "body": payload}
        as_text = {"path": "/v1/messages", "body": json.dumps(payload)}  # con escapes é
        for entry in (as_object, as_text):
            text = e2e_h3.debug_text(entry)
            self.assertIn("[[DNI_001]]", text)
            self.assertIn("anaé", text)
            self.assertEqual(e2e_h3.tagged([entry, {"path": "/x", "body": {}}], "caso-x"), [entry])

    def test_debug_text_finds_values_written_with_json_escapes(self) -> None:
        escaped = '{"m": "ana.perez\\u0040privada.test"}'
        self.assertIn(EMAIL, e2e_h3.debug_text({"path": "/v1/messages", "body": escaped}))

    def test_same_payload(self) -> None:
        payload = {"model": "m", "messages": [{"role": "user", "content": "á"}]}
        raw = e2e_h3.encode(payload)
        self.assertEqual(e2e_h3.same_payload(raw, payload), (True, False))
        self.assertEqual(e2e_h3.same_payload(raw, raw.decode()), (True, True))
        self.assertEqual(e2e_h3.same_payload(raw, json.dumps(payload, indent=1)), (True, False))
        self.assertEqual(e2e_h3.same_payload(raw, {**payload, "extra": 1}), (False, False))
        self.assertEqual(e2e_h3.same_payload(raw, "no es json"), (False, False))

    def test_provider_error(self) -> None:
        anthropic = {"type": "error", "error": {"type": "permission_error", "message": "eliminada"}}
        openai = {
            "error": {"message": "m", "type": "permission_error", "param": None, "code": "baas_conversation_deleted"}
        }
        self.assertEqual(e2e_h3.provider_error(json.dumps(anthropic).encode()), ("permission_error", None, "eliminada"))
        self.assertEqual(
            e2e_h3.provider_error(json.dumps(openai).encode()), ("permission_error", "baas_conversation_deleted", "m")
        )
        self.assertEqual(e2e_h3.provider_error(b"nada"), (None, None, ""))


class RequestsTest(unittest.TestCase):
    def test_tag_has_no_digits(self) -> None:
        nonce = e2e_h3.new_nonce()
        self.assertTrue(nonce.isalpha() and nonce.islower())
        tag = e2e_h3.case_tag(nonce, "texto", "responses", "stream")
        self.assertFalse(any(ch.isdigit() for ch in tag))

    def test_request_bodies(self) -> None:
        msg = e2e_h3.request_body("messages", "m", "hola", stream=True, tool=True)
        self.assertEqual(msg["messages"][-1], {"role": "user", "content": "hola"})
        self.assertTrue(msg["stream"])
        self.assertEqual(msg["tools"][0]["name"], "echo")
        chat = e2e_h3.request_body("chat", "m", "hola", stream=False, tool=False)
        self.assertEqual(chat["messages"][-1], {"role": "user", "content": "hola"})
        self.assertNotIn("tools", chat)
        self.assertFalse(chat["stream"])
        resp = e2e_h3.request_body("responses", "m", "hola", stream=False, tool=True)
        self.assertEqual(resp["input"][-1]["content"][0], {"type": "input_text", "text": "hola"})
        self.assertEqual(resp["tools"][0]["name"], "echo")

    def test_urls_from_binding(self) -> None:
        binding = {
            "proxy_anthropic_url": "http://p/org/privada/anthropic",
            "proxy_openai_url": "http://p/org/privada/openai/v1",
        }
        self.assertEqual(e2e_h3.api_url(binding, "messages"), "http://p/org/privada/anthropic/v1/messages")
        self.assertEqual(e2e_h3.api_url(binding, "chat"), "http://p/org/privada/openai/v1/chat/completions")
        self.assertEqual(e2e_h3.api_url(binding, "responses"), "http://p/org/privada/openai/v1/responses")
        self.assertEqual(e2e_h3.UPSTREAM_PATHS["chat"], "/v1/chat/completions")

    def test_proxy_override_keeps_contract_paths(self) -> None:
        binding = {"slug": "privada", "proxy_anthropic_url": "http://proxy.localhost:8080/org/privada/anthropic"}
        moved = e2e_h3.override_proxy(binding, "http://127.0.0.1:9999/")
        self.assertEqual(moved["proxy_anthropic_url"], "http://127.0.0.1:9999/org/privada/anthropic")
        self.assertEqual(moved["proxy_openai_url"], "http://127.0.0.1:9999/org/privada/openai/v1")


class ParseArgsTest(unittest.TestCase):
    def test_defaults_follow_dev_stack_ports(self) -> None:
        env = {"BAAS_DEV_HTTP_PORT": "48080", "BAAS_DEV_FAKE_UPSTREAM_PORT": "48090"}
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("BAAS_E2E_APP_URL", "BAAS_E2E_FAKE_URL"):
                os.environ.pop(name, None)
            ns = e2e_h3.parse_args([])
        self.assertEqual(ns.app_url, "http://app.localhost:48080")
        self.assertEqual(ns.fake_url, "http://127.0.0.1:48090")
        self.assertEqual(ns.org, "privada")
        self.assertEqual(ns.email, "analista@privada.test")
        self.assertEqual(ns.admin_email, "orgadmin@privada.test")
        self.assertEqual((ns.bypass_email, ns.bypass_org), ("analista@demo.test", "demo"))
        self.assertEqual(ns.only, ["aiip", "bypass", "borrado"])
        self.assertEqual(ns.apis, ["messages", "chat", "responses"])
        self.assertIsNone(ns.proxy_url)

    def test_explicit_values(self) -> None:
        ns = e2e_h3.parse_args(
            [
                "--app-url",
                "http://a:1/",
                "--fake-url",
                "http://f:2/",
                "--proxy-url",
                "http://p:3/",
                "--only",
                "bypass,borrado",
                "--apis",
                "chat",
                "--trace-timeout",
                "5",
                "--settle",
                "0",
            ]
        )
        self.assertEqual((ns.app_url, ns.fake_url, ns.proxy_url), ("http://a:1", "http://f:2", "http://p:3"))
        self.assertEqual((ns.only, ns.apis), (["bypass", "borrado"], ["chat"]))
        self.assertEqual((ns.trace_timeout, ns.settle), (5.0, 0.0))

    def test_invalid_choice_exits_two(self) -> None:
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as ctx:
            e2e_h3.parse_args(["--apis", "gemini"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
