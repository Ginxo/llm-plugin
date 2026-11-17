# Spike S1 · Inyección de la identidad en Claude Code

> **Estado**: cerrado con evidencia local (2026-11-17, Claude Code **2.1.325**). Falta la prueba manual con el stack real ([§6](#6-prueba-manual-con-el-stack-real)).
> **Riesgo** (fase-1-mvp §8, S1): que la API key de BaaS llegue a Anthropic en proyectos que no son BaaS o acabe en ficheros versionables. Decisiones D13 y D48; contrato §10.

## 1. Conclusiones

| Pregunta | Respuesta | Evidencia |
| :--- | :--- | :--- |
| ¿Dónde se configuran por proyecto la Base URL y `X-Baas-Api-Key`? | En el bloque `env` de `<proyecto>/.claude/settings.local.json`: `ANTHROPIC_BASE_URL` y `ANTHROPIC_CUSTOM_HEADERS="X-Baas-Api-Key: <clave>"`. Solo afecta a ese proyecto | Docs [settings], [env-vars]; prueba 3 |
| ¿Se puede referenciar la clave de `~/baas-ai-ip/.env` en vez de copiarla ahí? | **No para el proxy**: Claude Code **no expande** `${VAR}` en el bloque `env` (llega el texto literal `${BAAS_API_KEY}`) y no existe un *helper* de cabeceras para las peticiones al modelo (`apiKeyHelper` sustituye la credencial del **proveedor**, `X-Api-Key` y `Authorization`, que deben llegar intactas). La copia en `settings.local.json` es imprescindible | Prueba 7; docs [settings-reference#apikeyhelper] |
| ¿Y para el MCP? | **Sí**: servidor `baas` en ámbito **local** (`~/.claude.json`, por proyecto) con `headersHelper`, un script que lee `~/baas-ai-ip/.env` en cada conexión e imprime `{"Authorization": "Bearer …"}`. La clave **no** se copia a la configuración de Claude Code | Pruebas 1 y 2; docs [mcp#headershelper] |
| ¿Por qué no declara el MCP el propio plugin? | Claude Code renombra los servidores de plugins a `plugin:baas:<nombre>`; los prompts serían `/mcp__plugin_baas_baas__<skill>` y no `/mcp__baas__<skill>` (contrato §10) | Docs [mcp] (*plugin-provided servers*) |
| ¿Qué pasa con el inicio de sesión OAuth de Claude Code? | Con `ANTHROPIC_BASE_URL` apuntando al proxy, Claude Code sigue usando la suscripción y envía al proxy `Authorization: Bearer <token OAuth>` y `anthropic-beta: …oauth-2025-04-20…` junto a `X-Baas-Api-Key`. El proxy debe reenviarlos intactos (contrato §3) y quitar `X-Baas-*`. Con API key de Console, llega `x-api-key` | Prueba 4; docs [llm-gateway] (*Subscriptions and gateways*) |
| ¿Funciona la pestaña Code de Claude Desktop? | **No para el proxy**: en sesiones locales con cuenta de Claude, Desktop fija `ANTHROPIC_BASE_URL` a la API de Anthropic e **ignora** el valor de los ficheros de ajustes. Las peticiones irían directas a Anthropic y, como `ANTHROPIC_CUSTOM_HEADERS` sí se aplicaría, **con la cabecera `X-Baas-Api-Key`**. El MCP local sí funciona (Desktop lee `~/.claude.json`) | Docs [desktop] (*Environment variables Desktop sets itself*), [llm-gateway-connect#desktop-app]. No verificado en Desktop (sin GUI) |
| Confianza en la carpeta | El `env` de `settings.local.json` y el `headersHelper` solo se aplican tras aceptar el diálogo de confianza del proyecto (en `-p`, el `env` se aplica sin diálogo; el *helper*, no). Sin confianza, el MCP conecta sin cabeceras → 401 | Prueba 2; docs [settings-reference#env], [mcp] |
| Nombres de los comandos | Las skills del plugin responden a `/baas-init` y a `/baas:baas-init` (igual para `/baas-status`) | Prueba 6; docs [skills] |

## 2. Opción implementada

1. `~/baas-ai-ip/.env` (0600, directorio 0700): `BAAS_API_KEY` y `BAAS_APP_URL`. La clave se guarda con `baas.py set-key` en una **terminal aparte** (`getpass`, sin eco) o editando el fichero; nunca pasa por el chat ni por el historial del agente.
2. `<proyecto>/.claude/settings.local.json` (0600, en `.gitignore`; el plugin se niega si está versionado):
   `env.ANTHROPIC_BASE_URL = proxy_anthropic_url` y `env.ANTHROPIC_CUSTOM_HEADERS` con la línea `X-Baas-Api-Key` (conserva otras cabeceras del usuario). **Único fichero del proyecto con la clave.**
3. `<proyecto>/.claude/baas.local.json` (en `.gitignore`): organización y URLs del handshake, sin secretos.
4. Servidor MCP `baas` registrado con el CLI oficial: `claude mcp add-json baas '{"type":"http","url":"<mcp_url>","headersHelper":"<python> ~/baas-ai-ip/bin/baas_mcp_headers.py"}' --scope local`. El *helper* se copia a `~/baas-ai-ip/bin/` porque `${CLAUDE_PLUGIN_ROOT}` no se expande fuera del plugin y su ruta cambia en cada versión. Si `claude` no está en el `PATH`, `/baas-init` imprime el comando (sin la clave) para ejecutarlo a mano.

Mitigaciones añadidas: el ejecutor de scripts quita `ANTHROPIC_CUSTOM_HEADERS` y `BAAS_API_KEY` del entorno del script; `/baas-status` avisa si la sesión no pasa por el proxy (caso Desktop o confianza no aceptada); las URLs del handshake se rechazan si no son HTTPS (HTTP solo en `localhost`, `*.localhost` y `*.test`); `httpclient` no sigue redirecciones (no reenvía `Authorization` a otro host).

## 3. Compromisos aceptados

- **La clave está en un fichero del proyecto** (`settings.local.json`), local, 0600 y en `.gitignore`, pero dentro del árbol: puede acabar en copias de seguridad o *zips* del proyecto.
- **Los subprocesos de la sesión ven la clave**: Claude Code exporta el bloque `env` a todo lo que lanza (Bash, hooks). Un `env` del agente la mostraría en la conversación (que va al proveedor a través del proxy). Endurecimiento opcional, no activado por defecto porque afecta a todo el entorno del usuario en ese proyecto: `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` en el mismo `env` ([env-vars] *What the subprocess environment scrub removes*).
- **Claude Desktop (pestaña Code)**: fuera del alcance del proxy en la Fase 1; y con el proyecto vinculado la cabecera viajaría a Anthropic. Recomendación: usar el CLI o el IDE en proyectos BaaS. Desktop solo enruta a un *gateway* con su «configuración de inferencia de terceros», que es global de la aplicación.
- **Rotación de la clave**: tras `set-key`, hay que relanzar `/baas-init` en cada proyecto para actualizar `settings.local.json` (el MCP la toma sola de `.env`).

## 4. Alternativas descartadas (descritas, no implementadas)

| Alternativa | Por qué no |
| :--- | :--- |
| `${BAAS_API_KEY}` en `settings.local.json` | No se expande (prueba 7) |
| `apiKeyHelper` | Sustituye la credencial del proveedor (`X-Api-Key` y `Authorization`); rompería OAuth y la API key del usuario |
| Exportar `ANTHROPIC_CUSTOM_HEADERS` en el shell o en `~/.claude/settings.json` | Nivel de usuario: la clave iría a Anthropic en proyectos no BaaS (lo que S1 quiere evitar) |
| *Wrapper* `baas-claude` que lea `.env` y lance Claude Code con las variables | Cambia cómo arranca el usuario; no sirve para IDE ni Desktop. Válido como opción futura |
| Proxy local (127.0.0.1) del plugin que añada la cabecera | Clave fuera del proyecto, pero añade un proceso residente, un puerto local accesible a otros usuarios del equipo, latencia y un punto de fallo en el *streaming* |
| Puente MCP `stdio` del plugin que lea la clave | Nombre `plugin:baas:…` (incumple `/mcp__baas__<skill>`) y reimplementar MCP completo en biblioteca estándar |
| `.mcp.json` del proyecto con `headersHelper` | Sin secretos, pero es un fichero pensado para versionarse, exige aprobación del servidor y no se puede usar si ya está versionado con otros servidores |
| Clave literal en `headers` del ámbito local | Copia la clave a `~/.claude.json`; innecesario teniendo `headersHelper` |

## 5. Pruebas locales (reproducibles)

`dev/spike_s1.py` usa el CLI real de Claude Code con `HOME` y proyecto temporales, el BaaS falso (`dev/fake_baas.py`) y una API de Anthropic falsa (`dev/fake_anthropic.py`), dentro de un espacio de nombres de red vacío: **nada sale del equipo**, y las credenciales de proveedor son ficticias.

```bash
unshare -rn sh -c 'ip link set lo up && PYTHONPATH=dev:scripts .venv/bin/python dev/spike_s1.py'
```

Resultado (2026-11-17, Claude Code 2.1.325):

1. `baas.py init` con la clave determinista de `analista@demo.test` → `~/.claude.json` contiene `projects["<proyecto>"].mcpServers.baas = {type: http, url, headersHelper}` y **no** contiene la clave.
2. `claude mcp list` sin confianza en la carpeta: el MCP recibe peticiones **sin** `Authorization`. Con `hasTrustDialogAccepted`: `Authorization: Bearer <clave>` y `baas … ✔ Connected`.
3. `claude -p` con `ANTHROPIC_API_KEY` ficticia: la petición llega a `/org/demo/anthropic/v1/messages` con `X-Baas-Api-Key` correcta y `x-api-key` del proveedor intacta.
4. `claude -p` con `CLAUDE_CODE_OAUTH_TOKEN` ficticio: llegan `Authorization: Bearer sk-ant-oat01-…`, `anthropic-beta` con `oauth-2025-04-20` y `X-Baas-Api-Key`.
5. `claude -p "/mcp__baas__informe"`: el texto del prompt `informe` del MCP llega al modelo.
6. `--plugin-dir` + `/baas-status` y `/baas:baas-status`: ambas cargan la skill.
7. `ANTHROPIC_CUSTOM_HEADERS="X-Baas-Api-Key: ${BAAS_API_KEY}"` con la variable en el entorno: llega el texto literal `${BAAS_API_KEY}` (sin expansión).

Validación del plugin: `claude plugin validate --strict .` → `✔ Validation passed` (avisos del directorio de Anthropic: falta `LICENSE` e icono; no aplican a un marketplace propio).

## 6. Prueba manual con el stack real

Con el repositorio `deploy` (`./scripts/dev-up.sh --fake-upstream`; URLs de su README) y **HOME y proyecto temporales**:

```bash
export HOME=$(mktemp -d) P=$(mktemp -d) && git -C "$P" init -q
KEY=$(python3 -c 'import base64,hashlib;print("baas_"+base64.urlsafe_b64encode(hashlib.sha256(b"baas-dev-key:analista@demo.test").digest()).rstrip(b"=").decode())')
echo "$KEY" | python3 scripts/baas.py set-key --stdin --app-url http://app.localhost:8080
python3 scripts/baas.py init --project "$P"           # organización demo, settings y MCP local
python3 scripts/baas.py status --project "$P"         # usuario, roles, kill switch, AI-IP, «MCP baas: accesible»
python3 scripts/baas.py run-script <ruta de un script de dev-data> --project "$P"
cd "$P" && claude                                      # aceptar la confianza; /mcp → baas conectado; /baas-status
```

Comprobar además: (a) una pregunta a Claude aparece en la webapp (W8/W9) con el usuario `analista@demo.test`; (b) con la organización pausada desde la webapp, `/baas-status` lo muestra, el MCP devuelve `KILL_SWITCH_ACTIVE` y el proxy un 403 con «Acceso bloqueado por BaaS-AI-IP»; (c) en un proyecto **no** vinculado, el proxy no recibe nada; (d) con sesión OAuth (suscripción), la petición llega al proveedor falso con `Authorization` intacta y sin `X-Baas-*`; (e) en la pestaña Code de Claude Desktop, confirmar si `ANTHROPIC_CUSTOM_HEADERS` se aplica (riesgo del §3). Al terminar: `podman-compose down` con el mismo proyecto.

## Fuentes (consultadas el 2026-11-16/17)

- [plugins-reference] https://code.claude.com/docs/en/plugins-reference — `plugin.json`, `${CLAUDE_PLUGIN_ROOT}`, `userConfig`, `claude plugin validate`
- [marketplaces] https://code.claude.com/docs/en/plugin-marketplaces y https://code.claude.com/docs/en/plugins/marketplace-reference — `marketplace.json`, `source: "./"`, nombres reservados
- [skills] https://code.claude.com/docs/en/skills — cabecera, nombres `/plugin:skill` y corto, `allowed-tools`, sustituciones
- [mcp] https://code.claude.com/docs/en/mcp — ámbitos, HTTP, expansión `${VAR}` en `.mcp.json`, `headersHelper` (entorno, confianza), nombres de servidores de plugins, prompts `/mcp__<servidor>__<prompt>`
- [settings] https://code.claude.com/docs/en/settings — ficheros, precedencia, `settings.local.json`
- [settings-reference] https://code.claude.com/docs/en/settings-reference — `env` (cuándo se aplica, variables ignoradas), `apiKeyHelper`
- [env-vars] https://code.claude.com/docs/en/env-vars — `ANTHROPIC_CUSTOM_HEADERS`, `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`
- [llm-gateway] https://code.claude.com/docs/en/llm-gateway y [llm-gateway-connect] https://code.claude.com/docs/en/llm-gateway-connect — suscripción y OAuth a través de un *gateway*, Desktop
- [desktop] https://code.claude.com/docs/en/desktop — variables que Desktop fija en sesiones locales
