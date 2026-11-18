"""Copia SINTÉTICA (inventada, sin datos reales) de una skill con los patrones de `facturacion` del `brain`.

Patrones que reproduce: scripts fuera de la skill (`brain/scripts`), `import render_doc` entre scripts,
rutas calculadas con `__file__` y con `BASH_SOURCE`, enlaces `../../../scripts/…` y `../gestor/SKILL.md`,
estado mutable en `knowledge/`, binarios externos (`typst`, `mmdc`), plantillas `.typ` y `.docx`, datos
como NIF e IBAN (de ejemplo, válidos solo por formato), un `.js` que es un snippet de consola del navegador,
frontmatter con `allowed-tools` y `hooks`, ficheros ocultos y enlaces simbólicos.
"""

from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

NIF = "12345678Z"  # DNI de ejemplo con letra de control válida
CIF = "A58818501"  # CIF de ejemplo con dígito de control válido
IBAN = "ES91 2100 0418 4502 0005 1332"  # IBAN de ejemplo (el de la documentación de ES)
IBAN_COMPACT = IBAN.replace(" ", "")
EMAIL = "ana@empresa-ejemplo.es"
FAKE_GH_TOKEN = "ghp_" + "A1b2C3d4" * 5  # se monta en tiempo de ejecución: ningún literal parecido a un secreto
PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"

SKILL_MD = f"""---
name: facturacion
description: >-
  Use this skill whenever the user wants to issue an invoice (/facturacion).
  Renders the PDF through the /md-to-doc skill.
allowed-tools: Bash(python3 scripts/facturacion.py *) Read
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: "echo auditoria"
---

# Skill: emisión de facturas

Trabaja acoplado con [`/gestor`](../gestor/SKILL.md) y delega el PDF en [`/md-to-doc`](../md-to-doc/SKILL.md).
El script [`gestor-facturas.py`](../../../scripts/gestor-facturas.py) lee el contador de
[`ledger-facturas.json`](../../../knowledge/fiscal/ledger-facturas.json); el perfil del emisor está en
`knowledge/fiscal/perfil-fiscal-y-regimen.md` y las plantillas en `knowledge/fiscal/templates/factura-pro.typ`.

El emisor es la persona con NIF {NIF} (CIF de la sociedad {CIF}), IBAN {IBAN} y correo {EMAIL}.

## Pasos

```bash
python3 scripts/facturacion.py preflight
python3 scripts/render_doc.py --template knowledge/fiscal/templates/factura-pro.typ
python3 scripts/facturacion.py client-add --nif {NIF} --email {EMAIL}
```

Sandbox: `typst` (`/home/ana/.local/bin/typst`) no es visible dentro del sandbox; usa `mmdc` para los diagramas.
Normativa en [la referencia](references/normativa.md); el enlace [roto](references/no-existe.md) no existe.
"""

GESTOR_MD = """---
name: gestor
description: Gestor fiscal (declaraciones trimestrales).
---

# Gestor

Lee las facturas de `knowledge/fiscal/facturas/<YYYY>/` con `python3 scripts/gestor-facturas.py status`.
"""

MD_TO_DOC_MD = """---
name: md-to-doc
description: Convierte Markdown en documentos (PDF, DOCX).
---

# md-to-doc

Usa `scripts/md_to_doc.py` y las plantillas de `templates/`. Requiere `typst`.
"""

FACTURACION_PY = '''#!/usr/bin/env python3
"""Motor de facturas (sintético)."""
from __future__ import annotations

import json
import os
import smtplib
import subprocess
import sys
from pathlib import Path

import yaml  # dependencia de terceros

sys.path.insert(0, str(Path(__file__).resolve().parent))
import render_doc  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER = REPO_ROOT / "knowledge" / "fiscal" / "ledger-facturas.json"
FACTURAS = REPO_ROOT / "knowledge/fiscal/facturas"
PASSWORD = "hunter2-ejemplo-9"


def save_ledger(data):
    with LEDGER.open("w", encoding="utf-8") as fh:
        json.dump(data, fh)


def create(year):
    out = FACTURAS / year / "factura.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("{}")
    render_doc.render(out)
    subprocess.run([sys.executable, str(Path(__file__).parent / "md_to_doc.py"), str(out)], check=False)


def email():
    server = smtplib.SMTP("smtp.example.org")
    server.login("ana", os.environ["SMTP_PASSWORD"])
'''

RENDER_DOC_PY = '''#!/usr/bin/env python3
"""Render (sintético)."""
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def render(path):
    typst = shutil.which("typst")
    mmdc = shutil.which("mmdc")
    subprocess.run([typst, "compile", str(path)], check=False)
    return mmdc
'''

MD_TO_DOC_PY = '''"""Conversor (sintético): invoca render_doc.py por subproceso."""
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent


def main():
    subprocess.run([sys.executable, str(SCRIPTS_DIR / "render_doc.py"), "--x"], check=False)
'''

