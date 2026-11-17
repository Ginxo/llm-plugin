"""Utilidades de `dev/e2e_h2.py` (la prueba de punta a punta necesita la pila real; aquí, solo lo puro)."""

from __future__ import annotations

import e2e_h2
import pytest

DEV_TOTP_SECRET = "BAASAIIPDEVELOPMENTTOTPSECRETKEY"


def test_totp_rfc6238_vector() -> None:
    # RFC 6238, apéndice B: secreto ASCII «12345678901234567890», SHA-1, T = 59 s, 8 dígitos.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 del secreto del RFC
    assert e2e_h2.totp(secret, at=59, digits=8) == "94287082"
    assert e2e_h2.totp(secret, at=1111111109, digits=8) == "07081804"


def test_totp_dev_secret_has_six_digits() -> None:
    code = e2e_h2.totp(DEV_TOTP_SECRET, at=1_700_000_000)
    assert len(code) == 6
    assert code.isdigit()
    assert e2e_h2.totp(DEV_TOTP_SECRET.lower(), at=1_700_000_000) == code


def test_parse_custom_headers_multiline() -> None:
    raw = "X-Otra: 1\nX-Baas-Api-Key: baas_abc\n\nmalformada"
    assert e2e_h2.parse_custom_headers(raw) == {"X-Otra": "1", "X-Baas-Api-Key": "baas_abc"}


def test_cookies_from_set_cookie_headers() -> None:
    headers = [
        ("Set-Cookie", "baas_mfa=abc; HttpOnly; Path=/; SameSite=lax"),
        ("set-cookie", "baas_session=xyz=1; Max-Age=3600; Path=/"),
        ("Content-Type", "application/json"),
    ]
    assert e2e_h2.cookies_from(headers) == {"baas_mfa": "abc", "baas_session": "xyz=1"}


def test_runner_all_ok_exits_zero() -> None:
    runner = e2e_h2.Runner(echo=False)
    runner.step("uno", lambda: "bien")
    runner.step("dos", lambda: None)
    assert runner.exit_code() == 0
    assert [r.status for r in runner.results] == ["OK", "OK"]
    assert runner.results[0].detail == "bien"


def test_runner_failure_skips_dependents_and_exits_one() -> None:
    runner = e2e_h2.Runner(echo=False)

    def falla() -> str:
        raise e2e_h2.StepError("no cuadra")

    def revienta() -> str:
        raise RuntimeError("boom")

    runner.step("base", falla)
    runner.step("depende", lambda: "no debería ejecutarse", needs=("base",))
    runner.step("independiente", revienta)
    runner.step("sigue", lambda: "ok")
    statuses = {r.name: r.status for r in runner.results}
    assert statuses == {"base": "FALLO", "depende": "SALTADO", "independiente": "FALLO", "sigue": "OK"}
    assert "no cuadra" in runner.results[0].detail
    assert "RuntimeError" in runner.results[2].detail
    assert runner.exit_code() == 1


def test_runner_records_duration() -> None:
    runner = e2e_h2.Runner(echo=False)
    runner.step("rápido", lambda: "ok")
    assert runner.results[0].seconds >= 0
    assert runner.report()["ok"] is True


def test_check_helper() -> None:
    e2e_h2.check(True, "no se lanza")
    with pytest.raises(e2e_h2.StepError, match="mensaje"):
        e2e_h2.check(False, "mensaje")


def test_parse_args_defaults_and_urls() -> None:
    ns = e2e_h2.parse_args(["--app-url", "http://app.localhost:48080/", "--no-claude"])
    assert ns.app_url == "http://app.localhost:48080"
    assert ns.email == "analista@demo.test"
    assert ns.org == "demo"
    assert ns.no_claude is True


def _conv(cid: str, turns: int, provider: str = "ANTHROPIC", email: str = "analista@demo.test") -> dict:
    return {"id": cid, "turn_count": turns, "provider": provider, "user": {"email": email}}


def test_new_trace_detects_new_or_grown_conversation() -> None:
    baseline = {"a": 1, "b": 2}
    kwargs = {"email": "analista@demo.test", "provider": "anthropic"}
    assert e2e_h2.new_trace(baseline, [_conv("a", 1), _conv("b", 2)], **kwargs) is None
    assert e2e_h2.new_trace(baseline, [_conv("c", 0)], **kwargs) is None  # aún sin turno registrado
    assert e2e_h2.new_trace(baseline, [_conv("a", 1), _conv("c", 1)], **kwargs)["id"] == "c"
    assert e2e_h2.new_trace(baseline, [_conv("b", 3)], **kwargs)["id"] == "b"
    assert e2e_h2.new_trace(baseline, [_conv("d", 1, provider="OPENAI")], **kwargs) is None
    assert e2e_h2.new_trace(baseline, [_conv("d", 1, email="otro@demo.test")], **kwargs) is None
