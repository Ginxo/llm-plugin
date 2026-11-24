"""Reescritura determinista del `SKILL.md` de Claude Code para las skills nativas de Antigravity (T20 · #194).

Funciones puras: sin servidor, sin disco. Los fragmentos son sintéticos (sin datos reales).
"""

from __future__ import annotations

import pytest

from baas_plugin.skill_rewrite import RewriteContext, RunnerCommand, rewrite, safe_runner

BAAS_PY = "/opt/baas plugin/scripts/baas.py"  # con espacios a propósito
RUNNER = RunnerCommand("python3", BAAS_PY, "antigravity")
SCRIPTS = frozenset(
    {
        "scripts/gestor-facturas.py",
        "scripts/lib/util.py",
        "scripts/run.sh",
        "scripts/app.js",
        "scripts/sin-extension",
        "SKILL.md",
    }
)
SYNCED = frozenset({"gestor", "facturacion", "md-to-doc"})


def ctx(**overrides) -> RewriteContext:
    base = {
        "org": "demo",
        "skill": "gestor",
        "synced": SYNCED,
        "scripts": SCRIPTS,
        "runner": RUNNER,
        "executable": frozenset({"scripts/sin-extension"}),
    }
    return RewriteContext(**{**base, **overrides})


def cmd(rel: str, rest: str = "") -> str:
    """El comando reescrito para `rel` (el que debe producir el adaptador)."""
    return f'python3 "{BAAS_PY}" run-script --tool antigravity --synced-org demo skills/gestor/{rel} --{rest}'


def fence(*lines: str) -> str:
    return "```bash\n" + "\n".join(lines) + "\n```\n"


# --- invocaciones de scripts -----------------------------------------------------------------------


def test_a_double_quoted_python_call_in_a_code_block_becomes_run_script_and_keeps_its_arguments() -> None:
    src = fence(
        'python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" mark-paid --id "2026-11/001" --date 2026-11-15'
    )
    out, report = rewrite(src, ctx())
    assert out == fence(cmd("scripts/gestor-facturas.py", ' mark-paid --id "2026-11/001" --date 2026-11-15'))
    assert "CLAUDE_SKILL_DIR" not in out
    assert report.scripts == ["scripts/gestor-facturas.py"]


@pytest.mark.parametrize(
    "path",
    [
        '"${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py"',
        "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py",
        '"$CLAUDE_SKILL_DIR/scripts/gestor-facturas.py"',
        "$CLAUDE_SKILL_DIR/scripts/gestor-facturas.py",
    ],
)
def test_quoted_unquoted_and_braceless_variants_are_recognised(path: str) -> None:
    out, _ = rewrite(fence(f"python3 {path} status --year 2026"), ctx())
    assert out == fence(cmd("scripts/gestor-facturas.py", " status --year 2026"))


def test_a_call_without_arguments_still_ends_with_the_separator() -> None:
    out, _ = rewrite(fence('python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py"'), ctx())
    assert out == fence(cmd("scripts/gestor-facturas.py"))


def test_arguments_are_never_reinterpreted() -> None:
    args = " --name 'O\\'Brien' --x \"a  b\" $(date) `id` > out.txt 2>&1"
    out, _ = rewrite(fence(f'python3 "${{CLAUDE_SKILL_DIR}}/scripts/gestor-facturas.py"{args}'), ctx())
    assert out == fence(cmd("scripts/gestor-facturas.py", args))


def test_indentation_a_prompt_line_continuations_and_pipes_are_kept() -> None:
    src = fence(
        '   python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" create \\',
        "      --id x",
        '$ python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" list | head',
        'cd x && python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" audit',
        'echo hola | python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" stdin',
    )
    out, report = rewrite(src, ctx())
    expected = fence(
        "   " + cmd("scripts/gestor-facturas.py", " create \\"),
        "      --id x",
        "$ " + cmd("scripts/gestor-facturas.py", " list | head"),
        "cd x && " + cmd("scripts/gestor-facturas.py", " audit"),
        "echo hola | " + cmd("scripts/gestor-facturas.py", " stdin"),
    )
    assert out == expected
    assert len(report.scripts) == 4


