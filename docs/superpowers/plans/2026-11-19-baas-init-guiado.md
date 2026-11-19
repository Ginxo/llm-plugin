# /baas-init guiado Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que una persona no técnica vincule su proyecto a BaaS-AI-IP con `/baas-init`, aprobando en el navegador, sin crear ni copiar una API key y sin ejecutar nada a mano.

**Architecture:** Flujo de autorización de dispositivo. El plugin pide un código a la webapp (`device/start`), el usuario lo aprueba en una página nueva con su sesión y el plugin recoge una API key PERSONAL recién creada (`device/poll`). `baas.py init` se vuelve una máquina de estados con códigos de salida nuevos que la skill traduce a mensajes llanos; el guard deja pasar `/baas-init` y `/baas-status`.

**Tech Stack:** FastAPI + SQLAlchemy + Redis (webapp/backend, pytest); React + PatternFly + vitest (webapp/frontend); Python estándar sin dependencias (llm-plugin, unittest/pytest).

**Spec:** `llm-plugin/docs/superpowers/specs/2026-11-19-baas-init-guiado-design.md`

## Global Constraints

- Máxima: todo mensaje al usuario dice «qué ha pasado y qué hacer ahora», sin rutas de ficheros, códigos de salida, comandos ni jerga (proxy, handshake, hook, API key en bruto).
- La clave nunca pasa por el chat, ni por la salida de ningún comando, ni por logs. Ficheros con la clave: permisos 0600 y fuera de git.
- Código de dispositivo: 8 caracteres del alfabeto `ABCDEFGHJKMNPQRSTUVWXYZ23456789` (sin 0/O/1/I/L), mostrado `XXXX-XXXX`; caduca a **600 s**; un solo uso; intervalo de sondeo **3 s**.
- Clave resultante: PERSONAL, etiqueta `Claude Code · <client_name>`, creada con `services.api_keys.create_personal`.
- El plugin solo usa la biblioteca estándar (`baas_plugin/`), y `baas_guard.py` sigue autónomo.
- Compatibilidad: `set-key`, las claves existentes y `init --org/--app-url/--project` siguen funcionando.
- Textos en español; la pantalla de aprobación pasa por `i18n`.

## Contrato entre webapp y plugin (fijado aquí; ambas partes lo usan tal cual)

Públicos (sin sesión, sin CSRF), prefijo `/api/v1/plugin`:

- `POST /device/start` body `{"client_name": str (1–100)}` → `200 {"device_code": str, "user_code": "KQRT-7MWB", "verification_url": "<public_app_url>/connect?code=KQRT-7MWB", "expires_in": 600, "interval": 3}`. Límite por IP.
- `POST /device/poll` body `{"device_code": str}` → `200 {"status": "pending"}` | `200 {"status": "denied"}` | `200 {"status": "expired"}` | `200 {"status": "approved", "api_key": str, "email": str}` (solo la primera vez; las siguientes `expired`). Límite por IP y por código.

Con sesión y CSRF (`X-Requested-With: baas-webapp`), prefijo `/api/v1/account`:

- `GET /device/{user_code}` → `200 {"user_code", "client_name", "expires_at"}` o `404 NOT_FOUND`.
- `POST /device/{user_code}/approve` → `200 {"ok": true}`; `404` si no existe o caducó; `409 ALREADY_DECIDED`.
- `POST /device/{user_code}/deny` → `200 {"ok": true}`.

La clave se crea **en el primer `poll` aprobado** (no se guarda en Redis): `approve` solo registra `user_id`.

Exit codes nuevos de `baas.py init` (además de los existentes): `20` aprobación pendiente (hay enlace), `21` sigue esperando (reintentar `init --wait`), `22` el usuario canceló, `23` el código caducó. Salida con marca `[BAAS_APPROVAL_PENDING]`, `[BAAS_APPROVAL_WAITING]`, `[BAAS_APPROVAL_DENIED]`, `[BAAS_APPROVAL_EXPIRED]`.

## Review Focus

- Dos terminales/usuarios con el mismo código: solo el primer `poll` aprobado recibe la clave; el segundo ve `expired`.
- Aprobar un código ya aprobado, denegado o caducado: `409`/`404`, nunca una segunda clave.
- Un usuario aprueba un `user_code` ajeno adivinado: la página muestra `client_name` y debe exigir pulsar «Aprobar»; los intentos fallidos se limitan por usuario.
- `init` interrumpido entre `begin` y `--wait` (Claude cerrado): el estado pendiente caduca y `init` empieza de cero sin error.
- La URL equivocada no se guarda: solo se persiste tras contactar con éxito.
- `.claude/settings.local.json` o `baas.local.json` con JSON inválido: copia `.bak` y regeneración con aviso, no excepción.
- Mensajes de error: ninguno contiene `~/`, `.env`, `baas.py`, `ANTHROPIC_BASE_URL` ni un código de salida.

