"""Stage what the registry serves, and prove it is nothing but data.

The registry's promise to every deployment is that what it serves is data a
deployment parses — never code it could run. This repository may also hold the
source of a plugin's MCP server, beside the manifest that needs it; that source
is built into an image by CI and is never served. So Pages publishes a staged
directory, not the repository, and this script is the only thing that fills it:
``index.yaml`` and each ``plugins/<plugin_id>/manifest.yaml``, and nothing else.

The check runs on the staged directory after staging, so a later edit that
copies one file too many fails the build rather than publishing it.

    python scripts/stage_site.py _site
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def stage(site: Path) -> None:
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)
    shutil.copy2(ROOT / "index.yaml", site / "index.yaml")
    for manifest in sorted((ROOT / "plugins").glob("*/manifest.yaml")):
        target = site / manifest.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest, target)


def served_is_data_only(site: Path) -> list[str]:
    """Every file that should not be served, by its path in the site."""
    problems = []
    for path in sorted(p for p in site.rglob("*") if p.is_file()):
        rel = path.relative_to(site)
        parts = rel.parts
        allowed = rel == Path("index.yaml") or (
            len(parts) == 3 and parts[0] == "plugins" and parts[2] == "manifest.yaml"
        )
        if not allowed:
            problems.append(str(rel))
    return problems


def main() -> None:
    site = Path(sys.argv[1] if len(sys.argv) > 1 else "_site").resolve()
    stage(site)
    stray = served_is_data_only(site)
    for rel in stray:
        print(f"FAIL {rel} would be served, and only index.yaml and manifests may be")
    if stray:
        sys.exit(1)
    print(f"site ok: {sum(1 for p in site.rglob('*') if p.is_file())} files, all data")


if __name__ == "__main__":
    main()
