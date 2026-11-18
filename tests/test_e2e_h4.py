"""Prueba de punta a punta de H4 (`dev/e2e_h4.py`).

La pila real de H4 se integra al final; aquí se prueban las utilidades puras, el cliente WebSocket, el inspector de
contenedores y el recorrido completo contra la pila simulada de `dev/fake_h4_stack.py`, también con cada fallo que la
prueba tiene que detectar. Con `unittest` (se ejecutan con `pytest` en la CI y con `python -m unittest discover tests`).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))

import e2e_h4
import fake_h4_stack

DNI, EMAIL, IBAN = e2e_h4.DNI, e2e_h4.EMAIL, e2e_h4.IBAN
WARNING = {
    "status": "WARNING",
    "request_replacements_by_type": {},
    "response_replacements_by_type": {},
    "unresolved_response_tokens": 0,
    "orphan_request_tokens": 0,
    "residual_matches": [
        {"rule_id": "a", "token_type": "DNI", "count": 1},
        {"rule_id": "b", "token_type": "EMAIL", "count": 1},
        {"rule_id": "c", "token_type": "IBAN", "count": 1},
    ],
}


class PureUtilitiesTest(unittest.TestCase):
    def test_anonymizer_flag_accepts_both_names(self) -> None:
        self.assertIs(e2e_h4.anonymizer_flag({"anonymizer_enabled": False}), False)
        self.assertIs(e2e_h4.anonymizer_flag({"anonymizer_enabled_at_start": True}), True)
        self.assertIs(e2e_h4.anonymizer_flag({"anonymizer_enabled_at_start": False, "anonymizer_enabled": True}), False)
        self.assertIsNone(e2e_h4.anonymizer_flag({"ai_ip_enabled": True}))
        self.assertIsNone(e2e_h4.anonymizer_flag({"anonymizer_enabled": "no"}))
        self.assertIsNone(e2e_h4.anonymizer_flag(None))

    def test_segments_text(self) -> None:
        parts = [{"text": "a "}, {"token": "[[DNI_001]]"}, {"text": " b"}]
        self.assertEqual(e2e_h4.segments_text(parts), "a [[DNI_001]] b")
        self.assertEqual(e2e_h4.segments_text("texto"), "texto")
        self.assertEqual(e2e_h4.segments_text(None), "")

    def test_content_leaks_in_raw_and_decoded_strings(self) -> None:
        needles = e2e_h4.needles_for("abc")
        self.assertEqual(e2e_h4.content_leaks(b'{"turn": {"content_available": false}}', needles), [])
        self.assertEqual(e2e_h4.content_leaks(json.dumps({"x": f"DNI {DNI}"}), needles), [f"«{DNI}»"])
        escaped = '{"m": "ana.perez\\u0040privada.test"}'  # el email escrito con un escape JSON
        self.assertEqual(e2e_h4.content_leaks(escaped, needles), [f"«{EMAIL}»"])
        self.assertEqual(e2e_h4.content_leaks("caso-abc-observacion", needles), ["«caso-abc»"])
        self.assertEqual(e2e_h4.content_leaks("Eco: hola", needles), ["«Eco:»"])
        self.assertEqual(e2e_h4.content_leaks("caso-otra", needles), [])  # otra ejecución no cuenta

    def test_admin_turn_problems(self) -> None:
        good = {"turn_index": 1, "content_available": False, "verification": WARNING, "replacements": []}
        self.assertEqual(e2e_h4.admin_turn_problems(good), [])
        self.assertIn("content_available", " ".join(e2e_h4.admin_turn_problems({**good, "content_available": True})))
        self.assertIn("content_available", " ".join(e2e_h4.admin_turn_problems({**good, "content_available": None})))
        for key in e2e_h4.FORBIDDEN_TURN_KEYS:
            self.assertIn(key, " ".join(e2e_h4.admin_turn_problems({**good, key: []})))
        values = {**good, "replacements": [{"token": "[[DNI_001]]", "original_value": DNI}]}
        self.assertIn("valores", " ".join(e2e_h4.admin_turn_problems(values)))
        self.assertIn("métricas", " ".join(e2e_h4.admin_turn_problems({**good, "verification": None})))
        self.assertEqual(e2e_h4.admin_turn_problems({**good, "verification": None}, metrics=False), [])
        self.assertTrue(e2e_h4.admin_turn_problems("no es un objeto"))

    def test_observation_problems(self) -> None:
        self.assertEqual(e2e_h4.observation_problems(WARNING), [])
        self.assertIn("CLEAN", " ".join(e2e_h4.observation_problems({**WARNING, "status": "CLEAN"})))
        replaced = {**WARNING, "request_replacements_by_type": {"DNI": 1}}
        self.assertIn("no se sustituye", " ".join(e2e_h4.observation_problems(replaced)))
        partial = {**WARNING, "residual_matches": WARNING["residual_matches"][:2]}
        self.assertIn("IBAN", " ".join(e2e_h4.observation_problems(partial)))
        self.assertIn("DNI", " ".join(e2e_h4.observation_problems({**WARNING, "residual_matches": []})))
        self.assertIn("unresolved", " ".join(e2e_h4.observation_problems({**WARNING, "unresolved_response_tokens": 1})))
        self.assertTrue(e2e_h4.observation_problems(None))

    def test_case_names_and_slug(self) -> None:
        nonce = e2e_h4.h3.new_nonce()
        self.assertEqual(e2e_h4.new_slug(nonce), f"e2e-{nonce}")
        self.assertRegex(e2e_h4.new_slug(nonce), r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
        self.assertTrue(e2e_h4.case_model(nonce, "ia", "chat", "json").startswith("e2e-h4-"))

    def test_org_data_accepts_nested_and_flat(self) -> None:
        self.assertEqual(e2e_h4.org_data({"organization": {"slug": "x"}, "propagated": True}), {"slug": "x"})
        self.assertEqual(e2e_h4.org_data({"slug": "x", "propagated": True}), {"slug": "x", "propagated": True})
        self.assertEqual(e2e_h4.org_data(None), {})


class ParseArgsTest(unittest.TestCase):
    def test_defaults_follow_dev_stack(self) -> None:
        env = {"BAAS_DEV_HTTP_PORT": "48080", "BAAS_DEV_FAKE_UPSTREAM_PORT": "48090",
               "COMPOSE_PROJECT_NAME": "h4-plugin"}  # fmt: skip
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ("BAAS_E2E_APP_URL", "BAAS_E2E_FAKE_URL", "BAAS_E2E_PROXY_URL"):
                os.environ.pop(name, None)
            ns = e2e_h4.parse_args([])
        self.assertEqual(ns.app_url, "http://app.localhost:48080")
        self.assertEqual(ns.fake_url, "http://127.0.0.1:48090")
        self.assertEqual((ns.org, ns.email), ("observacion", "observador@observacion.test"))
        self.assertEqual((ns.admin_email, ns.orgadmin_email), ("admin@baas.test", "orgadmin@demo.test"))
        self.assertEqual(ns.only, ["observacion", "interruptor", "eliminar"])
        self.assertEqual(ns.apis, ["messages", "chat", "responses"])
        self.assertEqual((ns.inspect, ns.compose_project, ns.mcp_data_dir), ("auto", "h4-plugin", "/data"))
        self.assertIsNone(ns.proxy_url)

    def test_explicit_values(self) -> None:
        argv = ["--app-url", "http://a:1/", "--fake-url", "http://f:2/", "--only", "eliminar", "--apis", "chat",
                "--inspect", "none", "--propagation-wait", "0.2", "--trace-timeout", "5"]  # fmt: skip
        ns = e2e_h4.parse_args(argv)
        self.assertEqual((ns.app_url, ns.fake_url), ("http://a:1", "http://f:2"))
        self.assertEqual((ns.only, ns.apis, ns.inspect), (["eliminar"], ["chat"], "none"))
        self.assertEqual((ns.propagation_wait, ns.trace_timeout), (0.2, 5.0))

    def test_invalid_choice_exits_two(self) -> None:
        for argv in (["--apis", "gemini"], ["--only", "nada"], ["--inspect", "lxc"]):
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as ctx:
                e2e_h4.parse_args(argv)
            self.assertEqual(ctx.exception.code, 2)


# --- WebSocket ------------------------------------------------------------------------------------


class RawWsServer:
    """Un servidor WebSocket de una sola conexión que envía las tramas dadas tras el handshake."""

    def __init__(self, frames: list[bytes], *, bad_accept: bool = False) -> None:
        self.frames, self.bad_accept = frames, bad_accept
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.url = f"ws://127.0.0.1:{self.listener.getsockname()[1]}/api/v1/ws/traces?org=x"
        self.request = b""
        self.received = b""
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        conn, _ = self.listener.accept()
        with conn:
            while b"\r\n\r\n" not in self.request:
                self.request += conn.recv(4096)
            lines = self.request.decode().split("\r\n")
            key = next(ln.split(": ", 1)[1] for ln in lines if ln.lower().startswith("sec-websocket-key"))
            accept = "mal" if self.bad_accept else e2e_h4.ws_accept(key)
            head = "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            conn.sendall(f"{head}Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
            for frame in self.frames:
                conn.sendall(frame)
            conn.settimeout(1)
            with contextlib.suppress(OSError):
                self.received = conn.recv(4096)

    def close(self) -> None:
        self.listener.close()


class WsClientTest(unittest.TestCase):
    def client(self, server: RawWsServer) -> e2e_h4.WsClient:
        return e2e_h4.WsClient(server.url, cookie="sesion", origin="http://app.test", timeout=3)

    def test_frames_fragments_ping_and_close(self) -> None:
        text = "á" * 100  # más de 125 bytes: longitud de 16 bits
        big = "x" * 70000  # longitud de 64 bits
        frames = [
            e2e_h4.ws_frame(0x1, b'{"type":"hello"}', mask=False),
            e2e_h4.ws_frame(0x1, b"uno ", mask=False, fin=False),
            e2e_h4.ws_frame(0x9, b"latido", mask=False),  # un ping en medio de un mensaje fragmentado
            e2e_h4.ws_frame(0x0, b"dos", mask=False),
            e2e_h4.ws_frame(0x1, text.encode(), mask=False),
            e2e_h4.ws_frame(0x1, big.encode(), mask=False),
            e2e_h4.ws_frame(0x2, b"binario", mask=False),
            e2e_h4.ws_frame(0x1, b"fin", mask=True),  # una trama enmascarada también se lee
            e2e_h4.ws_frame(0x8, b"\x03\xe8", mask=False),
        ]
        server = RawWsServer(frames)
        ws = self.client(server)
        try:
            self.assertEqual(ws.recv(2), '{"type":"hello"}')
            self.assertEqual(ws.recv(2), "uno dos")
            self.assertEqual(ws.recv(2), text)
            self.assertEqual(ws.recv(2), big)
            self.assertEqual(ws.recv(2), "fin")
            with self.assertRaises(e2e_h4.WsClosedError) as ctx:
                ws.recv(2)
            self.assertEqual(ctx.exception.code, 1000)
        finally:
            ws.close()
            server.close()
        server.thread.join(3)
        request = server.request.decode()
        self.assertIn("Origin: http://app.test", request)
        self.assertIn("Cookie: baas_session=sesion", request)
        self.assertIn("GET /api/v1/ws/traces?org=x HTTP/1.1", request)
        # el cliente respondió al ping con un pong enmascarado
        self.assertEqual(server.received[0], 0x8A)
        self.assertTrue(server.received[1] & 0x80)

    def test_recv_returns_none_when_nothing_arrives(self) -> None:
        server = RawWsServer([])
        ws = self.client(server)
        try:
            self.assertIsNone(ws.recv(0.2))
        finally:
            ws.close()
            server.close()

    def test_bad_accept_and_refused_upgrade(self) -> None:
        server = RawWsServer([], bad_accept=True)
        with self.assertRaises(e2e_h4.StepError):
            self.client(server)
        server.close()
        with fake_h4_stack.FakeH4Stack() as stack:
            url = e2e_h4.ws_url(stack.app_url, "/api/v1/ws/traces")
            with self.assertRaises(e2e_h4.StepError) as ctx:  # sin sesión: no hay 101
                e2e_h4.WsClient(url, cookie="no", origin=stack.app_url, timeout=3)
            self.assertIn("401", str(ctx.exception))

    def test_ws_url(self) -> None:
        self.assertEqual(e2e_h4.ws_url("http://app.localhost:8080/", "/x"), "ws://app.localhost:8080/x")
        self.assertEqual(e2e_h4.ws_url("https://a.b", "/x"), "wss://a.b/x")


# --- inspector ------------------------------------------------------------------------------------


class ContainerInspectorTest(unittest.TestCase):
    """`ContainerInspector` con un motor de contenedores falso que anota sus argumentos."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "calls.jsonl"
        engine = Path(self.tmp.name) / "engine"
        engine.write_text(
            f"""#!{sys.executable}
import json, sys
argv = sys.argv[1:]
open({str(self.log)!r}, "a").write(json.dumps(argv) + "\\n")
if argv[0] == "ps":
    print("cid-" + [a for a in argv if a.startswith("label=com.docker.compose.service=")][0].split("=")[-1])
elif argv[:2] == ["exec", "cid-postgres"]:
    print("7")
elif argv[:2] == ["exec", "cid-redis"]:
    print("baas:policy:org:acme" if "baas:policy:org:acme" in argv else "baas:member:u1:acme")
elif argv[:2] == ["exec", "cid-mcp-server"]:
    if argv[-1].endswith(".quarantine"):
        print("acme-20261117T200000Z")
    else:
        print("demo")
        print("otra")
"""
        )
        engine.chmod(engine.stat().st_mode | stat.S_IXUSR)
        self.inspector = e2e_h4.ContainerInspector(str(engine), "h4-plugin", "/data/")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_vault_rows_queries_the_vault_by_org_slug(self) -> None:
        self.assertEqual(self.inspector.vault_rows("acme"), 7)
        ps, run = self.calls()
        self.assertEqual(ps[:3], ["ps", "-aq", "--filter"])
        self.assertIn("label=com.docker.compose.project=h4-plugin", ps)
        self.assertIn("label=com.docker.compose.service=postgres", ps)
        self.assertEqual(run[:6], ["exec", "cid-postgres", "psql", "-U", "postgres", "-d"])
        self.assertEqual(run[6:9], ["baas_vault", "-Atc", run[8]])
        for table in ("vault_entries", "vault_counters", "conversation_state"):
            self.assertIn(f"FROM {table} WHERE org_slug = 'acme'", run[8])

    def test_redis_keys_use_the_container_password_not_ours(self) -> None:
        self.assertEqual(self.inspector.redis_keys("acme"), ["baas:policy:org:acme", "baas:member:u1:acme"])
        run = [c for c in self.calls() if c[:2] == ["exec", "cid-redis"]]
        self.assertEqual(len(run), 2)
        self.assertIn('REDISCLI_AUTH="$BAAS_REDIS_PASSWORD"', " ".join(run[0]))
        self.assertEqual(run[0][-3:], ["--scan", "--pattern", "baas:policy:org:acme"])
        self.assertEqual(run[1][-1], "baas:member:*:acme")

    def test_mcp_directories(self) -> None:
        self.assertEqual(self.inspector.mcp_org_dirs(), ["demo", "otra"])
        self.assertEqual(self.inspector.mcp_quarantine(), ["acme-20261117T200000Z"])
        runs = [c for c in self.calls() if c[:2] == ["exec", "cid-mcp-server"]]
        self.assertEqual(runs[0][2:], ["ls", "-1", "/data/orgs"])
        self.assertEqual(runs[1][2:], ["ls", "-1", "/data/.quarantine"])

    def test_unsafe_slug_is_refused_before_any_command(self) -> None:
        for slug in ("x'; DROP TABLE vault_entries; --", "A", "", "a b", "a" * 41, "-a"):
            with self.assertRaises(e2e_h4.StepError, msg=slug):
                self.inspector.vault_rows(slug)
            with self.assertRaises(e2e_h4.StepError, msg=slug):
                self.inspector.redis_keys(slug)
        self.assertFalse(self.log.exists())

    def test_missing_container_is_a_step_error(self) -> None:
        engine = Path(self.tmp.name) / "vacio"
        engine.write_text("#!/bin/sh\nexit 0\n")
        engine.chmod(engine.stat().st_mode | stat.S_IXUSR)
        with self.assertRaises(e2e_h4.StepError) as ctx:
            e2e_h4.ContainerInspector(str(engine), "h4-plugin").vault_rows("acme")
        self.assertIn("no hay contenedor «postgres»", str(ctx.exception))

    def test_build_inspector(self) -> None:
        ns = e2e_h4.parse_args(["--inspect", "none"])
        self.assertIsNone(e2e_h4.build_inspector(ns))
        ns = e2e_h4.parse_args(["--inspect", "podman", "--compose-project", "h4-plugin"])
        with mock.patch("shutil.which", return_value=None):
            missing = e2e_h4.build_inspector(ns)
        self.assertIsInstance(missing, e2e_h4.MissingInspector)
        with self.assertRaises(e2e_h4.StepError) as ctx:
            missing.vault_rows("acme")  # type: ignore[union-attr]
        self.assertIn("--inspect none", str(ctx.exception))

        class Stub:
            def __init__(self, engine: str, project: str, data: str) -> None:
                self.args = (engine, project, data)

            def container(self, service: str) -> str | None:
                return "cid" if project_exists else None

        project_exists = True
        with (
            mock.patch.object(e2e_h4, "ContainerInspector", Stub),
            mock.patch("shutil.which", return_value="/bin/podman"),
        ):
            found = e2e_h4.build_inspector(ns)
            self.assertIsInstance(found, Stub)
            self.assertEqual(found.args, ("podman", "h4-plugin", "/data"))  # type: ignore[union-attr]
            project_exists = False
            lost = e2e_h4.build_inspector(ns)
        self.assertIsInstance(lost, e2e_h4.MissingInspector)
        self.assertIn("h4-plugin", lost.why)  # type: ignore[union-attr]
        ns = e2e_h4.parse_args(["--inspect", "auto"])
        with (
            mock.patch.object(e2e_h4, "ContainerInspector", Stub),
            mock.patch("shutil.which", side_effect=lambda e: None if e == "podman" else "/bin/docker"),
        ):
            project_exists = True
            self.assertEqual(e2e_h4.build_inspector(ns).args[0], "docker")  # type: ignore[union-attr]


