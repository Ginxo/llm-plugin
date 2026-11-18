"""Análisis de ficheros Markdown de una skill: enlaces, referencias a scripts y conocimiento, binarios."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .scan_sh import SPLIT_COMMANDS, classify_binary, command_words

LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
BARE_SCRIPT = re.compile(r"(?<![\w./~$-])(scripts/[A-Za-z0-9_][A-Za-z0-9_./-]*[A-Za-z0-9_])")
BARE_KNOWLEDGE = re.compile(r"(?<![\w./~$-])(knowledge/[A-Za-z0-9_./<>{}%*-]*[A-Za-z0-9_>}%*])")
SLASH_MENTION = re.compile(r"(?<![\w/.~:$-])/([a-z][a-z0-9_-]{1,60})(?![\w/-])")
_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|#|//)")


@dataclass
class MdResult:
    links: list[dict[str, Any]] = field(default_factory=list)
    refs: list[dict[str, Any]] = field(default_factory=list)
    mentions: list[dict[str, Any]] = field(default_factory=list)
    binaries: list[tuple[str, int]] = field(default_factory=list)
    interpreters: list[tuple[str, int]] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    fenced: list[bool] = field(default_factory=list)


def _in_inline_code(line: str, pos: int) -> bool:
    return line[:pos].count("`") % 2 == 1


def scan_markdown(text: str) -> MdResult:
    result = MdResult()
    result.lines = text.splitlines()
    in_fence = False
    for lineno, line in enumerate(result.lines, start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            result.fenced.append(True)
            continue
        result.fenced.append(in_fence)
        if in_fence:
            _scan_fenced(line, lineno, result)
            _scan_refs(line, lineno, result, in_code=True)
            continue
        masked = line
        for m in LINK_RE.finditer(line):
            target = m.group(2)
            if not _EXTERNAL.match(target):
                result.links.append(
                    {
                        "text": m.group(1),
                        "target": target.split("#", 1)[0].split("?", 1)[0],
                        "line": lineno,
                        "raw": m.group(0),
                    }
                )
            masked = masked.replace(m.group(0), " " * len(m.group(0)), 1)
        _scan_refs(masked, lineno, result, in_code=False)
        for m in SLASH_MENTION.finditer(masked):
            result.mentions.append({"name": m.group(1), "line": lineno})
        for m in _INLINE_CODE.finditer(masked):
            span = m.group(1).strip()
            first = span.split()[0] if span.split() else ""
            base = first.rsplit("/", 1)[-1]
            if "/" in first and not first.startswith(("/", "~", "./")):
                continue
            cls = classify_binary(base)
            if cls and cls[0] == "binary":
                result.binaries.append((cls[1], lineno))
            elif cls and " " in span:
                result.interpreters.append((cls[1], lineno))
    return result


def _scan_fenced(line: str, lineno: int, result: MdResult) -> None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return
    stripped = stripped.removeprefix("$ ").strip()
    for segment in SPLIT_COMMANDS.split(stripped):
        word = command_words(segment)
        cls = classify_binary(word) if word else None
        if cls and cls[0] == "interpreter":
            result.interpreters.append((cls[1], lineno))
        elif cls:
            result.binaries.append((cls[1], lineno))


def _scan_refs(line: str, lineno: int, result: MdResult, *, in_code: bool) -> None:
    for kind, pattern in (("scripts", BARE_SCRIPT), ("knowledge", BARE_KNOWLEDGE)):
        for m in pattern.finditer(line):
            code = in_code or _in_inline_code(line, m.start())
            result.refs.append(
                {
                    "kind": kind,
                    "target": m.group(1),
                    "line": lineno,
                    "in_code": code,
                    "fenced": in_code,
                    "start": m.start(),
                }
            )
