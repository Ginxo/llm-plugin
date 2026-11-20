"""Grupo «consumo» (pasos 16.0–16.4) de `dev/e2e_h4.py`: el informe de consumo de la plataforma (Fase 2, entrega 1)
contra la pila real.

Un paso por criterio de aceptación del contrato (`contrato-fase-2-consumo.md`, §9; los pasos 16.5–16.8 son de la
entrega 2):

  16.0  la pila tiene el consumo: `GET /api/v1/platform/usage?month=<mes en curso UTC>` como PLATFORM_ADMIN da `200` con
        `period` y `data_until_by_source`. Sin ellos (la pila no incluye T2–T3) falla con ese mensaje y los demás
        pasos quedan saltados.
  16.1  turnos por modo: turnos reales por el proxy en *bypass* (AI-IP apagado), observación (AI-IP sin anonimizador) y
        anonimizado; el informe de la organización cuenta exactamente los enviados por modo, `anonymizer.turns` es
        `requests`, y las sustituciones y la verificación de L1 cuadran con lo que la webapp dice de cada turno.
  16.2  disco: con una foto nueva, `disk.last` es lo de las cinco categorías facturables de la foto de W15 (sin el
        índice), `disk.index_last` es su índice aparte y `disk.last + disk.index_last` es su `total_bytes`; con otra
        foto tras crecer, la media recoge las anteriores y el máximo es la última.
  16.3  el CSV es el JSON: una fila por organización, sin totales, las 46 columnas del contrato §7.9 y los mismos
        valores (un nombre que empieza por `=` sale con apóstrofo), para un rango y para un mes.
  16.4  permisos: `403` a un ORG_ADMIN y a un USER (y a un USER de otra organización) en `GET /platform/usage`,
        en JSON y en CSV, también con parámetros no válidos (los permisos van antes que la validación); `401` sin
        sesión.

Cada paso con datos crea su organización (`consumo-<ejecución>-<paso>`, con usuarios de la semilla de desarrollo como
miembros) y la elimina al terminar: se reutilizan los ayudantes del grupo `uso` (`e2e_uso.UsoGroup`). Ningún paso
mira dentro de los contenedores (`--inspect none` basta).
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import e2e_h3 as h3
import e2e_uso
from e2e_h2 import Http, StepError, check

if TYPE_CHECKING:
    from e2e_h2 import Runner
    from e2e_h4 import E2EH4

MODES = ("ANONYMIZED", "OBSERVATION", "BYPASS")
BILLABLE = ("skills", "scripts", "schemas", "knowledge", "versions")  # lo que se factura: sin el índice (contrato §6.2)
VERIFIED = ("clean", "warning", "error")
NEWER_SNAPSHOT = "no son de la misma foto"
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
REASON = "Prueba de punta a punta del grupo consumo (F2)"
PLATFORM = "/api/v1/platform/usage"
CSV_SETTLE = 10.0  # s que se espera a que el JSON y el CSV (dos peticiones) hablen del mismo instante
# Contrato §7.9, `GET /platform/usage`: columnas fijas, en este orden
PLATFORM_CSV_COLUMNS = (
    "slug", "name", "timezone", "requests", "blocked_requests", "errors", "tokens_in", "tokens_out", "bytes_in",
    "bytes_out", "active_users", "invocations", "storage_bytes", "storage_taken_at", "synced", "skill_read",
    "script_downloaded", "knowledge_read", "knowledge_search", "skills", "skills_capped", "areas", "areas_capped",
    "disk_avg_bytes", "disk_max_bytes", "disk_index_last_bytes", "disk_snapshots", "disk_first_taken_at",
    "anonymized_turns", "observation_turns", "bypass_turns", "replaced_in", "restored_out", "verified_clean",
    "verified_warning", "verified_error", "l2_turns", "l2_enabled", "l2_applied", "l2_degraded",
    "l2_observed_turns", "l2_known_values_only", "l2_analyzed_bytes", "l2_cached_bytes", "l2_unanalyzed_bytes",
    "l2_observed",
)  # fmt: skip
_CSV_CONTENT = ("synced", "skill_read", "script_downloaded", "knowledge_read", "knowledge_search", "skills",
                "skills_capped", "areas", "areas_capped")  # fmt: skip
_CSV_DISK = {"disk_avg_bytes": "avg", "disk_max_bytes": "max", "disk_index_last_bytes": "index_last",
             "disk_snapshots": "snapshots", "disk_first_taken_at": "first_taken_at"}  # fmt: skip
_CSV_MODES = {"anonymized_turns": "ANONYMIZED", "observation_turns": "OBSERVATION", "bypass_turns": "BYPASS"}
_CSV_TOP = ("slug", "name", "timezone", "requests", "blocked_requests", "errors", "tokens_in", "tokens_out",
            "bytes_in", "bytes_out", "active_users", "invocations", "storage_bytes", "storage_taken_at")  # fmt: skip


# --- utilidades puras ----------------------------------------------------------------------------


def current_month(now: datetime | None = None) -> str:
    """El mes natural en curso en UTC (`YYYY-MM`), el valor por defecto de `GET /platform/usage` (contrato §6.1)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def utc_range(now: datetime | None = None) -> dict[str, str]:
    """`from`/`to` UTC que cubren desde **ayer** hasta hoy: un dato de las 23:59:5x consultado a las 00:00:0x sigue
    dentro (las organizaciones de cada paso se crean al empezar, sin histórico)."""
    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    return {"from": (today - timedelta(days=1)).isoformat(), "to": today.isoformat()}


