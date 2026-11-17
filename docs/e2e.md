# Prueba de punta a punta del hito H2 (épica E4)

> **Estado**: en verde contra la pila de desarrollo (2026-11-17, `upstream/main` de `webapp`, `mcp-server`,
> `llm-interceptor-proxy` y `deploy`; Claude Code 2.1.325). Tarea L1 del plan del hito H2; diseño §5.4.

`dev/e2e_h2.py` recorre lo que hace un usuario con el plugin contra una pila **real** de BaaS-AI-IP, desde un
`HOME`, un proyecto (`git init`) y un `TMPDIR` temporales y limpios, y sale con `0` si todo pasa, `1` si falla
algún paso y `2` si los argumentos no valen. No se distribuye con el plugin.

## 1. Cómo lanzarla

1. Levanta la pila de `deploy` con el proveedor falso:

   ```bash
   cd ../deploy && ./scripts/dev-up.sh --fake-upstream
   ```

   Puertos, nombre del proyecto de compose y carpeta de los repositorios se cambian con variables de entorno o
   con `deploy/.env` (`env/dev.env.example`): `COMPOSE_PROJECT_NAME`, `BAAS_DEV_HTTP_PORT`,
   `BAAS_DEV_POSTGRES_PORT`, `BAAS_DEV_REDIS_PORT`, `BAAS_SRC_DIR`.

2. Lanza la prueba con la URL de la webapp de esa pila (las del MCP y del proxy salen del *handshake*, como
   en el plugin):

   ```bash
   uv run python dev/e2e_h2.py --app-url http://app.localhost:8080
   ```

| Parámetro | Por defecto | Para qué |
| :--- | :--- | :--- |
| `--app-url` | `$BAAS_E2E_APP_URL` o `http://app.localhost:8080` | Webapp de la pila |
| `--email` | `analista@demo.test` | Usuario de desarrollo; su API key es la determinista del contrato §8 |
| `--password` · `--totp-secret` | Los del contrato §8 | Sesión en la webapp para leer las trazas |
| `--org` | `demo` | Organización que se vincula |
| `--no-claude` | — | No registra el MCP con el CLI `claude` (sin Claude Code instalado) |
| `--trace-timeout` | `30` | Segundos de espera de la traza |
| `--keep` | — | Conserva el `HOME` y el proyecto temporales |
| `--json <fichero>` | — | Informe en JSON (pasos, estado, duración y tiempos de `/baas-init`) |

## 2. Qué comprueba

| Paso | Comprobación |
| :--- | :--- |
| 0 | `GET <app>/healthz` → `200` |
| 1 | `/baas-init` sin interacción: `baas.py set-key --stdin --app-url …` + `baas.py init --org demo`. **Cronometrado** (objetivo < 120 s, sin contar la descarga del plugin). `settings.local.json` (0600) con `ANTHROPIC_BASE_URL` = `proxy_anthropic_url` y `X-Baas-Api-Key`; `baas.local.json` sin la clave; ambos en `.gitignore`; `~/baas-ai-ip/.env` 0600; la clave no aparece en la salida |
| 1b | Servidor MCP `baas` en el ámbito local de `~/.claude.json` (`type: http`, `mcp_url`, `headersHelper`) **sin** la clave. Con `--no-claude`, se omite |
| 2 | `baas.py status`: usuario, organización, roles y «MCP `baas`: accesible» |
| 3 | `baas_search_knowledge` («indicadores de agenda»): aparece `knowledge/operaciones/indicadores-de-agenda.md` y no el documento de `revisor` |
| 4 | `baas_read_document` de ese documento; su `sha256` coincide con el del contenido |
| 5 | `baas_read_document` de `knowledge/calidad/criterios-de-revision.md` → `AUTHORIZATION_DENIED` con `revisor` en `required_roles` (criterio de aceptación 6) |
| 6 | `baas_get_script` (`analisis-de-citas`, `resumen_citas.py`): `path` y `sha256` correctos. `baas.py run-script … --sha256 <hash>` sobre un CSV da la salida esperada; con un `sha256` falso sale con `9` y no se ejecuta; no quedan temporales `baas-script-*` |
| 7a | Sesión en la webapp como el mismo usuario: contraseña y TOTP (RFC 6238 con la biblioteca estándar); si el inicio de sesión ya da la cookie de sesión (`BAAS_DEV_SKIP_TOTP=1`), solo contraseña. Se anotan las conversaciones previas |
| 7b | `POST <ANTHROPIC_BASE_URL>/v1/messages` con las cabeceras de `ANTHROPIC_CUSTOM_HEADERS` (leídas de `.claude/settings.local.json`) y una credencial de proveedor ficticia → `200` con el texto del proveedor falso. Sin `X-Baas-Api-Key` → `401 authentication_error` |
| 8 | `GET /api/v1/conversations?org=demo`: aparece una conversación nueva (o con más turnos) de Anthropic del usuario, con al menos un turno |