---

### Task 1: Webapp backend — flujo de dispositivo

Repositorio: `webapp/backend`.

**Files:**
- Create: `src/baas_webapp/services/device_flow.py`
- Modify: `src/baas_webapp/api/routes/plugin.py` (rutas públicas), `src/baas_webapp/api/routes/account.py` (rutas con sesión), `src/baas_webapp/config.py` (ajustes), `src/baas_webapp/services/audit.py` o donde estén las acciones válidas (acciones nuevas)
- Test: `tests/test_device_flow.py`

**Interfaces:**
- Produces (servicio): `async def start(redis, *, client_name: str, ip: str | None, public_app_url: str) -> dict` (el cuerpo de `start`); `async def lookup(redis, user_code: str) -> dict | None`; `async def decide(redis, user_code: str, *, user_id: uuid.UUID, approve: bool) -> None` (lanza `ApiError(404, "NOT_FOUND")` o `ApiError(409, "ALREADY_DECIDED")`); `async def consume(redis, device_code: str) -> tuple[str, uuid.UUID | None]` devolviendo `("pending"|"denied"|"expired"|"approved", user_id)` y borrando el estado al devolver `approved` o `denied`.
- Produces (ajustes): `device_code_ttl_seconds: int = 600`, `device_poll_interval_seconds: int = 3`, `rate_limit_device_start_per_ip: int = 10`, `rate_limit_device_poll_per_ip: int = 120`, `rate_limit_device_decide_per_user: int = 20` en `Settings`.
- Auditoría: acciones `plugin.device.approved` y `plugin.device.denied` (metadata: `client_name`, `ip`), registradas en `decide`; `api_key.created` ya la registra `create_personal`.

Estado en Redis: hash `baas:device:{sha256(device_code)}` con campos `user_code`, `client_name`, `status` (`pending|approved|denied`), `user_id`; índice `baas:device-user:{user_code}` → hash del código; ambos con TTL `device_code_ttl_seconds`.

- [ ] **Step 1: Write the failing tests** en `tests/test_device_flow.py` (patrones de `test_keys_internal_jobs.py`: fixtures `client`, `seeded`, `db`, cabeceras `H`):
  - `test_start_returns_codes_and_url`: `POST /device/start` devuelve 8 caracteres `XXXX-XXXX` del alfabeto acordado, `expires_in == 600`, `interval == 3`, `verification_url` termina en `/connect?code=<user_code>`.
  - `test_poll_pending_then_approved_returns_key_once`: tras `approve` por el usuario `ANALISTA`, el primer `poll` devuelve `status=approved` con `api_key` que cumple `crypto.looks_like_api_key` y `email == ANALISTA`; el handshake con esa clave da 200 y la etiqueta en «Mis API keys» es `Claude Code · <client_name>`; el segundo `poll` devuelve `expired`.
  - `test_deny_returns_denied`; `test_unknown_device_code_is_expired`.
  - `test_approve_twice_is_409` y `test_approve_unknown_is_404`.
  - `test_expired_code_is_404_for_lookup` (con `frozen_redis` o TTL a 1 s).
  - `test_approve_requires_session_and_csrf` (401 sin sesión, 403 sin cabecera).
  - `test_device_start_rate_limited` (la 11.ª petición de la misma IP da 429).
  - `test_audit_records_device_approval` (aparece `plugin.device.approved` sin ningún secreto en la metadata).

- [ ] **Step 2: Run to verify they fail**
  Run: `cd webapp/backend && uv run pytest tests/test_device_flow.py -q`
  Expected: FAIL (rutas inexistentes, 404).

- [ ] **Step 3: Implement `services/device_flow.py`** con las firmas de Interfaces. Genera `user_code` con `secrets.choice` sobre el alfabeto fijado y `device_code` con `crypto.generate_token()`; guarda con `hash_token`. `consume` usa `GETDEL`-semántica (Lua o `WATCH`) para que solo un llamador reciba `approved`.

