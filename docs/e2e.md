# Pruebas de punta a punta

Scripts de `dev/` que recorren una pila **real** de BaaS-AI-IP (la de `deploy/compose.dev.yml`). No se
distribuyen con el plugin.

- [Hito H2 (épica E4)](#hito-h2-épica-e4): `dev/e2e_h2.py`, el plugin de punta a punta.
- [Hito H3 (AI-IP)](#hito-h3-ai-ip): `dev/e2e_h3.py`, anonimización y rehidratación por el proxy.
- [Hito H4 (observación y eliminación de organizaciones)](#hito-h4-observación-y-eliminación-de-organizaciones):
  `dev/e2e_h4.py`, el anonimizador apagado, el interruptor y eliminar una organización.
- [Mediciones de H4](#mediciones-de-h4): `dev/measure_h4.py` y `dev/measure_frozen_redis.py`.
- [E9 (roles funcionales)](#e9-roles-funcionales): `dev/e9_roles.py`, que los roles aíslan el contenido aunque una skill lo cite.

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

## Hito H4 (observación y eliminación de organizaciones)

> **Estado**: escrita y probada **solo contra la pila simulada** (`dev/fake_h4_stack.py`, 2026-11-17). **No se ha
> ejecutado contra la pila real**: la webapp con el interruptor y la eliminación, el proxy con el modo observación, el
> MCP con la cuarentena y `deploy` con el informe del monitor se desarrollan a la vez en otras ramas. El coordinador la
> ejecuta entera sobre `h4-int` en la tarea C2 del plan del hito H4. Tarea L1 del plan; diseño §5.5 y §6.

`dev/e2e_h4.py` comprueba por **HTTP directo** (sin Claude Code) las dos novedades de H4 de punta a punta, con las
claves deterministas del contrato §8 de `observador@observacion.test`, `admin@baas.test` (PLATFORM_ADMIN),
`orgadmin@demo.test` y `analista@demo.test`. Sale con `0` si todo pasa, `1` si falla algún paso (dice cuál y por qué) y
`2` si los argumentos no valen. No se distribuye con el plugin y solo usa la biblioteca estándar.

### 1. Cómo lanzarla

1. Levanta la pila de `deploy` con el proveedor falso con eco, con un proyecto y puertos propios (la prueba cambia el
   interruptor de `observacion` y crea y elimina una organización: no la lances contra una pila con datos que importen):

   ```bash
   cd ../deploy
   COMPOSE_PROJECT_NAME=h4-plugin BAAS_DEV_HTTP_PORT=48080 BAAS_DEV_POSTGRES_PORT=45432 \
   BAAS_DEV_REDIS_PORT=46379 BAAS_DEV_FAKE_UPSTREAM_PORT=48090 \
   BAAS_COMPOSE_CMD="uvx podman-compose --parallel 1" ./scripts/dev-up.sh --fake-upstream
   ```

2. Lánzala con la webapp y el proveedor falso de esa pila (las URLs del proxy y del MCP salen del *handshake*) y el
   nombre del proyecto de compose, para mirar dentro de sus contenedores:

   ```bash
   COMPOSE_PROJECT_NAME=h4-plugin uv run python dev/e2e_h4.py \
     --app-url http://app.localhost:48080 --fake-url http://127.0.0.1:48090
   ```

   Con `BAAS_DEV_HTTP_PORT` y `BAAS_DEV_FAKE_UPSTREAM_PORT` exportados bastan `COMPOSE_PROJECT_NAME=h4-plugin uv run python
   dev/e2e_h4.py`; contra la pila por defecto (`8080`, `18090` y `baas-dev`), también.

| Parámetro | Por defecto | Para qué |
| :--- | :--- | :--- |
| `--app-url` | `$BAAS_E2E_APP_URL` o `http://app.localhost:${BAAS_DEV_HTTP_PORT:-8080}` | Webapp de la pila |
| `--fake-url` | `$BAAS_E2E_FAKE_URL` o `http://127.0.0.1:${BAAS_DEV_FAKE_UPSTREAM_PORT:-18090}` | Proveedor falso publicado en el host (`/_debug/requests`) |
| `--proxy-url` | `$BAAS_E2E_PROXY_URL` o las URLs del *handshake* | Cambia solo el origen del proxy y conserva `/org/<slug>/anthropic` y `/org/<slug>/openai/v1` |
| `--org` · `--email` | `observacion` · `observador@observacion.test` | Organización con AI-IP activo y anonimizador apagado, y su usuario (USER, dueño de las conversaciones) |
| `--admin-email` | `admin@baas.test` | PLATFORM_ADMIN que cambia el interruptor, crea, bloquea y elimina, y al que se le comprueba que no ve contenido |
| `--orgadmin-email` | `orgadmin@demo.test` | ORG_ADMIN de otra organización: debe recibir `404` al cambiar el interruptor y es el primer miembro de la organización que se elimina |
| `--bypass-org` · `--bypass-email` | `demo` · `analista@demo.test` | Organización con AI-IP apagado |
| `--only` | `observacion,interruptor,eliminar` | Partes que se ejecutan (separadas por comas) |
| `--apis` | `messages,chat,responses` | APIs que se prueban |
| `--inspect` | `auto` | `podman`, `docker` o `auto` (el primero que vea el proyecto) para mirar el vault, Redis y la cuarentena del MCP; `none` omite esos pasos (9.1b y 9.8), que no se registran |
| `--compose-project` | `$COMPOSE_PROJECT_NAME` o `baas-dev` | Proyecto de compose de la pila |
| `--mcp-data-dir` | `/data` | `BAAS_DATA_DIR` del contenedor del MCP |
| `--trace-timeout` | `30` | Segundos de espera de cada traza o de lo que se propaga (bloqueo, eliminación, cuarentena) |
| `--propagation-wait` | `1.5` | Segundos entre cambiar el anonimizador y el turno siguiente (`ai_ip.changed` llega al proxy por Pub/Sub) |
| `--timeout` | `60` | Tiempo máximo de cada petición HTTP |
| `--json <fichero>` | — | Informe en JSON (pasos, estado, duración, detalle, identificador de la ejecución) |

### 2. Qué comprueba

Cada caso envía como último mensaje del usuario `<etiqueta>. DNI 12345678Z, email ana.perez@privada.test,
IBAN ES91 2100 0418 4502 0005 1332.` con una etiqueta única de la ejecución (solo letras y guiones) y un `model` único
(`e2e-h4-<ejecución>-…`), como en H3: cada caso es una conversación nueva y se localiza en `/_debug/requests` y en la
webapp (`?q=<ejecución>`).

| Paso | Comprobación |
| :--- | :--- |
| 0 · 1 | `GET <app>/healthz` → `200`; `GET <fake>/_debug/requests` responde y se vacía |
| 2 · 2b · 3 | *Handshake* de `observador@observacion.test` (`observacion`, AI-IP ✔) y de `analista@demo.test` (`demo`, AI-IP ✘); sesiones de `admin@baas.test`, `observador@observacion.test`, `analista@demo.test` y `orgadmin@demo.test` |
| 4 | `GET /api/v1/orgs/observacion` → AI-IP ✔ y `anonymizer_enabled: false`; si estaba encendido (una ejecución anterior que se cortó), se apaga con motivo y se anota |
| 5.1–5.6 | **Anonimizador apagado**, en Messages, Chat Completions y Responses, con y sin *streaming*: `200`; cada copia del texto de la respuesta trae los tres valores, ningún `[[` y el texto enviado (eco completo); el proveedor recibió el DNI, el email y el IBAN **en claro**, ningún token y un cuerpo **idéntico** al que envió el cliente (como JSON; byte a byte si el proveedor guarda texto) |
| 6 | Como dueño: cada caso es una conversación con AI-IP ✔ y `anonymizer_enabled: false` (**la causa**), `verification_status: WARNING`; su turno trae `verification.status: WARNING`, reemplazos de ida y de vuelta vacíos y `residual_matches` con `DNI`, `EMAIL` e `IBAN`, ningún token sin resolver ni huérfano; **control positivo**: el dueño sí lee el texto enviado en claro |
| 7.1 | Como `admin@baas.test` (PLATFORM_ADMIN, no miembro de `observacion`): la lista, el detalle, cada turno y el revelado de cada caso. `content_available: false`, sin `request_sent`, `response_received`, `request_original` ni `response_returned`, sin valores en `replacements`, con las métricas (`verification`); ninguna respuesta contiene los valores, la etiqueta del caso ni el eco; el revelado no responde `200` |
| 7.2 | Lo mismo con **AI-IP apagado** (`demo`): el texto enviado es el original y el PLATFORM_ADMIN tampoco lo ve (el turno no tiene `verification`) |
| 7.3 | **WebSocket** `/api/v1/ws/traces?org=observacion` (cliente mínimo con la biblioteca estándar, con la cookie y `Origin`) como PLATFORM_ADMIN y como dueño durante un turno nuevo: el PLATFORM_ADMIN recibe tramas de esa conversación y ninguna lleva valores, etiqueta, eco ni contenido en los turnos; se anota si el dueño sí recibió el contenido (control positivo) |
| 8.1 | Permisos y validaciones del interruptor (`PUT /api/v1/orgs/observacion/anonymizer`): sin motivo y con un motivo de 2 caracteres → `422 REASON_REQUIRED`; `observador@observacion.test` (USER) → `403`; `orgadmin@demo.test` (ORG_ADMIN de otra) → `404`; en `demo` (AI-IP apagado) → `409 AI_IP_DISABLED`; ninguna de ellas cambia el valor |
| 8.2–8.8 | **El interruptor en los dos sentidos**, por API (sin *streaming*; Responses con `previous_response_id`): la conversación **A** empieza con el anonimizador apagado → `admin@baas.test` lo **enciende** (con `propagated`) → **B**, nueva, sale tokenizada (solo `[[DNI_001]]`, `[[EMAIL_001]]` e `[[IBAN_001]]` hasta el proveedor y valores restaurados para el cliente) y **A**, con historial, **sube a anonimizada en su siguiente turno** (trinquete, H4-08: el proveedor ya no recibe ningún valor, tampoco los del historial) → lo **apaga** → **B sigue tokenizada** y restaurada, **C**, nueva, sale en claro y **A no baja** (8.9: con el historial completo sigue tokenizada) |
| 8.10 | En la webapp: A con el anonimizador ✘ al empezar, `WARNING` (su turno 1) y `protected_from_turn` = 2; C con ✘ y `WARNING`, sin `protected_from_turn`; B con ✔ y `CLEAN` |
| 8.11 | El PLATFORM_ADMIN ve **por turno** la conversación A (H4-08): el turno 1 (salió en claro) sin contenido ni valores (`content_available: false`) y los siguientes (salieron anonimizados) con `content_available: true`, solo con tokens, sin `request_original`, sin valores y sin poder revelarlos |
| 8.12 | El interruptor vuelve a su valor inicial (apagado), también si algún paso falló |
| 9.1 | Como PLATFORM_ADMIN: crear `e2e-<ejecución>`, invitar a `orgadmin@demo.test` como ORG_ADMIN, activar AI-IP; un turno con valores (el anonimizador está encendido por defecto) y un documento escrito por el MCP (`baas_write_document`), para que haya vault y directorio |
| 9.1b | Con el motor de contenedores: el vault tiene filas de esa organización antes de eliminarla (si no, «vacío» después no probaría nada) |
| 9.2 | Condiciones: sin bloquear → `409 ORG_NOT_BLOCKED`; su ORG_ADMIN → `403`; la organización meta (`baas-ai-ip`, sin bloquear) → `403 ORG_PROTECTED`; sin motivo → `422 REASON_REQUIRED`; la organización sigue existiendo |
| 9.3 | Bloquearla (`PUT …/block`): el proxy responde `403 baas_access_blocked` a su ORG_ADMIN; con otro slug de confirmación → `422 SLUG_MISMATCH` |
| 9.4 | `POST /api/v1/orgs/e2e-<ejecución>/delete` `{"reason", "confirm_slug"}` → `200` con `propagated` |
| 9.5 | Webapp: `404` (y `404` la segunda eliminación, sin conversaciones listadas); proxy: `403 baas_access_denied`; MCP: `404`; el *handshake* de su ORG_ADMIN sigue funcionando y ya no la lista (los usuarios no se eliminan) |
| 9.6 | Crear otra vez el mismo slug → `409 SLUG_TAKEN`, el mismo error que un slug en uso |
| 9.7 | `GET /api/v1/audit?action=org.deleted`: la entrada de la eliminación, con el slug y el motivo, sobrevive a la organización |
| 9.8 | Con el motor de contenedores: el vault no tiene filas de la organización (`vault_entries`, `vault_counters` y `conversation_state` en `baas_vault`), Redis no tiene `baas:policy:org:<slug>` ni `baas:member:*:<slug>` y el directorio del MCP ya no está en `orgs/` sino en `.quarantine/<slug>-<fecha>` |

Los pasos que dependen de uno fallido quedan `SALTADO` (y cuentan como fallo). El bloqueo, la eliminación y la
cuarentena se esperan hasta `--trace-timeout` s porque llegan a proxy y MCP por Pub/Sub. Una ejecución completa envía
unas 25 peticiones al proveedor (`/_debug/requests` guarda las 50 últimas). Si una ejecución se corta entre 9.1 y 9.4,
queda una organización `e2e-<ejecución>` que se puede bloquear y eliminar a mano desde W3.

### 3. Qué se ejecutó de verdad y qué no

| Qué | Estado |
| :--- | :--- |
| Utilidades puras (SSE, comprobaciones de contenido, cliente WebSocket, inspector) | **Ejecutado**: `tests/test_e2e_h4.py` (la CI) |
| Recorrido completo contra `dev/fake_h4_stack.py` (webapp, proxy, MCP y proveedor simulados) | **Ejecutado**: todos los pasos en verde con el cuerpo del proveedor guardado como objeto y como texto, y dos ejecuciones seguidas |
| Cada fallo inyectable de la pila simulada (31) detectado por el paso que le corresponde | **Ejecutado** (tabla de abajo) |
| Contra la pila real de H4 (`h4-int`): proxy real con el modo observación y el estado por conversación, webapp real con el interruptor, la eliminación, la vista del PLATFORM_ADMIN y el WebSocket, MCP real con la cuarentena, vault y Redis reales | **No ejecutado.** Lo hace el coordinador en C2. Los pasos 5, 6, 7, 8 y 9 dependen de los cuatro componentes |
| La interfaz web (W3, W7, W8, W11 en el navegador, en `es` y `en`) y «el PLATFORM_ADMIN no ve el contenido **por la UI**» | **Fuera de esta prueba.** La UI pinta lo que devuelve la API que se comprueba aquí (`content_available`); el recorrido en el navegador es de C2 |
| Claude Code real y Codex real | Fuera de esta prueba (prueba manual del usuario, C2; `docs/codex.md`) |

### 4. Probarla sin la pila: `dev/fake_h4_stack.py`

`dev/fake_h4_stack.py` simula en un solo proceso la webapp (con el interruptor, crear, bloquear, invitar y eliminar
organizaciones, la auditoría, la vista de verificación del PLATFORM_ADMIN y el WebSocket), un proxy que sustituye y
restaura email, IBAN y DNI **solo con el anonimizador encendido** y que en observación reenvía el cuerpo tal cual con
`WARNING` y `residual_matches`, un MCP con `baas_write_document` y la cuarentena, y el proveedor con eco de H3. Importa
de `dev/fake_h3_stack.py` el proveedor, el vault y la rehidratación sin tocarlo. No es una implementación de referencia:
es un doble para probar la prueba.

```bash
python3 dev/fake_h4_stack.py                          # imprime las URLs y el comando de la prueba
python3 dev/fake_h4_stack.py --fault observe-rewrites # con un fallo inyectado: la prueba sale con 1
```

`tests/test_e2e_h4.py` ejecuta el recorrido completo y, para cada fallo, comprueba el código `1`, el paso que falla y su
motivo; `test_every_fault_is_detected` falla si se añade un fallo sin su caso.

| Fallo (`--fault`) | Paso que falla |
| :--- | :--- |
| `observe-rewrites` (el proxy altera el cuerpo con el anonimizador apagado) · `observe-anonymizes` (sustituye igualmente) | 5 |
| `observe-clean` (el turno en observación sale `CLEAN` sin coincidencias) | 6 y 8.10 |
| `no-cause` (la webapp no dice con qué anonimizador empezó la conversación) | 6 y 8.10 |
| `state-follows-switch` (una conversación abierta adopta el valor actual del interruptor, en los dos sentidos) | 8.7 y 8.9 |
| `no-ratchet` (una conversación abierta en claro no sube al encender el anonimizador: el comportamiento de antes de H4-08) | 8.5 |
| `ratchet-lowers` (apagar el anonimizador baja una conversación que ya estaba anonimizada) | 8.9 |
| `no-protected-from` (la webapp no informa de `protected_from_turn`) | 8.10 |
| `admin-sees-pre-raise` (el PLATFORM_ADMIN ve el texto de los turnos que salieron en claro) · `admin-hides-raised` (no ve los que ya salieron anonimizados) | 8.11 |
| `toggle-no-propagation` (el proxy no se entera del cambio) | 8.4 |
| `admin-sees-content-api` (el PLATFORM_ADMIN recibe el texto en turnos, lista y WebSocket) · `admin-sees-bypass` (también con AI-IP apagado) | 7.1, 7.2 y 7.3 · 7.2 |
| `admin-detail-leak` (el detalle trae texto) · `admin-reveal` (puede revelar) · `ws-leak` · `ws-silent` | 7.1 · 7.1 · 7.3 · 7.3 |
| `toggle-no-reason` · `toggle-user-allowed` · `toggle-foreign-403` · `toggle-ai-ip-off-allowed` | 8.1 |
| `toggle-not-applied` (responde `200` y no cambia nada) | 8.3 |
| `delete-no-block-check` · `delete-meta-allowed` · `delete-orgadmin-allowed` | 9.2 |
| `delete-no-slug-check` · `block-not-applied` | 9.3 |
| `delete-twice-ok` · `proxy-still-serves` · `mcp-still-serves` · `delete-removes-users` | 9.5 |
| `slug-reusable` · `audit-missing` | 9.6 · 9.7 |
| `vault-kept` · `redis-kept` · `no-quarantine` | 9.8 |

### 5. Supuestos sobre los demás componentes

Fijados por el contrato y el plan, y contrastados con las ramas de los demás componentes en el momento de escribir esto
(`webapp` `feat/h4-webapp` en `3203758`, `llm-interceptor-proxy` `feat/h4-observacion` en `681a454`); conviene
confirmarlos al integrar:

- **Webapp**: `PUT /api/v1/orgs/{slug}/anonymizer` y `POST …/delete` responden como `PUT …/ai-ip` (`organization` y
  `propagated`; se acepta también la organización en la raíz); la organización (`GET /api/v1/orgs/{slug}`) y la
  conversación (lista y detalle) traen `anonymizer_enabled` (se acepta también `anonymizer_enabled_at_start`) y, desde
  H4-08, `protected_from_turn`; la conversación y el turno traen `content_available`, falso para el PLATFORM_ADMIN en una
  organización ajena en los turnos que salieron con AI-IP o el anonimizador apagados (por turno desde H4-08); `GET /api/v1/audit?action=org.deleted` lista la entrada con el slug y el motivo en
  cualquier campo; `POST /api/v1/orgs/{slug}/members` con un usuario existente lo añade sin pasos más; el WebSocket
  `/api/v1/ws/traces?org=<slug>` envía primero `{"type": "hello"}` y después mensajes `turn.completed` con la
  conversación y el turno ya filtrados por espectador. Las comprobaciones de la eliminación siguen el orden de la
  webapp (`404` → `403` → `ORG_PROTECTED` → `REASON_REQUIRED` → `SLUG_MISMATCH` → `ORG_NOT_BLOCKED`); cada paso prueba
  una sola condición, así que no depende de él.
- **Proxy**: con AI-IP activo y el anonimizador apagado, el cuerpo sale idéntico (la prueba lo compara con lo que guarda
  `/_debug/requests`), `residual_matches` trae una entrada por regla con `token_type` y `count` (las de `DNI`, `EMAIL`
  e `IBAN` al menos; otras reglas, como la de tarjeta, pueden añadir las suyas), y una organización bloqueada o eliminada
  responde `403` con `baas_access_blocked` o `baas_access_denied` (Chat Completions).
- **MCP**: `…/org/<slug>/mcp` → `404` tras eliminar la organización; `baas_write_document` en un documento
  `knowledge/e2e/e2e-<ejecución>.md` con `roles: []` lo acepta un ORG_ADMIN; la cuarentena es `<datos>/.quarantine/<slug>-<fecha>`
  (`quarantine.py`) y el directorio de datos de su contenedor es `/data` (`--mcp-data-dir`).
- **Pila**: el contenedor de PostgreSQL se llama por su servicio (`com.docker.compose.service=postgres`) y admite
  `psql -U postgres` sin contraseña desde dentro; el de Redis tiene `BAAS_REDIS_PASSWORD` en su entorno; el del MCP lleva
  `ls`. Con `--inspect none`, o sin `podman`/`docker` que vea el proyecto, los pasos 9.1b y 9.8 se omiten o fallan con
  el motivo.

### 6. Resultado

Pendiente de la integración (C2). Contra la pila simulada, la ejecución completa pasa en un par de segundos.

## E9 (roles funcionales)

`dev/e9_roles.py` comprueba contra la pila real que los **roles funcionales aíslan el contenido** de una organización,
aunque una skill del rol A cite la ruta de un documento del rol B (criterio 6 de la Fase 1). Crea lo que falte, así que
se puede lanzar las veces que haga falta.

### 1. Cómo lanzarla

La pila de `deploy` en modo desarrollo (`BAAS_DEV_SKIP_TOTP=1`, como `compose.dev.yml`):

```bash
uv run python dev/e9_roles.py --app-url http://app.localhost:8080 --evidence e9-evidencia.json
```

Sale con `0` si todo pasa, `1` si algún paso falla y `2` si los argumentos no valen. `--evidence` guarda cada llamada MCP
con su respuesta (sin API keys ni enlaces de activación; los textos largos se recortan). Los inicios de sesión están
limitados (20 por minuto y IP): con `RATE_LIMITED` la prueba espera y repite.

### 2. Qué crea y qué comprueba

Como PLATFORM_ADMIN (`admin@baas.test`), por la API de la webapp:

- La organización `e9-prueba` («E9 Prueba») con los roles funcionales `recepcion` y `facturacion`.
- Cuatro usuarios nuevos (`…@e9.test`, contraseña de desarrollo, activados por la API): un ORG_ADMIN, un USER solo con
  `recepcion`, un USER solo con `facturacion` y un USER sin rol funcional. Cada ejecución les crea una API key
  `e9-roles-<fecha>` y revoca las anteriores. Las URL del MCP salen del *handshake*.

Como ORG_ADMIN, **por MCP** y con el procedimiento de las skills `componer-conocimiento` y `componer-skill` de la
organización meta (buscar antes de crear, cabecera válida, `expected_sha256`, verificar con la búsqueda): un documento
por rol (`knowledge/recepcion/…`, `knowledge/facturacion/…`), un documento sin roles, y una skill con script por rol
(`gestion-de-citas`, `emision-de-facturas`) que **cita la ruta del documento del otro rol**.

Después, con la clave de cada usuario (9 búsquedas, filtros por `type` y `area` incluidos):

| Quién | Qué se comprueba |
| :--- | :--- |
| ORG_ADMIN y PLATFORM_ADMIN | Leen los siete ficheros y encuentran ambos documentos (control positivo) |
| USER con `recepcion` | Busca, lee y obtiene su documento, su skill y su script (con su `sha256`); la búsqueda y `baas_list_skills` no devuelven ni dejan ver trozos de lo de `facturacion`; leerlo por su ruta, su skill, su script o **siguiendo la cita de su skill** da `AUTHORIZATION_DENIED` con `required_roles: ["facturacion"]`; escribir da `AUTHORIZATION_DENIED` (`ORG_ADMIN`, `PLATFORM_ADMIN`) y no cambia nada |
| USER con `facturacion` | Lo simétrico |
| USER sin rol funcional | `baas_list_skills` vacío, búsquedas vacías y todas las lecturas denegadas |
| USER de otra organización (`user@otra.test`) | La ruta `/org/e9-prueba/mcp` responde `404 NOT_FOUND`; con `org=e9-prueba` desde su organización, `NOT_FOUND` en cinco tools |

`tests/test_e9_roles.py` prueba sin pila las utilidades puras, que el contenido es válido para el MCP, que lo visible para un
rol no contiene lo del otro (para que no haya falsos positivos) y que las funciones que buscan fugas detectan una.

### 3. Resultado (2026-11-18, pila `h4-int`)

La primera ejecución crea todo lo que falta (organización, roles, usuarios y los siete ficheros); las siguientes no crean
nada y repiten las comprobaciones. Con una mutación (el documento de facturación visible también para `recepcion`) el paso del rol `recepcion` falla y nombra la
ruta, el título y el extracto que se ven.

## Mediciones de H4

Dos scripts de `dev/` miden la pila de desarrollo real (`deploy/scripts/dev-up.sh --fake-upstream`, por defecto en
`*.localhost:28080`; si usas otros puertos, cambia las constantes `APP`, `PROXY` y `MCP`). Los resultados y su
método están en `knowledge/01-producto/fases/fase-1-h4-mediciones.md`. Son de una máquina de desarrollo: se repiten
en el servidor real (tramo 2).

| Script | Qué mide | Duración |
| :--- | :--- | :--- |
| `dev/measure_h4.py salida.json [n]` | Kill switch global → rechazo en el proxy y en el MCP, y petición al proxy → trama del WebSocket (con AI-IP y en bypass). Activa y levanta el kill switch `n` veces (30 por defecto) | ~1 min |
| `dev/measure_frozen_redis.py salida.json` | Redis **congelado** (`podman pause` del contenedor `h4-int_redis_1`, constante `REDIS`): línea base, al congelar, a los 4 min 30 s (dentro de la ventana de 5 min), a los 5 min 30 s y tras reanudar, con el proxy en bypass y con AI-IP, el MCP y el login. Lo reanuda siempre, también si falla | ~6 min |

Los textos de prueba no llevan dígitos seguidos: las reglas de AI-IP los toman por teléfonos y los sustituyen.
