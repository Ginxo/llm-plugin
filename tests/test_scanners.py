"""Los analizadores estáticos de `inspect` (Python, shell, JavaScript, Markdown y datos sensibles) por separado."""

from __future__ import annotations

import pytest

from baas_plugin import scan_md, scan_py, scan_sh, sensitive
from skill_fixture import CIF, FAKE_GH_TOKEN, IBAN, IBAN_COMPACT, NIF

# --- Python ---------------------------------------------------------------------------------------


def chains(src: str) -> dict[str | None, dict]:
    return {c["name"]: c for c in scan_py.scan_python(src, set()).chains}


@pytest.mark.parametrize(
    ("src", "name", "levels", "segs"),
    [
        ("from pathlib import Path\nROOT = Path(__file__).resolve().parent.parent\n", "ROOT", 2, []),
        ("from pathlib import Path\nROOT = Path(__file__).parents[1]\n", "ROOT", 2, []),
        ("from pathlib import Path\nHERE = Path(__file__).parent\n", "HERE", 1, []),
        ("import os\nHERE = os.path.dirname(os.path.abspath(__file__))\n", "HERE", 1, []),
        ("import os\nR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))\n", "R", 2, []),
        (
            "import os\nB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')\n",
            "B",
            2,
            ["data"],
        ),
        (
            "from pathlib import Path\nR = Path(__file__).parent.parent\nD = R / 'knowledge' / 'x' / 'y.json'\n",
            "D",
            2,
            ["knowledge", "x", "y.json"],
        ),
        (
            "from pathlib import Path\nR = Path(__file__).parent.parent\nD = R.joinpath('a', 'b')\n",
            "D",
            2,
            ["a", "b"],
        ),
        (
            "from pathlib import Path\nR = Path(__file__).parent.parent\nD = f'{R}/knowledge/{1}/z'\n",
            "D",
            2,
            ["knowledge", "*", "z"],
        ),
    ],
)
def test_python_path_chains(src: str, name: str, levels: int, segs: list[str]) -> None:
    c = chains(src)[name]
    assert c["levels"] == levels
    assert c["segs"] == segs


def test_python_alias_of_a_root_is_not_a_new_root() -> None:
    src = "from pathlib import Path\nR = Path(__file__).parent.parent\nrepo = R.resolve()\nup = R.parent\n"
    c = chains(src)
    assert c["R"]["alias"] is False
    assert c["repo"]["alias"] is True
    assert c["up"]["alias"] is False
    assert c["up"]["levels"] == 3


def test_python_writes_are_derived_only_when_they_come_from_the_file() -> None:
    src = (
        "from pathlib import Path\nimport json, shutil\n"
        "R = Path(__file__).parent.parent\n"
        "def f(p, data):\n"
        "    with open(p, 'w') as fh:\n        fh.write(data)\n"
        "    (R / 'estado' / 'a.json').write_text('{}')\n"
        "    shutil.copy('x', R / 'copia')\n"
        "    open('log.txt', 'a').close()\n"
        "    open('lectura.txt').read()\n"
    )
    res = scan_py.scan_python(src, set())
    assert {w["op"] for w in res.writes} == {"write_text", "shutil.copy"}
    assert {w["path_hint"] for w in res.writes} == {"estado/a.json", "copia"}
    assert res.other_writes == 2  # open(p, 'w') y open('log.txt', 'a'): sin ruta deducible


def test_python_binaries_from_which_subprocess_and_system() -> None:
    src = (
        "import os, shutil, subprocess, sys\n"
        "a = shutil.which('mmdc')\n"
        "subprocess.run(['pandoc', 'x'])\n"
        "subprocess.run([sys.executable, 'y.py'])\n"
        "os.system('typst compile z')\n"
        "subprocess.run('/usr/local/bin/ffmpeg -i a b', shell=True)\n"
    )
    found = {name for name, _ in scan_py.scan_python(src, set()).binaries}
    assert found == {"mmdc", "pandoc", "typst", "ffmpeg"}


def test_python_imports_and_relative_imports() -> None:
    src = "import a.b\nfrom c.d import e\nfrom . import f\nfrom .g import h\nfrom .. import i\n"
    got = [(i["module"], i["level"]) for i in scan_py.scan_python(src, set()).imports]
    assert ("a.b", 0) in got
    assert ("c.d", 0) in got
    assert ("f", 1) in got
    assert ("g", 1) in got
    assert ("i", 2) in got


