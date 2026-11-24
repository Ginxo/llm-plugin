"""Skills de la organización como skills nativas de otra herramienta (T19, #192): `baas.py sync --tool antigravity`.

En Claude Code las skills de la organización salen como `/<organización>:<skill>` porque el MCP las publica como
*prompts* y `sync` instala un plugin. Antigravity no documenta los *prompts* como comandos: sus skills son carpetas con
un `SKILL.md` (`~/.gemini/config/skills/<carpeta>/` en el ámbito global, que leen la CLI `agy` y la extensión) y la CLI
convierte cada una en `/<nombre>`. Este módulo deja ahí las skills accesibles de la organización.

Reutiliza lo del `sync` de H5: la lista de skills y sus dependencias (`sync.list_skills`, `sync.resolve_plans`),
el manifiesto y el SHA-256 de cada fichero (`package_fetch`), el confinamiento de rutas, la escritura en un temporal
y el renombrado atómico (`sync.swap_dir`), el bloqueo, y la revocación (lo que deja de ser accesible se retira).

Lo que añade, porque aquí el destino es una carpeta **del usuario** y no un plugin propio:

- **Nunca pisa lo del usuario.** Lo que el plugin gestiona de cada organización se anota en
  `.baas-skills-<organización>.json` (sin secretos: `package_sha256` y SHA-256 de lo escrito de cada skill). Una carpeta
  con el nombre de una skill de la organización que no está ahí es ajena (del usuario o, si los nombres se solapan, de
  otra organización): se deja como está y se cuenta (choque de nombre). Una skill gestionada que el usuario ha editado
  (o a la que ha añadido ficheros o enlaces) tampoco se sobrescribe ni se retira, salvo con `--force`.
- **Nombres.** La carpeta y el comando son `<organización>-<skill>`: el prefijo es obligatorio porque un usuario puede
  tener skills con el mismo nombre en varias organizaciones (como `/<org>:<skill>` en Claude Code), que conviven en
  la misma carpeta. No es `<org>:<skill>` porque la documentación de Antigravity no dice que `:` valga en `name` (solo
  «minúsculas» y «guiones») y Windows no lo admite en una carpeta. Solo minúsculas, dígitos y guiones; lo demás no se
  instala y se cuenta. Cada organización tiene su fichero de control y retira solo lo suyo.
- **Scripts verificados, copiados.** Como Claude Code, `sync` copia el paquete entero de cada skill (también los
  scripts y los ejecutables) a su carpeta, verificando el SHA-256 de cada fichero contra el manifiesto
  (`package_fetch`), con carpetas 0700 y ficheros 0600 (0700 solo los que el manifiesto marca como ejecutables), sin
  seguir enlaces ni salir de la carpeta. La confianza se da al sincronizar y no en cada ejecución; `--verify`
  recomprueba lo copiado contra el control. Sin ellos en disco, el agente improvisaba (`baas_get_script` y ejecución a
  mano), que es lo que la regla prohíbe. `baas.py run-script` sigue disponible como vía opcional que verifica en cada
  ejecución.
- **`SKILL.md` adaptado.** Antigravity documenta `description` (obligatorio) y `name` (opcional): se escriben solo esos
  dos (`name` = la carpeta; `when_to_use` se une a la descripción). El cuerpo se deja tal cual salvo lo que
  `skill_rewrite` reescribe con certeza (`${CLAUDE_SKILL_DIR}` y las llamadas relativas «desde la carpeta de la skill»
  → la ruta absoluta de la carpeta nativa; enlaces y citas a otras skills de la organización) y va precedido de una
  nota que dice a qué organización pertenece la skill (las tools `baas_*` son las del MCP configurado, que puede ser el
  de otra) y dónde están sus scripts. Los campos propios de Claude Code se quitan y se avisa; una skill que pide
  invocación solo manual (`disable-model-invocation`) no se instala porque Antigravity no puede respetarlo.
- **Idempotente.** El resultado depende del texto original, del prefijo, de la carpeta de la skill (si el texto o el
  paquete dependen de ella) y de qué skills citadas están sincronizadas: una huella (`adapt_fp`) por skill lo resume
  en el fichero de control y, si cambia (se movió el HOME, otra skill citada se ha sincronizado, cambió el
  adaptador), la siguiente sincronización vuelve a adaptar esa skill y solo esa.
- **Sandbox.** `agy` ejecuta las órdenes dentro de un sandbox (sin red y con el HOME oculto fuera del proyecto) salvo
  que una regla `command(...)` las permita; `sync` imprime (sin escribirla: son permisos de la persona) la regla que
  permitiría solo los scripts de estas carpetas.

Solo toca `~/.gemini/config/skills/` (y `~/baas-ai-ip/sync.lock`). Un cambio se ve con `/skills reload` en `agy`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import claude_home, handshake, package_fetch, setup_tools, skill_rewrite, skillmeta, sync
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

CONTROL_PREFIX = ".baas-skills-"  # + `<organización>.json`
MAX_FOLDER_NAME = 200  # los sistemas de ficheros admiten 255 bytes por nombre
CONTROL_FORMAT = 1
MANAGED_BY = "baas-llm-plugin"
NATIVE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_MD = 1024 * 1024  # un SKILL.md más grande no es una skill razonable (y se lee entero en memoria)
FRONTMATTER_KEPT = ("name", "description", "when_to_use")
# Las tools `baas_*` son las del MCP configurado en la herramienta, que puede ser el de otra organización
ORG_NOTE = (
    "> **Skill de la organización «{org}».** Usa las tools `baas_*` del servidor MCP `baas` configurado en esta"
    " herramienta. Antes de actuar, comprueba con `baas_whoami` que la organización es «{org}». Detente e informa al"
    " usuario si no lo es o si el MCP no está conectado, y no uses el parámetro `org` para saltártelo."
)
# En las skills que traen scripts: dónde están y cómo se ejecutan
SCRIPTS_NOTE = (
    "> Los scripts de esta skill están en su carpeta{where}, verificados con su SHA-256 al sincronizar: ejecútalos"
    " desde ahí, tal como indican las órdenes de abajo. No copies el contenido de `baas_get_script` o"
    " `baas_get_skill_files` ni lo ejecutes: si falta un script o algo no cuadra, dile al usuario que ejecute"
    " `baas.py sync --tool antigravity` (o `--verify`). Algunos leen ficheros locales del proyecto (por ejemplo"
    " `knowledge/…`): la skill solo funciona completa si el directorio de trabajo es un proyecto que los tenga."
)
ADAPTER_VERSION = 2  # súbela al cambiar lo que escribe el adaptador: obliga a volver a adaptar las skills
SETTINGS_FILE = "~/.gemini/antigravity-cli/settings.json"  # donde `agy` lee `permissions` (según su documentación)


def _windows() -> bool:
    """¿Es Windows? (una función para poder simularlo en las pruebas)"""
    return os.name == "nt"


def org_note(org_slug: str, skill_dir: str | None, with_scripts: bool) -> str:
    note = ORG_NOTE.format(org=org_slug)
    if not with_scripts:
        return note
    where = f" (`{skill_dir}`)" if skill_dir else ""
    return f"{note}\n>\n{SCRIPTS_NOTE.format(where=where)}"


def adapt_fingerprint(
    org_slug: str, skill_dir: str | None, windows: bool, refs: list[str], needs_dir: bool, synced: frozenset[str]
) -> str:
    """Resume de qué depende el texto adaptado además del original: si cambia, hay que volver a adaptar la skill.

    La carpeta de la skill (y el intérprete de Windows) solo cuentan si el texto o el paquete dependen de ella, y de las
    skills citadas, solo las sincronizadas.
    """
    payload: list[Any] = [ADAPTER_VERSION, org_slug, sorted(r for r in refs if r in synced)]
    if needs_dir:
        payload.append([skill_rewrite.safe_dir(skill_dir, windows=windows), windows])
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class NativeSkillTarget:
    """Dónde y cómo se dejan las skills nativas de una herramienta (solo datos)."""

    name: str  # valor de `--tool`
    label: str
    global_dir: tuple[str, ...]  # relativo al HOME; piezas, no una ruta POSIX
    reload_hint: str
    doc: str = "docs/antigravity.md"


ANTIGRAVITY = NativeSkillTarget(
    name="antigravity",
    label="Antigravity",
    global_dir=(".gemini", "config", "skills"),
    reload_hint=(
        "En `agy`, ejecuta `/skills reload` o abre una conversación nueva."
        " Cómo las ofrece la extensión de VS Code no está comprobado."
    ),
)
TARGETS: dict[str, NativeSkillTarget] = {t.name: t for t in (ANTIGRAVITY,)}


# --- qué se instala y cómo ------------------------------------------------------------------------


def folder_name(org_slug: str, skill: str) -> str:
    """La carpeta de una skill nativa y, con ella, el comando: `<organización>-<skill>` (sin `:`, por Windows)."""
    return f"{org_slug}-{skill}"


def name_problem(target: NativeSkillTarget, org_slug: str, name: str) -> str | None:
    """Por qué la skill `name` de la organización no puede ser una skill nativa, o `None`.

    Con el prefijo de organización ya no hay choques con comandos de la herramienta (`/mcp`, `/skills`) ni con nombres
    reservados de Windows (`con`, `nul`…): la carpeta siempre lleva un guion.
    """
    if not NATIVE_NAME_RE.match(name):
        return f"{target.label} espera nombres en minúsculas, dígitos y guiones, y «{name}» no lo es"
    if len(folder_name(org_slug, name).encode()) > MAX_FOLDER_NAME:
        return f"«{folder_name(org_slug, name)}» es demasiado largo para el nombre de una carpeta"
    return None


class NotAdaptable(Exception):  # noqa: N818 (no es un error: la skill se deja fuera y se cuenta)
    """La skill no se puede dejar como skill nativa sin cambiar lo que su autor pidió."""


def _yaml_string(value: str) -> str:
    """Una cadena YAML entre comillas dobles (JSON es YAML válido); sin saltos de línea Unicode sueltos."""
    text = json.dumps(value, ensure_ascii=False)
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").replace("\x85", "\\x85")


def adapt_skill_md(
    text: str, name: str, org_slug: str, ctx: skill_rewrite.RewriteContext | None = None
) -> tuple[str, list[str], skill_rewrite.RewriteReport | None]:
    """El `SKILL.md` con la cabecera de Antigravity (`name` y `description`), una nota con la organización y el cuerpo
    intacto.

    Devuelve `(texto, campos de Claude Code que se han quitado, informe de la reescritura)`; con `ctx` se reescriben
    las llamadas a scripts y los enlaces y citas a otras skills (`skill_rewrite`). Lanza `PackageError` si no tiene
    una cabecera o una `description` válidas y `NotAdaptable` si pide invocación solo manual.
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
    report = None
    if ctx is not None:
        body, report = skill_rewrite.rewrite(body, ctx)
    has_scripts = bool(ctx and (any(p.startswith("scripts/") for p in ctx.scripts) or ctx.executable))
    note = org_note(
        org_slug,
        ctx.skill_dir if ctx and skill_rewrite.safe_dir(ctx.skill_dir, windows=ctx.windows) else None,
        has_scripts,
    )
    adapted = f"---\nname: {name}\ndescription: {_yaml_string(description)}\n---\n\n{note}\n\n{body}"
    dropped = [str(k) for k in data if k not in FRONTMATTER_KEPT]
    return adapted, dropped, report


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_executable(path: Path) -> bool:
    return os.name == "posix" and bool(path.stat().st_mode & 0o100)


