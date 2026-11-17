# Pruebas de punta a punta

Scripts de `dev/` que recorren una pila **real** de BaaS-AI-IP (la de `deploy/compose.dev.yml`). No se
distribuyen con el plugin.

- [Hito H2 (épica E4)](#hito-h2-épica-e4): `dev/e2e_h2.py`, el plugin de punta a punta.
- [Hito H3 (AI-IP)](#hito-h3-ai-ip): `dev/e2e_h3.py`, anonimización y rehidratación por el proxy.

## Hito H2 (épica E4)

> **Estado**: en verde contra la pila de desarrollo (2026-11-17, `upstream/main` de `webapp`, `mcp-server`,
> `llm-interceptor-proxy` y `deploy`; Claude Code 2.1.325). Tarea L1 del plan del hito H2; diseño §5.4.

`dev/e2e_h2.py` recorre lo que hace un usuario con el plugin contra una pila **real** de BaaS-AI-IP, desde un
`HOME`, un proyecto (`git init`) y un `TMPDIR` temporales y limpios, y sale con `0` si todo pasa, `1` si falla
algún paso y `2` si los argumentos no valen. No se distribuye con el plugin.

### 1. Cómo lanzarla

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

### 2. Qué comprueba

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

### 3. Resultado (2026-11-17)

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

### 4. Observaciones

- La API de conversaciones devuelve `provider` y `api_flavor` **en mayúsculas** (`ANTHROPIC`, `MESSAGES`); la
  prueba compara sin distinguir mayúsculas.
- La conversación aparece primero con `turn_count = 0` y el turno se registra un instante después; por eso la
  prueba exige al menos un turno.
- Una petición mínima (sin `system` ni herramientas) queda marcada como `auxiliary: true`.
- El TOTP no admite reutilizar un código: dos ejecuciones seguidas en la misma ventana de 30 s hacen que la
  segunda espere a la siguiente (lo resuelve `BAAS_DEV_SKIP_TOTP=1`, tarea W1).

## Hito H3 (AI-IP)

> **Estado**: escrita y probada contra la pila simulada (`dev/fake_h3_stack.py`, 2026-11-17). Falta ejecutarla
> contra la pila real en la integración (C2 del plan): el proxy con AI-IP, la webapp con la organización `privada`
> y el borrado, y el proveedor falso con eco de `deploy` se desarrollan a la vez en otras ramas. Tarea L1 del plan
> del hito H3; diseño §5.4.

`dev/e2e_h3.py` comprueba AI-IP de punta a punta **por HTTP directo al proxy** (sin Claude Code), con la clave
determinista de `analista@privada.test` (contrato §8), en Messages, Chat Completions y Responses, con y sin
*streaming*. Sale con `0` si todo pasa, `1` si falla algún paso (dice cuál y por qué) y `2` si los argumentos no
valen. No se distribuye con el plugin y solo usa la biblioteca estándar.

### 1. Cómo lanzarla

1. Levanta la pila de `deploy` con el proveedor falso con eco (tarea D1), con un nombre de proyecto y puertos
   propios para no pisar `baas-dev`:

   ```bash
   cd ../deploy
   COMPOSE_PROJECT_NAME=h3-plugin BAAS_DEV_HTTP_PORT=48080 BAAS_DEV_POSTGRES_PORT=45432 \
   BAAS_DEV_REDIS_PORT=46379 BAAS_DEV_FAKE_UPSTREAM_PORT=48090 \
   BAAS_COMPOSE_CMD="uvx podman-compose --parallel 1" ./scripts/dev-up.sh --fake-upstream
   ```

2. Lanza la prueba con la webapp y el proveedor falso de esa pila (las URLs del proxy salen del *handshake*):

   ```bash
   uv run python dev/e2e_h3.py --app-url http://app.localhost:48080 --fake-url http://127.0.0.1:48090
   ```

   Con las mismas variables `BAAS_DEV_HTTP_PORT` y `BAAS_DEV_FAKE_UPSTREAM_PORT` exportadas basta con
   `uv run python dev/e2e_h3.py`; contra la pila por defecto (`8080` y `18090`), también.

| Parámetro | Por defecto | Para qué |
| :--- | :--- | :--- |
| `--app-url` | `$BAAS_E2E_APP_URL` o `http://app.localhost:${BAAS_DEV_HTTP_PORT:-8080}` | Webapp de la pila |
| `--fake-url` | `$BAAS_E2E_FAKE_URL` o `http://127.0.0.1:${BAAS_DEV_FAKE_UPSTREAM_PORT:-18090}` | Proveedor falso publicado en el host (`/_debug/requests`) |
| `--proxy-url` | `$BAAS_E2E_PROXY_URL` o las URLs del *handshake* | Cambia solo el origen del proxy y conserva `/org/<slug>/anthropic` y `/org/<slug>/openai/v1` |
| `--org` · `--email` | `privada` · `analista@privada.test` | Organización con AI-IP activo y usuario cuya clave determinista se usa |
| `--admin-email` | `orgadmin@privada.test` | ORG_ADMIN que lee las trazas y borra la conversación |
| `--bypass-org` · `--bypass-email` | `demo` · `analista@demo.test` | Organización con AI-IP apagado y su usuario |
| `--password` · `--totp-secret` | Los del contrato §8 | Sesión en la webapp (con `BAAS_DEV_SKIP_TOTP=1` basta la contraseña) |
| `--only` | `aiip,bypass,borrado` | Partes que se ejecutan (separadas por comas) |
| `--apis` | `messages,chat,responses` | APIs que se prueban |
| `--trace-timeout` | `30` | Segundos de espera de cada traza en la webapp |
| `--purge-wait` | `1` | Segundos entre el borrado y el turno siguiente: `conversation.purge` llega al proxy por Pub/Sub |
| `--settle` | `2` | Segundos antes de comprobar que las conversaciones borradas no reaparecen |
| `--timeout` | `60` | Tiempo máximo de cada petición HTTP |
| `--json <fichero>` | — | Informe en JSON (pasos, estado, duración, detalle, identificador de la ejecución) |

### 2. Qué comprueba

Cada caso envía como último mensaje del usuario `<etiqueta>. DNI 12345678Z, email ana.perez@privada.test,
IBAN ES91 2100 0418 4502 0005 1332.` La etiqueta (`caso-<ejecución>-<grupo>-<api>-<modo>`) solo tiene letras y
guiones, para que ninguna regla la reconozca, y el `model` también es único (`e2e-h3-<ejecución>-…`). Así cada
caso es una conversación nueva (tokens desde `001`), su petición se localiza en `/_debug/requests` y su
conversación en la webapp (`GET /api/v1/conversations?org=privada&q=<ejecución>`).

| Paso | Comprobación |
| :--- | :--- |
| 0 | `GET <app>/healthz` → `200` |
| 1 | `GET <fake>/_debug/requests` → `{"requests": […]}`; `DELETE` lo vacía |
| 2a · 2b | *Handshake* de `analista@privada.test` (`privada`, `ai_ip_enabled: true`) y de `analista@demo.test` (`demo`, `ai_ip_enabled: false`), con las URLs del proxy |
| 3 | Sesión en la webapp como `orgadmin@privada.test` |
| 4.1–4.6 | Texto, en cada API y modo: `200`; ningún `[[` en el cuerpo; el texto reconstruido de **cada copia** (deltas y, en Responses, `output_text.done`, `content_part.done`, `output_item.done` y `response.completed`) trae los tres valores y ningún `[[`; en *streaming*, `Content-Type: text/event-stream`. El proveedor recibió la petición en su ruta (`/v1/messages`, `/v1/chat/completions`, `/v1/responses`) con `[[DNI_001]]`, `[[EMAIL_001]]` e `[[IBAN_001]]` y ninguno de los valores |
| 5.1–5.6 | Lo mismo pidiendo la herramienta (`usa la herramienta echo`, con su definición en `tools`): hay una llamada a `echo` y sus argumentos son un objeto JSON válido con los tres valores y sin `[[` en cada copia (`input`, `input_json_delta`, `tool_calls[].function.arguments`, deltas y copias de `function_call`) |
| 6 | Cada conversación de 4 y 5 aparece en `GET /api/v1/conversations` con `ai_ip_enabled: true` y `verification_status: CLEAN`; su último turno (`GET …/turns/{n}`) tiene `verification.status: CLEAN`, `request_replacements_by_type` y `response_replacements_by_type` con `DNI`, `EMAIL` e `IBAN`, y ningún token sin resolver, huérfano ni coincidencia residual |
| 7.1–7.6 | Bypass con `analista@demo.test`: lo que recibió el proveedor es idéntico a lo que envió el cliente (como JSON, sin confundir `true` con `1`; se informa si además es idéntico byte a byte, lo que depende de cómo guarde el cuerpo el proveedor falso) y la respuesta trae el eco |
| 8.1–8.3 | Borrado, en cada API: primer turno → la conversación aparece en la webapp → `POST /api/v1/conversations/{id}/delete` `{"reason"}` → `200` con `propagated` → `GET` de la conversación → `404` → (tras `--purge-wait`) el turno siguiente con historial (la respuesta anterior del asistente; en Responses, `previous_response_id`) → `403`: `permission_error` con «eliminada» en el mensaje (Anthropic) o `permission_error` · `baas_conversation_deleted` (OpenAI); el proveedor no lo recibió |
| 8.4 | Tras `--settle` segundos, ninguna conversación borrada reaparece (el turno rechazado no emite eventos) |
| 9 | El mismo primer mensaje, sin historial, recibe `200` con los valores restaurados y abre una conversación **nueva** (id distinto del borrado) |
| 10 | Ninguna petición de `/_debug/requests` contiene los valores en claro, salvo las de bypass |

Los pasos que dependen de uno fallido quedan `SALTADO` (y cuentan como fallo). `/_debug/requests` guarda las 50
últimas peticiones; una ejecución completa envía 22 al proveedor.

### 3. Probarla sin la pila: `dev/fake_h3_stack.py`

`dev/fake_h3_stack.py` simula en un solo proceso la webapp, un proxy que anonimiza y rehidrata email, IBAN y DNI
(con los patrones del contrato §11, retención de los tokens partidos entre eventos, lápidas y
`previous_response_id`) y el proveedor falso con eco del plan (D1). No es una implementación de referencia: es un
doble para probar la prueba.

```bash
python3 dev/fake_h3_stack.py                       # imprime las URLs y el comando de la prueba
python3 dev/fake_h3_stack.py --fault leak          # con un fallo inyectado: la prueba sale con 1
python3 dev/fake_h3_stack.py --debug-body text     # el proveedor guarda los cuerpos como texto
```

`tests/test_e2e_h3.py` (con `unittest`; la CI lo ejecuta con `pytest`, y también vale
`python -m unittest discover -s tests -p test_e2e_h3.py`) prueba las utilidades y el recorrido completo: todo en
verde con el cuerpo guardado como objeto o como texto, y un fallo por cada caso que la prueba tiene que detectar:

| Fallo (`--fault`) | Paso que falla |
| :--- | :--- |
| `leak` (valores en claro hacia el proveedor) | 4, 5 y 10 |
| `no-rehydrate` (tokens sin restaurar) | 4 y 5 |
| `responses-done-unrestored` (copias completas de Responses sin restaurar) | 4.6 y 5.6 |
| `bad-tool-json` (valor insertado sin escapar en los argumentos) | 5.2–5.6 |
| `warning` (verificación distinta de `CLEAN`) | 6 |
| `no-trace` (el turno no llega a la webapp) | 6 y 8 |
| `aiip-off` (`privada` sin AI-IP) | 2a (y los que dependen de él, `SALTADO`) |
| `bypass-mutates` (el proxy altera el cuerpo con AI-IP apagado) | 7 |
| `delete-not-blocking` · `delete-wrong-code` (el borrado no bloquea o no da `baas_conversation_deleted`) | 8.1–8.3 |
| `delete-not-removed` (la webapp no borra) | 8.1–8.3 |
| `blocked-emits-events` (el turno rechazado vuelve a crear la conversación) | 8.4 |
| `fresh-blocked` (una conversación nueva con la misma huella se bloquea) | 9 |

### 4. Supuestos sobre los demás componentes

Fijados por el plan y el contrato; conviene confirmarlos al integrar:

- **Proveedor falso (D1)**: el eco es del último mensaje del usuario, también cuando su contenido es una lista de
  partes (`input_text` en Responses); con `usa la herramienta` responde **solo** con la llamada a `echo` (si además
  trae texto, se exige que no tenga `[[`); devuelve un `id` en Responses sin *streaming*; `body` de
  `/_debug/requests` es el objeto JSON o el texto recibido (se admiten los dos); `DELETE` responde `2xx`.
- **Proxy**: no anonimiza `model` ni la etiqueta (solo letras y guiones); con AI-IP apagado reenvía el cuerpo sin
  cambios; una conversación borrada rechaza también la cadena de Responses que parte de una de sus respuestas.
- **Webapp**: `GET /api/v1/conversations` filtra por `q` sobre el modelo y devuelve `model`, `turn_count`,
  `ai_ip_enabled` y `verification_status`; `GET /api/v1/conversations/{id}` devuelve `turns` y
  `GET …/turns/{n}`, `verification`; tras el borrado, `GET /api/v1/conversations/{id}` → `404`.

### 5. Resultado

Pendiente de la integración (C2). Contra la pila simulada, la ejecución completa pasa en menos de un segundo.
