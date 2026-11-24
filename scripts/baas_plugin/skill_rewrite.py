"""Reescritura determinista del `SKILL.md` de Claude Code para las skills nativas de otra herramienta (T20, #194).

Un `SKILL.md` de una skill de la organización está escrito para Claude Code: llama a sus scripts con
`python3 "${CLAUDE_SKILL_DIR}/scripts/x.py" …` (variable que Antigravity no define, y los scripts no se copian a
propósito) y enlaza o cita a otras skills por su nombre (`../facturacion/SKILL.md`, `/gestor`), que en Antigravity
llevan el prefijo de la organización. Esto lo deja usable sin inventar nada:

- **Scripts.** `python3 "${CLAUDE_SKILL_DIR}/scripts/x.py" args…` pasa a
  `python3 "<baas.py>" run-script --tool antigravity --synced-org <org> skills/<skill>/scripts/x.py -- args…`: el
  ejecutor del plugin descarga el script, comprueba su SHA-256, lo corre en una carpeta temporal privada, sin la
  clave en su entorno, y lo borra siempre. Los argumentos originales se dejan **tal cual, como texto**: no se
  reinterpretan.
- **Enlaces y citas.** `[…](../facturacion/SKILL.md)` → `…/<org>-facturacion/…` y `` `/gestor` `` →
  `` `/<org>-gestor` ``, solo cuando coinciden exactamente con una skill sincronizada.

Todo es conservador: se reescribe solo lo que se reconoce con certeza y el resto se deja intacto (y `RewriteReport`
lo cuenta, para que `sync` lo avise). Nada se reescribe dentro de bloques de código salvo las llamadas a scripts, y
nunca en prosa suelta. Cada pieza que entra en una orden se valida antes (nombres con la misma expresión que las
carpetas, `scripts/…` con una lista corta de caracteres y sin `.`/`..`, y que el fichero exista en el paquete): un
`SKILL.md` remoto no puede inyectar nada en la orden generada. Es una función pura y su resultado depende solo del
texto y del contexto, así que el `sync` es idempotente.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_PATH_FORBIDDEN = frozenset("\"$`%!^&|<>;*?'")  # el shell los interpretaría aun entre comillas dobles
_KIND_BY_EXTENSION = {".py": "python", ".sh": "bash", ".bash": "bash", ".js": "node", ".mjs": "node", ".cjs": "node"}
_KIND_BY_INTERPRETER = {"python3": "python", "python": "python", "bash": "bash", "sh": "bash", "node": "node"}

_VAR = r"\$(?:\{CLAUDE_SKILL_DIR\}|CLAUDE_SKILL_DIR(?![A-Za-z0-9_]))"
_REL = r"scripts/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*"
_END = r"(?=[ \t\r]|$|[;&|)>`<])"
_CALL = re.compile(
    r"(?P<pre>^[ \t>]*(?:\$[ \t]+)?|(?:&&|\|\||;|\|)[ \t]*)"
    r"(?:(?P<interp>python3|python|bash|sh|node)[ \t]+)?"
    rf'(?:"{_VAR}/(?P<relq>{_REL})"|{_VAR}/(?P<relu>{_REL})){_END}'
)
_RELATIVE_CALL = re.compile(
    r"(?:^[ \t>]*(?:\$[ \t]+)?|(?:&&|\|\||;|\|)[ \t]*)(?:python3?|bash|sh|node)[ \t]+\"?(?:\./)?scripts/"
)
_VAR_ANY = re.compile(_VAR)
_FENCE_OPEN = re.compile(r"^\s*(?:>\s*)*(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_CODE_SPAN = re.compile(r"(?<!`)(?P<ticks>`+)(?!`)(?P<body>.+?)(?<!`)(?P=ticks)(?!`)")
_CITATION = re.compile(r"^/(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)$")
_LINK = re.compile(
    r"\[(?P<text>(?:[^\[\]\\]|\\.)*)\]\((?P<target>[^()\s]+)(?P<title>[ \t]+(?:\"[^\"]*\"|'[^']*'))?[ \t]*\)"
)
_DEFINITION = re.compile(r"^(?P<head>\s{0,3}\[[^\]]+\]:[ \t]*<?)(?P<target>[^\s>]+)")
_SKILL_LINK = re.compile(r"^\.\./(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)/(?P<rest>.*)$")


@dataclass(frozen=True)
class RunnerCommand:
    """Cómo se invoca el ejecutor del plugin (solo piezas ya validadas: ver `safe_runner`)."""

    python: str
    baas_py: str
    tool: str

    def render(self, org: str, skill: str, rel: str) -> str:
        return (
            f'{self.python} "{self.baas_py}" run-script --tool {self.tool} --synced-org {org} skills/{skill}/{rel} --'
        )


def safe_runner(python: str, baas_py: str, tool: str, *, windows: bool | None = None) -> RunnerCommand | None:
    """`RunnerCommand` si la ruta de `baas.py` se puede escribir entre comillas dobles sin que ningún shell la
    reinterprete (sin `"`, `$`, `` ` ``, `%`, `!`, `\\` en POSIX, saltos de línea…); `None` si no."""
    windows = os.name == "nt" if windows is None else windows
    if python not in ("python3", "python") or not NAME_RE.match(tool):
        return None
    if any(ord(c) < 32 or ord(c) == 127 or c in _SAFE_PATH_FORBIDDEN for c in baas_py):
        return None
    if not windows and "\\" in baas_py:
        return None
    if not (PurePosixPath(baas_py).is_absolute() or PureWindowsPath(baas_py).is_absolute()):
        return None
    return RunnerCommand(python, baas_py, tool)


@dataclass(frozen=True)
class RewriteContext:
    org: str  # slug de la organización
    skill: str  # nombre original de la skill (el del MCP, no el prefijado)
    synced: frozenset[str]  # skills sincronizadas de la organización (nombres originales)
    scripts: frozenset[str]  # rutas, relativas a la skill, de los ficheros del paquete
    runner: RunnerCommand | None  # `None` si no hay una ruta de `baas.py` segura: no se reescribe ningún script
    executable: frozenset[str] = frozenset()  # los que el paquete declara ejecutables


@dataclass
class RewriteReport:
    scripts: list[str] = field(default_factory=list)  # una entrada por llamada reescrita
    links: int = 0
    citations: int = 0
    unresolved: int = 0  # `${CLAUDE_SKILL_DIR}` que quedan en el texto
    relative_scripts: int = 0  # llamadas a `scripts/x` con ruta relativa (no se sabe a qué carpeta se refieren)
    runner_candidates: int = 0  # lo que parece una llamada a un script de la skill, se reescriba o no
    refs: list[str] = field(default_factory=list)  # nombres citados o enlazados (sincronizados o no)


class _Rewriter:
    def __init__(self, ctx: RewriteContext) -> None:
        for what, value in (("organización", ctx.org), ("skill", ctx.skill)):
            if not NAME_RE.match(value):
                raise ValueError(f"Nombre de {what} no válido para una orden: {value!r}")
        self.ctx = ctx
        self.report = RewriteReport()
        self._refs: set[str] = set()

    # --- scripts -----------------------------------------------------------------------------------

    def _allowed(self, rel: str, interpreter: str | None, in_span: bool, has_args: bool) -> bool:
        ctx = self.ctx
        if ctx.runner is None or rel not in ctx.scripts:
            return False
        if any(part in (".", "..") for part in rel.split("/")):
            return False
        kind = _KIND_BY_EXTENSION.get(PurePosixPath(rel).suffix.lower())
        if interpreter is not None:
            return kind is not None and kind == _KIND_BY_INTERPRETER[interpreter]
        if in_span and not has_args:  # `…/scripts/x.py` a solas en un fragmento de código es una mención, no una orden
            return False
        return kind is not None or rel in ctx.executable

    def command(self, code: str, *, in_span: bool) -> str:
        """Reescribe las llamadas a scripts de la propia skill en un texto de código."""
        ctx = self.ctx

        def sub(m: re.Match[str]) -> str:
            rel = m.group("relq") or m.group("relu")
            self.report.runner_candidates += 1
            rest = code[m.end() :]
            if not self._allowed(rel, m.group("interp"), in_span, bool(rest.strip())):
                return m.group(0)
            assert ctx.runner is not None
            self.report.scripts.append(rel)
            gap = " " if rest and rest[0] not in " \t\r" else ""
            return m.group("pre") + ctx.runner.render(ctx.org, ctx.skill, rel) + gap

        out = _CALL.sub(sub, code)
        self.report.relative_scripts += len(_RELATIVE_CALL.findall(out))
        return out

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
            rewritten = self.command(body, in_span=True)
            if rewritten != body:
                edits.append((body_start, body_end, rewritten))
                continue
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
        return line

    # --- documento ---------------------------------------------------------------------------------

    def run(self, text: str) -> str:
        out: list[str] = []
        fence: tuple[str, int] | None = None  # (carácter, longitud) del bloque abierto
        for line in text.split("\n"):
            if fence is not None:
                closing = line.strip().lstrip(">").strip()
                if closing and set(closing) == {fence[0]} and len(closing) >= fence[1]:
                    fence = None
                    out.append(line)
                else:
                    out.append(self.command(line, in_span=False))
                continue
            opened = _FENCE_OPEN.match(line)
            if opened and not (opened.group("fence")[0] == "`" and "`" in opened.group("info")):
                fence = (opened.group("fence")[0], len(opened.group("fence")))
                out.append(line)
                continue
            out.append(self.prose_line(line))
        result = "\n".join(out)
        self.report.unresolved = len(_VAR_ANY.findall(result))
        self.report.refs = sorted(self._refs)
        return result


def rewrite(text: str, ctx: RewriteContext) -> tuple[str, RewriteReport]:
    """`(texto reescrito, informe)`. Lanza `ValueError` si un nombre no es seguro para entrar en una orden."""
    rewriter = _Rewriter(ctx)
    return rewriter.run(text), rewriter.report
