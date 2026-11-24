"""Reescritura determinista del `SKILL.md` de Claude Code para las skills nativas de Antigravity (T20 y T22).

Funciones puras: sin servidor, sin disco. Los fragmentos son sintéticos (sin datos reales).

Los scripts se copian a la carpeta nativa de la skill (verificados al sincronizar), así que `${CLAUDE_SKILL_DIR}` y las
llamadas con ruta relativa pasan a la ruta absoluta de esa carpeta, bien entrecomillada.
"""

from __future__ import annotations

import pytest

from baas_plugin.skill_rewrite import RewriteContext, rewrite, safe_dir

DIR = "/home/u/.gemini/config/skills/demo-gestor"
SPACED = "/home/mi usuario/(x)/.gemini/config/skills/demo-gestor"
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
        "skill_dir": DIR,
        "executable": frozenset({"scripts/sin-extension"}),
    }
    return RewriteContext(**{**base, **overrides})


def fence(*lines: str) -> str:
    return "```bash\n" + "\n".join(lines) + "\n```\n"


# --- ${CLAUDE_SKILL_DIR} -----------------------------------------------------------------------------


def test_a_double_quoted_call_gets_the_absolute_folder_and_keeps_its_arguments() -> None:
    line = 'python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" mark-paid --id "2026-11/001" --date 2026-11-15'
    out, report = rewrite(fence(line), ctx())
    assert out == fence(f'python3 "{DIR}/scripts/gestor-facturas.py" mark-paid --id "2026-11/001" --date 2026-11-15')
    assert "CLAUDE_SKILL_DIR" not in out
    assert report.calls == [("python3", "scripts/gestor-facturas.py")]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ('"${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py"', f'"{DIR}/scripts/gestor-facturas.py"'),
        ("${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py", f"{DIR}/scripts/gestor-facturas.py"),
        ('"$CLAUDE_SKILL_DIR/scripts/gestor-facturas.py"', f'"{DIR}/scripts/gestor-facturas.py"'),
        ("$CLAUDE_SKILL_DIR/scripts/gestor-facturas.py", f"{DIR}/scripts/gestor-facturas.py"),
    ],
)
def test_quoted_unquoted_and_braceless_variants_are_all_resolved(path: str, expected: str) -> None:
    out, _ = rewrite(fence(f"python3 {path} status --year 2026"), ctx())
    assert out == fence(f"python3 {expected} status --year 2026")


def test_a_folder_with_spaces_is_quoted_when_the_original_was_not() -> None:
    src = fence(
        "python3 ${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py a",
        'python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" b',
    )
    out, _ = rewrite(src, ctx(skill_dir=SPACED))
    assert out == fence(
        f'python3 "{SPACED}/scripts/gestor-facturas.py" a',
        f'python3 "{SPACED}/scripts/gestor-facturas.py" b',
    )


def test_an_unquoted_variable_followed_by_a_quote_in_a_folder_with_spaces_is_left_alone() -> None:
    src = fence('python3 ${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py"extra" a')
    out, report = rewrite(src, ctx(skill_dir=SPACED))
    assert out == src, "no se puede entrecomillar con certeza"
    assert report.unresolved == 1


def test_single_quotes_are_left_alone_because_the_shell_would_not_expand_them() -> None:
    src = fence("python3 '${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py' x")
    out, report = rewrite(src, ctx())
    assert out == src
    assert report.unresolved == 1


def test_a_call_without_arguments_and_arguments_with_shell_syntax_are_not_touched() -> None:
    out, _ = rewrite(fence('python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py"'), ctx())
    assert out == fence(f'python3 "{DIR}/scripts/gestor-facturas.py"')
    args = " --name 'O\\'Brien' --x \"a  b\" $(date) `id` > out.txt 2>&1"
    out, _ = rewrite(fence(f'python3 "${{CLAUDE_SKILL_DIR}}/scripts/gestor-facturas.py"{args}'), ctx())
    assert out == fence(f'python3 "{DIR}/scripts/gestor-facturas.py"{args}')


def test_other_paths_of_the_skill_folder_in_the_arguments_are_resolved_too() -> None:
    src = fence(
        'python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" --template "${CLAUDE_SKILL_DIR}/templates/x.typ"'
    )
    out, _ = rewrite(src, ctx())
    assert out == fence(f'python3 "{DIR}/scripts/gestor-facturas.py" --template "{DIR}/templates/x.typ"')


