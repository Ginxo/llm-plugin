"""Grupo «uso» (12.x) de la prueba de punta a punta de H4 (`dev/e2e_uso.py` y el inspector de `dev/e2e_h4.py`).

Los pasos 12.1–12.10 se ejecutan contra la pila real (la pila simulada de `dev/fake_h4_stack.py` no simula la
observabilidad de uso de la Fase 4); aquí se prueban las utilidades puras con las que deciden y las consultas que el
inspector hace dentro de los contenedores (con un motor de contenedores falso que anota sus argumentos). Con
`unittest` (se ejecutan con `pytest` en la CI).
"""

from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev"))

import e2e_h4
import e2e_uso

ORG_ID = "5963754d-a8c6-4822-9b1b-17edadd88c99"
USER_ID = "562fcbf4-4d2d-45df-87fa-cc2b4730fc53"


class DiskTest(unittest.TestCase):
    """La clasificación del disco del contrato de la Fase 4, §4 (la misma que `OrgStore.usage()` del MCP)."""

    def test_each_file_falls_in_one_category_by_precedence(self) -> None:
        cases = {
            "index.sqlite": "index",
            "index.sqlite-wal": "index",
            "index.sqlite-shm": "index",
            ".versions/skills/a/SKILL.md/current.json": "versions",
            ".versions/knowledge/x.md/v1.md": "versions",
            "skills/a/scripts/calc.py": "scripts",
            "skills/a/scripts/datos.json": "scripts",  # bajo scripts/ gana a la extensión
            "skills/a/scripts/lib/util.py": "scripts",
            "skills/a/esquema.json": "schemas",
            "skills/a/baas.yaml": "schemas",
            "skills/_acl.yaml": "schemas",
            "knowledge/fin/_acl.yaml": "schemas",
            "knowledge/fin/consulta.SQL": "schemas",
            "knowledge/fin/tabla.xsd": "schemas",
            "knowledge/fin/x.yml": "schemas",
            "skills/a/SKILL.md": "skills",
            "skills/a/assets/scripts/x.py": "skills",  # solo el scripts/ directo de la skill
            "skills/scripts": "skills",
            "knowledge/fin/doc.md": "knowledge",
            "knowledge/raiz.md": "knowledge",
            "knowledge/scripts/x.py": "knowledge",
            "knowledge/.json": "knowledge",  # sin nombre no hay extensión
        }
        for path, category in cases.items():
            self.assertEqual(e2e_uso.disk_category(path), category, path)

    def test_what_is_outside_the_measurement(self) -> None:
        for path in (".uploads/u1/part", ".lock", ".tmp/x", "index.sqlite.bak", "otro.txt", "notas/skills/a.md",
                     "skills", "knowledge", ".quarantine/x"):  # fmt: skip
            self.assertIsNone(e2e_uso.disk_category(path), path)

    def test_expected_usage_sums_files_and_bytes_per_category(self) -> None:
        files = [("skills/a/SKILL.md", 10), ("skills/a/scripts/x.py", 20), ("skills/a/e.json", 30),
                 ("knowledge/f/d.md", 40), (".versions/a", 50), ("index.sqlite", 60), (".uploads/u", 999),
                 (".lock", 0)]  # fmt: skip
        got = e2e_uso.disk_expected(files)
        self.assertEqual(
            got,
            {"files": 6, "skills": 10, "scripts": 20, "schemas": 30, "knowledge": 40, "versions": 50, "index": 60},
        )
        self.assertEqual(e2e_uso.disk_total(got), 210)

    def test_problems_with_a_one_percent_margin(self) -> None:
        expected = {"files": 3, "skills": 1000, "scripts": 0, "schemas": 0, "knowledge": 0, "versions": 0, "index": 0}
        self.assertEqual(e2e_uso.disk_problems(expected, dict(expected)), [])
        self.assertEqual(e2e_uso.disk_problems(expected, {**expected, "skills": 1010}), [])
        problems = e2e_uso.disk_problems(expected, {**expected, "skills": 1011})
        self.assertTrue(any("skills" in p for p in problems), problems)
        self.assertTrue(any("total" in p for p in problems), problems)
        problems = e2e_uso.disk_problems(expected, {**expected, "scripts": 1})
        self.assertTrue(any("scripts" in p for p in problems), "una categoría vacía tiene que seguir vacía")
        self.assertTrue(e2e_uso.disk_problems(expected, {**expected, "files": 4}))
        self.assertTrue(e2e_uso.disk_problems(expected, {"skills": 1000}), "falta una categoría")

    def test_snapshot_bytes_read_from_the_api(self) -> None:
        latest = {"files": 2, "total_bytes": 30, "bytes": {"skills": 10, "scripts": 20, "schemas": 0,
                                                            "knowledge": 0, "versions": 0, "index": 0}}  # fmt: skip
        self.assertEqual(
            e2e_uso.snapshot_usage(latest),
            {"files": 2, "skills": 10, "scripts": 20, "schemas": 0, "knowledge": 0, "versions": 0, "index": 0},
        )
        self.assertEqual(e2e_uso.snapshot_usage(None), {})


