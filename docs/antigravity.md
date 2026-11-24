# Antigravity con BaaS-AI-IP

> **Estado**: tarea T3 (#165) de «soporte de más herramientas». Comprobado a mano el **2026-11-23** en Linux con la
> **extensión de VS Code 1.13.0** y la **CLI `agy` 1.5.15** (resultados en [Lo que se comprobó](#9-lo-que-se-comprobó)).
> **Sin comprobar** todavía: Windows y macOS, una carpeta sin confianza y el campo `instructions` del MCP (al final).

Antigravity se usa como **extensión de VS Code** (`Google.google-antigravity`, VS Code 1.90 o posterior, cuenta de Google)
y como **CLI `agy`** (Linux, macOS y Windows). La extensión arranca el mismo motor que la CLI (`~/.gemini/bin/agy`), pero
**no lee la misma configuración de MCP**: de ahí el ámbito por defecto de `setup` (apartado 2).

| Mitad | ¿Con Antigravity? |
| :--- | :--- |
| **Cerebro** (el MCP de tu organización: skills y conocimiento) | **Sí**: `baas.py setup antigravity` |
| **Protección** (proxy y AI-IP) | **No**: sus conversaciones van directas a Google, **sin anonimizar**, y no aparecen en la webapp |

## 1. Qué necesitas

- **Python 3.10 o superior** (solo biblioteca estándar).
- **El repositorio del plugin.** Con Claude Code ya lo tienes instalado (`<ruta del plugin>`). Sin Claude Code, basta con
  clonarlo (es público) y usar su `scripts/baas.py`:

  ```bash
  git clone https://github.com/baas-ai-ip/llm-plugin
  ```

- Tu **API key personal de BaaS-AI-IP** en `~/baas-ai-ip/.env` (en Windows, `%USERPROFILE%\baas-ai-ip\.env`). La guarda
  `/baas-init` en Claude Code o, en una terminal y sin pasar por ningún chat (sirve sin Claude Code),
  `python3 llm-plugin/scripts/baas.py set-key --app-url https://app.<dominio>` (ver el [README](../README.md)).
  `setup` **no** pide la clave ni la acepta como argumento: la lee de ese fichero.
- La extensión de Antigravity o `agy` instalados por ti. El plugin **no** los instala ni los ejecuta.

## 2. Conectar: dos ámbitos

Antigravity tiene dos ficheros de configuración de MCP, y **no los lee igual**:

| Ámbito | Fichero | Lo leen | `setup antigravity …` |
| :--- | :--- | :--- | :--- |
| **global** (por defecto) | `~/.gemini/config/mcp_config.json` (en Windows, `%USERPROFILE%\.gemini\config\mcp_config.json`) | la **extensión** y la **CLI** | sin opción, o `--scope global` |
| **proyecto** | `<proyecto>/.agents/mcp_config.json` | solo la **CLI** `agy` | `--scope project` |

La extensión de VS Code lee **solo** el global (comprobado: con el fichero de proyecto, la extensión no ve `baas`;
tras copiarlo al global, conecta). Como lo normal es usar la extensión, el ámbito por defecto es el global. Si solo
usas `agy` y prefieres **una organización distinta por proyecto**, usa `--scope project`.

```bash
python3 "<ruta del plugin>/scripts/baas.py" setup antigravity --project <carpeta del proyecto>
python3 "<ruta del plugin>/scripts/baas.py" setup antigravity --project <carpeta del proyecto> --scope project
```

- `--project` indica la carpeta cuya **regla** se escribe (en los dos ámbitos). Sin él, la raíz git o la carpeta actual.
- Si perteneces a varias organizaciones, indica cuál con `--org <slug>`. Si el proyecto ya se vinculó con `/baas-init`,
  se usa esa organización.
- Para ver antes lo que escribiría, **sin la clave y sin tocar nada**: añade `--print`.
- Es seguro repetirlo (idempotente): también sirve para renovar la clave.

```bash
python3 "<ruta del plugin>/scripts/baas.py" setup antigravity --project <carpeta del proyecto> --print
```

### Una sola organización en el global

El global es del usuario, no del proyecto: `baas` queda disponible en **todos** tus proyectos y solo cabe **una
organización**. Si ya hay un `baas` que apunta a otro MCP (otra organización), `setup` **se niega a pisarlo** y explica
el motivo. Para cambiarlo a propósito:

```bash
python3 "<ruta del plugin>/scripts/baas.py" setup antigravity --project <carpeta del proyecto> --org <otra> --replace
```

Si el proyecto ya tiene un `baas` en su `.agents/mcp_config.json` (de una versión anterior o de `--scope project`) y
escribes el global, `setup` **avisa** de que quedaría duplicado (no se ha comprobado cuál usa la CLI) y de cómo quitarlo;
no lo borra por su cuenta.

### Qué escribe cada ámbito y qué se niega a hacer

| Fichero | Qué se escribe | Reglas |
| :--- | :--- | :--- |
| `~/.gemini/config/mcp_config.json` (global) | El servidor `baas` (forma de abajo). **Se conservan** los demás servidores (con sus secretos) y los demás ajustes, y lo que ya tuvieras en la entrada `baas` (`disabledTools`, `timeoutSeconds`…) | Permisos **0600**; escritura atómica; **sin copias de seguridad** (acumularían los secretos de otros servidores); no toca ningún `.gitignore`; se niega si no es JSON válido o no tiene la forma esperada (no lo toca); si es un enlace simbólico, escribe en su destino y el enlace sigue |
| `<proyecto>/.agents/mcp_config.json` (con `--scope project`) | Lo mismo | Además: se añade al `.gitignore` del proyecto y **se niega a escribir si está versionado** (`[BAAS_PROJECT_UNSAFE]`: sácalo con `git rm --cached`) |
| `<proyecto>/.agents/rules/baas.md` (siempre) | La regla `always_on` de abajo | Sin secretos: puede versionarse. Si ya existe un `baas.md` que no escribió este comando, **no lo pisa** (avisa) |
| `AGENTS.md`, `GEMINI.md` | **Nada**: nunca se tocan | — |

```json
{
  "mcpServers": {
    "baas": {
      "serverUrl": "https://mcp.example.com/org/mi-organizacion/mcp",
      "headers": {
        "Authorization": "Bearer <API key>"
      }
    }
  }
}
```

`serverUrl` es el MCP de tu organización (la devuelve el *handshake* de la webapp) y solo admite **Streamable HTTP**, que
es lo que sirve el MCP de BaaS-AI-IP. `<API key>` se sustituye por tu clave, **en claro** (ver más abajo).

### La regla `.agents/rules/baas.md`

Es el equivalente de la skill `baas-brain` de Claude Code: la regla (`trigger: always_on`, se inyecta en cada turno)
enseña al agente las tools `baas_*` y a **detenerse** ante `AUTHORIZATION_DENIED` y `KILL_SWITCH_ACTIVE`, sin buscar el
mismo contenido por otra vía. La regla es **de proyecto** también con el MCP global: Antigravity la lee en cada
proyecto donde exista (comprobado con la extensión), así que ejecuta `setup` una vez por proyecto. Ocupa unos 5 KB;
Antigravity admite 24 000 bytes por regla y trunca lo que pase. No usa `AGENTS.md` ni `GEMINI.md` para no pisar los
tuyos. Tampoco explica `/baas-sync` ni el ejecutor de scripts, que son de Claude Code: la regla le dice al agente que
**no ejecute** los scripts de las skills de la organización.

## 3. Extensión de VS Code

1. Instala la extensión **Antigravity** (`Google.google-antigravity`) desde el Marketplace e inicia sesión con tu cuenta
   de Google.
2. Abre la **carpeta del proyecto** en VS Code (la que pasaste a `--project`) y acepta la confianza en la carpeta.
3. Ejecuta `setup antigravity` (ámbito global, el de por defecto) en una terminal (la integrada vale).
4. Recarga la ventana (*Developer: Reload Window*) para que lea el `mcp_config.json`.
5. Comprueba que el servidor `baas` aparece conectado en la configuración de MCP de la extensión y pídele al agente:
   «Llama a `baas_whoami`». Antigravity pregunta por defecto antes de usar una tool MCP: acéptala.

## 4. CLI `agy`

1. Ejecuta `setup antigravity` en la carpeta del proyecto (global, o `--scope project` para que solo aplique ahí).
2. Lanza `agy` **desde esa carpeta**.
3. Escribe `/mcp` (el gestor de MCP de la CLI): `baas` debe aparecer conectado. Pídele al agente «Llama a `baas_whoami`».

## 5. Skills de la organización como skills nativas

En Claude Code las skills de la organización salen como `/<organización>:<skill>` porque el MCP las publica como
*prompts*. Antigravity **no documenta los *prompts* como comandos**: sus skills son carpetas con un `SKILL.md` y la CLI
convierte cada una en `/<nombre>`. Este comando copia las que tus roles autorizan:

```bash
python3 "<ruta del plugin>/scripts/baas.py" sync --tool antigravity --dry-run   # solo muestra qué haría
python3 "<ruta del plugin>/scripts/baas.py" sync --tool antigravity             # añade, actualiza y retira
python3 "<ruta del plugin>/scripts/baas.py" sync --tool antigravity --org <slug> --force
```

Se ejecuta **a mano** (no hay *hooks*): repítelo cuando quieras traer cambios. Las deja en
`~/.gemini/config/skills/<skill>/` (en Windows, `%USERPROFILE%\.gemini\config\skills\<skill>\`), el ámbito **global**
que, según la documentación, leen la CLI y la extensión, igual que el MCP de `setup`. No hay `--scope project` para las
skills (`<proyecto>/.agents/skills/`): en una carpeta de proyecto podrían acabar en un commit contenidos de la
organización que no son públicos.
Después, en `agy`, ejecuta `/skills reload` (o abre una conversación nueva).

| Qué | Cómo |
| :--- | :--- |
| **Qué se copia** | Lo mismo que `/baas-sync` de Claude Code: las skills de `baas_list_skills` (tus roles) y, de forma transitiva, las que dependen de ellas **si tienes acceso**. Se verifica el **SHA-256** de cada fichero, se rechazan las rutas que salgan de la carpeta de la skill y nunca se crean enlaces simbólicos; cada skill se escribe en un temporal y se renombra (si falla, queda la versión anterior). Carpetas 0700 y ficheros 0600 |
| **Nombre del comando** | La carpeta, **sin prefijo** de organización: así los enlaces `../otra/SKILL.md` y las citas `/otra` que ya traen las skills siguen valiendo, y solo hay una organización en el global. Sin `:` (Windows). Solo minúsculas, dígitos y guiones: el resto (`Mi_Skill`, `a.b`), los nombres reservados de Windows y los que taparían un comando de la CLI (`mcp`, `skills`) **no se instalan** y el comando los lista. El `name` del `SKILL.md` se iguala a la carpeta |
| **Sin sus scripts** | **No se copia nada ejecutable**: ficheros marcados como ejecutables, carpetas `scripts/`, código y binarios por extensión (`.py`, `.sh`, `.js`, `.bat`, `.exe`…) y todo lo que empiece por `#!`. Plantillas, referencias y demás ficheros de texto o datos sí. El comando lista lo omitido y avisa de que esa skill **no funcionará completa**. Motivo: un script en disco lo ejecutaría el agente sin la comprobación del SHA-256 que hace `run-script` al ejecutarlo y seguiría ahí tras una revocación |
| **`SKILL.md` adaptado** | Antigravity documenta `description` (obligatoria) y `name`: se escriben solo esos dos (`when_to_use` se une a la descripción) y el cuerpo queda tal cual. Los campos propios de Claude Code (`allowed-tools`, `hooks`, `argument-hint`…) se quitan y se avisa. Sin `description` no se instala. Una skill con `disable-model-invocation` (solo invocación manual) **no se instala**: Antigravity no puede respetarlo y el agente la usaría por su cuenta. Las referencias propias de Claude Code en el texto (`${CLAUDE_SKILL_DIR}`, `/<organización>:<skill>`) no se reescriben |
| **No pisa lo tuyo** | Lo que gestiona queda en `~/.gemini/config/skills/.baas-skills.json` (organización, `package_sha256` y SHA-256 de lo escrito; sin secretos). Una carpeta con el mismo nombre que no figura ahí es **tuya**: se deja como está y el comando lo cuenta («no la toco»). Una skill gestionada que **editaste** (o a la que añadiste ficheros o enlaces) tampoco se sobrescribe ni se retira, salvo con `--force`; una tuya, nunca |
| **Retirada** | Quitar un rol, retirar la skill o activar el kill switch la borra en la siguiente sincronización, solo si es de las gestionadas. Al cambiar de organización se retiran las de la anterior |
| **Organización** | `--org`, si no la del `baas` del `mcp_config.json` global, si no la del proyecto vinculado con `/baas-init`, si no la única; con varias, error 3. Avisa si no coincide con la del MCP global |

## 6. La clave queda en claro (riesgo aceptado)

La documentación de Antigravity **no admite `${VAR}`** en las cabeceras ni un comando que dé el secreto (como el
`headersHelper` de Claude Code): `Authorization` lleva la clave literal. Por eso:

- La clave está **en claro** en el `mcp_config.json` (el global, por defecto: **en un fichero de tu HOME que no pasa
  por ningún `.gitignore`**, así que cuidado si versionas tu HOME o tus *dotfiles*). El plugin lo deja en 0600, pero
  **cualquier proceso con tu usuario puede leerlo**, y el agente de Antigravity también si le abres ese fichero. La
  regla le prohíbe leerlo, pero no es una barrera técnica. Con `--scope project` se añade además al `.gitignore` y se
  niega a escribir si está versionado.
- En **Windows** los permisos 0600 no se aplican: el fichero hereda los de la carpeta. No lo pongas en una carpeta
  compartida ni sincronizada.
- No pegues la clave en el chat ni la copies a otro fichero. Si se expone: revócala en la webapp (*Mis API keys*),
  guarda otra (`set-key` o `/baas-init`) y repite `setup antigravity`.
- Cuando el proxy admita credenciales de proyecto (#175) se podrá evitar guardar la clave personal aquí.

## 7. Lo que NO cubre

- **Sin proxy ni AI-IP.** Las conversaciones de Antigravity van a Google **sin anonimizar**: nada se sustituye antes de
  salir, no hay trazas en la webapp (W8) y el kill switch, el bloqueo y la pausa de la organización **no cortan el
  modelo**. Solo cortan el MCP (`KILL_SWITCH_ACTIVE`). No pegues datos que no pegarías en una herramienta sin
  protección. (La protección para su CLI con clave de Gemini es el trabajo de #178.)
- **Varias organizaciones a la vez en la extensión**: el global solo admite una.
- **Las skills de la organización como comandos del MCP.** El servidor `baas` las expone como *prompts* del MCP
  (`/mcp__baas__<skill>` en Claude Code) y la documentación de Antigravity no dice que los muestre: por eso se copian
  como skills nativas (apartado 5), **sin sus scripts** y sin actualización automática. Mientras no estén copiadas, el
  agente las consulta con `baas_list_skills` y `baas_get_skill`.
- **El ejecutor de scripts** (`run-script`), `/baas-sync` y los *hooks* de sincronización desde Antigravity.
- Windows y macOS: los probará T6 (#168), no esta guía.

## 8. Solución de problemas

| Síntoma | Causa y solución |
| :--- | :--- |
| `[BAAS_KEY_MISSING]` (2) | No hay clave en `~/baas-ai-ip/.env`: lanza `/baas-init` o `set-key` primero |
| `[BAAS_CONFIG_ERROR]` (2) «una sola organización» | El global ya tiene `baas` con otra organización: repite con `--replace`, o usa `--scope project` |
| `[BAAS_ORG_SELECTION_REQUIRED]` (3) | Perteneces a varias organizaciones: repite con `--org <slug>` |
| `[BAAS_AUTH_ERROR]` (5) | La clave está revocada o caducada: guarda otra (`/baas-init` o `set-key`) |
| `[BAAS_PROJECT_UNSAFE]` (7) | El `mcp_config.json` no es JSON válido (corrígelo; no se toca) o, con `--scope project`, está versionado (`git rm --cached .agents/mcp_config.json` y commit) |
| `agy` conecta y la extensión no | Probablemente escribiste con `--scope project`: la extensión solo lee el global. Repite sin `--scope` |
| `baas` no aparece | Recarga la ventana o reinicia `agy` desde la carpeta del proyecto; comprueba que el JSON es válido |
| Falla con 401 | La clave del fichero está revocada: renuévala y repite `setup antigravity` |
| `KILL_SWITCH_ACTIVE` o 403 | La plataforma está detenida o tu organización bloqueada o pausada: habla con tu administrador |
| El agente no conoce las tools `baas_*` | Falta `.agents/rules/baas.md` en ese proyecto o ya existía uno que no es de BaaS (el comando avisa): renómbralo y repite |

## 9. Lo que se comprobó

Prueba manual del 2026-11-23, Linux, extensión de VS Code **1.13.0** y `agy` **1.5.15**:

- **CLI**: conecta con `baas` leyendo el `.agents/mcp_config.json` de proyecto (`/mcp` lo muestra y `baas_whoami`
  responde) aunque el global no lo tenga.
- **Extensión** (carpeta de confianza aceptada): con `baas` solo en el fichero de proyecto, **no** conecta (solo ve los
  servidores del global); sí carga la regla de proyecto `.agents/rules/baas.md`. Tras copiar el bloque `baas` al global,
  conecta y `baas_whoami` responde. Por eso el ámbito por defecto es el global.
- La extensión arranca `~/.gemini/bin/agy --hub … --add-dir=<proyecto>`: el motor es el mismo que el de la CLI; solo
  cambia qué configuración de MCP lee.

## 10. Lista de comprobación manual

Marca lo que compruebes (lo de Linux ya está hecho el 2026-11-23; falta Windows y lo que no se pudo probar):

- [ ] `setup antigravity --print` no muestra la clave y no escribe nada (ni en el HOME ni en el proyecto).
- [ ] `setup antigravity` crea `~/.gemini/config/mcp_config.json` con 0600 en Linux, conserva los demás servidores y
      deja `.agents/rules/baas.md` en el proyecto; repetirlo no cambia nada («sin cambios»).
- [ ] Con otra organización ya en el global, se niega sin `--replace`; con `--replace` la cambia.
- [ ] **Extensión**: `baas` aparece conectado tras recargar; «Llama a `baas_whoami`» devuelve tu usuario y organización.
- [ ] **CLI**: `/mcp` muestra `baas` conectado; el mismo `baas_whoami` responde.
- [ ] `sync --tool antigravity --dry-run` lista las skills y no escribe nada (ni crea `~/.gemini/config/skills/`).
- [ ] `sync --tool antigravity` crea `~/.gemini/config/skills/<skill>/SKILL.md` (carpetas 0700, ficheros 0600) y
      `.baas-skills.json`, **no copia ningún script** (el aviso los lista) y repetirlo dice «Todo al día».
- [ ] **CLI**: tras `/skills reload`, `/skills` muestra la skill y **`/<skill>`** la lanza. Anota si el comando lleva otro
      prefijo o si el `description` reescrito basta para que el agente la elija solo.
- [ ] **Extensión**: anota si la extensión ofrece las skills como comando (`/<skill>`, menú de skills…) o solo las usa
      el agente por su cuenta.
- [ ] Una skill tuya con el mismo nombre queda intacta y el comando dice «no la toco»; una skill sincronizada que editas
      no se pisa sin `--force`.
- [ ] Quitar un rol (o retirar la skill) la borra en la siguiente sincronización; las tuyas siguen ahí.
- [ ] Windows: `%USERPROFILE%\.gemini\config\skills\`, los nombres y las rutas salen bien.
- [ ] El agente aplica la regla: ante una pregunta sobre la organización busca con `baas_search_knowledge`.
- [ ] Con un recurso al que no tienes rol, el agente **se detiene** e informa de `AUTHORIZATION_DENIED` (sin buscar otra vía).
- [ ] El acceso queda **auditado** (la webapp registra la llamada a la tool).
- [ ] Con la organización pausada, `KILL_SWITCH_ACTIVE` y el agente se detiene.
- [ ] La clave **no** aparece en el chat, en ningún `.gitignore` ni en ningún otro fichero.
- [ ] Windows: las rutas (`%USERPROFILE%\.gemini\config`) y el JSON salen bien; anota los permisos reales del fichero.

## 11. Preguntas abiertas (la documentación no las responde)

La de si la extensión lee el mismo fichero que la CLI está **respondida** (apartado 9). Quedan estas:

| Pregunta | Cómo comprobarla |
| :--- | :--- |
| ¿Respeta el `.agents/mcp_config.json` de proyecto en una **carpeta sin confianza** (*Restricted Mode*)? | La extensión no lo lee ni con la confianza aceptada, así que el riesgo se limita a la CLI: abre una carpeta sin confianza con `agy` y mira si `baas` se conecta. Si lo hiciera, un repositorio ajeno podría traer su propio `.agents/mcp_config.json`: no confíes en uno que no hayas escrito tú |
| ¿Ofrece la extensión las skills nativas como comando, o solo las usa el agente? | Tras `sync --tool antigravity`, mira en la extensión si aparece `/<skill>` o algún menú de skills; pídele al agente una tarea que encaje con la `description` y mira si la usa. Solo la CLI está documentada como comando |
| ¿La CLI las lanza como `/<nombre>` (el de la carpeta) o con otro prefijo? | `/skills` en `agy` y prueba `/<skill>`. El plugin pone `name` = carpeta y no inventa prefijos |
| ¿Usa el campo `instructions` del MCP? | La documentación solo dice que lo guarda en una caché (`instructions.md`); mira si el agente lo recibe sin la regla (borra `.agents/rules/baas.md` y pregúntale qué tools tiene) |

Fuentes: <https://antigravity.google/docs/mcp>, <https://antigravity.google/docs/rules>, <https://antigravity.google/docs/skills>.
