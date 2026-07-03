# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavior pins for deploy/lib/install/ring-platform.sh (audio-graph
consolidation P1).

These execute the bash helper for real (with the privileged / heavy ops —
mkdir, chown, rsync, rm, sudo, run_contained_build — stubbed as no-op shell
functions so nothing touches /var/cache or the host) rather than eyeballing
the source. The one behavior worth this is the stale-.so signal: the
degrade-to-warn contract leaves a build failure non-fatal, but on a box with
a prior good deploy the PREVIOUS .so stays installed and the doctor cannot
tell it from a fresh one (the 2026-07-02 stale-binary class). The helper must
emit a distinct transcript WARN naming that, so a failed rebuild is not
silently masked as "unchanged".
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RING_PLATFORM_SH = ROOT / "deploy" / "lib" / "install" / "ring-platform.sh"
SO_NAME = "libasound_module_pcm_jts_ring.so"

# The distinct WARN this fix adds; asserted present/absent by ring state.
_STALE_MARKER = "REMAINS in place"


def _has_bash() -> bool:
    return shutil.which("bash") is not None


def _run_build_with_failure(tmp_path: Path, *, stale_so: bool) -> str:
    """Source ring-platform.sh and run build_install_jts_ring_ioplug with a
    forced build failure. When stale_so is True a prior .so pre-exists at the
    install dest. Returns combined stdout+stderr.

    All privileged / heavy commands are shadowed by no-op shell functions so
    the helper runs hermetically (no /var/cache write, no real rsync/chown)."""
    plugin_dir = tmp_path / "plugindir"
    plugin_dir.mkdir()
    repo_src = tmp_path / "repo" / "c" / "jts-ring-ioplug"
    repo_src.mkdir(parents=True)
    if stale_so:
        (plugin_dir / SO_NAME).write_bytes(b"\x7fELF stale so")

    script = f"""
set -euo pipefail
REPO_DIR="{tmp_path}/repo"
BUILD_USER="nobody"
JTS_RING_ALSA_PLUGIN_DIR="{plugin_dir}"
export JTS_RING_ALSA_PLUGIN_DIR
# Shadow the privileged / heavy ops so nothing touches the host, and force
# the contained build to fail (the branch under test).
mkdir() {{ :; }}
chown() {{ :; }}
rsync() {{ :; }}
rm() {{ :; }}
sudo() {{ :; }}
run_contained_build() {{ return 1; }}
source "{RING_PLATFORM_SH}"
build_install_jts_ring_ioplug
"""
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The helper returns 0 (degrade-to-warn: a build failure never fails install).
    assert proc.returncode == 0, (
        f"build_install_jts_ring_ioplug should not fail the install; "
        f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
    return proc.stdout + proc.stderr


@pytest.mark.skipif(not _has_bash(), reason="bash required")
def test_build_failure_with_prior_so_warns_it_is_stale(tmp_path):
    out = _run_build_with_failure(tmp_path, stale_so=True)
    # Both the generic degrade-to-warn lines AND the distinct stale-.so line.
    assert "ring platform unavailable" in out
    assert _STALE_MARKER in out
    assert "doctor cannot distinguish" in out


@pytest.mark.skipif(not _has_bash(), reason="bash required")
def test_first_ever_build_failure_omits_stale_warning(tmp_path):
    # No prior .so at the dest: this is the honest "asset missing -> doctor
    # warns" shape, so the stale-.so line must NOT fire (it would be a false
    # claim that a stale binary is installed).
    out = _run_build_with_failure(tmp_path, stale_so=False)
    assert "ring platform unavailable" in out
    assert _STALE_MARKER not in out


@pytest.mark.skipif(not _has_bash(), reason="bash required")
def test_build_failure_does_not_remove_the_prior_so(tmp_path):
    # The degrade path must LEAVE the prior .so installed (strictly less broken
    # than none in the inert phase). We assert the file survives by having the
    # (real) dest .so present before and after — the helper's own rm is stubbed
    # here, but the branch must not add its own removal.
    plugin_dir = tmp_path / "plugindir"
    plugin_dir.mkdir()
    repo_src = tmp_path / "repo" / "c" / "jts-ring-ioplug"
    repo_src.mkdir(parents=True)
    dest_so = plugin_dir / SO_NAME
    dest_so.write_bytes(b"\x7fELF stale so")

    script = f"""
set -euo pipefail
REPO_DIR="{tmp_path}/repo"
BUILD_USER="nobody"
JTS_RING_ALSA_PLUGIN_DIR="{plugin_dir}"
export JTS_RING_ALSA_PLUGIN_DIR
mkdir() {{ :; }}
chown() {{ :; }}
rsync() {{ :; }}
# NOTE: rm is intentionally NOT stubbed here, so a stray `rm -f "${{so_dest}}"`
# in the failure branch would really delete the file and fail this test.
sudo() {{ :; }}
run_contained_build() {{ return 1; }}
source "{RING_PLATFORM_SH}"
build_install_jts_ring_ioplug
"""
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert dest_so.exists(), "failure branch must not remove the prior .so"