Los pasos que dependen de uno fallido se marcan como `SALTADO` (y también cuentan como fallo).

## 3. Resultado (2026-11-17)

Pila `h2-plugin` (`dev-up.sh --fake-upstream`, puertos `48080`/`45432`/`46379`) construida desde `upstream/main`:

| Paso | Estado | Tiempo | Detalle |
| :--- | :---: | ---: | :--- |
| 0. webapp accesible | OK | 0,00 s | |
| 1. `/baas-init` sin interacción | OK | **0,40 s** | `set-key` 0,06 s + `init` 0,33 s (incluye `claude mcp add-json`); objetivo < 120 s |
| 1b. MCP `baas` registrado sin la clave | OK | 0,00 s | |
| 2. `/baas-status` | OK | 0,07 s | `analista@demo.test`, `demo`, USER, `analista`, «MCP `baas`: accesible» |
| 3. `baas_search_knowledge` | OK | 0,01 s | 3 resultados: `indicadores-de-agenda.md`, `skills/analisis-de-citas/SKILL.md`, `protocolo-de-citas.md` |
| 4. `baas_read_document` | OK | 0,01 s | `sha256` verificado |
| 5. lectura denegada (criterio 6) | OK | 0,01 s | `AUTHORIZATION_DENIED` · `required_roles: ["revisor"]` |
| 6. `baas_get_script` + `run-script` | OK | 0,18 s | «cardiologia: 50 citas, 20.0 % no presentados»; `sha256` falso → código 9 |
| 7a. sesión en la webapp | OK | 5,43 s | Contraseña + TOTP (la espera es la de la ventana de 30 s cuando el código anterior ya se usó) |
| 7b. petición por el proxy | OK | 0,01 s | `200`; sin cabecera, `401 authentication_error` |
| 8. traza | OK | 0,28 s | `ANTHROPIC` · `MESSAGES` · `claude-fake` · cliente `baas-e2e-h2/1` · 1 turno · 12/9 *tokens*; visible 0,29 s después de la petición |

El tiempo de `/baas-init` medido es el de los scripts del plugin (lo que ejecuta la skill); no incluye la
descarga del plugin ni el tiempo de respuesta del modelo en Claude Code. También pasa con `--no-claude`.

## 4. Observaciones

- La API de conversaciones devuelve `provider` y `api_flavor` **en mayúsculas** (`ANTHROPIC`, `MESSAGES`); la
  prueba compara sin distinguir mayúsculas.
- La conversación aparece primero con `turn_count = 0` y el turno se registra un instante después; por eso la
  prueba exige al menos un turno.
- Una petición mínima (sin `system` ni herramientas) queda marcada como `auxiliary: true`.
- El TOTP no admite reutilizar un código: dos ejecuciones seguidas en la misma ventana de 30 s hacen que la
  segunda espere a la siguiente (lo resuelve `BAAS_DEV_SKIP_TOTP=1`, tarea W1).
