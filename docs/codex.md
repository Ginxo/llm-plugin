# Codex CLI con BaaS-AI-IP

> **Estado**: configuración **probada con Codex CLI 0.169.0** (`codex exec`, 2026-11-17) contra el proxy de desarrollo
> con un proveedor de OpenAI falso: el spike S13 ([spike-s13.md](spike-s13.md)). Tarea L2 del hito H4. Lo que **no** se
> ha probado está al final.

Codex no necesita ningún plugin de BaaS-AI-IP (el plugin `baas` es de Claude Code): solo se le dice que hable con el
**proxy de OpenAI de tu organización** y se le da, en una cabecera, tu API key de BaaS-AI-IP. El proxy identifica al
usuario con esa cabecera, aplica el kill switch, el bloqueo y la pausa de la organización y, si tiene **AI-IP** activo,
sustituye los datos sensibles antes de que salgan y los restaura al volver. La conversación aparece en la webapp (W8)
como `OPENAI · RESPONSES` con el cliente `codex_exec/<versión>`. La credencial del proveedor (`OPENAI_API_KEY`) sigue
siendo tuya: el proxy la reenvía y no la guarda. El servidor MCP de la organización (skills y conocimiento) no se
configura aquí.

## 1. Qué necesitas

- **Codex CLI** (probado: 0.169.0).
- Tu **API key personal de BaaS-AI-IP** guardada en `~/baas-ai-ip/.env` como `BAAS_API_KEY=…` (la guarda
  `python3 "<ruta del plugin>/scripts/baas.py" set-key`, que la pide sin mostrarla; ver el [README](../README.md)).
- La **URL del proxy de OpenAI de tu organización**: la devuelve el *handshake* de la webapp. Si ya lanzaste
  `/baas-init` en algún proyecto, está en `<proyecto>/.claude/baas.local.json` (campo `proxy_openai_url`); tiene la
  forma `https://proxy.<dominio>/org/<organización>/openai/v1`.
- Tu **credencial de OpenAI** en `OPENAI_API_KEY` (la usa Codex con el proveedor; el proxy solo la reenvía).

## 2. El perfil de Codex (en tu configuración de **usuario**)

Crea `~/.codex/baas.config.toml` (si usas `CODEX_HOME`, en `$CODEX_HOME/baas.config.toml`):

```toml
# ~/.codex/baas.config.toml  →  codex -p baas
model = "MODELO"                       # el que uses con tu cuenta de OpenAI
model_provider = "baas"

[model_providers.baas]
name = "BaaS-AI-IP"
base_url = "https://proxy.example.com/org/mi-organizacion/openai/v1"   # el proxy_openai_url del handshake
env_key = "OPENAI_API_KEY"             # credencial del PROVEEDOR: el proxy la reenvía
wire_api = "responses"
env_http_headers = { "X-Baas-Api-Key" = "BAAS_API_KEY" }   # la clave de BaaS-AI-IP sale de esta variable de entorno
```

Por qué así (todo comprobado en el spike S13):

| Decisión | Motivo |
| :--- | :--- |
| `wire_api = "responses"` | Es la única API que Codex 0.169 admite: con `chat` no arranca («`wire_api = "chat"` is no longer supported»). El proxy trata `POST …/responses` con y sin *streaming* |
| `env_http_headers`, **no** `http_headers` | Con `http_headers` la clave quedaría en claro en `config.toml`. Con `env_http_headers` solo está en el entorno de Codex. **No uses `http_headers`** |
| Configuración de **usuario** (o perfil), no de proyecto | Codex descarta `model_provider` y `model_providers` en `<proyecto>/.codex/config.toml` (avisa «Ignored unsupported project-local config keys») y usa OpenAI. Tampoco sirve un proyecto sin confianza. Es lo que impide que un repositorio ajeno redirija tu tráfico o capture la clave |
| Un **perfil** (`-p baas`) | No cambia tu proveedor por defecto: Codex habla con OpenAI directamente salvo cuando lo lanzas con `-p baas` |
| Un perfil por organización | La URL lleva el *slug* de la organización: si perteneces a varias, copia el perfil como `baas-<organización>.config.toml` con su `base_url` y lanza `codex -p baas-<organización>` |

## 3. Lanzar Codex con la clave solo en su entorno

La clave debe estar en el entorno **del proceso de Codex**, no en el de tu *shell*. Un lanzador mínimo
(`~/bin/baas-codex`, con permisos de ejecución) que lee solo esa línea de `~/baas-ai-ip/.env`:

```bash
#!/usr/bin/env bash
# baas-codex: Codex con el perfil «baas». La clave de BaaS-AI-IP vive solo en el entorno de ese proceso.
set -euo pipefail
key="$(sed -n 's/^BAAS_API_KEY=//p' "$HOME/baas-ai-ip/.env" | tail -n 1)"
[[ -n "$key" ]] || { echo "falta BAAS_API_KEY en ~/baas-ai-ip/.env (baas.py set-key)" >&2; exit 1; }
BAAS_API_KEY="$key" exec codex -p baas "$@"
```

Uso: `baas-codex` (interactivo) o `baas-codex exec "Responde solo: hola"`. No pongas `BAAS_API_KEY` en el perfil de tu
*shell* (`.bashrc`, `.zshrc`): la verían todos tus procesos.