class HoursTest(unittest.TestCase):
    def test_window_and_its_complement(self) -> None:
        self.assertEqual(e2e_uso.hour_window(10, 10), ("10-11", "11-10"))
        self.assertEqual(e2e_uso.hour_window(23, 23), ("23-24", "0-23"))
        self.assertEqual(e2e_uso.hour_window(0, 0), ("0-1", "1-24"))
        self.assertEqual(e2e_uso.hour_window(9, 10), ("9-11", "11-9"))
        self.assertEqual(e2e_uso.hour_window(23, 0), ("23-1", "1-23"), "cruza la medianoche")

    def test_the_window_covers_exactly_its_hours(self) -> None:
        def hours(text: str) -> set[int]:
            h1, h2 = (int(x) for x in text.split("-"))
            return set(range(h1, h2)) if h1 < h2 else set(range(h1, 24)) | set(range(h2))

        for first in range(24):
            for span in (0, 1):
                last = (first + span) % 24
                inside, outside = e2e_uso.hour_window(first, last)
                self.assertEqual(hours(inside), {first, last}, inside)
                self.assertEqual(hours(outside), set(range(24)) - {first, last}, outside)

    def test_local_keys(self) -> None:
        madrid = ZoneInfo("Europe/Madrid")
        at = datetime(2026, 11, 18, 22, 19, tzinfo=timezone.utc).astimezone(madrid)  # miércoles, 23:19 local
        self.assertEqual(e2e_uso.weekday_hour_key(at), "3-23")
        self.assertEqual(e2e_uso.hour_key(at), "2026-11-18T23:00:00+01:00")
        summer = datetime(2026, 7, 1, 8, 5, tzinfo=timezone.utc).astimezone(madrid)
        self.assertEqual(e2e_uso.hour_key(summer), "2026-07-01T10:00:00+02:00")


class TokensTest(unittest.TestCase):
    def test_messages(self) -> None:
        body = json.dumps({"usage": {"input_tokens": 65, "output_tokens": 18}}).encode()
        self.assertEqual(e2e_uso.reply_tokens("messages", body, stream=False), (65, 18))
        sse = (
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":117,'
            b'"output_tokens":1}}}\n\nevent: content_block_delta\ndata: {"type":"content_block_delta"}\n\n'
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":9}}\n\n'
        )
        self.assertEqual(e2e_uso.reply_tokens("messages", sse, stream=True), (117, 9))

    def test_openai(self) -> None:
        chat = json.dumps({"usage": {"prompt_tokens": 50, "completion_tokens": 3}}).encode()
        self.assertEqual(e2e_uso.reply_tokens("chat", chat, stream=False), (50, 3))
        responses = json.dumps({"usage": {"input_tokens": 61, "output_tokens": 4}}).encode()
        self.assertEqual(e2e_uso.reply_tokens("responses", responses, stream=False), (61, 4))
        sse = (
            b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":'
            b'{"input_tokens":7,"output_tokens":2}}}\n\n'
        )
        self.assertEqual(e2e_uso.reply_tokens("responses", sse, stream=True), (7, 2))
        chunks = 'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        self.assertEqual(e2e_uso.reply_tokens("chat", chunks.encode(), stream=True), (5, 1))

    def test_without_usage(self) -> None:
        self.assertIsNone(e2e_uso.reply_tokens("chat", b'data: {"choices":[]}\n\ndata: [DONE]\n\n', stream=True))
        self.assertIsNone(e2e_uso.reply_tokens("messages", b"no es json", stream=False))
        self.assertIsNone(e2e_uso.reply_tokens("messages", b'{"usage": {"input_tokens": "1"}}', stream=False))


