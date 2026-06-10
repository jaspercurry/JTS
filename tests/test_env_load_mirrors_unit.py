"""ENV_FILES must be a superset of EVERY unit's persistent EnvironmentFile=.

A CLI that builds Config.from_env() via env_load (chiefly jasper-doctor, which
checks subsystems owned by many daemons) must see the UNION of all daemons'
config — not just one unit's. When ENV_FILES drifts behind, that CLI silently
sees less config than the running system: jasper-doctor reported transit / HA /
weather / peering / grouping / usbsink as "not configured" even when set,
because those wizard env files were sourced by some unit but missing here.
This guards against a new wizard env file (a future DAC/mic registry's, say)
reintroducing the bug in any unit.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.env_load import ENV_FILES

ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = ROOT / "deploy" / "systemd"


def _all_unit_env_files() -> set[str]:
    """Every persistent EnvironmentFile= path across all units (leading `-`
    optional marker stripped; /run/* runtime-IPC files excluded — they're
    generated at runtime, absent at CLI time, never config the doctor reads)."""
    paths: set[str] = set()
    for unit in sorted(UNIT_DIR.glob("*.service")):
        for line in unit.read_text().splitlines():
            m = re.match(r"^EnvironmentFile=-?(.+)$", line.strip())
            if m:
                path = m.group(1).strip()
                if not path.startswith("/run/"):
                    paths.add(path)
    return paths


def test_env_files_covers_every_unit_environmentfile():
    unit_files = _all_unit_env_files()
    assert unit_files, "no EnvironmentFile= directives parsed from any unit"
    missing = unit_files - set(ENV_FILES)
    assert not missing, (
        "ENV_FILES is missing persistent env file(s) that a systemd unit "
        f"sources: {sorted(missing)}. A CLI building Config.from_env() would "
        "see less config than the running system (this is how jasper-doctor "
        "missed transit/HA/weather/peering). Add them to jasper.env_load.ENV_FILES."
    )


def test_env_files_has_no_duplicates_and_operator_file_first():
    assert len(ENV_FILES) == len(set(ENV_FILES)), f"duplicate in ENV_FILES: {ENV_FILES}"
    assert ENV_FILES[0] == "/etc/jasper/jasper.env", (
        "operator-managed jasper.env must come first so wizard files (later) "
        "override a stale value left in it — matching systemd precedence"
    )
