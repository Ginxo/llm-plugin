# llm-plugin · plugin `baas` para Claude Code

> **English summary**: Claude Code plugin and marketplace for **BaaS-AI-IP** (Brain-as-a-Service + AI Interception Proxy). It binds a project to an organization (`/baas-init`), shows its status (`/baas-status`), teaches the agent how to use the organization's MCP server (`baas-brain` skill) and runs organization scripts after verifying their SHA-256. Python 3.10+ standard library only; no business logic.

Este repositorio es a la vez el **marketplace** `baas-ai-ip` y el **plugin** `baas`. Conecta Claude Code con BaaS-AI-IP:

- **`/baas-init`**: guarda tu API key fuera del chat, hace el *handshake* con la webapp y vincula el proyecto a una de tus organizaciones (proxy de Anthropic + servidor MCP `baas`).
- **`/baas-status`**: usuario, organización del proyecto, roles, kill switch (global, bloqueo, pausa), AI-IP y si el proxy y el MCP funcionan en esta sesión.
- **Skill `baas-brain`**: enseña al agente a usar las tools `baas_*` (skills, scripts y conocimiento de la organización) y a **detenerse** ante `AUTHORIZATION_DENIED` o `KILL_SWITCH_ACTIVE`.
- **Ejecutor de scripts**: descarga un script del MCP, comprueba su SHA-256, lo ejecuta desde un directorio temporal privado y lo borra siempre.

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

Los comandos también responden con su nombre completo: `/baas:baas-init`, `/baas:baas-status`. Para **cambiar de organización**: `/baas-init <slug>`. Relanzar `/baas-init` es seguro (no duplica nada).

## Qué se guarda y dónde

| Dónde | Qué | ¿Versionado? |
| :--- | :--- | :--- |
| `~/baas-ai-ip/.env` (0600, directorio 0700) | `BAAS_API_KEY` y `BAAS_APP_URL` | No: está en tu `HOME` |
| `~/baas-ai-ip/bin/baas_mcp_headers.py` | *Helper* que da a Claude Code la cabecera del MCP leyendo `.env` | No |
| `<proyecto>/.claude/settings.local.json` (0600) | `ANTHROPIC_BASE_URL` (proxy de la organización) y `ANTHROPIC_CUSTOM_HEADERS` con `X-Baas-Api-Key`. Se conservan tus otros ajustes | **No**: el plugin lo añade a `.gitignore` y se niega a escribir si ya está versionado |
| `<proyecto>/.claude/baas.local.json` | Organización y URLs (sin secretos) | **No** (`.gitignore`) |
| `~/.claude.json` (ámbito *local* del proyecto) | Servidor MCP `baas` (URL y `headersHelper`, **sin** la clave) | No |

La API key solo se copia a `settings.local.json` porque Claude Code no admite otra forma de enviar una cabecera propia al proxy (detalles y alternativas en [docs/spike-s1.md](docs/spike-s1.md)). Nunca se escribe a nivel de usuario: en proyectos no vinculados, Claude Code habla con Anthropic sin la clave de BaaS.

## Ejecutor de scripts

Las skills de la organización pueden traer scripts. El agente los ejecuta así (lo explica la skill `baas-brain`):

```bash
python3 "<ruta del plugin>/scripts/baas.py" run-script <ruta del script> [--sha256 <hash>] -- <argumentos>
```

1. Pide el script con `baas_get_script` (`{"path": …}`) al MCP del proyecto.
2. Calcula su SHA-256 y lo compara con el que declara el MCP (y con `--sha256` si se pasa). **Si no coincide, no se ejecuta** (código 9).
3. Lo escribe en un directorio temporal privado (0700) y lo ejecuta (`.py` con el mismo Python; `.sh` con bash) en el directorio del proyecto, sin la API key en su entorno.
4. Borra el directorio temporal siempre: al terminar, si falla o si se interrumpe.

Límite aceptado: un script ya ejecutado no se puede «revocar» (riesgo S6).

## Codex (OpenAI)

El plugin `baas` es de Claude Code, pero **Codex CLI** también puede pasar por el proxy de OpenAI de tu organización con un perfil de usuario y `env_http_headers` (la clave de BaaS-AI-IP solo en el entorno de Codex, nunca en un fichero): ver [docs/codex.md](docs/codex.md).

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
| `/baas-status`: «esta sesión no pasa por el proxy» | Reinicia la sesión y acepta la confianza. En la **pestaña Code de Claude Desktop** el proxy por proyecto no se aplica (Desktop fija su propia Base URL): usa el CLI o el IDE |
| `KILL_SWITCH_ACTIVE` o 403 «Acceso bloqueado por BaaS-AI-IP» | La plataforma está detenida o tu organización bloqueada o pausada: habla con tu administrador |
| `[BAAS_SCRIPT_HASH_MISMATCH]` | El script no coincide con su hash: no se ejecutó. Avisa al administrador de la organización |

## Desarrollo

```bash
uv sync                                   # pytest, ruff y pyyaml (solo desarrollo)
uv run pytest                             # tests (servidores falsos en puertos efímeros)
uv run --python 3.10 pytest               # mínimo soportado
uv run ruff check . && uv run ruff format --check .
uv run python tools/lint_skills.py        # lint de skills y manifiestos
claude plugin validate --strict .         # validación oficial (usa un HOME temporal)
python3 dev/fake_baas.py --port 8765      # BaaS falso (handshake + MCP) para pruebas manuales
```

`dev/e2e_h2.py` es la prueba de punta a punta contra la pila de desarrollo de `deploy` ([docs/e2e.md](docs/e2e.md); `dev/e2e_h3.py` y `dev/e2e_h4.py` son las de AI-IP y las de H4: el anonimizador apagado y eliminar una organización) y `dev/spike_s13_codex.sh`, el spike de Codex CLI ([docs/spike-s13.md](docs/spike-s13.md)). `dev/spike_s1.py` reproduce las pruebas del spike S1 con el CLI real, sin red (ver [docs/spike-s1.md](docs/spike-s1.md)). CI: `.github/workflows/ci.yml`; al etiquetar `v*`, `release.yml` publica una *release* (sin despliegue). La versión vive en `.claude-plugin/plugin.json` y debe coincidir con la etiqueta.