def tree_records(base: Path) -> list[dict[str, Any]]:
    """`[{path, sha256[, executable]}]` de todos los ficheros bajo `base` (sin seguir enlaces)."""
    records: list[dict[str, Any]] = []
    for dirpath, _dirs, files in os.walk(base):
        for f in files:
            p = Path(dirpath) / f
            record: dict[str, Any] = {"path": p.relative_to(base).as_posix(), "sha256": _file_sha256(p)}
            if _is_executable(p):
                record["executable"] = True
            records.append(record)
    return sorted(records, key=lambda r: r["path"])


def scan_changes(folder: Path, entry: dict[str, Any], *, check_modes: bool = False) -> list[str]:
    """Todo lo que difiere entre la carpeta de una skill gestionada y lo que el plugin escribió (vacío si está intacta).

    Compara con lo registrado (SHA-256 de cada fichero) y no sigue enlaces simbólicos. Con `check_modes` (solo POSIX)
    comprueba también los permisos: que sean ejecutables solo los que el manifiesto marcaba y que nada sea escribible
    por el grupo o por otros.
    """
    if folder.is_symlink():
        return ["ahora es un enlace simbólico"]
    if not folder.is_dir():
        return ["ya no es una carpeta"]
    recorded: dict[str, dict[str, Any]] = {
        f["path"]: f
        for f in entry.get("files") or []
        if isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("sha256"), str)
    }
    if not recorded:
        return ["no hay registro de sus ficheros"]
    problems: list[str] = []
    seen: dict[str, Path] = {}
    for dirpath, dirs, files in os.walk(folder):
        for entry_name in [*dirs, *files]:
            p = Path(dirpath) / entry_name
            if p.is_symlink():
                problems.append(f"contiene un enlace simbólico ({p.relative_to(folder).as_posix()})")
        for f in files:
            p = Path(dirpath) / f
            if not p.is_symlink():
                seen[p.relative_to(folder).as_posix()] = p
    for rel in sorted(seen):
        if rel in recorded and _file_sha256(seen[rel]) != recorded[rel]["sha256"]:
            problems.append(f"has editado {rel}")
    problems += [f"falta {rel}" for rel in sorted(set(recorded) - set(seen))]
    problems += [f"has añadido {rel}" for rel in sorted(set(seen) - set(recorded))]
    if check_modes and os.name == "posix":
        for rel, p in sorted(seen.items()):
            mode = p.stat().st_mode
            if rel in recorded and bool(mode & 0o100) != bool(recorded[rel].get("executable")):
                problems.append(
                    f"{rel} {'es ejecutable sin serlo' if mode & 0o100 else 'ya no es ejecutable'} en el manifiesto"
                )
            if mode & 0o022:
                problems.append(f"{rel} es escribible por el grupo o por otros (permisos {oct(mode & 0o777)})")
    return problems


