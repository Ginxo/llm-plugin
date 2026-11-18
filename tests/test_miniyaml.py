"""El YAML mínimo del plugin (solo biblioteca estándar) debe coincidir con PyYAML en el subconjunto que usa."""

from __future__ import annotations

import pytest
import yaml

from baas_plugin import miniyaml
from baas_plugin.miniyaml import YamlError

DOCS = [
    "version: 1\n",
    "version: 1\nroles: [facturacion, contabilidad]\nstatus: active\n",
    "roles: [\"*\"]\nname: \"con: dos puntos\"\nother: 'it''s'\n",
    "# comentario\nversion: 1 # al final\nroles:\n  - a\n  - b\n",
    "roles:\n- a\n- b\nstatus: active\n",
    "requires:\n  interpreters: [python3, bash]\n  binaries: [typst, mmdc]\n"
    "depends_on:\n  skills: [md-to-doc, gestor]\n  knowledge:\n    - knowledge/fiscal/perfil.md\n"
    "executable: [scripts/auditor.sh]\n",
    "empty:\nnull_value: null\ntilde: ~\nflag: true\nother: false\nint: 42\nneg: -3\nfloat: 1.5\n",
    "description: >-\n  Use this skill whenever\n  the user wants\n\n  second paragraph\nname: x\n",
    "description: |\n  line one\n  line two\nname: x\n",
    "description: |-\n  sin salto final\nname: x\n",
    "description: texto que sigue\n  en otra línea\nname: x\n",
    "items:\n  - name: a\n    value: 1\n  - name: b\n    value: 2\n",
    "nested:\n  deep:\n    deeper: [1, 2, 3]\n    map: {a: 1, b: two}\n",
    "tools: Bash(python3 scripts/x.py *) Read\n",
    'url: "http://example.org/a#b"\nhash: abc#def\n',
    "allowed-tools:\n  - Bash(python3 *)\n  - Read\n",
    "---\nversion: 1\n",
    'x: \'a: b\'\ny: "tab\\tseparated"\nz: "unicode \\u00e9"\n',
    "key with spaces: value\n'quoted key': 1\n",
    "list_of_lists:\n  - [1, 2]\n  - [3, 4]\n",
    "empty_list: []\nempty_map: {}\n",
]


@pytest.mark.parametrize("text", DOCS)
def test_loads_matches_pyyaml(text: str) -> None:
    assert miniyaml.loads(text) == yaml.safe_load(text)


@pytest.mark.parametrize(
    "text",
    [
        "description: Use when: x\n",  # `: ` en un escalar plano (lo que Claude tolera y YAML estricto no)
        "a: [1, 2\n",
        "a:\n\t- 1\n",
        "a: 1\n b: 2\n",
        "- a\nb: 1\n",
        'a: "sin cerrar\n',
    ],
)
def test_invalid_yaml_raises(text: str) -> None:
    with pytest.raises(YamlError):
        miniyaml.loads(text)


def test_empty_document_is_none() -> None:
    assert miniyaml.loads("") is None
    assert miniyaml.loads("# solo un comentario\n") is None


@pytest.mark.parametrize(
    "data",
    [
        {"version": 1},
        {"version": 1, "roles": ["*"], "status": "active"},
        {"version": 1, "roles": ["a", "b"], "requires": {"interpreters": ["python3"], "binaries": ["typst"]}},
        {"depends_on": {"skills": [], "knowledge": ["knowledge/x y/z.md"]}, "executable": ["scripts/a.sh"]},
        {"text": "con: dos puntos", "other": "con # almohadilla", "num": "123", "flag": "true", "nul": "null"},
        {"empty": "", "lead": " espacio", "multi": "linea1\nlinea2", "uni": "áéí ñ", "quote": 'dice "hola"'},
        {"nested": {"a": {"b": [1, 2.5, True, None]}}, "items": [{"a": 1, "b": "x"}, {"a": 2}]},
        {"clave desconocida": {"k": "v"}, "-guion": "x", "yes": "no", "~": "x"},
    ],
)
def test_dumps_roundtrips_and_is_valid_yaml(data: dict) -> None:
    text = miniyaml.dumps(data)
    assert yaml.safe_load(text) == data
    assert miniyaml.loads(text) == data


def test_dumps_uses_flow_lists_for_scalars_and_keeps_order() -> None:
    text = miniyaml.dumps({"version": 1, "roles": ["a", "b"], "requires": {"binaries": ["typst"]}})
    assert text == "version: 1\nroles: [a, b]\nrequires:\n  binaries: [typst]\n"