def mode_of(ai_ip: Any, anonymizer: Any) -> str:
    """El modo con el que sale un turno (contrato §5.1): sin AI-IP, bypass, tenga lo que tenga el anonimizador."""
    if not ai_ip:
        return "BYPASS"
    return "ANONYMIZED" if anonymizer else "OBSERVATION"


def replacements_total(by_type: Any) -> int:
    """Suma de los valores enteros de un `*_replacements_by_type` (contrato §5.3); lo que no es un entero no cuenta."""
    if not isinstance(by_type, dict):
        return 0
    return sum(v for v in by_type.values() if isinstance(v, int) and not isinstance(v, bool))


def consumo_problem(status: int, data: Any) -> str | None:
    """16.0: qué decir si `GET /platform/usage?month=…` (como PLATFORM_ADMIN) no trae el informe de consumo."""
    if status == 200:
        missing = [
            k
            for k in ("period", "data_until_by_source")
            if not (isinstance(data, dict) and isinstance(data.get(k), dict))
        ]
        if not missing:
            return None
        return (
            "pila sin el informe de consumo de la Fase 2 (el informe trae `period` y `data_until_by_source`; "
            f"esta respuesta no trae {', '.join(missing)}): reconstruye con la webapp de T2–T3 (agregado del "
            "anonimizador e informe de consumo), el mcp-server de T4 y deploy"
        )
    return f"GET …/platform/usage → HTTP {status} (se esperaba 200): ¿la pila tiene el consumo de la Fase 2 (T2–T3)?"


@dataclass
class Sent:
    """Un turno de 16.1: el modo que el paso pidió, los tokens que el proveedor declaró y, tras consumirse la traza, lo
    que la webapp dice del turno (el modo con el que salió y su verificación)."""

    mode: str
    tokens_in: int
    tokens_out: int
    state: str | None = None
    verification: Any = None
    model: str = ""


def expected_figures(sent: Sequence[Sent]) -> dict[str, Any]:
    """Lo que el informe de la organización debe decir de esos turnos (contrato §5.2): turnos por modo, tokens, las
    sustituciones de L1 (ida y vuelta) y cuántos turnos salieron `CLEAN`, `WARNING` y `ERROR`."""
    verified = dict.fromkeys(VERIFIED, 0)
    replaced = restored = 0
    for turn in sent:
        if not isinstance(turn.verification, dict):
            continue
        replaced += replacements_total(turn.verification.get("request_replacements_by_type"))
        restored += replacements_total(turn.verification.get("response_replacements_by_type"))
        status = str(turn.verification.get("status") or "").lower()
        if status in verified:
            verified[status] += 1
    return {
        "requests": len(sent),
        "turns_by_mode": {m: sum(1 for t in sent if t.mode == m) for m in MODES},
        "tokens_in": sum(t.tokens_in for t in sent),
        "tokens_out": sum(t.tokens_out for t in sent),
        "replaced_in": replaced,
        "restored_out": restored,
        "verified": verified,
    }


def state_problems(sent: Sequence[Sent]) -> list[str]:
    """Precondición de 16.1: cada turno salió en el modo que se pidió (si no, el interruptor no llegó al proxy y la
    comparación con el informe no diría nada del consumo)."""
    return [
        f"el turno {i} ({t.model}) pidió {t.mode} y la webapp dice que salió {t.state}: "
        "el interruptor no llegó al proxy"
        for i, t in enumerate(sent, 1)
        if t.state != t.mode
    ]


