"""Mediciones del hito H4 sobre una pila real: kill switch → rechazo (proxy y MCP) y petición al proxy → WebSocket.

Pila de `deploy/scripts/dev-up.sh --fake-upstream` (por defecto en `*.localhost:28080`: ajusta las constantes
APP, PROXY y MCP si usas otros puertos). Usa usuarios de desarrollo y cambia el kill switch global de la pila
(lo deja apagado). Resultados de H4 en `knowledge/01-producto/fases/fase-1-h4-mediciones.md`.

    uv run python dev/measure_h4.py salida.json [repeticiones=30]
"""

# ruff: noqa: S310, S311, S105  # script de desarrollo: HTTP contra la pila local, contraseña y azar de pruebas

from __future__ import annotations

import json
import random
import statistics
import string
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "dev"))
from e2e_h2 import dev_api_key  # noqa: E402
from e2e_h4 import WsClient  # noqa: E402

from baas_plugin.mcp_client import McpClient  # noqa: E402

APP = "http://app.localhost:28080"
PROXY = "http://proxy.localhost:28080"
MCP = "http://mcp.localhost:28080"
PASSWORD = "baas-dev-password"
OUT = Path(sys.argv[1])
N = int(sys.argv[2]) if len(sys.argv) > 2 else 30


def req(method, url, headers=None, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def login(email: str) -> str:
    status, _, headers = req(
        "POST",
        f"{APP}/api/v1/auth/login",
        {"Content-Type": "application/json", "X-Requested-With": "baas-webapp"},
        {"email": email, "password": PASSWORD},
    )
    assert status == 200, status
    for h in headers.get_all("Set-Cookie") or []:
        if h.startswith("baas_session="):
            return h.split(";", 1)[0].split("=", 1)[1]
    raise SystemExit("sin cookie de sesión")


def msg_body(text: str) -> dict:
    return {"model": "claude-test", "max_tokens": 16, "messages": [{"role": "user", "content": text}]}


def proxy_status(org: str, email: str, text: str) -> int:
    h = {
        "Content-Type": "application/json",
        "X-Baas-Api-Key": dev_api_key(email),
        "x-api-key": "sk-ant-fake",
        "anthropic-version": "2023-06-01",
    }
    return req("POST", f"{PROXY}/org/{org}/anthropic/v1/messages", h, msg_body(text))[0]


def mcp_state() -> str:
    try:
        with McpClient(f"{MCP}/org/demo/mcp", dev_api_key("analista@demo.test")) as c:
            c.call_tool("baas_search_knowledge", {"query": "indicadores de agenda", "limit": 1})
        return "ok"
    except Exception as exc:
        return type(exc).__name__ + ":" + str(exc)[:60]


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]


def summarize(xs):
    return {
        "n": len(xs),
        "p50": round(statistics.median(xs), 3),
        "p95": round(pct(xs, 0.95), 3),
        "max": round(max(xs), 3),
    }


def kill_switch(admin_cookie: str) -> dict:
    hdr = {
        "Content-Type": "application/json",
        "X-Requested-With": "baas-webapp",
        "Cookie": f"baas_session={admin_cookie}",
    }
    proxy_t, mcp_t, put_t = [], [], []
    for i in range(N):
        res: dict[str, float] = {}
        t0 = time.perf_counter()

        def watch_proxy(t0=t0, i=i, res=res):
            while time.perf_counter() - t0 < 10:
                if proxy_status("demo", "analista@demo.test", f"ks {i}") == 403:
                    res["proxy"] = time.perf_counter() - t0
                    return
                time.sleep(0.003)

        def watch_mcp(t0=t0, res=res):
            while time.perf_counter() - t0 < 10:
                if mcp_state() != "ok":
                    res["mcp"] = time.perf_counter() - t0
                    return
                time.sleep(0.003)

        threads = [threading.Thread(target=watch_proxy), threading.Thread(target=watch_mcp)]
        for t in threads:
            t.start()
        s, body, _ = req("PUT", f"{APP}/api/v1/platform/kill-switch", hdr, {"active": True, "reason": f"medicion {i}"})
        put_t.append(time.perf_counter() - t0)
        assert s == 200, (s, body[:120])
        for t in threads:
            t.join()
        proxy_t.append(res.get("proxy", 10.0))
        mcp_t.append(res.get("mcp", 10.0))
        s, body, _ = req("PUT", f"{APP}/api/v1/platform/kill-switch", hdr, {"active": False, "reason": f"fin {i}"})
        assert s == 200, (s, body[:120])
        for _ in range(400):  # vuelve a aceptar
            if proxy_status("demo", "analista@demo.test", "vuelta") == 200 and mcp_state() == "ok":
                break
            time.sleep(0.01)
        time.sleep(0.2)
    return {"proxy": summarize(proxy_t), "mcp": summarize(mcp_t), "put_http": summarize(put_t)}


def websocket(org: str, owner: str, cookie: str, label: str) -> dict:
    ws = WsClient(f"ws://app.localhost:28080/api/v1/ws/traces?org={org}", cookie=cookie, origin=APP, timeout=10)
    first, last = [], []
    try:
        assert '"hello"' in (ws.recv(5) or "")
        for _ in range(N):
            tag = (
                "ws" + label + "".join(random.choices(string.ascii_lowercase, k=14))
            )  # sin dígitos: las reglas de AI-IP los toman por teléfonos
            t0 = time.perf_counter()
            status = proxy_status(org, owner, f"{tag} DNI 12345678Z")
            assert status == 200, status
            t_first = t_done = None
            deadline = t0 + 5
            while time.perf_counter() < deadline and t_done is None:
                frame = ws.recv(0.05)
                if frame is None:
                    continue
                now = time.perf_counter()
                if tag in frame or (t_first is not None and '"turn.completed"' in frame):
                    if t_first is None:
                        t_first = now - t0
                    if '"turn.completed"' in frame:
                        t_done = now - t0
            first.append(t_first if t_first is not None else 5.0)
            last.append(t_done if t_done is not None else 5.0)
            time.sleep(0.1)
    finally:
        ws.close()
    return {"primera_trama": summarize(first), "turno_completado": summarize(last)}


if __name__ == "__main__":
    out: dict = {"n": N}
    pa = login("admin@baas.test")
    out["websocket_privada_aiip"] = websocket(
        "privada", "orgadmin@privada.test", login("orgadmin@privada.test"), "aiip"
    )
    print("ws privada", out["websocket_privada_aiip"], flush=True)
    out["websocket_demo_bypass"] = websocket("demo", "analista@demo.test", login("orgadmin@demo.test"), "byp")
    print("ws demo", out["websocket_demo_bypass"], flush=True)
    out["kill_switch"] = kill_switch(pa)
    print("kill switch", out["kill_switch"], flush=True)
    OUT.write_text(json.dumps(out, indent=2))
