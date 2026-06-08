"""Regression: a transient jasper-outputd readiness miss must not abort the
whole install.

Bug (REVIEW-2026-06-04-small-wins.md → "require_outputd_ready aborts the whole
install on a transient failure"): `require_outputd_ready` was a *bare* call
under `set -euo pipefail` inside `install_systemd_units`, which runs BEFORE
`provision_correction_tls` / `install_nginx_site` / `regenerate_audio_cues` /
`run_doctor_summary` in `main()`. The helper restarts jasper-outputd and then
probes its STATUS socket with only a 3 s deadline; a momentary "device busy"
or a >3 s service settle on a loaded 1 GB Pi returns non-zero and aborts
`main()` *before nginx exists* — stranding the operator with no web UI or
doctor to diagnose the box through. On a self-recovering appliance that is the
opposite of resilient.

Fix: the call site is non-fatal (guarded by `||`) and emits a loud WARN, so the
install always reaches the recovery surface. The systemd `Wants=/After=
jasper-outputd` dependency and the doctor's `check_outputd_service` remain the
real runtime guards. These tests pin that invariant so the bare-fatal form
cannot silently regress.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "deploy" / "install.sh"


def _install_text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def _call_site_index(lines: list[str]) -> int:
    """Index of the single line that INVOKES require_outputd_ready (i.e. not
    its `require_outputd_ready() {` definition)."""
    hits = [
        i
        for i, line in enumerate(lines)
        if line.strip().startswith("require_outputd_ready") and "()" not in line
    ]
    assert len(hits) == 1, (
        f"expected exactly one require_outputd_ready call site, found {len(hits)} "
        f"(at line numbers {[h + 1 for h in hits]})"
    )
    return hits[0]


def test_require_outputd_ready_call_is_non_fatal_and_loud():
    """The call must be `||`-guarded (so `set -e` can't abort the install on a
    transient probe miss) AND must still WARN loudly about outputd (the
    project's 'no silent failure' bar — non-fatal is not the same as silent)."""
    lines = _install_text().splitlines()
    idx = _call_site_index(lines)
    # The guard + WARN may use a `\`-continuation, so inspect a 2-line window.
    window = " ".join(lines[idx : idx + 2])
    assert "||" in window, (
        "require_outputd_ready must be non-fatal (guarded by `||`); a bare call "
        f"aborts the install on a transient failure. Got: {lines[idx]!r}"
    )
    low = window.lower()
    assert "warn" in low and "outputd" in low, (
        "the require_outputd_ready fallback must loudly WARN and name outputd so "
        f"the operator can act. Got: {lines[idx : idx + 2]!r}"
    )


def test_recovery_surface_is_wired_after_systemd_units_in_main():
    """Documents *why* non-fatal matters: the operator's recovery surface
    (nginx + the doctor summary) is wired in `main()` AFTER
    `install_systemd_units` — which is where the outputd probe lives. If the
    probe were fatal, a transient miss would skip all of these."""
    text = _install_text()
    m = re.search(r"^main\(\)\s*\{\n(.*?)\n\}", text, re.S | re.M)
    assert m, "could not locate main() body in install.sh"
    body = m.group(1)

    def call_pos(name: str) -> int:
        i = body.find(name)
        assert i != -1, f"{name} is not called in main()"
        return i

    units = call_pos("install_systemd_units")
    assert units < call_pos("install_nginx_site"), (
        "install_nginx_site must run after install_systemd_units so the web UI "
        "exists even if the outputd probe failed"
    )
    assert units < call_pos("run_doctor_summary"), (
        "run_doctor_summary must run after install_systemd_units so a genuine "
        "outputd failure is surfaced through the doctor"
    )
