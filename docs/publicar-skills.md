# Publicar y exportar skills

Una skill es un **paquete**: `SKILL.md` más scripts, plantillas y ficheros de contexto, en el formato que ya uses
(Claude Code, Cursor…). Publicarla es subirla al MCP de la organización para que el resto del equipo la use con los
roles que decidas. Exportarla es la operación inversa. Diseño en `knowledge/01-producto/fases/fase-1-h5-diseno.md`
(§5 el modelo, §8 la publicación guiada) y contrato de las tools en `knowledge/02-arquitectura/contratos-fase-1.md` §7.4.

Solo pueden publicar los **ORG_ADMIN** y los **PLATFORM_ADMIN**. Quien tenga el rol recibe la skill con
`/baas-sync`. Sin límites de tamaño ni de extensión: el plugin solo **avisa** (`LARGE_FILE` desde 25 MiB,
`LARGE_PACKAGE` desde 100 MiB).

## El camino corto: `/baas-publish`

```text
/baas-publish brain/.agents/skills/facturacion
```

La skill guía una entrevista corta, apoyada en `baas.py inspect`:

1. **Qué ruta subir**: una carpeta con `SKILL.md`, y dónde están sus scripts y su conocimiento si viven fuera.
2. **Qué scripts son dependencia**: propone los que cita la skill y los que ellos importan o llaman, y pregunta por
   cada duda.
3. **De qué otras skills depende** (`../gestor/SKILL.md`, `/md-to-doc`) y cuáles están ya publicadas.
4. **De dónde sale el conocimiento**: busca antes en el MCP (`baas_list_files`, `baas_search_knowledge`) y pregunta
   qué rutas subir y con qué roles.
5. **Qué roles**: `*` (cualquier miembro de la organización) o los que nombres.
6. **Plan y ensayo** (`--dry-run`): ficheros, tamaño total, tráfico estimado y avisos.
7. **Publica en un lote atómico** y **verifica** que los hashes del servidor coinciden.

Ante `AUTHORIZATION_DENIED` o `KILL_SWITCH_ACTIVE` la skill se detiene e informa; no busca otra vía.

## Las órdenes (también para un *script* o una CI)

```bash
python3 scripts/baas.py inspect <dir> [--scripts RUTA] [--knowledge DIR] [--json]
python3 scripts/baas.py publish <dir> [--org SLUG] [--roles R1,R2|'*'] [--include-scripts RUTA]... \
        [--knowledge DIR:PREFIJO]... [--knowledge-roles R1,R2] [--depends-on-skills S1,S2] [--prune] \
        [--rewrite-paths] [--dry-run] [--yes] [--json]
python3 scripts/baas.py export <skill> <dir> [--org SLUG] [--force] [--no-manifest] [--json]
```

Funcionan con la API key y la organización del proyecto vinculado (`/baas-init`). Códigos de salida: `0` bien ·
`2` configuración o argumento (también «falta confirmar») · `9` un hash no coincide (tras publicar, o al exportar) ·
`10` error del MCP, con su código en `[BAAS_MCP_<código>]`.

### `inspect`: qué hay en esa skill (solo lectura)

Informe mecánico (texto, o JSON con `--json`) de una carpeta de skill y de los scripts (`--scripts`) y el
conocimiento (`--knowledge`) que se le asocien:

| Sección | Qué detecta |
| :--- | :--- |
| Ficheros | Tipos y tamaños, texto o binario, ejecutables, ocultos y enlaces simbólicos (los internos se siguen; los que apuntan fuera del árbol **se omiten**) |
| Cabecera | `name`, `description`, `hooks` y `allowed-tools` (código y permisos que viajan), cabecera que no es YAML estricto |
| Scripts | Imports entre scripts propios, llamadas por subproceso o shell, paquetes de terceros, y el **cierre** de lo que cita la skill: scripts propuestos, no relacionados y los que faltan |
| Rutas calculadas | `__file__`, `BASH_SOURCE`, `dirname $0`, `__dirname` que **salen del paquete** y de qué rutas cuelgan |
| Enlaces | A otras skills (`../x/SKILL.md`, `/x`), a scripts (`../../../scripts/…`), a `knowledge/…` y enlaces rotos |
| Binarios externos | `typst`, `mmdc`, `node`, `pandoc`… (a `requires` de `baas.yaml`) |
| Estado mutable | Ficheros que los scripts escriben bajo el paquete (no pueden viajar: pasa la ruta como argumento) |
| Datos sensibles | NIF, DNI, NIE, CIF e IBAN (con su dígito de control), correos, contraseñas, *tokens*, claves privadas y ficheros `.env`/claves. **Siempre enmascarados** en el informe |
| No portable | `.js` de consola de navegador, rutas absolutas del equipo del autor, binarios nativos, scripts de Windows |
| Reescrituras | Propuestas, **sin aplicar** |

El análisis lee como máximo 16 MiB de cada fichero. Es una aproximación honesta (análisis estático): ayuda a decidir,
no sustituye a revisar.

### `publish`: subir en un lote

