"""Cabecera de `SKILL.md` (tolerante, como Claude) y manifiesto `baas.yaml` (diseño H5 §5.3 y §5.4)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import miniyaml

_TOP_KEY = re.compile(r"^([A-Za-z_][\w.-]*)\s*:(.*)$")
MANIFEST_ORDER = ("version", "roles", "status", "requires", "depends_on", "executable")


@dataclass
class Frontmatter:
    present: bool = False
    valid_yaml: bool = True
    keys: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    lines: int = 0  # líneas que ocupa la cabecera, con los separadores


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """`(cabecera, cuerpo)`. La cabecera es el texto entre los dos `---` (o `None` si no hay)."""
    if not text.startswith("---"):
        return None, text
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    return None, text


def _lenient(header: str) -> dict[str, Any]:
    """`clave: valor` línea a línea (lo que el MCP hace si el YAML no es válido)."""
    data: dict[str, Any] = {}
    for line in header.split("\n"):
        m = _TOP_KEY.match(line)
        if m:
            value = m.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            data[m.group(1)] = value
    return data


def parse_frontmatter(text: str) -> Frontmatter:
    header, _ = split_frontmatter(text)
    if header is None:
        return Frontmatter()
    fm = Frontmatter(present=True, lines=header.count("\n") + 3)
    fm.keys = [m.group(1) for line in header.split("\n") if (m := _TOP_KEY.match(line)) and not line.startswith(" ")]
    try:
        data = miniyaml.loads(header)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise miniyaml.YamlError("la cabecera no es un mapa")
        fm.data = data
    except miniyaml.YamlError as exc:
        fm.valid_yaml = False
        fm.error = str(exc)
        fm.data = _lenient(header)
    return fm


def parse_manifest(text: str) -> dict[str, Any]:
    """Interpreta un `baas.yaml`. Lanza `YamlError` si no es válido o no es un mapa."""
    data = miniyaml.loads(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise miniyaml.YamlError("baas.yaml debe ser un mapa")
    return data


def _union(*lists: list[str] | None) -> list[str]:
    out: list[str] = []
    for items in lists:
        for item in items or []:
            if item not in out:
                out.append(item)
    return out


def _as_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def merge_manifest(
    base: dict[str, Any] | None,
    *,
    roles: list[str] | None = None,
    interpreters: list[str] | None = None,
    binaries: list[str] | None = None,
    depends_skills: list[str] | None = None,
    extra_depends_skills: list[str] | None = None,
    depends_knowledge: list[str] | None = None,
    executable: list[str] | None = None,
    package_paths: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Genera el `baas.yaml` que se sube. Devuelve `(manifiesto, avisos)`.

    Prioridad: opciones explícitas > `base` (el `baas.yaml` local, o el que ya hay publicado) > lo detectado.
    Lo que ya figura en `base` manda sobre lo detectado (el autor lo ha revisado): la detección solo rellena
    lo que falta. Las claves desconocidas se conservan. `executable` refleja el paquete: se une lo calculado
    con lo que ya había, descartando rutas que ya no existen (con aviso).
    """
    notes: list[str] = []
    base = dict(base or {})
    out: dict[str, Any] = {"version": base.get("version") if isinstance(base.get("version"), int) else 1}
    if roles is not None:
        out["roles"] = roles
    elif base.get("roles") is not None:
        out["roles"] = _as_list(base["roles"])
    if base.get("status") is not None:
        out["status"] = base["status"]

    requires = dict(base.get("requires") or {}) if isinstance(base.get("requires"), dict) else {}
    for key, detected in (("interpreters", interpreters), ("binaries", binaries)):
        if requires.get(key) is None and detected:
            requires[key] = list(detected)
    if requires:
        out["requires"] = requires

    depends = dict(base.get("depends_on") or {}) if isinstance(base.get("depends_on"), dict) else {}
    skills = _as_list(depends.get("skills"))
    if skills is None:
        skills = list(depends_skills or [])
    skills = _union(skills, extra_depends_skills)
    if skills:
        depends["skills"] = skills
    knowledge = _union(_as_list(depends.get("knowledge")), depends_knowledge)
    if knowledge:
        depends["knowledge"] = knowledge
    if depends:
        out["depends_on"] = depends

    wanted = _union(_as_list(base.get("executable")), executable)
    if package_paths is not None:
        for gone in [p for p in wanted if p not in package_paths]:
            notes.append(f"`executable` de baas.yaml cita {gone}, que no está en el paquete: se descarta.")
        wanted = [p for p in wanted if p in package_paths]
    if wanted:
        out["executable"] = sorted(wanted)

    for key, value in base.items():  # claves desconocidas: se conservan
        if key not in MANIFEST_ORDER:
            out[key] = value
    return out, notes


def dump_manifest(manifest: dict[str, Any]) -> str:
    ordered = {k: manifest[k] for k in MANIFEST_ORDER if k in manifest}
    ordered.update({k: v for k, v in manifest.items() if k not in ordered})
    return miniyaml.dumps(ordered)


def parse_roles(value: str | None) -> list[str] | None:
    """`--roles a,b` → `["a", "b"]`; `--roles '*'` → `["*"]` (debe ir solo)."""
    if value is None:
        return None
    roles = [r.strip() for r in value.split(",") if r.strip()]
    if "*" in roles and roles != ["*"]:
        raise ValueError("El rol `*` (cualquier miembro de la organización) debe ir solo.")
    return roles


def with_roles(text: str, roles: list[str]) -> tuple[str, str]:
    """Añade `roles` a la cabecera de un `.md` de conocimiento. `(texto, "added" | "kept")`.

    Si la cabecera ya declara `roles`, no se toca. Sin cabecera, se crea una solo con `roles`.
    """
    line = miniyaml.dumps({"roles": roles}).strip()
    nl = "\r\n" if text.startswith("---\r\n") else "\n"
    header, _ = split_frontmatter(text)
    if header is None:
        return f"---{nl}{line}{nl}---{nl}{text}", "added"
    if any(m.group(1) == "roles" for raw in header.split("\n") if (m := _TOP_KEY.match(raw))):
        return text, "kept"
    return f"---{nl}{line}{nl}" + text[3 + len(nl) :], "added"