def test_indentation_a_prompt_line_continuations_and_pipes_are_kept() -> None:
    src = fence(
        '   python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" create \\',
        "      --id x",
        '$ python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" list | head',
        'cd x && python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" audit',
        'echo hola | python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" stdin',
    )
    out, report = rewrite(src, ctx())
    p = f'python3 "{DIR}/scripts/gestor-facturas.py"'
    expected = fence(
        f"   {p} create \\", "      --id x", f"$ {p} list | head", f"cd x && {p} audit", f"echo hola | {p} stdin"
    )
    assert out == expected
    assert len(report.calls) == 4


def test_a_direct_call_is_resolved_and_recorded_without_interpreter() -> None:
    out, report = rewrite(fence('"${CLAUDE_SKILL_DIR}/scripts/run.sh" init <slug>'), ctx())
    assert out == fence(f'"{DIR}/scripts/run.sh" init <slug>')
    assert report.calls == [(None, "scripts/run.sh")]


def test_calls_in_inline_code_and_mentions_everywhere_are_resolved() -> None:
    src = (
        "### CLI (`${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py`)\n"
        "Crea: `python3 ${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py --out x` y mira ${CLAUDE_SKILL_DIR}/a.md.\n"
    )
    out, report = rewrite(src, ctx())
    assert out == (
        f"### CLI (`{DIR}/scripts/gestor-facturas.py`)\n"
        f"Crea: `python3 {DIR}/scripts/gestor-facturas.py --out x` y mira {DIR}/a.md.\n"
    )
    assert report.unresolved == 0
    assert report.calls == [("python3", "scripts/gestor-facturas.py")], "una mención no es una llamada"


@pytest.mark.parametrize(
    ("interpreter", "script", "recorded"),
    [
        ("python3", "scripts/gestor-facturas.py", True),
        ("bash", "scripts/run.sh", True),
        ("sh", "scripts/run.sh", True),
        ("node", "scripts/app.js", True),
        ("python3", "scripts/run.sh", False),  # intérprete que no corresponde: se resuelve la ruta, no se anota
        ("python3", "scripts/no-esta.py", False),  # el script no está en el paquete
    ],
)
def test_only_calls_matching_a_script_of_the_package_are_recorded(
    interpreter: str, script: str, recorded: bool
) -> None:
    out, report = rewrite(fence(f'{interpreter} "${{CLAUDE_SKILL_DIR}}/{script}" a'), ctx())
    assert f'{interpreter} "{DIR}/{script}" a' in out
    assert bool(report.calls) is recorded


def test_the_folder_is_never_substituted_without_a_safe_absolute_path() -> None:
    src = fence('python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" x') + "[l](${CLAUDE_SKILL_DIR}/a.md)\n"
    for bad in (None, "relativa/dir", '/home/a"b', "/home/$HOME", "/home/a`id`", "/home/a%b"):
        out, report = rewrite(src, ctx(skill_dir=bad))
        assert out == src, bad
        assert report.unresolved == 2


@pytest.mark.parametrize("bad", ['/opt/a"b', "/opt/$HOME/x", "/opt/`id`", "/opt/a\nb", "/opt/a%b", "relativa", ""])
def test_a_folder_with_shell_special_characters_is_refused(bad: str) -> None:
    assert safe_dir(bad) is None


def test_paths_with_spaces_parentheses_and_windows_separators_are_accepted() -> None:
    assert safe_dir("/home/a b/(x)/skills/demo-x") == "/home/a b/(x)/skills/demo-x"
    assert safe_dir("C:\\Users\\Ana María\\.gemini\\config\\skills\\demo-x", windows=True) is not None
    assert safe_dir("/opt/a\\b", windows=False) is None, "una barra invertida en POSIX es un escape"
    assert safe_dir("relativa/dir", windows=True) is None