- [ ] **Step 4: Implement las rutas.** Públicas en `plugin.py` (límites con `ratelimit.check_many`, patrón del handshake; el `poll` aprobado crea la clave con `api_keys.create_personal(session, user, f"Claude Code · {client_name}", ip=info.ip)` dentro de `transaction(ctx)` y llama a `projection.write_api_key` como `create_key` en `account.py`, devolviendo `secret`). Con sesión en `account.py` (`Current`, límite `rate_limit_device_decide_per_user`). Las dos rutas con sesión van bajo el router ya protegido por CSRF; las públicas, sin `dependencies=csrf`: comprobar en `api/app.py` que `plugin.router` se incluye sin CSRF.

- [ ] **Step 5: Add settings y acciones de auditoría** (ver Interfaces) y registrar las acciones nuevas donde el backend valida/lista acciones.

- [ ] **Step 6: Run tests**
  Run: `uv run pytest tests/test_device_flow.py tests/test_keys_internal_jobs.py -q && uv run ruff check . && uv run mypy src`
  Expected: PASS.

- [ ] **Step 7: Commit** `feat(webapp): flujo de autorización de dispositivo para el plugin`

---

### Task 2: Webapp frontend — página «Conectar Claude Code»

Repositorio: `webapp/frontend`. Depende de Task 1 (contrato).

**Files:**
- Create: `src/pages/ConnectPage.tsx`, `src/__tests__/connect.test.tsx`
- Modify: `src/App.tsx` (ruta `/connect`, dentro de `RequireSession`, con redirección al login que **conserve** `?code=`), `src/api/types.ts`, `src/i18n/resources.ts` (bloque `connect`), `src/i18n/auditActions.json` (+ `plugin.device.approved`, `plugin.device.denied` y sus textos en `resources.ts`)

**Interfaces:**
- Consumes: endpoints `GET/POST /api/v1/account/device/{user_code}[/approve|/deny]` del contrato.
- Produces: `ConnectPage` en `/connect?code=XXXX-XXXX`.

Comportamiento (copiar la estructura de `ApiKeysPage`): lee `code` de la URL; si falta, campo para escribirlo; `GET` y muestra «**Claude Code · <client_name>** quiere conectarse a tu cuenta», el código en grande y los botones **Aprobar** y **Cancelar**; tras aprobar, «Listo. Vuelve a Claude Code: ya puedes cerrar esta pestaña»; tras cancelar, «Conexión cancelada»; `404` → «Este enlace ha caducado. Vuelve a Claude Code y lanza /baas-init de nuevo»; `409` → «Esta conexión ya se resolvió».

- [ ] **Step 1: Write the failing tests** (`connect.test.tsx`, estilo de `keys.test.tsx`): renderiza el nombre y el código; «Aprobar» llama a `POST …/approve` y muestra «Listo»; «Cancelar» llama a `…/deny`; `404` muestra el mensaje de caducado; sin `code` muestra el campo; el botón Aprobar es la acción primaria y está enfocado.
- [ ] **Step 2: Run** `cd webapp/frontend && npm test -- connect` → FAIL.
- [ ] **Step 3: Implement** `ConnectPage` y la ruta; hace que `RequireSession` redirija a `/login` con `state.from` incluyendo la query y que el login vuelva a ella (comprobar `LoginPage` y ajustar si descarta la query).
- [ ] **Step 4: Run** `npm run typecheck && npm run lint && npm test` → PASS.
- [ ] **Step 5: Commit** `feat(webapp): página de aprobación para conectar Claude Code`

---

### Task 3: Plugin — cliente de dispositivo y estado pendiente

Repositorio: `llm-plugin`. Puede hacerse en paralelo a Task 1–2 usando el contrato.

**Files:**
- Create: `scripts/baas_plugin/device.py`, `scripts/baas_plugin/defaults.py`, `tests/test_device.py`
- Modify: `scripts/baas_plugin/errors.py`, `scripts/baas_plugin/envfile.py`

