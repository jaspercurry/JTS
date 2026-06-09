"""Lock down the jasper-control.service systemd unit shape.

jasper-control persists small state files under /var/lib/jasper — the
wizard env files it owns (aec_mode.env, wake_model.env, debug.env),
speaker_volume.json, and the T5.2 SystemSupervisor's reboot rate-limit
at /var/lib/jasper/system_supervisor_reboot.json. That last one is
load-bearing: the persisted timestamp is what keeps a *permanent*
userspace wedge from reboot-looping forever (see
jasper/control/system_supervisor.py and
docs/HANDOFF-tier5-watchdog-liveness.md).

Today those writes work only because `ProtectSystem=full` leaves /var
writable. A future tightening to `ProtectSystem=strict` would silently
make /var read-only and regress the reboot-loop guard. The explicit
`ReadWritePaths=/var/lib/jasper` pins the contract; this test catches a
config edit that drops it. Mirrors tests/test_fanin_systemd.py.
"""
from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-control.service"


def _read_unit() -> str:
    return UNIT_PATH.read_text()


def _value_for(unit_text: str, key: str) -> str | None:
    """Pull the value of a `Key=Value` directive. Returns None if
    absent. Matches the systemd convention: case-sensitive key, no
    whitespace around `=`, value is everything to end-of-line."""
    for line in unit_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("["):
            continue
        if "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key:
            return v.strip()
    return None


def test_unit_file_exists():
    assert UNIT_PATH.exists(), (
        f"jasper-control.service missing at {UNIT_PATH}."
    )


def test_readwritepaths_pins_var_lib_jasper():
    """The state-write contract must be explicit, not incidental.

    Without this line the /var/lib/jasper writes (including the
    supervisor's reboot rate-limit) survive only because
    ProtectSystem=full happens to leave /var writable — a
    ProtectSystem=strict edit would silently break persistence and
    regress to the reboot-loop the rate-limit exists to prevent."""
    unit = _read_unit()
    val = _value_for(unit, "ReadWritePaths")
    assert val is not None, (
        "jasper-control.service must declare ReadWritePaths=/var/lib/jasper "
        "to pin its state-write contract. The T5.2 reboot rate-limit at "
        "/var/lib/jasper/system_supervisor_reboot.json depends on it."
    )
    assert "/var/lib/jasper" in val.split(), (
        f"ReadWritePaths must include /var/lib/jasper; got {val!r}"
    )
