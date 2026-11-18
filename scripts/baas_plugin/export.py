"""`baas.py export <skill> <dir>`: la inversa de `publish` (diseño H5 §8).

Descarga el paquete de una skill (`baas_get_skill_package` + `baas_get_skill_files`), verifica el `sha256` de cada
fichero ANTES de dejar nada en su sitio, restaura el bit de ejecución y escribe con las rutas confinadas al
directorio de destino: se rechazan `..`, rutas absolutas, `\\`, caracteres de control y cualquier enlace simbólico
del destino. El directorio resultante es una skill válida en cualquier cliente (incluye `baas.yaml`, que los
clientes ignoran; `--no-manifest` lo omite).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError, McpToolError, ProjectSafetyError, ScriptIntegrityError
from .skilltree import decode_content, package_sha256, sha256_bytes

CHUNK_FILES = 200  # ficheros por llamada a baas_get_skill_files
CHUNK_BYTES = 32 * 1024 * 1024  # y bytes por llamada (un fichero mayor va solo: no hay tope por fichero)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE = re.compile(r"^[A-Za-z]:")


class ExportVerificationError(ScriptIntegrityError):
    """Un fichero descargado no coincide con el hash que declara el servidor."""

    marker = "BAAS_PACKAGE_HASH_MISMATCH"


@dataclass
class ExportOptions:
    skill: str
    dest: Path
    org: str | None = None
    force: bool = False
    include_manifest: bool = True


@dataclass
class ExportedFile:
    rel: str
    size: int
    sha256: str
    executable: bool


@dataclass
class ExportResult:
    skill: str
    dest: Path
    files: list[ExportedFile] = field(default_factory=list)
    package_sha256: str | None = None
    package_hash_ok: bool | None = None
    replaced: int = 0


def safe_relpath(path: str) -> str:
    """Ruta relativa y limpia, o `ProjectSafetyError`. Nunca sale del directorio de destino."""
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or _CONTROL.search(path)
        or _DRIVE.match(path)
        or any(seg in ("", ".", "..") for seg in path.split("/"))
    ):
        raise ProjectSafetyError(
            f"El paquete trae una ruta insegura: {path!r}. No se escribe nada.",
            hint="No se admiten rutas absolutas, con `..`, `\\` o caracteres de control. Informa al administrador.",
        )
    return path


def _relative_to_skill(skill: str, path: str) -> str:
    prefix = f"skills/{skill}/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _chunks(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for e in entries:
        if current and (len(current) >= CHUNK_FILES or size + int(e.get("size") or 0) > CHUNK_BYTES):
            out.append(current)
            current, size = [], 0
        current.append(e)
        size += int(e.get("size") or 0)
    if current:
        out.append(current)
    return out


def _args(org: str | None, **kw: Any) -> dict[str, Any]:
    out = {k: v for k, v in kw.items() if v is not None}
    if org:
        out["org"] = org
    return out


def _make_parents(path: Path) -> list[Path]:
    created: list[Path] = []
    missing = []
    cur = path
    while not cur.exists():
        missing.append(cur)
        cur = cur.parent
    for d in reversed(missing):
        d.mkdir()
        created.append(d)
    return created


def _check_no_symlinks(root: Path, rel: str) -> Path:
    """`root/rel` sin que ningún componente (del destino, ya existente) sea un enlace simbólico."""
    cur = root
    for part in rel.split("/"):
        cur = cur / part
        if cur.is_symlink():
            raise ProjectSafetyError(
                f"«{cur}» es un enlace simbólico: no se escribe a través de enlaces.",
                hint="Elige otro destino o quita el enlace.",
            )
    real = Path(os.path.realpath(cur))
    if not (real == Path(os.path.realpath(root)) or str(real).startswith(os.path.realpath(root) + os.sep)):
        raise ProjectSafetyError(f"«{rel}» saldría del directorio de destino. No se escribe nada.")
    return cur


def run_export(client: Any, opts: ExportOptions) -> ExportResult:
    dest = Path(opts.dest)
    if dest.exists() and not dest.is_dir():
        raise ConfigError(f"El destino «{dest}» existe y no es una carpeta.")
    pkg = client.call_tool("baas_get_skill_package", _args(opts.org, skill=opts.skill))
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in pkg.get("files") or []:
        rel = safe_relpath(_relative_to_skill(opts.skill, f["path"]))
        if rel in seen:
            raise ProjectSafetyError(f"El paquete repite la ruta {rel!r}. No se escribe nada.")
        seen.add(rel)
        if rel == "baas.yaml" and not opts.include_manifest:
            continue
        entries.append({**f, "rel": rel})
    if not any(e["rel"] == "SKILL.md" for e in entries):
        raise ConfigError(f"El paquete de «{opts.skill}» no contiene SKILL.md: no es una skill.")
    executable = {e["rel"] for e in entries if e.get("executable")}
    executable |= {safe_relpath(str(p)) for p in (pkg.get("metadata") or {}).get("executable") or []} & seen

    if dest.exists() and any(dest.iterdir()) and not opts.force:
        raise ConfigError(
            f"El destino «{dest}» no está vacío.",
            hint="Elige otra carpeta, o usa --force para escribir encima (lo que no es del paquete se respeta).",
        )
    created_parents = _make_parents(dest.parent)
    staging = Path(tempfile.mkdtemp(prefix=".baas-export-", dir=dest.parent))
    try:
        exported: list[ExportedFile] = []
        for group in _chunks(entries):
            reply = client.call_tool(
                "baas_get_skill_files", _args(opts.org, skill=opts.skill, paths=[e["path"] for e in group])
            )
            by_path = {f["path"]: f for f in reply.get("files") or []}
            for e in group:
                got = by_path.get(e["path"])
                if got is None:
                    raise McpToolError("NOT_FOUND", f"El MCP no devolvió {e['path']}.")
                data = decode_content(got["content"], got.get("encoding") or "utf8")
                digest = sha256_bytes(data)
                if digest != e["sha256"] or (got.get("sha256") and got["sha256"] != digest):
                    raise ExportVerificationError(
                        f"El SHA-256 de {e['rel']} no coincide con el que declara el MCP; no se escribe nada.",
                        hint=f"Declarado {e['sha256']}, calculado {digest}. Informa al administrador.",
                    )
                target = _check_no_symlinks(staging, e["rel"])
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise ProjectSafetyError(f"Conflicto de rutas en el paquete: {e['rel']}.")
                try:
                    target.write_bytes(data)
                except OSError as exc:
                    raise ProjectSafetyError(f"No se puede escribir {e['rel']}: {exc.strerror}.") from exc
                target.chmod(0o755 if e["rel"] in executable else 0o644)
                exported.append(ExportedFile(e["rel"], len(data), digest, e["rel"] in executable))
        replaced = _install(staging, dest, [f.rel for f in exported])
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        for d in reversed(created_parents):
            try:
                d.rmdir()
            except OSError:
                break
        raise
    hashes = {e["rel"]: e["sha256"] for e in entries}
    declared = pkg.get("package_sha256")
    ok: bool | None = None
    if declared:
        # el contrato cuenta las rutas completas `skills/<skill>/…` y deja fuera los `_acl.yaml`: se prueban variantes
        variants = []
        for keep_acl in (False, True):
            chosen = {r: h for r, h in hashes.items() if keep_acl or r.rsplit("/", 1)[-1] != "_acl.yaml"}
            variants.append(package_sha256({f"skills/{opts.skill}/{r}": h for r, h in chosen.items()}))
            variants.append(package_sha256(chosen))
        ok = declared in variants
    return ExportResult(opts.skill, dest, exported, declared, ok, replaced)


def _install(staging: Path, dest: Path, rels: list[str]) -> int:
    """Mueve lo verificado a su sitio: renombra si el destino no existe o está vacío; si no, fichero a fichero."""
    if not dest.exists():
        os.rename(staging, dest)
        return 0
    if not any(dest.iterdir()):
        dest.rmdir()
        os.rename(staging, dest)
        return 0
    replaced = 0
    real_dest = Path(os.path.realpath(dest))
    try:
        for rel in rels:
            target = _check_no_symlinks(real_dest, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                replaced += 1
            os.replace(staging / rel, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return replaced


def render_result(result: ExportResult) -> str:
    total = sum(f.size for f in result.files)
    out = [f"Skill «{result.skill}» exportada a {result.dest}: {len(result.files)} ficheros, {total} bytes."]
    out.append("Cada fichero se ha verificado contra su sha256.")
    if result.package_sha256:
        state = {True: "verificado", False: "no coincide con el calculado", None: "sin comprobar"}[
            result.package_hash_ok
        ]
        out.append(f"package_sha256 {result.package_sha256} ({state}).")
    exe = [f.rel for f in result.files if f.executable]
    if exe:
        out.append("Ejecutables restaurados: " + ", ".join(exe))
    if result.replaced:
        out.append(f"Sustituidos {result.replaced} ficheros que ya existían en el destino.")
    return "\n".join(out) + "\n"


def cmd_export(args: argparse.Namespace) -> int:
    from .mcp_client import McpClient
    from .skillcmds import connection

    mcp_url, api_key = connection(args.project)
    with McpClient(mcp_url, api_key, timeout=600) as client:
        result = run_export(
            client,
            ExportOptions(
                skill=args.skill,
                dest=Path(args.dir),
                org=args.org,
                force=args.force,
                include_manifest=not args.no_manifest,
            ),
        )
    if args.json:
        sys.stdout.write(
            json.dumps(
                {
                    "skill": result.skill,
                    "dest": str(result.dest),
                    "package_sha256": result.package_sha256,
                    "package_hash_ok": result.package_hash_ok,
                    "files": [
                        {"path": f.rel, "size": f.size, "sha256": f.sha256, "executable": f.executable}
                        for f in result.files
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    else:
        sys.stdout.write(render_result(result))
    return 0


def add_export_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "export",
        help="Exporta una skill del MCP a un directorio (inversa de publish), verificando cada hash",
        description="Descarga el paquete de una skill a <dir>: un directorio válido en cualquier cliente.",
    )
    p.add_argument("skill", help="Nombre de la skill en el MCP")
    p.add_argument("dir", help="Directorio de destino (no existe, o está vacío)")
    p.add_argument("--org", help="Otra organización (exige ORG_ADMIN en ella o PLATFORM_ADMIN)")
    p.add_argument(
        "--force", action="store_true", help="Escribir en un destino no vacío (lo que no es del paquete se respeta)"
    )
    p.add_argument("--no-manifest", action="store_true", help="No escribir baas.yaml")
    p.add_argument("--project", help="Raíz del proyecto (por defecto CLAUDE_PROJECT_DIR o la raíz git)")
    p.add_argument("--json", action="store_true", help="Resultado en JSON")
    p.set_defaults(func=cmd_export)
