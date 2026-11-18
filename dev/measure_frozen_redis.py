"""Redis congelado sobre una pila real: línea base, congelado (0 s, 4,5 min, 5,5 min) y recuperación.

    uv run python dev/measure_frozen_redis.py salida.json

Pausa el contenedor `h4-int_redis_1` (cambia la constante REDIS si tu proyecto de compose se llama de otro modo)
y lo reanuda al terminar, también si falla. Tarda unos 6 minutos. Resultados en
`knowledge/01-producto/fases/fase-1-h4-mediciones.md`.
`podman pause` deja a Redis aceptando conexiones sin responder (el «congelado» del contrato §7.3).
"""

# ruff: noqa: S310  # script de desarrollo: HTTP contra la pila local, contraseña y azar de pruebas

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "dev"))
from e2e_h2 import dev_api_key  # noqa: E402

from baas_plugin.mcp_client import McpClient  # noqa: E402

APP = "http://app.localhost:28080"
PROXY = "http://proxy.localhost:28080"
MCP = "http://mcp.localhost:28080"
REDIS = "h4-int_redis_1"
OUT = Path(sys.argv[1])
TIMEOUT = 20


def timed(fn):
    t0 = time.perf_counter()
    try:
        status = fn()
    except Exception as exc:
        status = f"EXC {type(exc).__name__}: {str(exc)[:80]}"
    return round(time.perf_counter() - t0, 3), status


def post(url: str, headers: dict, body: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code


MSG = {"model": "claude-test", "max_tokens": 16, "messages": [{"role": "user", "content": "hola DNI 12345678Z"}]}


def proxy_call(org: str, email: str) -> int:
    h = {
        "Content-Type": "application/json",
        "X-Baas-Api-Key": dev_api_key(email),
        "x-api-key": "sk-ant-fake",
        "anthropic-version": "2023-06-01",
    }
    return post(f"{PROXY}/org/{org}/anthropic/v1/messages", h, MSG)


def mcp_call() -> str:
    with McpClient(f"{MCP}/org/demo/mcp", dev_api_key("analista@demo.test")) as c:
        data = c.call_tool("baas_search_knowledge", {"query": "indicadores de agenda", "limit": 3})
    return f"ok:{len(data.get('results') or [])}"


def login_call() -> int:
    return post(
        f"{APP}/api/v1/auth/login",
        {"Content-Type": "application/json", "X-Requested-With": "baas-webapp"},
        {"email": "analista@demo.test", "password": "baas-dev-password"},
    )


CALLS = {
    "proxy_bypass(demo)": lambda: proxy_call("demo", "analista@demo.test"),
    "proxy_aiip(privada)": lambda: proxy_call("privada", "orgadmin@privada.test"),
    "mcp_search(demo)": mcp_call,
    "login": login_call,
}


def round_(n: int) -> dict:
    return {name: [timed(fn) for _ in range(n)] for name, fn in CALLS.items()}


def p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(0.95 * (len(xs) - 1)))]


def summarize(label: str, res: dict) -> dict:
    out = {}
    for name, rows in res.items():
        lat = [r[0] for r in rows]
        out[name] = {"first": rows[0], "max": max(lat), "p95": p95(lat), "statuses": sorted({str(r[1]) for r in rows})}
    print(label, json.dumps(out, ensure_ascii=False), flush=True)
    return out


results: dict = {}
paused_at = None
try:
    for _ in range(2):  # calentar
        for fn in CALLS.values():
            timed(fn)
    results["baseline"] = summarize("baseline", round_(12))

    subprocess.run(["podman", "pause", REDIS], check=True, capture_output=True)
    paused_at = time.time()
    print("Redis congelado", flush=True)
    results["t0_frozen"] = summarize("t0", round_(12))

    wait = 270 - (time.time() - paused_at)  # 4 min 30 s
    time.sleep(max(0, wait))
    results["t4m30_inside_window"] = summarize(f"t+{int(time.time() - paused_at)}s", round_(4))

    wait = 330 - (time.time() - paused_at)  # 5 min 30 s
    time.sleep(max(0, wait))
    results["t5m30_after_window"] = summarize(f"t+{int(time.time() - paused_at)}s", round_(4))
finally:
    if paused_at is not None:
        subprocess.run(["podman", "unpause", REDIS], capture_output=True)
        print("Redis reanudado", flush=True)
    time.sleep(4)
    results["recovered_t+4s"] = summarize("recuperado", round_(6))
    time.sleep(6)
    results["recovered_t+16s"] = summarize("recuperado2", round_(6))
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False))
