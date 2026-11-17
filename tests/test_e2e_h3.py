"""Prueba de punta a punta de AI-IP (`dev/e2e_h3.py`).

La pila real de H3 se integra al final; aquí se prueban las utilidades puras y el recorrido completo contra la pila
simulada de `dev/fake_h3_stack.py`, también con cada fallo que la prueba tiene que detectar. Con `unittest` (se
ejecutan con `pytest` en la CI y con `python -m unittest discover tests`).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))

import e2e_h3
import fake_h3_stack

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

    def test_first_difference(self) -> None:
        self.assertIsNone(e2e_h3.first_difference({"a": [1, {"b": "x"}]}, {"a": [1, {"b": "x"}]}))
        self.assertEqual(e2e_h3.first_difference({"a": [1, {"b": "x"}]}, {"a": [1, {"b": "y"}]}), '$.a[1].b: "x" ≠ "y"')
        self.assertEqual(e2e_h3.first_difference({"s": True}, {"s": 1}), "$.s: true ≠ 1")
        self.assertIn("sobra", e2e_h3.first_difference({}, {"extra": 1}))
        self.assertIn("falta", e2e_h3.first_difference({"model": "m"}, {}))
        self.assertIn("elementos", e2e_h3.first_difference([1], [1, 2]))

    def test_verification_problems(self) -> None:
        clean = {
            "status": "CLEAN",
            "request_replacements_by_type": {"DNI": 1, "EMAIL": 1, "IBAN": 1},
            "response_replacements_by_type": {"DNI": 2, "EMAIL": 1, "IBAN": 1},
            "unresolved_response_tokens": 0,
            "orphan_request_tokens": 0,
            "residual_matches": [],
        }
        self.assertEqual(e2e_h3.verification_problems(clean), [])
        self.assertTrue(e2e_h3.verification_problems(None))
        warning = {**clean, "status": "WARNING", "unresolved_response_tokens": 1}
        joined = " ".join(e2e_h3.verification_problems(warning))
        self.assertIn("WARNING", joined)
        self.assertIn("unresolved_response_tokens", joined)
        missing = {**clean, "request_replacements_by_type": {"DNI": 1}}
        self.assertIn("EMAIL, IBAN", " ".join(e2e_h3.verification_problems(missing)))


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
        self.assertEqual((ns.trace_timeout, ns.settle, ns.purge_wait), (30.0, 2.0, 1.0))

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
                "--purge-wait",
                "0.5",
            ]
        )
        self.assertEqual((ns.app_url, ns.fake_url, ns.proxy_url), ("http://a:1", "http://f:2", "http://p:3"))
        self.assertEqual((ns.only, ns.apis), (["bypass", "borrado"], ["chat"]))
        self.assertEqual((ns.trace_timeout, ns.settle, ns.purge_wait), (5.0, 0.0, 0.5))

    def test_invalid_choice_exits_two(self) -> None:
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as ctx:
            e2e_h3.parse_args(["--apis", "gemini"])
        self.assertEqual(ctx.exception.code, 2)


# --- recorrido completo contra la pila simulada ------------------------------------------------------


def run_e2e(stack: fake_h3_stack.FakeH3Stack, *extra: str) -> tuple[int, dict[str, dict], str]:
    """Ejecuta `e2e_h3.main` contra la pila simulada; devuelve el código, los pasos por nombre y la salida."""
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "informe.json"
        argv = ["--app-url", stack.app_url, "--fake-url", stack.fake_url, "--trace-timeout", "3",
                "--settle", "0.05", "--purge-wait", "0", "--timeout", "10", "--json", str(report_path),
                *extra]  # fmt: skip
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = e2e_h3.main(argv)
        report = json.loads(report_path.read_text())
    return code, {s["name"]: s for s in report["steps"]}, out.getvalue()


def statuses(steps: dict[str, dict], prefix: str) -> set[str]:
    return {s["status"] for name, s in steps.items() if name.startswith(prefix)}


class FullRunTest(unittest.TestCase):
    def assert_all_ok(self, code: int, steps: dict[str, dict], out: str) -> None:
        self.assertEqual(code, 0, out)
        self.assertEqual({s["status"] for s in steps.values()}, {"OK"}, out)

    def test_everything_passes_with_body_as_object(self) -> None:
        with fake_h3_stack.FakeH3Stack() as stack:
            code, steps, out = run_e2e(stack)
        self.assert_all_ok(code, steps, out)
        self.assertEqual(len([n for n in steps if n.startswith("4.")]), 6)
        self.assertEqual(len([n for n in steps if n.startswith("5.")]), 6)
        self.assertEqual(len([n for n in steps if n.startswith("7.")]), 6)
        self.assertEqual(len([n for n in steps if n.startswith("8.")]), 4)
        for prefix in ("0.", "1.", "2a.", "2b.", "3.", "6.", "9.", "10."):
            self.assertEqual(len([n for n in steps if n.startswith(prefix)]), 1, prefix)
        self.assertIn("RESULTADO: todo OK", out)

    def test_everything_passes_with_body_as_text(self) -> None:
        with fake_h3_stack.FakeH3Stack(debug_body="text") as stack:
            code, steps, out = run_e2e(stack)
        self.assert_all_ok(code, steps, out)
        self.assertIn("byte a byte", " ".join(s["detail"] for n, s in steps.items() if n.startswith("7.")))

    def test_only_bypass_and_one_api(self) -> None:
        with fake_h3_stack.FakeH3Stack() as stack:
            code, steps, out = run_e2e(stack, "--only", "bypass", "--apis", "chat")
        self.assert_all_ok(code, steps, out)
        self.assertEqual(
            sorted(steps),
            sorted(["0. webapp accesible", "1. proveedor falso: /_debug/requests",
                    "2b. handshake de analista@demo.test (demo, AI-IP apagado)",
                    "7.3 bypass · Chat Completions · sin streaming", "7.4 bypass · Chat Completions · streaming"]),
        )  # fmt: skip

    def test_stack_unreachable_fails(self) -> None:
        with fake_h3_stack.FakeH3Stack() as stack:
            app, fake = stack.app_url, stack.fake_url
        argv = ["--app-url", app, "--fake-url", fake, "--timeout", "2"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(e2e_h3.main(argv), 1)


class FaultsTest(unittest.TestCase):
    """Cada fallo inyectado en la pila simulada hace fallar el paso que lo comprueba (código 1)."""

    def run_fault(self, *faults: str, extra: tuple[str, ...] = ()) -> dict[str, dict]:
        with fake_h3_stack.FakeH3Stack(faults=set(faults)) as stack:
            code, steps, out = run_e2e(stack, *extra)
        self.assertEqual(code, 1, out)
        return steps

    def assert_failed(self, steps: dict[str, dict], prefix: str, *fragments: str) -> None:
        chosen = {n: s for n, s in steps.items() if n.startswith(prefix)}
        self.assertTrue(chosen, prefix)
        for name, step in chosen.items():
            self.assertEqual(step["status"], "FALLO", f"{name}: {step['detail']}")
            for fragment in fragments:
                self.assertIn(fragment, step["detail"], name)

    def assert_ok(self, steps: dict[str, dict], *prefixes: str) -> None:
        for prefix in prefixes:
            self.assertEqual(statuses(steps, prefix), {"OK"}, prefix)

    def test_value_in_clear_to_provider(self) -> None:
        steps = self.run_fault("leak")
        self.assert_failed(steps, "4.", "en claro")
        self.assert_failed(steps, "5.", "en claro")
        self.assert_failed(steps, "10.", "en claro")
        self.assert_ok(steps, "7.")

    def test_token_not_restored(self) -> None:
        steps = self.run_fault("no-rehydrate")
        self.assert_failed(steps, "4.", "[[DNI_001]]")
        self.assert_failed(steps, "5.", "[[DNI_001]]")
        self.assert_ok(steps, "7.", "10.")

    def test_responses_full_copies_not_restored(self) -> None:
        steps = self.run_fault("responses-done-unrestored")
        for name in ("4.6", "5.6"):
            self.assert_failed(steps, name, "response.completed", "sin restaurar")
        self.assert_ok(steps, "4.1", "4.2", "4.3", "4.4", "4.5", "5.1", "5.5")

    def test_invalid_tool_json(self) -> None:
        steps = self.run_fault("bad-tool-json")
        for name in ("5.2", "5.3", "5.4", "5.5", "5.6"):
            self.assert_failed(steps, name, "JSON inválido")
        self.assert_ok(steps, "4.", "5.1")

    def test_verification_not_clean(self) -> None:
        steps = self.run_fault("warning")
        self.assert_failed(steps, "6.", "WARNING")
        self.assert_ok(steps, "4.", "5.")

    def test_turn_not_in_webapp(self) -> None:
        steps = self.run_fault("no-trace", extra=("--trace-timeout", "0.3"))
        self.assert_failed(steps, "6.", "no aparece")
        self.assert_failed(steps, "8.1", "no aparece")
        self.assertEqual(statuses(steps, "8.4"), {"SALTADO"})

    def test_ai_ip_off_in_privada(self) -> None:
        steps = self.run_fault("aiip-off")
        self.assert_failed(steps, "2a.", "ai_ip_enabled")
        for prefix in ("4.", "5.", "6.", "8.", "9.", "10."):
            self.assertEqual(statuses(steps, prefix), {"SALTADO"}, prefix)

    def test_bypass_changes_body(self) -> None:
        steps = self.run_fault("bypass-mutates")
        self.assert_failed(steps, "7.", "no es idéntico", "baas_e2e")
        self.assert_ok(steps, "4.", "5.")

    def test_deleted_conversation_not_blocked(self) -> None:
        steps = self.run_fault("delete-not-blocking")
        for name in ("8.1", "8.2", "8.3"):
            self.assert_failed(steps, name, "se esperaba 403", "el proveedor recibió el turno")

    def test_deleted_conversation_wrong_error(self) -> None:
        steps = self.run_fault("delete-wrong-code")
        self.assert_failed(steps, "8.1", "eliminada")
        self.assert_failed(steps, "8.2", "baas_conversation_deleted")
        self.assert_failed(steps, "8.3", "baas_conversation_deleted")

    def test_webapp_does_not_delete(self) -> None:
        steps = self.run_fault("delete-not-removed")
        self.assert_failed(steps, "8.1", "se esperaba 404")

    def test_blocked_turn_recreates_conversation(self) -> None:
        steps = self.run_fault("blocked-emits-events")
        self.assert_ok(steps, "8.1", "8.2", "8.3")
        self.assert_failed(steps, "8.4", "reaparece")

    def test_fresh_conversation_blocked(self) -> None:
        steps = self.run_fault("fresh-blocked")
        self.assert_ok(steps, "8.1", "8.2", "8.3", "8.4")
        self.assert_failed(steps, "9.", "HTTP 403")

    def test_unknown_fault_rejected(self) -> None:
        with self.assertRaises(ValueError):
            fake_h3_stack.FakeH3Stack(faults={"no-existe"})


if __name__ == "__main__":
    unittest.main()
