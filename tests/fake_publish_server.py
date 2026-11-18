"""MCP simulado para probar `publish` y `export` (contrato §7.4 de las skills como paquetes, hito H5).

Implementa `baas_write_files` (lote atómico con `dry_run`, `expected_sha256`, `delete` y todos los fallos de golpe),
`baas_get_skill_package`, `baas_get_skill_files`, `baas_list_files` y `baas_whoami`, con roles, avisos y errores
del contrato. No depende de `dev/fake_baas.py`. Dos formas de usarlo:

- `DirectClient(store)`: sin red; tiene el mismo `call_tool` que `McpClient`.
- `FakePublishServer(store)`: un servidor HTTP (Streamable HTTP mínimo) en un puerto efímero.

`path_style` decide cómo devuelve `files[].path` el manifiesto del paquete: `store` (`skills/<skill>/…`, lo que fija
el contrato) o `relative` (relativa a la skill). El cliente debe aguantar las dos.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import yaml

from baas_plugin.errors import McpToolError

SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TEXT_EXTS = {".md", ".py", ".sh", ".json", ".yaml", ".yml", ".txt", ".typ", ".js", ".sql", ".csv"}
MiB = 1024 * 1024


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_hash(files: dict[str, bytes]) -> str:
    lines = (f"{sha(d)}  {p}\n" for p, d in sorted(files.items(), key=lambda kv: kv[0].encode()))
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def is_text(data: bytes) -> bool:
    try:
        return "\x00" not in data.decode("utf-8")
    except UnicodeDecodeError:
        return False


def path_error(path: str) -> str | None:
    if not path or path.startswith("/") or "\\" in path or re.search(r"[\x00-\x1f\x7f]", path):
        return "path.invalid"
    if len(path.encode()) > 1024:
        return "path.invalid"
    segs = path.split("/")
    if any(s in ("", "..") for s in segs) or any(len(s.encode()) > 255 for s in segs):
        return "path.invalid"
    if segs[0] not in ("skills", "knowledge") or len(segs) < 2:
        return "path.invalid"
    if segs[0] == "skills" and (len(segs) < 3 or not SKILL_NAME_RE.match(segs[1])):
        return "path.invalid"
    return None


def frontmatter_of(data: bytes) -> tuple[dict[str, Any], bool, list[str]]:
    """(datos, yaml_válido, claves de primer nivel) de la cabecera de un .md."""
    text = data.decode("utf-8", "replace")
    if not text.startswith("---\n"):
        return {}, True, []
    end = text.find("\n---", 4)
    if end == -1:
        return {}, True, []
    header = text[4:end]
    keys = re.findall(r"^([A-Za-z_][\w-]*):", header, re.M)
    try:
        loaded = yaml.safe_load(header)
        return (loaded if isinstance(loaded, dict) else {}), True, keys
    except yaml.YAMLError:
        lenient = {m.group(1): m.group(2).strip() for m in re.finditer(r"^([A-Za-z_][\w-]*):(.*)$", header, re.M)}
        return lenient, False, keys


class FakeStore:
    """Estado y lógica del MCP simulado (una organización)."""

    def __init__(
        self,
        *,
        org: str = "demo",
        org_roles: tuple[str, ...] = ("facturacion", "analista", "revisor"),
        admin: bool = True,
        user_roles: tuple[str, ...] = (),
        path_style: str = "store",
        large_file_bytes: int = 25 * MiB,
        large_package_bytes: int = 100 * MiB,
        list_page: int | None = None,
    ) -> None:
        self.org = org
        self.org_roles = set(org_roles)
        self.admin = admin
        self.user_roles = set(user_roles)
        self.path_style = path_style
        self.large_file_bytes = large_file_bytes
        self.large_package_bytes = large_package_bytes
        self.list_page = list_page
        self.files: dict[str, bytes] = {}
        self.versions: list[tuple[str, bytes | None, str]] = []
        self.kill_scope: str | None = None
        self.corrupt_reads: dict[str, bytes] = {}  # ruta del almacén -> bytes que devolverá `get_skill_files`
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # carga y descarga por trozos (contrato §7.4)
        self.max_file_bytes = 500 * MiB
        self.max_chunk = 16 * MiB
        self.chunk_size = 8 * MiB
        self.uploads: dict[str, dict[str, Any]] = {}
        self.lose_put_responses = 0  # cuántos `baas_put_chunk` se aplican pero responden con un corte de red
        self.corrupt_chunk_once = 0  # cuántos `baas_put_chunk` llegan corruptos (el sha256 del trozo no coincide)
        self.inline_read_limit: int | None = None  # un `baas_get_skill_files` con un fichero mayor falla (memoria)
        self.inline_write_limit: int | None = None  # un `baas_write_files` con un `content` mayor falla (memoria)
        self.lock = threading.Lock()

    # --- utilidades ----------------------------------------------------------------------------------
    def put(self, path: str, data: bytes | str) -> None:
        self.files[path] = data.encode() if isinstance(data, str) else data

    def skill_files(self, skill: str) -> dict[str, bytes]:
        prefix = f"skills/{skill}/"
        return {p[len(prefix) :]: d for p, d in self.files.items() if p.startswith(prefix)}

    def skills(self) -> list[str]:
        return sorted({p.split("/")[1] for p in self.files if p.startswith("skills/") and p.count("/") >= 2})

    def has_skill(self, skill: str) -> bool:
        return f"skills/{skill}/SKILL.md" in self.files

    def manifest(self, skill: str, files: dict[str, bytes] | None = None) -> dict[str, Any]:
        raw = (files if files is not None else self.files).get(f"skills/{skill}/baas.yaml")
        if raw is None:
            return {}
        data = yaml.safe_load(raw.decode())
        return data if isinstance(data, dict) else {}

    def skill_roles(self, skill: str, files: dict[str, bytes] | None = None) -> list[str]:
        files = self.files if files is None else files
        manifest = self.manifest(skill, files)
        if manifest.get("roles") is not None:
            return list(manifest["roles"])
        md = files.get(f"skills/{skill}/SKILL.md")
        if md is not None:
            fm, _, _ = frontmatter_of(md)
            if fm.get("roles"):
                return list(fm["roles"])
        for acl in (f"skills/{skill}/_acl.yaml", "skills/_acl.yaml"):
            if acl in files:
                return list((yaml.safe_load(files[acl].decode()) or {}).get("roles") or [])
        return []

    def knowledge_roles(self, path: str) -> list[str]:
        if path.endswith(".md"):
            fm, _, _ = frontmatter_of(self.files[path])
            if fm.get("roles"):
                return list(fm["roles"])
        parts = path.split("/")[:-1]
        while parts:
            acl = "/".join([*parts, "_acl.yaml"])
            if acl in self.files:
                return list((yaml.safe_load(self.files[acl].decode()) or {}).get("roles") or [])
            parts.pop()
        return []

    def can_read(self, roles: list[str]) -> bool:
        return self.admin or "*" in roles or bool(set(roles) & self.user_roles)

    def doc_type(self, path: str) -> str:
        if path.startswith("knowledge/"):
            return "knowledge"
        rel = path.split("/", 2)[2]
        return "skill" if rel == "SKILL.md" else "script" if rel.startswith("scripts/") else "file"

    # --- tools -------------------------------------------------------------------------------------------
    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        args = json.loads(json.dumps(args))  # lo que viajaría por la red
        with self.lock:
            self.calls.append((name, args))
            if self.kill_scope:
                raise McpToolError(
                    "KILL_SWITCH_ACTIVE",
                    "El acceso está cortado.",
                    {"error": "KILL_SWITCH_ACTIVE", "scope": self.kill_scope},
                )
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                raise McpToolError("INVALID_ARGUMENT", f"Tool desconocida {name}", {"field": "name"})
            return handler(args)

    def _tool_baas_whoami(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "user": {"email": "orgadmin@demo.test", "is_platform_admin": False},
            "organization": {
                "slug": self.org,
                "governance_role": "ORG_ADMIN" if self.admin else "USER",
                "functional_roles": sorted(self.org_roles if self.admin else self.user_roles),
                "is_admin": self.admin,
            },
            "kill_switch": {"active": False},
        }

    def _tool_baas_get_skill_package(self, args: dict[str, Any]) -> dict[str, Any]:
        skill = args.get("skill", "")
        if not self.has_skill(skill):
            raise McpToolError("NOT_FOUND", f"La skill «{skill}» no existe.", {"resource": f"skills/{skill}"})
        files = self.skill_files(skill)
        manifest = self.manifest(skill)
        if manifest.get("status") == "deprecated" and not self.admin:
            raise McpToolError("NOT_FOUND", "La skill está retirada.", {"resource": f"skills/{skill}"})
        roles = self.skill_roles(skill)
        if not self.can_read(roles):
            raise McpToolError(
                "AUTHORIZATION_DENIED",
                f"No tienes acceso a «skills/{skill}».",
                {"resource": f"skills/{skill}", "required_roles": roles},
            )
        executable = set(manifest.get("executable") or [])
        fm, _, _ = frontmatter_of(files["SKILL.md"])
        prefix = f"skills/{skill}/" if self.path_style == "store" else ""
        dep_skills = []
        for dep in (manifest.get("depends_on") or {}).get("skills") or []:
            accessible = self.has_skill(dep) and self.can_read(self.skill_roles(dep))
            entry: dict[str, Any] = {"name": dep, "accessible": accessible}
            if accessible:
                entry["package_sha256"] = package_hash(self.skill_files(dep))
            dep_skills.append(entry)
        return {
            "skill": skill,
            "name": fm.get("name") or skill,
            "status": manifest.get("status", "active"),
            "roles": roles,
            "package_sha256": package_hash(files),
            "frontmatter": fm,
            "metadata": manifest,
            "files": [
                {
                    "path": prefix + rel,
                    "sha256": sha(data),
                    "size": len(data),
                    "encoding": "utf8" if is_text(data) else "base64",
                    "executable": rel in executable,
                }
                for rel, data in sorted(files.items())
            ],
            "depends_on": {
                "skills": dep_skills,
                "knowledge": (manifest.get("depends_on") or {}).get("knowledge") or [],
            },
            "warnings": [],
        }

    def _tool_baas_get_skill_files(self, args: dict[str, Any]) -> dict[str, Any]:
        pkg = self._tool_baas_get_skill_package(args)
        if self.inline_read_limit is not None:
            for entry in pkg["files"]:
                wanted = args.get("paths")
                if (wanted is None or entry["path"] in wanted) and entry["size"] > self.inline_read_limit:
                    raise McpToolError("UNAVAILABLE", "Un fichero grande no cabe inline.", {})
        skill = args["skill"]
        wanted = args.get("paths")
        prefix = f"skills/{skill}/" if self.path_style == "store" else ""
        out = []
        files = self.skill_files(skill)
        for rel in sorted(files) if wanted is None else wanted:
            if wanted is not None:
                if not rel.startswith(prefix) or rel[len(prefix) :] not in files:
                    raise McpToolError("NOT_FOUND", f"«{rel}» no pertenece a la skill.", {"resource": rel})
                key = rel[len(prefix) :]
            else:
                key = rel
            data = self.corrupt_reads.get(f"skills/{skill}/{key}", files[key])
            encoding = "utf8" if is_text(data) else "base64"
            out.append(
                {
                    "path": prefix + key,
                    "sha256": sha(files[key]),  # el servidor declara el hash del original
                    "size": len(data),
                    "encoding": encoding,
                    "content": data.decode() if encoding == "utf8" else base64.b64encode(data).decode(),
                }
            )
        assert pkg["skill"] == skill
        return {"skill": skill, "files": out}

    def _tool_baas_list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        prefix = args.get("prefix") or ""
        wanted_type = args.get("type")
        visible = []
        for path in sorted(self.files):
            if not path.startswith(prefix) or path.endswith("_acl.yaml"):
                continue
            kind = self.doc_type(path)
            if wanted_type and kind != wanted_type:
                continue
            roles = self.knowledge_roles(path) if kind == "knowledge" else self.skill_roles(path.split("/")[1])
            if not self.can_read(roles):
                continue
            visible.append(
                {
                    "path": path,
                    "type": kind,
                    "sha256": sha(self.files[path]),
                    "size": len(self.files[path]),
                    "status": "active",
                }
            )
        start = int(args.get("cursor") or 0)
        limit = int(args.get("limit") or self.list_page or len(visible) or 1)
        page = visible[start : start + limit]
        nxt = str(start + limit) if start + limit < len(visible) else None
        return {"files": page, "next_cursor": nxt}

    def _tool_baas_begin_upload(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_admin()
        path, size, declared = args.get("path"), args.get("size"), args.get("sha256")
        bad = path_error(path) if isinstance(path, str) else "path.invalid"
        if bad:
            raise McpToolError("INVALID_ARGUMENT", "Ruta no válida", {"field": "path"})
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise McpToolError("INVALID_ARGUMENT", "Tamaño no válido", {"field": "size"})
        if size > self.max_file_bytes:
            raise McpToolError("INVALID_ARGUMENT", "El fichero pasa del tope por fichero (MiB)", {"field": "size"})
        upload_id = secrets.token_urlsafe(18)
        self.uploads[upload_id] = {"path": path, "size": size, "sha256": declared, "data": bytearray()}
        return {
            "upload_id": upload_id,
            "path": path,
            "size": size,
            "chunk_size": self.chunk_size,
            "max_chunk_size": self.max_chunk,
            "expires_at": "2099-01-01T00:00:00.000000Z",
        }

    def _tool_baas_put_chunk(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_admin()
        up = self.uploads.get(args.get("upload_id", ""))
        if up is None:
            raise McpToolError("NOT_FOUND", "No existe la carga.", {"resource": str(args.get("upload_id"))})
        data = base64.b64decode(args["content"], validate=True)
        if len(data) > self.max_chunk:
            raise McpToolError(
                "INVALID_ARGUMENT", "Trozo demasiado grande", {"field": "content", "code": "chunk.too_large"}
            )
        if self.corrupt_chunk_once > 0:
            self.corrupt_chunk_once -= 1
            data = b"\x00" + data[1:]
        if args.get("sha256") and sha(data) != args["sha256"]:
            raise McpToolError(
                "INVALID_ARGUMENT",
                "El sha256 del trozo no coincide",
                {"field": "sha256", "code": "chunk.sha256_mismatch"},
            )
        received = len(up["data"])
        if args["offset"] != received:
            raise McpToolError(
                "INVALID_ARGUMENT",
                "El trozo no empieza donde toca",
                {"field": "offset", "code": "upload.offset", "received": received, "size": up["size"]},
            )
        if received + len(data) > up["size"]:
            raise McpToolError(
                "INVALID_ARGUMENT", "Se sale del tamaño", {"field": "content", "code": "upload.overflow"}
            )
        up["data"] += data
        if self.lose_put_responses > 0:
            self.lose_put_responses -= 1
            from baas_plugin.errors import NetworkError

            raise NetworkError("Se cortó la conexión antes de recibir la respuesta.")
        return {
            "upload_id": args["upload_id"],
            "received": len(up["data"]),
            "size": up["size"],
            "complete": len(up["data"]) == up["size"],
        }

    def _tool_baas_abort_upload(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_admin()
        return {"ok": True, "aborted": self.uploads.pop(args.get("upload_id", ""), None) is not None}

    def _tool_baas_read_chunk(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path", "")
        if path not in self.files:
            raise McpToolError("NOT_FOUND", "No existe el fichero.", {"resource": path})
        roles = self.knowledge_roles(path) if path.startswith("knowledge/") else self.skill_roles(path.split("/")[1])
        if not self.can_read(roles):
            raise McpToolError(
                "AUTHORIZATION_DENIED", f"No tienes acceso a «{path}».", {"resource": path, "required_roles": roles}
            )
        data = self.corrupt_reads.get(path, self.files[path])
        digest = sha(self.files[path])
        if args.get("expected_sha256") and args["expected_sha256"] != digest:
            raise McpToolError("CONFLICT", "El fichero cambió.", {"resource": path, "current_sha256": digest})
        offset = int(args.get("offset") or 0)
        length = int(args.get("length") or self.chunk_size)
        if offset > len(data):
            raise McpToolError("INVALID_ARGUMENT", "Desplazamiento fuera del fichero", {"field": "offset"})
        piece = data[offset : offset + min(length, self.max_chunk)]
        return {
            "path": path,
            "size": len(data),
            "sha256": digest,
            "offset": offset,
            "length": len(piece),
            "eof": offset + len(piece) >= len(data),
            "encoding": "base64",
            "content": base64.b64encode(piece).decode(),
        }

    def _require_admin(self) -> None:
        if not self.admin:
            raise McpToolError(
                "AUTHORIZATION_DENIED",
                "Solo un administrador puede escribir.",
                {"resource": "org:" + self.org, "required_roles": ["ORG_ADMIN", "PLATFORM_ADMIN"]},
            )

    def _role_failures(self, path: str, roles: Any) -> list[dict[str, Any]]:
        """`*` (solo) o roles existentes; vale igual en `baas.yaml`, `_acl.yaml` y cabeceras (contrato §7.4)."""
        if roles is None or roles == ["*"]:
            return []
        if not isinstance(roles, list) or "*" in roles:
            return [{"path": path, "field": "roles", "code": "roles.invalid", "message": f"Roles no válidos: {roles}"}]
        unknown = sorted(set(roles) - self.org_roles)
        if unknown:
            return [
                {"path": path, "field": "roles", "code": "roles.unknown", "message": f"Roles inexistentes: {unknown}"}
            ]
        return []

    def _tool_baas_write_files(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self.admin:
            raise McpToolError(
                "AUTHORIZATION_DENIED",
                "Solo un administrador puede escribir.",
                {"resource": "org:" + self.org, "required_roles": ["ORG_ADMIN", "PLATFORM_ADMIN"]},
            )
        failures: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        incoming: dict[str, bytes] = {}
        uploads_used: list[str] = []
        for f in args.get("files") or []:
            path = f.get("path", "")
            bad = path_error(path)
            if bad:
                failures.append({"path": path, "field": "path", "code": bad, "message": "Ruta no válida"})
                continue
            consumed: str | None = None
            if f.get("upload_id") is not None:
                up = self.uploads.get(f["upload_id"])
                if "content" in f:
                    failures.append(
                        {
                            "path": path,
                            "field": "upload_id",
                            "code": "content.invalid",
                            "message": "content y upload_id",
                        }
                    )
                    continue
                if up is None:
                    failures.append({"path": path, "field": "upload_id", "code": "upload.unknown", "message": "?"})
                    continue
                if up["path"] != path:
                    failures.append({"path": path, "field": "upload_id", "code": "upload.path", "message": "?"})
                    continue
                if len(up["data"]) != up["size"]:
                    failures.append({"path": path, "field": "upload_id", "code": "upload.incomplete", "message": "?"})
                    continue
                if up["sha256"] and sha(bytes(up["data"])) != up["sha256"]:
                    failures.append(
                        {"path": path, "field": "upload_id", "code": "upload.sha256_mismatch", "message": "?"}
                    )
                    continue
                data = bytes(up["data"])
                consumed = f["upload_id"]
            else:
                try:
                    data = (
                        base64.b64decode(f["content"], validate=True)
                        if f.get("encoding", "utf8") == "base64"
                        else f["content"].encode("utf-8")
                    )
                except (ValueError, KeyError):
                    failures.append(
                        {"path": path, "field": "content", "code": "content.invalid", "message": "Contenido no válido"}
                    )
                    continue
                if self.inline_write_limit is not None and len(data) > self.inline_write_limit:
                    raise McpToolError("UNAVAILABLE", "Un fichero grande no cabe inline.", {})
            if consumed is not None:
                uploads_used.append(consumed)
            expected = f.get("expected_sha256")
            current = sha(self.files[path]) if path in self.files else None
            if (expected == "none" and current is not None) or (expected not in (None, "none") and expected != current):
                conflicts.append({"path": path, "expected_sha256": expected, "current_sha256": current})
            if path in incoming:
                failures.append({"path": path, "field": "path", "code": "path.duplicate", "message": "Ruta repetida"})
            incoming[path] = data
        deletes: list[str] = []
        for path in args.get("delete") or []:
            parts = path.split("/")
            if path_error(path) or parts[0] != "skills" or len(parts) < 3:
                failures.append(
                    {
                        "path": path,
                        "field": "delete",
                        "code": "delete.scope",
                        "message": "Solo dentro de skills/<skill>/",
                    }
                )
            elif path not in self.files:
                failures.append({"path": path, "field": "delete", "code": "delete.missing", "message": "No existe"})
            else:
                deletes.append(path)
        new_files = {**self.files, **incoming}
        for p in deletes:
            new_files.pop(p, None)
        touched = sorted({p.split("/")[1] for p in [*incoming, *deletes] if p.startswith("skills/")})
        warnings: list[dict[str, Any]] = []
        for path, data in incoming.items():
            if path.endswith("/baas.yaml"):
                try:
                    manifest = yaml.safe_load(data.decode())
                    if not isinstance(manifest, dict) or manifest.get("version") != 1:
                        raise ValueError("version")
                except Exception:
                    failures.append(
                        {
                            "path": path,
                            "field": "baas.yaml",
                            "code": "manifest.invalid",
                            "message": "baas.yaml no válido",
                        }
                    )
                    continue
                failures.extend(self._role_failures(path, manifest.get("roles")))
            if path.endswith("/_acl.yaml"):
                acl = yaml.safe_load(data.decode()) or {}
                failures.extend(self._role_failures(path, acl.get("roles")))
        if failures:
            raise McpToolError(
                "INVALID_ARGUMENT",
                "El lote no es válido; no se ha escrito nada.",
                {"field": "files", "failures": failures},
            )
        if conflicts:
            raise McpToolError(
                "CONFLICT",
                "Hay ficheros que han cambiado; no se ha escrito nada.",
                {
                    "resource": conflicts[0]["path"],
                    "current_sha256": conflicts[0]["current_sha256"],
                    "conflicts": conflicts,
                },
            )
        # avisos y respuesta
        for skill in touched:
            sf = {p: d for p, d in new_files.items() if p.startswith(f"skills/{skill}/")}
            if not self.skill_roles(skill, new_files):
                warnings.append(
                    {
                        "code": "NO_ROLES",
                        "path": f"skills/{skill}",
                        "message": "La skill queda solo para administradores.",
                    }
                )
            manifest = self.manifest(skill, new_files)
            for dep in (manifest.get("depends_on") or {}).get("skills") or []:
                if f"skills/{dep}/SKILL.md" not in new_files:
                    warnings.append(
                        {
                            "code": "DEPENDENCY_MISSING",
                            "path": f"skills/{skill}",
                            "message": f"La skill «{dep}» no existe.",
                        }
                    )
            for ex in manifest.get("executable") or []:
                if f"skills/{skill}/{ex}" not in new_files:
                    warnings.append(
                        {
                            "code": "EXECUTABLE_MISSING",
                            "path": f"skills/{skill}/{ex}",
                            "message": "No está en el paquete.",
                        }
                    )
            md = new_files.get(f"skills/{skill}/SKILL.md")
            if md is not None and f"skills/{skill}/SKILL.md" in incoming:
                _fm, strict, keys = frontmatter_of(md)
                if not strict:
                    warnings.append(
                        {"code": "HEADER_LENIENT", "path": f"skills/{skill}/SKILL.md", "message": "Cabecera no YAML."}
                    )
                if "hooks" in keys:
                    warnings.append(
                        {"code": "FRONTMATTER_HOOKS", "path": f"skills/{skill}/SKILL.md", "message": "Declara hooks."}
                    )
                if "allowed-tools" in keys:
                    warnings.append(
                        {
                            "code": "FRONTMATTER_ALLOWED_TOOLS",
                            "path": f"skills/{skill}/SKILL.md",
                            "message": "Declara allowed-tools.",
                        }
                    )
            if sum(len(d) for d in sf.values()) > self.large_package_bytes:
                warnings.append({"code": "LARGE_PACKAGE", "path": f"skills/{skill}", "message": "Paquete muy grande."})
        for path, data in incoming.items():
            if len(data) > self.large_file_bytes:
                warnings.append({"code": "LARGE_FILE", "path": path, "message": "Fichero muy grande."})
            ext = "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
            if ext in TEXT_EXTS and not is_text(data):
                warnings.append({"code": "NOT_UTF8_TEXT", "path": path, "message": "Se guarda como binario."})
        entries = []
        for path, data in incoming.items():
            previous = self.files.get(path)
            entries.append(
                {
                    "path": path,
                    "type": self.doc_type(path),
                    "sha256": sha(data),
                    "previous_sha256": sha(previous) if previous is not None else None,
                    "size": len(data),
                    "created": previous is None,
                    "changed": previous is not None and previous != data,
                }
            )
        packages = []
        for skill in touched:
            sf = {p[len(f"skills/{skill}/") :]: d for p, d in new_files.items() if p.startswith(f"skills/{skill}/")}
            packages.append(
                {
                    "skill": skill,
                    "package_sha256": package_hash(sf),
                    "files": len(sf),
                    "size": sum(len(d) for d in sf.values()),
                }
            )
        dry = bool(args.get("dry_run"))
        summary = {
            "files": len(entries),
            "bytes": sum(e["size"] for e in entries),
            "created": sum(e["created"] for e in entries),
            "changed": sum(e["changed"] for e in entries),
            "unchanged": sum(1 for e in entries if not e["created"] and not e["changed"]),
            "estimated_sync_bytes": sum(p["size"] for p in packages),
        }
        if not dry:
            for upload_id in uploads_used:
                self.uploads.pop(upload_id, None)
            for path, data in incoming.items():
                self.versions.append((path, self.files.get(path), "write"))
                self.files[path] = data
            for path in deletes:
                self.versions.append((path, self.files.pop(path), "delete"))
        return {
            "ok": True,
            "dry_run": dry,
            "files": entries,
            "deleted": deletes,
            "packages": packages,
            "summary": summary,
            "warnings": warnings,
        }


class DirectClient:
    """Mismo `call_tool` que `McpClient`, sin red."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return self.store.call(name, arguments)

    def __enter__(self) -> DirectClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakePublishServer:
    """Servidor HTTP mínimo (Streamable HTTP, sin sesión) delante de un `FakeStore`."""

    def __init__(self, store: FakeStore, *, api_key: str) -> None:
        self.store = store
        self.api_key = api_key
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send(self, status: int, data: Any) -> None:
                body = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if self.headers.get("Authorization") != f"Bearer {outer.api_key}":
                    return self._send(401, {"error": "UNAUTHENTICATED", "message": "API key no válida"})
                msg = json.loads(body)
                if "id" not in msg:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                method = msg.get("method")
                if method == "initialize":
                    result: dict[str, Any] = {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake"},
                    }
                elif method == "tools/call":
                    params = msg["params"]
                    try:
                        data = outer.store.call(params["name"], params.get("arguments") or {})
                        result = {
                            "content": [{"type": "text", "text": json.dumps(data)}],
                            "structuredContent": data,
                            "isError": False,
                        }
                    except McpToolError as exc:
                        payload = {"error": exc.code, "message": exc.message.split(": ", 1)[-1], **exc.payload}
                        result = {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": True}
                else:
                    return self._send(
                        200, {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "método"}}
                    )
                return self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": result})

            def do_DELETE(self) -> None:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def mcp_url(self) -> str:
        return f"{self.url}/org/{self.store.org}/mcp"

    def __enter__(self) -> FakePublishServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
