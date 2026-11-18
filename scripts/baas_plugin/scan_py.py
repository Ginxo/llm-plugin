"""Análisis estático de un script Python (con `ast`, sin ejecutarlo).

Extrae lo que importa para publicar una skill como paquete: imports, llamadas a otros scripts, rutas calculadas
desde `__file__`, escrituras (estado mutable) y binarios externos.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

SUBPROCESS_FUNCS = {
    "run",
    "Popen",
    "call",
    "check_call",
    "check_output",
    "system",
    "popen",
    "execv",
    "execve",
    "execvp",
    "execl",
    "execlp",
    "spawnv",
    "spawnl",
}
_WRITE_MODE = re.compile(r"[wax+]")
_KNOWLEDGE_REF = re.compile(r"(?<![\w./~-])(knowledge/[A-Za-z0-9_./<>{}%*-]*[A-Za-z0-9_>}%*])")


@dataclass(frozen=True)
class PathExpr:
    """Una ruta calculada desde `__file__`: `levels` directorios por encima del fichero (1 = su carpeta)."""

    levels: int
    segs: tuple[str, ...] = ()
    root: str | None = None  # variable «raíz» (cadena pura de `__file__`) de la que cuelga


@dataclass
class PyResult:
    imports: list[dict[str, Any]] = field(default_factory=list)
    chains: list[dict[str, Any]] = field(default_factory=list)  # rutas desde __file__ (con nombre o en línea)
    writes: list[dict[str, Any]] = field(default_factory=list)
    other_writes: int = 0
    binaries: list[tuple[str, int]] = field(default_factory=list)
    mentions: list[dict[str, Any]] = field(default_factory=list)
    knowledge_refs: list[tuple[str, int]] = field(default_factory=list)
    parse_error: str | None = None
    has_import_os: bool = False


def _up(p: PathExpr | None, k: int) -> PathExpr | None:
    if p is None:
        return None
    segs = list(p.segs)
    levels = p.levels
    for _ in range(k):
        if segs:
            segs.pop()
        else:
            levels += 1
    return PathExpr(levels, tuple(segs), p.root)


def _seg_parts(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        text = "".join(v.value if isinstance(v, ast.Constant) else "*" for v in node.values)  # type: ignore[union-attr]
    else:
        return ["*"]
    return [part for part in text.split("/") if part not in ("", ".")]


def _join(p: PathExpr | None, parts: list[str]) -> PathExpr | None:
    if p is None:
        return None
    for part in parts:
        p = _up(p, 1) if part == ".." else PathExpr(p.levels, (*p.segs, part), p.root)  # type: ignore[union-attr]
    return p


class _Flow:
    """Evalúa expresiones de ruta que parten de `__file__` y siguen las variables intermedias."""

    def __init__(self) -> None:
        self.tracked: dict[str, PathExpr] = {}

    def eval(self, n: ast.AST | None) -> PathExpr | None:
        if n is None:
            return None
        if isinstance(n, ast.Name):
            return PathExpr(0) if n.id == "__file__" else self.tracked.get(n.id)
        if isinstance(n, ast.Attribute):
            return _up(self.eval(n.value), 1) if n.attr == "parent" else None
        if isinstance(n, ast.Subscript):
            v = n.value
            if (
                isinstance(v, ast.Attribute)
                and v.attr == "parents"
                and isinstance(n.slice, ast.Constant)
                and isinstance(n.slice.value, int)
            ):
                return _up(self.eval(v.value), n.slice.value + 1)
            return None
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
            return _join(self.eval(n.left), _seg_parts(n.right))
        if isinstance(n, ast.JoinedStr):  # f"{ROOT}/knowledge/x"
            first = n.values[0] if n.values else None
            if isinstance(first, ast.FormattedValue):
                rest = "".join(v.value if isinstance(v, ast.Constant) else "*" for v in n.values[1:])  # type: ignore[union-attr]
                return _join(self.eval(first.value), [p for p in rest.split("/") if p])
            return None
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            args = n.args
            if isinstance(f, ast.Attribute) and name in ("resolve", "absolute", "expanduser", "as_posix"):
                return self.eval(f.value)
            if isinstance(f, ast.Attribute) and name == "joinpath":
                p = self.eval(f.value)
                for a in args:
                    p = _join(p, _seg_parts(a))
                return p
            if name in ("Path", "PurePath", "PosixPath", "str", "fspath", "abspath", "realpath", "normpath") and args:
                return self.eval(args[0])
            if name == "expanduser" and args:
                return self.eval(args[0])
            if name == "dirname" and args:
                return _up(self.eval(args[0]), 1)
            if name == "join" and args:
                p = self.eval(args[0])
                for a in args[1:]:
                    p = _join(p, _seg_parts(a))
                return p
        return None

    def involves_file(self, n: ast.AST) -> bool:
        return any(isinstance(x, ast.Name) and (x.id == "__file__" or x.id in self.tracked) for x in ast.walk(n))


def _call_name(call: ast.Call) -> tuple[str, str]:
    """(módulo, función) de una llamada: `shutil.which` → ("shutil", "which"); `open` → ("", "open")."""
    f = call.func
    if isinstance(f, ast.Attribute):
        mod = f.value
        if isinstance(mod, ast.Name):
            return mod.id, f.attr
        if isinstance(mod, ast.Attribute):
            return mod.attr, f.attr
        return "", f.attr
    if isinstance(f, ast.Name):
        return "", f.id
    return "", ""


def _const_str(n: ast.AST | None) -> str | None:
    return n.value if isinstance(n, ast.Constant) and isinstance(n.value, str) else None


def _first_binary(call: ast.Call) -> str | None:
    """Primer elemento de la orden de un subproceso, si es una cadena (`["typst", "compile"]`)."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
        word = _const_str(first.elts[0])
    else:
        text = _const_str(first)
        word = text.split()[0] if text and text.split() else None
    if word is None:
        return None
    word = word.rsplit("/", 1)[-1]
    return word if re.fullmatch(r"[A-Za-z][\w.+-]*", word) else None