def local_changes(folder: Path, entry: dict[str, Any]) -> str | None:
    """La primera diferencia (ver `scan_changes`), o `None` si la carpeta está intacta."""
    problems = scan_changes(folder, entry)
    return problems[0] if problems else None


# --- estado: .baas-skills-<organización>.json -----------------------------------------------------------------------


def control_path(root: Path, org_slug: str) -> Path:
    """El fichero de control de una organización: cada una gestiona solo lo suyo."""
    return root / f"{CONTROL_PREFIX}{org_slug}.json"


def read_control(root: Path, org_slug: str) -> dict[str, Any] | None:
    """El fichero de control, `None` si no existe. Si no es nuestro o no se entiende, se aborta sin tocar nada."""
    path = control_path(root, org_slug)

    def foreign(why: str) -> ForeignDirectoryError:
        return ForeignDirectoryError(
            f"{path} {why}: no sé qué skills de la organización «{org_slug}» gestiona BaaS y no se toca nada.",
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
        "adapter": ADAPTER_VERSION,
        "skills": skills,
        "warnings": warnings,
    }
    claude_home.write_json_atomic(control_path(root, org), data)


# --- resultado --------------------------------------------------------------------------------------


@dataclass
class NativeResult(sync.SyncResult):
    tool: NativeSkillTarget = ANTIGRAVITY
    collisions: dict[str, str] = field(default_factory=dict)  # skill de la organización → por qué choca
    modified: dict[str, str] = field(default_factory=dict)  # skill gestionada editada por el usuario → qué
    excluded: dict[str, str] = field(default_factory=dict)  # skill que no se instala → por qué
    rewrites: dict[str, skill_rewrite.RewriteReport] = field(default_factory=dict)  # skill → qué se reescribió
    dir_ok: bool = True  # ¿la ruta de las carpetas de las skills se puede escribir en las órdenes sin riesgo?

    def cmd(self, skill: str) -> str:
        """El nombre de la carpeta y del comando de la skill (`<organización>-<skill>`)."""
        return folder_name(self.org, skill)

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
            lines += [f"  {mark} {self.cmd(n)} ({label}{self.details.get(n, '')})" for n in names]
        lines += [f"  ! {self.cmd(n)}: NO instalada ({why})" for n, why in sorted(self.failed.items())]
        lines += [
            f"  = {self.cmd(n)}: {why}: no la toco (renombra la tuya si quieres la de la organización)"
            for n, why in sorted(self.collisions.items())
        ]
        lines += [
            f"  ? {self.cmd(n)}: modificada en tu equipo ({why}): no la toco (`--force` la sobrescribe o la retira)"
            for n, why in sorted(self.modified.items())
        ]
        lines += [f"  · {n}: no se instala ({why})" for n, why in sorted(self.excluded.items())]
        for name, report in sorted(self.rewrites.items()):
            if report.unresolved:
                lines.append(
                    f"AVISO: «{self.cmd(name)}»: {report.unresolved} referencia(s) a `${{CLAUDE_SKILL_DIR}}` sin"
                    f" reescribir (entre comillas simples, sin poder entrecomillar la ruta…): {self.tool.label} no"
                    " define esa variable."
                )
            if report.relative_scripts:
                lines.append(
                    f"AVISO: «{self.cmd(name)}»: {report.relative_scripts} llamada(s) a scripts con ruta relativa"
                    " (`scripts/…`) sin reescribir: el texto no dice con certeza que se ejecuten desde la carpeta de"
                    " la skill."
                )
        if not self.dir_ok and any(r.uses_dir for r in self.rewrites.values()):
            lines.append(
                f"AVISO: la ruta de {self.plugin_dir} tiene caracteres que un shell reinterpretaría (comillas, `$`,"
                " `%`…): no se resuelve la carpeta de las skills en sus órdenes. Usa una ruta sin ellos."
            )
        lines += [f"AVISO: {w}" for w in self.warnings]
        lines += self.notes
        problems = self.failed or self.collisions or self.modified or self.excluded
        if not (self.changed or problems or self.kill_switch is not None):
            count = len(self.unchanged)
            lines.append(f"Todo al día: sin cambios ({count} {'skill' if count == 1 else 'skills'} sincronizadas).")
        elif not self.dry_run and self.changed:
            lines.append(self.tool.reload_hint)
            installed = sorted([*self.added, *self.updated, *self.unchanged])
            if installed:
                shown = ", ".join(f"/{self.cmd(n)}" for n in installed[:6])
                lines.append(f"Se invocan como {shown}{' …' if len(installed) > 6 else ''}")
        return lines