def mode_problems(row: dict[str, Any], want: dict[str, Any]) -> list[str]:
    """Diferencias entre la fila de la organización en `GET /platform/usage` y `want` (`expected_figures`), más las
    invariantes del contrato §6.5: `turns = Σ turns_by_mode = requests`, el anonimizador cuelga del tráfico (mismos
    tokens y bytes) y los verificados no superan a los turnos."""
    problems: list[str] = []
    anon = row.get("anonymizer") if isinstance(row.get("anonymizer"), dict) else {}

    def same(name: str, got: Any, expected: Any) -> None:
        if got != expected or type(got) is not type(expected):
            problems.append(f"{name} = {got!r} (se esperaba {expected!r})")

    for name in ("requests", "tokens_in", "tokens_out"):
        same(name, row.get(name), want[name])
    modes = anon.get("turns_by_mode")
    if not isinstance(modes, dict) or set(modes) != set(MODES):
        problems.append(f"anonymizer.turns_by_mode no trae siempre {', '.join(MODES)}: {modes!r}")
        modes = {}
    for mode in MODES:
        if mode in modes:
            same(f"anonymizer.turns_by_mode.{mode}", modes[mode], want["turns_by_mode"][mode])
    for name in ("replaced_in", "restored_out"):
        same(f"anonymizer.{name}", anon.get(name), want[name])
    for status in VERIFIED:
        same(f"anonymizer.verified_{status}", anon.get(f"verified_{status}"), want["verified"][status])
    turns = anon.get("turns")
    if modes and turns != sum(modes.values()):
        problems.append(f"anonymizer.turns = {turns!r} ≠ Σ turns_by_mode = {sum(modes.values())}")
    if turns != row.get("requests"):
        problems.append(f"anonymizer.turns = {turns!r} ≠ requests = {row.get('requests')!r}")
    for name in ("tokens_in", "tokens_out", "bytes_in"):
        if anon.get(name) != row.get(name):
            problems.append(f"anonymizer.{name} = {anon.get(name)!r} ≠ {name} = {row.get(name)!r} (el mismo tráfico)")
    verified = sum(anon.get(f"verified_{s}") or 0 for s in VERIFIED)
    if isinstance(turns, int) and verified > turns:
        problems.append(
            f"se esperaba verified_clean + verified_warning + verified_error ≤ turns ({verified} > {turns})"
        )
    return problems


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def billable_problems(row: dict[str, Any], latest: Any) -> list[str]:
    """16.2: el bloque `disk` de la fila de `GET /platform/usage` frente a `latest` del informe de disco de la
    organización (Fase 4 §8.5), que son las **dos** lecturas de la misma foto: `disk.last` es la suma de las cinco
    categorías facturables (sin el índice), `disk.index_last` es el índice y los dos suman `latest.total_bytes`. Si las
    dos lecturas no son de la misma foto, lo dice con `NEWER_SNAPSHOT` (una foto nueva llegó entre las dos: se vuelve
    a leer; no es un valor equivocado)."""
    disk = row.get("disk")
    if not isinstance(disk, dict) or not isinstance(latest, dict) or not isinstance(latest.get("bytes"), dict):
        return [f"sin bloque disk en el informe o sin latest.bytes en el de la organización: {disk!r}, {latest!r}"]
    problems = [
        f"disk.{key} = {disk.get(key)!r} (se esperaba un entero)"
        for key in ("avg", "max", "last", "index_last", "snapshots")
        if not _int(disk.get(key))
    ]
    if problems:
        return problems
    if disk.get("taken_at") != latest.get("taken_at"):
        return [
            f"las dos lecturas {NEWER_SNAPSHOT}: disk.taken_at = {disk.get('taken_at')}, "
            f"latest.taken_at = {latest.get('taken_at')}"
        ]
    data = latest["bytes"]
    billable = sum(data.get(c) or 0 for c in BILLABLE)
    if disk["snapshots"] < 1:
        problems.append(f"disk.snapshots = {disk['snapshots']} (la foto de latest cuenta: se esperaba ≥ 1)")
    if disk["last"] != billable:
        problems.append(
            f"disk.last = {disk['last']}, las cinco categorías de latest.bytes (sin el índice) suman {billable}"
        )
    if disk["index_last"] != data.get("index"):
        problems.append(f"disk.index_last = {disk['index_last']}, latest.bytes.index = {data.get('index')}")
    if disk["last"] + disk["index_last"] != latest.get("total_bytes"):
        problems.append(
            f"disk.last + disk.index_last = {disk['last'] + disk['index_last']}, "
            f"latest.total_bytes = {latest.get('total_bytes')} (difieren exactamente en el índice)"
        )
    if disk["avg"] > disk["max"]:
        problems.append(f"disk.avg = {disk['avg']} > disk.max = {disk['max']}")
    if disk["last"] > disk["max"]:
        problems.append(f"disk.last = {disk['last']} > disk.max = {disk['max']}")
    if row.get("storage_bytes") != disk["last"]:
        problems.append(
            f"storage_bytes = {row.get('storage_bytes')!r} "
            f"(se esperaba disk.last = {disk['last']}: ya no suma el índice)"
        )
    if row.get("storage_taken_at") != disk.get("taken_at"):
        problems.append(f"storage_taken_at = {row.get('storage_taken_at')!r} (se esperaba disk.taken_at)")
    first = disk.get("first_taken_at")
    if not isinstance(first, str) or not isinstance(disk.get("taken_at"), str) or first > disk["taken_at"]:
        problems.append(f"disk.first_taken_at = {first!r} (se esperaba un instante no posterior a disk.taken_at)")
    return problems


