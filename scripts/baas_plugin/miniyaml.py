"""YAML mínimo (solo biblioteca estándar) para `baas.yaml` y las cabeceras de `SKILL.md`.

Cubre el subconjunto que usan los manifiestos y las cabeceras de skills: mapas y listas por
sangría, listas y mapas en línea (`[a, b]`, `{a: 1}`), escalares (cadenas planas o entrecomilladas,
números, booleanos, `null`), bloques `|` y `>` (con `-`/`+`) y comentarios. No cubre anclas, etiquetas,
claves complejas ni varios documentos. Las fechas se dejan como texto (el MCP también las serializa
como texto). Un escalar plano con `: ` dentro (`description: Use when: x`) es un error, como en YAML.
"""

from __future__ import annotations

import json
import re
from typing import Any


class YamlError(ValueError):
    """YAML no válido (o fuera del subconjunto admitido)."""


_INT = re.compile(r"^[-+]?[0-9]+$")
_FLOAT = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][-+]?[0-9]+)?$")
_RESERVED = {"null", "~", "true", "false", "yes", "no", "on", "off", "y", "n"}


def _strip_comment(text: str) -> str:
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote and not (quote == '"' and text[i - 1] == "\\"):
                quote = ""
        elif ch in "'\"" and (i == 0 or text[i - 1] in " \t[{,:"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def _split_key(content: str) -> tuple[str, str] | None:
    """Parte `clave: resto` por los primeros `:` seguidos de espacio o fin, fuera de comillas."""
    if content[:1] in "'\"":
        quote = content[0]
        i = 1
        while i < len(content):
            if content[i] == "\\" and quote == '"':
                i += 2
                continue
            if content[i] == quote:
                if quote == "'" and content[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                break
            i += 1
        else:
            return None
        rest = content[i + 1 :]
        if rest.startswith(":") and (len(rest) == 1 or rest[1] in " \t"):
            return _scalar(content[: i + 1]), rest[1:].strip()  # type: ignore[return-value]
        return None
    if content[:1] in "[{":
        return None
    for i, ch in enumerate(content):
        if ch == ":" and (i + 1 == len(content) or content[i + 1] in " \t"):
            return content[:i].strip(), content[i + 1 :].strip()
    return None


def _scalar(text: str) -> Any:
    s = text.strip()
    if s == "" or s in ("~", "null", "Null", "NULL"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if s[0] == '"':
        if len(s) < 2 or not s.endswith('"'):
            raise YamlError(f"Cadena entre comillas sin cerrar: {s[:40]}")
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if s[0] == "'":
        if len(s) < 2 or not s.endswith("'"):
            raise YamlError(f"Cadena entre comillas sin cerrar: {s[:40]}")
        return s[1:-1].replace("''", "'")
    if s[0] in "[{":
        return _flow(s)
    if _INT.match(s):
        return int(s)
    if _FLOAT.match(s):
        return float(s)
    if ": " in s or s.endswith(":"):
        raise YamlError(f"Escalar no válido (`: ` sin comillas): {s[:60]}")
    return s


def _split_flow(body: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote = ""
    cur: list[str] = []
    for i, ch in enumerate(body):
        if quote:
            cur.append(ch)
            if ch == quote and not (quote == '"' and body[i - 1] == "\\"):
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if quote or depth:
        raise YamlError(f"Lista o mapa en línea sin cerrar: {body[:40]}")
    tail = "".join(cur)
    if tail.strip() or parts:
        parts.append(tail)
    return parts


def _flow(s: str) -> Any:
    if s[0] == "[":
        if not s.endswith("]"):
            raise YamlError(f"Lista en línea sin cerrar: {s[:40]}")
        return [_scalar(p) for p in _split_flow(s[1:-1]) if p.strip()]
    if not s.endswith("}"):
        raise YamlError(f"Mapa en línea sin cerrar: {s[:40]}")
    out: dict[str, Any] = {}
    for part in _split_flow(s[1:-1]):
        if not part.strip():
            continue
        pair = _split_key(part.strip())
        if pair is None:
            raise YamlError(f"Mapa en línea no válido: {part.strip()[:40]}")
        out[str(pair[0])] = _scalar(pair[1])
    return out


def _balanced(s: str) -> bool:
    """¿Se han cerrado todos los corchetes y llaves (fuera de comillas)?"""
    depth = 0
    quote = ""
    for i, ch in enumerate(s):
        if quote:
            if ch == quote and not (quote == '"' and s[i - 1] == "\\"):
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
    return depth <= 0 and not quote


class _Parser:
    def __init__(self, text: str) -> None:
        if "\t" in "".join(ln[: len(ln) - len(ln.lstrip(" \t"))] for ln in text.splitlines()):
            raise YamlError("Las tabulaciones no se admiten como sangría en YAML.")
        self.lines = text.splitlines()
        self.i = 0

    # --- líneas ---------------------------------------------------------------------------------
    def _sig(self) -> tuple[int, str] | None:
        """Siguiente línea con contenido: (sangría, texto sin comentario). No la consume."""
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            content = _strip_comment(raw.lstrip(" "))
            if content in ("", "---") or content == "...":
                self.i += 1
                continue
            return len(raw) - len(raw.lstrip(" ")), content
        return None

    def parse(self) -> Any:
        first = self._sig()
        if first is None:
            return None
        value = self._block(first[0])
        if self._sig() is not None:
            raise YamlError(f"Contenido inesperado en la línea {self.i + 1}.")
        return value

    def _block(self, indent: int) -> Any:
        sig = self._sig()
        assert sig is not None
        content = sig[1]
        if content == "-" or content.startswith("- "):
            return self._seq(indent)
        if _split_key(content) is not None:
            return self._map(indent)
        # escalar suelto (o en línea)
        self.i += 1
        return self._value(content, indent - 1)

    # --- valores --------------------------------------------------------------------------------
    def _value(self, rest: str, parent_indent: int) -> Any:
        """Valor tras `clave:` o `- `. Consume las líneas de continuación que correspondan."""
        if rest == "":
            sig = self._sig()
            if sig is None:
                return None
            indent, content = sig
            if indent > parent_indent:
                return self._block(indent)
            if indent == parent_indent and (content == "-" or content.startswith("- ")):
                return self._seq(indent)  # lista con la misma sangría que su clave
            return None
        if rest[0] in "|>":
            return self._block_scalar(rest, parent_indent)
        if rest[0] in "[{":
            text = rest
            while not _balanced(text):
                if self.i >= len(self.lines):
                    raise YamlError(f"Lista o mapa en línea sin cerrar: {rest[:40]}")
                text += " " + _strip_comment(self.lines[self.i].strip())
                self.i += 1
            return _flow(text)
        if rest[0] in "'\"":
            text = rest
            while not (len(text) >= 2 and text.endswith(rest[0]) and _closed(text)):
                if self.i >= len(self.lines):
                    raise YamlError(f"Cadena entre comillas sin cerrar: {rest[:40]}")
                text += " " + self.lines[self.i].strip()
                self.i += 1
            return _scalar(text)
        text = rest
        while self.i < len(self.lines):  # escalar plano en varias líneas
            raw = self.lines[self.i]
            stripped = _strip_comment(raw.strip())
            if stripped == "":
                if raw.strip() == "":
                    nxt = self.i + 1
                    while nxt < len(self.lines) and self.lines[nxt].strip() == "":
                        nxt += 1
                    follow = self.lines[nxt] if nxt < len(self.lines) else ""
                    if follow and len(follow) - len(follow.lstrip(" ")) > parent_indent:
                        text += "\n"
                        self.i = nxt
                        continue
                break
            if len(raw) - len(raw.lstrip(" ")) <= parent_indent:
                break
            text += (" " if not text.endswith("\n") else "") + stripped
            self.i += 1
        return _scalar(text)

    def _block_scalar(self, header: str, parent_indent: int) -> str:
        style, chomp = header[0], ""
        for ch in header[1:].strip():
            if ch in "+-":
                chomp = ch
            elif not ch.isdigit():
                raise YamlError(f"Indicador de bloque no válido: {header}")
        body: list[str] = []
        block_indent: int | None = None
        while self.i < len(self.lines):
            raw = self.lines[self.i]
            if raw.strip() == "":
                body.append("")
                self.i += 1
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            if ind <= parent_indent:
                break
            if block_indent is None:
                block_indent = ind
            if ind < block_indent:
                break
            body.append(raw[block_indent:])
            self.i += 1
        trailing = 0
        while body and body[-1] == "":
            body.pop()
            trailing += 1
        if style == "|":
            text = "\n".join(body)
        else:
            text = ""
            for idx, line in enumerate(body):
                if idx == 0:
                    text = line
                elif line == "":
                    text += "\n"
                elif body[idx - 1] == "":
                    text += line
                else:
                    text += " " + line
        if not body:
            return ""
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" * (trailing + 1)
        return text + "\n"

    # --- colecciones ----------------------------------------------------------------------------
    def _map(self, indent: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while True:
            sig = self._sig()
            if sig is None or sig[0] < indent:
                return out
            if sig[0] > indent:
                raise YamlError(f"Sangría inesperada en la línea {self.i + 1}.")
            pair = _split_key(sig[1])
            if pair is None:
                raise YamlError(f"Se esperaba `clave: valor` en la línea {self.i + 1}: {sig[1][:40]}")
            self.i += 1
            key, rest = pair
            out[str(key)] = self._value(rest, indent)

    def _seq(self, indent: int) -> list[Any]:
        out: list[Any] = []
        while True:
            sig = self._sig()
            if sig is None or sig[0] < indent:
                return out
            content = sig[1]
            if sig[0] > indent or not (content == "-" or content.startswith("- ")):
                if sig[0] == indent:
                    return out
                raise YamlError(f"Sangría inesperada en la línea {self.i + 1}.")
            rest = content[1:].lstrip(" ")
            if rest == "":
                self.i += 1
                out.append(self._value("", indent))
                continue
            offset = indent + (len(content) - len(rest))
            if _split_key(rest) is not None:
                # `- clave: valor`: el mapa continúa en las líneas siguientes con la misma sangría
                self.lines[self.i] = " " * offset + rest
                out.append(self._map(offset))
            else:
                self.i += 1
                out.append(self._value(rest, indent))


def _closed(text: str) -> bool:
    quote = text[0]
    i = 1
    while i < len(text):
        if text[i] == "\\" and quote == '"':
            i += 2
            continue
        if text[i] == quote:
            if quote == "'" and text[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i == len(text) - 1
        i += 1
    return False


def loads(text: str) -> Any:
    """Interpreta `text` (subconjunto de YAML). Lanza `YamlError` si no es válido."""
    return _Parser(text).parse()


# --- escritura -------------------------------------------------------------------------------------


def _plain_ok(s: str) -> bool:
    if s == "" or s != s.strip() or s.lower() in _RESERVED:
        return False
    if s[0] in "-?:,[]{}#&*!|>'\"%@`" or ": " in s or " #" in s or s.endswith(":") or "\n" in s:
        return False
    return not (_INT.match(s) or _FLOAT.match(s))


def _dump_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    return text if _plain_ok(text) else json.dumps(text, ensure_ascii=False)


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list, tuple))


def _dump(value: Any, indent: int, out: list[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            k = _dump_scalar(str(key))
            if isinstance(item, (list, tuple)) and all(_is_scalar(x) for x in item):
                out.append(f"{pad}{k}: [{', '.join(_dump_scalar(x) for x in item)}]")
            elif isinstance(item, dict):
                if item:
                    out.append(f"{pad}{k}:")
                    _dump(item, indent + 2, out)
                else:
                    out.append(f"{pad}{k}: {{}}")
            elif isinstance(item, (list, tuple)):
                out.append(f"{pad}{k}:")
                _dump(item, indent + 2, out)
            else:
                out.append(f"{pad}{k}: {_dump_scalar(item)}")
    else:  # lista con elementos compuestos
        for item in value:
            if isinstance(item, dict) and item:
                sub: list[str] = []
                _dump(item, indent + 2, sub)
                out.append(f"{pad}- {sub[0].lstrip()}")
                out.extend(sub[1:])
            elif isinstance(item, (list, tuple)) and all(_is_scalar(x) for x in item):
                out.append(f"{pad}- [{', '.join(_dump_scalar(x) for x in item)}]")
            else:
                out.append(f"{pad}- {_dump_scalar(item) if _is_scalar(item) else '{}'}")


def dumps(data: Any) -> str:
    """Serializa un mapa (o lista) en YAML legible y estable: respeta el orden de las claves."""
    out: list[str] = []
    _dump(data, 0, out)
    return "\n".join(out) + "\n"
