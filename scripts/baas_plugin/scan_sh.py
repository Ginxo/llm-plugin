"""Análisis (por expresiones regulares) de scripts de shell y de JavaScript, y vocabulario de binarios.

No ejecuta nada. Es una aproximación honesta: el informe de `inspect` ayuda a decidir, no sustituye a revisar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Intérpretes: se declaran en `requires.interpreters`. El resto de la lista, en `requires.binaries`.
INTERPRETERS = {
    "python": "python3",
    "python3": "python3",
    "bash": "bash",
    "sh": "sh",
    "zsh": "zsh",
    "node": "node",
    "nodejs": "node",
    "deno": "deno",
    "bun": "bun",
    "ruby": "ruby",
    "perl": "perl",
    "php": "php",
    "Rscript": "Rscript",
    "pwsh": "pwsh",
}
EXTERNAL_BINARIES = {
    "typst", "mmdc", "pandoc", "libreoffice", "soffice", "convert", "magick", "ffmpeg", "ffprobe", "jq", "yq",
    "curl", "wget", "git", "gh", "docker", "podman", "kubectl", "terraform", "aws", "gcloud", "az", "psql",
    "sqlite3", "mysql", "redis-cli", "pdftotext", "pdftoppm", "pdflatex", "xelatex", "latexmk", "qpdf", "gs",
    "tesseract", "ocrmypdf", "wkhtmltopdf", "chromium", "google-chrome", "npm", "npx", "pnpm", "yarn", "go",
    "cargo", "rustc", "java", "mvn", "gradle", "make", "cmake", "uv", "pip", "pip3", "poetry", "ssh", "scp",
    "rsync", "gpg", "openssl", "ollama", "claude", "codex",
}  # fmt: skip
_SKIP_WORDS = {
    "if",
    "then",
    "else",
    "elif",
    "while",
    "until",
    "do",
    "done",
    "fi",
    "!",
    "time",
    "sudo",
    "exec",
    "env",
    "nohup",
    "command",
    "builtin",
}
SPLIT_COMMANDS = re.compile(r"\|\||&&|\||;|&|\$\(|`|\(")
_ASSIGN = re.compile(r"^\s*(?:export\s+|local\s+|readonly\s+|declare\s+(?:-\w+\s+)?)?([A-Za-z_]\w*)=(.*)$")
_VAR_REF = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")
_MKDIR = re.compile(r"\bmkdir\b\s+(?:-\w+\s+)*(\S+)")
_REDIRECT = re.compile(r"(?<![\d&<>])(>>?)(?![>&=])\s*(\"?[^\s|;&\")]+\"?)")
_TEE = re.compile(r"\btee\b\s+(?:-a\s+)?(\S+)")
_FILEOPS = re.compile(r"(?:^|[;&|(]\s*|\s)(cp|mv|touch|rm|ln|install)\b\s+((?:-\w+\s+)*)(.+?)(?:[;&|)]|$)")
_SEDI = re.compile(r"\bsed\b\s+(?:-\w+\s+)*-i\S*\s+.*?(\S+)\s*$")
_WORD = re.compile(r"[A-Za-z][\w.+-]*")


def command_words(segment: str) -> str | None:
    """Primera palabra de una orden de shell, sin prefijos como `sudo` ni asignaciones de entorno."""
    for raw in segment.split():
        word = raw.strip("\"'(){}")
        if not word or word in _SKIP_WORDS or re.match(r"^[A-Za-z_]\w*=", word):
            continue
        base = word.rsplit("/", 1)[-1]
        return base if _WORD.fullmatch(base) else None
    return None


def classify_binary(word: str) -> tuple[str, str] | None:
    """`("interpreter", "python3")`, `("binary", "typst")` o `None`."""
    if word in INTERPRETERS:
        return "interpreter", INTERPRETERS[word]
    if word in EXTERNAL_BINARIES:
        return "binary", word
    return None


def shebang_interpreter(first_line: str) -> str | None:
    if not first_line.startswith("#!"):
        return None
    parts = first_line[2:].split()
    if not parts:
        return None
    prog = parts[0].rsplit("/", 1)[-1]
    if prog == "env":
        args = [p for p in parts[1:] if not p.startswith("-") and "=" not in p]
        prog = args[0].rsplit("/", 1)[-1] if args else ""
    return INTERPRETERS.get(prog)


@dataclass
class ShResult:
    interpreter: str | None = None
    chains: list[dict[str, Any]] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    other_writes: int = 0
    binaries: list[tuple[str, int]] = field(default_factory=list)
    interpreters: list[tuple[str, int]] = field(default_factory=list)
    mentions: list[dict[str, Any]] = field(default_factory=list)


def _levels(rhs: str) -> int | None:
    file_based = re.search(r"BASH_SOURCE|\$\{?0\}?|\$\{0%/\*\}", rhs)
    if not file_based:
        return None
    if "dirname" in rhs:
        return rhs.count("dirname") + rhs.count("/..")
    if re.search(r"\$\{(?:BASH_SOURCE\[0\]|0)%/\*\}", rhs):
        return 1 + rhs.count("/..")
    return None


def scan_shell(text: str, own_basenames: set[str]) -> ShResult:
    result = ShResult()
    lines = text.splitlines()
    if lines:
        result.interpreter = shebang_interpreter(lines[0])
    if result.interpreter is None:
        bashisms = bool(re.search(r"\[\[|BASH_SOURCE|\$\{#|=\(", text))
        result.interpreter = "bash" if bashisms else "sh"
    tracked: dict[str, tuple[int, tuple[str, ...], str]] = {}  # var -> (niveles, segmentos, raíz)
    own_re = (
        re.compile(
            r"(?<![\w.-])("
            + "|".join(re.escape(b) for b in sorted(own_basenames, key=len, reverse=True))
            + r")(?![\w-])"
        )
        if own_basenames
        else None
    )

    def expand(token: str) -> tuple[bool, str | None]:
        """¿Depende de una variable derivada del fichero? y la ruta resultante, si se puede calcular."""
        token = token.strip("\"'")
        refs = _VAR_REF.findall(token)
        derived = any(r in tracked for r in refs)
        hint = None
        m = re.match(r"^\$\{?(\w+)\}?(/.*)?$", token)
        if m and m.group(1) in tracked:
            _, segs, _ = tracked[m.group(1)]
            parts = list(segs)
            for part in (m.group(2) or "").split("/"):
                if part in ("", "."):
                    continue
                if part == "..":
                    parts.pop() if parts else None
                else:
                    parts.append(part)
            hint = "/".join(parts) or None
        return derived, hint

    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        code = re.sub(r"\s#\s.*$", "", raw)
        m = _ASSIGN.match(code)
        if m:
            name, rhs = m.group(1), m.group(2)
            lv = _levels(rhs)
            if lv is not None:
                tracked[name] = (lv, (), name)
                result.chains.append(
                    {
                        "name": name,
                        "line": lineno,
                        "levels": lv,
                        "kind": "BASH_SOURCE" if "BASH_SOURCE" in rhs else "$0",
                        "expr": rhs.strip(),
                        "statement": code.strip(),
                        "segs": [],
                        "root": name,
                    }
                )
            else:
                ref = re.match(r"^[\"']?\$\{?(\w+)\}?((?:/[^\"']*)?)[\"']?$", rhs.strip())
                if ref and ref.group(1) in tracked:
                    lv0, segs0, root = tracked[ref.group(1)]
                    parts = list(segs0)
                    up = 0
                    for part in ref.group(2).split("/"):
                        if part in ("", "."):
                            continue
                        if part == "..":
                            if parts:
                                parts.pop()
                            else:
                                up += 1
                        else:
                            parts.append(part)
                    tracked[name] = (lv0 + up, tuple(parts), root if parts else name)
                    result.chains.append(
                        {
                            "name": name,
                            "line": lineno,
                            "levels": lv0 + up,
                            "kind": "BASH_SOURCE",
                            "expr": rhs.strip(),
                            "statement": code.strip(),
                            "segs": parts,
                            "root": root if parts else name,
                        }
                    )
        # binarios e intérpretes
        for segment in SPLIT_COMMANDS.split(code):
            word = command_words(segment)
            if word in ("which", "command", "type"):
                tokens = segment.split()
                word = tokens[-1].strip("\"'") if len(tokens) > 1 else None
            if word:
                cls = classify_binary(word)
                if cls and cls[0] == "interpreter":
                    result.interpreters.append((cls[1], lineno))
                elif cls:
                    result.binaries.append((cls[1], lineno))
        # llamadas a otros scripts
        if own_re is not None:
            for mm in own_re.finditer(code):
                result.mentions.append({"name": mm.group(1), "line": lineno, "how": "shell"})
        # escrituras
        found: list[tuple[str, str]] = []
        for mm in _MKDIR.finditer(code):
            found.append(("mkdir", mm.group(1)))
        for mm in _REDIRECT.finditer(code):
            target = mm.group(2)
            if target.strip("\"'") not in ("/dev/null", "/dev/stderr", "/dev/stdout", "/dev/tty"):
                found.append((f"redirect {mm.group(1)}", target))
        for mm in _TEE.finditer(code):
            found.append(("tee", mm.group(1)))
        for mm in _FILEOPS.finditer(code):
            args = [a for a in mm.group(3).split() if not a.startswith("-")]
            if args:
                found.append((mm.group(1), args[-1] if mm.group(1) in ("cp", "mv", "ln", "install") else args[0]))
        sedi = _SEDI.search(code)
        if sedi:
            found.append(("sed -i", sedi.group(1)))
        for op, target in found:
            derived, hint = expand(target)
            if derived:
                result.writes.append(
                    {
                        "line": lineno,
                        "op": op,
                        "target": target.strip("\"'")[:100],
                        "derived_from_file": True,
                        "path_hint": hint,
                    }
                )
            else:
                result.other_writes += 1
    return result


# --- JavaScript ---------------------------------------------------------------------------------------

_BROWSER = re.compile(
    r"\bwindow\s*\.|\bdocument\s*\.|\blocalStorage\b|\bsessionStorage\b|\bnavigator\s*\.|\bXMLHttpRequest\b"
    r"|\bchrome\.(?:runtime|tabs)\b|\bMutationObserver\b|\bdocument\.querySelector"
)
_NODE = re.compile(
    r"\brequire\s*\(|^\s*import\s[^;\n]*\sfrom\s|\bmodule\.exports\b|\bprocess\.(?:argv|env|cwd|exit|stdout)\b"
    r"|\b__dirname\b|\b__filename\b|\bchild_process\b|^#!.*\bnode\b",
    re.M,
)
_JS_ROOT = re.compile(
    r"(?:const|let|var)\s+(\w+)\s*=\s*path\.(?:join|resolve)\(\s*__dirname((?:\s*,\s*(?:'[^']*'|\"[^\"]*\"|`[^`]*`))*)\s*\)"
)
_JS_WRITE = re.compile(
    r"\bfs(?:\.promises)?\.(writeFileSync|writeFile|appendFileSync|appendFile|mkdirSync|mkdir|rmSync|rm"
    r"|unlinkSync|unlink|renameSync|copyFileSync|createWriteStream)\s*\((.*)$"
)
_JS_EXEC = re.compile(r"\b(?:exec|execSync|spawn|spawnSync|execFile|execFileSync)\s*\(\s*['\"`]([^'\"`]+)['\"`]")


@dataclass
class JsResult:
    browser_snippet: bool = False
    node: bool = False
    chains: list[dict[str, Any]] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    other_writes: int = 0
    binaries: list[tuple[str, int]] = field(default_factory=list)
    mentions: list[dict[str, Any]] = field(default_factory=list)


def scan_js(text: str, own_basenames: set[str]) -> JsResult:
    result = JsResult()
    is_browser = bool(_BROWSER.search(text))
    result.node = bool(_NODE.search(text))
    result.browser_snippet = is_browser and not result.node
    tracked: dict[str, tuple[str, ...]] = {}
    own_re = (
        re.compile(
            r"(?<![\w.-])("
            + "|".join(re.escape(b) for b in sorted(own_basenames, key=len, reverse=True))
            + r")(?![\w-])"
        )
        if own_basenames
        else None
    )
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*")):
            continue
        m = _JS_ROOT.search(line)
        if m:
            args = re.findall(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`", m.group(2))
            parts = [next(g for g in a if g) if any(a) else "" for a in args]
            up = sum(1 for p in parts if p == "..")
            segs = tuple(p for p in parts if p not in ("..", "", "."))
            tracked[m.group(1)] = segs
            result.chains.append(
                {
                    "name": m.group(1),
                    "line": lineno,
                    "levels": 1 + up,
                    "kind": "__dirname",
                    "expr": m.group(0).split("=", 1)[1].strip(),
                    "statement": None,
                    "segs": list(segs),
                    "root": m.group(1),
                }
            )
        w = _JS_WRITE.search(line)
        if w:
            arg = w.group(2)
            refs = {name for name in tracked if re.search(rf"\b{re.escape(name)}\b", arg)}
            derived = bool(refs) or "__dirname" in arg
            if derived:
                lits = re.findall(r"'([^']*)'|\"([^\"]*)\"", arg)
                hint = "/".join(next(g for g in a if g) for a in lits[:2] if any(a)) or None
                result.writes.append(
                    {
                        "line": lineno,
                        "op": f"fs.{w.group(1)}",
                        "target": arg.strip()[:100],
                        "derived_from_file": True,
                        "path_hint": hint,
                    }
                )
            else:
                result.other_writes += 1
        for e in _JS_EXEC.finditer(line):
            word = command_words(e.group(1))
            cls = classify_binary(word) if word else None
            if cls and cls[0] == "binary":
                result.binaries.append((cls[1], lineno))
        if own_re is not None:
            for mm in own_re.finditer(line):
                result.mentions.append({"name": mm.group(1), "line": lineno, "how": "reference"})
    return result