**Interfaces:**
- Produces: `@dataclass DeviceStart(device_code: str, user_code: str, verification_url: str, expires_in: int, interval: int)`; `start(app_url: str, client_name: str, *, timeout: float = 15) -> DeviceStart` (lanza `NetworkError` si no hay servidor o la respuesta no es del contrato); `@dataclass DevicePoll(status: str, api_key: str | None, email: str | None)`; `poll(app_url: str, device_code: str, *, timeout: float = 15) -> DevicePoll`; `wait(app_url, device_code, *, interval: int, max_seconds: int, sleep=time.sleep, now=time.monotonic) -> DevicePoll` (sondea hasta salir de `pending` o agotar `max_seconds`, devolviendo `pending`).
- Produces (estado pendiente): `PendingFlow(app_url, device_code, user_code, verification_url, expires_at: float)`; `save_pending(paths: BaasPaths, flow: PendingFlow) -> None` (fichero `pending.json` junto a `.env`, 0600, escritura atómica con `atomic_write`); `load_pending(paths, *, now: float) -> PendingFlow | None` (devuelve `None` y borra el fichero si caducó o está dañado); `clear_pending(paths) -> None`.
- Produces: `DEFAULT_APP_URL: str` en `defaults.py` (cadena vacía en el repositorio; se fija al publicar) y `resolve_app_url(explicit: str | None, env: dict, saved: str | None) -> str | None` con orden explícito > `BAAS_APP_URL` del entorno > guardada > `DEFAULT_APP_URL` > `None`.
- Errores nuevos en `errors.py`: `ApprovalPendingError`, `ApprovalWaitingError`, `ApprovalDeniedError`, `ApprovalExpiredError` (todas `BaasError`, mensaje en lenguaje llano).

- [ ] **Step 1: Write the failing tests** (`tests/test_device.py`, servidor HTTP falso en un hilo como en `test_handshake.py`): `start` parsea el contrato; `start` contra puerto cerrado → `NetworkError`; `poll` mapea los cuatro estados y trata `429` como `pending` (respeta `Retry-After` si llega); `wait` devuelve `approved` en el segundo sondeo y `pending` al agotar `max_seconds` (con `sleep`/`now` inyectados, sin dormir); `save_pending`/`load_pending` con permisos `0o600`, caducidad devuelve `None` y borra el fichero, JSON dañado devuelve `None`; `resolve_app_url` respeta el orden.
- [ ] **Step 2: Run** `cd llm-plugin && uv run pytest tests/test_device.py -q` → FAIL.
- [ ] **Step 3: Implement** según Interfaces, reutilizando `httpclient.post_json` y `envfile.atomic_write`.
- [ ] **Step 4: Run** `uv run pytest tests/test_device.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(plugin): cliente del flujo de dispositivo y estado pendiente`

---

### Task 4: Plugin — `init` como máquina de estados

**Files:**
- Modify: `scripts/baas_plugin/cli.py` (`cmd_init`, argumentos, docstring de códigos de salida, mapeo error → código), `scripts/baas_plugin/project.py` (reparación), `scripts/baas_plugin/handshake.py` si hace falta
- Test: `tests/test_cli_init_status.py` (ampliar), `tests/test_project.py` (ampliar)

**Interfaces:**
- Consumes: `device.start/poll/wait`, `save_pending/load_pending/clear_pending`, `resolve_app_url`, errores `Approval*`.
- Produces: `baas.py init [--wait] [--org S] [--app-url U] [--project P]`; salida y códigos según el contrato. `project.repair_json_file(path: Path) -> bool` (si el JSON es inválido, mueve a `<nombre>.bak`, devuelve `True`).

Lógica de `init` (todas las ramas, cada una con su test):
1. Resolver URL (`resolve_app_url`); si es `None` → `MissingAppUrlError` (`[BAAS_APP_URL_MISSING]`, código 2).
2. Si hay clave en `.env` y no es `--wait`: `handshake`; con éxito sigue la vinculación normal (compatibilidad); con `AuthError` (clave revocada/ajena) **no es un fallo**: `clear` de la clave vieja y comienza el flujo nuevo, imprimiendo «Tu conexión anterior ya no es válida; vamos a renovarla».
3. Sin clave (o paso 2 fallido): `device.start`; si `NetworkError` → código 6 `[BAAS_NETWORK_ERROR]` **sin guardar la URL**; si funciona, guarda la URL, escribe `pending.json`, intenta `webbrowser.open(verification_url)` (cualquier fallo ignorado), imprime el enlace y el código y sale con 20 `[BAAS_APPROVAL_PENDING]`.
4. `--wait`: `load_pending`; sin pendiente → vuelve al paso 3. `device.wait(max_seconds=90)`; `approved` → `update_env(BAAS_API_KEY, BAAS_APP_URL)`, `clear_pending`, y continúa con la vinculación normal; `pending` → 21; `denied` → 22 y `clear_pending`; `expired` → 23 y `clear_pending`.
5. Varias organizaciones sigue siendo código 3; sin organizaciones, 4.
6. Antes de escribir `settings.local.json` y `baas.local.json`, `repair_json_file` en ambos: si repara, añade la línea «He reparado una configuración dañada (copia guardada)».
7. `client_name` = `<nombre de la carpeta del proyecto> en <hostname>`.

