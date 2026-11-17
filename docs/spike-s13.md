# Spike S13 · Codex CLI a través del proxy (parte de Codex)

> **Estado**: cerrado para Codex CLI con evidencia local (2026-11-17, **Codex CLI 0.169.0**, `codex exec`).
> Cursor queda fuera (pendiente, contrato §3). **Riesgo** (fase-1-mvp §8, S13): clientes OpenAI que no permitan
> cabeceras propias, de modo que el proxy no pueda identificar al usuario. Tarea L2 del plan del hito H2.

## 1. Conclusiones

| Pregunta | Respuesta | Evidencia |
| :--- | :--- | :--- |
| ¿Llega `X-Baas-Api-Key` al proxy? | **Sí**, con `env_http_headers = { "X-Baas-Api-Key" = "BAAS_API_KEY" }` (la clave sale de la variable de entorno) y con `http_headers` (clave literal) dentro de `[model_providers.baas]`. El proxy responde `200`; sin la cabecera, `401 baas_invalid_api_key`. **Codex no necesita credencial de proyecto** | Casos 1, 2, 4, 7 y 8 |
| ¿Qué recibe el proveedor? | `POST /v1/responses` con `Authorization: Bearer <clave del proveedor>` intacta y **sin** ninguna `X-Baas-*` (el proxy las quita, contrato §3) | Registro de `dev/fake_openai.py` |
| ¿Qué API usa? | **Responses** (`POST {base_url}/responses`, `stream: true`, SSE). `wire_api = "chat"` ya **no se admite**: Codex no arranca («`wire_api = "chat"` is no longer supported») | Casos 1 y 3 |
| ¿Aparece la traza? | **Sí**: `GET /api/v1/conversations` muestra `provider OPENAI`, `api_flavor RESPONSES`, modelo `gpt-fake`, cliente `codex_exec/0.169.0`, con turnos y *tokens* | Final del script |
| ¿Dónde vive la configuración? | El proveedor (`model_provider` y `[model_providers.*]`) solo se admite en la configuración **de usuario**: `$CODEX_HOME/config.toml` (por defecto `~/.codex/config.toml`), un **perfil** `$CODEX_HOME/<nombre>.config.toml` (`-p <nombre>`) o `-c` en la línea de órdenes. La configuración de **proyecto** (`<proyecto>/.codex/config.toml`) se ignora entera si el proyecto no es de confianza y, aunque lo sea, Codex **descarta** `model_provider` y `model_providers` («Ignored unsupported project-local config keys … manually set them in your user-level config.toml») y usa OpenAI | Casos 5, 6 y comprobación aparte (§4) |
| ¿Fuga de la clave a OpenAI? | Menor que en Claude Code: las cabeceras pertenecen **al proveedor** `baas`, no a la sesión; con otro proveedor (el de OpenAI por defecto) no se usan. Pero la clave tiene que estar en el **entorno** de Codex (o en claro en `config.toml`) y Codex la vuelca a sus *snapshots* de shell (§3) | Casos 1, 4–6; §3 |

## 2. Configuración recomendada (si se integra en el plugin o se documenta)

En la configuración **de usuario** de Codex, con un perfil para no cambiar el proveedor por defecto:

```toml
# ~/.codex/baas.config.toml  →  codex -p baas
model = "<modelo>"
model_provider = "baas"

[model_providers.baas]
name = "BaaS-AI-IP"
base_url = "<proxy_openai_url del handshake>"      # p. ej. https://proxy.<dominio>/org/demo/openai/v1
env_key = "OPENAI_API_KEY"                           # credencial del PROVEEDOR (la reenvía el proxy)
wire_api = "responses"
env_http_headers = { "X-Baas-Api-Key" = "BAAS_API_KEY" }
```

Y `BAAS_API_KEY` solo en el entorno del proceso de Codex: un lanzador (`baas-codex`, a decidir) que lea
`~/baas-ai-ip/.env` y haga `exec codex -p baas "$@"`, en lugar de exportarla en el perfil del shell. **No** usar
`http_headers` con la clave literal.