# --- recorrido completo contra la pila simulada ------------------------------------------------------


def run_e2e(
    stack: fake_h4_stack.FakeH4Stack, *extra: str, inspector: object = "stack"
) -> tuple[int, dict[str, dict], str]:
    """Ejecuta `e2e_h4.main` contra la pila simulada; devuelve el código, los pasos por nombre y la salida."""
    if inspector is None:
        extra = (*extra, "--inspect", "none")  # nunca el motor real del equipo
    with tempfile.TemporaryDirectory() as tmp:
        report_path = Path(tmp) / "informe.json"
        argv = ["--app-url", stack.app_url, "--fake-url", stack.fake_url, "--trace-timeout", "3",
                "--propagation-wait", "0", "--timeout", "10", "--json", str(report_path), *extra]  # fmt: skip
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = e2e_h4.main(argv, inspector=stack.inspector if inspector == "stack" else inspector)  # type: ignore[arg-type]
        report = json.loads(report_path.read_text())
    return code, {s["name"]: s for s in report["steps"]}, out.getvalue()


def statuses(steps: dict[str, dict], prefix: str) -> set[str]:
    return {s["status"] for name, s in steps.items() if name.startswith(prefix)}


class FullRunTest(unittest.TestCase):
    def assert_all_ok(self, code: int, steps: dict[str, dict], out: str) -> None:
        self.assertEqual(code, 0, out)
        self.assertEqual({s["status"] for s in steps.values()}, {"OK"}, out)

    def test_everything_passes(self) -> None:
        with fake_h4_stack.FakeH4Stack() as stack:
            code, steps, out = run_e2e(stack)
            initial = stack.state.orgs["observacion"].anonymizer
            leftovers = sorted(stack.state.orgs)
            raised = {
                c.api: (c.protected_from_turn, [(t["ai_ip"], t["anonymizer"]) for t in c.turns])
                for c in stack.state.convs.values()
                if c.model.startswith("e2e-h4-") and "-ia-" in c.model
            }
        self.assert_all_ok(code, steps, out)
        self.assertEqual(len([n for n in steps if n.startswith("5.")]), 6)
        once = ["0.", "1.", "2.", "2b.", "3.", "4.", "6.", "7.1", "7.2", "7.3", "8.1 ", "8.2", "8.3", "8.4", "8.5"]
        once += ["8.6", "8.7", "8.8", "8.9", "8.10", "8.11", "8.12", "9.1 ", "9.1b", "9.2", "9.3", "9.4", "9.5"]
        once += ["9.6", "9.7", "9.8"]
        for prefix in once:
            self.assertEqual(len([n for n in steps if n.startswith(prefix)]), 1, prefix)
        self.assertEqual(
            raised,
            {api: (2, [(True, False), (True, True), (True, True)]) for api in ("messages", "chat", "responses")},
            "A empieza en observación, sube en el turno 2 y no baja al apagar el anonimizador",
        )
        self.assertFalse(initial, "el interruptor debe quedar apagado, como al empezar")
        self.assertEqual(leftovers, ["baas-ai-ip", "demo", "observacion"], "la organización de la prueba se eliminó")
        self.assertIn("RESULTADO: todo OK", out)

    def test_everything_passes_with_body_as_text(self) -> None:
        with fake_h4_stack.FakeH4Stack(debug_body="text") as stack:
            code, steps, out = run_e2e(stack)
        self.assert_all_ok(code, steps, out)

    def test_two_runs_in_a_row(self) -> None:
        with fake_h4_stack.FakeH4Stack() as stack:
            self.assert_all_ok(*run_e2e(stack))
            self.assert_all_ok(*run_e2e(stack))

    def test_leaves_the_switch_off_even_when_it_started_on(self) -> None:
        with fake_h4_stack.FakeH4Stack() as stack:
            stack.state.orgs["observacion"].anonymizer = True
            stack.state.proxy_anonymizer["observacion"] = True
            code, steps, out = run_e2e(stack)
            final = stack.state.orgs["observacion"].anonymizer
        self.assert_all_ok(code, steps, out)
        self.assertIn("estaba encendido", steps["4. «observacion» con el anonimizador apagado"]["detail"])
        self.assertFalse(final)

    def test_only_one_group_and_one_api(self) -> None:
        with fake_h4_stack.FakeH4Stack() as stack:
            code, steps, out = run_e2e(stack, "--only", "observacion", "--apis", "chat")
        self.assert_all_ok(code, steps, out)
        self.assertEqual(len([n for n in steps if n.startswith("5.")]), 2)
        self.assertFalse([n for n in steps if n.startswith(("8.", "9."))])
        with fake_h4_stack.FakeH4Stack() as stack:
            code, steps, out = run_e2e(stack, "--only", "interruptor", "--apis", "messages")
        self.assert_all_ok(code, steps, out)
        self.assertFalse([n for n in steps if n.startswith(("5.", "9."))])
        with fake_h4_stack.FakeH4Stack() as stack:
            code, steps, out = run_e2e(stack, "--only", "eliminar", "--inspect", "none", inspector=None)
        self.assert_all_ok(code, steps, out)
        self.assertEqual(len([n for n in steps if n.startswith("9.")]), 7, "sin inspector no hay pasos 9.1b ni 9.8")

    def test_inspect_none_skips_container_steps(self) -> None:
        with fake_h4_stack.FakeH4Stack() as stack:
            code, steps, out = run_e2e(stack, "--only", "eliminar", inspector=None)
        self.assert_all_ok(code, steps, out)
        self.assertFalse([n for n in steps if n.startswith("9.8")])

    def test_missing_engine_fails_only_the_inside_steps(self) -> None:
        missing = e2e_h4.MissingInspector("no hay motor")
        with fake_h4_stack.FakeH4Stack() as stack:
            code, steps, out = run_e2e(stack, "--only", "eliminar", inspector=missing)
        self.assertEqual(code, 1, out)
        failed = sorted(n.split(" ")[0] for n, s in steps.items() if s["status"] == "FALLO")
        self.assertEqual(failed, ["9.1b", "9.8"], out)  # el resto de la eliminación se comprueba igual
        for name, step in steps.items():
            if name.startswith(("9.1b", "9.8")):
                self.assertIn("--inspect none", step["detail"])

    def test_stack_unreachable_fails(self) -> None:
        with fake_h4_stack.FakeH4Stack() as stack:
            app, fake = stack.app_url, stack.fake_url
        argv = ["--app-url", app, "--fake-url", fake, "--timeout", "2", "--inspect", "none"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(e2e_h4.main(argv), 1)

    def test_unknown_fault_rejected(self) -> None:
        with self.assertRaises(ValueError):
            fake_h4_stack.FakeH4Stack(faults={"no-existe"})

    def test_every_fault_has_a_test(self) -> None:
        tested = set(FaultsTest.EXPECTED)
        self.assertEqual(tested, set(fake_h4_stack.FAULTS), "cada fallo inyectable debe tener su test")


class FaultsTest(unittest.TestCase):
    """Cada fallo inyectado en la pila simulada hace fallar el paso que lo comprueba (código 1)."""

    # fallo → {prefijo del paso: fragmentos que deben aparecer en su detalle}
    EXPECTED: ClassVar[dict[str, dict[str, tuple[str, ...]]]] = {
        "observe-rewrites": {"5.": ("no es idéntico", "baas_e2e")},
        "observe-anonymizes": {"5.": ("no recibió en claro", "recibió tokens")},
        "observe-clean": {"6.": ("CLEAN",)},
        "state-follows-switch": {"8.7": ("recibió en claro",), "8.9": ("recibió en claro",)},
        "no-ratchet": {"8.5": ("recibió en claro",)},
        "ratchet-lowers": {"8.9": ("recibió en claro",)},
        "no-protected-from": {"8.10": ("protected_from_turn",)},
        "admin-sees-pre-raise": {"8.11": ("turno 1", "content_available")},
        "admin-hides-raised": {"8.11": ("turno 2", "content_available")},
        "toggle-no-propagation": {"8.4": ("no recibió [[DNI_001]]",)},
        "no-cause": {"6.": ("la causa no consta",), "8.10": ("anonymizer_enabled = None",)},
        "admin-sees-content-api": {
            "7.1": ("content_available",),
            "7.2": ("content_available",),
            "7.3": ("content_available",),
        },
        "admin-detail-leak": {"7.1": ("el detalle de la conversación contiene",)},
        "admin-reveal": {"7.1": ("puede revelar",)},
        "ws-leak": {"7.3": ("trama del PLATFORM_ADMIN",)},
        "ws-silent": {"7.3": ("no recibió ninguna trama",)},
        "admin-sees-bypass": {"7.2": ("content_available",)},
        "toggle-no-reason": {"8.1 ": ("sin motivo", "422")},
        "toggle-user-allowed": {"8.1 ": ("USER", "403")},
        "toggle-foreign-403": {"8.1 ": ("ORG_ADMIN de otra organización", "404")},
        "toggle-ai-ip-off-allowed": {"8.1 ": ("AI-IP apagado", "AI_IP_DISABLED")},
        "toggle-not-applied": {"8.3": ("anonymizer_enabled = False",)},
        "delete-no-block-check": {"9.2": ("sin bloquear", "ORG_NOT_BLOCKED")},
        "delete-no-slug-check": {"9.3": ("SLUG_MISMATCH",)},
        "delete-meta-allowed": {"9.2": ("la organización meta", "ORG_PROTECTED")},
        "delete-orgadmin-allowed": {"9.2": ("ORG_ADMIN de la organización", "403")},
        "delete-twice-ok": {"9.5": ("la segunda eliminación",)},
        "slug-reusable": {"9.6": ("409",)},
        "proxy-still-serves": {"9.5": ("baas_access_denied",)},
        "mcp-still-serves": {"9.5": ("el MCP sigue sirviendo",)},
        "block-not-applied": {"9.3": ("baas_access_blocked",)},
        "vault-kept": {"9.8": ("el vault conserva",)},
        "redis-kept": {"9.8": ("Redis conserva claves",)},
        "no-quarantine": {"9.8": (".quarantine", "no hay ninguna entrada")},
        "audit-missing": {"9.7": ("org.deleted",)},
        "delete-removes-users": {"9.5": ("handshake",)},
        "delete-no-reauth": {"9.2": ("sin contraseña", "contraseña incorrecta")},
    }

    def run_fault(self, fault: str) -> dict[str, dict]:
        with fake_h4_stack.FakeH4Stack(faults={fault}) as stack:
            code, steps, out = run_e2e(stack, "--trace-timeout", "0.5")
        self.assertEqual(code, 1, f"{fault}: {out}")
        return steps

    def test_every_fault_is_detected(self) -> None:
        for fault, expected in self.EXPECTED.items():
            with self.subTest(fault=fault):
                steps = self.run_fault(fault)
                for prefix, fragments in expected.items():
                    chosen = {n: s for n, s in steps.items() if n.startswith(prefix)}
                    self.assertTrue(chosen, f"{fault}: no hay pasos {prefix}")
                    for name, step in chosen.items():
                        self.assertEqual(step["status"], "FALLO", f"{fault}: {name}: {step['detail']}")
                        for fragment in fragments:
                            self.assertIn(fragment, step["detail"], f"{fault}: {name}")

    def test_faults_do_not_break_unrelated_steps(self) -> None:
        # ws-silent solo afecta al WebSocket; observe-clean a 6 y 8.10; audit-missing solo a 9.7
        cases = (("ws-silent", {"7.3"}), ("observe-clean", {"6.", "8.10"}), ("audit-missing", {"9.7"}))
        for fault, failing in cases:
            steps = self.run_fault(fault)
            failed = {n for n, s in steps.items() if s["status"] == "FALLO"}
            self.assertTrue(all(any(n.startswith(p) for p in failing) for n in failed), f"{fault}: {failed}")


if __name__ == "__main__":
    unittest.main()
