# llm-plugin · plugin `baas` para Claude Code

> **English summary**: Claude Code plugin and marketplace for **BaaS-AI-IP** (Brain-as-a-Service + AI Interception Proxy). It binds a project to an organization (`/baas-init`), shows its status (`/baas-status`), teaches the agent how to use the organization's MCP server (`baas-brain` skill), **synchronizes the organization's skills to the user's machine** as a Claude Code plugin invoked `/<org>:<skill>` (`/baas-sync`) and runs organization scripts on demand after verifying their SHA-256. Python 3.10+ standard library only; no business logic.

Este repositorio es a la vez el **marketplace** `baas-ai-ip` y el **plugin** `baas`. Conecta Claude Code con BaaS-AI-IP:

- **`/baas-init`**: guarda tu API key fuera del chat, hace el *handshake* con la webapp y vincula el proyecto a una de tus organizaciones (proxy de Anthropic + servidor MCP `baas`).
- **`/baas-status`**: usuario, organización del proyecto, roles, kill switch (global, bloqueo, pausa), AI-IP y si el proxy y el MCP funcionan en esta sesión.
- **Skill `baas-brain`**: enseña al agente a usar las tools `baas_*` (skills, scripts y conocimiento de la organización) y a **detenerse** ante `AUTHORIZATION_DENIED` o `KILL_SWITCH_ACTIVE`.
- **`/baas-sync`**: copia en tu equipo las skills de la organización que tus roles autorizan, con sus scripts, plantillas y ficheros de contexto, como un plugin de Claude Code que se invoca **`/<organización>:<skill>`**. También se sincroniza solo, en segundo plano, al iniciar y al cerrar la sesión. Quitar un rol, retirar una skill o activar el kill switch la elimina del equipo en la siguiente sincronización ([Skills de la organización](#skills-de-la-organización-sincronización)).
- **Ejecutor de scripts**: respaldo bajo demanda para una skill no sincronizada: descarga su paquete completo, comprueba el SHA-256 de cada fichero, ejecuta el script desde un directorio temporal privado y lo borra siempre.

## Requisitos

- Claude Code (CLI o extensión de IDE). Probado con 2.1.325.
- **Python 3.10 o superior** como `python3` (solo biblioteca estándar: no hay que instalar nada).
- Una cuenta de BaaS-AI-IP con una **API key** (webapp → *Mis API keys*) y la URL de la webapp (`https://app.<dominio>`).
- Linux o macOS. Windows no está probado.

## Instalación (menos de 2 minutos)

1. En Claude Code, añade el marketplace e instala el plugin:

   ```text
   /plugin marketplace add baas-ai-ip/llm-plugin
   /plugin install baas@baas-ai-ip
   ```

   (Desde Claude Code 2.1.275 basta con `/plugin install baas --marketplace baas-ai-ip/llm-plugin`.)

2. Abre Claude Code en tu proyecto y lanza **`/baas-init`**. La primera vez te dará un comando para guardar la clave **en una terminal aparte** (la pide sin mostrarla):

   ```bash
   python3 "<ruta del plugin>/scripts/baas.py" set-key --app-url https://app.<dominio>
   ```

   También puedes pasarla por tubería desde tu gestor de contraseñas (`… | python3 …/baas.py set-key --stdin`) o escribir `BAAS_API_KEY=…` en `~/baas-ai-ip/.env`. **No pegues nunca la clave en el chat**: quedaría en el historial y se enviaría al modelo. Si lo haces, revócala en la webapp.

3. Vuelve a lanzar `/baas-init`. Si perteneces a varias organizaciones te preguntará cuál; con una, la elige sola.
4. **Reinicia Claude Code** en el proyecto y acepta la confianza en la carpeta. Comprueba con `/baas-status` y `/mcp` (servidor `baas` conectado).

Los comandos también responden con su nombre completo: `/baas:baas-init`, `/baas:baas-status`, `/baas:baas-sync`. Para **cambiar de organización**: `/baas-init <slug>`. Relanzar `/baas-init` es seguro (no duplica nada).

## Qué se guarda y dónde

| Dónde | Qué | ¿Versionado? |
| :--- | :--- | :--- |
| `~/baas-ai-ip/.env` (0600, directorio 0700) | `BAAS_API_KEY` y `BAAS_APP_URL` | No: está en tu `HOME` |
| `~/baas-ai-ip/bin/baas_mcp_headers.py` | *Helper* que da a Claude Code la cabecera del MCP leyendo `.env` | No |
| `~/baas-ai-ip/bin/baas_desktop_credentials.py` | Solo con Claude Desktop en modo gateway (`desktop-config`): da a Desktop la API key de Anthropic y la cabecera `X-Baas-Api-Key` leyendo `.env` | No |
| `<proyecto>/.claude/settings.local.json` (0600) | `ANTHROPIC_BASE_URL` (proxy de la organización) y `ANTHROPIC_CUSTOM_HEADERS` con `X-Baas-Api-Key`. Se conservan tus otros ajustes | **No**: el plugin lo añade a `.gitignore` y se niega a escribir si ya está versionado |
| `<proyecto>/.claude/baas.local.json` | Organización y URLs (sin secretos) | **No** (`.gitignore`) |
| `~/.claude.json` (ámbito *local* del proyecto) | Servidor MCP `baas` (URL y `headersHelper`, **sin** la clave) | No |
| `~/.claude/skills/<organización>/` (0700) | *(H5)* El plugin de usuario con las skills sincronizadas: `.claude-plugin/plugin.json`, `.baas-managed` y `skills/<skill>/…` | No: está en tu `HOME` |
| `~/.claude/settings.json` | *(H5)* Solo `enabledPlugins["<organización>@skills-dir"] = false` (el plugin queda apagado a nivel de usuario). Se conserva el resto del fichero y se escribe de forma atómica | No |
| `<proyecto>/.claude/settings.local.json` | *(H5)* Además del bloque `env`, `enabledPlugins["<organización>@skills-dir"] = true`: el plugin de la organización solo se activa en sus proyectos | **No** (mismas reglas que arriba) |
| `~/baas-ai-ip/sync.lock` · `sync.log` | *(H5)* Bloqueo de «un solo `sync` a la vez» y registro de los hooks (se recorta a 64 KiB) | No |

La API key solo se copia a `settings.local.json` porque Claude Code no admite otra forma de enviar una cabecera propia al proxy (detalles y alternativas en [docs/spike-s1.md](docs/spike-s1.md)). Nunca se escribe a nivel de usuario: en proyectos no vinculados, Claude Code habla con Anthropic sin la clave de BaaS.

## Skills de la organización (sincronización)

Una skill de la organización es un **paquete**: su `SKILL.md` más scripts (`.py`, `.sh`, `.js`…), plantillas, binarios y ficheros de contexto, en cualquier estructura de carpetas. `/baas-sync` los copia a tu equipo **como si los hubieras instalado**:

```bash
python3 "<ruta del plugin>/scripts/baas.py" sync [--project <dir>] [--dry-run] [--force]
```

```text
~/.claude/skills/<organización>/                Plugin de usuario `<organización>@skills-dir`
├── .claude-plugin/plugin.json                  name = slug de la organización · version = hash corto del conjunto
├── .baas-managed                               JSON: marca de «gestionado» y estado (ver abajo)
└── skills/<skill>/…                            los ficheros del paquete (sin `baas.yaml`)
```

La skill `facturacion` de la organización `ginxo` se invoca **`/ginxo:facturacion`**, con la carga progresiva y los ficheros hermanos (`${CLAUDE_SKILL_DIR}`) de una skill instalada. Tras un cambio, **ejecuta `/reload-plugins` o reinicia Claude Code** (el comando lo indica; un cambio hecho por un hook se ve en la sesión siguiente).

- **Qué se sincroniza**: las skills que `baas_list_skills` te muestra (tus roles) y, de forma transitiva, las de las que dependen (`depends_on.skills` de su `baas.yaml`) **si tienes acceso**; si no, avisa y no concede nada. El conocimiento (`knowledge/`) **no** se copia: sigue sirviéndose por el MCP con los roles de cada documento.
- **Activación por proyecto**: el plugin queda **apagado a nivel de usuario** y se **activa solo** en `.claude/settings.local.json` del proyecto vinculado (el mismo fichero local de `/baas-init`; se niega a escribir si está versionado y lo añade a `.gitignore`). Así las skills de una organización no aparecen en los proyectos de otra. Si cambias un proyecto de organización, `sync` apaga ahí el plugin de la anterior.
- **Integridad y confinamiento**: se verifica el `sha256` de cada fichero; se rechazan rutas con `..`, absolutas o con `\`; nunca se crean enlaces simbólicos (y los que haya en el sitio de una skill se sustituyen sin seguirlos). Cada skill se escribe en un directorio temporal junto al destino y se renombra: si algo falla, la versión anterior queda como estaba. El bit de ejecución se aplica **solo** a lo que la skill declara en `executable`. Los ficheros se crean con permisos 0600 (0700 los ejecutables y las carpetas).
- **Solo toca lo suyo**: únicamente escribe en `~/.claude/skills/<organización>/` (más `enabledPlugins` de los dos `settings`, `~/baas-ai-ip/sync.lock` y `sync.log`). Si ese directorio ya existe **sin** la marca `.baas-managed`, es de otro origen: aborta con aviso (código 13) sin tocar nada. Dentro de uno gestionado, todo lo que no esté autorizado desaparece, así que no edites esos ficheros.
- **Revocación**: se borra lo que ya no autoriza el servidor (rol quitado, skill retirada o inexistente). Con `KILL_SWITCH_ACTIVE` (plataforma detenida, organización bloqueada o pausada) se retiran **todas**; vuelven cuando se levanta. Perder el acceso a la organización (401, 404) es un error, no una revocación: no borra nada.
- **Automático y a prueba de fallos**: los *hooks* `SessionStart` (`startup`/`resume`) y `SessionEnd` lanzan la sincronización **en segundo plano** (`scripts/baas_sync_hook.py`: un proceso aparte, sin salida) y vuelven al instante. Esperan al MCP como máximo 5 s (`BAAS_SYNC_TIMEOUT`); con el MCP caído o lento **no tocan nada** y no retrasan ni bloquean el arranque. Solo actúan en proyectos vinculados. Un solo `sync` a la vez.
- **`/baas-status`** añade las skills **sincronizadas**, **desfasadas** (el servidor tiene otra versión), **revocadas o retiradas** (se borrarán en la siguiente sincronización) y **pendientes**, los avisos de la última sincronización y si el plugin está activo en el proyecto.

`.baas-managed` (JSON, 0600): `managed_by`, `format`, `org`, `server` (URL del MCP), `synced_at`, `version` (la del `plugin.json`), `skills` (por skill: `package_sha256`, `depends_on` y `files`, una lista de `{path, sha256, executable}`) y `warnings`.

Pruebas y desarrollo: la raíz de Claude se fija con `BAAS_CLAUDE_HOME` (o `--claude-home`; `--skills-root` mueve solo la carpeta de plugins); sin ellas se usa `CLAUDE_CONFIG_DIR` y, por último, `~/.claude`.

## Ejecutor de scripts

Respaldo para una skill **no sincronizada** (o un cliente sin sincronización). El agente lo usa así (lo explica la skill `baas-brain`):

```bash
python3 "<ruta del plugin>/scripts/baas.py" run-script <ruta del script> [--sha256 <hash>] [--cwd <dir>] -- <argumentos>
```

1. Descarga el paquete **completo** de la skill con `baas_get_skill_package` (`{skill, org?}`) y `baas_get_skill_files` (`{skill, paths?, org?}`), para que el script tenga a su lado sus plantillas, módulos y datos. `<ruta del script>` es `skills/<skill>/<ruta>` (la de `baas_list_skills`) o `<skill>/<script>`.
2. Verifica el SHA-256 de cada fichero (y el del script con `--sha256`). **Si algo no coincide, no se ejecuta** (código 9).
3. Lo escribe en un directorio temporal privado (0700; `<tmp>/<skill>/…`, con el bit de ejecución solo donde la skill lo declara) y ejecuta el script con el intérprete que corresponde: el de su `#!`; si no lo tiene, por extensión (`.py` con el mismo Python del plugin, `.sh` con bash, `.js` con `node`); si no, el único de `requires.interpreters` o, si figura en `executable`, directamente. `CLAUDE_SKILL_DIR` apunta a la skill, sin la API key en su entorno. `requires` solo avisa de lo que falte.
4. El **directorio de trabajo** es el del proceso (no el temporal ni el del proyecto), o el de `--cwd`.
5. Borra el directorio temporal siempre: al terminar, si falla o si se interrumpe (también con SIGTERM).

**Códigos de salida**: el del script, **íntegro** (un script que sale con 9 o 10 no se confunde con un error del plugin; si lo mata una señal, `128 + señal`). Un error del propio plugin sale con su código (9 hash, 10 error del MCP, 2 configuración…) **y** una línea `BAAS_ERROR=<código>` en stderr (`SCRIPT_HASH_MISMATCH`, `AUTHORIZATION_DENIED`, `KILL_SWITCH_ACTIVE`, `CONFIG_ERROR`…): esa línea es la que distingue un error del plugin del código de salida del script.

Límite aceptado: un script ya ejecutado no se puede «revocar» (riesgo S6); las skills sincronizadas sí se retiran en la siguiente sincronización.

## Codex (OpenAI)

El plugin `baas` es de Claude Code, pero **Codex CLI** también puede pasar por el proxy de OpenAI de tu organización con un perfil de usuario y `env_http_headers` (la clave de BaaS-AI-IP solo en el entorno de Codex, nunca en un fichero): ver [docs/codex.md](docs/codex.md).

## Claude Desktop (pestaña Code)

La pestaña Code de Claude Desktop **no aplica el proxy del proyecto**: con cuenta de claude.ai, Desktop arranca Claude Code con `ANTHROPIC_BASE_URL=https://api.anthropic.com` e ignora la de `.claude/settings.local.json`. Las peticiones irían directas a Anthropic, sin AI-IP ni trazas, y con la cabecera `X-Baas-Api-Key` (que sí se aplica).

**Guardia (fallo cerrado).** El plugin trae dos *hooks* (`hooks/hooks.json` → `scripts/baas_guard.py`): en un proyecto vinculado cuya sesión no pase por el proxy, `SessionStart` avisa y `UserPromptSubmit` **bloquea cada prompt** con la explicación. En proyectos no vinculados no hacen nada. Cubre también el CLI sin la confianza aceptada.

**Para trabajar en un proyecto BaaS**, elige:

1. **CLI o IDE** (recomendado): usan el proxy del proyecto y tu suscripción de claude.ai.
2. **Desktop en modo gateway** («configuración de inferencia de terceros»):
   - Es **global** a Desktop: todas sus sesiones pasan por el proxy de **una** organización.
   - Desktop **deja de usar la suscripción de claude.ai**: necesitas una API key de **Anthropic Console**, que el proxy reenvía a Anthropic.
   - El soporte de este modo en Desktop para Linux no está confirmado.

   ```bash
   python3 <plugin>/scripts/baas.py set-key --anthropic     # en una terminal: guarda ANTHROPIC_API_KEY en ~/baas-ai-ip/.env
   python3 <plugin>/scripts/baas.py desktop-config --project <proyecto>
   ```

   `desktop-config` instala `~/baas-ai-ip/bin/baas_desktop_credentials.py` e imprime (sin secretos) los valores para Developer → Configure Third-Party Inference: `inferenceProvider: gateway`, `inferenceGatewayBaseUrl` = proxy de la organización, `inferenceGatewayAuthScheme: x-api-key` y `inferenceCredentialHelper` = el *helper*, que devuelve `{"token": <API key de Anthropic>, "headers": {"X-Baas-Api-Key": <API key de BaaS>}}`. Con Desktop en ese modo, `/baas-status` debe decir «Proxy activo en esta sesión: sí» y la guardia deja pasar los prompts.

## Solución de problemas

| Síntoma | Causa y solución |
| :--- | :--- |
| `[BAAS_KEY_MISSING]` | No hay clave en `~/baas-ai-ip/.env`: usa `set-key` en una terminal |
| `[BAAS_APP_URL_MISSING]` | Lanza `/baas-init` indicando la URL (`--app-url https://app.<dominio>`) |
| `[BAAS_AUTH_ERROR]` / MCP 401 | Clave revocada o mal copiada: crea otra y guárdala con `set-key`; después `/baas-init` en cada proyecto |
| `[BAAS_NETWORK_ERROR]` | Sin red, URL incorrecta, servidor caído o límite de tasa del handshake |
| `[BAAS_PROJECT_UNSAFE]` | `.claude/settings.local.json` está versionado (`git rm --cached …`) o no es JSON válido |
| `/mcp` no muestra `baas` | Reinicia Claude Code. Si `/baas-init` avisó de que no encontró `claude`, ejecuta el comando que imprimió |
| `baas` conecta pero falla con 401 | No aceptaste la confianza en la carpeta: el `headersHelper` solo se ejecuta en carpetas de confianza |
| Prompt bloqueado / `/baas-status`: «esta sesión no pasa por el proxy» | Reinicia la sesión y acepta la confianza. En la **pestaña Code de Claude Desktop** ver [Claude Desktop](#claude-desktop-pestaña-code) |
| `KILL_SWITCH_ACTIVE` o 403 «Acceso bloqueado por BaaS-AI-IP» | La plataforma está detenida o tu organización bloqueada o pausada: habla con tu administrador |
| `[BAAS_SCRIPT_HASH_MISMATCH]` | Un fichero no coincide con su hash: no se ejecutó ni se instaló. Avisa al administrador de la organización |
| `[BAAS_SYNC_FOREIGN_DIR]` (13) | `~/.claude/skills/<organización>/` existe y no es de BaaS (no tiene `.baas-managed`): renómbralo o muévelo. No se tocó nada |
| `[BAAS_SYNC_INCOMPLETE]` (12) | Alguna skill no se pudo instalar (hash o ruta no válidos): el resto sí; la versión anterior de esa skill queda como estaba |
| La skill sincronizada no aparece como `/<organización>:<skill>` | Lanza `/baas-sync` y después `/reload-plugins` (o reinicia). `/baas-status` dice si el plugin está activo en el proyecto; los hooks no ven el cambio hasta la sesión siguiente |
| El hook no sincroniza | Mira `~/baas-ai-ip/sync.log`: con el MCP caído o lento el hook no hace nada a propósito. `/baas-sync` lo hace a mano |

## Desarrollo

```bash
uv sync                                   # pytest, ruff y pyyaml (solo desarrollo)
uv run pytest                             # tests (servidores falsos en puertos efímeros)
uv run --python 3.10 pytest               # mínimo soportado
uv run ruff check . && uv run ruff format --check .
uv run python tools/lint_skills.py        # lint de skills y manifiestos
claude plugin validate --strict .         # validación oficial (usa un HOME temporal)
python3 dev/fake_baas.py --port 8765      # BaaS falso (handshake + MCP, con skills como paquetes) para pruebas manuales
```

Las pruebas de `sync` usan `BAAS_CLAUDE_HOME` en un directorio temporal y nunca tocan tu `~/.claude`. `dev/e2e_h2.py` es la prueba de punta a punta contra la pila de desarrollo de `deploy` ([docs/e2e.md](docs/e2e.md); `dev/e2e_h3.py` y `dev/e2e_h4.py` son las de AI-IP y las de H4: el anonimizador apagado, eliminar una organización y la prueba canario de que los originales no llegan al disco de Redis) y `dev/spike_s13_codex.sh`, el spike de Codex CLI ([docs/spike-s13.md](docs/spike-s13.md)). `dev/spike_s1.py` reproduce las pruebas del spike S1 con el CLI real, sin red (ver [docs/spike-s1.md](docs/spike-s1.md)). CI: `.github/workflows/ci.yml`; al etiquetar `v*`, `release.yml` publica una *release* (sin despliegue). La versión vive en `.claude-plugin/plugin.json` y debe coincidir con la etiqueta.