def test_on_windows_the_interpreter_of_the_recognised_calls_is_python() -> None:
    src = fence(
        'python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" a',
        'python3 "${CLAUDE_SKILL_DIR}/scripts/no-esta.py" b',
        'bash "${CLAUDE_SKILL_DIR}/scripts/run.sh" c',
    )
    win = "C:\\Users\\Ana\\skills\\demo-gestor"
    out, report = rewrite(src, ctx(windows=True, skill_dir=win))
    assert f'python "{win}/scripts/gestor-facturas.py" a' in out
    assert f'python3 "{win}/scripts/no-esta.py" b' in out, "solo las llamadas reconocidas"
    assert f'bash "{win}/scripts/run.sh" c' in out
    assert report.calls == [("python", "scripts/gestor-facturas.py"), ("bash", "scripts/run.sh")]


# --- rutas relativas «desde la carpeta de la skill» ------------------------------------------------------


def test_a_relative_call_is_resolved_when_the_same_line_says_to_run_it_from_the_skill_folder() -> None:
    src = "Ejecuta el script desde la carpeta de la skill: `python3 scripts/gestor-facturas.py`.\n"
    out, report = rewrite(src, ctx())
    assert out == f'Ejecuta el script desde la carpeta de la skill: `python3 "{DIR}/scripts/gestor-facturas.py"`.\n'
    assert report.calls == [("python3", "scripts/gestor-facturas.py")]
    assert report.relative_scripts == 0


@pytest.mark.parametrize(
    "hint",
    [
        "Ejecuta desde la carpeta de la skill:",
        "Desde el directorio de la skill:",
        "Run it from the skill directory:",
        "From the skill folder:",
    ],
)
def test_the_line_before_a_code_block_can_carry_the_hint(hint: str) -> None:
    src = f"{hint}\n\n```bash\npython3 scripts/gestor-facturas.py status\n./scripts/run.sh init\n```\n"
    out, report = rewrite(src, ctx())
    assert f'python3 "{DIR}/scripts/gestor-facturas.py" status' in out
    assert f'"{DIR}/scripts/run.sh" init' in out
    assert [c[1] for c in report.calls] == ["scripts/gestor-facturas.py", "scripts/run.sh"]


def test_cd_to_the_skill_folder_on_the_same_line_or_earlier_in_the_block_sets_the_directory() -> None:
    src = fence(
        'cd "${CLAUDE_SKILL_DIR}" && python3 scripts/gestor-facturas.py a',
        "cd ${CLAUDE_SKILL_DIR}; python3 scripts/gestor-facturas.py b",
    )
    out, _ = rewrite(src, ctx())
    assert out == fence(
        f'cd "{DIR}" && python3 "{DIR}/scripts/gestor-facturas.py" a',
        f'cd {DIR}; python3 "{DIR}/scripts/gestor-facturas.py" b',
    )
    block = fence(
        'cd "${CLAUDE_SKILL_DIR}"',
        "python3 scripts/gestor-facturas.py c",
        "cd /otro/sitio",
        "python3 scripts/gestor-facturas.py d",
    )
    out, report = rewrite(block, ctx())
    assert f'python3 "{DIR}/scripts/gestor-facturas.py" c' in out
    assert "python3 scripts/gestor-facturas.py d" in out, "tras otro `cd` ya no se sabe"
    assert report.relative_scripts == 1


@pytest.mark.parametrize(
    "src",
    [
        "Ejecuta `python3 scripts/gestor-facturas.py status`.\n",  # sin pista
        fence("python3 scripts/gestor-facturas.py status"),
        "Desde la carpeta de la skill:\n\nOtro párrafo.\n\n```bash\npython3 scripts/gestor-facturas.py x\n```\n",
        "Desde la carpeta de la skill: `python3 scripts/no-esta.py`.\n",  # el script no está en el paquete
        "Desde la carpeta de la skill: `node scripts/gestor-facturas.py`.\n",  # intérprete que no corresponde
        "Desde otra carpeta: `python3 scripts/gestor-facturas.py`.\n",
    ],
)
def test_a_relative_call_without_certainty_is_left_intact_and_counted(src: str) -> None:
    out, report = rewrite(src, ctx())
    assert out == src
    assert report.calls == []
    assert report.relative_scripts >= 1


def test_a_bare_path_or_a_call_with_options_is_not_a_call_the_rewriter_recognises() -> None:
    src = (
        "Desde la carpeta de la skill: `scripts/run.sh init` y `bash -x scripts/run.sh` y `python3 -u scripts/x.py`.\n"
    )
    out, report = rewrite(src, ctx())
    assert out == src
    assert report.calls == []


