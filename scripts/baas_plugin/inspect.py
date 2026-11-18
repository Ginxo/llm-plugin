"""`baas.py inspect <dir>`: informe MECÁNICO de una skill antes de publicarla (diseño H5 §8). Solo lectura.

Analiza el directorio de la skill y, si se indican, sus scripts (`--scripts`) y su conocimiento (`--knowledge`):
tipos y tamaños, imports y llamadas entre scripts, rutas calculadas desde el propio fichero que salen del
paquete, enlaces a otras skills o a `knowledge/`, binarios externos, estado mutable, posibles datos sensibles,
`hooks` y `allowed-tools`, ficheros poco portables, ocultos y enlaces simbólicos. Propone reescrituras, pero no
modifica nada: `publish --rewrite-paths` las aplica a la copia que sube, y solo si se pide.

Nunca se sube nada de fuera del árbol indicado. El informe enmascara los datos sensibles que encuentra.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import scan_md, scan_py, scan_sh, sensitive, skillmeta
from .errors import ConfigError
from .skilltree import LARGE_FILE_BYTES, LARGE_PACKAGE_BYTES, LocalFile, scan_tree

SCHEMA = 1
MAX_SCAN_BYTES = 16 * 1024 * 1024  # el análisis de contenido lee como máximo esto de cada fichero
SCRIPT_EXTS = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php", ".ps1", ".bat"}
WINDOWS_EXTS = {".ps1", ".bat", ".cmd", ".psm1", ".vbs"}
NATIVE_MAGIC = (
    (b"\x7fELF", "ELF (Linux)"),
    (b"\xcf\xfa\xed\xfe", "Mach-O (macOS)"),
    (b"\xfe\xed\xfa\xcf", "Mach-O (macOS)"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal o clase Java"),
    (b"MZ", "ejecutable de Windows (PE)"),
)
_ABS_HOME = re.compile(
    r"(?<![\w.:/$-])(/(?:home|Users)/[A-Za-z0-9._-]+(?:/[^\s\"'`)>\]]*)?|[A-Za-z]:\\Users\\[^\s\"'`)>\]]+)"
)
_STDLIB = getattr(sys, "stdlib_module_names", frozenset())


@dataclass
class Item:
    area: str  # skill | scripts | knowledge
    rel: str  # relativa a la raíz de su área
    disp: str  # ruta para el informe (la que tendría en el paquete, o `knowledge/…`)
    file: LocalFile
    text: str | None = None
    head: bytes = b""
    truncated: bool = False

    @property
    def ext(self) -> str:
        return posixpath.splitext(self.rel)[1].lower()

    @property
    def is_script_like(self) -> bool:
        return self.ext in SCRIPT_EXTS or self.head.startswith(b"#!")

    @property
    def executable(self) -> bool:
        return self.file.exec_bit or self.head.startswith(b"#!")


def _load(item: Item) -> None:
    with item.file.path.open("rb") as fh:
        data = fh.read(MAX_SCAN_BYTES + 1)
    item.truncated = len(data) > MAX_SCAN_BYTES
    data = data[:MAX_SCAN_BYTES]
    item.head = data[:4096]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:  # el corte pudo caer a mitad de un carácter
            text = data[:-4].decode("utf-8") if item.truncated else None
        except UnicodeDecodeError:
            text = None
    item.text = None if text is None or "\x00" in text else text


@dataclass
class Collected:
    items: list[Item] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    symlinks: list[dict[str, Any]] = field(default_factory=list)
    ignored: int = 0
    knowledge_roots: list[Path] = field(default_factory=list)


def _collect(
    skill: Path,
    scripts: list[Path],
    knowledge: list[Path],
    excludes: list[str],
    default_excludes: bool,
) -> Collected:
    out = Collected()

    def add(area: str, root: Path, prefix: str) -> None:
        scan = scan_tree(root, excludes=excludes, default_excludes=default_excludes)
        out.ignored += scan.ignored
        label = {"skill": "", "scripts": "scripts/", "knowledge": "knowledge/"}[area]
        shown = (lambda rel: rel) if area == "skill" else (lambda rel: f"{prefix}{rel}")
        for f in scan.files:
            out.items.append(Item(area, f.rel, shown(f.rel), f))
        for s in scan.skipped:
            out.skipped.append(
                {
                    "area": area,
                    "path": f"{label}{s.rel}" if area != "skill" else s.rel,
                    "reason": s.reason,
                    "target": s.target,
                }
            )
        for link in scan.links:
            out.symlinks.append(
                {
                    "area": area,
                    "path": f"{label}{link.rel}" if area != "skill" else link.rel,
                    "target": link.target,
                    "action": link.action,
                    "reason": link.reason,
                }
            )

    add("skill", skill, "")
    for s in scripts:
        add("scripts", s, "scripts/")
    for k in knowledge:
        add("knowledge", k, "knowledge/")
        out.knowledge_roots.append(Path(k).resolve())
    for item in out.items:
        _load(item)
    return out


def _stdlib(top: str) -> bool:
    return top in _STDLIB or top == "__future__"


def _locate_knowledge(ref: str, roots: list[Path]) -> bool:
    """¿Existe `knowledge/<x>` en alguna de las carpetas de conocimiento indicadas?"""
    parts = [p for p in ref.split("/") if p][1:]  # sin el `knowledge/` inicial
    if not parts or any(c in ref for c in "<>{}*%"):
        return False
    for root in roots:
        if (root.joinpath(*parts)).exists():
            return True
        for k in range(1, len(parts)):  # la carpeta indicada ya es `knowledge/<parts[:k]>`
            if tuple(root.parts[-k:]) == tuple(parts[:k]) and root.joinpath(*parts[k:]).exists():
                return True
    return False


def inspect_skill(
    skill_dir: str | Path,
    *,
    scripts: list[Path] | tuple[Path, ...] = (),
    knowledge: list[Path] | tuple[Path, ...] = (),
    excludes: list[str] | tuple[str, ...] = (),
    default_excludes: bool = True,
) -> dict[str, Any]:
    skill = Path(skill_dir).resolve()
    if not (skill / "SKILL.md").is_file():
        raise ConfigError(
            f"«{skill_dir}» no es una skill: no contiene SKILL.md.",
            hint="Indica la carpeta de la skill; los scripts y el conocimiento van en --scripts y --knowledge.",
        )
    for label, paths in (("--scripts", scripts), ("--knowledge", knowledge)):
        for p in paths:
            if not Path(p).exists():
                raise ConfigError(f"{label}: «{p}» no existe.")
    col = _collect(skill, [Path(p) for p in scripts], [Path(p) for p in knowledge], list(excludes), default_excludes)
    items = col.items
    by_disp: dict[str, list[Item]] = {}
    for it in items:
        by_disp.setdefault(it.disp, []).append(it)
    warnings: list[str] = []
    for disp, group in sorted(by_disp.items()):
        if len(group) > 1 and {g.area for g in group} != {"knowledge"}:
            warnings.append(
                f"Colisión de rutas: {disp} aparece {len(group)} veces (skill y scripts); se subiría una sola."
            )

    skill_name = skill.name
    skill_md = next(i for i in items if i.area == "skill" and i.rel == "SKILL.md")
    fm = skillmeta.parse_frontmatter(skill_md.text or "")
    fm_name = fm.data.get("name") if isinstance(fm.data.get("name"), str) else None
    if fm_name and fm_name != skill_name:
        warnings.append(
            f"El `name` de SKILL.md («{fm_name}») no coincide con la carpeta («{skill_name}»): "
            f"la skill se invocará como /<organización>:{skill_name}."
        )
    hooks = "hooks" in fm.keys
    allowed = fm.data.get("allowed-tools")
    if hooks:
        warnings.append(
            "FRONTMATTER_HOOKS: SKILL.md declara `hooks`: código de la organización que se ejecutará en el equipo."
        )
    if allowed:
        warnings.append(
            "FRONTMATTER_ALLOWED_TOOLS: SKILL.md declara `allowed-tools`: herramientas preaprobadas en el equipo."
        )
    if fm.present and not fm.valid_yaml:
        warnings.append(
            f"HEADER_LENIENT: la cabecera de SKILL.md no es YAML estricto ({fm.error}); el MCP la aceptará con aviso."
        )
    description = fm.data.get("description")
    frontmatter = {
        "present": fm.present,
        "valid_yaml": fm.valid_yaml,
        "name": fm_name,
        "description": " ".join(str(description).split()) if description else None,
        "keys": fm.keys,
        "hooks": hooks,
        "allowed_tools": " ".join(allowed) if isinstance(allowed, list) else allowed,
        "roles": fm.data.get("roles"),
        "error": fm.error,
    }

    # --- índice de scripts propios ----------
    pkg_scripts = [i for i in items if i.area == "scripts" or (i.area == "skill" and i.rel.startswith("scripts/"))]
    index: dict[str, Item] = {}
    for it in pkg_scripts:
        index.setdefault(it.disp, it)
    own_basenames = {posixpath.basename(i.disp) for i in pkg_scripts if posixpath.basename(i.disp) != "__init__.py"}
    basename_to_disps: dict[str, list[str]] = {}
    for it in pkg_scripts:
        basename_to_disps.setdefault(posixpath.basename(it.disp), []).append(it.disp)

    def pick(basename: str, from_disp: str) -> str | None:
        cands = [d for d in basename_to_disps.get(basename, []) if d != from_disp]
        if not cands:
            return None
        same_dir = [d for d in cands if posixpath.dirname(d) == posixpath.dirname(from_disp)]
        return (same_dir or cands)[0]

    imports: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    third_party: list[dict[str, Any]] = []
    chains_all: list[tuple[Item, dict[str, Any], str]] = []  # (item, cadena, kind)
    mutable: list[dict[str, Any]] = []
    other_writes = 0
    binaries: dict[str, list[str]] = {}
    interpreters: set[str] = set()
    nonportable: list[dict[str, Any]] = []
    knowledge_refs: dict[str, dict[str, Any]] = {}
    sensitive_items: list[dict[str, Any]] = []
    sensitive_files: list[dict[str, Any]] = []

    def add_binary(name: str, where: str) -> None:
        binaries.setdefault(name, [])
        if where not in binaries[name]:
            binaries[name].append(where)

    def note_knowledge(ref: str, where_file: str, line: int, kind: str) -> None:
        ref = ref.rstrip("/.,;:")
        if ref in knowledge_refs or not ref or ref == "knowledge":
            return
        knowledge_refs[ref] = {
            "target": ref,
            "from": where_file,
            "line": line,
            "kind": kind,
            "exists_locally": _locate_knowledge(ref, col.knowledge_roots) if col.knowledge_roots else None,
        }

    for it in items:
        reason = sensitive.sensitive_file_reason(posixpath.basename(it.rel))
        if reason:
            sensitive_files.append({"file": it.disp, "reason": reason})
        if it.text is not None:
            for m in sensitive.scan_text(it.text):
                sensitive_items.append({"kind": m.kind, "file": it.disp, "line": m.line, "sample": m.sample})
            for m in _ABS_HOME.finditer(it.text):
                line = it.text.count("\n", 0, m.start()) + 1
                nonportable.append(
                    {
                        "kind": "absolute_path",
                        "file": it.disp,
                        "line": line,
                        "detail": f"ruta del equipo del autor: {m.group(1)}",
                    }
                )
        elif it.head:
            for magic, label in NATIVE_MAGIC:
                if it.head.startswith(magic):
                    nonportable.append(
                        {
                            "kind": "native_binary",
                            "file": it.disp,
                            "detail": f"binario nativo dependiente de la plataforma: {label}",
                        }
                    )
                    break
        if it.ext in WINDOWS_EXTS:
            nonportable.append(
                {
                    "kind": "windows_only",
                    "file": it.disp,
                    "detail": "script de Windows: no se ejecuta en Linux ni macOS",
                }
            )
        if it.area == "knowledge":
            continue
        shebang = (
            scan_sh.shebang_interpreter(it.head.split(b"\n", 1)[0].decode("utf-8", "replace"))
            if it.head.startswith(b"#!")
            else None
        )
        text = it.text
        if text is None:
            continue
        if it.ext == ".py":
            interpreters.add(shebang or "python3")
            res = scan_py.scan_python(text, own_basenames - {posixpath.basename(it.disp)})
            if res.parse_error:
                warnings.append(f"{it.disp}: no se pudo analizar como Python ({res.parse_error}).")
            for imp in res.imports:
                target = _resolve_import(imp, it.disp, index)
                if target:
                    imports.append({"from": it.disp, "to": target, "module": imp["module"], "line": imp["line"]})
                elif imp["level"] == 0 and not _stdlib(imp["top"]):
                    third_party.append({"file": it.disp, "module": imp["top"], "line": imp["line"]})
            for mention in res.mentions:
                to = pick(mention["name"], it.disp)
                if to:
                    calls.append({"from": it.disp, "to": to, "line": mention["line"], "how": mention["how"]})
            for chain in res.chains:
                chains_all.append((it, chain, "__file__"))
            for w in res.writes:
                mutable.append({"file": it.disp, **w})
            other_writes += res.other_writes
            for name, line in res.binaries:
                cls = scan_sh.classify_binary(name)
                if cls and cls[0] == "interpreter":
                    interpreters.add(cls[1])
                elif name not in ("sys", "python", "python3"):
                    add_binary(name, f"{it.disp}:{line}")
            for ref, line in res.knowledge_refs:
                note_knowledge(ref, it.disp, line, "path")
        elif it.ext in (".sh", ".bash", ".zsh") or (shebang in ("bash", "sh", "zsh") and it.ext != ".py"):
            res_sh = scan_sh.scan_shell(text, own_basenames - {posixpath.basename(it.disp)})
            interpreters.add(res_sh.interpreter or "sh")
            for mention in res_sh.mentions:
                to = pick(mention["name"], it.disp)
                if to:
                    calls.append({"from": it.disp, "to": to, "line": mention["line"], "how": "shell"})
            for chain in res_sh.chains:
                chains_all.append((it, chain, chain["kind"]))
            for w in res_sh.writes:
                mutable.append({"file": it.disp, **w})
            other_writes += res_sh.other_writes
            for name, line in res_sh.binaries:
                add_binary(name, f"{it.disp}:{line}")
            for name, _ in res_sh.interpreters:
                interpreters.add(name)
        elif it.ext in (".js", ".mjs", ".cjs", ".ts"):
            res_js = scan_sh.scan_js(text, own_basenames - {posixpath.basename(it.disp)})
            if res_js.browser_snippet:
                nonportable.append(
                    {
                        "kind": "browser_snippet",
                        "file": it.disp,
                        "detail": "usa window/document sin Node: es un snippet de consola del navegador, no un script",
                    }
                )
            else:
                interpreters.add("node")
            for mention in res_js.mentions:
                to = pick(mention["name"], it.disp)
                if to:
                    calls.append({"from": it.disp, "to": to, "line": mention["line"], "how": "reference"})
            for chain in res_js.chains:
                chains_all.append((it, chain, "__dirname"))
            for w in res_js.writes:
                mutable.append({"file": it.disp, **w})
            other_writes += res_js.other_writes
            for name, line in res_js.binaries:
                add_binary(name, f"{it.disp}:{line}")
        elif shebang:
            interpreters.add(shebang)

    # --- rutas calculadas desde el propio fichero ----------
    derived_by_root: dict[tuple[str, str], list[str]] = {}
    for it, chain, _kind in chains_all:
        if chain["segs"]:
            key = (it.disp, chain["root"] or "(expr)")
            path = "/".join(chain["segs"])
            lst = derived_by_root.setdefault(key, [])
            if path not in lst:
                lst.append(path)
    path_escapes: list[dict[str, Any]] = []
    rewrites: list[dict[str, Any]] = []
    for it, chain, kind in chains_all:
        if chain["segs"] or chain.get("alias") or chain["levels"] == 0:
            continue
        depth = it.rel.count("/")
        escapes = chain["levels"] - 1 > depth
        name = chain["name"] or "(expr)"
        if chain["name"] is None and not escapes:
            continue
        derived = sorted(_prune_prefixes(derived_by_root.get((it.disp, chain["root"] or "(expr)"), [])))
        for d in derived:
            if d.startswith("knowledge"):
                note_knowledge(d, it.disp, chain["line"], "path")
        area_label = "la skill" if it.area == "skill" else "scripts/"
        target = (
            f"sale de {area_label} ({chain['levels']} niveles por encima del fichero)"
            if escapes
            else f"dentro de {area_label}"
        )
        path_escapes.append(
            {
                "file": it.disp,
                "line": chain["line"],
                "name": name,
                "kind": kind,
                "levels_up": chain["levels"],
                "escapes": escapes,
                "expr": chain["expr"][:120],
                "derived": derived,
                "target": target,
            }
        )
        if escapes and chain.get("statement") and it.area in ("scripts", "skill"):
            rw = _root_rewrite(it, chain, kind)
            if rw:
                rewrites.append(rw)

    # --- Markdown de la skill: enlaces, referencias, binarios ----------
    links_skills: dict[str, dict[str, Any]] = {}
    links_scripts: dict[str, dict[str, Any]] = {}
    links_outside: list[dict[str, Any]] = []
    broken: list[dict[str, Any]] = []
    missing_scripts: dict[str, dict[str, Any]] = {}
    skill_files = {i.rel for i in items if i.area == "skill"}
    skill_dirs = {d for r in skill_files for d in _parents(r)}
    script_refs_seed: set[str] = set()

    def sibling_exists(name: str) -> bool:
        return (skill.parent / name / "SKILL.md").is_file()

    for it in items:
        if it.area != "skill" or it.ext not in (".md", ".markdown") or it.text is None:
            continue
        md = scan_md.scan_markdown(it.text)
        file_dir = posixpath.dirname(it.rel)
        for name, line in md.binaries:
            add_binary(name, f"{it.disp}:{line}")
        for name, _ in md.interpreters:
            interpreters.add(name)
        for m in md.mentions:
            n = m["name"]
            if n != skill_name and sibling_exists(n):
                entry = links_skills.setdefault(
                    n, {"name": n, "from": it.disp, "line": m["line"], "via": [], "exists_locally": True}
                )
                if "mention" not in entry["via"]:
                    entry["via"].append("mention")
        for link in md.links:
            target = link["target"]
            if not target or target.startswith("/"):
                continue
            norm = posixpath.normpath(posixpath.join(file_dir, target))
            kind = "link"
            if norm == ".." or norm.startswith("../"):
                parts = norm.split("/")
                k = 0
                while k < len(parts) and parts[k] == "..":
                    k += 1
                rest = parts[k:]
                first = rest[0] if rest else ""
                if (
                    k == 1
                    and first
                    and first not in ("scripts", "knowledge")
                    and (sibling_exists(first) or rest[-1] == "SKILL.md")
                ):
                    entry = links_skills.setdefault(
                        first,
                        {
                            "name": first,
                            "from": it.disp,
                            "line": link["line"],
                            "via": [],
                            "exists_locally": sibling_exists(first),
                        },
                    )
                    if "link" not in entry["via"]:
                        entry["via"].append("link")
                    continue
                if first == "scripts" and len(rest) > 1:
                    tgt = "scripts/" + "/".join(rest[1:])
                    exists = tgt in index
                    links_scripts.setdefault(
                        tgt,
                        {
                            "target": tgt,
                            "from": it.disp,
                            "line": link["line"],
                            "kind": kind,
                            "exists": exists,
                            "own": False,
                        },
                    )
                    if exists:
                        script_refs_seed.add(tgt)
                        new_rel = posixpath.relpath(tgt, file_dir or ".")
                        rewrites.append(
                            {
                                "kind": "md_script_link",
                                "file": it.disp,
                                "line": link["line"],
                                "before": link["raw"],
                                "after": link["raw"].replace(f"]({link['target']}", f"]({new_rel}", 1),
                                "target": tgt,
                                "note": "el script viaja dentro del paquete (scripts/)",
                            }
                        )
                    else:
                        missing_scripts.setdefault(tgt, {"target": tgt, "from": it.disp, "line": link["line"]})
                    continue
                if first == "knowledge" and len(rest) > 1:
                    note_knowledge("knowledge/" + "/".join(rest[1:]), it.disp, link["line"], "link")
                    continue
                links_outside.append({"target": norm, "from": it.disp, "line": link["line"]})
                continue
            if norm in skill_files or norm in skill_dirs or norm == ".":
                if norm.startswith("scripts/"):
                    links_scripts.setdefault(
                        norm,
                        {
                            "target": norm,
                            "from": it.disp,
                            "line": link["line"],
                            "kind": kind,
                            "exists": True,
                            "own": True,
                        },
                    )
                    script_refs_seed.add(norm)
                continue
            if norm.startswith("scripts/"):
                exists = norm in index
                links_scripts.setdefault(
                    norm,
                    {
                        "target": norm,
                        "from": it.disp,
                        "line": link["line"],
                        "kind": kind,
                        "exists": exists,
                        "own": False,
                    },
                )
                if exists:
                    script_refs_seed.add(norm)
                else:
                    missing_scripts.setdefault(norm, {"target": norm, "from": it.disp, "line": link["line"]})
            elif norm.startswith("knowledge/"):
                note_knowledge(norm, it.disp, link["line"], "link")
            else:
                broken.append({"target": link["target"], "from": it.disp, "line": link["line"]})
        for ref in md.refs:
            if ref["kind"] == "knowledge":
                note_knowledge(ref["target"], it.disp, ref["line"], "command" if ref["in_code"] else "mention")
                continue
            tgt = ref["target"]
            exists = tgt in index
            entry = links_scripts.setdefault(
                tgt,
                {
                    "target": tgt,
                    "from": it.disp,
                    "line": ref["line"],
                    "kind": "command" if ref["in_code"] else "mention",
                    "exists": exists,
                    "own": exists and index[tgt].area == "skill",
                },
            )
            if ref["in_code"] and entry["kind"] == "mention":
                entry["kind"] = "command"
            if exists:
                script_refs_seed.add(tgt)
                if it.rel == "SKILL.md" and ref["in_code"] and index[tgt].area != "skill":
                    rewrites.append(_command_rewrite(it, ref, md))
            else:
                missing_scripts.setdefault(tgt, {"target": tgt, "from": it.disp, "line": ref["line"]})
    rewrites = _merge_command_rewrites(rewrites)

    # --- grafo de scripts y cierre ----------
    edges: dict[str, set[str]] = {}
    for e in imports + calls:
        edges.setdefault(e["from"], set()).add(e["to"])
    seeds = set(script_refs_seed) | {i.disp for i in pkg_scripts if i.area == "skill"}
    reached: set[str] = set()
    stack = list(seeds)
    while stack:
        cur = stack.pop()
        if cur in reached:
            continue
        reached.add(cur)
        stack.extend(edges.get(cur, ()))
    scripts_area = sorted({i.disp for i in items if i.area == "scripts"})
    suggested = [d for d in scripts_area if d in reached]
    other = [d for d in scripts_area if d not in reached]
    own = sorted(i.disp for i in pkg_scripts if i.area == "skill")

    # --- manifiesto sugerido ----------
    included = [i for i in items if i.area == "skill" or i.disp in suggested]
    executable = sorted({i.disp for i in included if i.is_script_like and i.executable and i.disp != "SKILL.md"})
    ext_bins = sorted(
        b for b in binaries if scan_sh.classify_binary(b) is None or scan_sh.classify_binary(b)[0] == "binary"
    )  # type: ignore[index]
    manifest_hint = {
        "requires": {"interpreters": sorted(interpreters), "binaries": ext_bins},
        "depends_on": {"skills": sorted(links_skills)},
        "executable": executable,
    }

    # --- avisos ----------
    for it in items:
        if it.file.size > LARGE_FILE_BYTES:
            warnings.append(
                f"LARGE_FILE: {it.disp} pesa {it.file.size / 1048576:.0f} MiB (aviso desde 25 MiB; se sube igualmente)."
            )
        if it.truncated:
            warnings.append(f"{it.disp}: el análisis solo lee los primeros {MAX_SCAN_BYTES // 1048576} MiB.")
    pkg_bytes = sum(i.file.size for i in included)
    if pkg_bytes > LARGE_PACKAGE_BYTES:
        warnings.append(
            f"LARGE_PACKAGE: el paquete pesa {pkg_bytes / 1048576:.0f} MiB (aviso desde 100 MiB; cada sync lo baja)."
        )
    for s in col.skipped:
        warnings.append(f"No se sube {s['path']}: {SKIP_TEXT.get(s['reason'], s['reason'])}.")
    if sensitive_files:
        warnings.append(
            "Hay ficheros que suelen contener credenciales ("
            + ", ".join(f["file"] for f in sensitive_files)
            + "): `publish` no los sube salvo `--include-secrets`."
        )
    if sensitive_items:
        kinds = sorted({s["kind"] for s in sensitive_items})
        warnings.append(f"Posibles datos sensibles ({', '.join(kinds)}): valora moverlos a conocimiento restringido.")
    for m in missing_scripts.values():
        warnings.append(f"{m['from']}:{m['line']} cita {m['target']}, que no está en la skill ni en --scripts.")

    files = [
        {
            "area": i.area,
            "path": i.disp,
            "size": i.file.size,
            "text": i.text is not None,
            "ext": i.ext,
            "exec": i.file.exec_bit,
            "hidden": i.file.hidden,
            "via_symlink": i.file.via_symlink,
        }
        for i in items
    ]
    by_ext: dict[str, dict[str, int]] = {}
    for f in files:
        slot = by_ext.setdefault(f["ext"] or "(sin extensión)", {"count": 0, "bytes": 0})
        slot["count"] += 1
        slot["bytes"] += f["size"]
    return {
        "schema": SCHEMA,
        "skill": {"name": skill_name, "path": str(skill)},
        "frontmatter": frontmatter,
        "files": files,
        "summary": {
            "files": len(files),
            "bytes": sum(f["size"] for f in files),
            "text_files": sum(1 for f in files if f["text"]),
            "binary_files": sum(1 for f in files if not f["text"]),
            "by_ext": dict(sorted(by_ext.items())),
        },
        "skipped": col.skipped,
        "ignored": col.ignored,
        "hidden": sorted(f["path"] for f in files if f["hidden"]),
        "symlinks": col.symlinks,
        "scripts": {
            "imports": imports,
            "calls": calls,
            "third_party": third_party,
            "own": own,
            "suggested": suggested,
            "other": other,
            "missing": sorted(missing_scripts),
        },
        "path_escapes": path_escapes,
        "links": {
            "skills": sorted(links_skills.values(), key=lambda s: s["name"]),
            "scripts": sorted(links_scripts.values(), key=lambda s: s["target"]),
            "knowledge": sorted(knowledge_refs.values(), key=lambda s: s["target"]),
            "outside": links_outside,
            "broken": broken,
        },
        "binaries": {
            "interpreters": sorted(interpreters),
            "external": [{"name": n, "where": w} for n, w in sorted(binaries.items()) if n in ext_bins],
        },
        "mutable_state": mutable,
        "other_writes": other_writes,
        "sensitive": {"items": sensitive_items, "files": sensitive_files},
        "nonportable": nonportable,
        "rewrites": rewrites,
        "manifest": manifest_hint,
        "warnings": warnings,
    }


SKIP_TEXT = {
    "symlink_outside": "es un enlace simbólico que apunta fuera del árbol (nunca se sube nada de fuera)",
    "symlink_broken": "es un enlace simbólico roto",
    "special": "no es un fichero regular",
    "unreadable": "no se pudo leer",
}


def _parents(rel: str) -> list[str]:
    parts = rel.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _prune_prefixes(paths: list[str]) -> list[str]:
    """Quita las rutas que son prefijo de otra (`knowledge/fiscal` si ya está `knowledge/fiscal/x.json`)."""
    return [p for p in paths if not any(o != p and o.startswith(p + "/") for o in paths)]


def _resolve_import(imp: dict[str, Any], importer: str, index: dict[str, Item]) -> str | None:
    module = imp["module"].replace(".", "/")
    base = posixpath.dirname(importer)
    if imp["level"] > 0:
        for _ in range(imp["level"] - 1):
            base = posixpath.dirname(base)
        dirs = [base]
    else:
        dirs = [base, "scripts", ""]
    for d in dirs:
        for cand in (posixpath.join(d, module + ".py"), posixpath.join(d, module, "__init__.py")):
            cand = posixpath.normpath(cand)
            if cand in index:
                return cand
    top = imp["top"]
    if imp["level"] == 0 and top != module:
        return _resolve_import({**imp, "module": top, "top": top}, importer, index)
    return None


def _root_rewrite(it: Item, chain: dict[str, Any], kind: str) -> dict[str, Any] | None:
    stmt = chain["statement"]
    name = chain["name"]
    if kind == "__file__" and it.ext == ".py" and chain.get("module_level") and chain.get("col", 0) == 0:
        expr = chain["expr"]
        if expr.startswith(("Path(", "pathlib.Path(")):
            after = f'{name} = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()'
        elif expr.startswith("os.path."):
            after = f'{name} = os.path.abspath(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())'
        else:
            return None
        return {
            "kind": "py_root",
            "file": it.disp,
            "line": chain["line"],
            "before": stmt,
            "after": after,
            "note": "la raíz pasa a ser el proyecto del usuario (CLAUDE_PROJECT_DIR), no se calcula desde __file__",
        }
    if kind in ("BASH_SOURCE", "$0") and it.ext in (".sh", ".bash", ".zsh", ""):
        m = re.match(r"^(\s*(?:export\s+)?)([A-Za-z_]\w*)=", stmt)
        if m and "$(" in stmt:
            return {
                "kind": "sh_root",
                "file": it.disp,
                "line": chain["line"],
                "before": stmt,
                "after": f'{m.group(1)}{m.group(2)}="${{CLAUDE_PROJECT_DIR:-$PWD}}"',
                "note": "la raíz pasa a ser el proyecto del usuario (CLAUDE_PROJECT_DIR), no con BASH_SOURCE",
            }
    return None


def _command_rewrite(it: Item, ref: dict[str, Any], md: scan_md.MdResult) -> dict[str, Any]:
    line = md.lines[ref["line"] - 1]
    return {
        "kind": "md_script_command",
        "file": it.disp,
        "line": ref["line"],
        "before": line.strip(),
        "after": _rewrite_command_line(line, md.fenced[ref["line"] - 1]).strip(),
        "target": ref["target"],
        "note": "los scripts del paquete se invocan con ${CLAUDE_SKILL_DIR}",
    }


_BARE = re.compile(r"(?<![\w./~$-])(scripts/[A-Za-z0-9_][A-Za-z0-9_./-]*[A-Za-z0-9_])")


def _rewrite_command_line(line: str, fenced: bool) -> str:
    def sub(m: re.Match[str]) -> str:
        new = "${CLAUDE_SKILL_DIR}/" + m.group(1)
        prev = line[m.start() - 1] if m.start() > 0 else " "
        nxt = line[m.end()] if m.end() < len(line) else " "
        if fenced and prev in " \t" and nxt in " \t\\":
            return f'"{new}"'
        return new

    return _BARE.sub(sub, line)


def _merge_command_rewrites(rewrites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Una línea con varios scripts da una sola reescritura (con todos), y no se repiten las de la misma línea."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for r in rewrites:
        key = (r["kind"], r["file"], r["line"])
        if r["kind"] == "md_script_command":
            if key in seen:
                continue
            seen.add(key)
        out.append(r)
    return sorted(out, key=lambda r: (r["file"], r["line"], r["kind"]))


def filter_rewrites(rewrites: list[dict[str, Any]], included: set[str]) -> list[dict[str, Any]]:
    """Deja las reescrituras de ficheros del paquete (`included`: sus rutas) que apuntan a scripts incluidos."""
    return [r for r in rewrites if r["file"] in included and (r.get("target") is None or r["target"] in included)]


def apply_rewrites(text: str, rewrites: list[dict[str, Any]]) -> str:
    """Aplica reescrituras a UN fichero (las de otros ficheros se ignoran). Devuelve el texto nuevo."""
    lines = text.split("\n")
    applied_py_root: list[int] = []
    for r in sorted(rewrites, key=lambda r: r["line"], reverse=True):
        idx = r["line"] - 1
        before = r["before"]
        if 0 <= idx < len(lines) and before in lines[idx]:
            lines[idx] = lines[idx].replace(before, r["after"], 1)
            if r["kind"] == "py_root":
                applied_py_root.append(idx)
    if applied_py_root and not re.search(r"^import os(?:\s*(?:#.*)?|\s*,.*)$", "\n".join(lines), re.M):
        lines.insert(min(applied_py_root), "import os")
    return "\n".join(lines)


# --- informe de texto --------------------------------------------------------------------------------------


def _more(items: list[str], limit: int = 12) -> list[str]:
    return items[:limit] + ([f"… y {len(items) - limit} más"] if len(items) > limit else [])


def _size(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.1f} MiB"
    if n >= 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n} B"


def render_text(rep: dict[str, Any]) -> str:
    out: list[str] = []
    s = rep["summary"]
    out.append(f"Informe de la skill «{rep['skill']['name']}» ({rep['skill']['path']})")
    out.append("")
    out.append("== Ficheros ==")
    out.append(
        f"{s['files']} ficheros, {_size(s['bytes'])} ({s['text_files']} de texto, {s['binary_files']} binarios)."
    )
    out.append("Por extensión: " + ", ".join(f"{e} {v['count']} ({_size(v['bytes'])})" for e, v in s["by_ext"].items()))
    if rep["hidden"]:
        out.append("Ocultos: " + ", ".join(_more(rep["hidden"])))
    for link in rep["symlinks"]:
        verb = (
            "se sigue (apunta dentro del árbol)" if link["action"] == "followed" else f"se OMITE ({link.get('reason')})"
        )
        out.append(f"Enlace simbólico {link['path']} → {link['target']}: {verb}")
    if rep["ignored"]:
        out.append(f"Ignorados por ser residuos habituales (.git, __pycache__, node_modules…): {rep['ignored']}.")
    fm = rep["frontmatter"]
    out += ["", "== Cabecera de SKILL.md =="]
    if not fm["present"]:
        out.append("Sin cabecera YAML.")
    else:
        out.append(f"name: {fm['name']} · claves: {', '.join(fm['keys'])}")
        if fm["description"]:
            out.append(f"description: {fm['description'][:160]}")
        if not fm["valid_yaml"]:
            out.append(f"AVISO: no es YAML estricto ({fm['error']}); el MCP la acepta con aviso HEADER_LENIENT.")
        if fm["hooks"]:
            out.append("AVISO: declara `hooks`: se ejecutarán en el equipo de quien use la skill.")
        if fm["allowed_tools"]:
            out.append(
                f"AVISO: declara `allowed-tools` ({fm['allowed_tools'][:100]}): quedan preaprobadas en el equipo."
            )
    sc = rep["scripts"]
    out += ["", "== Scripts =="]
    out.append(
        "Propuestos como dependencia (los que cita la skill y lo que ellos usan): "
        + (", ".join(sc["suggested"]) or "ninguno")
    )
    out.append("En --scripts pero sin relación con la skill (no se subirían): " + (", ".join(sc["other"]) or "ninguno"))
    if sc["own"]:
        out.append("Scripts que ya están dentro de la skill: " + ", ".join(sc["own"]))
    for e in sc["imports"]:
        out.append(f"  import  {e['from']}:{e['line']} → {e['to']}")
    for e in sc["calls"]:
        out.append(f"  llamada {e['from']}:{e['line']} → {e['to']} ({e['how']})")
    for t in sc["third_party"]:
        out.append(f"  paquete de terceros `{t['module']}` en {t['file']}:{t['line']} (hay que tenerlo instalado)")
    for m in sc["missing"]:
        out.append(f"  FALTA {m}: la cita la skill y no está en la skill ni en --scripts")
    out += ["", "== Rutas calculadas desde el propio fichero =="]
    if not rep["path_escapes"]:
        out.append("Ninguna.")
    for e in rep["path_escapes"]:
        flag = "SALE" if e["escapes"] else "ok  "
        out.append(f"  {flag} {e['file']}:{e['line']} {e['name']} = {e['expr']} ({e['kind']}, {e['target']})")
        for d in _more(e["derived"], 6):
            out.append(f"         cuelga de ahí: {d}")
    ln = rep["links"]
    out += ["", "== Enlaces =="]
    for sk in ln["skills"]:
        out.append(f"  otra skill: {sk['name']} ({'+'.join(sk['via'])}; {sk['from']}:{sk['line']})")
    for k in ln["knowledge"]:
        where = {True: "existe aquí", False: "no está en --knowledge", None: "sin --knowledge"}[k["exists_locally"]]
        out.append(f"  conocimiento: {k['target']} ({k['from']}:{k['line']}; {where})")
    for sct in ln["scripts"]:
        out.append(f"  script: {sct['target']} ({sct['kind']}; {'disponible' if sct['exists'] else 'NO disponible'})")
    for b in ln["broken"]:
        out.append(f"  enlace roto: {b['target']} ({b['from']}:{b['line']})")
    for o in ln["outside"]:
        out.append(f"  enlace fuera de la skill: {o['target']} ({o['from']}:{o['line']})")
    if not any(ln.values()):
        out.append("Ninguno.")
    out += ["", "== Binarios externos e intérpretes =="]
    out.append("Intérpretes: " + (", ".join(rep["binaries"]["interpreters"]) or "ninguno"))
    for b in rep["binaries"]["external"]:
        out.append(f"  {b['name']}: {', '.join(_more(b['where'], 4))}")
    out += ["", "== Estado mutable (lo que los scripts escriben bajo el paquete) =="]
    if not rep["mutable_state"]:
        out.append("Ninguno detectado.")
    for m in rep["mutable_state"]:
        out.append(f"  {m['file']}:{m['line']} {m['op']} {m['path_hint'] or m['target']}")
    if rep["other_writes"]:
        out.append(f"  (y {rep['other_writes']} escrituras más cuya ruta no se puede deducir: revísalas a mano)")
    out += ["", "== Datos sensibles (enmascarados) =="]
    sens = rep["sensitive"]
    if not sens["items"] and not sens["files"]:
        out.append("Ninguno detectado.")
    for f in sens["files"]:
        out.append(f"  FICHERO {f['file']}: {f['reason']}")
    for i in _more([f"{x['kind']} en {x['file']}:{x['line']} ({x['sample']})" for x in sens["items"]], 25):
        out.append("  " + i)
    out += ["", "== No portable =="]
    if not rep["nonportable"]:
        out.append("Nada.")
    for n in rep["nonportable"]:
        out.append(f"  {n['file']}: {n['detail']}")
    out += ["", "== Reescrituras propuestas (no se aplican sin `publish --rewrite-paths`) =="]
    if not rep["rewrites"]:
        out.append("Ninguna.")
    for r in rep["rewrites"]:
        out.append(f"  {r['file']}:{r['line']} [{r['kind']}] {r['before'][:90]}")
        out.append(f"      → {r['after'][:90]}")
    mf = rep["manifest"]
    out += ["", "== Para baas.yaml =="]
    out.append(f"requires: {json.dumps(mf['requires'], ensure_ascii=False)}")
    out.append(f"depends_on.skills: {', '.join(mf['depends_on']['skills']) or 'ninguna'}")
    out.append(f"executable: {', '.join(mf['executable']) or 'ninguno'}")
    out += ["", "== Avisos =="]
    out += [f"  {w}" for w in rep["warnings"]] or ["Ninguno."]
    return "\n".join(out) + "\n"


# --- CLI ------------------------------------------------------------------------------------------------------


def cmd_inspect(args: argparse.Namespace) -> int:
    rep = inspect_skill(
        args.dir,
        scripts=[Path(p) for p in args.scripts],
        knowledge=[Path(p) for p in args.knowledge],
        excludes=args.exclude,
        default_excludes=not args.no_default_excludes,
    )
    sys.stdout.write(json.dumps(rep, ensure_ascii=False, indent=2) + "\n" if args.json else render_text(rep))
    return 0


def add_inspect_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser(
        "inspect",
        help="Informe mecánico de una skill antes de publicarla (solo lectura)",
        description="Analiza una carpeta de skill y los scripts y el conocimiento asociados. No modifica nada.",
    )
    p.add_argument("dir", help="Carpeta de la skill (la que contiene SKILL.md)")
    p.add_argument(
        "--scripts",
        action="append",
        default=[],
        metavar="RUTA",
        help="Carpeta o fichero de scripts fuera de la skill (repetible)",
    )
    p.add_argument(
        "--knowledge", action="append", default=[], metavar="DIR", help="Carpeta de conocimiento asociada (repetible)"
    )
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATRÓN",
        help="Excluye lo que coincida (glob; `dir/` = solo carpetas)",
    )
    p.add_argument("--no-default-excludes", action="store_true", help="No ignorar .git, __pycache__, node_modules…")
    p.add_argument("--json", action="store_true", help="Salida en JSON")
    p.set_defaults(func=cmd_inspect)
