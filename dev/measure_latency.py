#!/usr/bin/env python3
"""Latencia de extremo a extremo por la pila real (Caddy → proxy → proveedor falso con eco).

Mide lo que nota un cliente: una conexión persistente (http.client) y tres escenarios, en milisegundos
(p50 / p95 / p99 / máximo). Sirve para comparar dos versiones de la pila con la misma carga (hito H4, H4-02):

- ``nueva``: una conversación nueva en cada petición con AI-IP y anonimizador (vault vacío);
- ``larga``: una sola conversación con historial creciente (lectura del vault caliente en cada turno);
- ``bypass``: una conversación nueva en cada petición con AI-IP apagado.

Usa los usuarios y organizaciones de la semilla de desarrollo (``analista@privada.test`` en ``privada`` y
``analista@demo.test`` en ``demo``) y las opciones de ``e2e_h4.py`` para las URLs. Solo biblioteca estándar.

    python3 dev/measure_latency.py --n 300 --label antes
"""

from __future__ import annotations

import argparse
import http.client
import json
import statistics
import sys
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import e2e_h3 as h3
import e2e_h4 as m

ORG_ANON, EMAIL_ANON = "privada", "analista@privada.test"
ORG_BYPASS, EMAIL_BYPASS = "demo", "analista@demo.test"
MAX_LONG_TURNS = 60


class Client:
    """Una conexión persistente al proxy de una organización."""

    def __init__(self, e2e: m.E2EH4, email: str, org: str, *, ai_ip: bool) -> None:
        e2e.keys[email] = m.dev_api_key(email)
        binding = e2e.handshake(email, org, ai_ip=ai_ip)
        url = urlsplit(h3.api_url(binding, "messages"))
        self.path = url.path
        self.conn = http.client.HTTPConnection("127.0.0.1", url.port or 80, timeout=30)
        self.headers = {
            "Host": url.netloc,
            "Content-Type": "application/json",
            "X-Baas-Api-Key": e2e.key_of(email),
            "x-api-key": h3.FAKE_ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        }

    def post(self, payload: dict[str, Any]) -> tuple[float, bytes]:
        body = json.dumps(payload).encode()
        start = time.perf_counter()
        self.conn.request("POST", self.path, body=body, headers=self.headers)
        response = self.conn.getresponse()
        data = response.read()
        elapsed = (time.perf_counter() - start) * 1000
        if response.status != 200:
            raise SystemExit(f"HTTP {response.status}: {data[:200]!r}")
        return elapsed, data


def report(label: str, name: str, values: list[float]) -> None:
    ordered = sorted(values)

    def quantile(p: float) -> float:
        return ordered[min(len(ordered) - 1, int(len(ordered) * p))]

    print(
        f"{label:<10} {name:<8} n={len(ordered):<4} p50={statistics.median(ordered):6.2f}  "
        f"p95={quantile(0.95):6.2f}  p99={quantile(0.99):6.2f}  max={ordered[-1]:7.2f} ms"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=300, help="peticiones por escenario (la larga, como mucho 60)")
    parser.add_argument("--label", default="", help="etiqueta que precede a cada línea")
    own, rest = parser.parse_known_args(argv)
    e2e = m.E2EH4(m.parse_args(rest))
    anon = Client(e2e, EMAIL_ANON, ORG_ANON, ai_ip=True)
    bypass = Client(e2e, EMAIL_BYPASS, ORG_BYPASS, ai_ip=False)
    text = f"{m.SENSITIVE_TEXT}."
    run = uuid.uuid4().hex[:8]

    for client in (anon, bypass):  # calentamiento
        for i in range(20):
            client.post(h3.request_body("messages", f"warm-{run}-{i}", f"warm {run} {i}. {text}"))

    fresh = [
        anon.post(h3.request_body("messages", f"nueva-{run}-{i}", f"nueva {run} {i}. {text}"))[0] for i in range(own.n)
    ]
    report(own.label, "nueva", fresh)

    long_run: list[float] = []
    history: list[dict[str, Any]] = []
    model = f"larga-{run}"
    for i in range(min(own.n, MAX_LONG_TURNS)):
        message = f"larga {run} turno {i}. {text}"
        elapsed, data = anon.post(h3.request_body("messages", model, message, history=history))
        long_run.append(elapsed)
        reply = h3.extract_reply("messages", data, stream=False).texts[0][1]
        history += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
    report(own.label, "larga", long_run)

    plain = [
        bypass.post(h3.request_body("messages", f"bypass-{run}-{i}", f"bypass {run} {i}. {text}"))[0]
        for i in range(own.n)
    ]
    report(own.label, "bypass", plain)
    return 0


if __name__ == "__main__":
    sys.exit(main())
