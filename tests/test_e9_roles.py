"""Utilidades puras y contenido de `dev/e9_roles.py` (la prueba de roles funcionales de E9).

La prueba completa necesita la pila real (`scripts/dev-up.sh`): aquí se comprueba que el contenido que escribe es
válido para el MCP, que no da falsos positivos (nada de lo visible para un rol contiene lo del otro) y que las
funciones que detectan una fuga detectan una de verdad.
"""

from __future__ import annotations

import json
import re

import e9_roles as e9
import pytest


def visible_to(side: e9.Side) -> dict[str, str]:
    """Lo que el rol de `side` lee: su documento, su skill y su script."""
    return {path: e9.CONTENT[path] for path in side.paths}


# --- el contenido -------------------------------------------------------------------------------------


def test_every_markdown_has_a_valid_header_for_the_mcp() -> None:
    for path, text in e9.CONTENT.items():
        if path.endswith(".md"):
            kind = "skill" if path.endswith("SKILL.md") else "knowledge"
            assert e9.header_errors(text, kind) == [], path


def test_paths_are_valid_for_the_mcp_layout() -> None:
    for path in e9.CONTENT:
        parts = path.split("/")
        assert parts[0] in {"knowledge", "skills"}, path
        if parts[0] == "skills":
            assert (len(parts) == 3 and parts[2] == "SKILL.md") or (len(parts) == 4 and parts[2] == "scripts"), path
        assert re.fullmatch(r"[a-z0-9-]+", parts[1]), path  # skill y área en minúsculas y guiones


def test_each_skill_cites_the_other_roles_document() -> None:
    assert e9.DOC_B in e9.cited_paths(e9.CONTENT[e9.SIDE_A.skill_path])
    assert e9.DOC_A in e9.cited_paths(e9.CONTENT[e9.SIDE_B.skill_path])
    assert e9.cited_paths(e9.CONTENT[e9.DOC_A]) == []


@pytest.mark.parametrize(("me", "other"), [(e9.SIDE_A, e9.SIDE_B), (e9.SIDE_B, e9.SIDE_A)])
def test_what_one_role_can_read_never_contains_the_marks_of_the_other(me: e9.Side, other: e9.Side) -> None:
    """Si no, la comprobación de que la búsqueda no deja ver trozos daría falsos positivos con lo propio."""
    for path, text in visible_to(me).items():
        for forbidden in (other.mark, other.body, other.title):
            assert forbidden.lower() not in text.lower(), f"{path} contiene «{forbidden}»"
    assert not e9.leaks(visible_to(me), other.paths, [other.mark, other.body, other.title])


def test_roles_of_each_side_are_declared_in_its_files() -> None:
    for side in (e9.SIDE_A, e9.SIDE_B):
        for path in (side.doc, side.skill_path):
            assert f"roles: [{side.role}]" in e9.CONTENT[path], path
    assert "roles: []" in e9.CONTENT[e9.DOC_ADMIN]
    assert set(e9.ROLE_DESCRIPTIONS) == {e9.SIDE_A.role, e9.SIDE_B.role}


def test_users_have_the_roles_the_test_needs() -> None:
    assert e9.USERS["a"][1:] == ("USER", [e9.ROLE_A])
    assert e9.USERS["b"][1:] == ("USER", [e9.ROLE_B])
    assert e9.USERS["c"][1:] == ("USER", [])
    assert e9.USERS["admin"][1] == "ORG_ADMIN"
    assert len({email for email, _, _ in e9.USERS.values()}) == 4


# --- header_errors ------------------------------------------------------------------------------------


def test_header_errors_flags_the_usual_mistakes() -> None:
    ok = "---\ntitle: T\ntype: skill\nroles: [a]\nstatus: active\ndescription: D\n---\ncuerpo\n"
    assert e9.header_errors(ok, "skill") == []
    assert "falta «description»" in e9.header_errors(ok.replace("description: D\n", ""), "skill")
    assert e9.header_errors("sin cabecera", "skill") == ["falta la cabecera: la primera línea debe ser ---"]
    assert e9.header_errors("---\ntitle: T\n", "skill") == ["la cabecera no está cerrada con ---"]
    assert "«type» debe ser knowledge" in e9.header_errors(ok, "knowledge")
    assert "rol no válido: Mayúsculas" in e9.header_errors(ok.replace("[a]", "[Mayúsculas]"), "skill")
    assert any("«roles»" in m for m in e9.header_errors(ok.replace("roles: [a]", "roles: a"), "skill"))
    assert any("roles" in m for m in e9.header_errors(ok.replace("roles: [a]\n", ""), "skill"))


