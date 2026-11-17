#!/usr/bin/env python3
"""Puente TCP ↔ socket Unix para probar un cliente real **sin salida a Internet**.

Un espacio de nombres de red vacío (`unshare -rn`) no ve el `127.0.0.1` del equipo, pero sí el sistema de
ficheros. Con dos puentes, el cliente aislado solo llega a un puerto concreto de la pila local:

    python3 dev/netns_bridge.py unix-to-tcp /ruta/puente.sock 127.0.0.1:8080 &      # fuera
    unshare -rn sh -c 'ip link set lo up &&
        python3 dev/netns_bridge.py tcp-to-unix 127.0.0.1:8080,[::1]:8080 /ruta/puente.sock & <cliente>'

Lo usa `dev/spike_s13_codex.sh`. Solo biblioteca estándar.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


def _hostport(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host.strip("[]"), int(port)


async def _serve(mode: str, listen: str, target: str) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if mode == "tcp-to-unix":
                up_r, up_w = await asyncio.open_unix_connection(target)
            else:
                up_r, up_w = await asyncio.open_connection(*_hostport(target))
        except OSError:
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))

    servers = []
    if mode == "tcp-to-unix":
        for addr in listen.split(","):
            servers.append(await asyncio.start_server(handle, *_hostport(addr)))
    else:
        servers.append(await asyncio.start_unix_server(handle, listen))
    print(f"puente {mode}: {listen} → {target}", file=sys.stderr, flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main() -> int:
    p = argparse.ArgumentParser(description="Puente TCP ↔ socket Unix")
    p.add_argument("mode", choices=["tcp-to-unix", "unix-to-tcp"])
    p.add_argument("listen", help="host:puerto[,host:puerto] (tcp-to-unix) o ruta del socket (unix-to-tcp)")
    p.add_argument("target", help="ruta del socket (tcp-to-unix) o host:puerto (unix-to-tcp)")
    ns = p.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(ns.mode, ns.listen, ns.target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
