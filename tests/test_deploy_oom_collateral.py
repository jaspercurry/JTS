# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the deploy-side OOM-collateral surfacing (Workstream B,
problems #2 and #5).

On jts2 (2026-06-21) a source build OOM-killed nginx AND jasper-voice and
the deploy tooling exited silently — the collateral was only discoverable
by SSHing in to read the journal. deploy-to-pi.sh now scans the kernel log
for the install window and surfaces what was killed, gating the deploy
when a *live production daemon* was the victim.

Two layers are pinned here:

* the pure ``oom_killed_units`` / ``oom_killed_comms`` /
  ``oom_unit_is_production`` parsers in scripts/_lib.sh, sourced under
  bash against captured kernel-log text (with the ``set -o pipefail``
  posture deploy-to-pi.sh runs under); and
* the real ``report_oom_collateral`` body from deploy-to-pi.sh, extracted
  and driven with the ssh read stubbed, to pin that it classifies a
  production-daemon kill (sets OOM_PRODUCTION_HIT) vs a build-tool kill
  and stays silent when there was no OOM.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "_lib.sh"
DEPLOY = ROOT / "scripts" / "deploy-to-pi.sh"


# Realistic cgroup-v2 kernel OOM excerpts. A venv console-script daemon
# (jasper-voice) is execve'd as the interpreter, so its process `comm`
# reads `python3` — only the task_memcg cgroup names the unit, which is
# why the gate keys on units, not comms.
_JASPER_VOICE_OOM = (
    "kernel: oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,"
    "mems_allowed=0,global_oom,task_memcg=/system.slice/jasper-voice.service,"
    "task=python3,pid=1234,uid=997\n"
    "kernel: Out of memory: Killed process 1234 (python3) total-vm:512000kB\n"
)
_NGINX_OOM = (
    "kernel: oom-kill:constraint=CONSTRAINT_NONE,task_memcg=/system.slice/"
    "nginx.service,task=nginx,pid=555,uid=33\n"
    "kernel: Memory cgroup out of memory: Killed process 555 (nginx) total-vm:90000kB\n"
)
# A compiler killed during the WebRTC build runs in the transient ssh
# session scope, not a named .service.
_CC1PLUS_OOM = (
    "kernel: oom-kill:constraint=CONSTRAINT_NONE,task_memcg=/user.slice/"
    "user-0.slice/session-5.scope,task=cc1plus,pid=9999,uid=1000\n"
    "kernel: Out of memory: Killed process 9999 (cc1plus) total-vm:430000kB\n"
)


def _lib_fn(fn: str, arg: str, *, pipefail: bool = False) -> subprocess.CompletedProcess[str]:
    prefix = "set -o pipefail; " if pipefail else ""
    script = f'{prefix}source "{LIB}"; {fn} "$1"'
    return subprocess.run(
        ["bash", "-c", script, "bash", arg],
        capture_output=True, text=True, timeout=30,
    )


def _lib_predicate(fn: str, arg: str) -> int:
    script = f'source "{LIB}"; {fn} "$1"'
    return subprocess.run(
        ["bash", "-c", script, "bash", arg],
        capture_output=True, text=True, timeout=30,
    ).returncode


# ── oom_killed_units ─────────────────────────────────────────────────────


def test_oom_killed_units_extracts_services_from_memcg():
    proc = _lib_fn("oom_killed_units", _JASPER_VOICE_OOM + _NGINX_OOM)
    assert proc.returncode == 0, proc.stderr
    assert set(proc.stdout.split()) == {"jasper-voice.service", "nginx.service"}


def test_oom_killed_units_ignores_non_service_cgroups():
    # A build tool's transient session scope is not a unit — empty result.
    proc = _lib_fn("oom_killed_units", _CC1PLUS_OOM)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_oom_killed_units_empty_on_unrelated_text():
    proc = _lib_fn("oom_killed_units", "kernel: usb 1-1: new high-speed device\n")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ── oom_killed_comms ─────────────────────────────────────────────────────