def test_header_errors_catches_a_title_with_colon_that_the_server_rejects() -> None:
    """Un título como `AI-IP: qué cubre` sin comillas no es YAML válido: el MCP responde INVALID_ARGUMENT."""
    text = "---\ntitle: AI-IP: qué cubre\ntype: knowledge\nroles: [a]\nstatus: active\n---\n"
    assert e9.header_errors(text, "knowledge") == ["«title» lleva «: » sin comillas"]


# --- leaks --------------------------------------------------------------------------------------------


def test_leaks_detects_paths_titles_and_excerpts_in_nested_results() -> None:
    forbidden_paths = {"knowledge/b/x.md"}
    text = ["SECRETO-B"]
    clean = {"results": [{"path": "knowledge/a/y.md", "title": "Lo mío", "excerpt": "nada de B"}]}
    assert e9.leaks(clean, forbidden_paths, text) == []
    by_path = {"results": [{"path": "knowledge/b/x.md", "title": "Otro", "excerpt": ""}]}
    assert e9.leaks(by_path, forbidden_paths, text) == ["ruta knowledge/b/x.md"]
    by_excerpt = {"results": [{"path": "knowledge/a/y.md", "title": "T", "excerpt": "…el secreto-b aparece…"}]}
    assert e9.leaks(by_excerpt, forbidden_paths, text) == ["«SECRETO-B» en excerpt"]
    nested = {"skills": [{"name": "s", "scripts": [{"path": "knowledge/b/x.md"}]}]}
    assert e9.leaks(nested, forbidden_paths, text) == ["ruta knowledge/b/x.md"]
    in_error = {"error": "AUTHORIZATION_DENIED", "message": "No tienes acceso", "content": "SECRETO-B"}
    assert e9.leaks(in_error, forbidden_paths, text) == ["«SECRETO-B» en content"]


def test_a_citation_inside_an_own_skill_text_is_not_a_leak() -> None:
    """La skill del rol A cita la ruta del documento B: el texto de la skill se ve; el documento, no."""
    own_skill = {"results": [{"path": e9.SKILL_A_PATH, "title": "Gestión de citas", "excerpt": f"…en `{e9.DOC_B}`."}]}
    assert e9.leaks(own_skill, e9.SIDE_B.paths, [e9.MARK_B, e9.BODY_B, e9.TITLE_B]) == []


# --- redact -------------------------------------------------------------------------------------------


def test_redact_hides_secrets_and_cuts_long_texts() -> None:
    key = "baas_" + "A" * 43
    link = "http://app.localhost/activate/" + "B" * 43
    out = e9.redact({"a": [f"clave {key}", f"enlace {link}", "x" * 1000]}, limit=100)
    text = json.dumps(out)
    assert key not in text
    assert "B" * 43 not in text
    assert "[oculto]" in text
    assert "1000 caracteres" in text
    assert e9.redact({"n": 3, "ok": True, "s": "corto"}) == {"n": 3, "ok": True, "s": "corto"}


def test_evidence_never_keeps_a_key_even_inside_arguments() -> None:
    ev = e9.Evidence()
    ev.add("a", "baas_write_document", {"content": "baas_" + "Z" * 43}, response={"ok": True})
    ev.add("a", "baas_read_document", {"path": "p"}, error={"error": "AUTHORIZATION_DENIED"})
    assert not e9.SECRET_RE.search(json.dumps(ev.calls))
    assert "response" in ev.calls[0]
    assert "error" in ev.calls[1]


def test_secret_pattern_matches_what_the_webapp_generates() -> None:
    assert e9.SECRET_RE.search(e9.dev_api_key("alguien@example.test"))
    assert e9.SECRET_RE.search("http://app.localhost:28080/activate/EoURNNoe9ieYMQCUQr-4la7lp5o7d9gGfB9VmGsfKlI")


def test_parse_args_defaults() -> None:
    args = e9.parse_args([])
    assert args.app_url.startswith("http://app.localhost")
    assert args.admin_email == "admin@baas.test"
    assert args.evidence is None
    with pytest.raises(SystemExit) as exc:
        e9.parse_args(["--no-existe"])
    assert exc.value.code == 2
