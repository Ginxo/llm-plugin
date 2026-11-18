#!/usr/bin/env python3
"""¿Nota el cliente que la instancia efímera de Redis falla? (hito H4, H4-02: criterio de transparencia)

Peticiones continuas (con AI-IP y anonimizador; mitad conversaciones nuevas y mitad una larga con historial) mientras
se para (``stop``), congela (``pause``) o no se toca (``none``, el control) el contenedor ``redis-ephemeral`` de la
pila de desarrollo. Imprime, antes, durante y después del fallo: p50, p95, máximo, peticiones de más de 100 ms y
errores; la petición más lenta durante el fallo; y cuántas conversaciones de la prueba aparecen en la webapp al
recuperarse (las trazas se encolan en el proxy y llegan después). La mediana sube también en el control porque el
historial de la conversación larga crece: compara siempre con ``none``.

    python3 dev/measure_ephemeral_outage.py --mode stop --before 4 --during 8 --after 6
"""

from __future__ import annotations

import argparse
import http.client
import json
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import e2e_h3 as h3
import e2e_h4 as m

ORG, EMAIL = "privada", "analista@privada.test"
SLOW_MS = 100
PACE_SECONDS = 0.05


def engine_command(engine: str, action: str, container: str) -> None:
    subprocess.run([engine, action, container], check=True, capture_output=True)


def summarize(rows: list[tuple[float, str, float, int]], phase: str) -> str | None:
    values = [r[2] for r in rows if r[1] == phase]
    if not values:
        return None
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    errors = sum(1 for r in rows if r[1] == phase and r[3] != 200)
    slow = sum(1 for v in values if v > SLOW_MS)
    return (
        f"{phase:<8} n={len(values):<4} p50={statistics.median(values):6.2f}  p95={p95:6.2f}  "
        f"max={max(values):7.2f} ms  >{SLOW_MS} ms: {slow}  errores: {errors}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("stop", "pause", "none"), required=True)
    parser.add_argument("--before", type=float, default=4, help="segundos antes del fallo")
    parser.add_argument("--during", type=float, default=8, help="segundos con el fallo")
    parser.add_argument("--after", type=float, default=6, help="segundos tras recuperarse")
    parser.add_argument("--wait", type=float, default=45, help="segundos que se espera a las trazas")
    own, rest = parser.parse_known_args(argv)
    args = m.parse_args(rest)
    engine = shutil.which("podman") or shutil.which("docker")
    if engine is None and own.mode != "none":
        raise SystemExit("hace falta podman o docker para parar o congelar la efímera")
    container = f"{args.compose_project}_redis-ephemeral_1"

    e2e = m.E2EH4(args)
    e2e.keys[EMAIL] = m.dev_api_key(EMAIL)
    binding = e2e.handshake(EMAIL, ORG, ai_ip=True)
    e2e.login(args.admin_email)
    url = urlsplit(h3.api_url(binding, "messages"))
    conn = http.client.HTTPConnection("127.0.0.1", url.port or 80, timeout=30)
    headers = {
        "Host": url.netloc,
        "Content-Type": "application/json",
        "X-Baas-Api-Key": e2e.key_of(EMAIL),
        "x-api-key": h3.FAKE_ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
    }

    def post(payload: dict[str, Any]) -> tuple[float, int, bytes]:
        start = time.perf_counter()
        conn.request("POST", url.path, body=json.dumps(payload).encode(), headers=headers)
        response = conn.getresponse()
        data = response.read()
        return (time.perf_counter() - start) * 1000, response.status, data

    run = uuid.uuid4().hex[:8]
    text = f"{m.SENSITIVE_TEXT}."
    long_model = f"outage-{run}-long"
    history: list[dict[str, Any]] = []
    rows: list[tuple[float, str, float, int]] = []
    sent: list[str] = [long_model]
    fail_at, back_at = own.before, own.before + own.during
    failed = restored = False
    started = time.monotonic()
    i = 0
    try:
        while time.monotonic() - started < own.before + own.during + own.after:
            now = time.monotonic() - started
            if not failed and now >= fail_at:
                if own.mode != "none":
                    engine_command(engine or "", own.mode, container)
                failed, fail_at = True, now
            if not restored and now >= back_at:
                if own.mode != "none":
                    engine_command(engine or "", "start" if own.mode == "stop" else "unpause", container)
                restored, back_at = True, now
            if i % 2:
                message = f"{long_model} turno {i}. {text}"
                payload = h3.request_body("messages", long_model, message, history=history)
            else:
                model = f"outage-{run}-{i}"
                sent.append(model)
                payload = h3.request_body("messages", model, f"{model}. {text}")
            elapsed, status, data = post(payload)
            if status == 200 and i % 2:
                reply = h3.extract_reply("messages", data, stream=False).texts[0][1]
                history += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
            phase = "antes" if now < fail_at else "durante" if now < back_at else "después"
            rows.append((now, phase, elapsed, status))
            i += 1
            time.sleep(PACE_SECONDS)
    finally:
        if failed and not restored and own.mode != "none":  # no deja la pila rota si algo falla a mitad
            engine_command(engine or "", "start" if own.mode == "stop" else "unpause", container)

    print(
        f"### {own.mode} de la efímera: fallo en t={fail_at:.1f}s, recuperada en t={back_at:.1f}s; "
        f"{len(rows)} peticiones"
    )
    for phase in ("antes", "durante", "después"):
        line = summarize(rows, phase)
        if line:
            print(line)
    after_failure = [r for r in rows if r[0] >= fail_at][:6]
    print("primeras peticiones tras el fallo:", ", ".join(f"{r[2]:.1f}" for r in after_failure), "ms")
    during = [r for r in rows if r[1] == "durante"]
    if during:
        worst = max(during, key=lambda r: r[2])
        print(f"la más lenta durante el fallo: {worst[2]:.1f} ms (t={worst[0]:.2f}s)")

    deadline = time.time() + own.wait
    found = 0
    while time.time() < deadline:
        path = f"/api/v1/conversations?org={ORG}&q=outage-{run}&limit=200"
        items = (e2e.webapp("GET", path, args.admin_email).json() or {}).get("items") or []
        found = sum(1 for x in items if str(x.get("model", "")).startswith(f"outage-{run}"))
        if found >= len(sent):
            break
        time.sleep(1)
    print(f"trazas: {found} de {len(sent)} conversaciones aparecen en la webapp tras recuperarse")
    return 0 if found >= len(sent) and not any(r[3] != 200 for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
