"""Errores del plugin. Cada tipo tiene un código de salida estable que las skills interpretan."""

from __future__ import annotations


class BaasError(Exception):
    """Error con mensaje para el usuario (en español) y código de salida."""

    exit_code = 1
    marker = "BAAS_ERROR"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        text = f"[{self.marker}] {self.message}"
        if self.hint:
            text += f"\n{self.hint}"
        return text


class ConfigError(BaasError):
    exit_code = 2
    marker = "BAAS_CONFIG_ERROR"


class MissingApiKeyError(ConfigError):
    marker = "BAAS_KEY_MISSING"


class MissingAppUrlError(ConfigError):
    marker = "BAAS_APP_URL_MISSING"


class SelectionRequiredError(BaasError):
    exit_code = 3
    marker = "BAAS_ORG_SELECTION_REQUIRED"


class NoOrganizationsError(BaasError):
    exit_code = 4
    marker = "BAAS_NO_ORGANIZATIONS"


class AuthError(BaasError):
    exit_code = 5
    marker = "BAAS_AUTH_ERROR"


class NetworkError(BaasError):
    exit_code = 6
    marker = "BAAS_NETWORK_ERROR"


class ProjectSafetyError(BaasError):
    exit_code = 7
    marker = "BAAS_PROJECT_UNSAFE"


class OrgNotFoundError(BaasError):
    exit_code = 8
    marker = "BAAS_ORG_NOT_FOUND"


class ScriptIntegrityError(BaasError):
    exit_code = 9
    marker = "BAAS_SCRIPT_HASH_MISMATCH"


class McpToolError(BaasError):
    """Error de una tool del MCP (contrato §7.1): `code` es el código del contrato."""

    exit_code = 10
    marker = "BAAS_MCP_ERROR"

    def __init__(self, code: str, message: str, payload: dict | None = None) -> None:
        super().__init__(f"{code}: {message}", hint=_tool_hint(code))
        self.code = code
        self.payload = payload or {}
        self.marker = f"BAAS_MCP_{code}"


def _tool_hint(code: str) -> str | None:
    if code in ("AUTHORIZATION_DENIED", "KILL_SWITCH_ACTIVE"):
        return "Detente e informa al usuario; no intentes rodear la restricción."
    return None
