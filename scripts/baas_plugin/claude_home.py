"""Directorio de usuario de Claude Code (`~/.claude`): dónde vive el plugin de cada organización.

La raíz se resuelve, por orden, con `--claude-home`, `BAAS_CLAUDE_HOME`, `CLAUDE_CONFIG_DIR` (la que usa el
propio Claude Code) y `~/.claude`. Las pruebas fijan `BAAS_CLAUDE_HOME`: nunca tocan el `~/.claude` real.

Lo único que el plugin escribe aquí (contrato §10):
- `skills/<slug>/`: el plugin de usuario de la organización (`sync`).
- `settings.json`: solo `enabledPlugins["<slug>@skills-dir"] = false` (el plugin queda apagado a nivel de
  usuario y se activa solo en los proyectos vinculados), conservando el resto del fichero.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .envfile import atomic_write
from .errors import ConfigError

ENV_VAR = "BAAS_CLAUDE_HOME"
MARKETPLACE = "skills-dir"  # los plugins de `~/.claude/skills/<nombre>/` salen como `<nombre>@skills-dir`


def plugin_key(slug: str) -> str:
    return f"{slug}@{MARKETPLACE}"


@dataclass(frozen=True)
class ClaudePaths:
    home: Path  # ~/.claude
    skills_root: Path  # <home>/skills

    @classmethod
    def resolve(cls, claude_home: str | None = None, skills_root: str | None = None) -> ClaudePaths:
        base = claude_home or os.environ.get(ENV_VAR) or os.environ.get("CLAUDE_CONFIG_DIR")
        home = Path(base).expanduser() if base else Path(os.path.expanduser("~")) / ".claude"
        root = Path(skills_root).expanduser() if skills_root else home / "skills"
        return cls(home=home, skills_root=root)

    @property
    def settings(self) -> Path:
        return self.home / "settings.json"

    def plugin_dir(self, slug: str) -> Path:
        return self.skills_root / slug


def read_json_object(path: Path) -> dict[str, Any] | None:
    """El objeto JSON de `path`, o `None` si no existe. Un fichero ilegible o que no es un objeto, es un error."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"No se pudo leer {path}: {exc}.") from exc
    try:
        data = json.loads(text or "{}")
    except ValueError as exc:
        raise ConfigError(
            f"{path} no es JSON válido; no se modifica.", hint="Corrígelo y vuelve a sincronizar."
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} no contiene un objeto JSON; no se modifica.")
    return data


def write_json_atomic(path: Path, data: dict[str, Any], default_mode: int = 0o600) -> None:
    """Escribe `data` de forma atómica. Si `path` es un enlace, se escribe su destino; se conserva el modo."""
    real = Path(os.path.realpath(path))
    try:
        mode = stat.S_IMODE(real.stat().st_mode)
    except FileNotFoundError:
        mode = default_mode
    real.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(real, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode)


def disable_user_plugin(paths: ClaudePaths, slug: str) -> str | None:
    """Deja `enabledPlugins["<slug>@skills-dir"] = false` en `settings.json`.

    Devuelve `None` si ya estaba así, `"created"` si no figuraba y `"was-enabled"` si estaba activado a nivel de
    usuario (el plugin de una organización solo debe verse en los proyectos vinculados a ella).
    Lanza `ConfigError` si el fichero no se puede leer o editar con seguridad.
    """
    key = plugin_key(slug)
    data = read_json_object(paths.settings) or {}
    plugins = data.get("enabledPlugins")
    if plugins is None:
        plugins = {}
    if not isinstance(plugins, dict):
        raise ConfigError(f"`enabledPlugins` de {paths.settings} no es un objeto JSON; no se modifica.")
    current = plugins.get(key)
    if current is False:
        return None
    plugins[key] = False
    data["enabledPlugins"] = plugins
    write_json_atomic(paths.settings, data)
    return "was-enabled" if current is True else "created"