def scan_python(text: str, own_basenames: set[str]) -> PyResult:
    result = PyResult()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        result.parse_error = f"{type(exc).__name__}: {exc}"
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = re.match(r"\s*(?:import|from)\s+([\w.]+)", line)
            if m:
                result.imports.append(
                    {"module": m.group(1), "top": m.group(1).split(".")[0], "level": 0, "line": lineno}
                )
        return result
    lines = text.splitlines()
    flow = _Flow()

    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))

    # --- imports -------------------------------------------------------------------------------
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result.imports.append(
                    {"module": alias.name, "top": alias.name.split(".")[0], "level": 0, "line": node.lineno}
                )
                if alias.name.split(".")[0] == "os":
                    result.has_import_os = True
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                result.imports.append(
                    {"module": node.module, "top": node.module.split(".")[0], "level": 0, "line": node.lineno}
                )
            elif node.module:
                result.imports.append(
                    {"module": node.module, "top": node.module.split(".")[0], "level": node.level, "line": node.lineno}
                )
            else:
                for alias in node.names:
                    result.imports.append(
                        {"module": alias.name, "top": alias.name, "level": node.level, "line": node.lineno}
                    )

    # --- variables de ruta (punto fijo) ----------------------------------------------------------------
    assigns: list[tuple[str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assigns.append((node.targets[0].id, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            assigns.append((node.target.id, node.value))
    for _ in range(5):
        changed = False
        for name, value in assigns:
            p = flow.eval(value)
            if p is None:
                continue
            if not p.segs:  # directorio puro: esta variable es la raíz de lo que cuelgue de ella
                p = PathExpr(p.levels, (), name)
            if flow.tracked.get(name) != p:
                flow.tracked[name] = p
                changed = True
        if not changed:
            break

    # --- cadenas de ruta, una por expresión máxima ----------------------------------------------------------
    def source(node: ast.AST) -> str:
        return (ast.get_source_segment(text, node) or "").strip()

    def is_alias(node: ast.AST) -> bool:
        """Una cadena sin ninguna operación de subida (`repo = REPO_ROOT.resolve()`): no aporta información nueva."""
        for x in ast.walk(node):
            if isinstance(x, ast.Name) and x.id == "__file__":
                return False
            if isinstance(x, ast.Attribute) and x.attr in ("parent", "parents"):
                return False
            if isinstance(x, ast.Call) and _call_name(x)[1] == "dirname":
                return False
            if isinstance(x, ast.Constant) and x.value == "..":
                return False
        return True

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None
            if isinstance(node, ast.AnnAssign):
                target = node.target
            if value is not None and isinstance(target, ast.Name):
                p = flow.eval(value)
                if p is not None:
                    tracked = flow.tracked.get(target.id, p)
                    stmt_line = lines[node.lineno - 1].strip() if node.lineno == node.end_lineno else None
                    result.chains.append(
                        {
                            "name": target.id,
                            "line": node.lineno,
                            "levels": tracked.levels,
                            "segs": list(tracked.segs),
                            "root": tracked.root,
                            "expr": source(value),
                            "statement": stmt_line,
                            "module_level": node in tree.body,
                            "col": node.col_offset,
                            "alias": is_alias(value),
                        }
                    )
                    return
        if isinstance(node, (ast.Call, ast.Attribute, ast.BinOp, ast.Subscript, ast.JoinedStr)):
            p = flow.eval(node)
            if p is not None and not (isinstance(node, ast.Attribute) and node.attr != "parent"):
                result.chains.append(
                    {
                        "name": None,
                        "line": node.lineno,
                        "levels": p.levels,
                        "segs": list(p.segs),
                        "root": p.root,
                        "expr": source(node),
                        "statement": None,
                        "module_level": False,
                        "col": node.col_offset,
                        "alias": is_alias(node),
                    }
                )
                return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)

    # --- escrituras, binarios y llamadas ------------------------------------------------------------------------
    own_re = (
        re.compile(
            r"(?<![\w.-])("
            + "|".join(re.escape(b) for b in sorted(own_basenames, key=len, reverse=True))
            + r")(?![\w-])"
        )
        if own_basenames
        else None
    )
    in_subprocess: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node)[1] in SUBPROCESS_FUNCS:
            for sub in ast.walk(node):
                in_subprocess.add(id(sub))

    def add_write(node: ast.Call, op: str, target: ast.AST | None) -> None:
        if target is None:
            return
        derived = flow.involves_file(target)
        p = flow.eval(target)
        hint = "/".join(p.segs) if p is not None and p.segs else None
        if derived:
            result.writes.append(
                {
                    "line": node.lineno,
                    "op": op,
                    "target": source(target)[:100],
                    "derived_from_file": True,
                    "path_hint": hint,
                }
            )
        else:
            result.other_writes += 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            for m in _KNOWLEDGE_REF.finditer(node.value):
                result.knowledge_refs.append((m.group(1), node.lineno))
            if own_re is not None:
                for m in own_re.finditer(node.value):
                    result.mentions.append(
                        {
                            "name": m.group(1),
                            "line": node.lineno,
                            "how": "subprocess" if id(node) in in_subprocess else "reference",
                        }
                    )
        if not isinstance(node, ast.Call):
            continue
        mod, fn = _call_name(node)
        f = node.func
        # binarios externos
        if (mod, fn) == ("shutil", "which") and node.args:
            word = _const_str(node.args[0])
            if word:
                result.binaries.append((word, node.lineno))
        elif fn in SUBPROCESS_FUNCS:
            word = _first_binary(node)
            if word:
                result.binaries.append((word, node.lineno))
        # escrituras
        if fn == "open" and isinstance(f, ast.Name) and node.args:
            mode = _const_str(node.args[1]) if len(node.args) > 1 else None
            mode = mode or next((_const_str(k.value) for k in node.keywords if k.arg == "mode"), None)
            if mode and _WRITE_MODE.search(mode):
                add_write(node, f"open({mode})", node.args[0])
        elif isinstance(f, ast.Attribute) and fn == "open" and mod not in ("os", "io", "gzip", "zipfile", "codecs"):
            mode = _const_str(node.args[0]) if node.args else None
            mode = mode or next((_const_str(k.value) for k in node.keywords if k.arg == "mode"), None)
            if mode and _WRITE_MODE.search(mode):
                add_write(node, f"open({mode})", f.value)
        elif isinstance(f, ast.Attribute) and (
            fn in ("write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir")
            or (fn in ("rename", "replace") and mod != "os")
        ):
            add_write(node, fn, f.value)
        elif mod == "os" and fn in ("makedirs", "mkdir", "remove", "unlink", "rmdir") and node.args:
            add_write(node, f"os.{fn}", node.args[0])
        elif mod == "os" and fn in ("rename", "replace") and len(node.args) > 1:
            add_write(node, f"os.{fn}", node.args[1])
        elif mod == "shutil" and fn in ("copy", "copy2", "copyfile", "copytree", "move") and len(node.args) > 1:
            add_write(node, f"shutil.{fn}", node.args[1])
        elif mod == "shutil" and fn == "rmtree" and node.args:
            add_write(node, "shutil.rmtree", node.args[0])
    return result
