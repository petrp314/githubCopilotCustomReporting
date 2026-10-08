"""Copy an allowlisted site, never the collector workspace or private database."""

import json
import os
from pathlib import Path


ASSETS = ("index.html", "styles.css", "app.js", "data-utils.js")
ROOT = Path(__file__).resolve().parent.parent


def publish(report, output):
    output = Path(output)
    if output.is_symlink():
        raise ValueError("Publication directory may not be a symlink")
    output = output.resolve()
    if output == ROOT or output in ROOT.parents or (ROOT in output.parents and output != ROOT / "dist"):
        raise ValueError("Within the repository, only dist is a publication destination")
    allowed = set(ASSETS) | {"data", "data/report.json"}
    if output.exists():
        for file in output.rglob("*"):
            if file.is_symlink() or str(file.relative_to(output)) not in allowed:
                raise ValueError("Publication destination contains unexpected files")
    payload = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > 50_000_000:
        raise ValueError("Approved snapshot exceeds publication size limit")
    assets = {name: (ROOT / "web" / name).read_bytes() for name in ASSETS}
    (output / "data").mkdir(parents=True, exist_ok=True)
    for name, content in {**assets, "data/report.json": payload}.items():
        temporary = output / (name + ".tmp")
        with temporary.open("xb") as handle:
            handle.write(content)
        os.replace(temporary, output / name)