def test_python_mentions_of_own_scripts_ignore_docstrings() -> None:
    src = '"""Usa render.py."""\nimport subprocess\nsubprocess.run(["python3", "x/render.py"])\nN = "other.py"\n'
    res = scan_py.scan_python(src, {"render.py", "other.py"})
    assert {(m["name"], m["how"]) for m in res.mentions} == {("render.py", "subprocess"), ("other.py", "reference")}


def test_python_syntax_error_still_gives_imports() -> None:
    res = scan_py.scan_python("import a\ndef roto(:\n", set())
    assert res.parse_error
    assert [i["module"] for i in res.imports] == ["a"]


# --- shell ----------------------------------------------------------------------------------------


def sh_chains(src: str) -> dict[str, dict]:
    return {c["name"]: c for c in scan_sh.scan_shell(src, set()).chains}


@pytest.mark.parametrize(
    ("src", "name", "levels"),
    [
        ('ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n', "ROOT", 2),
        ('SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"\n', "SCRIPT_DIR", 1),
        ('D="$(dirname "$0")"\n', "D", 1),
        ('D="$(dirname "$(dirname "$0")")"\n', "D", 2),
        ('D="${0%/*}"\n', "D", 1),
        ('H="$(dirname "$0")"\nR="$H/.."\n', "R", 2),
    ],
)
def test_shell_roots(src: str, name: str, levels: int) -> None:
    assert sh_chains(src)[name]["levels"] == levels


def test_shell_writes_follow_derived_variables_only() -> None:
    src = (
        'ROOT="$(dirname "$0")/.."\n'
        'OUT="$ROOT/salida"\n'
        'mkdir -p "$OUT"\n'
        'echo x > "$OUT/a.txt"\n'
        "echo e >&2\n"
        "ls 2>/dev/null\n"
        "echo y > /dev/null\n"
        "echo z > /tmp/otro.txt\n"
        'cp a "$ROOT/b"\n'
    )
    res = scan_sh.scan_shell(src, set())
    assert {w["op"] for w in res.writes} == {"mkdir", "redirect >", "cp"}
    assert {w["path_hint"] for w in res.writes} == {"salida", "salida/a.txt", "b"}
    assert res.other_writes == 1  # > /tmp/otro.txt


def test_shell_binaries_and_interpreters() -> None:
    src = "#!/usr/bin/env bash\nif command -v jq >/dev/null; then jq . x | sudo pandoc; fi\npython3 x.py && node y.js\n"
    res = scan_sh.scan_shell(src, set())
    assert res.interpreter == "bash"
    assert {n for n, _ in res.binaries} == {"jq", "pandoc"}
    assert {n for n, _ in res.interpreters} == {"python3", "node"}


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("#!/usr/bin/env python3", "python3"),
        ("#!/bin/bash", "bash"),
        ("#!/usr/bin/env -S node --flag", "node"),
        ("#!/bin/sh", "sh"),
        ("#!/usr/bin/awk -f", None),
        ("no es shebang", None),
    ],
)
def test_shebangs(line: str, expected: str | None) -> None:
    assert scan_sh.shebang_interpreter(line) == expected


# --- JavaScript -----------------------------------------------------------------------------------


def test_js_browser_snippet_vs_node_script() -> None:
    assert scan_sh.scan_js("(() => { document.title = 'x'; })();", set()).browser_snippet is True
    assert scan_sh.scan_js("const fs = require('fs');\ndocument.x;", set()).browser_snippet is False
    assert scan_sh.scan_js("window.localStorage.x = 1;", set()).browser_snippet is True
    assert scan_sh.scan_js("export const a = 1;\nimport x from './x.js';", set()).node is True


def test_js_dirname_roots_and_writes() -> None:
    src = "const root = path.join(__dirname, '..', 'data');\nfs.writeFileSync(path.join(root, 'o.json'), '{}');\n"
    res = scan_sh.scan_js(src, set())
    assert res.chains[0]["levels"] == 2
    assert res.chains[0]["segs"] == ["data"]
    assert res.writes[0]["op"] == "fs.writeFileSync"


# --- Markdown -------------------------------------------------------------------------------------


