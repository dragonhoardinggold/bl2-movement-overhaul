"""Package the mod for release.

    python tools/build.py

Writes dist/movement_overhaul.sdkmod - a zip holding exactly one root folder
named like the file, which is what the SDK requires - plus a copy of the
readme. Caches, backups and anything outside src/movement_overhaul are never
included, so personal sound clips can't leak into a release.
"""

from __future__ import annotations

import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "movement_overhaul"
DIST = ROOT / "dist"

INCLUDE_SUFFIXES = {".py", ".toml", ".md", ".wav", ""}  # "" = LICENSE
EXCLUDE_PARTS = {"__pycache__", ".cache", ".volume_cache"}


def files() -> list[Path]:
    out = []
    for path in sorted(PACKAGE.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(PACKAGE)
        if any(part in EXCLUDE_PARTS or part.startswith(".") for part in rel.parts):
            continue
        if path.suffix.lower() not in INCLUDE_SUFFIXES or path.name.endswith(".bak"):
            continue
        if path.suffix == "" and path.name != "LICENSE":
            continue
        out.append(path)
    return out


def main() -> int:
    meta = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))
    version = meta["project"]["version"]
    DIST.mkdir(exist_ok=True)
    target = DIST / "movement_overhaul.sdkmod"

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files():
            arcname = f"movement_overhaul/{path.relative_to(PACKAGE).as_posix()}"
            if path.suffix in {".py", ".toml", ".md"} or path.name == "LICENSE":
                # Normalise line endings; Windows editors like to mix them.
                text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
                zf.writestr(arcname, text)
            else:
                zf.write(path, arcname)
            print(f"  {arcname}")

    readme = PACKAGE / "README.md"
    if readme.exists():
        (DIST / "README.md").write_text(readme.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"\nbuilt {target} (v{version}, {target.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