- **Recorre el árbol** con los bytes exactos: `utf8` si el contenido es texto UTF-8 válido sin NUL y `base64` si no;
  `sha256` de los bytes. Se ignoran los residuos habituales (`.git`, `__pycache__`, `*.pyc`, `.DS_Store`,
  `node_modules`, `.venv`…; `--exclude PATRÓN` añade más y `--no-default-excludes` los incluye). Los ficheros ocultos
  (`.gitkeep`, `.env.example`) se suben; `.env`, claves y almacenes **no** (`--include-secrets` lo permite).
- **Dónde cae cada cosa**: la carpeta de la skill → `skills/<skill>/…`; `--include-scripts RUTA` (un fichero o una
  carpeta, repetible) → `skills/<skill>/scripts/…`; `--knowledge DIR:PREFIJO` → `knowledge/PREFIJO/…` (el `knowledge/`
  inicial del prefijo es opcional). La carpeta de la skill puede traer ya su propio `scripts/`.
- **`baas.yaml`**: se genera (o se actualiza) con `version: 1`, `roles`, `requires` (intérpretes y binarios
  detectados), `depends_on` (skills y conocimiento citado que se sube) y `executable` (ficheros con bit de ejecución
  o `#!`). Lo que ya diga tu `baas.yaml` local —o el publicado— manda sobre lo detectado, y las claves desconocidas se
  conservan. El `baas.yaml` local **no se modifica**: se sube una versión generada.
- **Sin pisar cambios ajenos**: solo se envía lo que cambia, con `expected_sha256` (el hash que había, o `"none"` si es
  nuevo). Si alguien cambió algo entre el plan y la escritura, el MCP responde `CONFLICT` con **todos** los ficheros
  afectados y no escribe nada. `--prune` rellena `delete[]` con lo que existe en el servidor y ya no en local (la
  versión anterior se conserva).
- **`--dry-run`** pide al MCP que valide y calcule (`dry_run: true`) sin escribir: lista los ficheros, el tamaño total,
  el tráfico estimado de la subida y de cada sincronización, y los avisos (`NO_ROLES`, `DEPENDENCY_MISSING`,
  `FRONTMATTER_HOOKS`…). Sin `--yes`, `publish` hace ese ensayo, enseña el plan y pregunta; sin terminal interactiva
  se niega (usa `--yes`).
- **Un único lote atómico** (`baas_write_files`): todo o nada. Al terminar se vuelve a leer el paquete y se comprueba
  el `sha256` de cada fichero (y de los documentos de conocimiento).
- **Roles del conocimiento** (`--knowledge-roles`): para una carpeta se escribe `knowledge/PREFIJO/_acl.yaml` (los
  roles por defecto de **todo ese directorio**: el plan lo avisa); para un `.md` suelto, se añaden a la cabecera de la
  copia que se sube (si ya declara `roles`, no se tocan).
- **`--rewrite-paths`** aplica a la copia que se sube las reescrituras que propone `inspect`; **nunca por defecto y
  nunca sobre el original**:

  | Reescritura | Antes | Después |
  | :--- | :--- | :--- |
  | Enlace a un script | `[x](../../../scripts/a.py)` | `[x](scripts/a.py)` |
  | Orden en `SKILL.md` | `python3 scripts/a.py` | `python3 "${CLAUDE_SKILL_DIR}/scripts/a.py"` |
  | Raíz en Python | `ROOT = Path(__file__).resolve().parent.parent` | `ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()` |
  | Raíz en shell | `ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"` | `ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"` |

  Solo se reescriben las asignaciones de nivel de módulo que salen de `scripts/`; el resto (`sys.path.insert(0,
  str(Path(__file__).resolve().parent))`) se conserva. La raíz pasa a ser el proyecto del usuario: **revisa** que sea
  lo que el script espera, porque el conocimiento (`knowledge/`) no viaja con la skill.

### `export`: la inversa

`export <skill> <dir>` descarga el paquete (`baas_get_skill_package` + `baas_get_skill_files`, en trozos), verifica el
`sha256` de cada fichero **antes** de dejar nada en su sitio y restaura el bit de ejecución según `baas.yaml`. El
resultado es una skill válida en cualquier cliente (`baas.yaml` lo ignoran; `--no-manifest` lo omite). Escribe con las
rutas confinadas al destino: se rechazan `..`, rutas absolutas, `\`, caracteres de control y enlaces simbólicos, y un
destino no vacío exige `--force` (que respeta lo que no es del paquete).

## Qué no hace

- No modifica el original en ningún caso. Los datos sensibles se **avisan**, no se retiran: decide el administrador.
- No sube el estado mutable de los scripts ni el conocimiento que no indiques con `--knowledge`.
- El conocimiento de `--knowledge` se sube tal cual: puede tener datos reales, revisa el plan y el informe de `inspect`.
- Con paquetes enormes todo el contenido se prepara en memoria: para varios GiB conviene dividir la publicación en
  varias skills o carpetas.

## Pruebas

`tests/test_inspect.py`, `test_publish.py`, `test_export.py`, `test_cli_publish.py`, `test_skillmeta.py` y
`test_skills_publish.py`, con una copia **sintética** de los patrones de la skill `facturacion` real
(`tests/skill_fixture.py`) y un MCP simulado propio (`tests/fake_publish_server.py`, sin depender de
`dev/fake_baas.py`) que implementa `baas_write_files` (con `dry_run`, `expected_sha256`, `delete` y todos los fallos),
`baas_get_skill_package`, `baas_get_skill_files`, `baas_list_files` y `baas_whoami`.