class LatencyTest(unittest.TestCase):
    def test_quantiles(self) -> None:
        values = [float(v) for v in range(1, 101)]
        self.assertEqual(e2e_uso.quantile(values, 0.95), 96.0)
        self.assertEqual(e2e_uso.quantile([3.0], 0.95), 3.0)
        self.assertEqual(e2e_uso.median([1.0, 3.0, 2.0, 10.0]), 2.5)

    def test_overhead_is_the_difference_of_the_quantiles(self) -> None:
        direct = [1.0] * 95 + [2.0] * 5
        proxied = [3.0] * 95 + [6.0] * 5
        got = e2e_uso.overhead(direct, proxied)
        self.assertEqual((got["p50"], got["p95"]), (2.0, 4.0))
        self.assertEqual((got["direct_p50"], got["proxy_p95"]), (1.0, 6.0))

    def test_degradation(self) -> None:
        before = [10.0] * 20
        self.assertEqual(e2e_uso.degradation(before, [12.0] * 20), [])
        self.assertTrue(e2e_uso.degradation(before, [40.0] * 20), "el triple no es «igual»")
        problems = e2e_uso.degradation(before, [10.0] * 18 + [200.0] * 2)
        self.assertTrue(any("p95" in p for p in problems), problems)
        self.assertEqual(e2e_uso.degradation([1.0] * 20, [4.0] * 20), [], "un margen absoluto para lo muy rápido")


