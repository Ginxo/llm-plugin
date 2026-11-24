"""Reescritura determinista del `SKILL.md` de Claude Code para las skills nativas de otra herramienta (T20 y T22).

Un `SKILL.md` de una skill de la organización está escrito para Claude Code: llama a sus scripts con
`python3 "${CLAUDE_SKILL_DIR}/scripts/x.py" …` (variable que Antigravity no define), a veces con una ruta relativa
(«ejecuta desde la carpeta de la skill: `python3 scripts/x.py`») y enlaza o cita a otras skills por su nombre
(`../facturacion/SKILL.md`, `/gestor`), que en Antigravity llevan el prefijo de la organización. Como `sync` copia los
scripts verificados a la carpeta de la skill (el mismo modelo que Claude Code), esto lo deja usable sin inventar nada:

- **La carpeta de la skill.** `${CLAUDE_SKILL_DIR}` (con o sin llaves, con o sin comillas, también en menciones) pasa a
  la ruta absoluta de la carpeta nativa de la skill, bien entrecomillada si hace falta (espacios, paréntesis…). Una
  llamada con ruta relativa (`python3 scripts/x.py`, `./scripts/x.sh`) solo se resuelve si el texto dice con certeza que
  se ejecuta desde la carpeta de la skill («desde la carpeta de la skill» en la misma línea o en la que precede al
  bloque de código, `cd "${CLAUDE_SKILL_DIR}" && …` o un `cd` anterior del mismo bloque). Los argumentos originales se
  dejan **tal cual, como texto**: no se reinterpretan.
- **Enlaces y citas.** `[…](../facturacion/SKILL.md)` → `…/<org>-facturacion/…` y `` `/gestor` `` →
  `` `/<org>-gestor` ``, solo cuando coinciden exactamente con una skill sincronizada.

Todo es conservador: se reescribe solo lo que se reconoce con certeza y el resto se deja intacto (y `RewriteReport`
lo cuenta, para que `sync` lo avise). Nada se reescribe dentro de bloques de código salvo la carpeta de la skill y las
llamadas relativas con contexto, y nunca en prosa suelta salvo la variable. Cada pieza que entra en el texto se valida
antes (nombres con la misma expresión que las carpetas; la ruta de la carpeta, sin caracteres que un shell
reinterprete): un `SKILL.md` remoto no puede inyectar nada. Es una función pura y su resultado depende solo del texto y
del contexto, así que el `sync` es idempotente.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_DIR_FORBIDDEN = frozenset('"$`%!^&|<>;*?')  # el shell los interpretaría aun entre comillas dobles
_PLAIN_DIR = re.compile(r"^[A-Za-z0-9._/:+@-]+$")  # no necesita comillas en ningún shell habitual
_KIND_BY_EXTENSION = {".py": "python", ".sh": "bash", ".bash": "bash", ".js": "node", ".mjs": "node", ".cjs": "node"}
_KIND_BY_INTERPRETER = {"python3": "python", "python": "python", "bash": "bash", "sh": "bash", "node": "node"}
_HINTS = (
    "desde la carpeta de la skill",
    "desde la carpeta del skill",
    "desde la carpeta de esta skill",
    "desde el directorio de la skill",
    "desde el directorio del skill",
    "desde el directorio de esta skill",
    "from the skill folder",
    "from the skill directory",
    "from the skill's folder",
    "from the skill's directory",
    "from this skill's folder",
    "from this skill's directory",
)
_HINT = re.compile("|".join(re.escape(h) for h in _HINTS), re.IGNORECASE)

_VAR = r"\$(?:\{CLAUDE_SKILL_DIR\}|CLAUDE_SKILL_DIR(?![A-Za-z0-9_]))"
_REL = r"scripts/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*"
_END = r"(?=[ \t\r]|$|[;&|)>`<])"
_PRE = r"(?P<pre>^[ \t>]*(?:\$[ \t]+)?|(?:&&|\|\||;|\|)[ \t]*)"
_INTERP = r"(?P<interp>python3|python|bash|sh|node)"
_CALL = re.compile(_PRE + rf'(?:{_INTERP}[ \t]+)?(?:"{_VAR}/(?P<relq>{_REL})"|{_VAR}/(?P<relu>{_REL})){_END}')
_REL_CALL = re.compile(
    _PRE + rf'(?:{_INTERP}[ \t]+(?:"(?:\./)?(?P<relq>{_REL})"|(?:\./)?(?P<relu>{_REL}))|\./(?P<reld>{_REL})){_END}'
)
_RELATIVE_CALL = re.compile(_PRE + r"(?:(?:python3?|bash|sh|node)[ \t]+\"?(?:\./)?|\./)scripts/")
_RELATIVE_ANY = re.compile(r"(?:python3?|bash|sh|node)[ \t]+\"?(?:\./)?scripts/|(?<![\w/.-])\./scripts/")
_CD = re.compile(rf'(?:^[ \t>]*(?:\$[ \t]+)?|(?:&&|;)[ \t]*)cd[ \t]+(?:"{_VAR}/?"|{_VAR}/?)[ \t]*(?=&&|;|\r|$)')
_CD_ANY = re.compile(r"(?:^[ \t>]*(?:\$[ \t]+)?|(?:&&|;)[ \t]*)cd\b")
_VAR_RE = re.compile(_VAR)
_PATH_TAIL = re.compile(r"(?:/[A-Za-z0-9._-]+)*/?")
_BOUNDARY = frozenset(" \t\r;&|)<>`")
_FENCE_OPEN = re.compile(r"^\s*(?:>\s*)*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_CODE_SPAN = re.compile(r"(?<!`)(?P<ticks>`+)(?!`)(?P<body>.+?)(?<!`)(?P=ticks)(?!`)")
_CITATION = re.compile(r"^/(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)$")
_LINK = re.compile(
    r"\[(?P<text>(?:[^\[\]\\]|\\.)*)\]\((?P<target>[^()\s]+)(?P<title>[ \t]+(?:\"[^\"]*\"|'[^']*'))?[ \t]*\)"
)
_DEFINITION = re.compile(r"^(?P<head>\s{0,3}\[[^\]]+\]:[ \t]*<?)(?P<target>[^\s>]+)")
_SKILL_LINK = re.compile(r"^\.\./(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)/(?P<rest>.*)$")


def safe_dir(path: str | None, *, windows: bool | None = None) -> str | None:
    """`path` si es una ruta absoluta que se puede escribir entre comillas dobles sin que ningún shell la reinterprete
    (sin `"`, `$`, `` ` ``, `%`, `!`, saltos de línea…; la barra invertida solo en Windows); `None` si no."""
    windows = os.name == "nt" if windows is None else windows
    if not isinstance(path, str) or not path:
        return None
    if any(ord(c) < 32 or ord(c) == 127 or c in _SAFE_DIR_FORBIDDEN for c in path):
        return None
    if not windows and "\\" in path:
        return None
    if not (PurePosixPath(path).is_absolute() or (windows and PureWindowsPath(path).is_absolute())):
        return None
    return path


@dataclass(frozen=True)
class RewriteContext:
    org: str  # slug de la organización
    skill: str  # nombre original de la skill (el del MCP, no el prefijado)
    synced: frozenset[str]  # skills sincronizadas de la organización (nombres originales)
    scripts: frozenset[str]  # rutas, relativas a la skill, de los ficheros del paquete (que se copian)
    skill_dir: str | None  # carpeta nativa de la skill; si no es segura (`safe_dir`), no se resuelve nada
    executable: frozenset[str] = frozenset()  # los que el paquete declara ejecutables
    windows: bool = False  # en Windows, `python3` pasa a `python` en las llamadas reconocidas


@dataclass
class RewriteReport:
    calls: list[tuple[str | None, str]] = field(default_factory=list)  # (intérprete o `None`, ruta) de cada llamada
    links: int = 0
    citations: int = 0
    unresolved: int = 0  # `${CLAUDE_SKILL_DIR}` que quedan en el texto
    relative_scripts: int = 0  # llamadas a `scripts/x` con ruta relativa que no se resolvieron
    refs: list[str] = field(default_factory=list)  # nombres citados o enlazados (sincronizados o no)
    uses_dir: bool = False  # ¿el texto original depende de la carpeta de la skill?

    @property
    def scripts(self) -> list[str]:
        return [rel for _interp, rel in self.calls]


class _Rewriter:
    def __init__(self, ctx: RewriteContext) -> None:
        for what, value in (("organización", ctx.org), ("skill", ctx.skill)):
            if not NAME_RE.match(value):
                raise ValueError(f"Nombre de {what} no válido para una orden: {value!r}")
        self.ctx = ctx
        self.dir = safe_dir(ctx.skill_dir, windows=ctx.windows)
        self.report = RewriteReport()
        self._refs: set[str] = set()
        self._last_prose = ""
        self._block_hint = False
        self._block_cwd = False

    # --- la carpeta de la skill --------------------------------------------------------------------

    def _script_ok(self, rel: str, interpreter: str | None) -> bool:
        """¿`rel` es un script del paquete y el intérprete (o su extensión, si no hay) es coherente?"""
        ctx = self.ctx
        if rel not in ctx.scripts or any(part in (".", "..") for part in rel.split("/")):
            return False
        kind = _KIND_BY_EXTENSION.get(PurePosixPath(rel).suffix.lower())
        if interpreter is not None:
            return kind is not None and kind == _KIND_BY_INTERPRETER[interpreter]
        return kind is not None or rel in ctx.executable

    def _relative(self, line: str, hint: bool, cwd: bool) -> str:
        """Llamadas con ruta relativa → `"${CLAUDE_SKILL_DIR}/scripts/x"`, solo si se sabe que es la de la skill."""
        cd = _CD.search(line)
        cd_end = cd.end() if cd else None

        def sub(m: re.Match[str]) -> str:
            rel = m.group("relq") or m.group("relu") or m.group("reld")
            if not (cwd or hint or (cd_end is not None and m.start() >= cd_end)):
                return m.group(0)
            if not self._script_ok(rel, m.group("interp")):
                return m.group(0)
            interp = f"{m.group('interp')} " if m.group("interp") else ""
            return f'{m.group("pre")}{interp}"${{CLAUDE_SKILL_DIR}}/{rel}"'

        return _REL_CALL.sub(sub, line)

    def _record(self, line: str, in_span: bool) -> str:
        """Anota las llamadas a scripts de la skill (para el informe y la sugerencia de permisos)."""

        def sub(m: re.Match[str]) -> str:
            rel = m.group("relq") or m.group("relu")
            interp = m.group("interp")
            if interp is None and in_span and not line[m.end() :].strip():  # una ruta a solas es una mención
                return m.group(0)
            if not self._script_ok(rel, interp):
                return m.group(0)
            shown = "python" if (self.ctx.windows and interp == "python3") else interp
            self.report.calls.append((shown, rel))
            if shown == interp:
                return m.group(0)
            pre = m.group("pre")
            return pre + str(shown) + m.group(0)[len(pre) + len(str(interp)) :]

        return _CALL.sub(sub, line)

    def _substitute(self, code: str) -> str:
        """`${CLAUDE_SKILL_DIR}` → la carpeta, respetando las comillas del shell (dobles: tal cual; sin comillas: se
        entrecomilla la palabra si hace falta; simples: no se toca, el shell no expandiría nada)."""
        folder = self.dir
        if folder is None or "CLAUDE_SKILL_DIR" not in code:
            return code
        out: list[str] = []
        i, dq, sq = 0, False, False
        while i < len(code):
            ch = code[i]
            if ch == "\\" and not sq:
                out.append(code[i : i + 2])
                i += 2
                continue
            if ch == "'" and not dq:
                sq = not sq
            elif ch == '"' and not sq:
                dq = not dq
            elif ch == "$":
                m = _VAR_RE.match(code, i)
                if m and not sq:
                    if dq or _PLAIN_DIR.match(folder):
                        out.append(folder)
                        i = m.end()
                        continue
                    tail = _PATH_TAIL.match(code, m.end())
                    end = tail.end() if tail else m.end()
                    if end >= len(code) or code[end] in _BOUNDARY:
                        out.append(f'"{folder}{code[m.end() : end]}"')
                        i = end
                        continue
            out.append(ch)
            i += 1
        return "".join(out)

    def code(self, line: str, *, in_span: bool, hint: bool = False, cwd: bool = False) -> str:
        """Una línea de un bloque de código o el contenido de un fragmento de código en línea."""
        if self.dir is not None:
            line = self._relative(line, hint, cwd)
            line = self._record(line, in_span)
            line = self._substitute(line)
        self.report.relative_scripts += len(_RELATIVE_CALL.findall(line))
        return line

    # --- enlaces y citas ---------------------------------------------------------------------------

    def _target(self, target: str) -> str | None:
        m = _SKILL_LINK.match(target)
        if not m:
            return None
        name = m.group("name")
        self._refs.add(name)
        if name not in self.ctx.synced:
            return None
        self.report.links += 1
        return f"../{self.ctx.org}-{name}/{m.group('rest')}"

    def _citation(self, text: str) -> str | None:
        """`text` es exactamente `/<skill>`: lo prefija si es una skill sincronizada."""
        m = _CITATION.match(text)
        if not m:
            return None
        name = m.group("name")
        self._refs.add(name)
        if name not in self.ctx.synced:
            return None
        self.report.citations += 1
        return f"/{self.ctx.org}-{name}"

    def prose_line(self, line: str) -> str:
        spans = [(m.start(), m.end(), m.start("body"), m.end("body")) for m in _CODE_SPAN.finditer(line)]
        edits: list[tuple[int, int, str]] = []
        for _start, _end, body_start, body_end in spans:
            body = line[body_start:body_end]
            stripped = body.strip()
            new = self._citation(stripped)
            if new is not None:
                lead = body[: len(body) - len(body.lstrip())]
                edits.append((body_start, body_end, lead + new + body[len(lead) + len(stripped) :]))

        def in_span(pos: int) -> bool:
            return any(start <= pos < end for start, end, _bs, _be in spans)

        for m in _LINK.finditer(line):
            if in_span(m.start()):
                continue
            new_target = self._target(m.group("target"))
            if new_target is not None:
                edits.append((m.start("target"), m.end("target"), new_target))
            new_text = self._citation(m.group("text"))  # texto sin comillas de código: `[/gestor](…)`
            if new_text is not None:
                edits.append((m.start("text"), m.end("text"), new_text))
        d = _DEFINITION.match(line)
        if d and not in_span(d.start("target")):
            new_target = self._target(d.group("target"))
            if new_target is not None:
                edits.append((d.start("target"), d.end("target"), new_target))
        for start, end, text in sorted(edits, reverse=True):
            line = line[:start] + text + line[end:]
        return self._folder_in_prose(line)

    def _folder_in_prose(self, line: str) -> str:
        """La carpeta de la skill en una línea de texto: dentro de los fragmentos de código se trata como un comando
        (llamadas y comillas); fuera de ellos, un simple reemplazo."""
        if "CLAUDE_SKILL_DIR" not in line and not _RELATIVE_ANY.search(line):
            return line
        hint = bool(_HINT.search(line))
        out: list[str] = []
        last = 0
        for m in _CODE_SPAN.finditer(line):
            out.append(self._prose_text(line[last : m.start("body")]))
            out.append(self.code(m.group("body"), in_span=True, hint=hint))
            last = m.end("body")
        out.append(self._prose_text(line[last:]))
        return "".join(out)

    def _prose_text(self, text: str) -> str:
        folder = self.dir
        return text if folder is None else _VAR_RE.sub(lambda _m: folder, text)

    # --- documento ---------------------------------------------------------------------------------

    def run(self, text: str) -> str:
        self.report.uses_dir = bool(_VAR_RE.search(text) or _RELATIVE_ANY.search(text))
        out: list[str] = []
        fence: tuple[str, int] | None = None  # (carácter, longitud) del bloque abierto
        for line in text.split("\n"):
            if fence is not None:
                closing = line.strip().lstrip(">").strip()
                if closing and set(closing) == {fence[0]} and len(closing) >= fence[1]:
                    fence = None
                    out.append(line)
                else:
                    out.append(self.code(line, in_span=False, hint=self._block_hint, cwd=self._block_cwd))
                    self._block_cwd = self._cwd_after(line, self._block_cwd)
                continue
            opened = _FENCE_OPEN.match(line)
            if opened and not (opened.group("fence")[0] == "`" and "`" in opened.group("info")):
                fence = (opened.group("fence")[0], len(opened.group("fence")))
                self._block_hint = bool(_HINT.search(self._last_prose))
                self._block_cwd = False
                out.append(line)
                continue
            if line.strip():
                self._last_prose = line
            out.append(self.prose_line(line))
        result = "\n".join(out)
        self.report.unresolved = len(_VAR_RE.findall(result))
        self.report.refs = sorted(self._refs)
        return result

    @staticmethod
    def _cwd_after(line: str, current: bool) -> bool:
        """Tras esta línea, ¿el directorio de trabajo del bloque es la carpeta de la skill? (último `cd` de la línea)"""
        last = None
        for m in _CD_ANY.finditer(line):
            last = m
        if last is None:
            return current
        return last.start() in {m.start() for m in _CD.finditer(line)}


def rewrite(text: str, ctx: RewriteContext) -> tuple[str, RewriteReport]:
    """`(texto reescrito, informe)`. Lanza `ValueError` si un nombre no es seguro para entrar en el texto."""
    rewriter = _Rewriter(ctx)
    return rewriter.run(text), rewriter.report