# --- sincronización ----------------------------------------------------------------------------------


@dataclass
class Staged:
    """Una skill descargada y adaptada en un temporal, lista para renombrar."""

    path: Path
    files: list[dict[str, Any]]  # lo escrito, con su SHA-256 (el `SKILL.md` ya adaptado)
    dropped: list[str]  # campos de Claude Code quitados de la cabecera
    report: skill_rewrite.RewriteReport
    meta: dict[str, Any]  # lo que se anota en el control: hash del original, citas, llamadas y huella de la adaptación


def _stage_skill(
    client: Any,
    tmp_root: Path,
    org_slug: str,
    name: str,
    manifest: Manifest,
    synced: frozenset[str],
    skill_dir: str,
    windows: bool,
) -> Staged:
    """Descarga el paquete entero de la skill a un temporal, lo adapta y lo deja listo para renombrar."""
    skill_md = manifest.file("SKILL.md")
    if skill_md is None:
        raise PackageError("el paquete no tiene SKILL.md.")
    if skill_md.size > MAX_SKILL_MD:
        raise PackageError(f"el SKILL.md es demasiado grande ({skill_md.size} bytes; el máximo es {MAX_SKILL_MD}).")
    folder = folder_name(org_slug, name)
    staged = tmp_root / f"new-{folder}"
    # El SHA-256 de cada fichero se verifica al descargarlo (`package_fetch`), las rutas no salen de la carpeta, no se
    # crean enlaces y solo llevan el bit de ejecución los que el manifiesto marca como ejecutables.
    package_fetch.fetch_to_dir(client, manifest, staged)
    md_path = staged / "SKILL.md"
    try:
        text = md_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise PackageError("el SKILL.md no es UTF-8.") from None
    ctx = skill_rewrite.RewriteContext(
        org=org_slug,
        skill=name,
        synced=synced,
        scripts=frozenset(f.path for f in manifest.files),
        skill_dir=skill_dir,
        executable=frozenset(manifest.executable_paths()),
        windows=windows,
    )
    adapted, dropped, report = adapt_skill_md(text, folder, org_slug, ctx)
    assert report is not None
    md_path.write_bytes(adapted.encode("utf-8"))
    needs_dir = report.uses_dir or has_scripts(manifest)
    meta = {
        "original_sha256": skill_md.sha256,
        "refs": report.refs,
        "needs_dir": needs_dir,
        "calls": [[interp, rel] for interp, rel in report.calls],
        "adapt_fp": adapt_fingerprint(org_slug, skill_dir, windows, report.refs, needs_dir, synced),
    }
    return Staged(staged, tree_records(staged), dropped, report, meta)