def test_a_call_inside_an_inline_code_span_is_rewritten() -> None:
    src = "Para crearla: `python3 ${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py --out x` y listo.\n"
    out, _ = rewrite(src, ctx())
    assert out == f"Para crearla: `{cmd('scripts/gestor-facturas.py', ' --out x')}` y listo.\n"


def test_a_direct_call_is_rewritten_in_a_code_block_and_in_a_span_with_arguments() -> None:
    out, _ = rewrite(
        fence('"${CLAUDE_SKILL_DIR}/scripts/run.sh" init <slug>', "${CLAUDE_SKILL_DIR}/scripts/run.sh"), ctx()
    )
    assert out == fence(cmd("scripts/run.sh", " init <slug>"), cmd("scripts/run.sh"))
    out, _ = rewrite("Ejecuta `${CLAUDE_SKILL_DIR}/scripts/run.sh init <slug>` (ver §8).\n", ctx())
    assert out == f"Ejecuta `{cmd('scripts/run.sh', ' init <slug>')}` (ver §8).\n"


def test_a_direct_call_needs_an_extension_run_script_knows_or_a_declared_executable() -> None:
    out, _ = rewrite(fence('"${CLAUDE_SKILL_DIR}/scripts/sin-extension" x'), ctx())
    assert out == fence(cmd("scripts/sin-extension", " x")), "declarado ejecutable en el paquete"
    out, _ = rewrite(fence('"${CLAUDE_SKILL_DIR}/scripts/sin-extension" x'), ctx(executable=frozenset()))
    assert "CLAUDE_SKILL_DIR" in out


def test_a_lone_path_in_prose_a_heading_or_a_span_is_a_mention_not_a_command() -> None:
    src = (
        "### CLI (`${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py`)\n"
        "Usa `${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py` como primer filtro.\n"
        "Y también ${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py en texto.\n"
        '# comentario: "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" aquí\n'
    )
    out, report = rewrite(src, ctx())
    assert out == src
    assert report.unresolved == 4
    assert report.scripts == []


@pytest.mark.parametrize(
    ("interpreter", "script", "rewritten"),
    [
        ("python3", "scripts/gestor-facturas.py", True),
        ("python", "scripts/gestor-facturas.py", True),
        ("bash", "scripts/run.sh", True),
        ("sh", "scripts/run.sh", True),
        ("node", "scripts/app.js", True),
        ("python3", "scripts/run.sh", False),  # intérprete que no corresponde al script
        ("node", "scripts/gestor-facturas.py", False),
        ("bash", "scripts/gestor-facturas.py", False),
        ("python3", "scripts/sin-extension", False),  # el ejecutor no sabría con qué ejecutarlo
    ],
)
def test_the_interpreter_must_match_what_run_script_would_use(interpreter: str, script: str, rewritten: bool) -> None:
    src = fence(f'{interpreter} "${{CLAUDE_SKILL_DIR}}/{script}" a')
    out, _ = rewrite(src, ctx())
    assert (out != src) is rewritten
    if rewritten:
        assert f"skills/gestor/{script} -- a" in out


@pytest.mark.parametrize(
    "line",
    [
        'python3 -u "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" x',  # opciones entre el intérprete y el script
        'FOO=1 python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" x',  # variables de entorno delante
        'time python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" x',
        "python3 '${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py' x",  # comillas simples: el shell no expandiría
        'python3 "${CLAUDE_SKILL_DIR}/scripts/otro.py" x',  # no está en el paquete
        'python3 "${CLAUDE_SKILL_DIR}/scripts/../gestor-facturas.py" x',
        'python3 "${CLAUDE_SKILL_DIR}/scripts/./gestor-facturas.py" x',
        'python3 "${CLAUDE_SKILL_DIR}/../facturacion/scripts/x.py" x',  # de otra skill
        'python3 "${CLAUDE_SKILL_DIR}/scripts/$NOMBRE.py" x',
        'python3 "${CLAUDE_SKILL_DIR}/scripts/a b.py" x',
        'python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py"extra x',
        'python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py',  # comilla sin cerrar
        "python3 scripts/gestor-facturas.py x",  # ruta relativa: no se sabe a qué carpeta se refiere
        'python3 "${CLAUDE_SKILL_DIR}/templates/informe.py" x',  # no está bajo scripts/
    ],
)
def test_what_is_not_recognised_with_certainty_is_left_intact(line: str) -> None:
    src = fence(line)
    out, report = rewrite(src, ctx())
    assert out == src
    assert report.scripts == []


