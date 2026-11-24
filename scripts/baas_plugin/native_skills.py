"""Skills de la organización como skills nativas de otra herramienta (T19, #192): `baas.py sync --tool antigravity`.

En Claude Code las skills de la organización salen como `/<organización>:<skill>` porque el MCP las publica como
*prompts* y `sync` instala un plugin. Antigravity no documenta los *prompts* como comandos: sus skills son carpetas con
un `SKILL.md` (`~/.gemini/config/skills/<carpeta>/` en el ámbito global, que leen la CLI `agy` y la extensión) y la CLI
convierte cada una en `/<nombre>`. Este módulo deja ahí las skills accesibles de la organización.

Reutiliza lo del `sync` de H5: la lista de skills y sus dependencias (`sync.list_skills`, `sync.resolve_plans`),
el manifiesto y el SHA-256 de cada fichero (`package_fetch`), el confinamiento de rutas, la escritura en un temporal
y el renombrado atómico (`sync.swap_dir`), el bloqueo, y la revocación (lo que deja de ser accesible se retira).

Lo que añade, porque aquí el destino es una carpeta **del usuario** y no un plugin propio:

- **Nunca pisa lo del usuario.** Lo que el plugin gestiona se anota en `.baas-skills.json` (sin secretos: organización,
  `package_sha256` y SHA-256 de lo escrito de cada skill). Una carpeta con el nombre de una skill de la organización que
  no está ahí es del usuario: se deja como está y se cuenta (choque de nombre). Una skill gestionada que el usuario ha
  editado (o a la que ha añadido ficheros o enlaces) tampoco se sobrescribe ni se retira, salvo con `--force`.
- **Nombres.** La carpeta es el nombre del comando, sin prefijo de organización (así los enlaces `../<otra>/SKILL.md`
  y las citas `/<otra>` de las propias skills siguen valiendo) y sin `:` (Windows). Solo minúsculas, dígitos y guiones;
  lo demás no se instala y se cuenta. El global solo admite una organización: al cambiar de organización se retiran las
  skills de la anterior.
- **Sin scripts.** No se copia nada que pueda ejecutarse sin la comprobación que hace el plugin al ejecutar
  (`run-script`): ficheros marcados como ejecutables, carpetas `scripts/`, extensiones de código o binarios y todo lo
  que empiece por `#!`. Se listan y se avisa de que esa skill no funcionará completa en la herramienta.
- **`SKILL.md` adaptado.** Antigravity documenta `description` (obligatorio) y `name` (opcional): se escriben solo esos
  dos (`name` = la carpeta; `when_to_use` se une a la descripción) y el cuerpo queda tal cual. Los campos propios de
  Claude Code se quitan y se avisa; una skill que pide invocación solo manual (`disable-model-invocation`) no se
  instala porque Antigravity no puede respetarlo.

Solo toca `~/.gemini/config/skills/` (y `~/baas-ai-ip/sync.lock`). Un cambio se ve con `/skills reload` en `agy`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from . import claude_home, handshake, package_fetch, setup_tools, skillmeta, sync
from .envfile import BaasPaths
from .errors import (
    BaasError,
    ConfigError,
    ForeignDirectoryError,
    McpToolError,
    PackageError,
    ProjectSafetyError,
    ScriptIntegrityError,
    SyncIncompleteError,
)
from .package_fetch import Manifest

CONTROL_FILE = ".baas-skills.json"
CONTROL_FORMAT = 1
MANAGED_BY = "baas-llm-plugin"
NATIVE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_MD = 1024 * 1024  # un SKILL.md más grande no es una skill razonable (y se lee entero en memoria)
SHOWN_OMITTED = 8  # ficheros omitidos que se nombran en pantalla (todos quedan en el fichero de control)
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)
# Código y binarios ejecutables: no se copian (ni siquiera sin el bit de ejecución: `python3 x.py` no lo necesita)
EXECUTABLE_EXTENSIONS = frozenset(
    {
        ".py", ".pyw", ".sh", ".bash", ".zsh", ".fish", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php", ".lua",
        ".ps1", ".psm1", ".bat", ".cmd", ".vbs", ".wsf", ".exe", ".dll", ".so", ".dylib", ".msi", ".jar", ".com",
        ".scr", ".appimage",
    }
)  # fmt: skip
FRONTMATTER_KEPT = ("name", "description", "when_to_use")


@dataclass(frozen=True)
class NativeSkillTarget:
    """Dónde y cómo se dejan las skills nativas de una herramienta (solo datos)."""

    name: str  # valor de `--tool`
    label: str
    global_dir: tuple[str, ...]  # relativo al HOME; piezas, no una ruta POSIX
    command_names: frozenset[str]  # comandos propios de la herramienta que una skill con ese nombre taparía
    reload_hint: str
    doc: str = "docs/antigravity.md"


ANTIGRAVITY = NativeSkillTarget(
    name="antigravity",
    label="Antigravity",
    global_dir=(".gemini", "config", "skills"),
    command_names=frozenset({"mcp", "skills"}),  # los de la CLI que documenta Antigravity; la lista no es exhaustiva
    reload_hint=(
        "En `agy`, ejecuta `/skills reload` o abre una conversación nueva; cada skill se invoca como `/<nombre>`."
        " Cómo las ofrece la extensión de VS Code no está comprobado."
    ),
)
TARGETS: dict[str, NativeSkillTarget] = {t.name: t for t in (ANTIGRAVITY,)}


# --- qué se instala y cómo ------------------------------------------------------------------------


def name_problem(target: NativeSkillTarget, name: str) -> str | None:
    """Por qué `name` no puede ser el nombre de una skill nativa (y, con él, el del comando), o `None`."""
    if not NATIVE_NAME_RE.match(name):
        return f"{target.label} espera nombres en minúsculas, dígitos y guiones, y «{name}» no lo es"
    if name in WINDOWS_RESERVED:
        return f"«{name}» es un nombre reservado de Windows"
    if name in target.command_names:
        return f"«{name}» taparía un comando de {target.label} (`/{name}`)"
    return None


def omission_reason(path: str, executable: bool) -> str | None:
    """Por qué un fichero del paquete no se copia (por su ruta y su marca), o `None`."""
    if executable:
        return "marcado como ejecutable"
    parts = [p.lower() for p in path.split("/")]
    if "scripts" in parts[:-1]:
        return "carpeta scripts/"
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in EXECUTABLE_EXTENSIONS:
        return f"código o binario ejecutable ({suffix})"
    return None


def has_shebang(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(2) == b"#!"


class NotAdaptable(Exception):  # noqa: N818 (no es un error: la skill se deja fuera y se cuenta)
    """La skill no se puede dejar como skill nativa sin cambiar lo que su autor pidió."""


def _yaml_string(value: str) -> str:
    """Una cadena YAML entre comillas dobles (JSON es YAML válido); sin saltos de línea Unicode sueltos."""
    text = json.dumps(value, ensure_ascii=False)
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").replace("\x85", "\\x85")


def adapt_skill_md(text: str, name: str) -> tuple[str, list[str]]:
    """El `SKILL.md` con la cabecera de Antigravity (`name` y `description`) y el cuerpo intacto.

    Devuelve `(texto, campos de Claude Code que se han quitado)`. Lanza `PackageError` si no tiene una cabecera o una
    `description` válidas y `NotAdaptable` si pide invocación solo manual.
    """
    text = text.removeprefix("\ufeff")
    fm = skillmeta.parse_frontmatter(text)
    if not fm.present:
        raise PackageError("el SKILL.md no tiene la cabecera YAML (--- … ---) con `description`.")
    if not fm.valid_yaml:
        raise PackageError(f"la cabecera YAML del SKILL.md no es válida ({fm.error}).")
    data = fm.data
    manual = data.get("disable-model-invocation")
    if manual is True or str(manual).strip().lower() == "true":
        raise NotAdaptable(
            "pide invocación solo manual (`disable-model-invocation`) y Antigravity no puede respetarlo: el agente"
            " la usaría por su cuenta"
        )
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise PackageError("el SKILL.md no tiene `description` (Antigravity la exige).")
    when = data.get("when_to_use")
    if isinstance(when, str) and when.strip():
        description = f"{description} {when}"
    description = " ".join(description.split())
    _, body = skillmeta.split_frontmatter(text)
    adapted = f"---\nname: {name}\ndescription: {_yaml_string(description)}\n---\n{body}"
    dropped = [str(k) for k in data if k not in FRONTMATTER_KEPT]
    return adapted, dropped


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_records(base: Path) -> list[dict[str, str]]:
    """`[{path, sha256}]` de todos los ficheros bajo `base` (sin seguir enlaces)."""
    records = []
    for dirpath, _dirs, files in os.walk(base):
        for f in files:
            p = Path(dirpath) / f
            records.append({"path": p.relative_to(base).as_posix(), "sha256": _file_sha256(p)})
    return sorted(records, key=lambda r: r["path"])


def local_changes(folder: Path, entry: dict[str, Any]) -> str | None:
    """Por qué la carpeta de una skill gestionada ya no es lo que el plugin escribió, o `None` si está intacta.

    Compara con lo registrado (SHA-256 de cada fichero) y no sigue enlaces simbólicos.
    """
    if folder.is_symlink():
        return "ahora es un enlace simbólico"
    if not folder.is_dir():
        return "ya no es una carpeta"
    recorded = {
        f["path"]: f["sha256"]
        for f in entry.get("files") or []
        if isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("sha256"), str)
    }
    if not recorded:
        return "no hay registro de sus ficheros"
    seen: dict[str, str] = {}
    for dirpath, dirs, files in os.walk(folder):
        for entry_name in [*dirs, *files]:
            p = Path(dirpath) / entry_name
            if p.is_symlink():
                return f"contiene un enlace simbólico ({p.relative_to(folder).as_posix()})"
        for f in files:
            p = Path(dirpath) / f
            seen[p.relative_to(folder).as_posix()] = _file_sha256(p)
    for rel in sorted(seen):
        if rel in recorded and seen[rel] != recorded[rel]:
            return f"has editado {rel}"
    for rel in sorted(set(recorded) - set(seen)):
        return f"falta {rel}"
    for rel in sorted(set(seen) - set(recorded)):
        return f"has añadido {rel}"
    return None


# --- estado: .baas-skills.json -----------------------------------------------------------------------


def read_control(root: Path) -> dict[str, Any] | None:
    """El fichero de control, `None` si no existe. Si no es nuestro o no se entiende, se aborta sin tocar nada."""
    path = root / CONTROL_FILE

    def foreign(why: str) -> ForeignDirectoryError:
        return ForeignDirectoryError(
            f"{path} {why}: no sé qué skills de esta carpeta gestiona BaaS y no se toca nada.",
            hint="Revisa o borra ese fichero (las skills ya copiadas quedarán como tuyas) y repite el comando.",
        )

    if path.is_symlink():
        raise foreign("es un enlace simbólico")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise foreign("no es JSON válido") from None
    if not isinstance(data, dict) or data.get("managed_by") != MANAGED_BY:
        raise foreign("no lo escribió este plugin")
    return data


def write_control(root: Path, org: str, mcp_url: str, skills: dict[str, Any], warnings: list[str]) -> None:
    data = {
        "managed_by": MANAGED_BY,
        "format": CONTROL_FORMAT,
        "org": org,
        "server": mcp_url,
        "synced_at": sync.now_iso(),
        "skills": skills,
        "warnings": warnings,
    }
    claude_home.write_json_atomic(root / CONTROL_FILE, data)


# --- resultado --------------------------------------------------------------------------------------


@dataclass
class NativeResult(sync.SyncResult):
    tool: NativeSkillTarget = ANTIGRAVITY
    collisions: dict[str, str] = field(default_factory=dict)  # skill de la organización → por qué choca
    modified: dict[str, str] = field(default_factory=dict)  # skill gestionada editada por el usuario → qué
    excluded: dict[str, str] = field(default_factory=dict)  # skill que no se instala → por qué
    omitted: dict[str, list[dict[str, str]]] = field(default_factory=dict)  # skill → ficheros que no se copian

    def render(self) -> list[str]:
        if self.skipped:
            return [self.skipped]
        lines = []
        if self.dry_run:
            lines.append("Simulación (--dry-run): no se ha escrito nada. Esto es lo que se haría:")
        lines.append(f"Organización «{self.org}»: skills nativas de {self.tool.label} en {self.plugin_dir}.")
        if self.kill_switch is not None:
            lines.append(
                f"{sync.KILL_SWITCH} ({self.kill_switch or '?'}): el acceso de la organización está cortado; "
                "se retiran todas las skills sincronizadas."
            )
        for mark, names, label in (
            ("+", self.added, "nueva"),
            ("~", self.updated, "actualizada"),
            ("-", self.removed, "retirada"),
        ):
            lines += [f"  {mark} {n} ({label}{self.details.get(n, '')})" for n in names]
        lines += [f"  ! {n}: NO instalada ({why})" for n, why in sorted(self.failed.items())]
        lines += [
            f"  = {n}: {why}: no la toco (renombra la tuya si quieres la de la organización)"
            for n, why in sorted(self.collisions.items())
        ]
        lines += [
            f"  ? {n}: modificada en tu equipo ({why}): no la toco (`--force` la sobrescribe o la retira)"
            for n, why in sorted(self.modified.items())
        ]
        lines += [f"  · {n}: no se instala ({why})" for n, why in sorted(self.excluded.items())]
        for name, files in sorted(self.omitted.items()):
            shown = ", ".join(f["path"] for f in files[:SHOWN_OMITTED])
            more = f" y {len(files) - SHOWN_OMITTED} más" if len(files) > SHOWN_OMITTED else ""
            lines.append(
                f"AVISO: «{name}»: no se copian {len(files)} fichero(s) ejecutables o scripts ({shown}{more}): esta"
                f" skill no funcionará completa en {self.tool.label}."
            )
        lines += [f"AVISO: {w}" for w in self.warnings]
        lines += self.notes
        problems = self.failed or self.collisions or self.modified or self.excluded
        if not (self.changed or problems or self.kill_switch is not None):
            count = len(self.unchanged)
            lines.append(f"Todo al día: sin cambios ({count} {'skill' if count == 1 else 'skills'} sincronizadas).")
        elif not self.dry_run and self.changed:
            lines.append(self.tool.reload_hint)
        return lines


# --- sincronización ----------------------------------------------------------------------------------


def _stage_skill(
    client: Any, tmp_root: Path, name: str, manifest: Manifest
) -> tuple[Path, list[dict[str, str]], list[dict[str, str]], list[str]]:
    """Descarga lo copiable de la skill a un temporal y la deja lista para renombrar.

    Devuelve `(carpeta, ficheros escritos con su SHA-256, ficheros omitidos con el motivo, campos quitados)`.
    """
    kept, omitted = [], []
    for f in manifest.files:
        reason = omission_reason(f.path, f.executable)
        if reason:
            omitted.append({"path": f.path, "reason": reason})
        else:
            kept.append(f)
    skill_md = next((f for f in kept if f.path == "SKILL.md"), None)
    if skill_md is None:
        raise PackageError("el paquete no tiene SKILL.md.")
    if skill_md.size > MAX_SKILL_MD:
        raise PackageError(f"el SKILL.md es demasiado grande ({skill_md.size} bytes; el máximo es {MAX_SKILL_MD}).")
    staged = tmp_root / f"new-{name}"
    # El SHA-256 de cada fichero se verifica al descargarlo (`package_fetch`); solo se pide lo que se va a copiar.
    package_fetch.fetch_to_dir(client, dataclasses.replace(manifest, files=kept), staged)
    for f in kept:  # un script con otro nombre (`#!` al principio) tampoco se copia
        path = staged.joinpath(*f.path.split("/"))
        if f.path != "SKILL.md" and has_shebang(path):
            path.unlink()
            omitted.append({"path": f.path, "reason": "empieza por #!"})
    for dirpath, _dirs, _files in os.walk(staged, topdown=False):
        if dirpath != str(staged) and not any(Path(dirpath).iterdir()):
            os.rmdir(dirpath)
    md_path = staged / "SKILL.md"
    try:
        text = md_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise PackageError("el SKILL.md no es UTF-8.") from None
    adapted, dropped = adapt_skill_md(text, name)
    md_path.write_bytes(adapted.encode("utf-8"))
    return staged, tree_records(staged), sorted(omitted, key=lambda o: o["path"]), dropped


def _managed_state(skills: dict[str, dict[str, Any]], name: str, plan: sync.SkillPlan, **extra: Any) -> None:
    skills[name] = {"package_sha256": plan.package_sha256, "depends_on": plan.depends_on, **extra}


def sync_native(
    target: NativeSkillTarget,
    org: handshake.Organization,
    api_key: str,
    baas: BaasPaths,
    opts: sync.SyncOptions,
    root: Path,
) -> NativeResult:
    from .mcp_client import McpClient

    result = NativeResult(org.slug, root, dry_run=opts.dry_run, tool=target)
    with sync._lock(baas, enabled=not opts.dry_run) as acquired:
        if not acquired:
            result.skipped = "Otra sincronización está en curso; no se hace nada. Vuelve a intentarlo en unos segundos."
            return result
        if root.exists() and not root.is_dir():
            raise ConfigError(f"{root} existe y no es una carpeta.", hint="Muévelo y repite el comando.")
        state = read_control(root) if root.is_dir() else None
        managed = sync.local_skills(state)
        with McpClient(org.mcp_url, api_key, timeout=opts.timeout) as client:  # sin MCP no se ha tocado nada
            plans: dict[str, sync.SkillPlan] = {}
            try:
                listed = sync.list_skills(client, result.notes)
                for name in sorted(listed):
                    problem = name_problem(target, name)
                    if problem:
                        result.excluded[name] = problem
                        del listed[name]
                plans = sync.resolve_plans(client, listed, managed, root, opts, result, skills_dir=root)
                for name in sorted(plans):  # una dependencia con un nombre que la herramienta no admite
                    problem = name_problem(target, name)
                    if problem:
                        result.excluded[name] = problem
                        del plans[name]
            except McpToolError as exc:
                if exc.code != sync.KILL_SWITCH:
                    raise
                result.kill_switch = str(exc.payload.get("scope") or "")
                result.failed.clear()
                plans = {}
            client.timeout = opts.transfer_timeout
            _apply(client, target, org, root, state, managed, plans, opts, result)
    return result


def _classify(
    root: Path,
    managed: dict[str, dict[str, Any]],
    plans: dict[str, sync.SkillPlan],
    opts: sync.SyncOptions,
    result: NativeResult,
) -> tuple[list[str], list[str]]:
    """`(a instalar, a retirar)`: separa lo ajeno (choques) y lo editado por el usuario, que no se toca."""
    to_install: list[str] = []
    for name, plan in sorted(plans.items()):
        final = root / name
        entry = managed.get(name)
        exists = final.is_symlink() or final.exists()
        if entry is None and exists:
            result.collisions[name] = "choca con una skill tuya del mismo nombre"
            continue
        if (
            entry is not None and exists and (plan.action != "keep" or opts.force)
        ):  # sin novedades, no hay nada que pisar
            change = local_changes(final, entry)
            if change and not opts.force:
                result.modified[name] = change
                continue
        if plan.action in ("add", "update"):
            to_install.append(name)
    to_retire = []
    for name, entry in sorted(managed.items()):
        if name in plans:
            continue
        final = root / name
        if not (final.is_symlink() or final.exists()):
            continue
        change = local_changes(final, entry)
        if change and not opts.force:
            result.modified[name] = change
        else:
            to_retire.append(name)
    return to_install, to_retire


def _apply(
    client: Any,
    target: NativeSkillTarget,
    org: handshake.Organization,
    root: Path,
    state: dict[str, Any] | None,
    managed: dict[str, dict[str, Any]],
    plans: dict[str, sync.SkillPlan],
    opts: sync.SyncOptions,
    result: NativeResult,
) -> None:
    to_install, to_retire = _classify(root, managed, plans, opts, result)
    result.unchanged = sorted(
        n for n, p in plans.items() if p.action == "keep" and n not in result.failed and n not in result.modified
    )
    for name in to_install:
        manifest = plans[name].manifest
        assert manifest is not None
        copied = [f for f in manifest.files if not omission_reason(f.path, f.executable)]
        result.details[name] = f", {len(copied)} ficheros, {sync._human_size(sum(f.size for f in copied))}"
        left = [{"path": f.path, "reason": omission_reason(f.path, f.executable) or ""} for f in manifest.files]
        if any(item["reason"] for item in left):
            result.omitted[name] = sorted((i for i in left if i["reason"]), key=lambda i: i["path"])
    result.added = [n for n in to_install if n not in managed]
    result.updated = [n for n in to_install if n in managed]
    result.removed = list(to_retire)
    for name in to_retire:
        result.details[name] = ""
    if state is None and not to_install:
        if not (result.collisions or result.excluded or result.failed):
            result.notes.append("No hay skills accesibles que sincronizar: no se crea nada.")
        result.unchanged = []
        return
    if opts.dry_run:
        return

    if not root.exists():
        root.mkdir(mode=0o700, parents=True)
    tmp_root = root / f"{sync.TMP_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
    for stale in root.glob(f"{sync.TMP_PREFIX}*"):  # restos de una ejecución interrumpida (hay bloqueo)
        sync._remove(stale)
    skills = {n: dict(e) for n, e in managed.items()}
    result.omitted = {}
    try:
        for name in to_retire:  # primero lo local: no depende de que las descargas salgan bien
            sync._remove(root / name)
            skills.pop(name, None)
        for name in [n for n in managed if n not in plans and n not in to_retire and not (root / n).exists()]:
            skills.pop(name, None)  # ya no está en disco ni en el servidor: se olvida
        tmp_root.mkdir(mode=0o700)
        for name in to_install:
            plan = plans[name]
            assert plan.manifest is not None
            try:
                staged, files, omitted, dropped = _stage_skill(client, tmp_root, name, plan.manifest)
            except NotAdaptable as exc:
                result.excluded[name] = str(exc)
                result.added = [n for n in result.added if n != name]
                result.updated = [n for n in result.updated if n != name]
                if name in managed and not _retire_managed(root, name, managed[name], opts, result):
                    continue
                skills.pop(name, None)
                continue
            except McpToolError as exc:
                if exc.code not in sync.NOT_FOR_YOU:
                    raise  # kill switch o servidor no disponible a mitad: se aborta (el estado ya es coherente)
                result.failed[name] = exc.message
                continue
            except (PackageError, ScriptIntegrityError) as exc:
                result.failed[name] = exc.message
                continue
            sync.swap_dir(staged, root / name, tmp_root / f"old-{name}")
            _managed_state(skills, name, plan, files=files, omitted=omitted)
            if omitted:
                result.omitted[name] = omitted
            if dropped:
                result.notes.append(
                    f"«{name}»: se quitan del SKILL.md los campos de Claude Code que {target.label} no documenta:"
                    f" {', '.join(dropped)}."
                )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        result.added = [n for n in result.added if n not in result.failed]
        result.updated = [n for n in result.updated if n not in result.failed]
        if skills or state is not None:
            write_control(root, org.slug, org.mcp_url, skills, list(result.warnings))


def _retire_managed(root: Path, name: str, entry: dict[str, Any], opts: sync.SyncOptions, result: NativeResult) -> bool:
    """Retira una skill gestionada que ya no se puede instalar (salvo que el usuario la haya modificado)."""
    final = root / name
    if not (final.is_symlink() or final.exists()):
        return True
    change = local_changes(final, entry)
    if change and not opts.force:
        result.modified[name] = change
        return False
    sync._remove(final)
    result.removed.append(name)
    result.details[name] = ""
    return True


# --- CLI ---------------------------------------------------------------------------------------------


def global_root(target: NativeSkillTarget) -> Path:
    """`~/.gemini/config/skills` (o su destino, si es un enlace simbólico de *dotfiles*)."""
    root = Path(os.path.expanduser("~")).joinpath(*target.global_dir)
    return root.resolve() if root.is_symlink() else root


def _global_mcp_url(name: str) -> str | None:
    """La URL del `baas` del `mcp_config.json` global (T3), si hay uno legible."""
    mcp = setup_tools.TARGETS[name]
    path = Path(os.path.expanduser("~")).joinpath(*mcp.global_config_path)
    try:
        return setup_tools.entry_url(mcp, setup_tools.read_config(path))
    except ProjectSafetyError:
        return None


def cmd_sync_native(args: Any) -> int:
    target = TARGETS[args.tool]
    opts = sync.SyncOptions(
        dry_run=args.dry_run, force=args.force, quiet=args.quiet, timeout=args.timeout, trigger=args.trigger
    )

    def _terminate(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)  # los `finally` borran los temporales

    with contextlib.suppress(ValueError):  # fuera del hilo principal no hay manejadores
        signal.signal(signal.SIGTERM, _terminate)
    try:
        proj, paths, creds, hs = setup_tools.connect(args)
        mcp_url = (_global_mcp_url(target.name) or "").rstrip("/")
        from_mcp = next((o.slug for o in hs.organizations if mcp_url and o.mcp_url == mcp_url), None)
        org = handshake.select_organization(hs, requested=args.org, current=from_mcp or setup_tools._linked_org(proj))
        result = sync_native(target, org, creds.api_key, paths, opts, global_root(target))
    except BaasError as exc:
        if not opts.quiet:
            raise
        print(f"[{opts.trigger}] {exc.render()}", file=sys.stderr)  # falla en abierto: nunca estorba
        return 0
    if not opts.quiet:
        for line in result.render():
            print(line)
        if mcp_url and mcp_url != org.mcp_url:
            print(
                f"AVISO: el MCP global de {target.label} apunta a otra organización ({mcp_url}): las skills y el"
                " MCP no coincidirán. Para cambiar también el MCP: `setup antigravity --replace --org <slug>`."
            )
        for warning in handshake.kill_switch_warnings(hs, org):
            print(f"AVISO: {warning}")
    if result.failed:
        why = "; ".join(f"{n}: {w}" for n, w in sorted(result.failed.items()))
        error = SyncIncompleteError(
            f"No se pudieron sincronizar {len(result.failed)} skill(s): {why}.",
            hint="El resto se aplicó. Avisa al administrador de la organización si persiste.",
        )
        if not opts.quiet:
            raise error
        print(f"[{opts.trigger}] {error.render()}", file=sys.stderr)
    return 0