## 3. Riesgos de la clave (en la línea del S1)

| Riesgo | Detalle | Comparación con Claude Code (S1) |
| :--- | :--- | :--- |
| Ámbito | No hay configuración de proveedor por proyecto: queda a nivel de usuario. Pero las cabeceras van atadas al proveedor `baas` y solo viajan a su `base_url` | En Claude Code `ANTHROPIC_CUSTOM_HEADERS` va a la Base URL que haya, por eso se fijó por proyecto |
| Clave en el entorno | `env_http_headers` exige `BAAS_API_KEY` en el entorno de Codex. Si se exporta en el perfil del shell, la ven todos los procesos del usuario | Igual que el bloque `env` de `settings.local.json`, que Claude Code exporta a sus subprocesos |
| *Snapshots* de shell | Codex guarda el entorno en `$CODEX_HOME/shell_snapshots/<sesión>.sh` (`declare -x BAAS_API_KEY="baas_…"`), con permisos `0644` (umask 022). Se borra al terminar bien la sesión, pero **queda** si Codex muere o falla (casos 4–6, cortados por `timeout`) | Sin equivalente |
| Clave literal | `http_headers` deja la clave en claro en `config.toml` (de usuario; los permisos son los de quien lo escriba) | Igual que `settings.local.json`, pero fuera del proyecto |
| Ficheros de sesión | En una ejecución normal (con y sin `--ephemeral`) la clave **no** aparece en `CODEX_HOME` (SQLite de estado, registros e historial) ni en `HOME` | — |
| Configuración de proyecto | No puede redirigir a otro proveedor ni capturar la clave: Codex ignora ahí `model_provider(s)` y la de un proyecto sin confianza entera | En Claude Code, `.claude/settings.json` de un repositorio sí puede fijar `ANTHROPIC_BASE_URL` |
| Sin cabecera o con clave revocada | `401` con el mensaje del proxy; Codex reintenta 5 veces («Reconnecting… 5/5») y sale con `1` | — |

## 4. Pruebas (reproducibles)

`dev/spike_s13_codex.sh` instala `@openai/codex` con `npm i` en un directorio temporal (o usa `CODEX_BIN`), con
`HOME` y `CODEX_HOME` temporales, y ejecuta `codex exec` en espacios de nombres de red y de montaje propios
(`unshare -rnm`): **nada sale a Internet** y solo llega al puerto del proxy por `dev/netns_bridge.py` (TCP ↔ socket
Unix). Se monta un `/etc/hosts` propio porque Codex es un binario estático (musl) que no usa NSS y resolvería
`*.localhost` por DNS. Ninguna cuenta de OpenAI; la credencial del proveedor es ficticia.

El proveedor falso de `deploy` (`dev/fake-upstream.caddy`) solo simula `/v1/messages` y `/v1/chat/completions`,
así que el proxy de la pila se apunta a `dev/fake_openai.py` (Responses en SSE, Chat Completions y `/v1/models`;
registra nombres de cabecera sin valores secretos) con la variable que ya admite `dev-up.sh`:

```bash
# pila (deploy): el OpenAI falso como contenedor en la red de la pila
podman run -d --name h2-plugin-fake-openai --network h2-plugin_default --network-alias fake-openai \
  -v "$PWD/dev/fake_openai.py:/app/fake_openai.py:ro,Z" docker.io/library/python:3.13-slim \
  python -u /app/fake_openai.py --host 0.0.0.0 --port 8091
BAAS_DEV_OPENAI_UPSTREAM=http://fake-openai:8091 COMPOSE_PROJECT_NAME=h2-plugin BAAS_DEV_HTTP_PORT=48080 \
  ../deploy/scripts/dev-up.sh --fake-upstream

# spike
PROXY_URL=http://proxy.localhost:48080 APP_URL=http://app.localhost:48080 \
  FAKE_LOG_CMD="podman logs h2-plugin-fake-openai" dev/spike_s13_codex.sh
```

