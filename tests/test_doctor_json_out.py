"""WS1 Phase 3b-2 — the root-fidelity /system/diagnostics capture path.

jasper-doctor is a root tool, and jasper-control is now non-root — running the
doctor in-process from jasper-control makes ~7 hardware checks fail on
permissions (false red on the dashboard). So the report is produced by the root
`jasper-doctor-json.service` oneshot (started via jasper-control's polkit
manage-units grant), which writes the JSON to a group-readable file
jasper-control serves. These tests pin the moving parts.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from jasper.cli.doctor import CheckResult, render_json
from jasper.control.restart_broker import MANAGED_UNITS

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy/systemd/jasper-doctor-json.service"


def test_render_json_out_writes_0640_and_returns_zero(tmp_path):
    """`--out` writes the report atomically at 0640 (group-readable, not world)
    and returns 0 — a "report with failures" must not flip the oneshot to
    `failed`, since the failures are inside the JSON."""
    results = [
        CheckResult("ok-check", "ok", "fine"),
        CheckResult("bad-check", "fail", "broken"),
    ]
    out = tmp_path / "result.json"
    rc = render_json(results, out_path=str(out))

    assert rc == 0, "out-path render must return 0 even with failing checks"
    mode = stat.S_IMODE(os.stat(out).st_mode)
    assert mode == 0o640, f"expected 0640 group-readable, got {oct(mode)}"
    import json
    payload = json.loads(out.read_text())
    assert payload["fails"] == 1
    assert {r["name"] for r in payload["results"]} == {"ok-check", "bad-check"}


def test_render_json_stdout_keeps_exit_semantics(capsys):
    """Without --out, the operator CLI contract is unchanged: exit 1 on a fail."""
    assert render_json([CheckResult("x", "fail", "")]) == 1
    assert render_json([CheckResult("x", "warn", "")]) == 0
    assert render_json([CheckResult("x", "ok", "")]) == 0


def test_oneshot_unit_runs_doctor_with_out():
    assert UNIT.is_file(), f"missing {UNIT}"
    text = UNIT.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    # root (no User=) for full fidelity; Group=jasper so the result is readable.
    assert not any(
        ln.strip().startswith("User=") for ln in text.splitlines()
    ), "the doctor oneshot must run as root (no User=) for full-fidelity checks"
    assert "Group=jasper" in text
    assert "jasper-doctor --json --out /run/jasper-control/doctor-result.json" in text
    # On-demand only — never enabled (no [Install] section header).
    assert "[Install]" not in [ln.strip() for ln in text.splitlines()]


def test_oneshot_in_managed_units_and_polkit_allowlist():
    """The non-root jasper-control `systemctl start`s the oneshot — so it must be
    in MANAGED_UNITS (and therefore the polkit allowlist, pinned set-equal by
    test_polkit_jasper_control)."""
    assert "jasper-doctor-json.service" in MANAGED_UNITS


def test_endpoint_uses_the_oneshot_not_inprocess_doctor():
    """The /system/diagnostics handler must start the root oneshot + read the
    result file — NOT spawn jasper-doctor in-process (which would run non-root
    and report false failures)."""
    server = (ROOT / "jasper/control/server.py").read_text(encoding="utf-8")
    assert "jasper-doctor-json.service" in server
    assert "/run/jasper-control/doctor-result.json" in server
    # The old in-process spawn must be gone.
    assert '"/opt/jasper/.venv/bin/jasper-doctor", "--json"' not in server


def test_install_installs_the_oneshot_unit():
    units_sh = (ROOT / "deploy/lib/install/systemd-units.sh").read_text()
    # Installed in BOTH the full and streambox unit-install paths.
    assert units_sh.count("jasper-doctor-json.service") >= 2
