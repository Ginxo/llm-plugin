"""`baas.py publish <dir>`: sube una skill (con sus scripts y su conocimiento) al MCP en UN lote atómico.

Parte mecánica de la publicación (diseño H5 §8); la entrevista la hace la skill `baas-publish`. Pasos:

1. Recorre el árbol (bytes exactos, `sha256`, `utf8` o `base64` según el contenido), sin salir nunca del árbol.
2. Genera o actualiza `baas.yaml` (roles, requisitos, dependencias, bits de ejecución), sin tocar el original.
3. Compara con lo publicado (`baas_get_skill_package`, `baas_list_files`) para enviar solo lo que cambia, con
   `expected_sha256` para no pisar cambios ajenos (`"none"` para lo nuevo) y, con `--prune`, `delete[]`.
4. Una sola llamada `baas_write_files` (con `dry_run: true` para ensayar) y verificación final por hash.

Sin límites de tamaño: solo avisa (LARGE_FILE desde 25 MiB, LARGE_PACKAGE desde 100 MiB).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import inspect as skill_inspect
from . import sensitive, skillmeta
from .errors import ConfigError, McpToolError, ScriptIntegrityError
from .skilltree import (
    LARGE_FILE_BYTES,
    LARGE_PACKAGE_BYTES,
    SKILL_NAME_RE,
    LocalFile,
    check_store_path,
    encode_content,
    scan_tree,
    sha256_bytes,
)

MAX_LISTED = 40


class PackageVerificationError(ScriptIntegrityError):
    """Tras escribir, lo que hay en el servidor no coincide con lo que se subió."""

    marker = "BAAS_PACKAGE_HASH_MISMATCH"


@dataclass
class PublishOptions:
    skill_dir: Path
    name: str | None = None
    org: str | None = None
    roles: list[str] | None = None
    scripts: list[Path] = field(default_factory=list)
    knowledge: list[tuple[Path, str]] = field(default_factory=list)
    knowledge_roles: list[str] | None = None
    depends_on_skills: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    default_excludes: bool = True
    prune: bool = False
    dry_run: bool = False
    rewrite_paths: bool = False
    include_secrets: bool = False


@dataclass
class PlanFile:
    path: str  # ruta en el almacén (`skills/<s>/…` o `knowledge/…`)
    data: bytes = field(repr=False)
    sha256: str
    size: int
    status: str  # new | changed | unchanged
    previous_sha256: str | None
    guard: str | None  # `expected_sha256` que se enviará
    origin: str
    area: str  # skill | scripts | knowledge | manifest | acl
    rewritten: bool = False


@dataclass
class Plan:
    skill: str
    org: str | None
    files: list[PlanFile]
    delete: list[str]
    omitted: list[dict[str, str]]
    warnings: list[str]
    manifest: dict[str, Any]
    knowledge_prefixes: list[str]
    remote_exists: bool
    rewrites_applied: list[dict[str, Any]] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        out = {"new": 0, "changed": 0, "unchanged": 0}
        for f in self.files:
            out[f.status] += 1
        return out

    @property
    def to_send(self) -> list[PlanFile]:
        return [f for f in self.files if f.status != "unchanged"]

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def upload_bytes(self) -> int:
        total = 0
        for f in self.to_send:
            content, _ = encode_content(f.data)
            total += len(content) + len(f.path) + 90
        return total

    @property
    def sync_bytes(self) -> int:
        """Lo que descarga cada sincronización: el paquete completo (el conocimiento no se copia)."""
        return sum(f.size for f in self.files if f.area in ("skill", "scripts", "manifest"))


@dataclass
class PublishResult:
    plan: Plan
    dry_run: bool
    written: bool = False
    server: dict[str, Any] = field(default_factory=dict)
    verified: bool | None = None
    package_sha256: str | None = None
    cancelled: bool = False
    extra_remote: list[str] = field(default_factory=list)


# --- argumentos ---------------------------------------------------------------------------------------


def normalize_prefix(prefix: str) -> str:
    """Prefijo bajo `knowledge/`: sin barras sobrantes y sin el `knowledge/` inicial (opcional)."""
    prefix = prefix.strip("/")
    if prefix == "knowledge":
        return ""
    return prefix.removeprefix("knowledge/")


def parse_knowledge_arg(raw: str) -> tuple[Path, str]:
    """`DIR:PREFIJO` → (DIR, prefijo bajo `knowledge/`)."""
    path, sep, prefix = raw.rpartition(":")
    if not sep or not path:
        raise ConfigError(
            f"--knowledge espera DIR:PREFIJO (recibido «{raw}»).",
            hint="Ejemplo: --knowledge ./knowledge/fiscal:fiscal (se sube bajo knowledge/fiscal/).",
        )
    return Path(path), normalize_prefix(prefix)


# --- el servidor ----------------------------------------------------------------------------------------


def _args(opts_org: str | None, **kw: Any) -> dict[str, Any]:
    out = {k: v for k, v in kw.items() if v is not None}
    if opts_org:
        out["org"] = opts_org
    return out


def _skill_path(skill: str, path: str) -> str:
    """La ruta de un fichero del paquete en forma de almacén (el contrato no fija si es relativa o completa)."""
    prefix = f"skills/{skill}/"
    return path if path.startswith(prefix) else prefix + path.lstrip("/")


def _remote_package(client: Any, skill: str, org: str | None) -> dict[str, Any] | None:
    try:
        return client.call_tool("baas_get_skill_package", _args(org, skill=skill))  # type: ignore[no-any-return]
    except McpToolError as exc:
        if exc.code == "NOT_FOUND":
            return None
        raise


def _remote_hashes(pkg: dict[str, Any] | None, skill: str) -> dict[str, str]:
    if pkg is None:
        return {}
    return {_skill_path(skill, f["path"]): f["sha256"] for f in pkg.get("files") or []}


def _list_files(client: Any, prefix: str, org: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    cursor: str | None = None
    while True:
        page = client.call_tool("baas_list_files", _args(org, prefix=prefix, cursor=cursor))
        for f in page.get("files") or []:
            out[f["path"]] = f["sha256"]
        cursor = page.get("next_cursor")
        if not cursor:
            return out


def describe_tool_error(exc: McpToolError) -> str:
    """Texto legible de un error del lote: lista TODOS los fallos o conflictos que devolvió el servidor."""
    payload = exc.payload or {}
    lines: list[str] = []
    if payload.get("failures"):
        lines.append("El MCP rechazó el lote y no ha escrito nada:")
        for f in payload["failures"]:
            where = f" [{f['field']}]" if f.get("field") else ""
            lines.append(f"  - {f.get('path', '')}{where}: {f.get('message') or f.get('code')}")
    elif payload.get("conflicts"):
        lines.append("Esos ficheros han cambiado en el servidor desde que se calculó el plan; no se ha escrito nada.")
        lines.append("Vuelve a lanzar publish para recalcularlo (nadie queda pisado):")
        for c in payload["conflicts"]:
            current = c.get("current_sha256") or "no existía"
            lines.append(f"  - {c['path']}: esperado {c.get('expected_sha256')}, ahora {current}")
    else:
        lines.append(exc.message.split(": ", 1)[-1])
    return "\n".join(lines)


def _enriched(exc: McpToolError) -> McpToolError:
    if not (exc.payload or {}).get("failures") and not (exc.payload or {}).get("conflicts"):
        return exc
    return McpToolError(exc.code, describe_tool_error(exc), exc.payload)


# --- plan -----------------------------------------------------------------------------------------------


def _check_name(name: str) -> None:
    if not SKILL_NAME_RE.match(name):
        raise ConfigError(
            f"El nombre de skill «{name}» no es válido.",
            hint="Letras, dígitos, `-`, `_` y `.` (máx. 128, sin `:`, espacios ni barras). Usa --name.",
        )


def _gather(
    opts: PublishOptions, name: str
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], list[str], str | None]:
    """Reúne los ficheros locales → `{ruta_almacén: {...}}`, omitidos, avisos y el `baas.yaml` local (texto)."""
    gathered: dict[str, dict[str, Any]] = {}
    omitted: list[dict[str, str]] = []
    warnings: list[str] = []
    local_manifest: str | None = None

    def add(store_path: str, lf: LocalFile, area: str, origin: str, rel_for_secret: str) -> None:
        base = rel_for_secret.rsplit("/", 1)[-1]
        reason = sensitive.sensitive_file_reason(base)
        if reason and not opts.include_secrets:
            omitted.append({"path": rel_for_secret, "reason": reason})
            return
        problem = check_store_path(store_path)
        if problem:
            raise ConfigError(f"La ruta «{store_path}» no es válida para el almacén: {problem}.")
        if store_path in gathered:
            if gathered[store_path]["lf"].sha256 == lf.sha256:
                return
            raise ConfigError(
                f"Dos ficheros distintos acaban en {store_path}: {gathered[store_path]['origin']} y {origin}.",
                hint="Renombra uno, o quita uno de los dos (--exclude).",
            )
        gathered[store_path] = {"lf": lf, "area": area, "origin": origin, "pkg": None}

    skill_scan = scan_tree(opts.skill_dir, excludes=opts.excludes, default_excludes=opts.default_excludes)
    for lf in skill_scan.files:
        if lf.rel == "baas.yaml":
            local_manifest = lf.read_bytes().decode("utf-8", "replace")
            continue
        add(f"skills/{name}/{lf.rel}", lf, "skill", lf.rel, lf.rel)
    for s in skill_scan.skipped:
        warnings.append(f"No se sube {s.rel}: {skill_inspect.SKIP_TEXT.get(s.reason, s.reason)}.")
    for path in opts.scripts:
        scan = scan_tree(path, excludes=opts.excludes, default_excludes=opts.default_excludes)
        for lf in scan.files:
            add(f"skills/{name}/scripts/{lf.rel}", lf, "scripts", f"scripts/{lf.rel} ({path})", f"scripts/{lf.rel}")
        for s in scan.skipped:
            warnings.append(f"No se sube {path}/{s.rel}: {skill_inspect.SKIP_TEXT.get(s.reason, s.reason)}.")
    for path, raw_prefix in opts.knowledge:
        prefix = normalize_prefix(raw_prefix)
        scan = scan_tree(path, excludes=opts.excludes, default_excludes=opts.default_excludes)
        base = f"knowledge/{prefix}/" if prefix else "knowledge/"
        for lf in scan.files:
            if lf.rel == "_acl.yaml" and opts.knowledge_roles is not None:
                continue  # se genera con --knowledge-roles
            add(base + lf.rel, lf, "knowledge", f"{base}{lf.rel}", base + lf.rel)
        for s in scan.skipped:
            warnings.append(f"No se sube {path}/{s.rel}: {skill_inspect.SKIP_TEXT.get(s.reason, s.reason)}.")
    return gathered, omitted, warnings, local_manifest


def build_plan(client: Any, opts: PublishOptions) -> Plan:
    skill_dir = Path(opts.skill_dir)
    if not (skill_dir / "SKILL.md").is_file():
        raise ConfigError(
            f"«{skill_dir}» no es una skill: no contiene SKILL.md.",
            hint="Indica la carpeta de la skill; los scripts y el conocimiento van en --include-scripts y --knowledge.",
        )
    name = opts.name or skill_dir.resolve().name
    _check_name(name)
    for label, paths in (("--include-scripts", opts.scripts), ("--knowledge", [p for p, _ in opts.knowledge])):
        for p in paths:
            if not Path(p).exists():
                raise ConfigError(f"{label}: «{p}» no existe.")

    gathered, omitted, warnings, local_manifest = _gather(opts, name)
    pkg_of = lambda store_path: store_path[len(f"skills/{name}/") :]  # noqa: E731
    skill_paths = {p for p in gathered if p.startswith(f"skills/{name}/")}

    # --- análisis (avisos, requisitos y reescrituras) ----------------------------------------------------
    rep = skill_inspect.inspect_skill(
        skill_dir,
        scripts=list(opts.scripts),
        knowledge=[p for p, _ in opts.knowledge],
        excludes=opts.excludes,
        default_excludes=opts.default_excludes,
    )
    contents: dict[str, bytes] = {p: g["lf"].read_bytes() for p, g in gathered.items()}
    rewritten_paths: set[str] = set()
    applied: list[dict[str, Any]] = []
    if opts.rewrite_paths:
        included = {pkg_of(p) for p in skill_paths}
        chosen = skill_inspect.filter_rewrites(rep["rewrites"], included)
        by_file: dict[str, list[dict[str, Any]]] = {}
        for r in chosen:
            by_file.setdefault(r["file"], []).append(r)
        for rel, rws in by_file.items():
            store_path = f"skills/{name}/{rel}"
            if store_path not in contents:
                continue
            try:
                text = contents[store_path].decode("utf-8")
            except UnicodeDecodeError:
                continue
            new = skill_inspect.apply_rewrites(text, rws)
            if new != text:
                contents[store_path] = new.encode("utf-8")
                rewritten_paths.add(store_path)
                applied.extend(rws)

    # --- manifiesto ---------------------------------------------------------------------------------------
    remote_pkg = _remote_package(client, name, opts.org)
    remote_skill = _remote_hashes(remote_pkg, name)
    base: dict[str, Any] | None = None
    if local_manifest is not None:
        try:
            base = skillmeta.parse_manifest(local_manifest)
        except skillmeta.miniyaml.YamlError as exc:
            raise ConfigError(
                f"El baas.yaml de la skill no es válido ({exc}).", hint="Corrígelo o bórralo; se regenera."
            ) from exc
    elif remote_pkg is not None and isinstance(remote_pkg.get("metadata"), dict):
        base = dict(remote_pkg["metadata"])
    uploaded_knowledge = {p for p, g in gathered.items() if g["area"] == "knowledge"}
    knowledge_deps = sorted(
        t["target"]
        for t in rep["links"]["knowledge"]
        if t["target"] in uploaded_knowledge
        or any(u.startswith(t["target"].rstrip("/") + "/") for u in uploaded_knowledge)
    )
    executable = sorted(pkg_of(p) for p in skill_paths if gathered[p]["lf"].exec_bit or contents[p].startswith(b"#!"))
    package_paths = {pkg_of(p) for p in skill_paths} | {"baas.yaml"}
    manifest, notes = skillmeta.merge_manifest(
        base,
        roles=opts.roles,
        interpreters=rep["manifest"]["requires"]["interpreters"],
        binaries=rep["manifest"]["requires"]["binaries"],
        depends_skills=rep["manifest"]["depends_on"]["skills"],
        extra_depends_skills=opts.depends_on_skills,
        depends_knowledge=knowledge_deps,
        executable=executable,
        package_paths=package_paths,
    )
    warnings.extend(notes)
    manifest_path = f"skills/{name}/baas.yaml"
    contents[manifest_path] = skillmeta.dump_manifest(manifest).encode("utf-8")
    gathered[manifest_path] = {"lf": None, "area": "manifest", "origin": "(generado)", "pkg": None}

    # --- _acl.yaml del conocimiento ----------------------------------------------------------------------
    prefixes = sorted({normalize_prefix(p) for _, p in opts.knowledge})
    if opts.knowledge_roles is not None:
        for prefix in prefixes:
            acl = f"knowledge/{prefix}/_acl.yaml" if prefix else "knowledge/_acl.yaml"
            contents[acl] = skillmeta.miniyaml.dumps({"roles": opts.knowledge_roles}).encode("utf-8")
            gathered[acl] = {"lf": None, "area": "acl", "origin": "(generado: --knowledge-roles)", "pkg": None}

    # --- lo que ya hay publicado ---------------------------------------------------------------------------
    remote_knowledge: dict[str, str] = {}
    for prefix in prefixes:
        remote_knowledge.update(_list_files(client, f"knowledge/{prefix}/" if prefix else "knowledge/", opts.org))
    remote = {**remote_skill, **remote_knowledge}
    files: list[PlanFile] = []
    for path in sorted(gathered):
        g = gathered[path]
        data = contents[path]
        digest = sha256_bytes(data)
        previous = remote.get(path)
        status = "new" if previous is None else "unchanged" if previous == digest else "changed"
        guard = previous if previous is not None else None if g["area"] == "acl" else "none"
        files.append(
            PlanFile(
                path, data, digest, len(data), status, previous, guard, g["origin"], g["area"], path in rewritten_paths
            )
        )

    local_paths = {f.path for f in files}
    stale = sorted(p for p in remote_skill if p not in local_paths)
    delete = stale if opts.prune else []
    if stale and not opts.prune:
        warnings.append(
            f"En el servidor hay {len(stale)} fichero(s) de la skill que ya no están en local "
            f"({', '.join(p.split('/', 2)[2] for p in stale[:5])}{'…' if len(stale) > 5 else ''}): "
            "usa --prune para retirarlos."
        )

    # --- avisos del cliente -----------------------------------------------------------------------------------
    for o in omitted:
        warnings.append(f"No se sube {o['path']} ({o['reason']}); usa --include-secrets si de verdad debe subirse.")
    sens = rep["sensitive"]
    if sens["items"]:
        kinds: dict[str, set[str]] = {}
        for item in sens["items"]:
            kinds.setdefault(item["kind"], set()).add(item["file"])
        detail = "; ".join(
            f"{k} en {', '.join(sorted(v)[:3])}{'…' if len(v) > 3 else ''}" for k, v in sorted(kinds.items())
        )
        warnings.append(
            f"Posibles datos sensibles ({detail}): se suben tal cual; valora moverlos a conocimiento restringido."
        )
    escaping = [e for e in rep["path_escapes"] if e["escapes"]]
    if escaping and not opts.rewrite_paths:
        warnings.append(
            f"{len(escaping)} ruta(s) calculadas desde __file__/BASH_SOURCE salen del paquete y fallarán en el equipo "
            "de quien use la skill (revisa `inspect`; --rewrite-paths propone una reescritura de la copia que se sube)."
        )
    for m in rep["scripts"]["missing"]:
        warnings.append(f"La skill cita {m}, que no está en el paquete (¿falta --include-scripts?).")
    for n in rep["nonportable"]:
        if n["kind"] in ("browser_snippet", "native_binary", "windows_only"):
            warnings.append(f"{n['file']}: {n['detail']}.")
    for f in files:
        if f.size > LARGE_FILE_BYTES:
            warnings.append(f"LARGE_FILE: {f.path} pesa {f.size / 1048576:.0f} MiB (se sube igualmente).")
    pkg_size = sum(f.size for f in files if f.area in ("skill", "scripts", "manifest"))
    if pkg_size > LARGE_PACKAGE_BYTES:
        warnings.append(
            f"LARGE_PACKAGE: el paquete pesa {pkg_size / 1048576:.0f} MiB; cada sincronización lo descarga."
        )
    if "roles" not in manifest:
        warnings.append("Sin roles: la skill quedará solo para administradores (usa --roles).")
    return Plan(
        skill=name,
        org=opts.org,
        files=files,
        delete=delete,
        omitted=omitted,
        warnings=warnings,
        manifest=manifest,
        knowledge_prefixes=prefixes,
        remote_exists=remote_pkg is not None,
        rewrites_applied=applied,
    )


# --- escribir y verificar ----------------------------------------------------------------------------------


def write_plan(client: Any, plan: Plan, *, dry_run: bool) -> dict[str, Any]:
    """UNA llamada `baas_write_files` con todo lo que cambia (y `delete[]`). Sin cambios: no llama."""
    if not plan.to_send and not plan.delete:
        return {}
    sent = []
    for f in plan.to_send:
        content, encoding = encode_content(f.data)
        entry: dict[str, Any] = {"path": f.path, "content": content, "encoding": encoding}
        if f.guard:
            entry["expected_sha256"] = f.guard
        sent.append(entry)
    arguments = _args(plan.org, files=sent, dry_run=dry_run)
    if plan.delete:
        arguments["delete"] = plan.delete
    return client.call_tool("baas_write_files", arguments)  # type: ignore[no-any-return]


def verify_plan(client: Any, plan: Plan) -> tuple[list[str], str | None]:
    """Compara lo publicado con lo local, fichero a fichero. Devuelve (discrepancias, package_sha256)."""
    problems: list[str] = []
    pkg = _remote_package(client, plan.skill, plan.org)
    if pkg is None:
        return [f"La skill «{plan.skill}» no existe en el servidor tras publicar."], None
    remote = _remote_hashes(pkg, plan.skill)
    for f in plan.files:
        if f.area == "knowledge" or f.area == "acl":
            continue
        if remote.get(f.path) is None:
            problems.append(f"{f.path}: falta en el servidor")
        elif remote[f.path] != f.sha256:
            problems.append(f"{f.path}: el hash del servidor no coincide con el subido")
    for path in plan.delete:
        if path in remote:
            problems.append(f"{path}: debía retirarse y sigue en el servidor")
    knowledge = [f for f in plan.files if f.area == "knowledge"]
    if knowledge:
        listed: dict[str, str] = {}
        for prefix in plan.knowledge_prefixes:
            listed.update(_list_files(client, f"knowledge/{prefix}/" if prefix else "knowledge/", plan.org))
        for f in knowledge:
            if listed.get(f.path) != f.sha256:
                problems.append(f"{f.path}: el hash del servidor no coincide con el subido")
    return problems, pkg.get("package_sha256")


def run_publish(
    client: Any,
    opts: PublishOptions,
    *,
    confirm: Callable[[PublishResult], bool] | None = None,
) -> PublishResult:
    """Plan → (ensayo y confirmación) → lote único → verificación. Con `dry_run` solo ensaya."""
    try:
        plan = build_plan(client, opts)
        if opts.dry_run:
            return PublishResult(plan, True, server=write_plan(client, plan, dry_run=True))
        if confirm is not None:
            preview = PublishResult(plan, True, server=write_plan(client, plan, dry_run=True))
            if not confirm(preview):
                preview.cancelled = True
                return preview
        server = write_plan(client, plan, dry_run=False)
    except McpToolError as exc:
        raise _enriched(exc) from exc
    problems, package_hash = verify_plan(client, plan)
    if problems:
        raise PackageVerificationError(
            "La verificación final falló: " + "; ".join(problems[:5]) + ("…" if len(problems) > 5 else "") + ".",
            hint="El servidor no guarda lo mismo que se subió. Informa al administrador de la plataforma.",
        )
    extra = []
    if not opts.prune:
        pkg = _remote_package(client, plan.skill, plan.org)
        local = {f.path for f in plan.files}
        extra = sorted(p for p in _remote_hashes(pkg, plan.skill) if p not in local)
    return PublishResult(
        plan,
        False,
        written=bool(server),
        server=server,
        verified=True,
        package_sha256=package_hash,
        extra_remote=extra,
    )


# --- presentación ----------------------------------------------------------------------------------------------


def _size(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.1f} MiB"
    if n >= 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n} B"


def render_plan(result: PublishResult) -> str:
    plan = result.plan
    c = plan.counts
    where = f" en la organización «{plan.org}»" if plan.org else ""
    state = (
        "ENSAYO (dry-run: no se ha escrito nada)"
        if result.dry_run
        else "PUBLICADA"
        if result.written
        else "SIN CAMBIOS"
    )
    out = [f"Skill «{plan.skill}»{where}: {state}", ""]
    out.append(
        f"Ficheros: {len(plan.files)} ({c['new']} nuevos, {c['changed']} cambiados, {c['unchanged']} sin cambios), "
        f"{_size(plan.total_bytes)} en total."
    )
    if not plan.to_send and not plan.delete:
        out.append("Todo está al día: sin cambios que publicar.")
    else:
        out.append(
            f"Tráfico estimado: subida de {_size(plan.upload_bytes)} (en un solo lote); "
            f"cada sincronización de un usuario descarga {_size(plan.sync_bytes)}."
        )
    roles = plan.manifest.get("roles")
    out.append(f"Roles: {', '.join(roles) if roles else 'ninguno (solo administradores)'}")
    deps = (plan.manifest.get("depends_on") or {}).get("skills")
    if deps:
        out.append(f"Depende de las skills: {', '.join(deps)}")
    req = plan.manifest.get("requires") or {}
    if req:
        out.append("Requiere: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in req.items()))
    if plan.manifest.get("executable"):
        out.append(f"Ejecutables: {', '.join(plan.manifest['executable'])}")
    rewrites = [f for f in plan.files if f.rewritten]
    if rewrites:
        out.append(f"Reescritas (solo la copia que se sube): {', '.join(f.path.split('/', 2)[-1] for f in rewrites)}")
    out += ["", "Se enviarán:" if result.dry_run else "Enviados:"]
    sendable = plan.to_send
    for f in sendable[:MAX_LISTED]:
        mark = "+" if f.status == "new" else "~"
        out.append(f"  {mark} {f.path}  ({_size(f.size)}{', reescrito' if f.rewritten else ''})")
    if len(sendable) > MAX_LISTED:
        out.append(f"  … y {len(sendable) - MAX_LISTED} más")
    if not sendable:
        out.append("  (nada)")
    for path in plan.delete:
        out.append(f"  - {path}  (se retira; la versión anterior se conserva)")
    if plan.omitted:
        out.append("No se suben: " + ", ".join(f"{o['path']} ({o['reason']})" for o in plan.omitted))
    warnings = list(plan.warnings)
    for w in result.server.get("warnings") or []:
        where_w = f" ({w['path']})" if w.get("path") else ""
        warnings.append(f"{w['code']}{where_w}: {w['message']}")
    if warnings:
        out += ["", "Avisos:"] + [f"  - {w}" for w in warnings]
    if result.verified:
        out += [
            "",
            f"Verificado: los hashes del servidor coinciden con lo subido (package_sha256 {result.package_sha256}).",
        ]
    if result.extra_remote:
        out.append(
            f"En el servidor quedan {len(result.extra_remote)} fichero(s) que ya no están en local (usa --prune)."
        )
    return "\n".join(out) + "\n"


def result_to_json(result: PublishResult) -> dict[str, Any]:
    plan = result.plan
    return {
        "ok": not result.cancelled,
        "skill": plan.skill,
        "org": plan.org,
        "dry_run": result.dry_run,
        "written": result.written,
        "cancelled": result.cancelled,
        "verified": result.verified,
        "package_sha256": result.package_sha256,
        "files": [
            {
                "path": f.path,
                "status": f.status,
                "size": f.size,
                "sha256": f.sha256,
                "area": f.area,
                "origin": f.origin,
                "rewritten": f.rewritten,
            }
            for f in plan.files
        ],
        "delete": plan.delete,
        "omitted": plan.omitted,
        "summary": {
            "files": len(plan.files),
            **plan.counts,
            "bytes": plan.total_bytes,
            "upload_bytes": plan.upload_bytes,
            "estimated_sync_bytes": plan.sync_bytes,
        },
        "manifest": plan.manifest,
        "warnings": plan.warnings,
        "server": {k: result.server.get(k) for k in ("summary", "warnings", "packages") if result.server},
    }


# --- CLI ---------------------------------------------------------------------------------------------------------


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _confirm_interactively(preview: PublishResult) -> bool:
    sys.stdout.write(render_plan(preview))
    if not sys.stdin.isatty():
        raise ConfigError(
            "Falta la confirmación: no hay terminal interactiva para preguntar.",
            hint="Revisa el plan con --dry-run y vuelve a lanzar con --yes para publicar.",
        )
    answer = input("¿Publicar ahora? [s/N] ").strip().lower()
    return answer in ("s", "si", "sí", "y", "yes")


def cmd_publish(args: argparse.Namespace) -> int:
    from .mcp_client import McpClient
    from .skillcmds import connection

    roles = skillmeta_roles(args.roles)
    knowledge_roles = skillmeta_roles(args.knowledge_roles)
    options = PublishOptions(
        skill_dir=Path(args.dir),
        name=args.name,
        org=args.org,
        roles=roles,
        scripts=[Path(p) for p in args.include_scripts],
        knowledge=[parse_knowledge_arg(k) for k in args.knowledge],
        knowledge_roles=knowledge_roles,
        depends_on_skills=_csv(args.depends_on_skills),
        excludes=args.exclude,
        default_excludes=not args.no_default_excludes,
        prune=args.prune,
        dry_run=args.dry_run,
        rewrite_paths=args.rewrite_paths,
        include_secrets=args.include_secrets,
    )
    if args.json and not (args.yes or args.dry_run):
        raise ConfigError("--json no puede preguntar: combínalo con --dry-run o con --yes.")
    mcp_url, api_key = connection(args.project)
    with McpClient(mcp_url, api_key, timeout=600) as client:
        result = run_publish(
            client, options, confirm=None if (args.yes or args.dry_run or args.json) else _confirm_interactively
        )
    if args.json:
        sys.stdout.write(json.dumps(result_to_json(result), ensure_ascii=False, indent=2) + "\n")
        return 0
    if result.cancelled:
        sys.stdout.write("Cancelado: no se ha publicado nada.\n")
        return 1
    sys.stdout.write(render_plan(result))
    return 0


def skillmeta_roles(value: str | None) -> list[str] | None:
    try:
        return skillmeta.parse_roles(value)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def add_publish_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "publish",
        help="Publica una skill (con scripts y conocimiento) en el MCP, en un único lote atómico",
        description="Sube una carpeta de skill al MCP de la organización (solo ORG_ADMIN o PLATFORM_ADMIN).",
        epilog="Empieza siempre con --dry-run: no escribe nada y muestra ficheros, tamaño, tráfico y avisos.",
    )
    p.add_argument("dir", help="Carpeta de la skill (la que contiene SKILL.md)")
    p.add_argument("--name", help="Nombre de la skill en el MCP (por defecto, el de la carpeta)")
    p.add_argument("--org", help="Otra organización (exige ORG_ADMIN en ella o PLATFORM_ADMIN)")
    p.add_argument(
        "--roles", help="Roles con acceso: R1,R2 o '*' (cualquier miembro). Sin él se conservan los publicados"
    )
    p.add_argument(
        "--include-scripts",
        action="append",
        default=[],
        metavar="RUTA",
        help="Script o carpeta de scripts de fuera de la skill (repetible); viajan en scripts/",
    )
    p.add_argument(
        "--knowledge",
        action="append",
        default=[],
        metavar="DIR:PREFIJO",
        help="Conocimiento que se sube bajo knowledge/<PREFIJO>/ (repetible)",
    )
    p.add_argument(
        "--knowledge-roles",
        metavar="R1,R2",
        help="Roles del conocimiento subido (escribe un _acl.yaml en cada PREFIJO)",
    )
    p.add_argument("--depends-on-skills", metavar="S1,S2", help="Skills de las que depende, además de las detectadas")
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATRÓN",
        help="Excluye lo que coincida (glob; `dir/` = solo carpetas)",
    )
    p.add_argument("--no-default-excludes", action="store_true", help="No ignorar .git, __pycache__, node_modules…")
    p.add_argument(
        "--prune", action="store_true", help="Retira del servidor los ficheros de la skill que ya no están en local"
    )
    p.add_argument("--dry-run", action="store_true", help="Ensaya: valida y calcula en el servidor sin escribir nada")
    p.add_argument("--yes", action="store_true", help="No pedir confirmación")
    p.add_argument(
        "--rewrite-paths",
        action="store_true",
        help="Aplica a la copia que se sube las reescrituras que propone `inspect`",
    )
    p.add_argument(
        "--include-secrets", action="store_true", help="Sube también .env, claves y similares (por defecto se omiten)"
    )
    p.add_argument("--project", help="Raíz del proyecto (por defecto CLAUDE_PROJECT_DIR o la raíz git)")
    p.add_argument("--json", action="store_true", help="Resultado en JSON")
    p.set_defaults(func=cmd_publish)