def published_problems(row: dict[str, Any], *, skills: int, areas: int) -> list[str]:
    """16.2: las skills y las áreas publicadas de la última foto (contrato §6.3): las que se escribieron, sin tope."""
    content = row.get("content") if isinstance(row.get("content"), dict) else {}
    problems = []
    for name, want in (("skills", skills), ("areas", areas)):
        if content.get(name) != want:
            problems.append(f"content.{name} = {content.get(name)!r} (se esperaba {want})")
        if content.get(f"{name}_capped") is not False:
            problems.append(f"content.{name}_capped = {content.get(f'{name}_capped')!r} (se esperaba false)")
    return problems


def growth_problems(row: dict[str, Any], *, first: int) -> list[str]:
    """16.2: con una segunda foto mayor que la primera (`first` bytes facturables), la última es el máximo y la media
    recoge las fotos de antes (es menor que el máximo)."""
    disk = row.get("disk") if isinstance(row.get("disk"), dict) else {}
    problems = []
    if not _int(disk.get("snapshots")) or disk["snapshots"] < 2:
        problems.append(f"disk.snapshots = {disk.get('snapshots')!r} (con dos fotos se esperaba ≥ 2)")
    if disk.get("max") != disk.get("last"):
        problems.append(f"disk.max = {disk.get('max')!r} ≠ disk.last = {disk.get('last')!r} (el disco solo creció)")
    if _int(disk.get("avg")) and _int(disk.get("max")) and disk["avg"] >= disk["max"]:
        problems.append(f"disk.avg = {disk['avg']} ≥ disk.max = {disk['max']}: la media no recoge las fotos de antes")
    if _int(disk.get("last")) and disk["last"] <= first:
        problems.append(f"disk.last = {disk['last']} no supera los {first} B de la primera foto")
    return problems