- [ ] **Step 1: Write the failing tests** (parchear `device` y `handshake`, no red): `test_init_without_key_starts_device_flow_exit_20` (verifica `pending.json` 0600 y que la salida no contiene la clave ni `~/`); `test_init_revoked_key_restarts_flow_with_friendly_notice`; `test_init_wait_pending_exit_21`; `test_init_wait_approved_writes_key_and_binds` (afirma `.env` 0600 con clave nueva y `settings.local.json` con la clave y la URL del proxy); `test_init_wait_denied_exit_22`; `test_init_wait_expired_exit_23`; `test_init_unreachable_url_not_saved_exit_6`; `test_init_existing_valid_key_keeps_old_behaviour`; `test_init_repairs_corrupt_settings_local`; `test_init_expired_pending_starts_over`.
- [ ] **Step 2: Run** `uv run pytest tests/test_cli_init_status.py tests/test_project.py -q` → FAIL.
- [ ] **Step 3: Implement** la lógica; mapear las excepciones nuevas a los códigos 20–23 en el gestor de errores de `main`; actualizar el docstring de códigos.
- [ ] **Step 4: Run** `uv run pytest -q` (toda la suite del plugin) → PASS.
- [ ] **Step 5: Commit** `feat(plugin): /baas-init guiado con aprobación en el navegador`

---

### Task 5: Plugin — guard y mensajes en lenguaje llano

**Files:**
- Modify: `scripts/baas_guard.py`
- Test: `tests/test_desktop_guard.py` (ampliar)

**Interfaces:**
- Produces: `ALLOWED_PROMPT = re.compile(r"^\s*/(?:baas:)?baas-(?:init|status)(?:\s|$)")`; `is_allowed_prompt(data: dict) -> bool`. En `main`, con `event == "prompt"` y `is_allowed_prompt(_hook_input())` no se bloquea.
- Textos de `diagnose` sin `ANTHROPIC_BASE_URL`, rutas ni comandos: caso general «Este proyecto está conectado a «<org>», pero esta sesión aún no está protegida. Cierra Claude Code, vuelve a abrirlo en esta carpeta y acepta la confianza. Si sigue igual, escribe /baas-init»; caso Desktop «Estás usando la pestaña Code de Claude Desktop, que no admite la protección de BaaS-AI-IP. Usa Claude Code en el terminal o en tu editor».

- [ ] **Step 1: Write the failing tests:** `/baas-init` y `/baas-init demo` y `/baas:baas-status` pasan (sin salida); `hola` se bloquea; `/baas-initx` se bloquea; ningún texto de `diagnose` contiene `ANTHROPIC_BASE_URL`, `.claude/`, `baas.py` ni `python3`; el aviso de «revoca tu API key» de Desktop sigue presente reescrito sin jerga.
- [ ] **Step 2: Run** `uv run pytest tests/test_desktop_guard.py -q` → FAIL.
- [ ] **Step 3: Implement** el cambio en `main` y los textos (el guard sigue sin importar nada del plugin).
- [ ] **Step 4: Run** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `fix(plugin): el guard deja pasar /baas-init y habla en llano`

---

### Task 6: Plugin — skills y documentación

**Files:**
- Modify: `skills/baas-init/SKILL.md`, `skills/baas-status/SKILL.md`, `README.md`, `docs/e2e.md`
- Test: `tests/test_lint_skills.py` (debe seguir pasando; ampliar si comprueba códigos)

**Interfaces:**
- Consumes: códigos y marcas de salida del contrato (2, 3, 4, 5→ya no se muestra, 6, 7, 8, 20–23).
- Produces: `allowed-tools: Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/baas.py" init *)` (sin cambios de forma).