def test_oom_killed_comms_extracts_from_both_line_formats():
    proc = _lib_fn("oom_killed_comms", _JASPER_VOICE_OOM + _CC1PLUS_OOM)
    assert proc.returncode == 0, proc.stderr
    comms = set(proc.stdout.split())
    # `task=python3` + `(python3)`, `task=cc1plus` + `(cc1plus)` → deduped.
    assert comms == {"python3", "cc1plus"}


def test_oom_killed_comms_empty_on_no_match():
    proc = _lib_fn("oom_killed_comms", "nothing interesting here\n")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ── pipefail safety (deploy-to-pi.sh runs under set -euo pipefail) ────────


def test_oom_parsers_are_pipefail_safe_on_no_match():
    # grep returns 1 on no-match; without `|| true` the pipe would abort
    # the deploy script. Pin rc 0 under pipefail with non-matching input.
    for fn in ("oom_killed_units", "oom_killed_comms"):
        proc = _lib_fn(fn, "no oom here\n", pipefail=True)
        assert proc.returncode == 0, (fn, proc.stderr)


# ── oom_unit_is_production ────────────────────────────────────────────────


def test_oom_unit_is_production_true_for_live_daemons():
    for unit in (
        "jasper-voice.service",
        "jasper-outputd.service",
        "nginx.service",
        "shairport-sync.service",
        "librespot.service",
    ):
        assert _lib_predicate("oom_unit_is_production", unit) == 0, unit


def test_oom_unit_is_production_false_for_build_and_unknown():
    for unit in (
        "session-5.scope",
        "user-0.slice",
        "cargo.service",  # not a real JTS daemon
        "",
    ):
        assert _lib_predicate("oom_unit_is_production", unit) == 1, unit


# ── report_oom_collateral (the real deploy-to-pi.sh body) ─────────────────

_OOM_HARNESS = r"""
set -o pipefail
source "@LIB@"
# Extract the real report_oom_collateral() — its def line through the first
# column-0 '}'. eval defines it; nothing else in the deploy script runs.
eval "$(awk '/^report_oom_collateral\(\) \{/{f=1} f{print} f&&/^\}$/{exit}' "@DEPLOY@")"
declare -F report_oom_collateral >/dev/null || { echo "harness: extraction failed" >&2; exit 99; }
# Stub the one external seam: the ssh+journalctl read returns $JOURNAL
# (passed via the environment so journal text with slashes/commas/parens
# needs no shell quoting).
run_remote_sudo() { printf '%s\n' "$JOURNAL"; }
SUDO_INTERACTIVE=0
OOM_PRODUCTION_HIT=0
report_oom_collateral 1700000000
echo "OOM_PRODUCTION_HIT=${OOM_PRODUCTION_HIT}"
"""


def _run_report_oom(journal_text: str) -> subprocess.CompletedProcess[str]:
    script = _OOM_HARNESS.replace("@LIB@", str(LIB)).replace("@DEPLOY@", str(DEPLOY))
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "JOURNAL": journal_text},
    )


def test_report_oom_collateral_flags_production_daemon():
    proc = _run_report_oom(_JASPER_VOICE_OOM + _NGINX_OOM)
    assert proc.returncode == 0, proc.stderr
    # The kills are surfaced on stderr...
    assert "PRODUCTION daemon killed: jasper-voice.service" in proc.stderr
    assert "PRODUCTION daemon killed: nginx.service" in proc.stderr
    # ...and the gate flag is raised for the caller to act on.
    assert "OOM_PRODUCTION_HIT=1" in proc.stdout


def test_report_oom_collateral_build_tool_does_not_flag_production():
    proc = _run_report_oom(_CC1PLUS_OOM)
    assert proc.returncode == 0, proc.stderr
    assert "cc1plus" in proc.stderr               # surfaced as context
    assert "PRODUCTION daemon killed" not in proc.stderr
    assert "no live production daemon among the victims" in proc.stderr
    assert "OOM_PRODUCTION_HIT=0" in proc.stdout   # build OOM doesn't gate


def test_report_oom_collateral_silent_when_no_oom():
    proc = _run_report_oom("")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.strip() == ""
    assert "OOM_PRODUCTION_HIT=0" in proc.stdout