GESTOR_PY = '''#!/usr/bin/env python3
"""Gestor (sintético)."""
import json
import os
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO_ROOT = Path(__file__).resolve().parent.parent


def informe():
    data = json.loads((REPO_ROOT / "knowledge" / "fiscal" / "ledger-facturas.json").read_text())
    (REPO_ROOT / "knowledge" / "fiscal" / "informe.json").write_text(json.dumps(data))
'''

AUDITOR_SH = """#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECTS="$ROOT/knowledge/projects"

typst --version
mkdir -p "$PROJECTS/x"
echo hola > "$PROJECTS/x/ficha.md"
python3 "$ROOT/scripts/md_to_doc.py" --x
"""

BROWSER_JS = """(function () {
  if (window._fixture) return;
  window._fixture = true;
  document.querySelectorAll('a').forEach((a) => console.log(a.href));
  localStorage.setItem('k', 'v');
})();
"""

NODE_JS = """const fs = require('fs');
const path = require('path');
const root = path.join(__dirname, '..', '..');
fs.writeFileSync(path.join(root, 'salida.txt'), 'x');
"""

UNRELATED_PY = '''#!/usr/bin/env python3
"""No lo cita ninguna skill."""
print("hola")
'''

PERFIL_MD = f"""---
title: Perfil fiscal
roles: [facturacion]
---
# Perfil fiscal

Titular NIF {NIF}. Cuenta {IBAN_COMPACT}.
"""

LEDGER_JSON = f'{{"emisor": {{"nif": "{NIF}", "iban": "{IBAN}"}}, "contadores": {{"ordinaria_ultimo_numero": 3}}}}\n'


def docx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


@dataclass
class Brain:
    root: Path
    skill: Path
    scripts: Path
    knowledge: Path  # brain/knowledge/fiscal


def _w(path: Path, data: str | bytes, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    if mode is not None:
        path.chmod(mode)
    return path


def make_brain(tmp_path: Path, *, extras: bool = True) -> Brain:
    """Crea `brain/` con la skill `facturacion`, sus vecinas, los scripts y el conocimiento."""
    root = tmp_path / "brain"
    skills = root / ".agents" / "skills"
    skill = skills / "facturacion"
    _w(skill / "SKILL.md", SKILL_MD)
    _w(skill / "references" / "normativa.md", "# Normativa\n\nTexto de referencia.\n")
    _w(skill / "assets" / "logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)))
    _w(skill / "templates" / "informe.docx", docx_bytes())
    _w(skill / "templates" / "informe.typ", '#set page(paper: "a4")\n')
    _w(skills / "gestor" / "SKILL.md", GESTOR_MD)
    _w(skills / "gestor" / "references" / "normativa-aeat.md", "# AEAT\n")
    _w(skills / "md-to-doc" / "SKILL.md", MD_TO_DOC_MD)
    _w(skills / "md-to-doc" / "templates" / "informe.typ", '#set page(paper: "a4")\n')
    _w(skills / "md-to-doc" / "templates" / "informe.docx", docx_bytes())

    scripts = root / "scripts"
    _w(scripts / "facturacion.py", FACTURACION_PY, 0o755)
    _w(scripts / "render_doc.py", RENDER_DOC_PY, 0o644)  # con `#!` pero sin bit de ejecución
    _w(scripts / "md_to_doc.py", MD_TO_DOC_PY, 0o644)
    _w(scripts / "gestor-facturas.py", GESTOR_PY, 0o755)
    _w(scripts / "auditor.sh", AUDITOR_SH, 0o755)
    _w(scripts / "browser-scraper.js", BROWSER_JS, 0o644)
    _w(scripts / "tool.js", NODE_JS, 0o644)
    _w(scripts / "unrelated_tool.py", UNRELATED_PY, 0o755)
    _w(scripts / "__pycache__" / "facturacion.cpython-313.pyc", b"\x00\x01\x02")

    knowledge = root / "knowledge" / "fiscal"
    _w(knowledge / "perfil-fiscal-y-regimen.md", PERFIL_MD)
    _w(knowledge / "ledger-facturas.json", LEDGER_JSON)
    _w(knowledge / "templates" / "factura-pro.typ", "#let factura(data) = [Factura]\n")
    _w(knowledge / "facturas" / "2026" / ".gitkeep", "")
    if extras:
        _w(skill / ".env.example", "SMTP_PASSWORD=\n")
        _w(skill / ".env", "SMTP_PASSWORD=ejemplo-no-real\n")
        _w(skill / "references" / "claves.md", f"token: {FAKE_GH_TOKEN}\n{PRIVATE_KEY_HEADER}\n")
        os.symlink("references", skill / "refs-enlace")  # enlace interno: se sigue
        outside = _w(tmp_path / "fuera" / "secreto.txt", "no debe subirse\n")
        os.symlink(outside, skill / "fuera.txt")  # enlace externo: se omite
    return Brain(root, skill, scripts, knowledge)
