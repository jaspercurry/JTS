# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavioral test for deploy-to-pi.sh's verify_manifest_advanced gate
(Workstream B, problem #4).

install.sh writes the build manifest ONLY as its final step, so after a
successful deploy the Pi's manifest must record the deployed full SHA with
JASPER_INSTALL_STATUS=ok. verify_manifest_advanced proves that end-to-end
and FAILS the deploy on a mismatch (the install didn't run to completion).

This is a deploy-failing gate, so it gets a behavioral pin, not just the
structural one in test_deploy_wiring_guards.py. We drive the real function
body — extracted from the script and sourced — stubbing only its external
seams (the ssh manifest read and the maintenance-marker finalizer). The
DECISION (match vs mismatch, status check) stays real, using the real
build_manifest_value parser from _lib.sh.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "_lib.sh"
DEPLOY = ROOT / "scripts" / "deploy-to-pi.sh"

_FULL = "abc1234deadbeefcafe5678" + "0" * 17  # 40-ish chars, shape only


_HARNESS = r"""
set -o pipefail
source "@LIB@"
# Extract the real verify_manifest_advanced() — def line through the first
# column-0 '}'. eval defines it; nothing else in the deploy script runs.
eval "$(awk '/^verify_manifest_advanced\(\) \{/{f=1} f{print} f&&/^\}$/{exit}' "@DEPLOY@")"
declare -F verify_manifest_advanced >/dev/null || { echo "harness: extraction failed" >&2; exit 99; }
# Stub the external seams: the ssh manifest read returns $MANIFEST; the
# maintenance finalizer is a no-op (it would otherwise try to ssh).
run_remote_sudo() { printf '%s\n' "$MANIFEST"; }
finish_airplay_health_maintenance() { :; }
trap - EXIT
SHA_FULL="@FULL@"; DIRTY=""; SHA="${SHA_FULL:0:8}"; PI_HOST="bench-pi.local"
verify_manifest_advanced
"""


def _run_verify(manifest: str, *, full: str = _FULL) -> subprocess.CompletedProcess[str]:
    script = _HARNESS.replace("@LIB@", str(LIB)).replace("@DEPLOY@", str(DEPLOY)).replace("@FULL@", full)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "MANIFEST": manifest},
    )


def _manifest(full: str, status: str = "ok") -> str:
    return (
        f"JASPER_GIT_SHA={full[:8]}\n"
        f"JASPER_GIT_SHA_FULL={full}\n"
        "JASPER_GIT_BRANCH=main\n"
        "JASPER_INSTALL_AT=2026-06-21T00:00:00-04:00\n"
        f"JASPER_INSTALL_STATUS={status}\n"
    )


def test_verify_passes_when_manifest_matches_sha_and_status_ok():
    proc = _run_verify(_manifest(_FULL))
    assert proc.returncode == 0, proc.stderr
    assert "build manifest advanced" in proc.stdout


def test_verify_fails_when_manifest_sha_differs():
    # install.sh writes the manifest last, so a different SHA means the
    # install didn't run to completion — fail the deploy.
    proc = _run_verify(_manifest("f" * 40))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "DEPLOY VERIFICATION FAILED" in proc.stderr
    assert "did not run to completion" in proc.stderr


def test_verify_fails_when_status_not_ok():
    # A SHA match but a missing/!=ok status means an old-format or
    # incomplete manifest — also a failure.
    proc = _run_verify(_manifest(_FULL, status="pending"))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "DEPLOY VERIFICATION FAILED" in proc.stderr


def test_verify_fails_when_no_manifest():
    proc = _run_verify("")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "DEPLOY VERIFICATION FAILED" in proc.stderr
