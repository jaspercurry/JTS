# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor correction domain."""

import grp
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import jasper.active_speaker._common as _common
import jasper.active_speaker.setup_status as setup_status_mod
from jasper.cli import doctor
from jasper.correction import bundles

from .correction_bundle_fixtures import write_golden_correction_bundle

from .doctor_test_support import (
    _pretend_group_is_jasper,
    _registered_check_names,
    _write_identity_env,
)


def test_check_correction_web_service_ok_when_socket_active(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        unit = cmd[-1]
        out = "active\n" if unit.endswith(".socket") else "inactive\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_web_service()
    assert r.status == "ok"
    assert "socket active" in r.detail


# ---------- #1860: jasper-doctor check for long-outstanding idle-exit holds


def test_check_correction_idle_exit_holds_registered_in_sync_checks():
    assert "check_correction_idle_exit_holds" in _registered_check_names()


def test_check_correction_idle_exit_holds_skips_when_service_not_active(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        assert cmd[:2] == ["systemctl", "is-active"]
        return subprocess.CompletedProcess(cmd, 0, stdout="inactive\n", stderr="")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "ok"
    assert "not running" in r.detail


def test_check_correction_idle_exit_holds_skips_when_journalctl_unavailable(
    monkeypatch,
):
    """N-B1: the exception path must not collapse to a generic, indistinguishable
    "unavailable" — the actual exception text must be visible so a broken
    invocation reads differently from a clean no-findings run."""

    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        raise FileNotFoundError("journalctl not found")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "ok"
    assert "journalctl" in r.detail
    assert "journalctl not found" in r.detail
    assert "FileNotFoundError" in r.detail


def test_check_correction_idle_exit_holds_skips_on_nonzero_journalctl_returncode(
    monkeypatch,
):
    """N-B1: a malformed invocation (e.g. a bad --since) exits non-zero without
    raising — that stderr must reach the detail string too, or this is
    indistinguishable from a clean no-findings run forever."""

    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        assert cmd[0] == "journalctl"
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="journalctl: invalid option -- since\n",
        )

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "ok"
    assert "rc=1" in r.detail
    assert "invalid option" in r.detail


def test_check_correction_idle_exit_holds_ok_with_no_deferred_warning(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        assert cmd[0] == "journalctl"
        assert "warning" in cmd  # -p warning: only escalated lines are fetched
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "ok"
    assert "no long-outstanding holds" in r.detail


def test_check_correction_idle_exit_holds_warns_on_leaked_hold(monkeypatch):
    """Mirrors the exact rendered line from _systemd.py's _log_deferred_exit
    once HOLD_LEAK_WARN_AFTER_SEC is crossed (#1856's escalation), proving
    this check surfaces the SAME evidence at the doctor surface (#1860)."""
    leaked_line = (
        "systemd idle-exit deferred: 1 active requests/holds after 7530s "
        "idle, busy for 7530s (threshold 600s, holds: capture:level_ramp:room) "
        "— busy past 7200s, so this is a LEAKED hold, not a long session: "
        "the process can no longer idle-exit and its on-idle-exit hook "
        "cannot run"
    )

    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        assert cmd[0] == "journalctl"
        return subprocess.CompletedProcess(cmd, 0, stdout=leaked_line + "\n", stderr="")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "warn"
    assert "7530s" in r.detail
    assert "capture:level_ramp:room" in r.detail


def test_check_correction_idle_exit_holds_reports_the_latest_of_several_lines(
    monkeypatch,
):
    """journalctl returns oldest-first; an older (possibly since-resolved)
    line must not shadow the most recent evidence."""
    older = (
        "systemd idle-exit deferred: 1 active requests/holds after 7300s "
        "idle, busy for 7300s (threshold 600s, holds: capture:crossover_v2:session) "
        "— busy past 7200s, so this is a LEAKED hold, not a long session: "
        "the process can no longer idle-exit and its on-idle-exit hook "
        "cannot run"
    )
    newer = (
        "systemd idle-exit deferred: 1 active requests/holds after 7830s "
        "idle, busy for 7830s (threshold 600s, holds: capture:level_ramp:crossover) "
        "— busy past 7200s, so this is a LEAKED hold, not a long session: "
        "the process can no longer idle-exit and its on-idle-exit hook "
        "cannot run"
    )

    def fake_run(cmd, timeout=5.0):
        if cmd[0] == "systemctl":
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        assert cmd[0] == "journalctl"
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"{older}\n{newer}\n",
            stderr="",
        )

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_idle_exit_holds()
    assert r.status == "warn"
    assert "7830s" in r.detail
    assert "capture:level_ramp:crossover" in r.detail
    assert "7300s" not in r.detail


def test_check_correction_https_assets_registered_in_sync_checks():
    assert "check_correction_https_assets" in _registered_check_names()


def _web_root_with_app_css(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.css").write_text("/* x */", encoding="utf-8")
    return tmp_path


def test_check_correction_https_assets_ok_on_200(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))
    monkeypatch.setattr(
        doctor.correction, "_probe_https_status", lambda *a, **k: (200, "")
    )
    r = doctor.check_correction_https_assets()
    assert r.status == "ok"
    assert "200" in r.detail


def test_check_correction_https_assets_warns_on_http_downgrade(monkeypatch, tmp_path):
    # The bug signature: an HTTPS asset downgrade to http:// → browsers
    # mixed-content-block it.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))
    monkeypatch.setattr(
        doctor.correction,
        "_probe_https_status",
        lambda *a, **k: (308, "http://jts.local/assets/app.css"),
    )
    r = doctor.check_correction_https_assets()
    assert r.status == "warn"
    assert "mixed-content" in r.detail.lower()
    assert "443" in r.detail


def test_check_correction_https_assets_skips_without_web_root(monkeypatch, tmp_path):
    # Dev checkout: no /usr/share/jasper-web/assets/app.css → skip, never probes.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise AssertionError("must not probe when the web root is absent")

    monkeypatch.setattr(doctor.correction, "_probe_https_status", _boom)
    r = doctor.check_correction_https_assets()
    assert r.status == "ok"
    assert "skip" in r.detail.lower()


def test_check_correction_https_assets_skips_when_443_unreachable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))

    def _refused(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor.correction, "_probe_https_status", _refused)
    r = doctor.check_correction_https_assets()
    assert r.status == "ok"
    assert "not reachable" in r.detail.lower()


def test_check_correction_state_dirs_warns_on_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_CORRECTION_ROOT", str(tmp_path / "missing"))
    r = doctor.check_correction_state_dirs()
    assert r.status == "warn"
    assert "missing" in r.detail


# ---------- nanny-audit item 10: os.access(W_OK) always reports root's own
# access, not the dropped jasper-web writer's — a root:root 0700 dir (the
# fresh-install bug: nothing minted these before this same box's fix in
# install.sh, so the first root-lane writer created them with a bare mkdir)
# read as "ok" while jasper-web was locked out.


def _own_group() -> str:
    return grp.getgrgid(os.getgid()).gr_name


@pytest.mark.parametrize(
    "mode, group, expect_flagged",
    [
        (0o2770, None, False),
        (0o2750, None, True),  # the exact regression: group-write stripped
        # setgid lost (2770 -> 0770): a root-run process creating a NEW
        # subdirectory later (e.g. a session folder) would land it
        # group-root, not group-jasper, locking the non-root arm out of it.
        (0o0770, None, True),
        (0o2770, "jts-no-such-group-xyz", True),
        (0o0700, None, True),  # the fresh-install bug: no group bits at all
    ],
    ids=[
        "group-writable", "group-readonly", "setgid-lost", "wrong-group",
        "owner-only-0700",
    ],
)
def test_not_writable_by_group_verdicts(tmp_path, mode, group, expect_flagged):
    d = tmp_path / "sweeps"
    d.mkdir()
    os.chmod(d, mode)

    flagged = doctor.correction._not_writable_by_group(
        [d], expected_group=group or _own_group()
    )

    assert (str(d) in flagged) is expect_flagged


def test_check_correction_state_dirs_fails_when_locked_out_by_mode(
    monkeypatch, tmp_path
):
    """os.access(p, os.W_OK) is always True for jasper-doctor's own root
    caller regardless of a directory's actual mode, so it cannot see that the
    dropped jasper-web writer is locked out. The fix must FAIL and name the
    locked-out service user instead of the caller's own (irrelevant) access.

    _pretend_group_is_jasper pins the group match so MODE ALONE (0700 — no
    group write/search bits, the fresh-install shape) is what fails this,
    not an incidental group mismatch with the dev/CI box's own group
    (without it, this test would still pass with the mode arm deleted
    entirely, since a plain tmp_path dir's real group is never "jasper" on a
    dev machine — that would make it dishonest about what it pins). The
    group-mismatch and setgid-loss arms are covered directly by the
    parametrized test_not_writable_by_group_verdicts above.

    CheckResult carries no structured field for "which identity is locked
    out", so `"jasper-web" in r.detail` stays a detail-string assertion —
    the same convention this module already uses for other identifying
    substrings (e.g. "socket active" above)."""
    _pretend_group_is_jasper(monkeypatch)
    root = tmp_path / "correction"
    root.mkdir()
    os.chmod(root, 0o700)
    for name in ("sweeps", "captures", "sessions", "calibration_mics"):
        d = root / name
        d.mkdir()
        os.chmod(d, 0o700)
    monkeypatch.setenv("JASPER_CORRECTION_ROOT", str(root))

    r = doctor.check_correction_state_dirs()

    assert r.status == "fail"
    assert "jasper-web" in r.detail


def test_check_correction_uploaded_calibration_sign_flags_only_uploads(
    monkeypatch,
    tmp_path,
):
    """Vendor records are repaired automatically on deploy; an UPLOADED
    record's convention is the household's own declaration, so the doctor
    surfaces it for review instead of anyone flipping it silently."""
    from jasper.audio_measurement import calibration as cal

    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path))
    cal.store_calibration(
        text="20 -1\n1000 0\n20000 2\n",
        provider="manual_upload",
        model="other",
        label="Lab mic",
        source="uploaded:lab.txt",
        sign_convention="correction",
        root=tmp_path,
    )
    cal.store_calibration(  # a vendor record: not this check's business
        text="20 -1\n1000 0\n20000 2\n",
        provider="minidsp",
        model="minidsp_umik2",
        label="UMIK-2",
        source="vendor_lookup",
        serial="810-8494",
        sign_convention="correction",
        root=tmp_path,
    )

    r = doctor.check_correction_uploaded_calibration_sign()
    assert r.status == "warn"
    assert "Lab mic" in r.detail
    assert "1 uploaded calibration(s)" in r.detail
    assert "/correction/" in r.detail

    # An upload that already declares the response convention is clean, and
    # the check never fails the doctor either way.
    cal.store_calibration(
        text="20 -3\n1000 0\n20000 4\n",
        provider="manual_upload",
        model="other",
        label="Other mic",
        source="uploaded:other.txt",
        sign_convention="response",
        root=tmp_path,
    )
    assert doctor.check_correction_uploaded_calibration_sign().status == "warn"

    monkeypatch.setenv("JASPER_CORRECTION_CALIBRATION_DIR", str(tmp_path / "empty"))
    clean = doctor.check_correction_uploaded_calibration_sign()
    assert clean.status == "ok"