def test_without_a_safe_folder_relative_calls_are_not_rewritten() -> None:
    src = "Desde la carpeta de la skill: `python3 scripts/gestor-facturas.py`.\n"
    out, _ = rewrite(src, ctx(skill_dir=None))
    assert out == src


def test_the_report_says_whether_the_text_depends_on_the_folder() -> None:
    assert rewrite("sin nada\n", ctx())[1].uses_dir is False
    assert rewrite("`${CLAUDE_SKILL_DIR}/a.md`\n", ctx())[1].uses_dir is True
    assert rewrite("Desde la carpeta de la skill: `python3 scripts/gestor-facturas.py`\n", ctx())[1].uses_dir is True
    assert rewrite("`python3 scripts/otro.py` suelto\n", ctx())[1].uses_dir is True


def test_the_names_that_enter_the_text_cannot_inject_anything() -> None:
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


def test_nothing_is_rewritten_inside_fenced_blocks_except_the_folder_variable() -> None:
    src = (
        "```markdown\n"
        "Ejemplo ajeno: [x](../facturacion/SKILL.md) y `/gestor`\n"
        "${CLAUDE_SKILL_DIR}/templates/a.docx\n"
        "```\n"
        "~~~\n[y](../gestor/SKILL.md)\n~~~\n"
    )
    out, _ = rewrite(src, ctx())
    assert "(../facturacion/SKILL.md)" in out
    assert "`/gestor`" in out
    assert "(../gestor/SKILL.md)" in out
    assert f"{DIR}/templates/a.docx" in out


def test_links_inside_inline_code_are_examples_not_links() -> None:
    src = "Escribe `[x](../facturacion/SKILL.md)` tal cual.\n"
    out, _ = rewrite(src, ctx())
    assert out == src


def test_reference_style_definitions_are_rewritten() -> None:
    out, _ = rewrite("Ver [fact][f].\n\n[f]: ../facturacion/SKILL.md\n", ctx())
    assert out == "Ver [fact][f].\n\n[f]: ../demo-facturacion/SKILL.md\n"


def test_an_unclosed_fence_swallows_the_rest_and_nothing_else_is_rewritten_there() -> None:
    src = "```\n[x](../facturacion/SKILL.md)\n"
    out, _ = rewrite(src, ctx())
    assert out == src


def test_the_report_lists_the_candidate_names_whether_or_not_they_are_synced() -> None:
    _, report = rewrite("[x](../otra/SKILL.md) [y](../facturacion/SKILL.md) `/tampoco` `/gestor`\n", ctx())
    assert report.refs == ["facturacion", "gestor", "otra", "tampoco"]


# --- propiedades ---------------------------------------------------------------------------------


def test_rewriting_is_deterministic_and_idempotent() -> None:
    src = (
        "Ver [f](../facturacion/SKILL.md) y `/gestor`.\n"
        + fence('python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" list', "python3 scripts/x.py")
        + "Desde la carpeta de la skill: `python3 scripts/gestor-facturas.py`\n"
    )
    once, _ = rewrite(src, ctx())
    assert once == rewrite(src, ctx())[0]
    assert "CLAUDE_SKILL_DIR" not in once
    twice, _ = rewrite(once, ctx())
    assert twice == once, "lo ya reescrito no vuelve a cambiar"


def test_crlf_files_keep_their_line_endings() -> None:
    src = (
        "[f](../facturacion/SKILL.md)\r\n```bash\r\n"
        'python3 "${CLAUDE_SKILL_DIR}/scripts/gestor-facturas.py" x\r\n```\r\n'
    )
    out, _ = rewrite(src, ctx())
    assert out == (
        f'[f](../demo-facturacion/SKILL.md)\r\n```bash\r\npython3 "{DIR}/scripts/gestor-facturas.py" x\r\n```\r\n'
    )


def test_a_document_without_anything_to_rewrite_is_returned_unchanged() -> None:
    src = "# Título\n\nTexto normal con `código` y [web](https://example.com).\n"
    out, report = rewrite(src, ctx())
    assert out == src
    assert (report.calls, report.links, report.citations, report.unresolved) == ([], 0, 0, 0)