def has_scripts(manifest: Manifest) -> bool:
    """¿El paquete trae scripts o ejecutables? (su nota dice dónde están)"""
    return any(f.path.startswith("scripts/") or f.executable for f in manifest.files)


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

    slug = sync.validate_slug(org.slug)  # entra en el nombre de una carpeta: solo minúsculas, dígitos y guiones
    windows = _windows()
    result = NativeResult(
        slug,
        root,
        dry_run=opts.dry_run,
        tool=target,
        dir_ok=skill_rewrite.safe_dir(str(root), windows=windows) is not None,
    )
    with sync._lock(baas, enabled=not opts.dry_run) as acquired:
        if not acquired:
            result.skipped = "Otra sincronización está en curso; no se hace nada. Vuelve a intentarlo en unos segundos."
            return result
        if root.exists() and not root.is_dir():
            raise ConfigError(f"{root} existe y no es una carpeta.", hint="Muévelo y repite el comando.")
        state = read_control(root, slug) if root.is_dir() else None
        managed = sync.local_skills(state)
        with McpClient(org.mcp_url, api_key, timeout=opts.timeout) as client:  # sin MCP no se ha tocado nada
            plans: dict[str, sync.SkillPlan] = {}
            try:
                listed = sync.list_skills(client, result.notes)
                for name in sorted(listed):
                    problem = name_problem(target, slug, name)
                    if problem:
                        result.excluded[name] = problem
                        del listed[name]
                plans = sync.resolve_plans(
                    client,
                    listed,
                    managed,
                    root,
                    opts,
                    result,
                    skills_dir=root,
                    folder_of=lambda n: folder_name(slug, n),
                )
                for name in sorted(plans):  # una dependencia con un nombre que la herramienta no admite
                    problem = name_problem(target, slug, name)
                    if problem:
                        result.excluded[name] = problem
                        del plans[name]
                _refresh_stale(client, slug, root, windows, plans, managed, result)
            except McpToolError as exc:
                if exc.code != sync.KILL_SWITCH:
                    raise
                result.kill_switch = str(exc.payload.get("scope") or "")
                result.failed.clear()
                plans = {}
            client.timeout = opts.transfer_timeout
            _apply(client, target, org, root, state, managed, plans, opts, result, windows)
    return result