def test_the_leftovers_are_counted_so_the_sync_can_warn() -> None:
    src = fence(
        'python3 "${CLAUDE_SKILL_DIR}/scripts/otro.py" x',
        "python3 scripts/gestor-facturas.py x",
        'python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" ok',
    )
    _, report = rewrite(src, ctx())
    assert report.unresolved == 1
    assert report.relative_scripts == 1
    assert report.scripts == ["scripts/gestor-facturas.py"]
    assert report.runner_candidates == 2, "cuenta lo que parece una llamada, se reescriba o no"


def test_without_a_usable_runner_nothing_is_rewritten() -> None:
    src = fence('python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" x')
    out, report = rewrite(src, ctx(runner=None))
    assert out == src
    assert report.unresolved == 1


@pytest.mark.parametrize(
    "bad", ['/opt/a"b/baas.py', "/opt/$HOME/baas.py", "/opt/`id`/baas.py", "/opt/a\nb/baas.py", "/opt/a%b/baas.py"]
)
def test_a_baas_py_path_with_shell_special_characters_is_refused(bad: str) -> None:
    assert safe_runner("python3", bad, "antigravity") is None


def test_paths_with_spaces_parentheses_and_windows_separators_are_accepted() -> None:
    win = safe_runner("python", "C:\\Program Files (x86)\\baas plugin\\scripts\\baas.py", "antigravity", windows=True)
    assert win is not None
    out, _ = rewrite(fence('python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" x'), ctx(runner=win))
    assert out.startswith('```bash\npython "C:\\Program Files (x86)\\baas plugin\\scripts\\baas.py" run-script')
    assert safe_runner("python3", "/home/a b/(x)/baas.py", "antigravity") is not None


def test_a_backslash_in_a_posix_path_is_refused() -> None:
    assert safe_runner("python3", "/opt/a\\b/baas.py", "antigravity", windows=False) is None
    assert safe_runner("python", "C:\\a\\baas.py", "antigravity", windows=True) is not None


def test_the_interpolated_names_cannot_inject_anything() -> None:
    for kwargs in ({"org": 'demo"; rm -rf /; "'}, {"skill": "ges tor"}, {"org": "Demo"}, {"skill": "../x"}):
        with pytest.raises(ValueError, match="no válido"):
            rewrite(fence('python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" x'), ctx(**kwargs))


# --- enlaces y citas -------------------------------------------------------------------------------


def test_links_to_other_synced_skills_get_the_prefixed_folder_and_keep_the_fragment() -> None:
    src = (
        "Ver [la guía](../facturacion/SKILL.md) y [otra](../md-to-doc/SKILL.md#uso)"
        " y [d](../facturacion/references/a.md).\n"
    )
    out, report = rewrite(src, ctx())
    assert out == (
        "Ver [la guía](../demo-facturacion/SKILL.md) y [otra](../demo-md-to-doc/SKILL.md#uso)"
        " y [d](../demo-facturacion/references/a.md).\n"
    )
    assert report.links == 3


def test_links_to_unsynced_skills_to_the_brain_and_to_the_own_skill_are_not_touched() -> None:
    src = (
        "[x](../otra/SKILL.md) [k](../../../knowledge/fiscal/ledger.json) [s](scripts/x.py) [t](./templates/a.docx)\n"
        "[abs](/gestor/SKILL.md) [web](https://example.com/../facturacion/SKILL.md)\n"
    )
    out, report = rewrite(src, ctx())
    assert out == src
    assert report.links == 0


def test_citations_that_match_a_synced_skill_exactly_are_prefixed() -> None:
    src = "Usa `/gestor` y [`/facturacion`](../facturacion/SKILL.md) o [/md-to-doc](../md-to-doc/SKILL.md).\n"
    out, report = rewrite(src, ctx())
    assert out == (
        "Usa `/demo-gestor` y [`/demo-facturacion`](../demo-facturacion/SKILL.md)"
        " o [/demo-md-to-doc](../demo-md-to-doc/SKILL.md).\n"
    )
    assert report.citations == 3