Contenido de la skill `baas-init` (tabla salida → acción, en el mismo formato que la actual):
- `0`: resumir organización en una frase y decir «Cierra y vuelve a abrir Claude Code en esta carpeta».
- `20`: «Abre este enlace para conectar este proyecto: <enlace>. El código es <código>. Te he abierto el navegador si he podido.» y a continuación ejecutar `init --wait` sin esperar respuesta.
- `21`: repetir `init --wait` (hasta 4 veces; después «Sigo esperando; avísame cuando hayas aprobado» y esperar mensaje).
- `22`/`23`: ofrecer «¿Genero otro enlace?» y relanzar `init`.
- `6`: «No encuentro el servidor en <url>». Preguntar con `AskUserQuestion` la dirección correcta (precargar la actual) y repetir con `--app-url`.
- `2 [BAAS_APP_URL_MISSING]`: preguntar la dirección de BaaS-AI-IP de su organización y repetir.
- `3`: elegir organización con `AskUserQuestion` y repetir con `--org`.
- `4`: «Tu cuenta aún no tiene organización; pide acceso a un administrador».
- `7`: explicar en llano y, **solo si el usuario lo acepta**, ejecutar `git rm --cached` de los ficheros afectados (la skill amplía `allowed-tools` solo para esos ficheros) — decisión del plan: la reparación de ficheros versionados requiere consentimiento porque toca el índice de git del usuario.
- Eliminar de la skill toda referencia a `set-key` como camino normal; mantenerlo solo como nota para administradores en el README.

- [ ] **Step 1: Write/extend the failing test** en `test_lint_skills.py`: la skill `baas-init` menciona los códigos 20, 21, 22 y 23 y no contiene `set-key`; ningún texto entre comillas que se muestre al usuario contiene `~/baas-ai-ip` ni `.env`.
- [ ] **Step 2: Run** `uv run pytest tests/test_lint_skills.py -q` → FAIL.
- [ ] **Step 3: Rewrite** las skills y el README (sección «Primer uso» de tres pasos: instalar, `/baas-init`, aprobar) y documentar `DEFAULT_APP_URL` y `BAAS_APP_URL`.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `docs(plugin): skill /baas-init guiada y README de primer uso`

---

### Task 7: e2e y verificación en la pila real

**Files:**
- Modify: `dev/fake_h4_stack.py` (endpoints `device/start` y `device/poll` simulados, con modo «aprobar tras N sondeos», «denegar», «caducar», «clave revocada»), `dev/e2e_h4.py` o un nuevo `dev/e2e_connect.py` según el patrón existente
- Test: `tests/test_e2e_connect.py` (patrón de `tests/test_e2e_h4.py`)

**Interfaces:**
- Consumes: la CLI de Task 4 y el contrato.

- [ ] **Step 1: Write the failing e2e** `tests/test_e2e_connect.py` sobre la pila simulada: recorrido feliz (`init` → 20 → «aprobar» → `init --wait` → 0, `.env` 0600, `settings.local.json` correcto); clave revocada que se recupera sola; URL inexistente → 6 y luego URL correcta → 20; código caducado → 23 y reinicio; denegado → 22; sesión sin proxy con `/baas-init` pasando por el guard y otro prompt bloqueado.
- [ ] **Step 2: Run** `uv run pytest tests/test_e2e_connect.py -q` → FAIL.
- [ ] **Step 3: Implement** lo mínimo en la pila simulada.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS.
- [ ] **Step 5: Verificación manual en la pila de desarrollo** (puerto 8080, `deploy/scripts/dev-up.sh`): sin tocar `~/baas-ai-ip/.env` a mano, en un proyecto limpio abrir Claude Code, `/baas-init`, aprobar en `http://app.localhost:8080/connect` como `orgadmin@demo.test`, reiniciar y comprobar `/baas-status`. Repetir con la clave revocada desde «Mis API keys» y con una URL errónea. Anotar el resultado en `docs/e2e.md`.
- [ ] **Step 6: Commit** `test(plugin): e2e del /baas-init guiado`

---

## Self-review

- **Cobertura de la spec:** recorrido (T1–T4, T6); fallos y salidas — URL inexistente/ausente (T4, T6), clave inválida (T4), caducado/rechazado (T4, T6), varias/ninguna organización (T4, T6), sin red (T4), límite de tasa (T1; el plugin reintenta vía `wait` — ver nota abajo), sesión sin proxy (T5), ficheros dañados/versionados (T4, T6), ya conectado (T4, compatibilidad); seguridad (T1: clave creada en el primer `poll`, auditoría; T4: sin secretos en salida); guard (T5); verificación (T7).
- **Consistencia de tipos:** `DeviceStart`, `DevicePoll`, `PendingFlow` y los códigos 20–23 se usan con los mismos nombres en T3, T4, T6 y T7.
- **Proporción:** plan más corto que el código que describe; no hay cuerpos de funciones.