class StreamsTest(unittest.TestCase):
    def test_entries_of_redis_cli_raw(self) -> None:
        raw = '1795040370082-0\ndata\n{"type":"content.usage","org_slug":"a"}\n1795040370083-1\ndata\n{"x":1}\n'
        self.assertEqual(
            e2e_uso.stream_entries(raw),
            [("1795040370082-0", '{"type":"content.usage","org_slug":"a"}'), ("1795040370083-1", '{"x":1}')],
        )
        self.assertEqual(e2e_uso.stream_entries(""), [])
        self.assertEqual(e2e_uso.stream_entries("1-0\notro\nvalor\n"), [], "solo el campo data")

    def test_events_of_an_organization(self) -> None:
        mine = {"type": "content.usage", "org_slug": "a", "event_id": "E1"}
        entries = [("1-0", json.dumps(mine)), ("2-0", json.dumps({**mine, "org_slug": "b"})), ("3-0", "no json"),
                   ("4-0", json.dumps({**mine, "type": "storage.snapshot"}))]  # fmt: skip
        found = e2e_uso.events_of(entries, "content.usage", "a")
        self.assertEqual([(i, e["event_id"]) for i, e, _ in found], [("1-0", "E1")])
        self.assertEqual(found[0][2], json.dumps(mine), "conserva el texto tal cual para reinyectarlo")

    def test_group_info(self) -> None:
        raw = "name\nwebapp\nconsumers\n1\npending\n0\nlast-delivered-id\n1-0\nentries-read\n29\nlag\n0\n"
        self.assertEqual(e2e_uso.group_info(raw, "webapp"), {"consumers": "1", "pending": "0",
                                                             "last-delivered-id": "1-0", "entries-read": "29",
                                                             "lag": "0"})  # fmt: skip
        self.assertIsNone(e2e_uso.group_info(raw, "otro"))
        self.assertIsNone(e2e_uso.group_info("", "webapp"))

    def test_event_ids_are_ulids(self) -> None:
        ids = {e2e_uso.new_event_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for event_id in ids:
            self.assertRegex(event_id, r"^[0-9A-HJKMNP-TV-Z]{26}$")


class ContentTest(unittest.TestCase):
    def test_counts_by_key(self) -> None:
        series = [{"key": "a", "counts": {"INVOKED": 1, "SYNCED": 0}}, {"key": "", "counts": {"KNOWLEDGE_READ": 2}}]
        self.assertEqual(e2e_uso.counts_by_key(series), {"a": {"INVOKED": 1}, "": {"KNOWLEDGE_READ": 2}})
        self.assertEqual(e2e_uso.counts_by_key(None), {})

    def test_leaks(self) -> None:
        self.assertEqual(e2e_uso.leaks("x secreto-abc y", ["secreto-abc", "raiz-abc"]), ["secreto-abc"])
        self.assertEqual(e2e_uso.leaks(b"nada", ["secreto-abc"]), [])
        self.assertEqual(e2e_uso.leaks('{"p":"secreto\\u002dabc"}', ["secreto-abc"]), ["secreto-abc"],
                         "también con escapes de JSON")  # fmt: skip


# --- el inspector: lo que el grupo «uso» mira y toca dentro de los contenedores ----------------------------


class UsageInspectorTest(unittest.TestCase):
    """`ContainerInspector` (métodos de la Fase 4) con un motor falso que anota sus argumentos y responde lo dado."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.log = root / "calls.jsonl"
        self.replies = root / "replies.json"
        self.replies.write_text("{}")
        engine = root / "engine"
        engine.write_text(
            f"""#!{sys.executable}
import json, sys
argv = sys.argv[1:]
open({str(self.log)!r}, "a").write(json.dumps(argv) + "\\n")
if argv[0] == "ps":
    print("cid-" + [a for a in argv if a.startswith("label=com.docker.compose.service=")][0].split("=")[-1])
    sys.exit(0)
replies = json.load(open({str(self.replies)!r}))
for needle, (out, err, rc) in replies.items():
    if needle in " ".join(argv):
        sys.stdout.write(out)
        sys.stderr.write(err)
        sys.exit(rc)
"""
        )
        engine.chmod(engine.stat().st_mode | stat.S_IXUSR)
        self.inspector = e2e_h4.ContainerInspector(str(engine), "f4-uso")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def reply(self, **by_needle: tuple[str, str, int]) -> None:
        self.replies.write_text(json.dumps({k.replace("_", " "): v for k, v in by_needle.items()}))

    def runs(self) -> list[list[str]]:
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [c for c in calls if c[0] != "ps"]

    def test_usage_rows_of_an_organization(self) -> None:
        self.reply(psql=("3|1|0\n", "", 0))
        self.assertEqual(self.inspector.usage_rows(ORG_ID), {"traffic": 3, "content": 1, "snapshots": 0})
        [run] = self.runs()
        self.assertEqual(run[:7], ["exec", "cid-postgres", "psql", "-U", "postgres", "-d", "baas_control"])
        for table in ("usage_traffic_hourly", "usage_content_hourly", "storage_snapshots"):
            self.assertIn(f"FROM {table} WHERE org_id = '{ORG_ID}'", run[-1])

    def test_unsafe_values_are_refused_before_any_command(self) -> None:
        for bad in ("x'; DROP TABLE users; --", "", ORG_ID.upper() + "x"):
            with self.assertRaises(e2e_h4.StepError):
                self.inspector.usage_rows(bad)
            with self.assertRaises(e2e_h4.StepError):
                self.inspector.insert_old_usage(bad, USER_ID, months=3, tag="viejo-abc")
        with self.assertRaises(e2e_h4.StepError):
            self.inspector.insert_old_usage(ORG_ID, USER_ID, months=3, tag="Viejo'")
        with self.assertRaises(e2e_h4.StepError):
            self.inspector.insert_old_usage(ORG_ID, USER_ID, months=0, tag="viejo-abc")
        with self.assertRaises(e2e_h4.StepError):
            self.inspector.snapshot_count("x' OR '1'='1")
        with self.assertRaises(e2e_h4.StepError):
            self.inspector.mcp_files("../demo")
        with self.assertRaises(e2e_h4.StepError):
            self.inspector.stream_entries("redis", "baas:policy:org:demo")
        with self.assertRaises(e2e_h4.StepError):
            self.inspector.stream_add("redis-ephemeral", "baas:audit", "{}")
        with self.assertRaises(e2e_h4.StepError):
            self.inspector.service_control("postgres", "stop")
        with self.assertRaises(e2e_h4.StepError):
            self.inspector.service_control("webapp-api", "restart")
        self.assertEqual(self.runs(), [])

    def test_insert_old_usage_writes_three_rows_in_the_past(self) -> None:
        self.reply(psql=("INSERT 0 1\n", "", 0))
        self.inspector.insert_old_usage(ORG_ID, USER_ID, months=3, tag="viejo-abc")
        sql = self.runs()[0][-1]
        self.assertIn("make_interval(months => 3)", sql)
        for table in ("usage_traffic_hourly", "usage_content_hourly", "storage_snapshots"):
            self.assertIn(f"INSERT INTO {table}", sql)
        self.assertIn("'viejo-abc'", sql)

    def test_source_status_and_dump(self) -> None:
        self.reply(psql=("TRAFFIC|2026-11-18 22:19:20.549+00\nCONTENT|2026-11-18 22:19:20.453+00\n", "", 0))
        self.assertEqual(
            self.inspector.source_status(),
            {"TRAFFIC": "2026-11-18 22:19:20.549+00", "CONTENT": "2026-11-18 22:19:20.453+00"},
        )
        self.reply(psql=("usage_content_hourly|(a,b)\n", "", 0))
        self.assertIn("(a,b)", self.inspector.usage_dump())
        sql = self.runs()[-1][-1]
        for table in ("usage_traffic_hourly", "usage_content_hourly", "storage_snapshots", "storage_snapshot_items"):
            self.assertIn(table, sql)

    def test_streams(self) -> None:
        self.reply(XRANGE=("1-0\ndata\n{}\n", "", 0), XADD=("1795-0\n", "", 0), EXISTS=("1\n", "", 0),
                   XINFO=("name\nwebapp\npending\n0\n", "", 0))  # fmt: skip
        self.assertEqual(self.inspector.stream_entries("redis", "baas:usage"), [("1-0", "{}")])
        self.assertEqual(self.inspector.stream_add("redis", "baas:usage", '{"a":1}'), "1795-0")
        self.assertTrue(self.inspector.key_exists("redis-ephemeral", "baas:usage"))
        self.assertEqual(self.inspector.stream_group("redis", "baas:usage", "webapp"), {"pending": "0"})
        runs = self.runs()
        self.assertEqual(runs[0][:3], ["exec", "cid-redis", "sh"])
        self.assertEqual(runs[0][-5:], ["--raw", "XRANGE", "baas:usage", "-", "+"])
        self.assertEqual(runs[1][-5:], ["XADD", "baas:usage", "*", "data", '{"a":1}'])
        self.assertEqual(runs[2][1], "cid-redis-ephemeral")
        self.assertEqual(runs[2][-2:], ["EXISTS", "baas:usage"])
        self.reply(XADD=("ERR wrong\n", "", 0))
        with self.assertRaises(e2e_h4.StepError):
            self.inspector.stream_add("redis", "baas:usage", "{}")

    def test_entry_present(self) -> None:
        self.reply(XRANGE=("", "", 0))
        self.assertFalse(self.inspector.stream_has("redis-ephemeral", "baas:traces", "17-0"))
        self.assertEqual(self.runs()[-1][-5:], ["--raw", "XRANGE", "baas:traces", "17-0", "17-0"])
        with self.assertRaises(e2e_h4.StepError):
            self.inspector.stream_has("redis", "baas:usage", "17-0 +")

    def test_services(self) -> None:
        self.inspector.service_control("webapp-api", "stop")
        self.inspector.service_control("mcp-server", "unpause")
        self.reply(logs=("salida\n", "error\n", 0))
        logs = self.inspector.service_logs("mcp-server", "2026-11-18T22:00:00Z")
        self.assertIn("salida", logs)
        self.assertIn("error", logs, "los logs de la aplicación salen por stderr")
        runs = self.runs()
        self.assertEqual(runs[0], ["stop", "cid-webapp-api"])
        self.assertEqual(runs[1], ["unpause", "cid-mcp-server"])
        self.assertEqual(runs[2], ["logs", "--since", "2026-11-18T22:00:00Z", "cid-mcp-server"])

    def test_starting_a_service_waits_longer_than_a_query(self) -> None:
        """`podman start webapp-api` vuelve a ejecutar la migración y la semilla: 60 s no bastan."""
        seen: list[float] = []
        real = e2e_h4.subprocess.run

        def spy(*args: object, **kwargs: object) -> object:
            seen.append(float(kwargs["timeout"]))  # type: ignore[arg-type]
            return real(*args, **kwargs)  # type: ignore[call-overload]

        with mock.patch.object(e2e_h4.subprocess, "run", side_effect=spy):
            self.inspector.service_control("webapp-api", "start")
        self.assertEqual(seen[-1], e2e_h4.SERVICE_TIMEOUT)
        self.assertGreaterEqual(e2e_h4.SERVICE_TIMEOUT, 180)
        self.assertEqual(seen[0], 60, "las consultas siguen con 60 s")

    def test_mcp_files(self) -> None:
        self.reply(find=("f\t73\tskills/a/SKILL.md\nf\t49152\tindex.sqlite\nl\t9\tskills/b\n", "", 0))
        self.assertEqual(self.inspector.mcp_files("uso-abc-dk"), [("skills/a/SKILL.md", 73), ("index.sqlite", 49152)])
        run = self.runs()[0]
        self.assertEqual(run[:3], ["exec", "cid-mcp-server", "find"])
        self.assertIn("/data/orgs/uso-abc-dk", run)

    def test_missing_inspector_says_how_to_continue(self) -> None:
        missing = e2e_h4.MissingInspector("podman no está instalado")
        for name in ("usage_rows", "tagged_rows", "usage_dump", "source_status", "insert_old_usage", "snapshot_count",
                     "key_exists", "stream_group", "stream_entries", "stream_add", "stream_has",
                     "service_control", "service_logs", "mcp_files"):  # fmt: skip
            with self.assertRaises(e2e_h4.StepError, msg=name):
                getattr(missing, name)("x")


class FollowUpTest(unittest.TestCase):
    """El segundo turno de una conversación tal como lo envía Claude Code (12.2): el historial con la respuesta del
    modelo tal cual (para que el proxy lo correlacione) y el bloque nuevo del usuario."""

    def test_after_a_skill_tool_use_comes_its_tool_result(self) -> None:
        first = [{"type": "text", "text": "usa la herramienta skill a:informes"}]
        block = {"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {"skill": "a:informes"}}
        body = json.dumps({"content": [block], "stop_reason": "tool_use"}).encode()
        messages = e2e_uso.follow_up(first, body, e2e_uso.tool_result_for(body, "Launching skill: a:informes"))
        self.assertEqual(messages[0], {"role": "user", "content": first})
        self.assertEqual(messages[1], {"role": "assistant", "content": [block]}, "la respuesta, sin tocar")
        self.assertEqual(
            messages[2],
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                          "content": "Launching skill: a:informes"}]},
        )  # fmt: skip

    def test_after_a_text_reply_comes_a_new_question(self) -> None:
        body = json.dumps({"content": [{"type": "text", "text": "Eco: hola"}]}).encode()
        messages = e2e_uso.follow_up("hola", body, "segunda pregunta")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[2]["content"], "segunda pregunta")

    def test_a_reply_without_content_or_tool_use_is_an_error(self) -> None:
        with self.assertRaises(e2e_h4.StepError):
            e2e_uso.follow_up("hola", b"no es json", "x")
        with self.assertRaises(e2e_h4.StepError):
            e2e_uso.tool_result_for(json.dumps({"content": [{"type": "text", "text": "x"}]}).encode(), "y")


class SecondTurnTest(unittest.TestCase):
    """12.2: el segundo turno de la herramienta tiene que traer, al principio de su `request_original`, el
    `[tool_use: Skill]` y el `tool_result` «Launching skill:»; si no, «no hay doble recuento» no prueba nada."""

    GOOD = '[tool_use: Skill] {"skill":"uso-a-sk:informes"}\n\n[tool_result] Launching skill: uso-a-sk:informes'

    def test_the_expected_text_passes(self) -> None:
        self.assertEqual(e2e_uso.second_turn_problems("uso-a-sk", "informes", self.GOOD), [])
        segments = [{"text": self.GOOD[:20]}, {"text": self.GOOD[20:]}]
        self.assertEqual(e2e_uso.second_turn_problems("uso-a-sk", "informes", segments), [], "también en segmentos")

    def test_missing_pieces_are_reported(self) -> None:
        only_result = "[tool_result] Launching skill: uso-a-sk:informes"
        problems = e2e_uso.second_turn_problems("uso-a-sk", "informes", only_result)
        self.assertTrue(problems and "tool_use: Skill" in problems[0], problems)
        only_use = '[tool_use: Skill] {"skill":"uso-a-sk:informes"}'
        problems = e2e_uso.second_turn_problems("uso-a-sk", "informes", only_use)
        self.assertTrue(any("Launching skill" in p for p in problems), problems)
        self.assertTrue(e2e_uso.second_turn_problems("uso-a-sk", "informes", None))
        self.assertTrue(e2e_uso.second_turn_problems("uso-b-sk", "informes", self.GOOD), "otra organización")


class SpikeRegexTest(unittest.TestCase):
    """Las expresiones del spike T0 son las del contrato §7 (con las que cuenta la webapp)."""

    def test_skill_name_is_the_contract_one(self) -> None:
        import spike_f4_skill_invocation as spike

        self.assertEqual(spike.SKILL_NAME, r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})")
        self.assertEqual(spike.regex_self_test(), [])
        text = "<command-name>/ginxo:Informe.v2</command-name>"
        self.assertEqual(spike.slash_re("ginxo").findall(text), ["Informe.v2"])
        self.assertEqual(spike.slash_re("ginxo").findall("<command-name>/ginxo:" + "a" * 129 + "</command-name>"), [])

    def test_the_notes_carry_the_contract_expressions(self) -> None:
        import spike_f4_skill_invocation as spike

        for key, expression in (("regex_slash", spike.slash_re), ("regex_tool", spike.tool_re)):
            self.assertEqual(spike.NOTES[key].split("  (")[0], expression("ORG").pattern.replace("ORG", "{org}"))
        self.assertNotIn("[a-z0-9][a-z0-9_-]*", json.dumps(spike.NOTES))


class RestartTest(unittest.TestCase):
    """12.7: lo que se paró se vuelve a arrancar aunque algo falle, y se espera antes de eliminar la organización."""

    class Inspector:
        def __init__(self, fail: set[str]) -> None:
            self.fail, self.calls = fail, []

        def service_control(self, service: str, action: str) -> None:
            self.calls.append((service, action))
            if service in self.fail:
                raise e2e_h4.StepError(f"{service} no arranca")

    def group(self, fail: set[str]) -> tuple[e2e_uso.UsoGroup, list[str]]:
        e2e = e2e_h4.E2EH4(e2e_h4.parse_args(["--inspect", "none"]), inspector=self.Inspector(fail))  # type: ignore[arg-type]
        group = e2e_uso.UsoGroup(e2e)
        waited: list[str] = []
        group.wait_webapp = lambda: waited.append("webapp")  # type: ignore[method-assign]
        group.wait_mcp = lambda org: waited.append("mcp")  # type: ignore[method-assign]
        return group, waited

    def test_each_service_is_started_even_if_the_other_fails(self) -> None:
        group, waited = self.group({"mcp-server"})
        org = e2e_uso.UsoOrg("uso-x-pr", ORG_ID, ZoneInfo("Europe/Madrid"))
        with self.assertRaises(e2e_h4.StepError) as ctx:
            group.restart(org, ["webapp-api", "mcp-server"], failed=False)
        self.assertIn("mcp-server", str(ctx.exception))
        calls = group.inspector().calls
        self.assertEqual(calls, [("mcp-server", "start"), ("webapp-api", "start")])
        self.assertEqual(waited, ["webapp", "mcp"], "espera a los dos antes de volver")

    def test_a_failed_step_keeps_its_own_error(self) -> None:
        group, waited = self.group({"webapp-api"})
        org = e2e_uso.UsoOrg("uso-x-pr", ORG_ID, ZoneInfo("Europe/Madrid"))
        group.restart(org, ["webapp-api"], failed=True)  # no lanza: el error del paso es el que cuenta
        self.assertEqual(waited, ["webapp"])


class PurgeTest(unittest.TestCase):
    """12.8: la retención se restaura siempre (también con Ctrl-C) y la webapp se vuelve a arrancar si falló."""

    class Inspector:
        def __init__(self, start_failures: int) -> None:
            self.start_failures, self.calls = start_failures, []

        def service_control(self, service: str, action: str) -> None:
            self.calls.append((service, action))
            if action == "start" and self.start_failures:
                self.start_failures -= 1
                raise e2e_h4.StepError("no arranca")

    def group(self, start_failures: int = 0) -> tuple[e2e_uso.UsoGroup, list[object]]:
        e2e = e2e_h4.E2EH4(e2e_h4.parse_args(["--inspect", "none"]), inspector=self.Inspector(start_failures))  # type: ignore[arg-type]
        group = e2e_uso.UsoGroup(e2e)
        puts: list[object] = []

        def webapp(method: str, path: str, who: str, payload: object = None) -> e2e_h4.Http:
            puts.append((method, path, payload))
            return e2e_h4.Http(200, [], b"{}")

        e2e.webapp = webapp  # type: ignore[method-assign]
        group.wait_webapp = lambda: None  # type: ignore[method-assign]
        return group, puts

    def months(self, puts: list[object]) -> list[int]:
        return [p[2]["months"] for p in puts if p[0] == "PUT"]  # type: ignore[index]

    def test_happy_path_sets_and_restores(self) -> None:
        group, puts = self.group()
        self.assertEqual(group.purge_with_retention(60, 13, lambda: None), [])
        self.assertEqual(self.months(puts), [60, 13])
        self.assertEqual(group.inspector().calls, [("webapp-api", "stop"), ("webapp-api", "start")])

    def test_a_failed_start_is_retried_and_reported(self) -> None:
        group, puts = self.group(start_failures=1)
        with self.assertRaises(e2e_h4.StepError) as ctx:
            group.purge_with_retention(60, 13, lambda: None)
        self.assertIn("no arranca", str(ctx.exception))
        self.assertIn("se volvió a arrancar", str(ctx.exception))
        self.assertEqual(group.inspector().calls[-1], ("webapp-api", "start"))
        self.assertEqual(self.months(puts), [60, 13])

    def test_an_interrupt_still_restores_the_retention(self) -> None:
        group, puts = self.group()

        def interrupted() -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            group.purge_with_retention(60, 13, interrupted)
        self.assertEqual(self.months(puts), [60, 13])


class MidnightTest(unittest.TestCase):
    """Peticiones a las 23:59:5x locales y consulta a las 00:00:0x: el rango por defecto incluye el día anterior."""

    def test_default_range_starts_yesterday(self) -> None:
        group = e2e_uso.UsoGroup(e2e_h4.E2EH4(e2e_h4.parse_args(["--inspect", "none"]), inspector=None))
        madrid = ZoneInfo("Europe/Madrid")
        org = e2e_uso.UsoOrg("uso-x-tr", ORG_ID, madrid)
        sent = datetime(2026, 11, 18, 23, 59, 55, tzinfo=madrid)
        later = datetime(2026, 11, 19, 0, 0, 3, tzinfo=madrid)
        with mock.patch.object(e2e_uso, "local_now", return_value=later):
            dates = group.today(org)
            self.assertLessEqual(dates["from"], sent.date().isoformat())
            self.assertEqual(dates, {"from": "2026-11-18", "to": "2026-11-19"})
            self.assertEqual(group.today(org, sent), {"from": "2026-11-18", "to": "2026-11-19"})


class RetentionWarningTest(unittest.TestCase):
    def test_a_retention_left_by_a_killed_run_is_reported(self) -> None:
        self.assertEqual(e2e_uso.retention_warning(13), "")
        self.assertIn("AVISO", e2e_uso.retention_warning(e2e_uso.TEST_RETENTION))


class Phase4Test(unittest.TestCase):
    def test_a_stack_without_the_usage_api_is_named(self) -> None:
        self.assertIsNone(e2e_uso.phase4_problem(200))
        self.assertIn("pila sin la Fase 4", e2e_uso.phase4_problem(404) or "")
        self.assertIn("reconstruye", e2e_uso.phase4_problem(404) or "")
        self.assertIn("HTTP 500", e2e_uso.phase4_problem(500) or "")


class CreateOrgTest(unittest.TestCase):
    def test_a_half_created_organization_is_deleted(self) -> None:
        e2e = e2e_h4.E2EH4(e2e_h4.parse_args(["--inspect", "none"]), inspector=None)
        group = e2e_uso.UsoGroup(e2e)
        posts: list[str] = []

        def admin_post(path: str, payload: object = None, who: object = None, method: str = "POST") -> e2e_h4.Http:
            posts.append(f"{method} {path}")
            if path == "/api/v1/orgs":
                body = {"organization": {"id": ORG_ID, "timezone": "Europe/Madrid"}}
                return e2e_h4.Http(201, [], json.dumps(body).encode())
            if path.endswith("/members"):
                return e2e_h4.Http(500, [], b'{"error": "BOOM"}')
            return e2e_h4.Http(200, [], b"{}")

        e2e.admin_post = admin_post  # type: ignore[method-assign]
        with self.assertRaises(e2e_h4.StepError):
            group.create_org("tr", {"analista@demo.test": "USER"})
        self.assertIn(f"POST /api/v1/orgs/uso-{e2e.nonce}-tr/delete", posts, "la organización a medias se elimina")


class GroupTest(unittest.TestCase):
    def test_uso_is_a_group_and_runs_alone(self) -> None:
        self.assertIn("uso", e2e_h4.GROUPS)
        self.assertEqual(e2e_h4.parse_args(["--only", "uso"]).only, ["uso"])
        ns = e2e_h4.parse_args([])
        self.assertIn("uso", ns.only)
        self.assertEqual((ns.usage_timeout, ns.overhead_n, ns.usage_window), (90.0, 200, 5.0))
        self.assertEqual(ns.overhead_p95_ms, 10.0)
        self.assertEqual(e2e_h4.parse_args(["--overhead-p95-ms", "25"]).overhead_p95_ms, 25.0)

    def test_steps_are_registered_in_order(self) -> None:
        ns = e2e_h4.parse_args(["--only", "uso", "--inspect", "none"])
        names: list[str] = []

        class Recorder:
            def step(self, name: str, fn: object, *, needs: object = ()) -> None:
                names.append(name)

        e2e_h4.E2EH4(ns, inspector=None).run(Recorder())  # type: ignore[arg-type]
        uso = [n for n in names if n.startswith("12.")]
        self.assertEqual([n.split(" ")[0] for n in uso], ["12.0", "12.1", "12.2", "12.3", "12.6", "12.10"],
                         "sin inspector solo los pasos que no miran dentro de los contenedores")  # fmt: skip
        self.assertFalse([n for n in names if n.startswith(("5.", "8.", "9.", "10.", "11."))])
        inspector = e2e_h4.MissingInspector("prueba")
        names.clear()
        e2e_h4.E2EH4(ns, inspector=inspector).run(Recorder())  # type: ignore[arg-type]
        uso = [n.split(" ")[0] for n in names if n.startswith("12.")]
        self.assertEqual(uso, [f"12.{i}" for i in range(11)], "12.0 (la pila tiene la Fase 4) va el primero")


if __name__ == "__main__":
    unittest.main()
