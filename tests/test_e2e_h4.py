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
import re
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


class OrphanUtilitiesTest(unittest.TestCase):
    """Las utilidades puras del grupo «huérfanos» (11) y el cliente de inicio de sesión."""

    ADMIN = "admin@baas.test"
    A = e2e_h4.Person("a", "huerf-abc-a@huerf.test", "Huerfano A abc", user_id="u-a")
    B = e2e_h4.Person("b", "huerf-abc-b@huerf.test", "Huerfano B abc", user_id="u-b")

    @staticmethod
    def row(**changes: object) -> dict[str, object]:
        row: dict[str, object] = {
            "action": "user.disabled", "target_type": "user", "target_id": "u-a", "reason": e2e_h4.REASON,
            "org_slug": None, "actor": {"id": "x", "email": "admin@baas.test", "name": "Administrador"},
            "target_label": "huerf-abc-a@huerf.test",  # la webapp lo resuelve al leer: no cuenta
            "metadata": {"cause": "org.deleted", "org_slug": "o1", "previous_status": "ACTIVE", "keys_revoked": 1},
        }  # fmt: skip
        return {**row, **changes}

    def audit_problems(self, row: object, **changes: object) -> list[str]:
        kwargs: dict[str, object] = {
            "org_slug": "o1", "previous_status": "ACTIVE", "actor_email": self.ADMIN, "reason": e2e_h4.REASON,
            "forbidden": [self.A.email, self.A.name], "min_keys": 1,
        }  # fmt: skip
        return e2e_h4.disabled_audit_problems(row, **{**kwargs, **changes})  # type: ignore[arg-type]

    def test_new_names_are_unique_and_valid(self) -> None:
        e2e = e2e_h4.E2EH4(e2e_h4.parse_args(["--inspect", "none"]), nonce="abcdefghij")
        self.assertEqual(sorted(e2e.orph_slugs), ["o1", "o2", "o3", "o4"])
        for slug in e2e.orph_slugs.values():
            self.assertRegex(slug, r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
            self.assertTrue(slug.startswith("huerf-abcdefghij-"))
        emails = [p.email for p in e2e.orph.values()]
        self.assertEqual(len(set(emails)), 7)
        self.assertTrue(all(re.fullmatch(r"huerf-abcdefghij-[a-z]@huerf\.test", e) for e in emails))
        names = [p.name for p in e2e.orph.values()]
        self.assertEqual(len(set(names)), 7)
        self.assertFalse([n for n in names if n.lower() in " ".join(emails)], "el nombre no es parte de ningún email")
        other = e2e_h4.E2EH4(e2e_h4.parse_args(["--inspect", "none"]), nonce="zzzzzzzzzz")
        self.assertFalse(set(emails) & {p.email for p in other.orph.values()})

    def test_activation_token_and_counts(self) -> None:
        self.assertEqual(e2e_h4.activation_token("http://app.localhost:8080/activate/AbC-_9/"), "AbC-_9")
        self.assertEqual(e2e_h4.activation_token(None), "")
        self.assertEqual(e2e_h4.activation_token(""), "")
        self.assertTrue(e2e_h4.is_count(0) and e2e_h4.is_count(3))
        self.assertFalse(e2e_h4.is_count(True) or e2e_h4.is_count(None) or e2e_h4.is_count("1") or e2e_h4.is_count(1.0))

    def test_api_error_never_prints_the_body(self) -> None:
        res = e2e_h4.Http(409, [], b'{"error": "USER_DISABLED", "activation_url": "http://x/activate/secreto"}')
        self.assertEqual(e2e_h4.api_error(res), "HTTP 409 USER_DISABLED")
        self.assertEqual(e2e_h4.api_error(e2e_h4.Http(502, [], b"<html>")), "HTTP 502")
        self.assertEqual(e2e_h4.api_error(e2e_h4.Http(200, [], b"[1]")), "HTTP 200")

    def test_audit_rows_filter_by_action_and_target(self) -> None:
        body = {"items": [self.row(), self.row(target_id="u-b"), self.row(action="member.invited"), "no", None]}
        self.assertEqual(len(e2e_h4.audit_rows(body, "user.disabled", "u-a")), 1)
        self.assertEqual(len(e2e_h4.audit_rows(body, "user.disabled", "u-b")), 1)
        self.assertEqual(e2e_h4.audit_rows(body, "user.reactivated", "u-a"), [])
        self.assertEqual(e2e_h4.audit_rows(None, "user.disabled", "u-a"), [])
        self.assertEqual(e2e_h4.audit_rows({"items": None}, "user.disabled", "u-a"), [])

    def preview(self, *orphans: dict[str, object], members: int = 3) -> dict[str, object]:
        return {"members": members, "conversations": 0, "orphans": list(orphans)}

    def entry(self, person: e2e_h4.Person, status: str = "ACTIVE", keys: int = 1) -> dict[str, object]:
        return {"id": person.user_id, "email": person.email, "name": person.name, "status": status,
                "personal_keys": keys}  # fmt: skip

    def test_preview_problems(self) -> None:
        want = [(self.A, "ACTIVE", 1)]
        self.assertEqual(e2e_h4.preview_problems(self.preview(self.entry(self.A)), want, members=3), [])
        # lista a quien tiene otra organización (o al PLATFORM_ADMIN)
        extra = self.preview(self.entry(self.A), self.entry(self.B))
        self.assertIn(
            "lista a quien no se quedaría sin organización", " ".join(e2e_h4.preview_problems(extra, want, members=3))
        )
        self.assertIn("no lista a", " ".join(e2e_h4.preview_problems(self.preview(), want, members=3)))
        # campos
        bad = self.preview({**self.entry(self.A), "personal_keys": 0, "status": "INVITED", "id": "otro"})
        text = " ".join(e2e_h4.preview_problems(bad, want, members=3))
        for field in ("personal_keys", "status", "id"):
            self.assertIn(f"a: {field} =", text)
        self.assertIn(
            "members = 2",
            " ".join(e2e_h4.preview_problems(self.preview(self.entry(self.A), members=2), want, members=3)),
        )
        self.assertTrue(
            e2e_h4.preview_problems({**self.preview(self.entry(self.A)), "conversations": None}, want, members=3)
        )
        self.assertTrue(e2e_h4.preview_problems({**self.preview(self.entry(self.A)), "members": True}, want, members=1))
        self.assertTrue(e2e_h4.preview_problems({"members": 3, "conversations": 0, "orphans": None}, want, members=3))
        self.assertTrue(e2e_h4.preview_problems("no es un objeto", want, members=3))
        dup = self.preview(self.entry(self.A), self.entry(self.A))
        self.assertIn("repetidas", " ".join(e2e_h4.preview_problems(dup, want, members=3)))

    def test_disabled_audit_problems(self) -> None:
        self.assertEqual(self.audit_problems(self.row()), [])
        meta = {"cause": "org.deleted", "org_slug": "o1", "previous_status": "ACTIVE", "keys_revoked": 1}
        cases = {
            "target_type": self.row(target_type="organization"),
            "reason": self.row(reason="otro"),
            "org_slug = 'o1'": self.row(org_slug="o1"),
            "actor": self.row(actor={"email": "otro@baas.test"}),
            "metadata.cause": self.row(metadata={**meta, "cause": "member.removed"}),
            "metadata.org_slug": self.row(metadata={**meta, "org_slug": "o9"}),
            "metadata.previous_status": self.row(metadata={**meta, "previous_status": "INVITED"}),
            "metadata.keys_revoked": self.row(metadata={**meta, "keys_revoked": 0}),
        }
        for fragment, row in cases.items():
            self.assertIn(fragment, " ".join(self.audit_problems(row)), fragment)
        self.assertIn("keys_revoked", " ".join(self.audit_problems(self.row(metadata={**meta, "keys_revoked": True}))))
        self.assertEqual(self.audit_problems(self.row(metadata={**meta, "keys_revoked": 0}), min_keys=0), [])
        self.assertTrue(self.audit_problems("no es un objeto"))
        self.assertTrue(self.audit_problems(self.row(metadata=None)))

    def test_disabled_audit_rejects_email_or_name_anywhere_but_target_label(self) -> None:
        meta = {"cause": "org.deleted", "org_slug": "o1", "previous_status": "ACTIVE", "keys_revoked": 1}
        for where, row in (
            ("metadata", self.row(metadata={**meta, "email": self.A.email})),
            ("name", self.row(metadata={**meta, "note": self.A.name.upper()})),  # sin distinguir mayúsculas
            ("reason", self.row(reason=f"{e2e_h4.REASON} {self.A.email}")),
            ("key", self.row(**{self.A.email: 1})),
            ("actor", self.row(actor={"email": self.ADMIN, "name": self.A.name})),
        ):
            problems = self.audit_problems(row, reason=row["reason"])  # type: ignore[arg-type]
            self.assertTrue(any("no debe llevar el email ni el nombre" in p for p in problems), where)
        # target_label (derivado al leer) no cuenta
        self.assertEqual(self.audit_problems(self.row(target_label=self.A.name)), [])

    def test_reactivated_audit_problems(self) -> None:
        row = {"actor": {"email": self.ADMIN}, "org_slug": "o2", "metadata": {"org_slug": "o2", "new_status": "ACTIVE"}}
        kwargs = {"org_slug": "o2", "actor_email": self.ADMIN, "new_status": "ACTIVE"}
        self.assertEqual(e2e_h4.reactivated_audit_problems(row, **kwargs), [])
        invited = {**row, "metadata": {"org_slug": "o2", "new_status": "INVITED"}}
        self.assertEqual(e2e_h4.reactivated_audit_problems(invited, **{**kwargs, "new_status": "INVITED"}), [])
        self.assertIn("new_status", " ".join(e2e_h4.reactivated_audit_problems(invited, **kwargs)))
        wrong = {"actor": {"email": "x@baas.test"}, "org_slug": "o9", "metadata": {"org_slug": "o9", "new_status": "X"}}
        text = " ".join(e2e_h4.reactivated_audit_problems(wrong, **kwargs))
        for fragment in ("actor", "org_slug = 'o9'", "metadata.org_slug", "metadata.new_status"):
            self.assertIn(fragment, text)
        # sin organización (org_id nulo): su ORG_ADMIN no la vería
        no_org = {**row, "org_slug": None}
        self.assertIn("org_slug = None", " ".join(e2e_h4.reactivated_audit_problems(no_org, **kwargs)))
        # el contrato ya no tiene previous_status
        old = {**row, "metadata": {"org_slug": "o2", "new_status": "ACTIVE", "previous_status": "DISABLED"}}
        self.assertIn("previous_status", " ".join(e2e_h4.reactivated_audit_problems(old, **kwargs)))
        self.assertTrue(e2e_h4.reactivated_audit_problems(None, **kwargs))

    def test_login_waits_when_rate_limited(self) -> None:
        e2e = e2e_h4.E2EH4(e2e_h4.parse_args(["--inspect", "none"]), nonce="abcdefghij")
        cookie = [("Set-Cookie", "baas_session=abc; Path=/")]
        answers = [e2e_h4.Http(429, [], b'{"error": "RATE_LIMITED", "retry_after_seconds": 7}'),
                   e2e_h4.Http(200, cookie, b'{"next": "DONE"}')]  # fmt: skip
        with (
            mock.patch.object(e2e_h4.h3, "http", side_effect=answers) as http,
            mock.patch("time.sleep") as sleep,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(e2e.new_session("a@huerf.test"), "abc")
        sleep.assert_called_once_with(8)
        self.assertEqual(http.call_count, 2)
        self.assertIn("RATE_LIMITED", out.getvalue())
        # tres 429 seguidos: se devuelve el último, sin esperar para siempre
        with (
            mock.patch.object(e2e_h4.h3, "http", return_value=e2e_h4.Http(429, [], b"{}")),
            mock.patch("time.sleep") as sleep,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(e2e.raw_login("a@huerf.test").status, 429)
        self.assertEqual(sleep.call_count, 2)

    def test_login_without_dev_skip_totp_says_so(self) -> None:
        e2e = e2e_h4.E2EH4(e2e_h4.parse_args(["--inspect", "none"]), nonce="abcdefghij")
        with (
            mock.patch.object(e2e_h4.h3, "http", return_value=e2e_h4.Http(200, [], b'{"next": "TOTP"}')),
            self.assertRaises(e2e_h4.StepError) as ctx,
        ):
            e2e.new_session("a@huerf.test")
        self.assertIn("BAAS_DEV_SKIP_TOTP=1", str(ctx.exception))


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
        self.assertEqual(ns.only, ["observacion", "interruptor", "eliminar", "huerfanos"])
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


# --- inspector de Redis: las dos instancias y el disco (H4-02) ------------------------------------------


UUID_A = "0b1d5f64-7f0e-4c43-9a43-5a3a0f7c1a11"
UUID_B = "9c2e7b30-1d44-4f6e-8b2a-6d1f3c9e0a22"
RDB_BASE = b"REDIS0011\xfa\x09redis-ver\x057.4.0\xfe\x00\xfb\x01\x00\x00\x05hello\x00" + b"\xff" * 8


class FakeEngine:
    """Un `podman`/`docker` falso (ver `tests/fake_engine.py`) con su estado y su registro de llamadas."""

    def __init__(self, root: Path, **state: object) -> None:
        self.root = root
        self.state_path = root / "engine-state.json"
        self.data = {name: root / f"data-{name}" for name in ("redis", "redis-ephemeral")}
        for path in self.data.values():
            path.mkdir()
        base = {
            "containers": ["postgres", "redis", "redis-ephemeral", "mcp-server"],
            "data_dirs": {name: str(path) for name, path in self.data.items()},
            "config": {
                "redis": {"appendonly": "yes", "save": "", "dir": "/data"},
                "redis-ephemeral": {"appendonly": "no", "save": "", "dir": "/data"},
            },
            "keys": {},
        }
        self.state_path.write_text(json.dumps({**base, **state}))
        self.path = root / "engine"
        helper = Path(__file__).resolve().parent / "fake_engine.py"
        self.path.write_text(f'#!/bin/sh\nFAKE_ENGINE_STATE={self.state_path} exec {sys.executable} {helper} "$@"\n')
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR)

    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    def calls(self, container: str | None = None) -> list[list[str]]:
        calls = self.state().get("calls", [])
        return [c for c in calls if container is None or c[:2] == ["exec", f"cid-{container}"]]

    def inspector(self, **options: float) -> e2e_h4.ContainerInspector:
        inspector = e2e_h4.ContainerInspector(str(self.path), "h4-plugin")
        for name, value in options.items():
            setattr(inspector, name, value)
        return inspector


class NeedlePatternTest(unittest.TestCase):
    def test_matches_the_plain_text_and_every_mix_of_json_escapes(self) -> None:
        pattern = e2e_h4.needle_pattern(EMAIL)
        for written in (
            EMAIL,
            "ana.perez\\u0040privada.test",
            "ana\\u002eperez@privada.test",
            "ana.perez\\u0040privada\\u002Etest",
        ):
            self.assertRegex(f"xx{written}yy", pattern)
        self.assertNotRegex("ana.perezXprivada.test", pattern, "el punto no es un comodín")
        self.assertNotRegex("ana.perez@privada.tes", pattern)
        escapes = (
            ("señor", "se\\u00f1or"), ("x/y", "x\\/y"), ("<b>", "\\u003cb\\u003e"), ("😀", "\\ud83d\\ude00"),
            ('dice "hola"', 'dice \\"hola\\"'), ("C:\\tmp", "C:\\\\tmp"), ("a\tb", "a\\tb"),
        )  # fmt: skip
        for needle, written in escapes:
            self.assertRegex(written, e2e_h4.needle_pattern(needle), needle)
            self.assertRegex(needle, e2e_h4.needle_pattern(needle), needle)

    def test_special_characters_are_literal(self) -> None:
        for needle in ("a.b", "a*b", "[a-z]+", "(x|y)", "$^", "a{2}", "\\"):
            pattern = e2e_h4.needle_pattern(needle)
            self.assertRegex(f"-{needle}-", pattern)
        self.assertNotRegex("aab", e2e_h4.needle_pattern("a.b"))
        self.assertNotRegex("aab", e2e_h4.needle_pattern("a*b"))
        self.assertNotRegex("a", e2e_h4.needle_pattern("[a-z]+"))


class RedisInspectorTest(unittest.TestCase):
    """`ContainerInspector` frente a las dos instancias de Redis (la persistente y la efímera, H4-02)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # --- qué instancias hay ---
    def test_redis_services_lists_the_instances_that_exist(self) -> None:
        engine = FakeEngine(self.root)
        self.assertEqual(engine.inspector().redis_services(), ["redis", "redis-ephemeral"])

    def test_redis_services_of_an_old_stack_has_only_the_persistent_one(self) -> None:
        engine = FakeEngine(self.root, containers=["postgres", "redis"])
        self.assertEqual(engine.inspector().redis_services(), ["redis"])

    def test_redis_services_without_any_redis_is_a_step_error(self) -> None:
        engine = FakeEngine(self.root, containers=["postgres"])
        with self.assertRaises(e2e_h4.StepError) as ctx:
            engine.inspector().redis_services()
        self.assertIn("no hay contenedor «redis»", str(ctx.exception))

    # --- claves de una organización ---
    def test_redis_keys_look_in_both_instances_and_say_which(self) -> None:
        engine = FakeEngine(self.root, keys={
            "redis": ["baas:policy:org:acme", f"baas:member:{UUID_A}:acme", "baas:policy:org:otra"],
            "redis-ephemeral": [f"baas:conv:{UUID_A}:vault", f"baas:conv:{UUID_B}:tombstone", "baas:conv:otra:vault"],
        })  # fmt: skip
        keys = engine.inspector().redis_keys("acme", [UUID_A, UUID_B])
        self.assertEqual(keys, [
            "[redis] baas:policy:org:acme",
            f"[redis] baas:member:{UUID_A}:acme",
            f"[redis-ephemeral] baas:conv:{UUID_A}:vault",
            f"[redis-ephemeral] baas:conv:{UUID_B}:tombstone",
        ])  # fmt: skip

    def test_redis_keys_authenticate_with_the_password_of_each_container(self) -> None:
        engine = FakeEngine(self.root)
        engine.inspector().redis_keys("acme", [UUID_A])
        for service in ("redis", "redis-ephemeral"):
            runs = engine.calls(service)
            self.assertTrue(runs, service)
            for run in runs:
                self.assertEqual(run[2:4], ["sh", "-c"], run)
                self.assertIn('REDISCLI_AUTH="$BAAS_REDIS_PASSWORD"', run[4])
        persistent = [c[-1] for c in engine.calls("redis")]
        self.assertEqual(persistent, ["baas:policy:org:acme", "baas:member:*:acme"])
        ephemeral = [c[-1] for c in engine.calls("redis-ephemeral")]
        self.assertEqual(
            ephemeral[-1], f"baas:conv:{UUID_A}:*", "las claves de conversación solo se buscan en la efímera"
        )

    def test_redis_keys_of_an_old_stack_only_look_in_the_persistent_one(self) -> None:
        engine = FakeEngine(self.root, containers=["postgres", "redis"], keys={"redis": ["baas:policy:org:acme"]})
        self.assertEqual(engine.inspector().redis_keys("acme", [UUID_A]), ["[redis] baas:policy:org:acme"])
        self.assertFalse(engine.calls("redis-ephemeral"))

    def test_unsafe_arguments_are_refused_before_any_command(self) -> None:
        engine = FakeEngine(self.root)
        inspector = engine.inspector()
        with self.assertRaises(e2e_h4.StepError):
            inspector.redis_keys("a b")
        for bad in ("*", f"{UUID_A}:*", "no-es-un-uuid", UUID_A.upper(), ""):
            with self.assertRaises(e2e_h4.StepError, msg=bad):
                inspector.redis_keys("acme", [bad])
        for slug in ("x'; DROP TABLE vault_entries; --", "A", "", "-a"):
            with self.assertRaises(e2e_h4.StepError, msg=slug):
                inspector.conversation_ids(slug)
        for name in ("*", "Append", "a b", "", "x;y"):
            with self.assertRaises(e2e_h4.StepError, msg=name):
                inspector.redis_setting("redis", name)
        for service in ("postgres", "redis;ls", "", "mcp-server"):
            with self.assertRaises(e2e_h4.StepError, msg=service):
                inspector.redis_setting(service, "appendonly")
            with self.assertRaises(e2e_h4.StepError, msg=service):
                inspector.disk_files(service)
            with self.assertRaises(e2e_h4.StepError, msg=service):
                inspector.disk_contains(service, ["x"])
            with self.assertRaises(e2e_h4.StepError, msg=service):
                inspector.force_aof_rewrite(service)
        self.assertEqual(engine.calls(), [], "no se ejecutó ningún comando")

    def test_conversation_ids_of_an_organization(self) -> None:
        engine = FakeEngine(self.root, psql=f"{UUID_B}\n{UUID_A}\n")
        self.assertEqual(engine.inspector().conversation_ids("acme"), [UUID_A, UUID_B])
        run = engine.calls("postgres")[0]
        self.assertEqual(run[2:8], ["psql", "-U", "postgres", "-d", "baas_vault", "-Atc"])
        for table in ("conversation_state", "vault_entries"):
            self.assertIn(f"FROM {table} WHERE org_slug = 'acme'", run[8])

    def test_conversation_ids_refuses_what_is_not_a_uuid(self) -> None:
        engine = FakeEngine(self.root, psql="ERROR: nope\n")
        with self.assertRaises(e2e_h4.StepError) as ctx:
            engine.inspector().conversation_ids("acme")
        self.assertIn("identificador", str(ctx.exception))

    # --- parámetros de cada instancia ---
    def test_redis_setting_reads_config_get_of_each_instance(self) -> None:
        engine = FakeEngine(self.root)
        inspector = engine.inspector()
        self.assertEqual(inspector.redis_setting("redis", "appendonly"), "yes")
        self.assertEqual(inspector.redis_setting("redis-ephemeral", "appendonly"), "no")
        self.assertEqual(inspector.redis_setting("redis-ephemeral", "save"), "", 'save "" es una cadena vacía')
        run = engine.calls("redis-ephemeral")[-1]
        self.assertEqual(run[-4:], ["--raw", "CONFIG", "GET", "save"])
        self.assertIn('REDISCLI_AUTH="$BAAS_REDIS_PASSWORD"', run[4])

    def test_redis_setting_unknown_parameter_is_a_step_error(self) -> None:
        engine = FakeEngine(self.root)
        with self.assertRaises(e2e_h4.StepError) as ctx:
            engine.inspector().redis_setting("redis", "inventado")
        self.assertIn("inventado", str(ctx.exception))

    # --- ficheros de /data ---
    def test_disk_files_lists_every_file_below_data(self) -> None:
        engine = FakeEngine(self.root)
        self.assertEqual(engine.inspector().disk_files("redis-ephemeral"), [])
        aof = engine.data["redis"] / "appendonlydir"
        aof.mkdir()
        (aof / "appendonly.aof.1.base.rdb").write_bytes(RDB_BASE)
        (aof / "appendonly.aof.manifest").write_text("file appendonly.aof.1.base.rdb seq 1 type b\n")
        (engine.data["redis"] / "dump.rdb").write_bytes(RDB_BASE)
        self.assertEqual(
            engine.inspector().disk_files("redis"),
            ["/data/appendonlydir/appendonly.aof.1.base.rdb", "/data/appendonlydir/appendonly.aof.manifest",
             "/data/dump.rdb"],
        )  # fmt: skip
        self.assertEqual(engine.calls("redis")[-1][2:], ["find", "/data", "-mindepth", "1", "!", "-type", "d"])

    # --- buscar centinelas en el disco ---
    def write_files(self, engine: FakeEngine, service: str = "redis") -> None:
        aof = engine.data[service] / "appendonlydir"
        aof.mkdir()
        resp = f"*3\r\n$4\r\nHSET\r\n$9\r\n{DNI}\r\n$22\r\n{EMAIL}\r\n".encode()
        (aof / "appendonly.aof.1.incr.aof").write_bytes(resp)
        (aof / "appendonly.aof.1.base.rdb").write_bytes(RDB_BASE + IBAN.encode() + b"\x00\x01" + b"caso-abc")

    def test_disk_contains_finds_each_sentinel_in_any_file(self) -> None:
        engine = FakeEngine(self.root)
        self.write_files(engine)
        needles = [DNI, EMAIL, IBAN, "caso-abc", "caso-otra", "no-esta"]
        self.assertEqual(engine.inspector().disk_contains("redis", needles), [DNI, EMAIL, IBAN, "caso-abc"])
        self.assertEqual(engine.inspector().disk_contains("redis-ephemeral", needles), [])

    def test_disk_contains_finds_the_json_escaped_forms(self) -> None:
        engine = FakeEngine(self.root)
        events = engine.data["redis"] / "appendonly.aof"
        events.write_text(
            '{"a": "ana.perez\\u0040privada.test", "b": "se\\u00f1or", "c": "x\\/y", "d": "\\u003cb\\u003e",'
            ' "e": "dice \\"hola\\"", "f": "C:\\\\tmp"}'
        )
        needles = [EMAIL, "señor", "x/y", "<b>", 'dice "hola"', "C:\\tmp", "no-esta"]
        found = engine.inspector().disk_contains("redis", needles)
        self.assertEqual(found, needles[:-1])

    def test_disk_contains_treats_hostile_sentinels_as_plain_text(self) -> None:
        engine = FakeEngine(self.root)
        hostile = ["-v", "--help", "a'b\"c", "$(touch /tmp/x)", "`id`", "a;b", "*", ".", "[a-z]+", "\\"]
        (engine.data["redis"] / "datos.aof").write_text("a'b\"c y $(touch /tmp/x) y `id` y a;b")
        found = engine.inspector().disk_contains("redis", hostile)
        self.assertEqual(found, ["a'b\"c", "$(touch /tmp/x)", "`id`", "a;b"])
        run = engine.calls("redis")[-1]
        self.assertEqual(run[2:4], ["sh", "-c"])
        for needle in hostile:
            self.assertNotIn(needle, run[4], "los centinelas viajan como argumentos, no dentro del script")
        self.assertEqual(len(run[7:]), len(hostile), "un patrón por centinela")
        self.assertEqual(run[6], "/data", "el primer argumento es el directorio")
        self.assertIn(e2e_h4.needle_pattern(hostile[2]), run[7:], "cada centinela viaja como un argumento")

    def test_disk_contains_without_needles_or_with_unusable_ones(self) -> None:
        engine = FakeEngine(self.root)
        self.assertEqual(engine.inspector().disk_contains("redis", []), [])
        self.assertEqual(engine.calls(), [])
        for bad in ("", "uno\ndos", "nul\x00"):
            with self.assertRaises(e2e_h4.StepError, msg=repr(bad)):
                engine.inspector().disk_contains("redis", [DNI, bad])
        self.assertEqual(engine.calls(), [], "se rechazan antes de ejecutar nada")

    def test_disk_contains_cannot_read_a_missing_directory(self) -> None:
        engine = FakeEngine(self.root)
        engine.data["redis"].rmdir()
        with self.assertRaises(e2e_h4.StepError) as ctx:
            engine.inspector().disk_contains("redis", [DNI])
        self.assertIn("/data", str(ctx.exception))

    # --- reescritura del AOF ---
    def test_force_aof_rewrite_waits_for_the_rewrite_to_finish(self) -> None:
        engine = FakeEngine(self.root, info_polls={"redis": [1, 1, 1]})
        engine.inspector(poll_interval=0.01).force_aof_rewrite("redis")
        sent = [c[-1] for c in engine.calls("redis")]
        self.assertEqual(sent[0], "BGREWRITEAOF")
        self.assertEqual(sent.count("BGREWRITEAOF"), 1)
        self.assertGreaterEqual(sent.count("persistence"), 4, "pregunta hasta que aof_rewrite_in_progress es 0")
        self.assertEqual(sent[-1], "persistence")
        self.assertEqual(engine.calls("redis-ephemeral"), [], "solo toca la instancia que se le pide")

    def test_force_aof_rewrite_retries_when_one_is_already_running(self) -> None:
        busy = "ERR Background append only file rewriting already in progress"
        engine = FakeEngine(self.root, rewrite_replies={"redis": [busy, busy]}, info_polls={"redis": [1, 0]})
        engine.inspector(poll_interval=0.01).force_aof_rewrite("redis")
        sent = [c[-1] for c in engine.calls("redis")]
        self.assertEqual(sent.count("BGREWRITEAOF"), 3, "reintenta hasta que la suya empieza")

    def test_force_aof_rewrite_accepts_a_scheduled_rewrite(self) -> None:
        engine = FakeEngine(self.root, rewrite_replies={"redis": ["Background append only file rewriting scheduled"]})
        engine.inspector(poll_interval=0.01).force_aof_rewrite("redis")

    def test_force_aof_rewrite_times_out(self) -> None:
        engine = FakeEngine(self.root, info_polls={"redis": [1] * 1000})
        with self.assertRaises(e2e_h4.StepError) as ctx:
            engine.inspector(poll_interval=0.01, rewrite_timeout=0.2).force_aof_rewrite("redis")
        self.assertIn("no terminó", str(ctx.exception))

    def test_force_aof_rewrite_reports_a_failed_rewrite(self) -> None:
        engine = FakeEngine(self.root, bgrewrite_status={"redis": "err"})
        with self.assertRaises(e2e_h4.StepError) as ctx:
            engine.inspector(poll_interval=0.01).force_aof_rewrite("redis")
        self.assertIn("aof_last_bgrewrite_status", str(ctx.exception))

    def test_force_aof_rewrite_refused_by_redis(self) -> None:
        engine = FakeEngine(self.root, rewrite_replies={"redis": ["ERR unknown command"]})
        with self.assertRaises(e2e_h4.StepError) as ctx:
            engine.inspector(poll_interval=0.01).force_aof_rewrite("redis")
        self.assertIn("BGREWRITEAOF", str(ctx.exception))

    # --- la clave temporal del control positivo ---
    def test_a_temporary_key_is_written_and_deleted_with_arguments(self) -> None:
        engine = FakeEngine(self.root)
        inspector = engine.inspector()
        inspector.redis_set_temp("redis", "baas:e2e:control-abc", "valor con espacios; $(id)", ttl=120)
        self.assertEqual(engine.state()["set"]["redis"], {"baas:e2e:control-abc": "valor con espacios; $(id)"})
        run = engine.calls("redis")[-1]
        self.assertEqual(run[-5:], ["SET", "baas:e2e:control-abc", "valor con espacios; $(id)", "EX", "120"])
        self.assertNotIn("$(id)", run[4])
        inspector.redis_delete("redis", "baas:e2e:control-abc")
        self.assertEqual(engine.state()["set"]["redis"], {})
        self.assertEqual(engine.calls("redis")[-1][-2:], ["DEL", "baas:e2e:control-abc"])

    def test_temporary_keys_are_restricted_to_the_e2e_prefix(self) -> None:
        engine = FakeEngine(self.root)
        inspector = engine.inspector()
        for key in ("baas:policy:global", "baas:e2e:", "baas:e2e:A", "baas:e2e:a b", "x", "baas:e2e:*"):
            with self.assertRaises(e2e_h4.StepError, msg=key):
                inspector.redis_set_temp("redis", key, "v", ttl=60)
            with self.assertRaises(e2e_h4.StepError, msg=key):
                inspector.redis_delete("redis", key)
        for value, ttl in (("", 60), ("v", 0), ("v", 100000), ("a\x00b", 60)):
            with self.assertRaises(e2e_h4.StepError, msg=(value, ttl)):
                inspector.redis_set_temp("redis", "baas:e2e:ok", value, ttl=ttl)
        self.assertEqual(engine.calls(), [])


class MissingInspectorTest(unittest.TestCase):
    def test_every_query_fails_saying_how_to_continue(self) -> None:
        missing = e2e_h4.MissingInspector("no hay motor")
        calls = {
            "vault_rows": ("acme",), "conversation_ids": ("acme",), "redis_services": (),
            "redis_keys": ("acme", []), "redis_setting": ("redis", "appendonly"), "disk_files": ("redis",),
            "force_aof_rewrite": ("redis",), "disk_contains": ("redis", ["x"]),
            "redis_set_temp": ("redis", "baas:e2e:x", "v", 60), "redis_delete": ("redis", "baas:e2e:x"),
            "mcp_org_dirs": (), "mcp_quarantine": (),
        }  # fmt: skip
        for name, args in calls.items():
            with self.assertRaises(e2e_h4.StepError, msg=name) as ctx:
                getattr(missing, name)(*args)
            self.assertIn("--inspect none", str(ctx.exception), name)


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
        once += ["11.1 ", "11.2", "11.3", "11.4", "11.5", "11.6", "11.7", "11.8", "11.9", "11.10", "11.11", "11.12 "]
        once += ["11.12b", "11.13", "11.14"]
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
        self.assertFalse([n for n in steps if n.startswith(("8.", "9.", "11."))])
        with fake_h4_stack.FakeH4Stack() as stack:
            code, steps, out = run_e2e(stack, "--only", "interruptor", "--apis", "messages")
        self.assert_all_ok(code, steps, out)
        self.assertFalse([n for n in steps if n.startswith(("5.", "9.", "11."))])
        with fake_h4_stack.FakeH4Stack() as stack:
            code, steps, out = run_e2e(stack, "--only", "eliminar", "--inspect", "none", inspector=None)
        self.assert_all_ok(code, steps, out)
        self.assertEqual(len([n for n in steps if n.startswith("9.")]), 7, "sin inspector no hay pasos 9.1b ni 9.8")
        self.assertFalse([n for n in steps if n.startswith("11.")])

    def test_only_the_orphans_group(self) -> None:
        with fake_h4_stack.FakeH4Stack() as stack:
            code, steps, out = run_e2e(stack, "--only", "huerfanos", inspector=None)
            leftovers = sorted(stack.state.orgs)
            users = sorted(u.email for u in stack.state.users.values() if u.email.startswith("huerf-"))
            statuses = {u.status for u in stack.state.users.values() if u.email.startswith("huerf-")}
            keys = [k for u in stack.state.users.values() if u.email.startswith("huerf-") for k in u.keys]
        self.assert_all_ok(code, steps, out)
        self.assertEqual(len([n for n in steps if n.startswith("11.")]), 15)
        self.assertFalse([n for n in steps if n.startswith(("5.", "8.", "9."))])
        self.assertEqual(leftovers, ["baas-ai-ip", "demo", "observacion"], "las cuatro organizaciones se eliminaron")
        self.assertEqual(len(users), 7)
        # B, X, A y G (las de O2) y F quedan desactivados; C (O3, false) sigue ACTIVE y E sigue INVITED
        self.assertEqual(statuses, {"DISABLED", "ACTIVE", "INVITED"})
        self.assertEqual(sum(not k.revoked for k in keys), 1, "solo la clave de C sigue vigente")

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
        "preview-lists-all": {"11.3": ("lista a quien no se quedaría sin organización",)},
        "preview-open": {"11.4": ("USER de la organización", "403")},
        "preview-needs-block": {"11.3": ("409 ORG_NOT_BLOCKED",)},
        "orphans-kept": {"11.5": ("disabled_users = 0",)},
        "orphans-ignore-flag": {"11.9": ("disabled_users = 2",)},
        "orphans-default-off": {"11.10": ("disabled_users = 0",)},
        "orphans-other-org": {"11.5": ("disabled_users = 2",)},
        "orphans-invited-skipped": {"11.10": ("no lista a",)},
        "orphans-keys-kept": {"11.6": ("baas_invalid_api_key", "handshake"), "11.8": ("keys_revoked",)},
        "orphans-sessions-kept": {"11.6": ("su sesión de antes sigue abierta",)},
        "orphans-links-kept": {"11.13": ("vuelve a valer",)},
        "orphans-audit-missing": {"11.8": ("0 fila(s)",)},
        "orphans-audit-email": {"11.8": ("no debe llevar el email ni el nombre",)},
        "disabled-can-login": {"11.6": ("inicia sesión con la contraseña correcta",)},
        "reactivate-orgadmin": {"11.11": ("USER_DISABLED",)},
        "reactivate-no-audit": {"11.12 ": ("user.reactivated",)},
        "reactivate-restores-keys": {"11.12 ": ("clave de antes", "revocada")},
        "reactivate-audit-no-org": {"11.12 ": ("user.reactivated: org_slug = None",)},
        "audit-orgadmin-sees-disabled": {"11.12b": ("X ve 1 fila(s) user.disabled", "no debe ver ninguna")},
        "audit-orgadmin-blind": {"11.12b": ("X ve 0 fila(s) user.reactivated",)},
    }

    ORPHAN_PREFIXES = ("preview-", "orphans-", "reactivate-", "disabled-")  # solo los pasos 11 los comprueban

    def run_fault(self, fault: str) -> dict[str, dict]:
        only = ("--only", "huerfanos") if fault.startswith(self.ORPHAN_PREFIXES) else ()
        with fake_h4_stack.FakeH4Stack(faults={fault}) as stack:
            code, steps, out = run_e2e(stack, "--trace-timeout", "0.5", *only, inspector=None if only else "stack")
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
