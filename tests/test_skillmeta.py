from __future__ import annotations

import pytest
import yaml

from baas_plugin import skillmeta


def test_frontmatter_strict_yaml() -> None:
    fm = skillmeta.parse_frontmatter("---\nname: x\ndescription: >-\n  uno\n  dos\nroles: [a, b]\n---\ncuerpo\n")
    assert fm.present
    assert fm.valid_yaml
    assert fm.keys == ["name", "description", "roles"]
    assert fm.data == {"name": "x", "description": "uno dos", "roles": ["a", "b"]}


def test_frontmatter_lenient_like_claude() -> None:
    fm = skillmeta.parse_frontmatter("---\nname: x\ndescription: Use when: el usuario pide X\nhooks:\n  a: 1\n---\n")
    assert fm.present
    assert fm.valid_yaml is False
    assert fm.error
    assert fm.data["description"] == "Use when: el usuario pide X"
    assert "hooks" in fm.keys


def test_frontmatter_absent_or_open() -> None:
    assert skillmeta.parse_frontmatter("# solo cuerpo\n").present is False
    assert skillmeta.parse_frontmatter("---\nname: x\nsin cierre\n").present is False


def test_frontmatter_dates_are_kept_as_text() -> None:
    fm = skillmeta.parse_frontmatter("---\nname: x\ncreated: 2026-01-01\n---\n")
    assert fm.data["created"] == "2026-01-01"


def test_merge_manifest_defaults_and_detection() -> None:
    manifest, notes = skillmeta.merge_manifest(
        None,
        roles=["*"],
        interpreters=["python3", "bash"],
        binaries=["typst"],
        depends_skills=["gestor"],
        depends_knowledge=["knowledge/fiscal/perfil.md"],
        executable=["scripts/a.sh"],
        package_paths={"scripts/a.sh", "SKILL.md"},
    )
    assert notes == []
    assert manifest == {
        "version": 1,
        "roles": ["*"],
        "requires": {"interpreters": ["python3", "bash"], "binaries": ["typst"]},
        "depends_on": {"skills": ["gestor"], "knowledge": ["knowledge/fiscal/perfil.md"]},
        "executable": ["scripts/a.sh"],
    }


def test_merge_manifest_base_wins_over_detection_and_unknown_keys_are_kept() -> None:
    base = {
        "version": 1,
        "roles": ["facturacion"],
        "status": "active",
        "requires": {"binaries": ["typst>=0.11"]},
        "depends_on": {"skills": ["md-to-doc"]},
        "executable": ["scripts/a.sh", "scripts/gone.sh"],
        "owner": "equipo-fiscal",
    }
    manifest, notes = skillmeta.merge_manifest(
        base,
        interpreters=["python3"],
        binaries=["typst", "mmdc"],
        depends_skills=["gestor"],  # detectada, pero la base ya declara sus dependencias
        extra_depends_skills=["auditor"],
        executable=["scripts/b.py"],
        package_paths={"scripts/a.sh", "scripts/b.py"},
    )
    assert manifest["roles"] == ["facturacion"]
    assert manifest["status"] == "active"
    assert manifest["requires"] == {"binaries": ["typst>=0.11"], "interpreters": ["python3"]}
    assert manifest["depends_on"]["skills"] == ["md-to-doc", "auditor"]
    assert manifest["executable"] == ["scripts/a.sh", "scripts/b.py"]
    assert manifest["owner"] == "equipo-fiscal"
    assert any("scripts/gone.sh" in n for n in notes)


def test_explicit_roles_override_base() -> None:
    manifest, _ = skillmeta.merge_manifest({"version": 1, "roles": ["a"]}, roles=["b", "c"])
    assert manifest["roles"] == ["b", "c"]


def test_manifest_without_roles_has_no_roles_key() -> None:
    manifest, _ = skillmeta.merge_manifest(None)
    assert manifest == {"version": 1}


def test_dump_manifest_is_valid_yaml_in_a_stable_order() -> None:
    manifest = {
        "owner": "x",
        "executable": ["scripts/a.sh"],
        "version": 1,
        "depends_on": {"skills": ["a"]},
        "roles": ["*"],
        "requires": {"binaries": ["typst"]},
    }
    text = skillmeta.dump_manifest(manifest)
    assert [line.split(":")[0] for line in text.splitlines() if not line.startswith(" ")] == [
        "version",
        "roles",
        "requires",
        "depends_on",
        "executable",
        "owner",
    ]
    assert yaml.safe_load(text) == manifest
    assert skillmeta.parse_manifest(text) == manifest


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("*", ["*"]), ("a,b", ["a", "b"]), (" a , b ,", ["a", "b"]), ("", [])],
)
def test_parse_roles(raw, expected) -> None:
    assert skillmeta.parse_roles(raw) == expected


def test_wildcard_role_must_be_alone() -> None:
    with pytest.raises(ValueError, match="solo"):
        skillmeta.parse_roles("*,a")


def test_parse_manifest_rejects_non_mappings() -> None:
    with pytest.raises(skillmeta.miniyaml.YamlError):
        skillmeta.parse_manifest("- a\n- b\n")


def test_with_roles_adds_them_to_a_header_without_touching_the_rest() -> None:
    text = "---\ntitle: Perfil\nstatus: active\n---\n# Cuerpo\n"
    new, state = skillmeta.with_roles(text, ["a", "b"])
    assert state == "added"
    assert new == "---\nroles: [a, b]\ntitle: Perfil\nstatus: active\n---\n# Cuerpo\n"
    assert skillmeta.parse_frontmatter(new).data == {"roles": ["a", "b"], "title": "Perfil", "status": "active"}


def test_with_roles_creates_a_header_and_respects_existing_roles() -> None:
    new, state = skillmeta.with_roles("# Solo cuerpo\n", ["a"])
    assert (new, state) == ("---\nroles: [a]\n---\n# Solo cuerpo\n", "added")
    same, state = skillmeta.with_roles("---\nroles: [x]\n---\ncuerpo\n", ["a"])
    assert (same, state) == ("---\nroles: [x]\n---\ncuerpo\n", "kept")
    crlf, _ = skillmeta.with_roles("---\r\ntitle: t\r\n---\r\ncuerpo\r\n", ["a"])
    assert crlf.startswith("---\r\nroles: [a]\r\ntitle: t")
