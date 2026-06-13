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
INSTALL_LIB_DIR = ROOT / "deploy" / "lib" / "install"


def _install_text() -> str:
    paths = [INSTALL_SH, *sorted(INSTALL_LIB_DIR.glob("*.sh"))]
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _call_site_index(lines: list[str]) -> int:
    """Index of the single line that INVOKES require_outputd_ready (i.e. not
    its `require_outputd_ready() {` definition, and not a comment). Uses a
    substring match so it finds the call under either non-fatal idiom — the
    `cmd || <warn>` fallback or an `if ! cmd; then <warn> fi` wrapper."""
    hits = [
        i
        for i, line in enumerate(lines)
        if "require_outputd_ready" in line
        and "require_outputd_ready()" not in line
        and not line.lstrip().startswith("#")
    ]
    assert len(hits) == 1, (
        f"expected exactly one require_outputd_ready call site, found {len(hits)} "
        f"(at line numbers {[h + 1 for h in hits]})"
    )
    return hits[0]


def _logical_line(lines: list[str], idx: int) -> str:
    """Reconstruct one logical shell line starting at `idx`, joining physical
    lines linked by a trailing backslash. Lets the non-fatal check see a guard
    that sits after a `\\`-continuation without false-matching a `||` that
    belongs to an unrelated later command."""
    parts = [lines[idx]]
    while parts[-1].rstrip().endswith("\\") and idx + 1 < len(lines):
        idx += 1
        parts.append(lines[idx])
    return " ".join(p.rstrip().rstrip("\\") for p in parts)


def test_require_outputd_ready_call_is_non_fatal_and_loud():
    """The call must be non-fatal — guarded so `set -euo pipefail` cannot abort
    the install on a transient probe miss — AND still WARN loudly about outputd
    (the project's 'no silent failure' bar; non-fatal is not silent). Asserts
    the *property*, not one idiom: a `|| <warn>` fallback and an
    `if ! …; then <warn> fi` wrapper both pass; a bare statement does not."""
    lines = _install_text().splitlines()
    idx = _call_site_index(lines)
    call_line = lines[idx]
    stripped = call_line.strip()
    # `set -e` is suppressed only when the command is on the LHS of `||` or is
    # the condition of an if/while/until. `&&` does NOT count — a failed LHS
    # still propagates its non-zero status and aborts under set -e.
    logical = _logical_line(lines, idx)
    non_fatal = "||" in logical or stripped.startswith(
        ("if ", "if!", "while ", "until ", "elif ")
    )
    assert non_fatal, (
        "require_outputd_ready must be non-fatal — guard it with `|| <warn>` or "
        "wrap it in an `if`. A bare call aborts the install on a transient "
        f"failure under set -e. Got: {call_line!r}"
    )
    # The WARN may sit a couple of lines down (a `then` block or after a
    # `\`-continuation); a small window keeps the 'no silent failure' check
    # idiom-independent without matching an unrelated distant WARN.
    low = " ".join(lines[idx : idx + 3]).lower()
    assert "warn" in low and "outputd" in low, (
        "the require_outputd_ready failure path must loudly WARN and name "
        f"outputd so the operator can act. Got: {lines[idx : idx + 3]!r}"
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

    full_branch_start = body.find('if [[ "${install_profile}" == "endpoint" ]]')
    assert full_branch_start != -1, "could not locate endpoint branch in main()"
    body = body[full_branch_start:]
    full_branch_start = body.find("require_build_user")
    assert full_branch_start != -1, "could not locate full install branch in main()"
    body = body[full_branch_start:]

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
