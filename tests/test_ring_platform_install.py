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

import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RING_PLATFORM_SH = ROOT / "deploy" / "lib" / "install" / "ring-platform.sh"
SO_NAME = "libasound_module_pcm_jts_ring.so"

# The distinct WARN this fix adds; asserted present/absent by ring state.
_STALE_MARKER = "REMAINS in place"


def _provenance_preamble(tmp_path: Path) -> str:
    """Point the provenance record at the tmpdir, in EVERY script this file runs.

    ``revoke_ring_ioplug_provenance`` runs on the failure paths these tests
    exercise, and it ``rm -f``s ``JTS_RING_IOPLUG_PROVENANCE`` — which without
    an override is the REAL absolute path ``/var/lib/jasper/ring-ioplug.provenance``.
    A test reaching outside its tmpdir at an absolute system path is the defect
    regardless of whether that file happens to exist on the machine running it;
    "harmless here" is a property of the host, not of the test.

    The override goes in ALL THREE, including the two that shadow ``rm`` with a
    no-op shell function. A stub hides the reach rather than removing it, and a
    later edit that decides the stub is unnecessary — the third test already
    makes exactly that call, deliberately, so that a stray ``rm`` of the ``.so``
    would fail it — would silently restore the reach.
    """
    return (
        f'JTS_RING_IOPLUG_PROVENANCE="{tmp_path}/ring-ioplug.provenance"\n'
        "export JTS_RING_IOPLUG_PROVENANCE"
    )


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
{_provenance_preamble(tmp_path)}
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
    # The WARN used to say the doctor "cannot distinguish it from a fresh
    # build". It can now: a failed build REVOKES the installer's provenance
    # record, so the plugin reads as unvouched instead of ok. The transcript
    # says what is true today.
    assert "provenance record has been revoked" in out
    assert "doctor cannot distinguish" not in out
    # The WARN also used to promise that "loopback coupling remains the
    # transport (inert phase)". Boxes arm the ring now, so on an armed box that
    # sentence is false in both halves — the transport is the ring, and nothing
    # about the platform is inert. What the transcript says instead is which
    # doctor check carries the verdict, pinned by name in
    # test_ring_ioplug_provenance.py.
    assert "inert phase" not in out


@pytest.mark.skipif(not _has_bash(), reason="bash required")
def test_first_ever_build_failure_omits_stale_warning(tmp_path):
    # No prior .so at the dest: this is the honest "asset missing -> doctor
    # warns" shape, so the stale-.so line must NOT fire (it would be a false
    # claim that a stale binary is installed).
    out = _run_build_with_failure(tmp_path, stale_so=False)
    assert "ring platform unavailable" in out
    assert _STALE_MARKER not in out