def csv_cell(value: Any) -> str:
    """Una celda del CSV (contrato §7.9 y Fase 4 §8.8): `null` vacío, los booleanos en minúsculas y, en un texto que
    empieza por `=`, `+`, `-`, `@`, tabulador o retorno, un apóstrofo delante (no se interpreta como fórmula)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return str(value)


def csv_expected(org: dict[str, Any]) -> list[str]:
    """La fila del CSV de una organización del JSON, en el orden de `PLATFORM_CSV_COLUMNS`."""
    content, disk, anon = org.get("content") or {}, org.get("disk") or {}, org.get("anonymizer") or {}
    cells = []
    for column in PLATFORM_CSV_COLUMNS:
        if column in _CSV_TOP:
            value = org.get(column)
        elif column in _CSV_CONTENT:
            value = content.get(column)
        elif column in _CSV_DISK:
            value = disk.get(_CSV_DISK[column])
        elif column in _CSV_MODES:
            value = (anon.get("turns_by_mode") or {}).get(_CSV_MODES[column])
        else:
            value = anon.get(column)
        cells.append(csv_cell(value))
    return cells


def csv_problems(text: str, orgs: Sequence[dict[str, Any]]) -> list[str]:
    """16.3: el CSV frente a las organizaciones del JSON: BOM y `\\r\\n`, la cabecera exacta, una fila por organización
    (en su orden) sin totales y cada celda igual al valor del JSON."""
    problems = []
    if not text.startswith("﻿"):
        problems.append("el CSV no empieza por el BOM (U+FEFF)")
    body = text.removeprefix("﻿")
    if "\n" in body.replace("\r\n", ""):
        problems.append("el CSV tiene saltos de línea que no son \\r\\n")
    rows = list(csv.reader(io.StringIO(body, newline="")))
    columns = list(PLATFORM_CSV_COLUMNS)
    if not rows or rows[0] != columns:
        got = rows[0] if rows else []
        extra, missing = [c for c in got if c not in columns], [c for c in columns if c not in got]
        problems.append(
            f"cabecera distinta de la del contrato §7.9 ({len(got)} columnas; sobran {extra}, faltan {missing})"
        )
        return problems
    data = rows[1:]
    if len(data) != len(orgs):
        problems.append(
            f"{len(data)} fila(s) para {len(orgs)} organización(es): una por organización y sin fila de totales "
            f"(slugs del CSV: {[r[0] for r in data][:8]})"
        )
    for got, org in zip(data, orgs, strict=False):
        want = csv_expected(org)
        if len(got) != len(columns):
            problems.append(f"«{org.get('slug')}»: {len(got)} celdas (se esperaban {len(columns)})")
            continue
        problems += [
            f"«{org.get('slug')}»: {column} = {cell!r} en el CSV y {expected!r} en el JSON"
            for column, cell, expected in zip(columns, got, want, strict=True)
            if cell != expected
        ]
    return problems


def csv_filename(period: dict[str, str]) -> str:
    return f"consumo-plataforma-{period['from']}-{period['to']}.csv"


def disposition_problems(header: str | None, filename: str) -> list[str]:
    if header != f'attachment; filename="{filename}"':
        return [f'Content-Disposition = {header!r} (se esperaba attachment; filename="{filename}")']
    return []


def denied_problems(status: int, data: Any, want: int) -> list[str]:
    """16.4: la respuesta es `want` (`403 FORBIDDEN` o `401 UNAUTHENTICATED`) y no lleva ningún dato del informe."""
    error = {401: "UNAUTHENTICATED", 403: "FORBIDDEN"}[want]
    problems = []
    if status != want:
        problems.append(f"HTTP {status} (se esperaba {want})")
    elif (data.get("error") if isinstance(data, dict) else None) != error:
        problems.append(f"error = {data.get('error') if isinstance(data, dict) else data!r} (se esperaba {error})")
    if isinstance(data, dict) and {"orgs", "totals", "period"} & data.keys():
        problems.append(f"la respuesta lleva datos del informe ({sorted({'orgs', 'totals', 'period'} & data.keys())})")
    return problems


# --- la prueba ------------------------------------------------------------------------------------


class ConsumoGroup:
    def __init__(self, e2e: E2EH4) -> None:
        self.e = e2e
        self.a = e2e.args
        self.nonce = e2e.nonce
        self.uso = e2e_uso.UsoGroup(e2e)  # las organizaciones de cada paso, los turnos y las esperas
        self.uso.slug_prefix = "consumo"

    # --- quién -------------------------------------------------------------------------------------
    @property
    def admin(self) -> str:
        return str(self.a.admin_email)

    @property
    def org_admin(self) -> str:
        return str(self.a.orgadmin_email)

    @property
    def user(self) -> str:
        return str(self.a.bypass_email)

    @property
    def other_user(self) -> str:
        return str(self.a.anon_email)  # USER de otra organización (la semilla trae «privada»)

    def logins(self) -> list[str]:
        return [self.admin, self.org_admin, self.user, self.other_user]

    # --- el informe --------------------------------------------------------------------------------
    def platform(self, who: str | None, params: dict[str, Any] | None = None) -> Http:
        query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
        path = PLATFORM + (f"?{query}" if query else "")
        if who is None:
            return h3.http("GET", f"{self.a.app_url}{path}", timeout=self.a.timeout)
        return self.e.webapp("GET", path, who)

    def report(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        res = self.platform(self.admin, params if params is not None else utc_range())
        data = res.json()
        check(
            res.status == 200 and isinstance(data, dict) and isinstance(data.get("orgs"), list),
            f"GET {PLATFORM}?{urlencode(params or utc_range())} ({self.admin}) → {e2e_uso.describe(res)}",
        )
        return data

    def row(self, slug: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """La fila de `slug` en el informe (todas las organizaciones salen, con ceros)."""
        rows = {o.get("slug"): o for o in self.report(params)["orgs"] if isinstance(o, dict)}
        check(slug in rows, f"«{slug}» no sale en el informe de consumo: {sorted(map(str, rows))}")
        return rows[slug]

    # --- 16.0 la pila tiene el consumo ------------------------------------------------------------------
    def ready(self) -> str:
        month = current_month()
        res = self.platform(self.admin, {"month": month})
        data = res.json()
        problem = consumo_problem(res.status, data)
        check(problem is None, f"{problem}: {h3.snippet(res.body, 200)}")
        period, sources = data["period"], data["data_until_by_source"]
        return (
            f"GET {PLATFORM}?month={month} → 200: period {{month: {period.get('month')}, open: {period.get('open')}}}; "
            f"datos hasta por tubería {sources}"
        )

    # --- 16.1 turnos por modo -------------------------------------------------------------------------
    def org_state(self, org: e2e_uso.UsoOrg) -> dict[str, Any]:
        res = self.e.webapp("GET", f"/api/v1/orgs/{org.slug}", self.admin)
        check(res.status == 200, f"GET /api/v1/orgs/{org.slug} → {e2e_uso.describe(res)}")
        body = res.json() or {}
        return body["organization"] if isinstance(body.get("organization"), dict) else body

    def set_mode(self, org: e2e_uso.UsoOrg, mode: str) -> None:
        """Pone la organización en `mode` (BYPASS = AI-IP apagado; OBSERVATION = AI-IP con el anonimizador apagado;
        ANONYMIZED = los dos) como PLATFORM_ADMIN y espera a que llegue al proxy. Una conversación nueva sale con lo que
        el proxy tenga entonces (las que ya están abiertas solo suben, H4-08)."""
        want_ai_ip, want_anonymizer = mode != "BYPASS", mode == "ANONYMIZED"
        changed = False
        if self.org_state(org).get("ai_ip_enabled") is not want_ai_ip:
            self.toggle(org, "ai-ip", want_ai_ip)
            changed = True
        if want_ai_ip and self.org_state(org).get("anonymizer_enabled") is not want_anonymizer:
            self.toggle(org, "anonymizer", want_anonymizer)
            changed = True
        if changed:
            time.sleep(self.a.propagation_wait)  # el cambio llega al proxy por Pub/Sub, sin esperar respuesta

    def toggle(self, org: e2e_uso.UsoOrg, what: str, enabled: bool) -> None:
        res = self.e.admin_post(f"/api/v1/orgs/{org.slug}/{what}", {"enabled": enabled, "reason": REASON}, method="PUT")
        check(
            res.status == 200, f"PUT …/{org.slug}/{what} {{enabled: {str(enabled).lower()}}} → {e2e_uso.describe(res)}"
        )

    def read_turns(self, org: e2e_uso.UsoOrg, sent: Sequence[Sent]) -> None:
        """Completa `sent` con lo que la webapp dice de cada turno (como su dueño): con qué modo salió y su
        verificación."""
        convs = self.uso.conversations(org)
        for turn in sent:
            conv = convs.get(turn.model)
            check(conv is not None, f"la conversación {turn.model} no aparece en la organización «{org.slug}»")
            indexes = self.e.turn_indexes(self.user, str(conv["id"]))  # type: ignore[index]
            res = self.e.turn(self.user, str(conv["id"]), indexes[-1])  # type: ignore[index]
            check(res.status == 200, f"GET …/{conv['id']}/turns/{indexes[-1]} ({self.user}) → {e2e_uso.describe(res)}")  # type: ignore[index]
            data = res.json() or {}
            turn.state = mode_of(data.get("ai_ip_enabled"), data.get("anonymizer_enabled"))
            turn.verification = data.get("verification")

    def modes(self) -> str:
        cases = [
            (api, stream)
            for api, stream in (("messages", False), ("messages", True), ("chat", False), ("responses", False))
            if api in self.a.apis
        ]
        check(cases, "--apis no deja ninguna API que probar")
        plan = (("BYPASS", 1), ("OBSERVATION", 2), ("ANONYMIZED", 3))  # una cifra distinta por modo
        with self.uso.org("modos", {self.org_admin: "ORG_ADMIN", self.user: "USER"}) as org:
            sent: list[Sent] = []
            for mode, count in plan:
                self.set_mode(org, mode)
                for i in range(count):
                    api, stream = cases[len(sent) % len(cases)]
                    model = f"consumo-{self.nonce}-{mode.lower()}-{i}"
                    text = f"consumo {self.nonce} {mode.lower()} {i}. {h3.SENSITIVE_TEXT}."
                    _, res = self.uso.send(org, self.user, api, text, stream=stream, model=model)
                    tokens = e2e_uso.reply_tokens(api, res.body, stream=stream)
                    check(tokens, f"{h3.API_NAMES[api]}: la respuesta del proveedor no declara los tokens")
                    sent.append(Sent(mode, tokens[0], tokens[1], model=model))  # type: ignore[index]
            got: dict[str, Any] = {}

            def consumed() -> list[str]:
                got["row"] = self.row(org.slug)
                n = got["row"].get("requests")
                return [] if n == len(sent) else [f"requests = {n!r} (se esperaba {len(sent)}: trazas sin consumir)"]

            self.uso.wait(
                consumed, self.a.trace_timeout, f"el informe de «{org.slug}» no llega a {len(sent)} turnos", 0.5
            )
            self.read_turns(org, sent)
            want = expected_figures(sent)  # tras leer los turnos: de ellos salen las sustituciones y la verificación
            problems = state_problems(sent)
            check(not problems, "\n".join(problems))
            problems = mode_problems(got["row"], want)
            check(not problems, "\n".join(problems))
            anon = got["row"]["anonymizer"]
        by_mode = " · ".join(f"{m} {want['turns_by_mode'][m]}" for m in MODES)
        return (
            f"{len(sent)} turnos nuevos en «{org.slug}» ({by_mode}) = turns_by_mode del informe; anonymizer.turns = "
            f"requests = {len(sent)}; tokens {want['tokens_in']}/{want['tokens_out']}; L1: replaced_in "
            f"{anon['replaced_in']} = {want['replaced_in']}, restored_out {anon['restored_out']} = "
            f"{want['restored_out']}; "
            f"verificación {want['verified']}"
        )

    # --- 16.2 disco ------------------------------------------------------------------------------------
    def settle(
        self, org: e2e_uso.UsoOrg, written: datetime, what: str, *, above: int = -1
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Espera a una foto posterior a `written` que ya cuente lo escrito (la skill y el área, y más de `above` bytes
        facturables: una foto cortada antes de las escrituras no cuenta) y comprueba la fila del informe frente al
        `latest` de la organización. Una foto nueva entre las dos lecturas se vuelve a leer; una diferencia de valores
        de la misma foto, no."""
        got: dict[str, Any] = {}

        def compare() -> list[str]:
            latest = self.uso.report_json("storage", org.slug, self.org_admin, self.uso.today(org)).get("latest")
            taken = e2e_uso.parse_instant((latest or {}).get("taken_at"))
            if taken is None or taken <= written:
                return [f"sin foto posterior a las escrituras (latest.taken_at = {(latest or {}).get('taken_at')})"]
            row = self.row(org.slug)  # la de plataforma se lee después: puede ir por delante, no por detrás
            problems = billable_problems(row, latest)
            if problems and not (len(problems) == 1 and NEWER_SNAPSHOT in problems[0]):
                raise StepError(what + ":\n" + "\n".join(problems))
            problems += published_problems(row, skills=1, areas=1)
            if not problems and row["disk"]["last"] <= above:
                problems.append(f"disk.last = {row['disk']['last']} no supera los {above} B de la foto anterior")
            if not problems:
                got.update(latest=latest, row=row)
            return problems

        self.uso.wait(compare, self.a.usage_timeout, what, every=2)
        return got["latest"], got["row"]

    def disk(self) -> str:
        skill, area = f"disco-{self.nonce}", f"area-{self.nonce}"
        pad = "x" * 4000
        with self.uso.org("disco", {self.org_admin: "ORG_ADMIN"}) as org:
            self.uso.write_skill(
                org,
                skill,
                {"scripts/proceso.py": f"# {pad}\nprint(1)\n", "esquema.json": json.dumps({"relleno": pad}) + "\n"},
            )
            self.uso.write_doc(org, f"knowledge/{area}/doc.md", pad * 3)
            latest, row = self.settle(
                org, datetime.now(timezone.utc), "la foto de disco no llega o no cuadra con la de W15"
            )
            first = row["disk"]["last"]
            # el disco crece: otra foto, más grande. La media recoge la anterior y el máximo es la última
            self.uso.write_doc(org, f"knowledge/{area}/mas.md", pad * 8)
            what = "la foto de disco tras crecer no llega o no cuadra con la de W15"
            latest2, row2 = self.settle(org, datetime.now(timezone.utc), what, above=first)
            problems = growth_problems(row2, first=first)
            check(not problems, "\n".join(problems))
        disk = row2["disk"]
        return (
            f"foto {latest['taken_at']}: facturable {first} B + índice {latest['bytes']['index']} B (aparte) = "
            "total con "
            f"índice {latest['total_bytes']} B; skills 1, áreas 1 (sin tope). Tras crecer ({latest2['taken_at']}): "
            f"facturable {disk['last']} B = máximo, media {disk['avg']} B, {disk['snapshots']} fotos, índice "
            f"{disk['index_last']} B"
        )

    # --- 16.3 CSV = JSON -------------------------------------------------------------------------------
    def csv(self) -> str:
        name = f'=E2E consumo, "{self.nonce}"'  # empieza por «=» y lleva coma y comillas
        with self.uso.org("csv", {self.org_admin: "ORG_ADMIN", self.user: "USER"}, name=name) as org:
            for i in range(2):
                self.uso.send(org, self.user, "messages", f"csv {self.nonce} {i}. Hola.")
            self.uso.wait_requests(org, 2)
            details = []
            for params in (utc_range(), {"month": current_month()}):
                found: dict[str, Any] = {}

                def compare(params: dict[str, Any] = params, found: dict[str, Any] = found) -> list[str]:
                    data = self.report(params)
                    res = self.platform(self.admin, {**params, "format": "csv"})
                    if res.status != 200:
                        raise StepError(f"GET {PLATFORM}?format=csv → {e2e_uso.describe(res)}")
                    header = {k.lower(): v for k, v in res.headers}
                    text = res.body.decode("utf-8")
                    problems = []
                    if not str(header.get("content-type", "")).startswith("text/csv"):
                        problems.append(f"Content-Type = {header.get('content-type')!r} (se esperaba text/csv)")
                    problems += disposition_problems(header.get("content-disposition"), csv_filename(data["period"]))
                    problems += csv_problems(text, data["orgs"])
                    mine = next((o for o in data["orgs"] if o.get("slug") == org.slug), None)
                    if mine is None or mine.get("requests") != 2:
                        problems.append(
                            f"«{org.slug}» no sale con 2 peticiones en el JSON: {mine and mine.get('requests')}"
                        )
                    table = list(csv.reader(io.StringIO(text.removeprefix("\ufeff"), newline="")))
                    ours = next(
                        (dict(zip(table[0], r, strict=False)) for r in table[1:] if r and r[0] == org.slug), None
                    )
                    if ours is None or ours.get("name") != "'" + name:
                        problems.append(
                            f"el nombre «{name}» no sale con apóstrofo en el CSV: {ours and ours.get('name')!r}"
                        )
                    found.update(rows=len(data["orgs"]), name=ours and ours.get("name"))
                    return problems

                self.uso.wait(compare, CSV_SETTLE, f"el CSV no es el JSON ({params})", every=1)
                period = f"month={params['month']}" if "month" in params else f"from={params['from']}&to={params['to']}"
                details.append(f"{period}: {found['rows']} fila(s)")
        return (
            f"CSV = JSON (46 columnas, BOM, \\r\\n, una fila por organización y sin totales) con "
            f"{' y '.join(details)}; "
            f"el nombre {name!r} sale con apóstrofo ({found['name']!r})"
        )

    # --- 16.4 permisos ---------------------------------------------------------------------------------
    def permissions(self) -> str:
        other = self.other_user
        month = current_month()
        problems: list[str] = []
        for label, params in (
            ("JSON", {"month": month}),
            ("CSV", {"month": month, "format": "csv"}),
            ("month no válido", {"month": "2026-13"}),
            ("from y to no válidos", {"from": "2026-12-31", "to": "2026-01-01"}),
        ):
            for who, want in ((self.org_admin, 403), (self.user, 403), (other, 403), (None, 401)):
                res = self.platform(who, params)
                found = denied_problems(res.status, res.json(), want)
                problems += [f"{label} ({who or 'sin sesión'}): {p}" for p in found]
        control = self.platform(self.admin, {"month": month})
        if control.status != 200:
            problems.append(f"control: el PLATFORM_ADMIN recibe {e2e_uso.describe(control)} (se esperaba 200)")
        invalid = self.platform(self.admin, {"month": "2026-13"})
        field = (invalid.json() or {}).get("field") if isinstance(invalid.json(), dict) else None
        if (invalid.status, field) != (422, "month"):
            problems.append(
                f"control: month=2026-13 como PLATFORM_ADMIN → {e2e_uso.describe(invalid)} "
                "(se esperaba 422 con field month)"
            )
        check(not problems, "\n".join(problems))
        return (
            f"GET {PLATFORM}: ORG_ADMIN ({self.org_admin}), USER ({self.user}) y USER de otra organización ({other}) "
            f"→ 403 FORBIDDEN y sin sesión → 401, en JSON, en CSV y con parámetros no válidos (los "
            "permisos van antes que la validación: no se filtra ni un dato); el PLATFORM_ADMIN → 200 y 422 en month "
            "con un valor no válido"
        )

    # --- el recorrido ----------------------------------------------------------------------------------
    def register(self, runner: Runner, base: list[str]) -> None:
        step = runner.step
        ready = "16.0 la pila tiene el consumo (informe de plataforma con period y datos hasta por tubería)"
        step(ready, self.ready, needs=base)
        base = [*base, ready]
        step("16.1 turnos por modo: bypass, observación y anonimizado cuadran con lo enviado", self.modes, needs=base)
        step("16.2 disco: media, máximo y última; el índice aparte y no suma", self.disk, needs=base)
        step("16.3 el CSV es el JSON (una fila por organización, sin totales)", self.csv, needs=base)
        step("16.4 permisos: 403 al ORG_ADMIN y al USER, 401 sin sesión", self.permissions, needs=base)