def _refresh_stale(
    client: Any,
    slug: str,
    root: Path,
    windows: bool,
    plans: dict[str, sync.SkillPlan],
    managed: dict[str, dict[str, Any]],
    result: NativeResult,
) -> None:
    """Una skill sin cambios en el servidor puede necesitar adaptarse de nuevo: se movió el HOME (carpeta de la skill),
    se ha sincronizado otra skill que cita, cambió el adaptador o el control es de antes de esta adaptación (T19 y
    T20 no copiaban los scripts). Se compara la huella guardada con la de ahora y, si difiere, se pasa la skill a
    `update` (solo esa)."""
    synced = frozenset(plans)
    for name, plan in sorted(plans.items()):
        if plan.action != "keep":
            continue
        entry = managed.get(name) or {}
        refs = [r for r in entry.get("refs") or [] if isinstance(r, str)]
        folder = str(root / folder_name(slug, name))
        now = adapt_fingerprint(slug, folder, windows, refs, bool(entry.get("needs_dir")), synced)
        if entry.get("adapt_fp") == now:
            continue
        try:
            manifest = package_fetch.fetch_manifest(client, name)
        except McpToolError as exc:
            if exc.code not in sync.NOT_FOR_YOU:
                raise
            continue  # ya no se puede leer: se queda como está hasta que `resolve_plans` la retire
        except PackageError as exc:
            result.failed[name] = exc.message
            continue
        plans[name] = sync.SkillPlan(
            name, "update", manifest.package_sha256, manifest, [d.name for d in manifest.dependencies]
        )


def _classify(
    root: Path,
    org_slug: str,
    managed: dict[str, dict[str, Any]],
    plans: dict[str, sync.SkillPlan],
    opts: sync.SyncOptions,
    result: NativeResult,
) -> tuple[list[str], list[str]]:
    """`(a instalar, a retirar)`: separa lo ajeno (choques) y lo editado por el usuario, que no se toca."""
    to_install: list[str] = []
    for name, plan in sorted(plans.items()):
        final = root / folder_name(org_slug, name)
        entry = managed.get(name)
        exists = final.is_symlink() or final.exists()
        if entry is None and exists:
            result.collisions[name] = "choca con una carpeta que no es de esta organización (tuya o de otra skill)"
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
        final = root / folder_name(org_slug, name)
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
    windows: bool,
) -> None:
    slug = org.slug
    synced = frozenset(plans)
    to_install, to_retire = _classify(root, slug, managed, plans, opts, result)
    result.unchanged = sorted(
        n for n, p in plans.items() if p.action == "keep" and n not in result.failed and n not in result.modified
    )
    for name in to_install:
        manifest = plans[name].manifest
        assert manifest is not None
        size = sync._human_size(sum(f.size for f in manifest.files))
        result.details[name] = f", {len(manifest.files)} ficheros, {size}"
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
    try:
        for name in to_retire:  # primero lo local: no depende de que las descargas salgan bien
            sync._remove(root / folder_name(slug, name))
            skills.pop(name, None)
        for name in [
            n for n in managed if n not in plans and n not in to_retire and not (root / folder_name(slug, n)).exists()
        ]:
            skills.pop(name, None)  # ya no está en disco ni en el servidor: se olvida
        tmp_root.mkdir(mode=0o700)
        for name in to_install:
            plan = plans[name]
            assert plan.manifest is not None
            try:
                folder = folder_name(slug, name)
                staged = _stage_skill(client, tmp_root, slug, name, plan.manifest, synced, str(root / folder), windows)
            except NotAdaptable as exc:
                result.excluded[name] = str(exc)
                result.added = [n for n in result.added if n != name]
                result.updated = [n for n in result.updated if n != name]
                if name in managed and not _retire_managed(root, slug, name, managed[name], opts, result):
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
            folder = folder_name(slug, name)
            sync.swap_dir(staged.path, root / folder, tmp_root / f"old-{folder}")
            _managed_state(skills, name, plan, files=staged.files, **staged.meta)
            result.rewrites[name] = staged.report
            if staged.report.calls:
                count = len(staged.report.calls)
                result.details[name] = (
                    result.details.get(name, "") + f", {count} llamada(s) a scripts → carpeta de la skill"
                )
            if staged.dropped:
                result.notes.append(
                    f"«{folder}»: se quitan del SKILL.md los campos de Claude Code que {target.label} no documenta:"
                    f" {', '.join(staged.dropped)}."
                )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        result.added = [n for n in result.added if n not in result.failed]
        result.updated = [n for n in result.updated if n not in result.failed]
        warnings = list(result.warnings)
        unchanged_state = state is not None and (
            state.get("org"),
            state.get("server"),
            state.get("skills"),
            state.get("warnings"),
            state.get("adapter"),
        ) == (org.slug, org.mcp_url, skills, warnings, ADAPTER_VERSION)
        if (skills or state is not None) and not unchanged_state:  # un ciclo sin novedades no escribe nada
            write_control(root, org.slug, org.mcp_url, skills, warnings)


