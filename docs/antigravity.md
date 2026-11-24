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
tuyos. Tampoco explica `/baas-sync`, que es de Claude Code. Sí explica las skills nativas y sus scripts: se ejecutan
desde la carpeta de la skill (verificados al sincronizar), nunca copiando ni ejecutando a mano el contenido de
`baas_get_script`; qué hacer si una orden falla dentro del sandbox; y que, si algo no cuadra, el usuario ejecute
`sync` o `--verify` (ver «Scripts, enlaces y citas del `SKILL.md`»).

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
convierte cada una en `/<nombre>`. Este comando copia las que tus roles autorizan, de **una organización cada vez**:

```bash
python3 "<ruta del plugin>/scripts/baas.py" sync --tool antigravity --dry-run   # solo muestra qué haría
python3 "<ruta del plugin>/scripts/baas.py" sync --tool antigravity             # añade, actualiza y retira
python3 "<ruta del plugin>/scripts/baas.py" sync --tool antigravity --org <slug> --force
python3 "<ruta del plugin>/scripts/baas.py" sync --tool antigravity --verify       # recomprueba lo copiado (local, sin red)
python3 "<ruta del plugin>/scripts/baas.py" sync --tool antigravity --permissions  # imprime la regla de permisos de `agy`
```

Se ejecuta **a mano** (no hay *hooks*): repítelo cuando quieras traer cambios. Las deja en
`~/.gemini/config/skills/<organización>-<skill>/` (en Windows, `%USERPROFILE%\.gemini\config\skills\`), el ámbito **global**
que, según la documentación, leen la CLI y la extensión, igual que el MCP de `setup`. No hay `--scope project` para las
skills (`<proyecto>/.agents/skills/`): en una carpeta de proyecto podrían acabar en un commit contenidos de la
organización que no son públicos.
Después, en `agy`, ejecuta `/skills reload` (o abre una conversación nueva); la skill `gestor` de la organización `demo` se lanza como `/demo-gestor`.

| Qué | Cómo |
| :--- | :--- |
| **Qué se copia** | Lo mismo que `/baas-sync` de Claude Code: las skills de `baas_list_skills` (tus roles) y, de forma transitiva, las que dependen de ellas **si tienes acceso**. Se verifica el **SHA-256** de cada fichero, se rechazan las rutas que salgan de la carpeta de la skill y nunca se crean enlaces simbólicos; cada skill se escribe en un temporal y se renombra (si falla, queda la versión anterior). Carpetas 0700 y ficheros 0600 |
| **Nombre del comando** | `<organización>-<skill>` (el *slug* de la organización: `<slug>-<skill>`, p. ej. `enrique-mingorance-cano-gestor`), con el **prefijo obligatorio**: un usuario puede tener skills con el mismo nombre (`gestor`) en varias organizaciones, como pasa con `/<org>:<skill>` en Claude Code, y todas conviven en la misma carpeta. La carpeta, el `name` del `SKILL.md` y el comando son ese mismo nombre. **No es `<org>:<skill>`**: la documentación de Antigravity solo dice que `name` va en minúsculas y con guiones (no menciona `:` ni prefijos) y Windows no admite `:` en una carpeta. Solo minúsculas, dígitos y guiones en la skill: el resto (`Mi_Skill`, `a.b`) **no se instala** y el comando lo lista. Con el prefijo ya no hay choques con comandos de la CLI (`/mcp`, `/skills`) ni con nombres reservados de Windows. Los enlaces y las citas a otras skills de la misma organización se reescriben al nombre con prefijo (ver «Scripts, enlaces y citas del `SKILL.md`») |
| **Scripts: copiados y verificados** | Como en Claude Code, **se copia el paquete entero** de cada skill, scripts y ejecutables incluidos, a `<organización>-<skill>/` con el **SHA-256** de cada fichero comprobado contra el manifiesto; carpetas 0700 y ficheros 0600 (0700 solo los que el manifiesto marca como ejecutables). No se omite nada por tipo ni por tamaño. Si un hash no coincide, esa skill no se instala y la anterior queda como estaba. Ver «Scripts, enlaces y citas» |
| **`SKILL.md` adaptado** | Antigravity documenta `description` (obligatoria) y `name`: se escriben solo esos dos (`when_to_use` se une a la descripción) y el cuerpo queda tal cual, **precedido de una nota** que dice a qué organización pertenece la skill, que sus tools `baas_*` son las del MCP configurado y que, si `baas_whoami` no devuelve esa organización, el agente debe detenerse (ver «Varias organizaciones, un solo MCP»). Si la skill trae scripts, la nota dice dónde están (la ruta absoluta de su carpeta), que se verificaron al sincronizar, que no se ejecuta ninguna copia hecha a mano con `baas_get_script` y que algunos leen ficheros locales del proyecto. Los campos propios de Claude Code (`allowed-tools`, `hooks`, `argument-hint`…) se quitan y se avisa. Sin `description` no se instala. Una skill con `disable-model-invocation` (solo invocación manual) **no se instala**: Antigravity no puede respetarlo y el agente la usaría por su cuenta. El resto del cuerpo se reescribe solo en lo que se reconoce con certeza (ver «Scripts, enlaces y citas del `SKILL.md`») |
| **No pisa lo tuyo** | Lo que gestiona cada organización queda en `~/.gemini/config/skills/.baas-skills-<organización>.json` (`package_sha256` y SHA-256 de lo escrito; sin secretos). Una carpeta con el nombre de una skill que no figura en el fichero de **esa** organización es ajena: se deja como está y el comando lo cuenta («no la toco»). Es el caso de una tuya, y también el de una carpeta de otra organización cuando los nombres se solapan (`demo` + `x-y` y `demo-x` + `y` dan la misma `demo-x-y`). Una skill gestionada que **editaste** (o a la que añadiste ficheros o enlaces) tampoco se sobrescribe ni se retira, salvo con `--force`; una ajena, nunca |
| **Retirada** | Quitar un rol, retirar la skill o activar el kill switch (o bloquear o pausar esa organización) la borra en la siguiente sincronización **de esa organización**, solo si es de las que gestiona. Sincronizar una organización no toca las skills ni el fichero de control de otra |
| **Organización** | `--org <slug>` (una por ejecución: repite el comando para cada una), si no la del `baas` del `mcp_config.json` global, si no la del proyecto vinculado con `/baas-init`, si no la única; con varias, error 3 |

### Scripts, enlaces y citas del `SKILL.md`

**Es el modelo de confianza de Claude Code.** Los scripts de la organización se copian a tu HOME
(`~/.gemini/config/skills/<organización>-<skill>/scripts/…`) **verificados al sincronizar**: el SHA-256 de cada fichero
se comprueba contra el manifiesto del MCP, las rutas no pueden salir de la carpeta (nada de `..`, absolutas ni `\`), no
se crean enlaces simbólicos y cada skill se escribe en un temporal y se renombra (si algo falla, queda la versión
anterior). Después el agente los ejecuta **directamente desde su carpeta**: la confianza se da al sincronizar, no en cada
ejecución. Lo que eso **no** cubre: un script malicioso publicado por un administrador es el que se ejecutará (igual que
en Claude Code); una revocación llega con el siguiente `sync`; y quien tenga tu usuario (el propio agente, por ejemplo)
puede editarlos después. Para eso está `--verify`:

```bash
python3 "<ruta del plugin>/scripts/baas.py" sync --tool antigravity --verify
```

`--verify` recomprueba, **sin red, sin clave y sin modificar nada**, todo lo copiado contra el SHA-256 del fichero de
control (`.baas-skills-<organización>.json`): ficheros editados, que faltan o añadidos, enlaces simbólicos, carpetas
que faltan y, en POSIX, el bit de ejecución (solo los marcados en el manifiesto) y los permisos de escritura. Sale con
código 9 si algo difiere. `sync --force` restaura las skills (sobrescribe tus cambios).

Un `SKILL.md` de la organización está escrito para Claude Code: llama a sus scripts con
`python3 "${CLAUDE_SKILL_DIR}/scripts/x.py" args…` (variable que Antigravity no define) o con una ruta relativa
(«ejecuta el script desde la carpeta de la skill: `python3 scripts/x.py`») y enlaza o cita a otras skills por su nombre
(`../facturacion/SKILL.md`, `/gestor`). El `sync` lo reescribe de forma **determinista y conservadora**: solo lo que
reconoce con certeza; lo demás queda tal cual y el comando lo cuenta por skill.

| Se reescribe | A qué |
| :--- | :--- |
| `${CLAUDE_SKILL_DIR}` y `$CLAUDE_SKILL_DIR`, con o sin comillas dobles, en bloques de código, fragmentos de código en línea y menciones (`python3 "${CLAUDE_SKILL_DIR}/scripts/x.py" args`, `"${CLAUDE_SKILL_DIR}/scripts/x.sh" args`, `--template "${CLAUDE_SKILL_DIR}/templates/a.typ"`…) | La **ruta absoluta de la carpeta de la skill**, entrecomillada si hace falta (espacios, paréntesis). Los **argumentos originales quedan como texto**, sin reinterpretar. Se anota qué scripts se llaman (`calls`) para la regla de permisos |
| Llamadas con ruta relativa (`python3 scripts/x.py`, `./scripts/x.sh`) **solo si el texto dice con certeza que se ejecutan desde la carpeta de la skill**: «desde la carpeta de la skill» (o «from the skill directory») en la misma línea o en la que precede al bloque, `cd "${CLAUDE_SKILL_DIR}" && …` o un `cd` anterior del mismo bloque | `python3 "<carpeta>/scripts/x.py"`. El script debe existir en el paquete y el intérprete corresponder a la extensión (`python3` con `.py`, `bash`/`sh` con `.sh`, `node` con `.js`) |
| Enlaces markdown `../<skill>/…` a otras skills **sincronizadas de la misma organización** | `../<org>-<skill>/…` (se conserva el resto y el `#fragmento`) |
| Citas `` `/<skill>` `` (el fragmento de código es exactamente eso) o `[/<skill>](…)`, solo si es una skill sincronizada | `/<org>-<skill>` |
| En **Windows**, el intérprete `python3` de las llamadas reconocidas | `python` |

| Se deja **intacto** (y se cuenta en la salida) | Por qué |
| :--- | :--- |
| `python3 scripts/x.py` sin que el texto diga desde dónde, o con otra carpeta («desde otra carpeta»), y rutas relativas con opciones (`python3 -u …`) | No se sabe a qué carpeta se refiere («llamada(s) a scripts con ruta relativa sin reescribir») |
| `${CLAUDE_SKILL_DIR}` entre comillas simples, o sin comillas cuando la carpeta tiene espacios y pegado a otra comilla | El shell no lo expandiría, o no se puede entrecomillar con certeza («referencia(s) a `${CLAUDE_SKILL_DIR}` sin reescribir») |
| Todo, si la ruta de la carpeta tiene caracteres que un shell reinterpretaría (`"`, `$`, `` ` ``, `%`, `!`…) | Se avisa y no se reescribe nada de esa skill |
| Enlaces a ficheros del cerebro (`../../../knowledge/…`) | Son **locales**: apuntan a un proyecto que tenga ese cerebro, no a la carpeta de la skill |
| Enlaces y citas dentro de bloques de código, citas `/gestor` en prosa suelta, citas de skills que no están sincronizadas | Podrían ser ejemplos ajenos o rutas |

- **Qué necesitan los scripts.** Algunos leen y escriben ficheros locales (por ejemplo `knowledge/fiscal/…` en `gestor` y
  `facturacion`): se ejecutan en el **directorio de trabajo actual** y la skill **solo funciona completa** en un proyecto
  que tenga ese cerebro como **carpeta de trabajo** (abre el proyecto de cerebro en VS Code o lanza `agy` desde él). No se
  asumen rutas. Otros guardan estado fuera del proyecto: el contador escribe `~/.local/state/contador/valor`, **el mismo
  fichero que usa Claude Code**, así que comparten el valor.
- **El sandbox de `agy`.** Según su documentación, `agy` ejecuta las órdenes dentro de un sandbox: el proyecto en
  lectura y escritura, el resto de tu HOME **oculto**, `.env` bloqueado y **sin red** salvo `read_url(<dominio>)`. Los
  scripts están en `~/.gemini/config/skills/…`, fuera del proyecto, y `python3` puede ser un **envoltorio** de shell que
  lanza un entorno virtual de tu HOME (p. ej. `~/.local/bin/python3`): dentro del sandbox falla con «No such file or
  directory» (código 127), y un script que escribe en `~/.local/state/…` tampoco puede. Sin una regla de permisos, `agy`
  **reintenta dentro del sandbox y te pide salir de él**; el agente no debe improvisar (la regla se lo prohíbe: nada de
  copiar el script al proyecto). Para dejar de preguntar, **solo para estos scripts**, el `sync` imprime (no la escribe:
  son tus permisos) la regla que va en `permissions.allow` de `~/.gemini/antigravity-cli/settings.json`
  (`sync --tool antigravity --permissions` la repite sin red):

  ```json
  {
    "permissions": {
      "allow": [
        "command(python3 /home/<tú>/.gemini/config/skills/<organización>-<skill>/scripts/<script>.py)"
      ]
    }
  }
  ```

  Una regla por llamada reconocida (la documentación solo admite `*` como comodín global, y `command(…)` casa por
  prefijo, palabra a palabra, y deja ejecutar la orden con o sin sandbox). **NO comprobada con `agy` real**: la
  documentación no dice cómo se escriben las comillas ni las rutas con espacios, ni si `*` vale dentro de un patrón; si
  una orden lleva la ruta entrecomillada y la regla no casa, `agy` te lo preguntará. La extensión de VS Code puede
  comportarse distinto (no comprobado). Las reglas `deny` siguen mandando sobre las `allow`.
- **Si mueves la carpeta** (`~/.gemini`, tus *dotfiles*), las rutas absolutas del `SKILL.md` cambian: el siguiente `sync`
  lo detecta y vuelve a adaptar **solo** las skills que dependen de ella (las que citan la carpeta o traen scripts). Lo
  mismo si se sincroniza una skill que otra cita (su enlace pasa a tener prefijo).
- **Migración.** Un fichero de control de versiones anteriores (que no copiaban los scripts y escribían órdenes
  `run-script`) se migra solo en el siguiente `sync`: cambia la huella, se descargan los scripts y se reescribe el texto.
  Una skill que editaste no se pisa sin `--force`. Quitar una skill borra también sus scripts.
- **Windows.** El intérprete que se escribe es `python` (no `python3`) en las llamadas reconocidas, y la carpeta va entre
  comillas dobles, con espacios y paréntesis. Sin probar.
- **`run-script` sigue disponible**, como vía opcional que **verifica en cada ejecución** y no deja el script en disco:
  `python3 "<ruta del plugin>/scripts/baas.py" run-script --tool antigravity --synced-org <org> skills/<skill>/scripts/<script> -- <argumentos>`.
  Ya no se escribe en los `SKILL.md`: dentro del sandbox no puede funcionar (necesita red y la clave de `~/baas-ai-ip/.env`,
  que el sandbox oculta). El MCP sale del fichero de control, nunca de un argumento, y solo vale para skills
  sincronizadas de esa organización.
- **Seguridad.** Un `SKILL.md` remoto dicta órdenes al agente, y eso ya era así: el agente lee el texto de la skill y
  ejecuta lo que dice (con el permiso de Antigravity para ejecutar comandos). La reescritura **no se amplía** más allá de
  sustituir la carpeta de la skill: cada pieza que entra en el texto se valida (nombres con la misma expresión que las
  carpetas y una ruta de carpeta sin caracteres que un shell reinterprete). No impide que una skill maliciosa pida otra
  cosa (`curl | bash`): la confianza está en quien administra la organización y en la confirmación de órdenes de
  Antigravity, que conviene mantener activa; la regla de permisos sugerida solo cubre los scripts de las skills.

### Varias organizaciones, un solo MCP (limitación conocida)

Las skills de varias organizaciones conviven, pero `setup antigravity` solo admite **una** entrada `baas` en el
`mcp_config.json` global (`--replace` para cambiarla, apartado 2). Una skill usa las tools `baas_*` del MCP configurado:
si solo está el de `demo`, la skill `otra-gestor` **actuaría sobre `demo`** (leería y, con rol de administrador,
escribiría documentos de la organización equivocada). Lo que se hace hoy para reducir el riesgo, sin cambiar el MCP:

- Cada `SKILL.md` adaptado empieza con una nota: «comprueba con `baas_whoami` que la organización es la tuya; si no, detente».
- La regla `baas.md` dice lo mismo para cualquier skill nativa.
- `sync` avisa si el MCP global es de otra organización, o si no hay ninguno.

Es una barrera de instrucciones (depende de que el agente las siga), no técnica. Opciones para resolverlo de raíz:

| Opción | Qué cambia | Coste y riesgos |
| :--- | :--- | :--- |
| **A. Una entrada `baas` (la actual)** | Nada | Coste cero. Riesgo residual: depende de la nota |
| **B. Una entrada por organización, `baas-<slug>`. Recomendada** | `setup antigravity` escribe y actualiza solo la entrada `baas-<slug>` (URL propia, misma clave personal); desaparece `--replace`, y la entrada `baas` antigua se renombra si su URL es la de una organización del usuario. La nota de cada `SKILL.md` nombra el servidor `baas-<slug>` y la regla `baas.md` explica que las tools `baas_*` están por servidor y que `baas_whoami` de cada uno dice su organización. La extensión y la CLI muestran las tools por servidor (con carga ansiosa se llaman `mcp_<servidor>_<tool>`; con la perezosa por defecto se invocan por servidor), así que no chocan | Cambio **pequeño-medio**, pero **rompe los tests de T3** (nombre de la entrada, conflicto y `--replace`: unos 15-20 tests) y reescribe la regla y esta guía. N organizaciones son N conexiones y más tools en el contexto (carga perezosa por defecto). La clave queda en claro **una vez por entrada** (más copias en el fichero global). Sigue habiendo riesgo de elegir mal el servidor, pero cada nombre lleva el *slug* y la skill dice cuál usar |
| **C. Una organización activa y skills solo de ella** | Volver al nombre sin prefijo | **Descartada**: contradice la decisión de que las skills de varias organizaciones convivan |
| **D. Un único MCP multiorganización con `org` en cada llamada** | Nada en el plugin | **No sirve**: el parámetro `org` exige ser ORG_ADMIN de esa organización o PLATFORM_ADMIN, no vale para un miembro normal |

La opción B no está hecha: queda a la espera de decisión.

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
- **Varias organizaciones a la vez con el MCP**: las skills sí conviven, pero el global solo admite un MCP `baas` (apartado 5, «Varias organizaciones, un solo MCP»).
- **Las skills de la organización como comandos del MCP.** El servidor `baas` las expone como *prompts* del MCP
  (`/mcp__baas__<skill>` en Claude Code) y la documentación de Antigravity no dice que los muestre: por eso se copian
  como skills nativas (apartado 5), con sus scripts verificados al sincronizar, y sin actualización automática. Mientras no estén copiadas, el
  agente las consulta con `baas_list_skills` y `baas_get_skill`.
- **`/baas-sync` y los *hooks* de sincronización** desde Antigravity: el `sync` es manual.
- **Lo no reescrito** del `SKILL.md` (tablas del apartado 5): rutas relativas `scripts/…` sin una indicación clara de la carpeta, `${CLAUDE_SKILL_DIR}` entre comillas simples y citas `/gestor` en prosa suelta.
- **El sandbox de `agy`**: el plugin no lo configura ni escribe tus permisos; solo imprime la regla sugerida (apartado 5).
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
| La orden de una skill falla en `agy` con «No such file or directory» (127), sin red o pidiendo salir del sandbox | El sandbox oculta tu HOME y `python3` puede ser un envoltorio de shell: añade la regla que imprime `sync --tool antigravity --permissions` a `permissions.allow` de `~/.gemini/antigravity-cli/settings.json`, o aprueba la salida del sandbox. No se copia el script al proyecto |
| `sync --verify` dice que un script ha cambiado, falta o es ejecutable sin serlo | Alguien (tú, el agente) lo tocó: `sync --tool antigravity --force` lo restaura (sobrescribe tus cambios); no uses la skill hasta revisarlo |
| La orden lleva la ruta de una carpeta que ya no existe | Moviste `~/.gemini`: repite `sync --tool antigravity` (actualiza solo las skills que dependen de la ruta) |
| El script dice que no encuentra `knowledge/…` | La skill necesita el cerebro como carpeta de trabajo: abre ese proyecto (o lanza `agy` desde él) |
| `run-script`: «no figura entre las skills sincronizadas» o «no hay skills… sincronizadas» | Repite `sync --tool antigravity --org <organización>`: la vía opcional solo ejecuta scripts de skills sincronizadas de esa organización |
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
- [ ] `sync --tool antigravity` crea `~/.gemini/config/skills/<organización>-<skill>/SKILL.md` (carpetas 0700, ficheros 0600) y
      `.baas-skills-<organización>.json`, **copia también los scripts** (`scripts/…`, 0600; 0700 solo los marcados como
      ejecutables), cuenta las llamadas reescritas, imprime la regla de permisos sugerida y repetirlo dice «Todo al día».
- [ ] `sync --tool antigravity --verify` sale limpio (código 0); edita a mano un script y vuelve a lanzarlo: lo señala y
      sale con 9 sin tocar nada. `--force` lo restaura.
- [ ] **CLI**: tras `/skills reload`, `/skills` muestra la skill y **`/<organización>-<skill>`** la lanza. Anota si el comando lleva otro
      prefijo distinto de `/<organización>-<skill>` o si el `description` reescrito basta para que el agente la elija solo.
- [ ] **Extensión**: anota si la extensión ofrece las skills como comando (`/<skill>`, menú de skills…) o solo las usa
      el agente por su cuenta.
- [ ] **Dos organizaciones** (si las tienes) con una skill del mismo nombre: `sync --tool antigravity --org <a>` y
      `--org <b>` dejan `<a>-<skill>` y `<b>-<skill>`; retirar una no toca la otra. Con el MCP de solo una, el agente de
      la otra skill comprueba `baas_whoami` y se detiene.
- [ ] **`contador` en `agy`** (la skill que tardó ~55 s): ya no tiene que improvisar. Su `SKILL.md` en
      `~/.gemini/config/skills/<organización>-contador/` lleva `python3 "<carpeta>/scripts/contador.py"` (la ruta absoluta
      de su carpeta, ya no la relativa) y la nota de los scripts. Pídele `/<organización>-contador`: anota cuánto tarda y
      si `agy` pide salir del sandbox. El contador escribe `~/.local/state/contador/valor`, **el mismo fichero que usa
      Claude Code**: el valor se comparte, no te sorprenda.
- [ ] **Sandbox**: si `agy` pide salir del sandbox, o falla con «No such file or directory» (127) porque `python3` es un
      envoltorio de tu HOME, añade la regla que imprime `sync --tool antigravity --permissions` a `permissions.allow` de
      `~/.gemini/antigravity-cli/settings.json` y repite: ¿deja de preguntar solo para esos scripts? ¿casa la regla con
      la orden entrecomillada? Anota la sintaxis que funcione (la documentación no la aclara). Comprueba lo mismo en la
      extensión de VS Code.
- [ ] **`gestor` en tu proyecto de cerebro** (el que tiene `knowledge/fiscal/…`; lanza `agy` desde él o ábrelo en VS Code):
      el `SKILL.md` de `~/.gemini/config/skills/<organización>-gestor/` ya no contiene `${CLAUDE_SKILL_DIR}` en las
      órdenes (ahora son `python3 "<carpeta>/scripts/gestor-facturas.py" …`), los enlaces a `facturacion` y `md-to-doc`
      llevan el prefijo y las citas `/gestor` pasan a `/<organización>-gestor`.
- [ ] Pídele al agente `/<organización>-gestor` «estado del año»: ejecuta la orden **tal cual** desde la carpeta de la
      skill, sin copiar ningún script ni llamar a `baas_get_script`, lee el cerebro del directorio de trabajo y responde.
- [ ] Lo mismo con `facturacion`, `md-to-doc`, `smart-tech` y `auditor` (`auditor.sh` es una llamada directa): anota
      cuáles siguen avisando de referencias sin reescribir y si eso estorba.
- [ ] Mueve o renombra `~/.gemini` (o un enlace de tus *dotfiles*) y repite `sync`: solo se vuelven a adaptar las skills
      que dependen de la ruta y sus órdenes llevan la ruta nueva.
- [ ] Una skill tuya con el mismo nombre queda intacta y el comando dice «no la toco»; una skill sincronizada que editas
      no se pisa sin `--force`.
- [ ] Quitar un rol (o retirar la skill) la borra en la siguiente sincronización; las tuyas siguen ahí.
- [ ] Windows: `%USERPROFILE%\.gemini\config\skills\`, los nombres y las rutas salen bien; las órdenes reescritas usan `python` y la carpeta de la skill entre comillas funciona (con espacios).
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
| ¿La CLI las lanza como `/<nombre>` (el de la carpeta, `<organización>-<skill>`) o con otro prefijo? | `/skills` en `agy` y prueba `/<skill>`. El plugin pone `name` = carpeta y no inventa otros prefijos |
| ¿Qué sintaxis exacta tiene la regla `command(...)` con rutas entrecomilladas o con espacios, y admite `*` dentro de un patrón? | Lo que dice la documentación es solo prefijo palabra a palabra y `*` global. Prueba la regla sugerida en `agy` y anota qué órdenes casan |
| ¿Cómo se comporta el sandbox en la extensión de VS Code? | Ejecuta una skill con scripts desde la extensión: ¿falla igual con `python3` envoltorio? ¿pide salir? |
| ¿Usa el campo `instructions` del MCP? | La documentación solo dice que lo guarda en una caché (`instructions.md`); mira si el agente lo recibe sin la regla (borra `.agents/rules/baas.md` y pregúntale qué tools tiene) |

Fuentes: <https://antigravity.google/docs/mcp>, <https://antigravity.google/docs/rules>, <https://antigravity.google/docs/skills>.