@pytest.mark.parametrize(
    "src",
    [
        "Usa `/otra` (no sincronizada).\n",
        "Ruta `/etc` o `/gestor/x` o `a/gestor` o `/gestor.py`.\n",
        "Texto con /gestor sin comillas de código.\n",
        "Mayúsculas `/Gestor`.\n",
        "Un `/gestor --flag` con más cosas.\n",
    ],
)
def test_citations_that_do_not_match_exactly_a_synced_skill_are_left_alone(src: str) -> None:
    out, report = rewrite(src, ctx())
    assert out == src
    assert report.citations == 0


def test_nothing_is_rewritten_inside_fenced_blocks_except_the_script_calls() -> None:
    src = (
        "```markdown\n"
        "Ejemplo ajeno: [x](../facturacion/SKILL.md) y `/gestor`\n"
        '${CLAUDE_SKILL_DIR}/templates/a.docx y python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" ok\n'
        "```\n"
        "~~~\n[y](../gestor/SKILL.md)\n~~~\n"
    )
    out, _ = rewrite(src, ctx())
    assert "(../facturacion/SKILL.md)" in out
    assert "`/gestor`" in out
    assert "(../gestor/SKILL.md)" in out
    assert "${CLAUDE_SKILL_DIR}/templates/a.docx" in out


def test_links_inside_inline_code_are_examples_not_links() -> None:
    src = "Escribe `[x](../facturacion/SKILL.md)` tal cual.\n"
    out, _ = rewrite(src, ctx())
    assert out == src


def test_reference_style_definitions_are_rewritten() -> None:
    src = "Ver [fact][f].\n\n[f]: ../facturacion/SKILL.md\n"
    out, _ = rewrite(src, ctx())
    assert out == "Ver [fact][f].\n\n[f]: ../demo-facturacion/SKILL.md\n"


def test_an_unclosed_fence_swallows_the_rest_and_nothing_else_is_rewritten_there() -> None:
    src = "```\n[x](../facturacion/SKILL.md)\n"
    out, _ = rewrite(src, ctx())
    assert out == src


def test_the_report_lists_the_candidate_names_whether_or_not_they_are_synced() -> None:
    src = "[x](../otra/SKILL.md) [y](../facturacion/SKILL.md) `/tampoco` `/gestor`\n"
    _, report = rewrite(src, ctx())
    assert report.refs == ["facturacion", "gestor", "otra", "tampoco"]


# --- propiedades ---------------------------------------------------------------------------------


def test_rewriting_is_deterministic_and_idempotent() -> None:
    src = (
        "Ver [f](../facturacion/SKILL.md) y `/gestor`.\n"
        + fence('python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" list', "python3 scripts/x.py")
        + "Tras: `python3 ${CLAUDE_SKILL_DIR}/scripts/otro.py`.\n"
    )
    once, _ = rewrite(src, ctx())
    again, _ = rewrite(src, ctx())
    assert once == again
    twice, report = rewrite(once, ctx())
    assert twice == once, "lo ya reescrito no vuelve a cambiar"
    assert report.scripts == []


def test_crlf_files_keep_their_line_endings() -> None:
    src = (
        "[f](../facturacion/SKILL.md)\r\n```bash\r\n"
        'python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" x\r\n```\r\n'
    )
    out, _ = rewrite(src, ctx())
    assert out == (
        "[f](../demo-facturacion/SKILL.md)\r\n```bash\r\n" + cmd("scripts/gestor-facturas.py", " x") + "\r\n```\r\n"
    )


def test_a_document_without_anything_to_rewrite_is_returned_unchanged() -> None:
    src = "# Título\n\nTexto normal con `código` y [web](https://example.com).\n"
    out, report = rewrite(src, ctx())
    assert out == src
    assert (report.scripts, report.links, report.citations, report.unresolved) == ([], 0, 0, 0)


@pytest.mark.parametrize(
    "hostile", ["scripts/../gestor-facturas.py", "scripts/./gestor-facturas.py", "scripts/a/../../x.py"]
)
def test_dot_segments_are_refused_even_if_a_manifest_listed_them(hostile: str) -> None:
    """El manifiesto ya los rechaza al leerlo; aquí se comprueba la segunda barrera, la de la propia orden."""
    src = fence(f'python3 "${{CLAUDE_SKILL_DIR}}/{hostile}" x')
    out, report = rewrite(src, ctx(scripts=SCRIPTS | {hostile}))
    assert out == src
    assert report.scripts == []
