"""Ejecutor bajo demanda de scripts del MCP (fase-1-mvp §2.1, contrato §10, riesgo S6; H5 §7.3).

Es el respaldo para clientes sin sincronización (`baas.py sync`). Mecanismo:

1. Se descarga el paquete **completo** de la skill (`baas_get_skill_package` + `baas_get_skill_files`),
   de modo que el script ve a su lado sus ficheros hermanos (plantillas, módulos, datos).
2. Cada fichero se verifica con su `sha256` del manifiesto y, si se pasa `--sha256`, el del script
   también con ese. Si algo no coincide, no se ejecuta nada.
3. El árbol se escribe en un directorio temporal privado (`mkdtemp`, 0700; `<tmp>/<skill>/…`), con el
   bit de ejecución solo en lo que declara `executable`, y `CLAUDE_SKILL_DIR` apunta a la skill, como en
   una skill instalada.
4. Intérprete (en este orden): el `#!` del script (un `python` genérico se sustituye por el Python del
   plugin; `env` y `-S` se resuelven); si no hay `#!`, la extensión (`.py` con el Python del plugin,
   `.sh`/`.bash` con bash, `.js`/`.mjs`/`.cjs` con `node`); si no, un único intérprete en
   `baas.yaml.requires.interpreters`; si no, se ejecuta directamente si figura en `executable`.
   `requires` solo avisa de lo que falte.
5. Directorio de trabajo: el del proceso (o `--cwd`); no el temporal ni el del proyecto.
6. El entorno del script no incluye la API key (`ANTHROPIC_CUSTOM_HEADERS`, `BAAS_API_KEY`).
7. El directorio temporal se borra siempre: al terminar, si el script falla o si se interrumpe
   (también con SIGTERM o SIGHUP, que además terminan el script).

Códigos de salida: el del script, **íntegro** (un script que sale con 9 o 10 no se confunde con un error
del plugin; si lo mata una señal, `128 + señal`). Los errores del propio plugin salen con su código de
`errors.py` **y** una línea `BAAS_ERROR=<código>` en stderr (la añade `cli.main`): esa línea es la que
distingue un error del plugin del código de salida del script.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import package_fetch
from .errors import ConfigError, ScriptIntegrityError
from .package_fetch import Package

SECRET_ENV = ("ANTHROPIC_CUSTOM_HEADERS", "BAAS_API_KEY")
_PYTHON = ("python", "python3")
_BY_EXTENSION = {
    ".py": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".js": "node",
    ".mjs": "node",
    ".cjs": "node",
}


def split_script_path(path: str) -> tuple[str, str]:
    """Ruta de script → (skill, ruta dentro de la skill).

    Formas: `skills/<skill>/<ruta>` (la de `baas_list_skills`; cualquier ruta del paquete),
    `<skill>/<ruta>` y, por compatibilidad con H1–H4, `<skill>/<script>` = `scripts/<script>`.
    """
    parts = PurePosixPath(path).parts if path else ()
    bad = ConfigError(
        f"Ruta de script no válida: «{path}».",
        hint="Usa skills/<skill>/scripts/<script> (como la devuelve baas_list_skills) o <skill>/<script>.",
    )
    if len(parts) >= 3 and parts[0] == "skills":
        skill, rel = parts[1], "/".join(parts[2:])
    elif len(parts) == 2 and parts[0] != "skills":
        skill, rel = parts[0], f"scripts/{parts[1]}"
    elif len(parts) >= 3:
        skill, rel = parts[0], "/".join(parts[1:])
    else:
        raise bad
    if not package_fetch.valid_skill_name(skill) or "//" in path or ".." in rel.split("/") or rel.startswith("/"):
        raise bad
    return skill, rel


# --- intérprete ----------------------------------------------------------------------------------


def _shebang(content: bytes) -> list[str] | None:
    if not content.startswith(b"#!"):
        return None
    line = content.split(b"\n", 1)[0][2:].decode("utf-8", errors="replace").strip()
    try:
        argv = shlex.split(line)
    except ValueError:
        argv = line.split()
    if argv and PurePosixPath(argv[0]).name == "env":
        argv = [a for a in argv[1:] if a != "-S" and "=" not in a]  # `env -S VAR=x prog` → `prog`
    return argv or None


def _is_python(program: str) -> bool:
    name = PurePosixPath(program).name
    return name in _PYTHON or (name.startswith("python3.") and name[8:].isdigit())


def _find(program: str) -> str:
    if program == "python":
        return sys.executable
    found = shutil.which(program)
    if found is None:
        raise ConfigError(
            f"No se encontró el intérprete «{program}» en el PATH.",
            hint="Instálalo (la skill lo necesita) o ejecuta el script a mano desde un paquete exportado.",
        )
    return found


def _program(spec: str) -> str:
    """`python3>=3.10` → `python3` (las versiones de `requires` son informativas)."""
    for sep in (">", "<", "=", "~", " "):
        spec = spec.split(sep)[0]
    return spec.strip()


def resolve_interpreter(rel: str, content: bytes, manifest: package_fetch.Manifest) -> list[str]:
    """Argumentos previos al script. Lista vacía = ejecutarlo directamente (declarado en `executable`)."""
    shebang = _shebang(content)
    if shebang:
        if _is_python(shebang[0]):
            return [sys.executable]
        return [_find(shebang[0]), *shebang[1:]]
    kind = _BY_EXTENSION.get(PurePosixPath(rel).suffix.lower())
    if kind == "python":
        return [sys.executable]
    if kind == "bash":
        return [shutil.which("bash") or "/bin/sh"]
    if kind:
        return [_find(kind)]
    interpreters = manifest.requires.get("interpreters")
    if isinstance(interpreters, list) and len(interpreters) == 1 and isinstance(interpreters[0], str):
        name = _program(interpreters[0])
        if name:
            return [sys.executable] if _is_python(name) else [_find(name)]
    if rel in manifest.executable_paths():
        return []
    raise ConfigError(
        f"No se sabe cómo ejecutar {rel}: sin `#!`, con una extensión desconocida y sin declarar en `executable`.",
        hint="Añade un `#!` al script o decláralo en `executable` / `requires.interpreters` de baas.yaml.",
    )


def requires_warnings(manifest: package_fetch.Manifest) -> list[str]:
    """`baas.yaml.requires` es informativo: avisa de los binarios e intérpretes que no estén en el PATH."""
    warnings = []
    for key in ("interpreters", "binaries"):
        names = manifest.requires.get(key)
        for item in names if isinstance(names, list) else []:
            program = _program(item) if isinstance(item, str) else ""
            if program and not _is_python(program) and shutil.which(program) is None:
                warnings.append(f"AVISO: la skill «{manifest.skill}» requiere «{item}», que no está en el PATH.")
    return warnings


# --- ejecución -----------------------------------------------------------------------------------


def _child_env(skill_dir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env["BAAS_SCRIPT"] = "1"
    env["CLAUDE_SKILL_DIR"] = str(skill_dir)
    return env


def _exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def execute(package: Package, rel: str, args: list[str], *, cwd: Path) -> int:
    """Escribe el paquete en un directorio temporal privado, ejecuta `rel` y lo borra siempre."""
    manifest = package.manifest
    content = package.contents.get(rel)
    if content is None:
        raise ConfigError(f"El script {rel} no está en el paquete de «{manifest.skill}».")
    if not cwd.is_dir():
        raise ConfigError(f"El directorio de trabajo {cwd} no existe.")
    interpreter = resolve_interpreter(rel, content, manifest)
    for warning in requires_warnings(manifest):
        print(warning, file=sys.stderr)
    tmpdir = tempfile.mkdtemp(prefix="baas-script-")  # 0700
    previous: dict[int, Any] = {}

    def _on_signal(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)  # `subprocess.run` mata al script y el `finally` borra el temporal

    try:
        for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is not None:
                try:
                    previous[sig] = signal.signal(sig, _on_signal)
                except ValueError:  # fuera del hilo principal: sin manejador
                    break
        skill_dir = Path(tmpdir) / manifest.skill
        package_fetch.materialize(skill_dir, package.contents, manifest.executable_paths())
        target = skill_dir.joinpath(*rel.split("/"))
        res = subprocess.run([*interpreter, str(target), *args], cwd=cwd, env=_child_env(skill_dir), check=False)
        return _exit_code(res.returncode)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_script(
    mcp_url: str,
    api_key: str,
    path: str,
    *,
    args: list[str] | None = None,
    expected_sha256: str | None = None,
    org: str | None = None,
    cwd: Path,
) -> int:
    from .mcp_client import McpClient

    skill, rel = split_script_path(path)
    with McpClient(mcp_url, api_key) as client:
        manifest = package_fetch.fetch_manifest(client, skill, org)
        entry = manifest.file(rel)
        if entry is None:
            raise ConfigError(
                f"El script {rel} no está en el paquete de «{skill}».",
                hint="Consulta los ficheros con baas_list_skills o baas_get_skill_package.",
            )
        if expected_sha256 and entry.sha256 != expected_sha256.strip().lower():
            raise ScriptIntegrityError(
                f"El SHA-256 de {skill}/{rel} no es el esperado ({expected_sha256}); no se ejecuta.",
                hint="El script ha cambiado desde que se consultó la skill. Informa al usuario.",
            )
        package = Package(manifest, package_fetch.fetch_contents(client, manifest, org=org))
    return execute(package, rel, list(args or []), cwd=cwd)


def _synced_mcp_url(ns: argparse.Namespace) -> str | None:
    """El MCP de una skill nativa sincronizada (`--tool` y `--synced-org`), o `None` si no se pidió.

    Sale del fichero de control que escribió `sync --tool <herramienta>`, nunca de un argumento: una URL elegida por
    quien escribe el `SKILL.md` podría llevarse la clave. Solo vale para las skills sincronizadas de esa organización.
    """
    tool, synced = getattr(ns, "tool", None), getattr(ns, "synced_org", None)
    if not tool and not synced:
        return None
    if not (tool and synced):
        raise ConfigError("`--tool` y `--synced-org` van juntas.", hint="Las genera `sync --tool antigravity`.")
    if ns.org:
        raise ConfigError(
            "`--org` no se combina con `--synced-org`: el MCP es el de la organización sincronizada.",
            hint="Quita `--org`.",
        )
    from .native_skills import synced_server

    skill, _ = split_script_path(ns.path)
    return synced_server(tool, synced, skill)


def cmd_run_script(ns: argparse.Namespace) -> int:
    from . import project
    from .envfile import BaasPaths, load_credentials

    creds = load_credentials(BaasPaths.default())
    mcp_url = _synced_mcp_url(ns)
    if mcp_url is None:
        proj = project.resolve_project_dir(ns.project or os.environ.get("CLAUDE_PROJECT_DIR"), cwd=Path.cwd())
        binding = project.read_binding(proj)
        if not binding or not binding.get("mcp_url"):
            raise ConfigError(f"El proyecto {proj} no está vinculado a BaaS-AI-IP: lanza /baas-init.")
        mcp_url = str(binding["mcp_url"])
    return run_script(
        mcp_url,
        creds.api_key,
        ns.path,
        args=list(ns.script_args),
        expected_sha256=ns.sha256,
        org=ns.org,
        cwd=Path(ns.cwd).resolve() if ns.cwd else Path.cwd(),
    )


def add_run_script_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "run-script", help="Descarga el paquete de una skill del MCP, verifica los hashes, ejecuta un script y lo borra"
    )
    p.add_argument("path", help="Ruta del script tal como la devuelven baas_list_skills / baas_get_skill")
    p.add_argument("--sha256", help="SHA-256 esperado del script (p. ej. el que lista baas_get_skill)")
    p.add_argument("--org", help="Otra organización (exige ORG_ADMIN en ella o PLATFORM_ADMIN)")
    p.add_argument("--project", help="Raíz del proyecto (por defecto CLAUDE_PROJECT_DIR o la raíz git)")
    p.add_argument("--cwd", help="Directorio de trabajo del script (por defecto, el del proceso)")
    from .native_skills import TARGETS  # herramientas con skills nativas (T20): no necesitan un proyecto vinculado

    p.add_argument(
        "--tool",
        choices=sorted(TARGETS),
        help="Skill sincronizada como skill nativa de esta herramienta (con `--synced-org`; sin proyecto vinculado)",
    )
    p.add_argument(
        "--synced-org",
        metavar="ORG",
        help="Organización de la que se sincronizó la skill: el MCP sale de su fichero de control, no de un argumento",
    )
    p.epilog = (
        "Los argumentos para el script van al final, tras `--`. El código de salida es el del script; "
        "un error del plugin añade una línea BAAS_ERROR=<código> en stderr."
    )
    p.set_defaults(func=cmd_run_script)