Resultado (2026-11-17, Codex CLI 0.169.0, pila `h2-plugin` desde `upstream/main`):

| Caso | Configuración | Resultado |
| :--- | :--- | :--- |
| 1 | Usuario, `env_http_headers` | `exit 0`, respuesta del falso. Proveedor: `POST /v1/responses`, `stream: true`, `Authorization: Bearer sk-fak…`, `X-Baas-*`: ninguna, `originator: codex_exec`. Clave no escrita en disco |
| 2 | Usuario, `http_headers` (literal) | `exit 0`, igual que 1. La clave está en `config.toml` |
| 3 | `wire_api = "chat"` | `exit 1`: «`wire_api = "chat"` is no longer supported» (no hay petición) |
| 4 | Sin cabecera | `exit 1`: `401 Unauthorized: BaaS-AI-IP: falta la cabecera X-Baas-Api-Key o la clave no es válida` tras 5 reintentos. Queda un *snapshot* de shell con la clave |
| 5 | Solo proyecto, sin confianza | «Untrusted project-local configuration is disabled»; usa el proveedor `openai` (WebSocket y después HTTPS a OpenAI, que el aislamiento corta: «failed to lookup address») |
| 6 | Solo proyecto, con confianza | «Ignored unsupported project-local config keys: model_provider, model_providers»; usa `openai` como en 5 |
| 7 | Solo `-c` en la línea de órdenes (con `env_http_headers`) | `exit 0`, igual que 1 |
| 8 | Perfil `-p baas` (`$CODEX_HOME/baas.config.toml`) | `exit 0`, igual que 1 |
| — | Proyecto con confianza (`--trust`) y `model = "…"` | Se aplica (`model: gpt-proyecto`): la configuración de proyecto se lee, salvo el proveedor |
| — | Sesión persistida (sin `--ephemeral`) | La clave no aparece en `CODEX_HOME` (SQLite de estado, historial, registros) |

Traza en la webapp: conversaciones `OPENAI · RESPONSES · gpt-fake · cliente codex_exec/0.169.0` con 12/9 *tokens*
por turno (las cuatro ejecuciones correctas del script, con el mismo mensaje, quedaron en una conversación de 4 turnos).

## 5. Observaciones para otros componentes

- **deploy**: `dev/fake-upstream.caddy` no simula `POST /v1/responses`; Codex no se puede probar con la pila tal
  cual. Propuesta: añadir una respuesta SSE fija de Responses (como la de `dev/fake_openai.py`).
- **deploy**: las imágenes de `compose.dev.yml` se llaman `localhost/baas-dev/*` aunque cambie el proyecto de
  compose, así que dos pilas de desarrollo se pisan las etiquetas. Para este spike se renombraron en un *worktree*
  temporal (sin comitear). Propuesta: `localhost/${COMPOSE_PROJECT_NAME:-baas-dev}/*`.
- **proxy**: el proveedor recibe `Via`, `X-Forwarded-For`, `X-Forwarded-Host` y `X-Forwarded-Proto` (de Caddy y
  del proxy). En producción, `X-Forwarded-For` enviaría la IP del usuario al proveedor; conviene decidir si se
  quitan.

## 6. Lo que no se ha comprobado

- La **TUI** interactiva de Codex y la extensión de IDE: solo `codex exec`.
- Si los comandos de shell que lanza el **agente** ven `BAAS_API_KEY` (la `shell_environment_policy` de Codex
  debería excluir nombres con `KEY`, pero el modelo falso no pide herramientas).
- Que con el proveedor `openai` por defecto Codex **no** envíe las cabeceras de `baas` (es lo esperable por diseño,
  pero sin red no se observa ninguna petición a OpenAI).
- El transporte WebSocket de Responses con un proveedor propio (Codex usó HTTPS directamente con `baas`; con
  `openai` intenta WebSocket primero).
- Un proveedor OpenAI real, macOS y Windows, y **Cursor** (fuera del hito H2).
