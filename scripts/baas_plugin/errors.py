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

    @property
    def error_code(self) -> str:
        """Código simbólico estable (el de `BAAS_ERROR=<código>`): la marca sin el prefijo `BAAS_`."""
        return self.marker.removeprefix("BAAS_")


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


class PlainNetworkError(NetworkError):
    """El servidor respondió, pero con un límite de intentos o un fallo propio: ya trae el texto en llano
    (no se sustituye por el genérico de «sin conexión»)."""


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

    @property
    def error_code(self) -> str:
        return self.code


class PackageError(BaasError):
    """Paquete de skill inválido: ruta insegura (`..`, absoluta), manifiesto o respuesta mal formados."""

    exit_code = 11
    marker = "BAAS_PACKAGE_INVALID"


class SyncIncompleteError(BaasError):
    """La sincronización terminó, pero alguna skill no se pudo instalar (el resto sí se aplicó)."""

    exit_code = 12
    marker = "BAAS_SYNC_INCOMPLETE"


class ForeignDirectoryError(BaasError):
    """El directorio del plugin de la organización existe y no lo gestiona BaaS: no se toca nada."""

    exit_code = 13
    marker = "BAAS_SYNC_FOREIGN_DIR"


class _ApprovalError(BaasError):
    """Aprobación en el navegador (flujo de dispositivo): la skill traduce cada una a un mensaje llano."""

    default_message = ""
    default_hint: str | None = None

    def __init__(self, message: str | None = None, *, hint: str | None = None) -> None:
        super().__init__(message or self.default_message, hint=hint or self.default_hint)


class ApprovalPendingError(_ApprovalError):
    """Hay un enlace que la persona debe abrir para aprobar la conexión."""

    exit_code = 20
    marker = "BAAS_APPROVAL_PENDING"
    default_message = "Falta que apruebes la conexión de este proyecto en tu navegador."

    def __init__(
        self,
        message: str | None = None,
        *,
        hint: str | None = None,
        verification_url: str | None = None,
        user_code: str | None = None,
    ) -> None:
        self.verification_url = verification_url
        self.user_code = user_code
        if message is None and verification_url:
            message = f"Abre este enlace para aprobar la conexión de este proyecto: {verification_url}"
            if user_code:
                message += f" (comprueba que el código que ves es {user_code})."
        super().__init__(message, hint=hint)


class ApprovalWaitingError(_ApprovalError):
    """Se sigue esperando la aprobación: hay que volver a comprobarlo."""

    exit_code = 21
    marker = "BAAS_APPROVAL_WAITING"
    default_message = "Todavía no has aprobado la conexión en el navegador."
    default_hint = "Cuando termines de aprobarla, dímelo y lo compruebo de nuevo."


class ApprovalDeniedError(_ApprovalError):
    """La persona canceló la conexión en el navegador."""

    exit_code = 22
    marker = "BAAS_APPROVAL_DENIED"
    default_message = "Has cancelado la conexión de este proyecto, así que no se ha cambiado nada."
    default_hint = "Si fue sin querer, vuelve a empezar y aprueba la conexión en el navegador."


class ApprovalExpiredError(_ApprovalError):
    """El código de aprobación caducó o ya se usó."""

    exit_code = 23
    marker = "BAAS_APPROVAL_EXPIRED"
    default_message = "El enlace de aprobación ha caducado."
    default_hint = "Vuelve a empezar para recibir uno nuevo."


def _tool_hint(code: str) -> str | None:
    if code in ("AUTHORIZATION_DENIED", "KILL_SWITCH_ACTIVE"):
        return "Detente e informa al usuario; no intentes rodear la restricción."
    return None