def _retire_managed(
    root: Path, org_slug: str, name: str, entry: dict[str, Any], opts: sync.SyncOptions, result: NativeResult
) -> bool:
    """Retira una skill gestionada que ya no se puede instalar (salvo que el usuario la haya modificado)."""
    final = root / folder_name(org_slug, name)
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


# --- verificación local y permisos del sandbox ------------------------------------------------------------------


def control_orgs(root: Path) -> list[str]:
    """Las organizaciones que tienen fichero de control en la carpeta de skills."""
    if not root.is_dir():
        return []
    slugs = []
    for path in sorted(root.glob(f"{CONTROL_PREFIX}*.json")):
        slug = path.name[len(CONTROL_PREFIX) : -len(".json")]
        if sync.SLUG_RE.match(slug):
            slugs.append(slug)
    return slugs


def verify_native(target: NativeSkillTarget, org: str | None) -> tuple[list[str], int]:
    """Recomprueba lo copiado contra el fichero de control (solo lectura, sin red ni clave): `(líneas, diferencias)`."""
    root = global_root(target)
    slugs = [sync.validate_slug(org)] if org else control_orgs(root)
    if not slugs:
        return [f"No hay skills sincronizadas para {target.label} en {root}: nada que verificar."], 0
    lines = [f"Verificación local de las skills nativas de {target.label} en {root} (no se modifica nada):"]
    bad = 0
    for slug in slugs:
        state = read_control(root, slug)
        managed = sync.local_skills(state)
        if state is None:
            lines.append(f"  «{slug}»: no hay fichero de control (nada que verificar).")
            continue
        for name, entry in sorted(managed.items()):
            folder = root / folder_name(slug, name)
            exists = folder.is_symlink() or folder.exists()
            problems = scan_changes(folder, entry, check_modes=True) if exists else ["falta la carpeta"]
            if problems:
                bad += 1
                lines.append(f"  ✗ {folder_name(slug, name)}: {'; '.join(problems)}")
            else:
                lines.append(f"  ✓ {folder_name(slug, name)} ({len(entry.get('files') or [])} ficheros)")
    return lines, bad


def permission_rules(root: Path, org_slug: str, skills: dict[str, Any]) -> list[str]:
    """Las reglas `command(...)` que permiten ejecutar fuera del sandbox solo las llamadas a scripts que los
    `SKILL.md` de la organización hacen desde su carpeta (una por llamada: la documentación solo admite `*` como
    comodín global)."""
    rules: list[str] = []
    for name in sorted(skills):
        folder = (root / folder_name(org_slug, name)).as_posix()
        for call in skills[name].get("calls") or []:
            if isinstance(call, list) and len(call) == 2 and isinstance(call[1], str):
                target = f"{folder}/{call[1]}"
                rule = f"command({call[0]} {target})" if call[0] else f"command({target})"
                if rule not in rules:
                    rules.append(rule)
    return rules


def permission_suggestion(target: NativeSkillTarget, root: Path, rules: list[str]) -> list[str]:
    """Texto de la sugerencia de permisos para `agy` (se imprime; nunca se escribe en los ajustes de la persona)."""
    block = json.dumps({"permissions": {"allow": rules}}, indent=2, ensure_ascii=False)
    lines = [
        f"Permisos de `agy` (sugerencia: NO he escrito nada en {SETTINGS_FILE}; son tus permisos):",
        "  Sin una regla `command(...)`, `agy` ejecuta estos scripts dentro de su sandbox (sin red y con tu HOME oculto"
        " fuera del proyecto) y, si fallan ahí, te pide salir de él. Para permitir fuera del sandbox solo los scripts"
        f" de estas carpetas, añade a `permissions.allow` de {SETTINGS_FILE}:",
        *(f"  {line}" for line in block.splitlines()),
        "  Sintaxis según la documentación de Antigravity (`command(...)` casa por prefijo, palabra a palabra, y deja"
        " ejecutar la orden con o sin sandbox). NO comprobada con `agy` real: la documentación no dice cómo se tratan"
        " las comillas ni las rutas con espacios, ni admite `*` dentro de un patrón (por eso hay una regla por"
        " llamada); si una orden lleva la ruta entrecomillada y la regla no casa, `agy` te lo preguntará.",
    ]
    if re.search(r"\s", str(root)):
        lines.append("  AVISO: la ruta tiene espacios y la documentación no dice cómo escribirlos en una regla.")
    return lines


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


