"""Detección de posibles datos sensibles en el texto de una skill (NIF/DNI/NIE, CIF, IBAN, correos, claves).

Solo detecta y enmascara: decidir qué hacer con ello es del ORG_ADMIN (diseño H5 §3 y §9). Las coincidencias de
DNI, NIE, CIF e IBAN se validan con su dígito de control para no avisar de números cualquiera.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"
_DNI = re.compile(r"(?<![A-Za-z0-9])(?:[XYZxyz]\d{7}|\d{8})[A-Za-z](?![A-Za-z0-9])")
_CIF = re.compile(r"(?<![A-Za-z0-9])[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J](?![A-Za-z0-9])")
_IBAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?(?![A-Za-z0-9])")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
_EMAIL_IGNORED_DOMAINS = ("example.com", "example.org", "example.net", "localhost", "users.noreply.github.com")
_EMAIL_IGNORED_LOCALS = ("git", "noreply", "no-reply", "user", "usuario", "nombre", "name", "tu", "your")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_TOKENS = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|baas_[A-Za-z0-9_-]{43}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r")(?![A-Za-z0-9_-])"
)
_BEARER = re.compile(r"Bearer\s+([A-Za-z0-9._~+/=-]{24,})")
_SECRET_ASSIGN = re.compile(
    r"""(?ix)
    (?:pass(?:word|wd)?|pwd|secret|api[_-]?key|token|auth[_-]?key|private[_-]?key)\w*["']?
    \s*[:=]\s*
    (?P<q>["'])(?P<value>[^"'\s]{6,})(?P=q)
    """
)
_DOTENV_ASSIGN = re.compile(
    r"^\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:PASS(?:WORD)?|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY)[A-Z0-9_]*=(?P<value>\S{6,})\s*$"
)
_PLACEHOLDER = re.compile(r"^(?:<.*>|\{.*\}|\$.*|%.*|x{3,}|\*{3,}|\.{3,}|change-?me|your.*|none|null|todo)$", re.I)

SENSITIVE_FILE_PATTERNS = (
    (".env", "fichero de entorno con credenciales"),
    ("*.pem", "certificado o clave privada"),
    ("*.key", "clave privada"),
    ("*.p12", "almacén de claves"),
    ("*.pfx", "almacén de claves"),
    ("*.jks", "almacén de claves Java"),
    ("*.keystore", "almacén de claves"),
    ("id_rsa", "clave SSH privada"),
    ("id_ed25519", "clave SSH privada"),
    ("id_ecdsa", "clave SSH privada"),
    ("*.kdbx", "base de contraseñas"),
    (".netrc", "credenciales de red"),
    (".npmrc", "configuración con posible token"),
    (".pypirc", "configuración con posible token"),
    ("credentials.json", "credenciales"),
    ("service-account*.json", "cuenta de servicio"),
    ("*.secret", "secreto"),
)
_ENV_SAFE_SUFFIXES = ("example", "sample", "template", "dist", "defaults", "default")


@dataclass(frozen=True)
class Match:
    kind: str
    line: int
    sample: str  # enmascarada: nunca el valor completo


def mask(value: str) -> str:
    """`12345678Z` → `12…8Z`: lo justo para localizarlo sin repetirlo en el informe."""
    v = value.strip()
    return "***" if len(v) <= 6 else f"{v[:2]}…{v[-2:]}"


def valid_dni(value: str) -> bool:
    v = value.upper()
    prefix = {"X": "0", "Y": "1", "Z": "2"}.get(v[0])
    digits = (prefix + v[1:-1]) if prefix else v[:-1]
    return digits.isdigit() and DNI_LETTERS[int(digits) % 23] == v[-1]


def valid_cif(value: str) -> bool:
    v = value.upper()
    digits, control = v[1:8], v[8]
    total = 0
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == 0:  # posiciones impares (1, 3, 5, 7): se duplica y se suman las cifras
            n *= 2
            total += n // 10 + n % 10
        else:
            total += n
    digit = (10 - total % 10) % 10
    letter = "JABCDEFGHI"[digit]
    if v[0] in "PQRSNW":
        return control == letter
    if v[0] in "ABEH":
        return control == str(digit)
    return control in (str(digit), letter)


def valid_iban(value: str) -> bool:
    iban = value.replace(" ", "").upper()
    if not 15 <= len(iban) <= 34 or not iban.isalnum():
        return False
    moved = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _iban_candidates(text: str) -> list[str]:
    found = []
    for m in _IBAN.finditer(text):
        tokens = m.group(0).split(" ")
        for k in range(len(tokens), 0, -1):  # la cola opcional puede arrastrar una palabra en mayúsculas
            if valid_iban("".join(tokens[:k])):
                found.append(" ".join(tokens[:k]))
                break
    return found


def scan_text(text: str) -> list[Match]:
    """Busca datos sensibles línea a línea. Devuelve coincidencias con la muestra enmascarada."""
    out: list[Match] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if len(line) > 20000:  # una línea enorme (minificado, base64) no aporta y es lenta
            line = line[:20000]
        for m in _DNI.finditer(line):
            if valid_dni(m.group(0)):
                out.append(Match("nif", lineno, mask(m.group(0))))
        for m in _CIF.finditer(line):
            if valid_cif(m.group(0)):
                out.append(Match("cif", lineno, mask(m.group(0))))
        for cand in _iban_candidates(line):
            out.append(Match("iban", lineno, mask(cand)))
        for m in _EMAIL.finditer(line):
            local, _, domain = m.group(0).rpartition("@")
            if domain.lower() in _EMAIL_IGNORED_DOMAINS or local.lower() in _EMAIL_IGNORED_LOCALS:
                continue
            out.append(Match("email", lineno, mask(m.group(0))))
        if _PRIVATE_KEY.search(line):
            out.append(Match("private_key", lineno, "-----BEGIN … PRIVATE KEY-----"))
        for m in _TOKENS.finditer(line):
            out.append(Match("token", lineno, mask(m.group(0))))
        for m in _BEARER.finditer(line):
            out.append(Match("token", lineno, mask(m.group(1))))
        for m in _SECRET_ASSIGN.finditer(line):
            value = m.group("value")
            if not _PLACEHOLDER.match(value):
                out.append(Match("password", lineno, mask(value)))
        dotenv = _DOTENV_ASSIGN.match(line)
        if dotenv and not _PLACEHOLDER.match(dotenv.group("value").strip("'\"")):
            out.append(Match("password", lineno, mask(dotenv.group("value").strip("'\""))))
    return _dedupe(out)


def _dedupe(matches: list[Match]) -> list[Match]:
    seen: set[tuple[str, int, str]] = set()
    unique = []
    for m in matches:
        key = (m.kind, m.line, m.sample)
        if key not in seen:
            seen.add(key)
            unique.append(m)
    return unique


def sensitive_file_reason(name: str) -> str | None:
    """Motivo si el NOMBRE del fichero suele contener credenciales (`.env`, claves, almacenes…)."""
    lower = name.lower()
    if lower == ".env" or (lower.startswith(".env.") and lower.rsplit(".", 1)[-1] not in _ENV_SAFE_SUFFIXES):
        return "fichero de entorno con credenciales"
    for pattern, reason in SENSITIVE_FILE_PATTERNS:
        if fnmatch.fnmatchcase(lower, pattern):
            return reason
    return None