@pytest.mark.skipif(not _has_bash(), reason="bash required")
def test_conf_asset_echoes_state_what_was_placed_not_what_the_box_is_doing(tmp_path):
    """Every deploy prints these two lines, so they must hold on an ARMED box.

    Both used to label the freshly-placed conf.d and tmpfiles assets `inert` —
    false where it matters most, because on a box whose coupling is armed those
    exact files carry the audio. This helper cannot see the coupling from here,
    so the honest line states what it PLACED rather than what the box is doing.

    The renderer-lane echo is the shape that survives, and asserting it still
    prints is deliberate: its `inert` is CONDITIONED on an arm that has not
    happened ("until a lane is armed"), which is a statement about that lane's
    state rather than a claim about the platform, so a later sweep of this
    vocabulary must not take it with the rest.
    """
    repo = tmp_path / "repo"
    (repo / "deploy" / "alsa" / "conf.d").mkdir(parents=True)
    (repo / "deploy" / "tmpfiles").mkdir(parents=True)
    for rel in (
        "deploy/alsa/conf.d/60-jts-ring.conf",
        "deploy/alsa/conf.d/61-jts-renderer-lanes.conf",
        "deploy/alsa/conf.d/62-jts-ring-grouping.conf",
        "deploy/tmpfiles/jts-ring.conf",
    ):
        (repo / rel).write_text("# fixture\n", encoding="utf-8")
    proc = _run_sh(
        tmp_path,
        "install() { :; }\n"
        "systemd-tmpfiles() { :; }\n"
        "install_jts_ring_conf_assets\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert "60-jts-ring.conf (pcm.jts_ring_capture" in out
    assert (
        "Installed /etc/alsa/conf.d/62-jts-ring-grouping.conf "
        "(pcm.jts_ring_grouping; the bonded endpoint's snapcast ingress)" in out
    )
    assert "Installed /etc/tmpfiles.d/jts-ring.conf and applied /dev/shm/jts-ring" in out
    # The unconditioned label, in both parenthetical spellings it had.
    assert "; inert)" not in out
    assert "(inert)" not in out
    assert "inert until a lane is armed" in out


# --- the provenance helpers, EXECUTED ---------------------------------------
#
# `_jts_ring_ioplug_caps` interrogates the built artifact for diagnostic string
# literals the compiler embedded at each conf.d field's parse site, and
# `record_ring_ioplug_provenance` / `revoke_ring_ioplug_provenance` write and
# withdraw the installer's claim about it. Everything downstream — the doctor's
# provenance verdict and the coupling reconciler's fail-closed capability gate —
# reads what these three produce, so they are run rather than eyeballed.
#
# The markers are pinned against the C source in test_ring_ioplug_provenance.py;
# what is proven HERE is that the shell actually finds them and emits the record
# the Python reader parses.
_CAP_FORMAT_MARKER = "format %s unsupported (S16_LE|S32_LE)"
_CAP_CHANNELS_MARKER = "channels out of range 2..=8"


def _run_sh(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    """Source the installer and run `body`, hermetically and under `set -u`.

    `set -u` is not incidental: the caps helper builds a bash array that is
    EMPTY for an uncapable plugin, and bash 3.2 (the macOS system bash this lane
    runs on) aborts on an unguarded empty-array expansion under `set -u`. The
    uncapable plugin is the whole point of the probe, so running without `set -u`
    would test the helper on everything except its most important input.
    """
    script = f"""
set -euo pipefail
REPO_DIR="{tmp_path}/repo"
BUILD_USER="nobody"
JTS_RING_ALSA_PLUGIN_DIR="{tmp_path}/plugindir"
{_provenance_preamble(tmp_path)}
source "{RING_PLATFORM_SH}"
{body}
"""
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )


def _fake_so(tmp_path: Path, *, markers: tuple[str, ...]) -> Path:
    """A binary-ish file carrying the given capability marker literals.

    Bytes around the markers, because the real probe greps a compiled ELF and
    uses `grep -a`; a pure-text fixture would not exercise that.
    """
    so = tmp_path / "fake.so"
    blob = b"\x7fELF\x02\x01\x01\x00\x00\xff\xfe"
    for m in markers:
        blob += b"\x00" + m.encode() + b"\x00\xfe"
    so.write_bytes(blob + b"\x00\x01\x02")
    return so


@pytest.mark.skipif(not _has_bash(), reason="bash required")
@pytest.mark.parametrize(
    ("markers", "expected"),
    [
        ((), ""),
        ((_CAP_FORMAT_MARKER,), "wire_format"),
        ((_CAP_CHANNELS_MARKER,), "wire_channels"),
        ((_CAP_FORMAT_MARKER, _CAP_CHANNELS_MARKER), "wire_format,wire_channels"),
    ],
)
def test_caps_probe_reads_capabilities_off_the_artifact(tmp_path, markers, expected):
    """One case per capability subset, INCLUDING the empty one.

    The empty case is a pre-ring-v2 plugin — the honest answer is no
    capabilities, and it must be an empty string rather than an abort: the
    installer runs under `set -euo pipefail`, so a failure here would take the
    whole deploy step with it.
    """
    so = _fake_so(tmp_path, markers=markers)
    proc = _run_sh(tmp_path, f'_jts_ring_ioplug_caps "{so}"')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == expected


@pytest.mark.skipif(not _has_bash(), reason="bash required")
def test_record_then_revoke_round_trips_through_the_python_reader(tmp_path):
    """The installer WRITES what jasper.ring_assets READS, and revoke undoes it.

    The two halves are in different languages, so nothing but an executed
    round-trip proves the key names and the comma-joined caps value survive the
    boundary. A record the reader cannot parse answers `recorded=False`, which
    the reconciler's gate treats as fail-closed — a wide wire would be refused
    on a box whose plugin is in fact capable, and the installer transcript would
    say it recorded one.
    """
    from jasper import ring_assets

    so = _fake_so(tmp_path, markers=(_CAP_FORMAT_MARKER, _CAP_CHANNELS_MARKER))
    provenance = tmp_path / "ring-ioplug.provenance"

    proc = _run_sh(tmp_path, f'record_ring_ioplug_provenance "{so}" deadbeefcafe')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "recorded ioplug provenance" in proc.stdout
    assert "caps=[wire_format,wire_channels]" in proc.stdout

    record = ring_assets.read_ring_ioplug_provenance(str(provenance))
    assert record.recorded is True
    assert record.sha256 == "deadbeefcafe"
    assert record.caps == {
        ring_assets.RING_CAP_WIRE_FORMAT,
        ring_assets.RING_CAP_WIRE_CHANNELS,
    }

    proc = _run_sh(tmp_path, "revoke_ring_ioplug_provenance")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cannot vouch" in proc.stderr
    assert not provenance.exists()
    assert ring_assets.read_ring_ioplug_provenance(str(provenance)).recorded is False


@pytest.mark.skipif(not _has_bash(), reason="bash required")
def test_recording_an_uncapable_plugin_is_a_record_with_no_caps(tmp_path):
    """VOUCHED and CAPABILITY-LESS are two different facts.

    A pre-ring-v2 plugin THIS deploy built is genuinely the installed one, so
    the record must exist (not stale) while claiming nothing (so a wide wire is
    still refused). Collapsing the two would either vouch for capabilities the
    plugin lacks or report a fresh build as stale.
    """
    from jasper import ring_assets

    so = _fake_so(tmp_path, markers=())
    proc = _run_sh(tmp_path, f'record_ring_ioplug_provenance "{so}" abc123')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "caps=[none]" in proc.stdout, "the transcript must not print an empty []"

    record = ring_assets.read_ring_ioplug_provenance(
        str(tmp_path / "ring-ioplug.provenance")
    )
    assert record.recorded is True
    assert record.caps == frozenset()


@pytest.mark.skipif(not _has_bash(), reason="bash required")
def test_revoking_an_absent_record_is_silent_and_successful(tmp_path):
    """Revoke runs on paths where there may never have been a record (a first-ever
    build failure). It must be a clean no-op, not a WARN claiming it withdrew
    something, and not a non-zero exit inside `set -e`."""
    proc = _run_sh(tmp_path, "revoke_ring_ioplug_provenance")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cannot vouch" not in proc.stderr


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
{_provenance_preamble(tmp_path)}
mkdir() {{ :; }}
chown() {{ :; }}
rsync() {{ :; }}
# rm is intentionally NOT stubbed here, so a stray `rm -f "${{so_dest}}"` in the failure branch would
# really delete the file and fail this test. That is also why the provenance override above is
# load-bearing rather than belt: the revoke on this path runs a REAL rm, and without the override
# its target is /var/lib/jasper/ring-ioplug.provenance on the host running the test.
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


@pytest.mark.skipif(not _has_bash(), reason="bash required")
def test_record_provenance_does_not_rechmod_an_existing_state_dir(tmp_path):
    """The default provenance path's parent is STATE_DIR itself
    (/var/lib/jasper), so `install -d -m 0755 "$(dirname "$JTS_RING_IOPLUG_PROVENANCE")"`
    re-chmods an EXISTING STATE_DIR on every deploy — the same trap #3879/#3930
    closed elsewhere, left open here. Pin that an existing parent dir never
    reaches `install` at all (via a fake `install` on PATH that records
    invocations) and that its mode survives untouched."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(0o770)  # mkdir(mode=...) is masked by umask; chmod isn't
    provenance = state_dir / "ring-ioplug.provenance"
    so = _fake_so(tmp_path, markers=())

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "install.calls"
    fake_install = bin_dir / "install"
    fake_install.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> "
        + shlex.quote(str(calls))
        + "\nexit 0\n",
        encoding="utf-8",
    )
    fake_install.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    script = f"""
set -euo pipefail
REPO_DIR="{tmp_path}/repo"
BUILD_USER="nobody"
JTS_RING_ALSA_PLUGIN_DIR="{tmp_path}/plugindir"
JTS_RING_IOPLUG_PROVENANCE="{provenance}"
export JTS_RING_IOPLUG_PROVENANCE
source "{RING_PLATFORM_SH}"
record_ring_ioplug_provenance "{so}" deadbeef
"""
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not calls.exists(), (
        "record_ring_ioplug_provenance called `install -d` on an existing "
        f"parent dir: {calls.read_text(encoding='utf-8') if calls.exists() else ''}"
    )
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o770