def test_check_correction_current_config_reports_missing_config(
    monkeypatch,
    tmp_path,
):
    statefile = tmp_path / "statefile.yml"
    missing = tmp_path / "does-not-exist.yml"
    statefile.write_text(f"config_path: {missing}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    r = doctor.check_correction_current_config()
    assert r.status == "fail"
    assert "missing config" in r.detail


def test_check_correction_current_config_reports_flat_base(
    monkeypatch,
    tmp_path,
):
    statefile = tmp_path / "statefile.yml"
    base = tmp_path / "v1.yml"
    base.write_text("# base\n")
    statefile.write_text(f"config_path: {base}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    r = doctor.check_correction_current_config()
    assert r.status == "warn"
    assert "custom/non-JTS" in r.detail


def test_check_correction_current_config_reports_jts_sound_config(
    monkeypatch,
    tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "sound_current.yml"
    generated.write_text(
        "# Source: jasper.sound.camilla_yaml.emit_sound_config\n"
        "filters:\n"
        "  flat:\n"
        "    type: Gain\n"
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "JTS sound preference" in r.detail
    assert "no room correction" in r.detail.lower()


def test_check_correction_current_config_reports_active_speaker_baseline(
    monkeypatch,
    tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "active_speaker_baseline.yml"
    generated.write_text(
        "# Source: jasper.active_speaker.camilla_yaml."
        "emit_active_speaker_baseline_config\n"
        "filters:\n"
        "  active_baseline_headroom:\n"
        "    type: Gain\n",
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "JTS active-speaker baseline" in r.detail
    assert "managed by the active-speaker" in r.detail


def test_check_correction_current_config_reports_active_leader_program_bake(
    monkeypatch,
    tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "grouping_active_leader_bake.yml"
    generated.write_text(
        "# Source: jasper.active_speaker.camilla_yaml."
        "emit_active_speaker_program_bake_config\n"
        "devices:\n"
        "  playback:\n"
        "    type: File\n",
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "JTS active-leader program bake" in r.detail
    assert "managed by the active-speaker" in r.detail


def test_check_correction_current_config_reports_generated_correction(
    monkeypatch,
    tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "correction_abc_1700000000.yml"
    generated.write_text("filters:\n  room_peq_1:\n    type: Biquad\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "session=abc" in r.detail
    assert "peqs=1" in r.detail


def test_check_camilla_volume_limit_ok(monkeypatch, tmp_path):
    config = tmp_path / "v1.yml"
    config.write_text("devices:\n  samplerate: 48000\n  volume_limit: 0.0\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_volume_limit()

    assert r.status == "ok"
    assert "volume_limit=0.0" in r.detail


def _stage_ring_config(tmp_path, monkeypatch, chunksize: int) -> None:
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE, RING_PLAYBACK_DEVICE

    config = tmp_path / "ring.yml"
    config.write_text(
        "devices:\n"
        "  samplerate: 48000\n"
        f"  chunksize: {chunksize}\n"
        "  capture:\n"
        "    type: Alsa\n"
        f'    device: "{RING_CAPTURE_DEVICE}"\n'
        "  playback:\n"
        "    type: Alsa\n"
        f'    device: "{RING_PLAYBACK_DEVICE}"\n'
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))


def test_check_camilla_ring_chunk_fails_over_capacity(monkeypatch, tmp_path):
    """jts4's shape: a chunk the ring cannot open, so the box is silent."""
    from jasper.fanin_coupling import ring_capacity_frames

    _stage_ring_config(tmp_path, monkeypatch, ring_capacity_frames() * 4)

    r = doctor.check_camilla_ring_chunk_fits()

    assert r.status == "fail"
    assert r.speaker_silent is True


def test_check_camilla_ring_chunk_ok_at_capacity(monkeypatch, tmp_path):
    """jts.local's shape: a floor that exactly fills the ring is fine."""
    from jasper.fanin_coupling import ring_capacity_frames

    _stage_ring_config(tmp_path, monkeypatch, ring_capacity_frames())

    r = doctor.check_camilla_ring_chunk_fits()

    assert r.status == "ok"


def test_check_camilla_ring_chunk_fails_a_target_over_camillas_ceiling(
    monkeypatch, tmp_path
):
    """The state jts4 actually landed in: chunk fits the ring, box still dead.

    256/4096 passes the ring-capacity half and is still refused by CamillaDSP
    (ceiling is chunk x (queuelimit + 4) = 2048), so the box crash-loops with
    the ring half of this check green. Observed on hardware 2026-09-01.
    """
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE, RING_PLAYBACK_DEVICE

    config = tmp_path / "ring.yml"
    config.write_text(
        "devices:\n"
        "  samplerate: 48000\n"
        "  chunksize: 256\n"
        "  queuelimit: 4\n"
        "  target_level: 4096\n"
        "  capture:\n"
        "    type: Alsa\n"
        f'    device: "{RING_CAPTURE_DEVICE}"\n'
        "  playback:\n"
        "    type: Alsa\n"
        f'    device: "{RING_PLAYBACK_DEVICE}"\n'
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_ring_chunk_fits()

    assert r.status == "fail"
    assert r.speaker_silent is True


def test_check_camilla_ring_chunk_discloses_the_clamp(monkeypatch, tmp_path):
    """A clamped box says so, so the running chunk is never unexplained.

    A floorless HiFiBerry DAC8x Studio resolves the 1024 default and runs 256.
    Without this line the only account of the difference is a source comment,
    which is how the bug this check exists for stayed invisible.

    Not the InnoMaker: since #3542 it declares the already-clamped 256/1024
    outright, so it no longer takes this path (see
    test_camilla_config_contract.py::test_a_floor_that_already_fits_the_ring_is_not_clamped).
    """
    from jasper.fanin_coupling import ring_capacity_frames
    from jasper.output_hardware import OutputHardwareState, write_state

    state_path = tmp_path / "output_hardware.json"
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(state_path))
    write_state(
        OutputHardwareState(
            profile_id="hifiberry_dac8x_studio",
            profile_label="HiFiBerry DAC8x Studio",
            status="ready",
            physical_output_count=2,
            selected_card_id="hifiberry_dac8x_studio",
        ),
        state_path,
    )
    monkeypatch.delenv("JASPER_CAMILLA_CHUNKSIZE", raising=False)
    _stage_ring_config(tmp_path, monkeypatch, ring_capacity_frames())

    r = doctor.check_camilla_ring_chunk_fits()

    assert r.status == "ok"
    assert "clamped from" in r.detail


def test_check_camilla_volume_limit_fails_when_missing(monkeypatch, tmp_path):
    config = tmp_path / "v1.yml"
    config.write_text("devices:\n  samplerate: 48000\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_volume_limit()

    assert r.status == "fail"
    assert "omits devices.volume_limit" in r.detail


@pytest.mark.parametrize(
    "text",
    [
        "devices:\n  playback:\n    volume_limit: 0.0\n",
        "devices:\n  volume_limit: 0.0\ndevices: {volume_limit: 9.0}\n",
        "devices:\n  volume_limit: 0.0\n  volume_limit: 9.0\n",
    ],
)
def test_check_camilla_volume_limit_fails_when_ownership_is_ambiguous(
    monkeypatch,
    tmp_path,
    text,
):
    config = tmp_path / "v1.yml"
    config.write_text(text)
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    result = doctor.check_camilla_volume_limit()

    assert result.status == "fail"
    assert "omits devices.volume_limit" in result.detail


def test_check_camilla_volume_limit_fails_when_positive(monkeypatch, tmp_path):
    config = tmp_path / "v1.yml"
    config.write_text("devices:\n  samplerate: 48000\n  volume_limit: 6.0\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_volume_limit()

    assert r.status == "fail"
    assert "expected <=" in r.detail


def test_check_camilla_volume_limit_registered_in_sync_checks():
    assert "check_camilla_volume_limit" in _registered_check_names()


def test_active_speaker_runtime_graph_registered_in_sync_checks():
    assert "check_active_speaker_runtime_graph" in _registered_check_names()


def test_active_speaker_output_hardware_match_registered_in_sync_checks():
    assert "check_active_speaker_output_hardware_match" in _registered_check_names()


def test_active_speaker_runtime_graph_fails_flat_graph_without_a_saved_layout(
    monkeypatch, tmp_path
):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _flat_yaml, _topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_topology([]), path=topology_path)
    config = tmp_path / "outputd-cutover.yml"
    config.write_text(_flat_yaml(), encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "fail"
    assert "No speaker layout is configured" in r.detail


def test_active_speaker_runtime_graph_warns_when_unconfigured_is_parked(
    monkeypatch, tmp_path
):
    """Fresh/reset topology is intentional silence with one next action."""
    from jasper.active_speaker.runtime_contract import (
        UNCONFIGURED_PARKED_EXIT,
        build_parked_muted_graph,
    )
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _topology

    topology = _topology([])
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    text, graph = build_parked_muted_graph(topology)
    assert graph.allowed
    config = tmp_path / "speaker_setup_parked.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "warn"
    assert "unconfigured" in r.detail
    assert UNCONFIGURED_PARKED_EXIT in r.detail


def test_active_speaker_runtime_graph_fails_incomplete_nonroleful_layout(
    monkeypatch, tmp_path
):
    """Parked is not a healthy substitute for an invalid passive layout."""
    from dataclasses import replace

    from jasper.active_speaker.runtime_contract import build_parked_muted_graph
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _full_range_stereo

    complete = _full_range_stereo()
    topology = replace(
        complete,
        speaker_groups=tuple(
            replace(
                group,
                channels=tuple(
                    replace(channel, physical_output_index=None)
                    if group.kind == "right"
                    else channel
                    for channel in group.channels
                ),
            )
            for group in complete.speaker_groups
        ),
    )
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    text, graph = build_parked_muted_graph(topology)
    assert graph.allowed
    config = tmp_path / "speaker_setup_parked.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "fail"
    assert "not assigned to a DAC output" in r.detail


def test_active_speaker_runtime_graph_fails_corrupt_saved_topology(
    monkeypatch,
    tmp_path,
):
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "fail"
    assert "saved output topology is unavailable or invalid" in r.detail
    assert "not valid JSON" in r.detail
    # An unreadable topology is not proof of silence — it is proof of not
    # knowing, so this branch must not claim it (#2471).
    assert r.speaker_silent is False


def test_active_speaker_runtime_graph_fails_flat_graph_on_tweeter_topology(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _flat_yaml,
    )

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    config = tmp_path / "outputd-cutover.yml"
    config.write_text(_flat_yaml(), encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "fail"
    assert "DAC output 2" in r.detail
    assert "flat full-range graph" in r.detail


def test_active_speaker_runtime_graph_accepts_staged_active_startup(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _active_yaml,
        _staged_metadata,
    )

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    config = tmp_path / "active_speaker_staged_startup.yml"
    config.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text(
        json.dumps(_staged_metadata(topology, config)),
        encoding="utf-8",
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH", str(metadata))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "ok"
    assert "all_muted_active_startup" in r.detail
    # A legal graph is not silence: the flag stays off (#2471).
    assert r.speaker_silent is False


def test_active_speaker_runtime_graph_warns_when_parked(monkeypatch, tmp_path):
    # #2135: a declared-but-uncommissioned roleful topology is seeded a parked
    # (DAC-less, all-muted) graph so the deploy can finish. Nothing is broken and
    # nothing is audible, so this is a WARN, not a fail — and it must name the two
    # exits verbatim rather than restating graph internals.
    from jasper.active_speaker.runtime_contract import (
        PARKED_MUTED_EXITS,
        build_parked_muted_graph,
    )
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _active_topology

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    text, graph = build_parked_muted_graph(topology)
    assert graph.allowed
    config = tmp_path / "active_speaker_parked.yml"
    config.write_text(text, encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "warn"
    assert PARKED_MUTED_EXITS in r.detail
    # #2471: this is the speaker-is-silent state, so the summary composer must
    # be able to name it instead of ending on "warning(s) — non-critical".
    assert r.speaker_silent is True


def test_active_speaker_topology_blockers_registered_in_sync_checks():
    assert "check_active_speaker_topology_blockers" in _registered_check_names()


def _blocker_bearing_roleful_topology():
    """A roleful topology whose tweeter has no DAC output assigned."""
    from dataclasses import replace

    from tests.test_active_speaker_runtime_contract import _active_topology

    topology = _active_topology("mono", "active_2_way")
    return replace(
        topology,
        speaker_groups=tuple(
            replace(
                group,
                channels=tuple(
                    replace(channel, physical_output_index=None)
                    if channel.role == "tweeter"
                    else channel
                    for channel in group.channels
                ),
            )
            for group in topology.speaker_groups
        ),
    )


# Branch-distinguishing markers for `check_active_speaker_topology_blockers`.
#
# Each of the six branches asserts a substring the OTHERS must not carry, and
# every test cross-asserts absence. Without that, "warn + blockers + URL" is
# satisfied by both the parked branch and the probe-failure branch, so one
# refactor collapsing them would leave the suite green — the tests would be
# describing a shape rather than a branch.
_MARK_NO_ROLEFUL = "no roleful/protected outputs"
_MARK_NO_BLOCKERS = "no topology blockers"
_MARK_PARKED = "does not by itself unpark"
_MARK_PROBE_FAILED = "could not determine the selected runtime graph"
_MARK_NOT_PARKED = "but the speaker is not parked"
_MARK_LOAD_FAILED = "could not be loaded"
_ALL_MARKS = (
    _MARK_NO_ROLEFUL,
    _MARK_NO_BLOCKERS,
    _MARK_PARKED,
    _MARK_PROBE_FAILED,
    _MARK_NOT_PARKED,
    _MARK_LOAD_FAILED,
)


# #2471: exactly one of the six branches proves the speaker emits nothing.
# `_MARK_PROBE_FAILED` is deliberately NOT here — it has blockers but could not
# classify the graph, so it does not know; `_MARK_NOT_PARKED` knows the box is
# playing. Asserting the flag inside `_assert_only_branch` pins the partition
# across every branch at once, with no extra fixtures.
_SILENT_MARKS = frozenset({_MARK_PARKED})


def _assert_only_branch(result, expected_mark: str) -> None:
    """The detail names its own branch and no other."""
    assert expected_mark in result.detail, (
        f"expected branch marker {expected_mark!r} in {result.detail!r}"
    )
    for mark in _ALL_MARKS:
        if mark != expected_mark:
            assert mark not in result.detail, (
                f"detail also matched a different branch's marker {mark!r}"
            )
    assert result.speaker_silent is (expected_mark in _SILENT_MARKS), (
        f"branch {expected_mark!r} claims speaker_silent="
        f"{result.speaker_silent}; only the parked branch may claim it"
    )


def test_topology_blockers_ok_without_roleful_outputs(monkeypatch, tmp_path):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_topology([]), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    r = doctor.check_active_speaker_topology_blockers()

    assert r.status == "ok"
    _assert_only_branch(r, _MARK_NO_ROLEFUL)


def test_topology_blockers_ok_when_a_clean_roleful_layout_is_parked(
    monkeypatch, tmp_path
):
    # Parked but CLEAN: `check_active_speaker_runtime_graph` already warns about
    # the parked state itself. This check speaks only to unresolved blockers, so
    # a clean layout must stay quiet rather than adding a second warning.
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _active_topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_active_topology("mono", "active_2_way"), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    r = doctor.check_active_speaker_topology_blockers()

    assert r.status == "ok"
    _assert_only_branch(r, _MARK_NO_BLOCKERS)


def test_topology_blockers_warn_names_each_blocker_and_the_real_exits(
    monkeypatch, tmp_path
):
    # #2145: this state used to abort every deploy, which was its own loud
    # notification. It now parks and deploys, so the doctor carries the signal.
    from jasper.active_speaker.runtime_contract import parked_muted_exits
    from jasper.output_topology import save_output_topology

    topology = _blocker_bearing_roleful_topology()
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    r = doctor.check_active_speaker_topology_blockers()

    # WARN, never FAIL: doctor exits non-zero only on fails, and a parked box
    # must stay deployable.
    assert r.status == "warn"
    _assert_only_branch(r, _MARK_PARKED)
    # This check's own contribution: the blocker codes and messages...
    assert "physical_output_unassigned" in r.detail
    assert "tweeter is not assigned to a DAC output" in r.detail
    assert "/sound/setup/" in r.detail
    # NOT the discriminator: this fixture is an ordinary DAC, where the helper
    # and the bare constant return the same string, so this line only checks the
    # wording is PRESENT — it cannot tell which produced it. That claim belongs
    # to `test_topology_blockers_exits_are_capability_aware` below, which uses a
    # DAC that makes the two disagree.
    assert parked_muted_exits(topology) in r.detail


def test_topology_blockers_exits_are_capability_aware(monkeypatch, tmp_path):
    """The doctor detail must resolve the exits through the OWNED helper.

    The sibling test above cannot prove this: on an ordinary DAC
    `parked_muted_exits(topology) == PARKED_MUTED_EXITS`, so asserting the
    helper's output appears in the detail is a tautology that a revert to the
    bare constant satisfies just as well. Only a DAC that declares NO active
    outputd lane separates them — there "finish crossover preview" can never
    succeed, so the helper drops it and the constant still offers it.

    Same discriminating shape as
    `test_runtime_safe_graph_cli_names_capability_aware_exits`: register the
    passive-only profile, assert up front that the helper and the constant
    DISAGREE, then assert the detail follows the helper.
    """
    from jasper.active_speaker.runtime_contract import (
        PARKED_MUTED_EXITS,
        parked_muted_exits,
    )
    from jasper.output_topology import (
        OUTPUT_TOPOLOGY_KIND,
        OutputTopology,
        save_output_topology,
    )
    from tests.active_speaker_fixtures import register_passive_only_dac

    profile = register_passive_only_dac(monkeypatch)
    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "bench",
        "name": "Bench speaker",
        "status": "draft",
        "hardware": {
            "device_id": profile.id,
            "device_label": profile.label,
            "physical_output_count": profile.physical_output_count,
        },
        "speaker_groups": [{
            "id": "mono",
            "label": "Mono",
            "kind": "mono",
            "mode": "active_2_way",
            "channels": [
                {
                    "role": "woofer",
                    "physical_output_index": 0,
                    "identity_verified": True,
                },
                {
                    # Unassigned -> the topology-level blocker this check reports.
                    "role": "tweeter",
                    "physical_output_index": None,
                    "identity_verified": True,
                    "startup_muted": True,
                    "protection_required": True,
                    "protection_status": "present",
                },
            ],
        }],
        "routing": {"mono_group_id": "mono"},
    })
    # The fixture is only meaningful if the helper and the constant DISAGREE.
    assert parked_muted_exits(topology) != PARKED_MUTED_EXITS

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    r = doctor.check_active_speaker_topology_blockers()

    assert r.status == "warn"
    _assert_only_branch(r, _MARK_PARKED)
    # Follows the helper...
    assert parked_muted_exits(topology) in r.detail
    assert profile.label in r.detail
    # ...and therefore does NOT offer the impossible action on this hardware.
    assert PARKED_MUTED_EXITS not in r.detail
    # The check's own contribution still rides alongside.
    assert "physical_output_unassigned" in r.detail
    assert "/sound/setup/" in r.detail


def test_topology_blockers_warn_survives_a_failed_selection_probe(
    monkeypatch, tmp_path
):
    """The code promises "a failed probe must not hide them" — pin it.

    The blockers are this check's finding; the parked/not-parked split is only
    how it phrases them. So a probe that raises must still report every blocker
    and the wizard step, and must say plainly that it could not classify.
    """
    from jasper.active_speaker import runtime_contract
    from jasper.output_topology import save_output_topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_blocker_bearing_roleful_topology(), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    def _boom(*_args, **_kwargs):
        raise OSError("statefile unreadable")

    monkeypatch.setattr(
        runtime_contract, "safe_graph_for_current_topology", _boom
    )

    r = doctor.check_active_speaker_topology_blockers()

    assert r.status == "warn"
    _assert_only_branch(r, _MARK_PROBE_FAILED)
    # The finding survives the failed probe...
    assert "physical_output_unassigned" in r.detail
    assert "tweeter is not assigned to a DAC output" in r.detail
    assert "/sound/setup/" in r.detail
    # ...and the reason the probe could not answer is named, not swallowed.
    assert "statefile unreadable" in r.detail


def test_topology_blockers_defers_when_the_speaker_is_not_parked(
    monkeypatch, tmp_path
):
    # A blocker-bearing topology that is NOT parked is already reported by
    # `check_active_speaker_runtime_graph` (it blocks the deploy). Repeating it
    # here would be a second voice for one fact, so this branch defers.
    from jasper.active_speaker import runtime_contract
    from jasper.active_speaker.runtime_contract import (
        SafeGraphDecision,
        classify_output_contract,
    )
    from jasper.output_topology import save_output_topology

    topology = _blocker_bearing_roleful_topology()
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    def _blocked(*_args, **_kwargs):
        return SafeGraphDecision(
            status="blocked",
            selected_config_path=None,
            reason="test double",
            topology_contract=classify_output_contract(topology),
        )

    monkeypatch.setattr(
        runtime_contract, "safe_graph_for_current_topology", _blocked
    )

    r = doctor.check_active_speaker_topology_blockers()

    assert r.status == "ok"
    _assert_only_branch(r, _MARK_NOT_PARKED)
    # Defers rather than re-prescribing: no wizard step, because the sibling
    # check owns this state's remediation.
    assert "/sound/setup/" not in r.detail
    assert "active speaker runtime graph check" in r.detail


def test_topology_blockers_points_at_the_sibling_on_an_unloadable_topology(
    monkeypatch, tmp_path
):
    """Three checks read this artifact; only the other two restate the error.

    `check_active_speaker_runtime_graph` and
    `check_active_speaker_output_hardware_match` both already print
    "saved output topology is unavailable or invalid: <exc>" verbatim. This one
    fails too (it cannot answer its question, and a green line beside two reds
    would read as "the layout is fine"), but points instead of adding a third
    copy of the parse error.
    """
    from jasper.output_topology import OutputTopologyError, load_output_topology_strict

    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    # Prove the fixture actually reaches the branch under test.
    with pytest.raises(OutputTopologyError):
        load_output_topology_strict()

    r = doctor.check_active_speaker_topology_blockers()

    assert r.status == "fail"
    _assert_only_branch(r, _MARK_LOAD_FAILED)
    assert "active speaker runtime graph check" in r.detail
    # Not a third copy of the sibling's sentence.
    assert "unavailable or invalid" not in r.detail


def test_check_sound_profile_reports_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "missing.json"))

    r = doctor.check_sound_profile()

    assert r.status == "ok"
    assert "default Flat" in r.detail


def test_check_sound_profile_warns_when_saved_profile_not_active(
    monkeypatch,
    tmp_path,
):
    profile = tmp_path / "sound_profile.json"
    profile.write_text(
        json.dumps(
            {
                "enabled": True,
                "curve_id": "harman",
                "simple_eq": {"bass_db": 1.0, "mid_db": 0.0, "treble_db": 0.0},
            }
        )
    )
    statefile = tmp_path / "statefile.yml"
    base = tmp_path / "v1.yml"
    base.write_text("# base\n")
    statefile.write_text(f"config_path: {base}\n")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_sound_profile()

    assert r.status == "warn"
    assert "curve=harman" in r.detail
    assert "not reflected" in r.detail


@pytest.mark.parametrize(
    "active_name",
    [
        # The four shapes the check's old literal set did not know about.
        "sound_reset_s1_1717000000.yml",
        "sound_snapshot_s1_1717000000.yml",
        "sound_lean_current.yml",
        "grouping_leader.yml",
    ],
)
def test_check_sound_profile_accepts_every_jts_generated_active_name(
    monkeypatch, tmp_path, active_name,
):
    """One owner decides what "JTS-generated" means, and the doctor asks it.

    `check_sound_profile` held a second, narrower copy of that predicate as a
    literal set (`correction_*`, `sound_current.yml`, `sound_audition.yml`) and
    had fallen four shapes behind
    :func:`jasper.sound.camilla_yaml.is_jts_generated_config`. Every name below
    is one JTS itself emits — `sound_reset_*` comes from the household's own
    /sound/ reset (`_write_no_room_correction_config`) — so the doctor told a
    household its saved profile was "not reflected in active generated config"
    about a graph that demonstrably carries it. The config written here really
    does contain the saved profile's filter, so the warning would be false in
    substance and not merely in name.

    Latent before #2572, permanent after it: the reconcile now leaves a
    content-identical graph running under whatever it is named rather than
    rewriting it to `sound_current.yml`, so nothing clears the false warn.

    The negative half — a genuinely foreign config still warns — is
    `test_check_sound_profile_warns_when_saved_profile_not_active` above, which
    stays green as-written.
    """
    from jasper.sound.camilla_yaml import emit_sound_config
    from jasper.sound.profile import SoundProfile, build_sound_filters

    saved = {
        "enabled": True,
        "curve_id": "harman",
        "simple_eq": {"bass_db": 1.0, "mid_db": 0.0, "treble_db": 0.0},
    }
    profile = tmp_path / "sound_profile.json"
    profile.write_text(json.dumps(saved))

    # The active graph genuinely carries the saved preference EQ.
    active = tmp_path / active_name
    active.write_text(
        emit_sound_config(SoundProfile.from_mapping(saved), profile_id="t"),
        encoding="utf-8",
    )
    assert build_sound_filters(SoundProfile.from_mapping(saved))
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {active}\n")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_sound_profile()

    assert r.status == "ok", r.detail
    assert "not reflected" not in r.detail
    # The check still reports what it always did; only the drift verdict moved.
    assert "curve=harman" in r.detail


def test_check_sound_profile_fails_on_corrupt_json(monkeypatch, tmp_path):
    profile = tmp_path / "sound_profile.json"
    profile.write_text("{not json")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))

    r = doctor.check_sound_profile()

    assert r.status == "fail"
    assert "could not read" in r.detail


def test_check_dsp_apply_state_reports_success(monkeypatch, tmp_path):
    state = tmp_path / "dsp_apply_state.json"
    state.write_text(
        json.dumps(
            {
                "op_id": "abcdef123456",
                "source": "sound",
                "phase": "done",
                "result": "success",
                "candidate_config_path": "/var/lib/camilladsp/configs/sound_current.yml",
            }
        )
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state))

    r = doctor.check_dsp_apply_state()

    assert r.status == "ok"
    assert "source=sound" in r.detail
    assert "result=success" in r.detail


def test_check_dsp_apply_state_fails_on_rollback_failure(monkeypatch, tmp_path):
    state = tmp_path / "dsp_apply_state.json"
    state.write_text(
        json.dumps(
            {
                "op_id": "abcdef123456",
                "source": "correction",
                "phase": "load",
                "result": "load_failed_rollback_failed",
                "rollback_attempted": True,
                "rollback_succeeded": False,
            }
        )
    )
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state))

    r = doctor.check_dsp_apply_state()

    assert r.status == "fail"
    assert "rollback_failed" in r.detail


# --- #1666: canonical active-speaker baseline durability -------------------


def test_check_active_speaker_baseline_canonical_registered_in_sync_checks():
    assert "check_active_speaker_baseline_canonical" in _registered_check_names()


def test_active_speaker_baseline_canonical_ok_when_live_is_canonical(
    monkeypatch,
    tmp_path,
):
    canonical = tmp_path / "active_speaker_baseline.yml"
    canonical.write_text("# whatever is currently loaded\n", encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {canonical}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(canonical))

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert "live config is the canonical file" in r.detail


def test_active_speaker_baseline_canonical_ok_when_not_applicable(
    monkeypatch,
    tmp_path,
):
    """A live config that is not an active-speaker baseline candidate at all
    (a plain stereo/flat topology's own generated config) is not applicable
    -- never a false warning on the majority of the fleet that has no active
    baseline commissioned."""
    live = tmp_path / "outputd-cutover.yml"
    live.write_text("devices:\n  samplerate: 48000\n", encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {live}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
        str(tmp_path / "active_speaker_baseline.yml"),
    )

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert "not applicable" in r.detail


def test_active_speaker_baseline_canonical_ok_when_statefile_unreadable(
    monkeypatch,
    tmp_path,
):
    """N2 (#1666 review): a missing/unreadable outputd statefile is already
    surfaced as a real failure by check_active_speaker_runtime_graph (when a
    roleful topology needs it); this check's own scope -- comparing canonical
    to the live baseline -- cannot be evaluated at all here, so it is not
    applicable rather than a redundant false warning."""
    monkeypatch.setenv(
        "JASPER_CAMILLA_STATEFILE", str(tmp_path / "missing-statefile.yml")
    )
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH",
        str(tmp_path / "active_speaker_baseline.yml"),
    )

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert "not applicable" in r.detail


async def test_active_speaker_baseline_canonical_ok_when_content_matches_live(
    monkeypatch,
    tmp_path,
):
    """#1666: the common case -- every successful apply's post-success
    promote keeps canonical's content equal to whatever candidate is
    actually live."""
    from tests.test_active_speaker_baseline_profile import _apply_prior_then_run8

    (
        _state_path,
        config_path,
        _load_config,
        _current_config_path,
        _prior_payload,
        run8_payload,
        _retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)

    live_path = Path(run8_payload["profile"]["config"]["path"])
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {live_path}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(config_path))

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "ok"
    assert str(config_path) in r.detail


async def test_active_speaker_baseline_canonical_warns_on_divergence(
    monkeypatch,
    tmp_path,
):
    """#1666: canonical currently holds run-8's promoted bytes (the fixture's
    most recent apply); simulating CamillaDSP still running the PRIOR
    candidate produces a genuine content mismatch -- warn, since the live
    graph is the audible truth and is correct, but the canonical file is
    stale for other readers."""
    from tests.test_active_speaker_baseline_profile import _apply_prior_then_run8

    (
        _state_path,
        config_path,
        _load_config,
        _current_config_path,
        prior_payload,
        _run8_payload,
        _retained,
    ) = await _apply_prior_then_run8(monkeypatch, tmp_path)

    live_path = Path(prior_payload["profile"]["config"]["path"])
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {live_path}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(config_path))

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "warn"
    assert "does not match" in r.detail
    assert str(config_path) in r.detail
    assert str(live_path) in r.detail


def test_active_speaker_baseline_canonical_warns_on_missing_canonical(
    monkeypatch,
    tmp_path,
):
    """#1666: a live applied-candidate sibling with no canonical file yet (a
    box whose last promote never ran, or hasn't run since this fix
    deployed) warns rather than failing -- the next apply/restore
    re-promotes it."""
    canonical = tmp_path / "active_speaker_baseline.yml"
    live = tmp_path / "active_speaker_baseline_candidate_abc123def456.yml"
    live.write_text("# a real applied candidate, never promoted\n", encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {live}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH", str(canonical))

    r = doctor.check_active_speaker_baseline_canonical()

    assert r.status == "warn"
    assert "missing" in r.detail


def test_check_correction_latest_bundle_warns_without_calibration(
    monkeypatch,
    tmp_path,
):
    sessions = tmp_path / "sessions"
    bundle = sessions / "abc"
    bundle.mkdir(parents=True)
    bundles.write_json_artifact(
        bundle,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "abc",
            "state": "ready",
            "started_at": 1000,
            "capture_quality": [],
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.test_doctor",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    bundles.write_json_artifact(
        bundle,
        "result.json",
        {"bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION},
        kind="analysis_result",
        sensitivity="private_metadata",
        recomputable=True,
        generated_by="tests.test_doctor",
        dependencies=["info.json"],
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = doctor.check_correction_latest_bundle()

    assert r.status == "warn"
    assert "no calibrated mic" in r.detail
    # Audit gauntlet 5a: the remedy names the concrete surface, not just the
    # problem — the /correction/ page's Microphone section and its
    # fetch-by-serial step.
    assert "/correction/" in r.detail
    assert "Fetch calibration" in r.detail


def test_check_correction_latest_bundle_warns_when_failed(
    monkeypatch,
    tmp_path,
):
    sessions = tmp_path / "sessions"
    bundle = sessions / "failed"
    bundle.mkdir(parents=True)
    bundles.write_json_artifact(
        bundle,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "failed",
            "state": "failed",
            "started_at": 1000,
            "error": "analysis failed: capture clipped",
            "capture_quality": [],
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.test_doctor",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = doctor.check_correction_latest_bundle()

    assert r.status == "warn"
    assert "capture clipped" in r.detail


def test_check_correction_latest_bundle_reports_bundle_collection(
    monkeypatch,
    tmp_path,
):
    sessions = tmp_path / "sessions"
    write_golden_correction_bundle(sessions, "old", started_at=1000)
    write_golden_correction_bundle(sessions, "new", started_at=2000)
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = doctor.check_correction_latest_bundle()

    assert r.status == "ok"
    assert "session=new" in r.detail
    assert "bundles=2" in r.detail
    assert "storage=" in r.detail
    assert "private_raw=8/" in r.detail
    assert "evidence=complete(" in r.detail
    assert "old raw recordings present (8 files)" in r.detail


def test_correction_doctor_checks_registered():
    names = _registered_check_names()
    assert "check_correction_web_service" in names
    assert "check_correction_state_dirs" in names
    assert "check_correction_current_config" in names
    assert "check_sound_profile" in names
    assert "check_dsp_apply_state" in names
    assert "check_correction_latest_bundle" in names
    assert "check_crossover_v2_cloud_pipeline" in names


def test_check_crossover_v2_cloud_pipeline_ok_when_never_run(monkeypatch):
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(v2host, "load_v2_state", lambda: None)

    r = doctor.check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert "no cloud-measurement session" in r.detail


def test_check_crossover_v2_cloud_pipeline_reports_per_group_verdict(monkeypatch):
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: {
            "cloud": {
                "cloud_measure": {
                    "geometry": {"locked": True},
                    "pipeline": {
                        "available": True,
                        "spec": {
                            "overall_passed": True,
                            "bands": [
                                {"f_lo_hz": 250.0, "f_hi_hz": 2000.0, "passed": True},
                            ],
                        },
                        "merged_excluded_bands_hz": [[8000.0, 9000.0]],
                    },
                },
                "cloud_verify": {
                    "geometry": {"locked": False},
                    "pipeline": {
                        "available": True,
                        "spec": {"overall_passed": False, "bands": []},
                        "merged_excluded_bands_hz": [],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_cloud_pipeline()

    # cloud_verify (the post-apply, household-actionable grade) failed its
    # spec -- WARN, not FAIL (an out-of-spec speaker is a measurement
    # finding, not a broken daemon). BLOCKER review finding (2026-07-27):
    # only cloud_verify's own verdict gates the warn -- cloud_measure's PASS
    # here is incidental to this fixture, not what drives the status.
    assert r.status == "warn"
    assert (
        "cloud_measure: spec=pass excluded_intervals=1 geometry_locked=True" in r.detail
    )
    assert (
        "cloud_verify: spec=fail excluded_intervals=0 geometry_locked=False" in r.detail
    )


def test_check_crossover_v2_cloud_pipeline_ok_when_pre_apply_fails_but_post_apply_passes(
    monkeypatch,
):
    """BLOCKER review finding (2026-07-27): ``cloud_measure`` is the
    PRE-APPLY cloud -- the uncorrected baseline that EXISTS in order to be
    out of spec. Gating the warn on ANY phase's verdict meant a perfectly
    corrected speaker warned forever (reviewer reproduced against the real
    S0 corpus through this exact doctor check: the pre-apply grade never
    changes no matter how good the fix is). Only ``cloud_verify`` -- the
    post-apply, household-actionable grade -- may gate the warn;
    ``cloud_measure``'s own verdict still appears in the detail text, never
    hidden."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: {
            "cloud": {
                "cloud_measure": {
                    "geometry": {"locked": False},
                    "pipeline": {
                        "available": True,
                        "spec": {"overall_passed": False, "bands": []},
                        "merged_excluded_bands_hz": [[8000.0, 9000.0]],
                    },
                },
                "cloud_verify": {
                    "geometry": {"locked": False},
                    "pipeline": {
                        "available": True,
                        "spec": {"overall_passed": True, "bands": []},
                        "merged_excluded_bands_hz": [],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert "cloud_measure: spec=fail" in r.detail
    assert "cloud_verify: spec=pass" in r.detail


def _v2_applied_state(**overrides):
    state = {"applied": True, "session_id": "sess-graded"}
    state.update(overrides)
    return state


def test_applied_profile_with_no_post_apply_grade_warns(monkeypatch):
    """PR-L4 item 6: the silence its sibling check structurally cannot see.

    ``check_crossover_v2_cloud_pipeline`` warns on a FAILING post-apply grade
    and is deliberately gated that way; a MISSING grade renders as no phase at
    all, so it printed a clean tick over a speaker running an unverified
    correction — the 2026-07-27 shape."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(v2host, "load_v2_state", _v2_applied_state)
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_applied_is_graded()
    assert r.status == "warn"
    assert "never graded" in r.detail


def test_applied_profile_graded_by_verify_alone_is_ok(monkeypatch):
    """Express tier omits the post-apply position group entirely, so a passing
    VERIFY outcome is the whole grade and must satisfy the check on its own."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: _v2_applied_state(verify={"outcome": "pass"}),
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_applied_is_graded()
    assert r.status == "ok"
    assert "applied and graded" in r.detail


def test_applied_profile_with_inconclusive_verify_warns(monkeypatch):
    """Checked, and the check could not decide — a distinct silence from
    "never checked", and reported as such."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: _v2_applied_state(verify={"outcome": "inconclusive"}),
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_applied_is_graded()
    assert r.status == "warn"
    assert "inconclusive" in r.detail
    assert "never graded" not in r.detail


# --- R19 honest grading: the green tick over a failed gauge (#2160/#2098) ---


def _r19_cloud_verify(*, passed, flatness):
    return {
        "cloud_verify": {
            "geometry": {"locked": False},
            "pipeline": {
                "available": True,
                "spec": {"overall_passed": passed, "bands": []},
                "merged_excluded_bands_hz": [
                    [1400.0, 1900.0], [3000.0, 3200.0],
                    [5000.0, 5400.0], [9000.0, 9600.0],
                ],
                "flatness": flatness,
            },
        },
    }


_R19_FAILED_GAUGE = {
    "max_db": -4.628, "max_hz": 1650.0, "max_band_hz": [1250.0, 2000.0],
    "tolerance_db": 1.5, "rms_db": 1.9, "n_bins": 700, "n_excluded": 40,
    "evaluable": True, "passed": False,
}


def _r19_cloud_verify_pipeline_unavailable(*, reason):
    """A group that CLOSED but whose pipeline failed to combine/analyze —
    ``assemble_cloud_group_result``'s own ``combined is None`` shape
    (``{"available": False, "reason": "combine_failed"}``). Distinct from a
    group that never closed at all: ``_r19_cloud_verify`` above hardcodes
    ``available: True``, so this shape had no test coverage (#2242 SF1)."""
    return {
        "cloud_verify": {
            "geometry": {"locked": True},
            "pipeline": {"available": False, "reason": reason},
        },
    }


def _r19_doctor_state(monkeypatch, **overrides):
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host, "load_v2_state", lambda: _v2_applied_state(**overrides)
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )


def test_a_failed_spatial_grade_is_not_a_green_tick(monkeypatch):
    """#2160, measured on jts3 2026-08-07: this line printed ``applied and
    graded (state=graded, verify=pass)`` while the cloud line one row up read
    ``spec=fail worst=-4.63dB excluded_intervals=4 geometry_locked=False``.
    ``state`` answers "was it checked" and ``graded`` is an honest answer to
    that, so keying ok on it reported a failure as a pass. Grade and disclose,
    never gate: still a warn, and the tune stays on the speaker."""
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={"outcome": "pass"},
        cloud=_r19_cloud_verify(passed=False, flatness=_R19_FAILED_GAUGE),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "spatial grade missed the target" in r.detail
    # The number rides the verdict, from the same gauge the cloud line prints.
    assert "-4.63dB" in r.detail
    assert "@ 1650Hz" in r.detail
    # The ruling, in the words a household reads: nothing was reverted.
    assert "stays on the speaker" in r.detail


def test_a_full_session_verified_only_at_the_mark_warns(monkeypatch):
    """#2098's own field evidence — Full, ``verify.outcome=pass``, no cloud.
    The local pass is real and is preserved in the wording; what changes is
    that it stops being reported as the claim Full promised.

    SF1 (#2242 gate): the wording is delivered-evidence only — "no full-tier
    spatial grade exists" — never "never closed", which asserts a mechanism
    this check cannot know (a closed-but-unavailable pipeline reaches this
    same branch; see the fixture below)."""
    _r19_doctor_state(monkeypatch, tier="full", verify={"outcome": "pass"})

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "verified at the mark" in r.detail
    assert "no full-tier spatial grade exists" in r.detail


def test_a_closed_but_unavailable_pipeline_does_not_claim_the_group_never_closed(
    monkeypatch,
):
    """SF1 (#2242 gate): the group DID close — its pipeline just failed to
    combine (``assemble_cloud_group_result``'s own ``combine_failed`` shape)
    — the second falsifying case the old "never closed" wording contradicted.
    That shape renders this line beside ``check_crossover_v2_cloud_pipeline``'s
    own line describing the same closed-but-unavailable group — the
    adjacent-contradiction defect this PR exists to remove, reintroduced one
    branch over. The new wording claims only what THIS check can see."""
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={"outcome": "pass"},
        cloud=_r19_cloud_verify_pipeline_unavailable(reason="combine_failed"),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "no full-tier spatial grade exists" in r.detail
    assert "never closed" not in r.detail


def test_an_express_session_verified_at_the_mark_stays_ok(monkeypatch):
    """The mark IS express's whole promise. Warning here would fire on every
    express session ever run — the mirror of the defect being fixed."""
    _r19_doctor_state(monkeypatch, tier="express", verify={"outcome": "pass"})

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "ok"
    assert "scope=mark" in r.detail


def test_a_group_that_could_not_be_graded_is_not_reported_as_a_miss(monkeypatch):
    """``passed=False, evaluable=False`` means "could not be measured", not
    "failed" — ``SpecFlatness.passed``'s own read-it-with-evaluable rule."""
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={"outcome": "pass"},
        cloud=_r19_cloud_verify(passed=False, flatness={
            **_R19_FAILED_GAUGE,
            "max_db": None, "max_hz": None, "evaluable": False,
        }),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "could not be measured" in r.detail
    assert "FAILED" not in r.detail


def test_a_closed_and_passing_spatial_grade_is_ok(monkeypatch):
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={"outcome": "pass"},
        cloud=_r19_cloud_verify(passed=True, flatness={
            **_R19_FAILED_GAUGE, "max_db": 0.9, "passed": True,
        }),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "ok"
    assert "scope=spatial" in r.detail


# --- #2464: a failed mark-VERIFY caps this line at warn -----------------------


_R19_PASSING_GAUGE = {**_R19_FAILED_GAUGE, "max_db": 0.9, "passed": True}


def test_a_failed_verify_is_not_masked_by_a_passing_spatial_grade(monkeypatch):
    """#2464, ruled 2026-08-19 (option (a)): a failed mark-VERIFY caps this
    line at warn whatever the spatial group says.

    The producer tested the closed post-apply group BEFORE its own fail arm,
    so a re-verify that failed against a carried-forward passing group reached
    ``GRADE_GRADED`` — and this check, which reads that state, printed
    ``applied and graded`` over it. The remediation copy the household needs
    was unreachable, not absent."""
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={
            "outcome": "fail",
            "claims": {"integration": {"status": "fail", "max_db": 4.2}},
        },
        cloud=_r19_cloud_verify(passed=True, flatness=_R19_PASSING_GAUGE),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "came back failed" in r.detail
    assert "re-verify at /correction/ or undo" in r.detail
    assert "applied and graded" not in r.detail


def test_a_failed_absolute_claim_warns_and_names_the_clean_capture(monkeypatch):
    """The retro-audit shape: the capture was clean (``outcome=pass``) and the
    crossover-region claim missed its tolerance. ``verify.outcome`` grades
    capture and tracking health only, so the claims record is what sees it —
    and BOTH facts print, because a line that says "came back failed" beside a
    `/state` reading ``verify_outcome=pass`` is the adjacent contradiction this
    module keeps removing. The ``capture`` qualifier is what stops the pair
    reading as self-contradictory: it names which instrument answered."""
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={
            "outcome": "pass",
            "claims": {
                "integration": {"status": "pass", "max_db": 0.7},
                "absolute": {"status": "fail", "max_db": 4.31},
            },
        },
        cloud=_r19_cloud_verify(passed=True, flatness=_R19_PASSING_GAUGE),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "came back failed" in r.detail
    assert "capture verify=pass" in r.detail


def test_an_inconclusive_verify_is_not_masked_by_a_closed_group(monkeypatch):
    """The same mask one arm over — and this arm now names the outcome word
    too, for the same reason the graded branches beside it always have."""
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={"outcome": "inconclusive"},
        cloud=_r19_cloud_verify(passed=False, flatness=_R19_FAILED_GAUGE),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "came back inconclusive" in r.detail
    assert "capture verify=inconclusive" in r.detail


def test_an_unknown_spatial_word_from_a_later_build_warns_and_names_it(
    monkeypatch,
):
    """S1 (#2242 gate): the direction ``state`` already follows for a value
    this build cannot read is WARN, never the passing wording — this used to
    fall through to "applied and graded" instead, a real divergence the
    reviewer proved by measurement (unknown ``state`` warns; unknown
    ``spatial`` used to tick green). The word is named rather than guessed
    at. The grade is injected directly because no producer path can emit it
    — that is the point of the case."""
    from jasper.web import correction_crossover_v2 as v2host
    from jasper.web import correction_crossover_v2_status as v2status

    monkeypatch.setattr(
        v2status,
        "crossover_v2_status_block",
        lambda: {
            "tier": "full",
            "post_apply_grade": {
                "state": v2host.GRADE_GRADED,
                "graded": True,
                "verify_outcome": "pass",
                "scope": "hemispherical-2027",
                "spatial": "graded_from_orbit",
                "complete": True,
            },
        },
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "warn"
    assert "graded_from_orbit" in r.detail
    assert "not one this build recognizes" in r.detail
    assert "applied and graded," in r.detail  # still says WHAT it checked


# --- the result code is DISCLOSED beside the grade, and never gates it --------


def test_a_kept_previous_result_is_disclosed_on_the_green_line(monkeypatch):
    """The doctor and the household badge may honestly disagree about the same
    durable state, and this pins BOTH halves of that ruling at once.

    The cell: a clean, complete grade — capture ``pass``, both claims ``pass``,
    a closed and passing spatial group — whose accountability comparison says
    the correction was not an improvement, so the producer's ``outcome`` is
    ``keep_previous``. The household badge reads "Keep the previous sound."
    while this line read ``applied and graded (…capture verify=pass)`` with no
    hint that a result code existed at all, which is what sent a support read
    hunting for a second speaker's state.

    **``ok`` is the ruling, not an oversight — argue with this test before
    keying a warn on the result code.** Three reasons, and the disclosure below
    is what discharges the legibility half without touching the severity half:

    * This check's question is the one in its own title: was the applied
      correction CHECKED. It was, completely, and every instrument that graded
      the checking passed. ``outcome`` answers a different question — was the
      result any GOOD — which has its own owner (``_post_apply_grade``) and its
      own surface (the done screen's badge and ``/state``).
    * A warn keyed on the code would fire on HEALTHY speakers: a session that
      published no authorized winner, or whose VERIFY recorded no pass/fail
      ``absolute`` claim, reaches ``inconclusive`` with every instrument that
      graded the checking passing — see the express sibling below, which is
      that cell. Warning there is the nanny failure, and the mirror of the
      #2098 defect this family exists to fix.
    * It is the house style for a fact this check observes but does not own:
      ``check_crossover_v2_cloud_pipeline`` one screen up reports MEASURE's
      verdict in detail while deliberately not gating on it, and
      ``check_dac_usb_sync_mode`` says at length that it is an observation and
      never a switch.
    """
    _r19_doctor_state(
        monkeypatch,
        tier="full",
        verify={
            "outcome": "pass",
            "claims": {
                "integration": {"status": "pass", "max_db": 0.7},
                "absolute": {"status": "pass", "max_db": 0.8},
            },
        },
        verify_priors={
            "predicted_spec": {
                "comparison": {
                    "reason": "not_an_improvement",
                    "improvement_db": 0.1,
                    "required_db": 0.5,
                },
            },
        },
        cloud=_r19_cloud_verify(passed=True, flatness=_R19_PASSING_GAUGE),
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "ok"
    assert "result=keep_previous" in r.detail
    # The grade's own facts still read exactly as before — the result code is
    # an addition beside them, never a replacement for any of them.
    assert "state=graded" in r.detail
    assert "scope=spatial" in r.detail
    assert "capture verify=pass" in r.detail


def test_an_express_mark_verified_line_discloses_an_inconclusive_result(
    monkeypatch,
):
    """The express shape, and the reason the disclosure above is not a gate:
    mark-verified, no cloud group by design (express never walks one), capture
    and tracking both clean — and ``inconclusive`` anyway, because no candidate
    fingerprint was published and the crossover region carried no spec
    tolerance for an ``absolute`` verdict. Every instrument that grades the
    CHECKING passed, so this stays ``ok`` and simply says which result code the
    badge is reading; a warn here would fire on a healthy commission."""
    _r19_doctor_state(
        monkeypatch,
        tier="express",
        verify={
            "outcome": "pass",
            "claims": {
                "integration": {"status": "pass", "max_db": 0.7},
                # The producer's own reason word for this shape
                # (``crossover_v2.verification.ABSOLUTE_NO_SPEC_TOLERANCE``);
                # only ``status`` is read here, but an invented reason string
                # in a fixture teaches the next reader a word that does not
                # exist.
                "absolute": {
                    "status": "not_evaluated",
                    "reason": "no_spec_tolerance_for_region",
                },
            },
        },
    )

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "ok"
    assert "result=inconclusive" in r.detail
    assert "state=mark_verified" in r.detail


def test_a_legacy_state_with_no_result_evidence_discloses_no_result_code(
    monkeypatch,
):
    """A pre-R18 durable state carries a VERIFY outcome and nothing the result
    code is derived from, so the producer omits ``outcome`` entirely. The line
    then says nothing about a result rather than printing a code no instrument
    produced — the same never-fabricate-a-reading rule the spatial number
    beside it already follows."""
    _r19_doctor_state(monkeypatch, verify={"outcome": "pass"})

    r = doctor.check_crossover_v2_applied_is_graded()

    assert r.status == "ok"
    assert "result=" not in r.detail
    assert "capture verify=pass" in r.detail


def test_grade_spatial_and_scope_member_sets_are_pinned_for_their_consumers():
    """Walking-class guard (S1, #2242 gate). ``GRADE_SPATIAL_*`` and
    ``GRADE_SCOPE_*`` are each consumed by literal membership/equality tests
    in more than one surface — this doctor check (``spatial ==
    GRADE_SPATIAL_FAILED`` etc.) and the envelope's done-screen branches
    (``jasper/active_speaker/crossover_envelope_v2.py``, which spells the
    same words as raw string LITERALS by design — it never imports
    ``jasper.web``, so it cannot walk the producer's constants the way this
    test does). Neither consumer iterates the producer's vocabulary, so a
    NEW member added to either family is invisible to both until someone
    teaches each dispatch site the new word by hand — the exact shape of the
    S1 defect: an unrecognised ``spatial`` word used to fall through to the
    passing wording, unmentioned by any consumer and uncaught by any test.

    This pins the CURRENT member set by walking the producer module's own
    namespace (never a hand-copied list that can silently drift from it), so
    growing either family fails HERE, loudly, rather than surfacing as a
    branch nobody wrote on a live speaker.

    If this test fails because you added a new ``GRADE_SPATIAL_*`` or
    ``GRADE_SCOPE_*`` constant: that is the guard working. Review
    ``jasper/cli/doctor/correction.py``'s ``check_crossover_v2_applied_is_graded``
    and ``jasper/active_speaker/crossover_envelope_v2.py``'s done-screen
    branches, teach each one the new word (or confirm its existing
    fallthrough is the behaviour you want), and only then extend the pinned
    sets below.
    """
    from jasper.web import correction_crossover_v2 as v2host

    spatial_members = {
        value for name, value in vars(v2host).items()
        if name.startswith("GRADE_SPATIAL_") and isinstance(value, str)
    }
    scope_members = {
        value for name, value in vars(v2host).items()
        if name.startswith("GRADE_SCOPE_") and isinstance(value, str)
    }

    assert spatial_members == {
        v2host.GRADE_SPATIAL_ABSENT,
        v2host.GRADE_SPATIAL_PASSED,
        v2host.GRADE_SPATIAL_FAILED,
        v2host.GRADE_SPATIAL_UNMEASURABLE,
    }, (
        "GRADE_SPATIAL_* grew a new member — review "
        "jasper/cli/doctor/correction.py's check_crossover_v2_applied_is_graded "
        "and jasper/active_speaker/crossover_envelope_v2.py's done-screen "
        "spatial branches, then extend this pinned set"
    )
    assert scope_members == {
        v2host.GRADE_SCOPE_NONE,
        v2host.GRADE_SCOPE_MARK,
        v2host.GRADE_SCOPE_SPATIAL,
    }, (
        "GRADE_SCOPE_* grew a new member — review "
        "jasper/cli/doctor/correction.py's check_crossover_v2_applied_is_graded "
        "and jasper/active_speaker/crossover_envelope_v2.py's done-screen "
        "scope/complete handling, then extend this pinned set"
    )


def test_unapplied_session_is_not_asked_for_a_grade(monkeypatch):
    """Nothing on the speaker, nothing to grade — never a manufactured warn."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(v2host, "load_v2_state", lambda: {"applied": False})
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_applied_is_graded()
    assert r.status == "ok"
    assert "no applied measured crossover" in r.detail


def test_check_crossover_v2_cloud_pipeline_ok_when_every_group_passes(monkeypatch):
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: {
            "cloud": {
                "cloud_measure": {
                    "geometry": {"locked": False},
                    "pipeline": {
                        "available": True,
                        "spec": {"overall_passed": True, "bands": []},
                        "merged_excluded_bands_hz": [],
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert "cloud_measure: spec=pass" in r.detail


def test_check_crossover_v2_cloud_pipeline_reports_n_a_when_pipeline_unavailable(
    monkeypatch,
):
    """N4 review finding (2026-07-26): a group that CLOSED (geometry
    decided) but whose honest-instrument pipeline never became available
    (``combine_failed`` / ``pipeline_failed``) must read as ``spec=n/a`` —
    distinct from both ``pass`` and ``fail`` — and must not flip the
    overall check status to warn (an unavailable reading is not itself a
    spec failure). ``excluded_intervals=n/a``, not ``=0`` (SF-1 review
    finding, 2026-07-27): ``0`` would read as "the pipeline looked and found
    no interference" — a fabricated-clean claim for a pipeline that never
    ran at all."""
    from jasper.web import correction_crossover_v2 as v2host

    monkeypatch.setattr(
        v2host,
        "load_v2_state",
        lambda: {
            "cloud": {
                "cloud_measure": {
                    "geometry": {"locked": True},
                    "pipeline": {"available": False, "reason": "combine_failed"},
                },
            },
        },
    )
    monkeypatch.setattr(
        v2host, "session_volume_plan", lambda: SimpleNamespace(needs_recovery=False)
    )

    r = doctor.check_crossover_v2_cloud_pipeline()

    assert r.status == "ok"
    assert (
        "cloud_measure: spec=n/a excluded_intervals=n/a geometry_locked=True"
        in r.detail
    )


# --------------------------------------------------------------------------- #
# measurement hold + unresolved session volume (test 8 of the plan)
# --------------------------------------------------------------------------- #


def _patch_measurement(monkeypatch, hold=None, error=None):
    """Point check_measurement_hold's control read at a scripted answer."""
    from jasper.control import client as control_client

    def fake_get_measurement(**_kwargs):
        if error is not None:
            raise error
        return hold or {}

    monkeypatch.setattr(control_client, "get_measurement", fake_get_measurement)


def test_measurement_hold_ok_when_nothing_is_held(monkeypatch):
    _patch_measurement(monkeypatch, hold={"active": False})
    r = doctor.check_measurement_hold()
    assert r.status == "ok"
    assert "no measurement in progress" in r.detail


def test_measurement_hold_ok_for_an_ordinary_live_measurement(monkeypatch):
    _patch_measurement(monkeypatch, hold={
        "active": True, "owner": "seat-level", "mode": "gate", "held_for_s": 90.0,
    })
    r = doctor.check_measurement_hold()
    assert r.status == "ok"
    assert "held by seat-level" in r.detail
    assert "mode=gate" in r.detail


def test_measurement_hold_warns_past_the_longest_legal_session(monkeypatch):
    """The shape TTLs cannot catch: a live holder whose session never ends.

    Mutation-verified: dropping the ``held_for_s > MAX_WALL_CLOCK_CEILING_S``
    branch in ``check_measurement_hold`` turns this green-as-ok and the warn
    assertion red.
    """
    from jasper.active_speaker.session_volume_plan import MAX_WALL_CLOCK_CEILING_S

    _patch_measurement(monkeypatch, hold={
        "active": True,
        "owner": "correction-measurement",
        "mode": "gate",
        "held_for_s": MAX_WALL_CLOCK_CEILING_S + 1.0,
    })
    r = doctor.check_measurement_hold()
    assert r.status == "warn"
    assert "correction-measurement" in r.detail
    assert "declined" in r.detail
    # The remedy is reachable without an operator learning the internals.
    assert "Close the measurement page" in r.detail


def test_measurement_hold_warns_when_held_for_is_unreadable(monkeypatch):
    """A hold whose age cannot be read must not read as healthy."""
    _patch_measurement(monkeypatch, hold={
        "active": True, "owner": "seat-level", "mode": "gate", "held_for_s": None,
    })
    r = doctor.check_measurement_hold()
    assert r.status == "warn"
    assert "could not be ruled out" in r.detail


def test_measurement_hold_skips_when_control_is_down(monkeypatch):
    from jasper.control import client as control_client

    _patch_measurement(monkeypatch, error=control_client.ControlError("refused"))
    r = doctor.check_measurement_hold()
    assert r.status == "ok"
    assert "skipped" in r.detail


def _point_session_volume_at(monkeypatch, path):
    monkeypatch.setattr(
        doctor.correction, "DEFAULT_SESSION_VOLUME_STATE_PATH", path,
    )


def _write_session_volume(path, *, status, opened_at, ceiling_s=1800.0):
    from jasper.active_speaker.session_volume_plan import (
        SCHEMA_VERSION,
        STATE_KIND,
    )

    path.write_text(json.dumps({
        "kind": STATE_KIND,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": None if status == "active" else "restore_unconfirmed",
        "opened_at": opened_at,
        "wall_clock_ceiling_s": ceiling_s,
        "measurement_volume_db": -20.0,
        "original_main_volume_db": -6.0,
    }), encoding="utf-8")


def test_session_volume_ok_on_an_idle_speaker(monkeypatch, tmp_path):
    _point_session_volume_at(monkeypatch, tmp_path / "absent.json")
    r = doctor.check_session_volume_unresolved()
    assert r.status == "ok"
    assert "no unresolved" in r.detail


def test_session_volume_warns_on_an_unresolved_latch(monkeypatch, tmp_path):
    """Nothing in jasper/cli/doctor read needs_recovery before this check.

    A speaker could sit holding a measurement volume with every check green.
    Mutation-verified: making the check return ok unconditionally turns this red.
    """
    import time

    state = tmp_path / "session_volume.json"
    _write_session_volume(state, status="unresolved", opened_at=time.time())
    _point_session_volume_at(monkeypatch, state)

    r = doctor.check_session_volume_unresolved()
    assert r.status == "warn"
    assert "unresolved" in r.detail
    assert "crossover" in r.detail


def test_session_volume_reports_a_stale_active_state_as_a_crash_remnant(
    monkeypatch, tmp_path,
):
    """The flow force-drains this one, so it is reported, not acted on."""
    import time

    state = tmp_path / "session_volume.json"
    _write_session_volume(
        state, status="active", opened_at=time.time() - 4000.0, ceiling_s=1800.0,
    )
    _point_session_volume_at(monkeypatch, state)

    r = doctor.check_session_volume_unresolved()
    assert r.status == "ok"
    assert "force-drains" in r.detail


def test_session_volume_warns_on_a_durably_active_state_with_no_owner(
    monkeypatch, tmp_path,
):
    import time

    state = tmp_path / "session_volume.json"
    _write_session_volume(state, status="active", opened_at=time.time())
    _point_session_volume_at(monkeypatch, state)

    r = doctor.check_session_volume_unresolved()
    assert r.status == "warn"
    assert "durably active" in r.detail


def test_both_new_checks_are_registered():
    names = _registered_check_names()
    assert "check_measurement_hold" in names
    assert "check_session_volume_unresolved" in names


def test_check_active_speaker_startup_hold_registered_in_sync_checks():
    assert "check_active_speaker_startup_hold" in _registered_check_names()


def test_active_speaker_startup_hold_ok_when_no_hold_is_in_flight():
    # The conftest fixture points the marker at a per-test path that starts
    # absent, which is the no-hold baseline.
    r = doctor.check_active_speaker_startup_hold()

    assert r.status == "ok"
    assert "no staged-startup hold" in r.detail


def test_active_speaker_startup_hold_ok_while_a_load_is_actually_loaded(
    monkeypatch,
    tmp_path,
):
    from jasper.active_speaker.startup_hold import hold_staged_startup

    assert hold_staged_startup() is True
    state = tmp_path / "startup_load.json"
    state.write_text('{"status": "loaded"}', encoding="utf-8")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE", str(state))

    r = doctor.check_active_speaker_startup_hold()

    assert r.status == "ok"
    assert "in-flight protected load" in r.detail


def test_active_speaker_startup_hold_warns_on_a_stale_marker(monkeypatch, tmp_path):
    """A hold with no load behind it keeps a commissioned box on its SILENT
    anchor across every reconcile. This is the surface the household-facing
    "Open System status" copy for `staged_startup_hold_unavailable` points at,
    so it has to be able to say something."""

    from jasper.active_speaker.startup_hold import hold_staged_startup

    assert hold_staged_startup() is True
    state = tmp_path / "startup_load.json"
    state.write_text('{"status": "rolled_back"}', encoding="utf-8")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE", str(state))

    r = doctor.check_active_speaker_startup_hold()

    assert r.status == "warn"
    assert "stale staged-startup hold" in r.detail
    assert "rolled_back" in r.detail


# --- seat-SPL measurement reference -----------------------------------------


def _bank(path, **overrides):
    from jasper.active_speaker.seat_level_reference import (
        SeatLevelTarget,
        write_seat_level_reference,
    )

    payload = dict(
        reference_volume_db=-17.25,
        measured_db_spl=77.4,
        target=SeatLevelTarget(target_db_spl=77.5, tolerance_db=2.5),
        sensitivity={"sens_factor_db": -12.07, "serial": "8108494"},
        max_main_volume_db=-30.0,
        state_path=path,
    )
    payload.update(overrides)
    return write_seat_level_reference(**payload)


def test_seat_level_reference_absent_is_ok_not_a_warning(tmp_path):
    # A box that never ran the leveling step is healthy: the session falls back
    # to the codified reference and measures exactly as it always did.
    result = doctor.correction._classify_seat_level_reference(tmp_path / "absent.json")
    assert result.status == "ok"
    assert "not measured" in result.detail
    assert "-20 dB" in result.detail


def test_seat_level_reference_reports_the_value_the_session_will_hold(tmp_path):
    path = tmp_path / "ref.json"
    _bank(path)
    result = doctor.correction._classify_seat_level_reference(path)
    assert result.status == "ok"
    assert "-17.25 dB" in result.detail
    assert "77.4 dB SPL" in result.detail
    assert "8108494" in result.detail
    assert "0d ago" in result.detail


def test_seat_level_reference_present_but_unusable_warns(tmp_path):
    """The reader falls back silently; the doctor must not.

    An out-of-envelope value reads as absent at runtime — the speaker measures
    at -20 dB and says nothing. That silence is the whole reason for this line.
    """
    path = tmp_path / "ref.json"
    path.write_text(
        json.dumps(
            {
                "kind": "jts_active_speaker_seat_level_reference",
                "artifact_schema_version": 1,
                "reference_volume_db": 3.0,
            }
        )
    )
    result = doctor.correction._classify_seat_level_reference(path)
    assert result.status == "warn"
    assert "unusable" in result.detail
    assert "jasper-seat-level" in result.detail


def test_seat_level_reference_unparseable_timestamp_still_reports_the_value(tmp_path):
    path = tmp_path / "ref.json"
    _bank(path)
    raw = json.loads(path.read_text())
    raw["updated_at"] = "not-a-date"
    path.write_text(json.dumps(raw))
    result = doctor.correction._classify_seat_level_reference(path)
    assert result.status == "ok"
    assert "-17.25 dB" in result.detail
    assert "unparseable updated_at" in result.detail


def test_seat_level_reference_check_is_registered():
    assert "check_seat_level_reference" in _registered_check_names()


# --------------------------------------------------- /correction/ TLS cert
#
# check_correction_cert_hostname compares the cert's SAN against the name the
# speaker actually advertises, so a collision-renamed box stops serving a cert
# nobody's browser will accept.


def _with_cert(monkeypatch, tmp_path, exists=True):
    cert = tmp_path / "jts.local.crt"
    if exists:
        cert.write_text("---")
    real_path = doctor.correction.Path
    monkeypatch.setattr(
        doctor.correction,
        "Path",
        lambda p: cert if p == "/etc/nginx/ssl/jts.local.crt" else real_path(p),
    )


def _openssl_san(*names: str):
    return SimpleNamespace(
        returncode=0,
        stdout=(
            "X509v3 Subject Alternative Name:\n    "
            + ", ".join(f"DNS:{n}" for n in names)
            + "\n"
        ),
        stderr="",
    )


def test_cert_check_skips_without_a_cert(monkeypatch, tmp_path):
    _with_cert(monkeypatch, tmp_path, exists=False)

    assert doctor.correction.check_correction_cert_hostname().status == "ok"


@pytest.mark.parametrize(
    "advertised, san, status",
    [
        ("jts3.local", ("jts3.local", "*.jts3.local", "jts.local"), "ok"),
        ("jts3-2.local", ("jts3.local", "*.jts3.local"), "warn"),
    ],
    ids=["san-covers", "san-misses"],
)
def test_cert_check_compares_the_san_to_the_advertised_name(
    monkeypatch, tmp_path, advertised, san, status
):
    _with_cert(monkeypatch, tmp_path)
    _write_identity_env(
        tmp_path,
        monkeypatch,
        avahi=advertised,
        collision="0" if status == "ok" else "1",
        drift="0" if status == "ok" else "1",
    )

    with patch("subprocess.run", return_value=_openssl_san(*san)):
        r = doctor.correction.check_correction_cert_hostname()

    assert r.status == status
    if status == "warn":
        assert advertised in r.detail

def test_check_room_correction_authority_registered_in_sync_checks():
    assert "check_room_correction_authority" in _registered_check_names()


@pytest.mark.parametrize(
    "acoustic, expected_status",
    [
        pytest.param({"required": False}, "ok", id="no_authority_needed"),
        pytest.param(
            {"required": True, "allowed": True, "authority": "manual_applied_profile"},
            "ok",
            id="banked",
        ),
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": "active_commissioning_receipt_stale",
                "detail": "re-mint it when convenient",
            },
            "warn",
            id="unproven_runs_anyway",
        ),
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": _common.ROOM_AUTHORITY_RECEIPT_ABSENT,
                "detail": "finish commissioning when convenient",
            },
            "ok",
            id="never_minted_is_the_state_most_speakers_are_in",
        ),
        pytest.param(
            {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": _common.ROOM_AUTHORITY_RECEIPT_UNREADABLE,
                "cause": "PermissionError:EACCES:/var/lib/jasper/receipt.json",
                "detail": "a machine-level fault",
            },
            "warn",
            id="machine_fault_still_warns",
        ),
    ],
)
def test_room_correction_authority_warns_but_never_fails(
    monkeypatch, acoustic, expected_status,
):
    """The doctor line is the only place an unproven room run is visible.

    Ruling S10 stopped the receipt from refusing the run, so nothing else
    tells a household that the result it just measured is not banked as
    verified. WARN, never FAIL: nothing is broken and nothing is stopped.
    """
    from jasper.active_speaker import setup_status

    monkeypatch.setattr(
        setup_status,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {"acoustic_commissioning": acoustic},
    )

    r = doctor.check_room_correction_authority()

    assert r.status == expected_status


def test_room_correction_authority_names_the_record_it_could_not_open(monkeypatch):
    """The one sentence that ends the incident, without a trip to the journal.

    The reason names the CLASS. Only the cause says which file and which
    errno, and an unreadable receipt is indistinguishable from a stale one --
    a wrong remedy, not merely a vague line -- until the doctor prints it.
    """
    from jasper.active_speaker import setup_status

    cause = "PermissionError:EACCES:/var/lib/jasper/receipt.json"
    monkeypatch.setattr(
        setup_status,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {
            "acoustic_commissioning": {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": _common.ROOM_AUTHORITY_RECEIPT_UNREADABLE,
                "cause": cause,
                "detail": "a machine-level fault",
            },
        },
    )

    r = doctor.check_room_correction_authority()

    assert r.status == "warn"
    assert cause in r.detail


def test_absent_forwards_its_cause_so_a_vanished_receipt_is_not_hidden(monkeypatch):
    """ABSENT is demoted from a fleet-wide nag but is the catch-all default.

    "lifecycle is not verified" is the normal never-commissioned state; a store
    MISSING code under a verified lifecycle is a receipt that VANISHED -- a real
    anomaly. Both surface as ABSENT, so forwarding `cause` keeps them
    distinguishable in doctor output without turning the normal state that most
    of the fleet is in back into a warning (ruling 10 / ADR-0196).
    """
    from jasper.active_speaker import setup_status

    monkeypatch.setattr(
        setup_status,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {
            "acoustic_commissioning": {
                "required": True,
                "allowed": False,
                "authority": None,
                "reason": _common.ROOM_AUTHORITY_RECEIPT_ABSENT,
                "cause": "missing",
                "detail": "finish commissioning when convenient",
            },
        },
    )

    r = doctor.check_room_correction_authority()

    assert r.status == "ok"
    assert "missing" in r.detail


def test_room_correction_authority_warns_when_setup_cannot_be_read(monkeypatch):
    from jasper.active_speaker import setup_status

    def _unreadable(**_kwargs):
        raise OSError("secret filesystem detail")

    monkeypatch.setattr(
        setup_status, "read_active_speaker_setup_status", _unreadable
    )

    r = doctor.check_room_correction_authority()

    assert r.status == "warn"


def test_check_active_speaker_setup_notices_registered_in_sync_checks():
    assert "check_active_speaker_setup_notices" in _registered_check_names()


@pytest.mark.parametrize(
    "issues, expected_status",
    [
        pytest.param([], "ok", id="nothing_standing"),
        pytest.param(
            [{"severity": "blocker", "code": "x", "message": "m"}],
            "ok",
            id="blockers_have_their_own_surfaces",
        ),
        pytest.param(
            [{
                "severity": "warning",
                "code": "active_baseline_topology_changed",
                "message": "topology changed since the applied baseline",
            }],
            "warn",
            id="topology_notice",
        ),
    ],
)
def test_active_speaker_setup_notices_renders_only_non_blockers(
    monkeypatch, issues, expected_status,
):
    """Wave 7j's disclosure would otherwise be invisible.

    Nothing else in the tree renders a non-blocker setup issue, so demoting
    the topology block without this line would have been a silent deletion.
    """
    from jasper.active_speaker import setup_status

    monkeypatch.setattr(
        setup_status,
        "read_active_speaker_setup_status",
        lambda **_kwargs: {"issues": issues},
    )

    r = doctor.check_active_speaker_setup_notices()

    assert r.status == expected_status
    if expected_status == "warn":
        assert issues[0]["code"] in r.detail


def test_check_active_speaker_applied_graph_registered_in_sync_checks():
    assert "check_active_speaker_applied_graph" in _registered_check_names()


@pytest.mark.parametrize(
    "payload, expected_status",
    [
        pytest.param({"protected_profile": None}, "ok", id="nothing_applied_to_bind"),
        pytest.param(
            {"protected_profile": {"layer_a_binding": {
                "status": "current", "matches": True, "loaded_fingerprint": "a",
            }}},
            "ok",
            id="durable_graph_is_the_profiles",
        ),
        pytest.param(
            {"protected_profile": {"layer_a_binding": {
                "status": "distributed_active_unsupported", "matches": False,
            }}},
            "ok",
            id="grouped_active_is_not_drift",
        ),
        pytest.param(
            {
                "issues": [{
                    "severity": "blocker",
                    "code": setup_status_mod.IN_SEQUENCE_CAPTURE_ANCHOR_REASON,
                    "message": "commissioning graph is loaded",
                }],
                "protected_profile": {"layer_a_binding": {
                    "status": "mismatch", "matches": False,
                }},
            },
            "ok",
            id="staged_anchor_mismatch_is_by_design",
        ),
        pytest.param(
            {
                "active_config_path": "/var/lib/camilladsp/configs/candidate.yml",
                "protected_profile": {"layer_a_binding": {
                    "status": "mismatch", "matches": False,
                    "expected_fingerprint": "a", "loaded_fingerprint": "b",
                    "differences": [{
                        "field": "as_woofer_delay.delay",
                        "expected": "0.0",
                        "loaded": "0.1286",
                    }],
                }},
            },
            "warn",
            id="f6_rejected_delay_left_on_the_durable_anchor",
        ),
    ],
)
def test_active_speaker_applied_graph_discloses_never_gates(
    monkeypatch, payload, expected_status,
):
    """Blind-run finding F-6 had no surface that named the disagreement.

    WARN, never FAIL, and the two states that legitimately differ — a grouped
    active runtime, a staged/commissioning anchor — must not read as drift.
    """
    monkeypatch.setattr(
        setup_status_mod,
        "read_active_speaker_setup_status",
        lambda **_kwargs: payload,
    )

    r = doctor.check_active_speaker_applied_graph()

    assert r.status == expected_status
    if expected_status == "warn":
        assert "as_woofer_delay.delay" in r.detail
        assert "0.1286" in r.detail
