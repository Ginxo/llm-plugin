#!/usr/bin/env python3
"""Motor de contenedores falso (el `podman`/`docker` de `ContainerInspector`) para probarlo sin contenedores.

Lo lanza un envoltorio que fija `FAKE_ENGINE_STATE` (un JSON con lo que «hay» en la pila y el registro de llamadas).
Lo que no es Redis ni PostgreSQL **se ejecuta de verdad**: el script de búsqueda de `disk_contains` y `find` corren con
el `sh` del equipo sobre un directorio temporal que hace de `/data` del contenedor, de modo que los tests prueban el
script que se manda a los contenedores y no un resultado enlatado.

Estado (`FAKE_ENGINE_STATE`):

- `containers`: servicios que existen.
- `data_dirs`: servicio → directorio local que hace de `/data`.
- `config`: servicio → {parámetro: valor} de `CONFIG GET`.
- `keys`: servicio → claves que ve `redis-cli --scan`.
- `xlen`: servicio → {clave: longitud o texto de error} de `XLEN` (una clave que no está da 0).
- `rewrite_replies`: servicio → respuestas sucesivas de `BGREWRITEAOF` (por defecto, «started»).
- `info_polls`: servicio → `aof_rewrite_in_progress` de cada `INFO persistence` sucesivo (después, 0).
- `bgrewrite_status`: servicio → `aof_last_bgrewrite_status` (por defecto, `ok`).
- `psql`: salida de `psql`.
- `calls`: lo que se le pidió (lo anota él).
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

STARTED = "Background append only file rewriting started"


def _redis_cli(state: dict[str, Any], service: str, args: list[str]) -> tuple[int, str]:
    if args[:2] == ["--scan", "--pattern"]:
        keys = [k for k in state.get("keys", {}).get(service, []) if fnmatch.fnmatchcase(k, args[2])]
        return 0, "".join(f"{k}\n" for k in keys)
    if args[:3] == ["--raw", "CONFIG", "GET"]:
        config = state.get("config", {}).get(service, {})
        return 0, f"{args[3]}\n{config[args[3]]}\n" if args[3] in config else ""
    if args == ["BGREWRITEAOF"]:
        replies = state.setdefault("rewrite_replies", {}).setdefault(service, [])
        return 0, (replies.pop(0) if replies else STARTED) + "\n"
    if args == ["INFO", "persistence"]:
        polls = state.setdefault("info_polls", {}).setdefault(service, [])
        busy = polls.pop(0) if polls else 0
        status = state.get("bgrewrite_status", {}).get(service, "ok")
        return 0, f"aof_rewrite_in_progress:{busy}\r\naof_rewrite_scheduled:0\r\naof_last_bgrewrite_status:{status}\r\n"
    if args[:1] == ["XLEN"]:
        length = state.get("xlen", {}).get(service, {}).get(args[1], 0)  # un error sale con rc 0, como en redis-cli
        return 0, f"{length}\n"
    if args[:1] == ["SET"]:
        state.setdefault("set", {}).setdefault(service, {})[args[1]] = args[2]
        return 0, "OK\n"
    if args[:1] == ["DEL"]:
        removed = state.setdefault("set", {}).setdefault(service, {}).pop(args[1], None)
        return 0, f"{0 if removed is None else 1}\n"
    return 1, f"(error) ERR comando no simulado: {args}\n"


def handle(state: dict[str, Any], argv: list[str]) -> tuple[int, str]:
    if argv[0] == "ps":
        service = next(a for a in argv if a.startswith("label=com.docker.compose.service=")).split("=")[-1]
        return 0, f"cid-{service}\n" if service in state["containers"] else ""
    service, rest = argv[1].removeprefix("cid-"), argv[2:]
    if service not in state["containers"]:
        return 1, f"no such container {argv[1]}\n"
    if rest[0] == "psql":
        return 0, state.get("psql", "")
    if rest[:2] == ["sh", "-c"] and "redis-cli" in rest[2]:
        return _redis_cli(state, service, rest[4:])
    local = state.get("data_dirs", {}).get(service)
    if rest[0] == "find" and local:
        cmd = ["find", *(local if a == "/data" else a for a in rest[1:])]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return done.returncode, done.stdout.replace(local, "/data")
    if rest[:2] == ["sh", "-c"] and local:  # el script de búsqueda de disk_contains: se ejecuta de verdad
        args = [local if a == "/data" else a for a in rest[4:]]
        done = subprocess.run(["sh", "-c", rest[2], "sh", *args], capture_output=True, text=True, check=False)
        return done.returncode, done.stdout
    return 1, f"comando no simulado: {rest}\n"


def main() -> int:
    path = Path(os.environ["FAKE_ENGINE_STATE"])
    state = json.loads(path.read_text())
    state.setdefault("calls", []).append(sys.argv[1:])
    code, text = handle(state, sys.argv[1:])
    path.write_text(json.dumps(state))
    sys.stdout.write(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