## 4. Comprobar que pasa por el proxy

1. `baas-codex exec "Responde solo: hola"` responde.
2. En la webapp (W8, conversaciones) aparece una conversación de **`OPENAI`** · **`RESPONSES`** con el modelo y el cliente
   `codex_exec/…`, con sus turnos y *tokens*. Si la organización tiene AI-IP activo, el turno lleva su verificación.
3. Sin la cabecera (por ejemplo, sin `BAAS_API_KEY` en el entorno) el proxy responde `401` y Codex reintenta cinco veces
   («Reconnecting… 5/5») antes de salir con `1`: es la señal de que la clave no llega.

## 5. Con AI-IP

- **AI-IP activo y anonimizador encendido** (lo normal): OpenAI recibe `[[TIPO_NNN]]` en lugar del DNI, el email, el IBAN…
  que reconocen las reglas, y Codex recibe el texto restaurado. Los límites de AI-IP (nombres propios, direcciones,
  razonamiento, etc.) son los de la webapp (W7, banda de cobertura).
- **Anonimizador apagado** (modo observación, decisión explícita de un administrador de la organización): AI-IP detecta y
  mide, pero **no sustituye nada**; lo que escribas en Codex llega **en claro** a OpenAI y W8 marca la conversación con
  «AI-IP: observación» y los datos detectados como aviso.
- Con AI-IP activo, el proxy solo admite las rutas de los turnos y las lecturas sin contenido; cualquier otra ruta
  recibe `400` con el código `baas_route_not_supported`.

## 6. Riesgos de la clave

| Riesgo | Detalle y qué hacer |
| :--- | :--- |
| Clave en el entorno | `env_http_headers` exige `BAAS_API_KEY` en el entorno de Codex: usa el lanzador de arriba, no un `export` global |
| *Snapshots* de shell | Codex guarda su entorno en `$CODEX_HOME/shell_snapshots/<sesión>.sh` (`declare -x BAAS_API_KEY="baas_…"`) con permisos `0644`. Se borra al terminar bien la sesión, pero **queda** si Codex muere o falla. Revisa y borra `~/.codex/shell_snapshots/` tras un fallo, y `chmod 700 ~/.codex` |
| Clave literal | No uses `http_headers` con la clave: queda en claro en `config.toml` |
| Ficheros de sesión | En una ejecución normal la clave no aparece en `CODEX_HOME` (estado, registros, historial) ni en `HOME`: comprobado |
| Fuga a otro proveedor | Las cabeceras pertenecen al proveedor `baas` y, por el diseño de Codex, solo deberían viajar a su `base_url`; con el proveedor por defecto (OpenAI) no deberían enviarse, pero no se observó ninguna petición real a OpenAI (ver el final) |
| Clave expuesta | Revócala en la webapp (*Mis API keys*), crea otra y guárdala de nuevo con `set-key` |

## 7. Solución de problemas

| Síntoma | Causa y solución |
| :--- | :--- |
| «`wire_api = "chat"` is no longer supported» | El perfil usa Chat Completions: pon `wire_api = "responses"` |
| `401` y «Reconnecting… 5/5» | La clave no llega (falta `BAAS_API_KEY` en el entorno de Codex, o `env_http_headers` apunta a otro nombre) o está revocada. Prueba `baas-codex` y revisa `~/baas-ai-ip/.env` |
| Codex responde pero no aparece nada en W8 | No está usando el perfil (falta `-p baas`) o el perfil está en un `.codex/` de proyecto, que se ignora. Usa `~/.codex/baas.config.toml` |
| `403` «Acceso bloqueado por BaaS-AI-IP» | La plataforma está detenida o tu organización está bloqueada o pausada, o no eres miembro de la organización de la URL: habla con tu administrador |
| `400` `baas_route_not_supported` | Con AI-IP activo, Codex pidió una ruta que el proxy no admite |
| `403` `baas_conversation_deleted` | Un administrador eliminó esa conversación: empieza una nueva |
| Restos en `~/.codex/shell_snapshots/` | Ver los riesgos de la clave |

## 8. Lo que no se ha comprobado

**No se ha comprobado**: la TUI interactiva y la extensión de IDE de Codex (solo `codex exec`); que los comandos de
*shell* que lanza el agente no vean `BAAS_API_KEY` (la política de entorno de Codex debería excluir los nombres con
`KEY`, pero el modelo falso no pidió herramientas); que con el proveedor por defecto (OpenAI) Codex **no** envíe las cabeceras de `baas` (es lo esperable, pero el
spike no tuvo red); el transporte WebSocket de Responses con un proveedor propio (con
`baas` Codex usó HTTPS directamente); un OpenAI real (todo fue con un proveedor falso: el proxy y AI-IP se probaron con
el eco de `deploy`, no con las respuestas de un modelo real); AI-IP con el tráfico real de Codex (turnos largos,
herramientas, `previous_response_id`); macOS, Windows y otras versiones de Codex; **Cursor** (fuera de la Fase 1). La
prueba manual con Codex y Claude Code reales la hace el usuario en el tramo 2 del hito H4.