def test_markdown_links_refs_and_code_context() -> None:
    text = (
        "Ver [otra](../otra/SKILL.md) y [web](https://x.org) y [ancla](#a).\n"
        "Corre `python3 scripts/a.py` y mira scripts/b.py o knowledge/x/y.md.\n"
        "```bash\n"
        "python3 scripts/c.py --x\n"
        "# scripts/comentario.py\n"
        "```\n"
        "Plantilla `scripts/<nombre>.py` y /otra-skill y /etc/passwd.\n"
    )
    md = scan_md.scan_markdown(text)
    assert [lk["target"] for lk in md.links] == ["../otra/SKILL.md"]
    refs = {(r["target"], r["in_code"], r["fenced"]) for r in md.refs}
    assert ("scripts/a.py", True, False) in refs
    assert ("scripts/b.py", False, False) in refs
    assert ("scripts/c.py", True, True) in refs
    assert ("knowledge/x/y.md", False, False) in refs
    assert not any("<" in r["target"] for r in md.refs)
    assert {m["name"] for m in md.mentions} >= {"otra-skill"}
    assert md.fenced == [False, False, True, True, True, True, False]


def test_markdown_binaries_in_code() -> None:
    text = "Usa `typst` y `/opt/bin/mmdc` y `typst compile x`.\n```bash\n$ pandoc a -o b\npython3 x.py\n```\n"
    md = scan_md.scan_markdown(text)
    assert {n for n, _ in md.binaries} == {"typst", "mmdc", "pandoc"}
    assert {n for n, _ in md.interpreters} == {"python3"}


# --- datos sensibles ------------------------------------------------------------------------------


def kinds(text: str) -> list[str]:
    return [m.kind for m in sensitive.scan_text(text)]


def test_spanish_identifiers_are_validated() -> None:
    assert kinds(f"DNI {NIF}") == ["nif"]
    assert kinds("DNI 12345678A") == []  # letra de control incorrecta
    assert kinds("NIE X1234567L") == ["nif"]
    assert kinds("NIE X1234567A") == []
    assert kinds(f"CIF {CIF}") == ["cif"]
    assert kinds("CIF A58818502") == []  # dígito de control incorrecto
    assert kinds(f"IBAN {IBAN}") == ["iban"]
    assert kinds(f"IBAN {IBAN_COMPACT}.") == ["iban"]
    assert kinds("IBAN ES91 2100 0418 4502 0005 1333") == []
    assert kinds(f"IBAN {IBAN} NIF") == ["iban"]  # la palabra que sigue no rompe la validación


def test_samples_are_masked() -> None:
    (m,) = sensitive.scan_text(f"IBAN {IBAN}")
    assert m.sample == "ES…32"
    assert IBAN_COMPACT not in m.sample
    assert sensitive.mask("abc") == "***"


@pytest.mark.parametrize(
    "text",
    [
        f"token = '{FAKE_GH_TOKEN}'",
        f"Authorization: Bearer {'x' * 30}",
        "aws AKIAABCDEFGHIJKLMNOP",
        "sk-ant-" + "a" * 30,
        'password = "s3cr3t-valor"',
        'API_KEY: "abcdef123456"',
        "DB_PASSWORD=valor-real-123",
    ],
)
def test_secrets_are_detected(text: str) -> None:
    assert kinds(text)


@pytest.mark.parametrize(
    "text",
    [
        'password = "<tu-clave>"',
        'password = "changeme"',
        'password = "${PASSWORD}"',
        "password = os.environ['X']",
        "DB_PASSWORD=",
        'token = ""',
        "contact git@github.com or noreply@example.com",
        "version 1.2.3 build 20260115T093000",
    ],
)
def test_placeholders_and_noise_are_not_secrets(text: str) -> None:
    assert kinds(text) == []


def test_email_and_private_key() -> None:
    assert kinds("escribe a ana@empresa-ejemplo.es") == ["email"]
    assert kinds("-----BEGIN " + "OPENSSH PRIVATE KEY-----") == ["private_key"]


@pytest.mark.parametrize(
    ("name", "flagged"),
    [
        (".env", True),
        (".env.local", True),
        (".env.example", False),
        (".env.sample", False),
        ("id_rsa", True),
        ("id_rsa.pub", False),
        ("clave.pem", True),
        ("credentials.json", True),
        ("README.md", False),
        ("environment.md", False),
    ],
)
def test_sensitive_file_names(name: str, flagged: bool) -> None:
    assert (sensitive.sensitive_file_reason(name) is not None) is flagged
