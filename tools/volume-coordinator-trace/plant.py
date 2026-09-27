"""Plant one bug at a time into a copy of a checkout and rerun the trace.

    python plant.py <checkout> <harness> <plants.json>

Each plant is [file, old, new]; ``old`` must occur exactly once.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VENV_PY = sys.executable


def digest(root: Path, harness: str) -> str:
    out = subprocess.run(
        [VENV_PY, harness, "--expect-root", str(root)],
        env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
        capture_output=True, text=True, check=True, cwd=tempfile.gettempdir(),
    ).stdout
    return [line for line in out.splitlines() if line.startswith("digest=")][0][7:]


def main() -> int:
    checkout, harness, plants_file = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    plants = json.loads(Path(plants_file).read_text())
    base = digest(checkout, harness)
    print(f"base {base}")
    failures = 0
    for index, (rel, old, new) in enumerate(plants, 1):
        work = Path(tempfile.mkdtemp(prefix="plant-", dir=Path(harness).parent))
        shutil.copytree(checkout / "jasper", work / "jasper")
        target = work / rel
        text = target.read_text()
        if text.count(old) != 1:
            print(f"plant {index} {rel}: old text found {text.count(old)}x — SKIPPED")
            failures += 1
            shutil.rmtree(work)
            continue
        target.write_text(text.replace(old, new))
        planted = digest(work, harness)
        verdict = "CHANGED" if planted != base else "UNCHANGED"
        if planted == base:
            failures += 1
        print(f"plant {index} {verdict} {planted[:16]} {rel}: {old.strip()[:70]!r} -> {new.strip()[:70]!r}")
        shutil.rmtree(work)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
