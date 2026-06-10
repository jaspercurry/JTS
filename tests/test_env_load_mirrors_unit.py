"""ENV_FILES must mirror jasper-voice.service's EnvironmentFile= directives.

Drift here silently shrinks what CLI tools (jasper-doctor, jasper-cues, …)
see vs the running daemon when they build Config.from_env() — the exact bug
where jasper-doctor reported transit / Home Assistant / weather as "not
configured" even when the household had them set, because those wizard env
files weren't in ENV_FILES.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.env_load import ENV_FILES

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "jasper-voice.service"


def _unit_env_files() -> list[str]:
    """The unit's EnvironmentFile= paths in order (the leading `-` optional
    marker stripped)."""
    out: list[str] = []
    for line in UNIT.read_text().splitlines():
        m = re.match(r"^EnvironmentFile=-?(.+)$", line.strip())
        if m:
            out.append(m.group(1).strip())
    return out


def test_env_files_mirrors_jasper_voice_unit():
    unit_files = _unit_env_files()
    assert unit_files, "no EnvironmentFile= directives parsed from the unit"
    assert list(ENV_FILES) == unit_files, (
        "ENV_FILES drifted from jasper-voice.service's EnvironmentFile= order. "
        "A CLI building Config.from_env() would see less config than the daemon "
        "(this is how jasper-doctor missed transit/HA/weather). Update both."
    )