def synced_server(tool: str, org_slug: str, skill: str) -> str:
    """La URL del MCP de una skill sincronizada como skill nativa (la que escribió `sync` en el fichero de control).

    Falla si la herramienta o la organización no son válidas, si no hay nada sincronizado de esa organización, si la
    skill no figura entre las sincronizadas o si la URL no es segura (HTTPS, salvo en local): la clave de la persona
    solo se envía a un MCP que `sync` anotó tras el *handshake*.
    """
    from .envfile import normalize_app_url

    target = TARGETS.get(tool)
    if target is None:
        raise ConfigError(f"Herramienta desconocida: «{tool}».")
    try:
        slug = sync.validate_slug(org_slug)
    except ConfigError:
        raise ConfigError(f"Organización no válida: «{org_slug}».", hint="Es el slug de la organización.") from None
    state = read_control(global_root(target), slug)
    hint = f"Ejecuta `baas.py sync --tool {tool} --org {slug}`."
    if state is None:
        raise ConfigError(f"No hay skills de «{slug}» sincronizadas para {target.label}.", hint=hint)
    if skill not in sync.local_skills(state):
        raise ConfigError(f"La skill «{skill}» no figura entre las skills sincronizadas de «{slug}».", hint=hint)
    server = state.get("server")
    if not isinstance(server, str):
        raise ConfigError(f"El fichero de control de «{slug}» no tiene la URL del MCP.", hint=hint)
    try:
        return normalize_app_url(server)
    except ConfigError:
        raise ConfigError(
            f"El fichero de control de «{slug}» tiene una URL de MCP que no es segura.", hint=hint
        ) from None


def cmd_local_native(target: NativeSkillTarget, args: Any) -> int:
    """`--verify` y `--permissions`: solo leen el fichero de control y los ficheros copiados (sin red ni clave)."""
    root = global_root(target)
    bad = 0
    if args.verify:
        lines, bad = verify_native(target, args.org)
        for line in lines:
            print(line)
    if args.permissions:
        for slug in [sync.validate_slug(args.org)] if args.org else control_orgs(root):
            rules = permission_rules(root, slug, sync.local_skills(read_control(root, slug)))
            if rules:
                for line in permission_suggestion(target, root, rules):
                    print(line)
            else:
                print(f"«{slug}»: ninguna skill sincronizada llama a scripts desde su carpeta: nada que permitir.")
    if bad:
        raise ScriptIntegrityError(
            f"{bad} skill(s) no coinciden con el fichero de control: no las uses sin revisarlas.",
            hint="Revisa lo que cambió; `sync --tool antigravity --force` las restaura (sobrescribe tus cambios).",
        )
    return 0


def cmd_sync_native(args: Any) -> int:
    target = TARGETS[args.tool]
    if getattr(args, "verify", False) or getattr(args, "permissions", False):
        return cmd_local_native(target, args)
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
                f"AVISO: el MCP global de {target.label} apunta a otra organización ({mcp_url}): las skills de"
                f" «{org.slug}» usarían las tools `baas_*` de esa otra organización y actuarían sobre ella (cada"
                " SKILL.md lo recuerda al agente). El global solo admite un MCP: para cambiarlo,"
                f" `setup antigravity --replace --org {org.slug}`."
            )
        elif not mcp_url:
            print(
                f"AVISO: no hay un servidor `baas` en el mcp_config.json global de {target.label}: las skills no"
                f" podrán usar las tools `baas_*` hasta que ejecutes `setup antigravity --org {org.slug}`."
            )
        for warning in handshake.kill_switch_warnings(hs, org):
            print(f"AVISO: {warning}")
        if result.changed and not opts.dry_run:  # la regla de permisos, solo si algo cambió (`--permissions` la repite)
            root = global_root(target)
            rules = permission_rules(root, org.slug, sync.local_skills(read_control(root, org.slug)))
            if rules:
                for line in permission_suggestion(target, root, rules):
                    print(line)
    if result.failed:
        why = "; ".join(f"{result.cmd(n)}: {w}" for n, w in sorted(result.failed.items()))
        error = SyncIncompleteError(
            f"No se pudieron sincronizar {len(result.failed)} skill(s): {why}.",
            hint="El resto se aplicó. Avisa al administrador de la organización si persiste.",
        )
        if not opts.quiet:
            raise error
        print(f"[{opts.trigger}] {error.render()}", file=sys.stderr)
    return 0
