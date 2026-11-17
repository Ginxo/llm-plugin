#!/usr/bin/env python3
"""Prueba de punta a punta del hito H2 (épica E4) contra una pila real de BaaS-AI-IP.

Recorre lo que hace un usuario con el plugin, desde un `HOME` y un proyecto temporales y limpios:

  0. La webapp responde.
  1. `/baas-init` sin interacción (`set-key --stdin` + `init`) con la clave determinista de desarrollo
     (contrato §8), cronometrado (objetivo < 2 min); ficheros del proyecto y MCP registrado sin la clave.
  2. `status`.
  3-4. `baas_search_knowledge` y `baas_read_document` sobre contenido de `analista`.
  5. Lectura de `knowledge/calidad/criterios-de-revision.md` → `AUTHORIZATION_DENIED` (criterio 6).
  6. `baas_get_script` y `run-script` con su `sha256` verificado (y un `sha256` falso, rechazado).
  7. Sesión en la webapp (contraseña y TOTP, o solo contraseña con `BAAS_DEV_SKIP_TOTP=1`) y petición de
     Anthropic por el proxy con `ANTHROPIC_BASE_URL` y la cabecera de `.claude/settings.local.json`.
  8. Esa petición aparece como traza en `GET /api/v1/conversations`.

Uso (la pila de `deploy`: `scripts/dev-up.sh --fake-upstream`):

    uv run python dev/e2e_h2.py --app-url http://app.localhost:8080

Las URLs del MCP y del proxy salen del *handshake*, igual que en el plugin. Sale con 0 si todo pasa,
1 si algún paso falla y 2 si los argumentos no son válidos. No se distribuye con el plugin.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from baas_plugin.errors import McpToolError  # noqa: E402
from baas_plugin.mcp_client import McpClient  # noqa: E402

BAAS = [sys.executable, str(REPO / "scripts" / "baas.py")]
DEV_PASSWORD = "baas-dev-password"  # noqa: S105 (contrato §8: valor de prueba de desarrollo)
DEV_TOTP_SECRET = "BAASAIIPDEVELOPMENTTOTPSECRETKEY"  # noqa: S105 (contrato §8)
FAKE_PROVIDER_KEY = "sk-ant-api03-FAKE-e2e-provider-key"  # credencial de proveedor ficticia
INIT_TARGET_SECONDS = 120
DENIED_DOC = "knowledge/calidad/criterios-de-revision.md"
ANALISTA_DOC = "knowledge/operaciones/indicadores-de-agenda.md"
SCRIPT_PATH = "skills/analisis-de-citas/scripts/resumen_citas.py"
CSV = "especialidad,estado,citas\ncardiologia,atendida,40\ncardiologia,no_presentado,10\ncardiologia,cancelada,5\n"
CSV_EXPECTED = "cardiologia: 50 citas, 20.0 % no presentados"


# --- utilidades puras ------------------------------------------------------------------------------


class StepError(Exception):
    """Un paso no cumple lo esperado (fallo de la prueba, no del script)."""


def check(condition: object, message: str) -> None:
    if not condition:
        raise StepError(message)


def dev_api_key(email: str) -> str:
    """API key determinista de desarrollo (contrato §8)."""
    digest = hashlib.sha256(f"baas-dev-key:{email}".encode()).digest()
    return "baas_" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def totp(secret_b32: str, *, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """TOTP del RFC 6238 con HMAC-SHA1 (lo que usa la webapp)."""
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**digits
    return str(code).zfill(digits)


def parse_custom_headers(raw: str) -> dict[str, str]:
    """`ANTHROPIC_CUSTOM_HEADERS`: una cabecera `Nombre: valor` por línea."""
    headers: dict[str, str] = {}
    for line in raw.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip():
            headers[name.strip()] = value.strip()
    return headers


def cookies_from(headers: Iterable[tuple[str, str]]) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for name, value in headers:
        if name.lower() == "set-cookie":
            pair = value.split(";", 1)[0]
            key, _, val = pair.partition("=")
            cookies[key.strip()] = val.strip()
    return cookies


def new_trace(
    baseline: dict[str, int], items: Iterable[dict[str, Any]], *, email: str, provider: str
) -> dict[str, Any] | None:
    """La conversación que es nueva o tiene más turnos que en `baseline` (id → turnos), del usuario y proveedor."""
    for item in items:
        turns = item.get("turn_count") or 0
        if (
            str(item.get("provider") or "").lower() == provider
            and (item.get("user") or {}).get("email") == email
            and turns >= 1
            and turns > baseline.get(str(item.get("id")), 0)
        ):
            return item
    return None


@dataclass
class StepResult:
    name: str
    status: str  # OK · FALLO · SALTADO
    seconds: float = 0.0
    detail: str = ""


@dataclass
class Runner:
    echo: bool = True
    results: list[StepResult] = field(default_factory=list)

    def _status(self, name: str) -> str | None:
        return next((r.status for r in self.results if r.name == name), None)

    def step(self, name: str, fn: Callable[[], object], *, needs: Iterable[str] = ()) -> object:
        missing = [n for n in needs if self._status(n) != "OK"]
        if missing:
            return self._record(StepResult(name, "SALTADO", 0.0, f"depende de: {', '.join(missing)}"))
        start = time.monotonic()
        try:
            value = fn()
        except StepError as exc:
            self._record(StepResult(name, "FALLO", time.monotonic() - start, str(exc)))
            return None
        except Exception as exc:
            tail = traceback.format_exc(limit=3).strip().splitlines()[-1]
            self._record(StepResult(name, "FALLO", time.monotonic() - start, f"{type(exc).__name__}: {exc} ({tail})"))
            return None
        self._record(StepResult(name, "OK", time.monotonic() - start, "" if value is None else str(value)))
        return value

    def _record(self, result: StepResult) -> None:
        self.results.append(result)
        if self.echo:
            print(f"[{result.status:^7}] {result.name} ({result.seconds:.2f} s)")
            for line in result.detail.splitlines():
                print(f"          {line}")
            sys.stdout.flush()

    def exit_code(self) -> int:
        return 0 if all(r.status == "OK" for r in self.results) else 1

    def report(self) -> dict[str, Any]:
        return {"ok": self.exit_code() == 0, "steps": [asdict(r) for r in self.results]}


# --- HTTP (con todas las cabeceras de la respuesta, para las cookies) ----------------------------


@dataclass
class Http:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http(method: str, url: str, *, headers: dict[str, str] | None = None, payload: Any = None) -> Http:
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method=method)  # noqa: S310 (URL de la pila de desarrollo)
    req.add_header("User-Agent", "baas-e2e-h2/1")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=30) as resp:
            return Http(resp.status, list(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as exc:
        return Http(exc.code, list(exc.headers.items()) if exc.headers else [], exc.read() or b"")


# --- la prueba ------------------------------------------------------------------------------------


class E2E:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.key = dev_api_key(args.email)
        self.work = Path(tempfile.mkdtemp(prefix="baas-e2e-h2-"))
        self.home = self.work / "home"
        self.project = self.work / "proyecto"
        self.tmp = self.work / "tmp"
        for d in (self.home, self.project, self.tmp):
            d.mkdir(mode=0o700)
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.env = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(self.tmp),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        if args.no_claude:
            self.env["BAAS_CLAUDE_BIN"] = str(self.work / "sin-claude")  # no existe → registro manual
        self.binding: dict[str, Any] = {}
        self.session_cookie: str | None = None
        self.baseline: dict[str, int] = {}  # conversaciones (id → turnos) antes de la petición por el proxy
        self.proxy_sent_at: datetime | None = None
        self.timings: dict[str, float] = {}

    # utilidades
    def baas(self, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*BAAS, *argv],
            cwd=self.project,
            env=self.env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    def mcp(self) -> McpClient:
        return McpClient(str(self.binding["mcp_url"]), self.key)

    def cleanup(self) -> None:
        if self.args.keep:
            print(f"Directorio de trabajo conservado: {self.work}")
        else:
            shutil.rmtree(self.work, ignore_errors=True)

    # 0
    def webapp_up(self) -> str:
        res = http("GET", f"{self.args.app_url}/healthz")
        check(res.status == 200, f"GET {self.args.app_url}/healthz → HTTP {res.status}")
        return f"{self.args.app_url} responde"

    # 1
    def init(self) -> str:
        t0 = time.monotonic()
        r = self.baas("set-key", "--stdin", "--app-url", self.args.app_url, stdin=self.key + "\n")
        check(r.returncode == 0, f"set-key salió con {r.returncode}: {r.stderr.strip()}")
        t1 = time.monotonic()
        argv = ["init", "--project", str(self.project)]
        if self.args.org:
            argv += ["--org", self.args.org]
        r = self.baas(*argv)
        t2 = time.monotonic()
        self.timings = {"set_key": t1 - t0, "init": t2 - t1, "baas_init_total": t2 - t0}
        check(r.returncode == 0, f"init salió con {r.returncode}: {(r.stdout + r.stderr).strip()}")
        check(self.key not in r.stdout + r.stderr, "init imprimió la API key")
        total = self.timings["baas_init_total"]
        check(total < INIT_TARGET_SECONDS, f"/baas-init tardó {total:.1f} s (objetivo < {INIT_TARGET_SECONDS} s)")

        claude_dir = self.project / ".claude"
        settings = json.loads((claude_dir / "settings.local.json").read_text())
        self.binding = json.loads((claude_dir / "baas.local.json").read_text())
        check(self.binding.get("org") == self.args.org, f"organización vinculada: {self.binding.get('org')}")
        env = settings.get("env", {})
        check(env.get("ANTHROPIC_BASE_URL") == self.binding["proxy_anthropic_url"], "ANTHROPIC_BASE_URL incorrecta")
        check(
            parse_custom_headers(env.get("ANTHROPIC_CUSTOM_HEADERS", "")).get("X-Baas-Api-Key") == self.key,
            "ANTHROPIC_CUSTOM_HEADERS no lleva la X-Baas-Api-Key",
        )
        check(self.key not in (claude_dir / "baas.local.json").read_text(), "baas.local.json contiene la clave")
        check((os.stat(claude_dir / "settings.local.json").st_mode & 0o777) == 0o600, "settings.local.json no es 0600")
        gitignore = (self.project / ".gitignore").read_text()
        check(".claude/settings.local.json" in gitignore, ".gitignore sin settings.local.json")
        env_file = self.home / "baas-ai-ip" / ".env"
        check((os.stat(env_file).st_mode & 0o777) == 0o600, "~/baas-ai-ip/.env no es 0600")
        return (
            f"/baas-init en {total:.2f} s (set-key {self.timings['set_key']:.2f} s + init {self.timings['init']:.2f} s;"
            f" objetivo < {INIT_TARGET_SECONDS} s) · org {self.binding['org']} · MCP {self.binding['mcp_url']}"
            f" · proxy {self.binding['proxy_anthropic_url']}\n" + r.stdout.strip()
        )

    def mcp_registered(self) -> str:
        if self.args.no_claude:
            return "omitido (--no-claude): init imprime el comando para registrarlo a mano"
        claude_json = self.home / ".claude.json"
        check(claude_json.exists(), "no existe ~/.claude.json: `claude mcp add-json` no se ejecutó")
        raw = claude_json.read_text()
        check(self.key not in raw, "~/.claude.json contiene la API key")
        servers = [p.get("mcpServers", {}).get("baas") for p in (json.loads(raw).get("projects") or {}).values()]
        baas = next((s for s in servers if s), None)
        check(baas is not None, "el servidor `baas` no está en el ámbito local del proyecto")
        check(baas.get("type") == "http" and baas.get("url") == self.binding["mcp_url"], f"configuración: {baas}")
        check("baas_mcp_headers.py" in str(baas.get("headersHelper")), "falta el headersHelper")
        return f"`baas` en ~/.claude.json (ámbito local): {baas['url']}, headersHelper, sin la clave"

    # 2
    def status(self) -> str:
        r = self.baas("status", "--project", str(self.project))
        out = r.stdout + r.stderr
        check(r.returncode == 0, f"status salió con {r.returncode}: {out.strip()}")
        for expected in (self.args.email, f"Organización del proyecto: {self.args.org}", "MCP `baas`: accesible"):
            check(expected in out, f"falta «{expected}» en la salida de status:\n{out.strip()}")
        check(self.key not in out, "status imprimió la API key")
        return r.stdout.strip()

    # 3
    def search(self) -> str:
        with self.mcp() as client:
            data = client.call_tool("baas_search_knowledge", {"query": "indicadores de agenda", "limit": 5})
        results = data.get("results") or []
        paths = [r.get("path") for r in results]
        check(results, f"sin resultados: {data}")
        check(ANALISTA_DOC in paths, f"{ANALISTA_DOC} no está entre los resultados: {paths}")
        check(DENIED_DOC not in paths, f"la búsqueda devuelve un documento no autorizado: {DENIED_DOC}")
        return f"{len(results)} resultado(s): {', '.join(map(str, paths))}"

    # 4
    def read(self) -> str:
        with self.mcp() as client:
            data = client.call_tool("baas_read_document", {"path": ANALISTA_DOC})
        content = str(data.get("content") or "")
        check("Indicadores de agenda" in content, f"contenido inesperado: {content[:120]!r}")
        actual = hashlib.sha256(content.encode()).hexdigest()
        check(data.get("sha256") == actual, f"sha256 declarado {data.get('sha256')} ≠ calculado {actual}")
        return f"{data.get('path')} ({len(content)} caracteres, sha256 {actual[:12]}… verificado)"

    # 5
    def denied(self) -> str:
        try:
            with self.mcp() as client:
                data = client.call_tool("baas_read_document", {"path": DENIED_DOC})
        except McpToolError as exc:
            check(exc.code == "AUTHORIZATION_DENIED", f"error {exc.code} en lugar de AUTHORIZATION_DENIED")
            roles = exc.payload.get("required_roles") or []
            check("revisor" in roles, f"required_roles sin «revisor»: {exc.payload}")
            return f"AUTHORIZATION_DENIED · resource {exc.payload.get('resource')} · required_roles {roles}"
        raise StepError(f"la lectura no se denegó (devolvió {list(data)})")

    # 6
    def script(self) -> str:
        with self.mcp() as client:
            data = client.call_tool("baas_get_script", {"skill": "analisis-de-citas", "script": "resumen_citas.py"})
        content, declared = str(data.get("content") or ""), str(data.get("sha256") or "")
        check(data.get("path") == SCRIPT_PATH, f"path {data.get('path')} ≠ {SCRIPT_PATH}")
        actual = hashlib.sha256(content.encode()).hexdigest()
        check(declared == actual, f"sha256 declarado {declared} ≠ calculado {actual}")
        (self.project / "citas.csv").write_text(CSV)
        common = ["--project", str(self.project), "--cwd", str(self.project)]
        r = self.baas("run-script", SCRIPT_PATH, "--sha256", declared, *common, "--", "citas.csv")
        check(r.returncode == 0, f"run-script salió con {r.returncode}: {(r.stdout + r.stderr).strip()}")
        check(CSV_EXPECTED in r.stdout, f"salida inesperada: {r.stdout.strip()!r}")
        bad = self.baas("run-script", SCRIPT_PATH, "--sha256", "0" * 64, *common, "--", "citas.csv")
        check(bad.returncode == 9, f"con un sha256 falso, run-script salió con {bad.returncode} (se esperaba 9)")
        check(CSV_EXPECTED not in bad.stdout, "con un sha256 falso el script se ejecutó")
        leftovers = list(self.tmp.glob("baas-script-*"))
        check(not leftovers, f"quedan directorios temporales: {leftovers}")
        return (
            f"sha256 {actual[:12]}… verificado · salida «{r.stdout.strip()}»"
            " · sha256 falso → código 9 · temporal borrado"
        )

    # 7b
    def proxy(self) -> str:
        settings = json.loads((self.project / ".claude" / "settings.local.json").read_text())
        env = settings["env"]
        base = env["ANTHROPIC_BASE_URL"].rstrip("/")
        headers = {
            **parse_custom_headers(env["ANTHROPIC_CUSTOM_HEADERS"]),
            "x-api-key": FAKE_PROVIDER_KEY,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": "claude-fake",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": f"Prueba e2e H2 {self.work.name}"}],
        }
        without_key = {k: v for k, v in headers.items() if k != "X-Baas-Api-Key"}
        unauth = http("POST", f"{base}/v1/messages", headers=without_key, payload=payload)
        err = (unauth.json() or {}).get("error") or {}
        check(unauth.status == 401, f"sin X-Baas-Api-Key el proxy respondió {unauth.status} (se esperaba 401)")
        check(err.get("type") == "authentication_error", f"error sin cabecera: {unauth.body[:200]!r}")
        self.proxy_sent_at = datetime.now(timezone.utc)
        res = http("POST", f"{base}/v1/messages", headers=headers, payload=payload)
        body = res.json() or {}
        check(res.status == 200, f"POST {base}/v1/messages → HTTP {res.status}: {res.body[:300]!r}")
        text = "".join(c.get("text", "") for c in body.get("content") or [] if isinstance(c, dict))
        check(text, f"respuesta sin texto: {body}")
        return f"POST {base}/v1/messages → 200 «{text}» · sin X-Baas-Api-Key → 401 authentication_error"

    # 7a y 8
    def login(self) -> str:
        base = self.args.app_url
        hdrs = {"X-Requested-With": "baas-webapp"}
        res = http("POST", f"{base}/api/v1/auth/login", headers=hdrs,
                   payload={"email": self.args.email, "password": self.args.password})  # fmt: skip
        check(res.status == 200, f"login → HTTP {res.status}: {res.body[:200]!r}")
        cookies = cookies_from(res.headers)
        if "baas_session" in cookies:  # BAAS_DEV_SKIP_TOTP=1
            self.session_cookie = cookies["baas_session"]
            self.baseline = {c["id"]: c.get("turn_count") or 0 for c in self.conversations()}
            return "sesión iniciada solo con contraseña (BAAS_DEV_SKIP_TOTP=1)"
        check("baas_mfa" in cookies, f"login sin cookie de MFA ni de sesión: {res.json()}")
        for attempt in range(2):
            code = totp(self.args.totp_secret)
            r = http("POST", f"{base}/api/v1/auth/totp", headers={**hdrs, "Cookie": f"baas_mfa={cookies['baas_mfa']}"},
                     payload={"code": code})  # fmt: skip
            session = cookies_from(r.headers).get("baas_session")
            if r.status == 200 and session:
                self.session_cookie = session
                self.baseline = {c["id"]: c.get("turn_count") or 0 for c in self.conversations()}
                return f"sesión iniciada con contraseña y TOTP · {len(self.baseline)} conversación(es) previas"
            if attempt == 0:  # el código ya se usó en esta ventana: se espera a la siguiente
                time.sleep(31 - time.time() % 30)
        raise StepError(f"TOTP rechazado: HTTP {r.status} {r.body[:200]!r}")

    def conversations(self) -> list[dict[str, Any]]:
        url = f"{self.args.app_url}/api/v1/conversations?org={self.args.org}&limit=200"
        res = http("GET", url, headers={"Cookie": f"baas_session={self.session_cookie}"})
        check(res.status == 200, f"GET /api/v1/conversations → HTTP {res.status}: {res.body[:200]!r}")
        return (res.json() or {}).get("items") or []

    def trace(self) -> str:
        check(self.proxy_sent_at is not None, "no se envió la petición por el proxy")
        deadline = time.monotonic() + self.args.trace_timeout
        while True:
            seen = self.conversations()
            item = new_trace(self.baseline, seen, email=self.args.email, provider="anthropic")
            if item is not None:
                waited = datetime.now(timezone.utc) - self.proxy_sent_at
                return (
                    f"conversación {item['id']} · {item.get('api_flavor')} · modelo {item.get('model')}"
                    f" · cliente {item.get('client_tool')} · turnos {item.get('turn_count')}"
                    f" · tokens {item.get('tokens_in')}/{item.get('tokens_out')}"
                    f" · visible {waited.total_seconds():.2f} s después de la petición"
                )
            if time.monotonic() > deadline:
                raise StepError(f"sin traza tras {self.args.trace_timeout} s; conversaciones visibles: {len(seen)}")
            time.sleep(0.25)

    def run(self, runner: Runner) -> None:
        init = ["1. /baas-init sin interacción"]
        runner.step("0. webapp accesible", self.webapp_up)
        runner.step(init[0], self.init, needs=["0. webapp accesible"])
        runner.step("1b. MCP `baas` registrado sin la clave", self.mcp_registered, needs=init)
        runner.step("2. /baas-status", self.status, needs=init)
        runner.step("3. baas_search_knowledge (analista)", self.search, needs=init)
        runner.step("4. baas_read_document (analista)", self.read, needs=init)
        runner.step("5. lectura denegada (criterio 6)", self.denied, needs=init)
        runner.step("6. baas_get_script + run-script con sha256", self.script, needs=init)
        # La sesión se abre antes de la petición (el TOTP puede esperar a la siguiente ventana de 30 s)
        # para que la espera de la traza mida solo la propagación proxy → webapp.
        runner.step("7a. sesión en la webapp", self.login, needs=["0. webapp accesible"])
        runner.step("7b. petición de Anthropic por el proxy", self.proxy, needs=init)
        runner.step(
            "8. traza en GET /api/v1/conversations",
            self.trace,
            needs=["7a. sesión en la webapp", "7b. petición de Anthropic por el proxy"],
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--app-url", default=os.environ.get("BAAS_E2E_APP_URL", "http://app.localhost:8080"),
                   help="URL pública de la webapp (las del MCP y del proxy salen del handshake)")  # fmt: skip
    p.add_argument("--email", default="analista@demo.test", help="Usuario de desarrollo (contrato §8)")
    p.add_argument("--password", default=DEV_PASSWORD)
    p.add_argument("--totp-secret", default=DEV_TOTP_SECRET)
    p.add_argument("--org", default="demo")
    p.add_argument("--no-claude", action="store_true", help="No registrar el MCP con el CLI `claude`")
    p.add_argument("--trace-timeout", type=float, default=30, help="Segundos de espera de la traza")
    p.add_argument("--keep", action="store_true", help="Conservar el HOME y el proyecto temporales")
    p.add_argument("--json", type=Path, help="Escribe aquí el informe en JSON")
    ns = p.parse_args(argv)
    ns.app_url = ns.app_url.rstrip("/")
    return ns


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runner = Runner()
    e2e = E2E(args)
    print(f"Prueba de punta a punta H2 · webapp {args.app_url} · usuario {args.email} · organización {args.org}")
    try:
        e2e.run(runner)
    finally:
        e2e.cleanup()
    report = {**runner.report(), "app_url": args.app_url, "timings": e2e.timings}
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    failed = [r.name for r in runner.results if r.status != "OK"]
    print("RESULTADO:", "todo OK" if not failed else f"{len(failed)} paso(s) sin éxito: {', '.join(failed)}")
    return runner.exit_code()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
    sys.exit(130)
