"""Grupo «acl» (pasos 13.1–13.5) de `dev/e2e_h4.py`: cada servicio solo escribe lo suyo en Redis (H4-05).

Con las credenciales reales de cada usuario (las lee el propio contenedor de Redis de su entorno) se intenta lo que
el diseño (`fase-1-h4-acl-redis-diseno.md`, §4 y §8) prohíbe, y Redis debe responder `NOPERM`:

  13.1  los servicios están conectados con su usuario y ninguno como `default` (la persistente: webapp, proxy y mcp;
        la efímera: webapp y proxy).
  13.2  el MCP no escribe política, claves, membresías ni el informe, ni publica en `baas:control`.
  13.3  el proxy tampoco.
  13.4  solo el proxy escribe en `baas:traces` (la webapp no) y el MCP no tiene usuario en la efímera.
  13.5  el monitor solo escribe su informe (no lee ni el informe ni la política).

Todo intento va por una conexión con el nombre `e2e-acl` (`CLIENT_NAME`): así `deploy/scripts/e2e-ci.sh` distingue en
`ACL LOG` los rechazos que provoca a propósito este grupo de los de un servicio de verdad. Los pasos miran dentro de
los contenedores de Redis (`--inspect`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from e2e_h2 import StepError, check

if TYPE_CHECKING:
    from e2e_h2 import Runner
    from e2e_h4 import E2EH4

# Nombre de la conexión de cada intento: `e2e-ci.sh` ignora las entradas de ACL LOG cuyo client-info lo lleve
CLIENT_NAME = "e2e-acl"
ACL_USERS = ("webapp", "proxy", "mcp", "monitor")
PERSISTENT, EPHEMERAL = "redis", "redis-ephemeral"
SAFE_COMMAND = re.compile(r"^[A-Za-z0-9 :*~_.-]{1,200}$")
_LOOPBACK = re.compile(r"^(127\.0\.0\.1|\[::1\]|::1):")
# Lo que intentan el MCP y el proxy (§4.1 del diseño): todo debe dar NOPERM
FORBIDDEN_WRITES = (
    "HSET baas:policy:org:demo blocked 0",
    "HSET baas:key:e2e-acl user_id x",
    "HSET baas:member:e2e-acl:demo governance_role ORG_ADMIN",
    "SET baas:platform:status x",
    "PUBLISH baas:control x",
)
TRACE_WRITE = "XADD baas:traces * data x"
MONITOR_READS = ("GET baas:platform:status", "HGETALL baas:policy:org:demo")


def is_noperm(reply: str) -> bool:
    """`True` si la respuesta de Redis es un rechazo de la ACL."""
    return reply.startswith("NOPERM")


def client_users(client_list: str) -> set[str]:
    """Los `user=` de las conexiones de `CLIENT LIST` que no vienen de la propia máquina: el *healthcheck* y el
    `redis-cli` del e2e corren dentro del contenedor como `default` (loopback); los servicios llegan por la red."""
    users: set[str] = set()
    for line in client_list.splitlines():
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        if "user" in fields and not _LOOPBACK.match(fields.get("addr", "")):
            users.add(fields["user"])
    return users


def clean_reply(reply: str) -> str:
    """La respuesta sin el prefijo «(error) » que `redis-cli` pone a los errores fuera de `--raw`."""
    reply = reply.strip()
    return reply.removeprefix("(error) ")


def safe_command(user: str, command: str) -> None:
    if user not in ACL_USERS:
        raise StepError(f"usuario de Redis no válido: {user!r} (se esperaba {', '.join(ACL_USERS)})")
    if not SAFE_COMMAND.fullmatch(command):
        raise StepError(f"orden de Redis no válida para el e2e: {command!r}")


def attempts(e2e: E2EH4, service: str, user: str, commands: Sequence[str]) -> list[str]:
    """Ejecuta cada orden como `user`; devuelve las que Redis NO rechazó con NOPERM («orden → respuesta»)."""
    assert e2e.inspector is not None
    accepted = []
    for command in commands:
        reply = clean_reply(e2e.inspector.redis_as(service, user, command))
        if not is_noperm(reply):
            accepted.append(f"«{command}» como {user} en {service} → {reply[:120] or '(vacío)'}")
    return accepted


def register(runner: Runner, e2e: E2EH4, base: Sequence[str] = ()) -> None:
    step = runner.step
    inspector = e2e.inspector
    assert inspector is not None
    needs = list(base)

    def connected() -> str:
        found = {
            service: client_users(inspector.redis_admin(service, "--raw", "CLIENT", "LIST"))
            for service in (PERSISTENT, EPHEMERAL)
        }
        want = {PERSISTENT: {"webapp", "proxy", "mcp"}, EPHEMERAL: {"webapp", "proxy"}}
        problems = []
        for service, users in found.items():
            if "default" in users:
                problems.append(f"{service}: hay un servicio conectado como «default»")
            if missing := sorted(want[service] - users):
                problems.append(f"{service}: no hay conexiones de {', '.join(missing)}")
        check(not problems, "; ".join(problems) + f" (conectados: { {s: sorted(u) for s, u in found.items()} })")
        return (
            "persistente: "
            + ", ".join(sorted(found[PERSISTENT]))
            + " · efímera: "
            + ", ".join(sorted(found[EPHEMERAL]))
        )

    def denied(user: str) -> Sequence[str]:
        def run() -> str:
            accepted = attempts(e2e, PERSISTENT, user, FORBIDDEN_WRITES)
            check(not accepted, "Redis no rechazó: " + "; ".join(accepted))
            return f"{len(FORBIDDEN_WRITES)} intentos como {user} → NOPERM"

        return run  # type: ignore[return-value]

    def traces() -> str:
        accepted = attempts(e2e, EPHEMERAL, "webapp", [TRACE_WRITE])
        check(not accepted, "Redis no rechazó: " + "; ".join(accepted))
        mcp_user = inspector.redis_admin(EPHEMERAL, "--raw", "ACL", "GETUSER", "mcp").strip()
        check(not mcp_user, f"la efímera tiene un usuario «mcp»: {mcp_user[:100]!r}")
        return f"«{TRACE_WRITE}» como webapp → NOPERM; ACL GETUSER mcp en {EPHEMERAL} → vacío"

    def monitor() -> str:
        accepted = attempts(e2e, PERSISTENT, "monitor", MONITOR_READS)
        check(not accepted, "Redis no rechazó: " + "; ".join(accepted))
        return f"{len(MONITOR_READS)} lecturas como monitor → NOPERM"

    step("13.1 los servicios entran con su usuario y ninguno como default", connected, needs=needs)
    step("13.2 el MCP no escribe política, claves, membresías ni el informe, ni publica en baas:control",
         denied("mcp"), needs=needs)  # fmt: skip
    step("13.3 el proxy tampoco", denied("proxy"), needs=needs)
    step("13.4 solo el proxy escribe en baas:traces; el MCP no tiene usuario en la efímera", traces, needs=needs)
    step("13.5 el monitor solo escribe su informe", monitor, needs=needs)
