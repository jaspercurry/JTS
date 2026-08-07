# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor aec domain."""

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jasper.audio_profile_state import MicProbe
from jasper.cli import doctor


# --------------------------------------------- AEC bridge output assessment


def _rms_log_line(ref: int, mic: int, aec: int, attn_db: float) -> str:
    """Synthesize one bridge `rms over` log line in the journal `--output=cat`
    format the parser sees. Helper for the _assess_aec_bridge_output tests
    below."""
    return (
        f"2026-05-16 17:00:00,000 aec-bridge INFO "
        f"rms over 5.0s: ref={ref} mic={mic} aec={aec} → "
        f"attenuation={attn_db:.1f} dB (frames=1 ref_q=0 mic_q=0 "
        f"ref_clip=0.00% out_clip=0.00%)"
    )


def test_active_aec_probe_is_owned_by_dedicated_module(monkeypatch):
    monkeypatch.setattr(
        doctor.aec_probe,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive\n"),
    )

    results = doctor.probe_aec_ref_path()

    assert doctor.probe_aec_ref_path is doctor.aec_probe.probe_aec_ref_path
    assert [(result.name, result.status) for result in results] == [
        ("probe — bridge running", "fail")
    ]


def test_assess_aec_output_empty_journal_is_ok():
    """No rms lines = bridge probably just restarted in the assessment
    window. Not a failure, just nothing to evaluate."""
    r = doctor._assess_aec_bridge_output("")
    assert r.status == "ok"
    assert "no recent rms windows" in r.detail.lower()


def test_assess_aec_output_idle_returns_ok():
    """Mic and ref both quiet — speaker has been idle, no music has
    played. Doctor must NOT flag this as a degradation."""
    lines = [_rms_log_line(ref=0, mic=200, aec=30, attn_db=-16.5) for _ in range(10)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "no music activity" in r.detail.lower()


def test_assess_aec_output_silent_ref_with_no_healthy_window_fails():
    """The PR #75 dsnoop rate-lock signature: mic shows music acoustically
    throughout, ref delivers silence throughout, ZERO windows prove the
    ref chain ever worked in this period. The check MUST fail — this is
    the regression we exist to catch."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "fail"
    assert "reference path is delivering silence" in r.detail
    assert "Lessons learned" in r.detail  # actionable doc link


def test_assess_aec_output_silent_ref_downgrades_when_loopback_closed():
    """Same mic-loud + ref-silent shape as the rate-lock fail, but the
    music chain isn't active (no renderer writing the loopback). In
    that case ref MUST be silent — snd-aloop produces zeros without a
    producer — and the mic-loud bursts are TTS or voice (both bypass
    the loopback). Downgrade to OK with the diagnosis so a pure-voice
    session doesn't show as a degraded AEC bridge."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]
    r = doctor._assess_aec_bridge_output(
        "\n".join(lines),
        music_chain_active=False,
    )
    assert r.status == "ok"
    assert "loopback playback is closed" in r.detail
    assert "jasper_out bypasses the loopback" in r.detail
    # Counterpart: when music chain IS active, same input still fails —
    # the guard only relaxes the FAIL when we have positive evidence
    # the loopback is idle, not on uncertainty.
    r_active = doctor._assess_aec_bridge_output(
        "\n".join(lines),
        music_chain_active=True,
    )
    assert r_active.status == "fail"


def test_assess_aec_output_silent_ref_with_healthy_window_is_ok():
    """The 2026-05-16 false-positive: TTS / wake cues / loud ambient
    push silent_ref over threshold, but at least one window in the
    assessment period has ref signal (proving the chain works). The
    check must NOT fail — silent-ref windows have benign explanations
    when the ref path is demonstrably alive."""
    lines = [
        # 5 mic-loud + ref-silent windows from non-reference loud sound.
        _rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4),
        _rms_log_line(ref=0, mic=2400, aec=2300, attn_db=-0.4),
        _rms_log_line(ref=0, mic=2600, aec=2500, attn_db=-0.3),
        _rms_log_line(ref=0, mic=2100, aec=2050, attn_db=-0.2),
        _rms_log_line(ref=0, mic=2300, aec=2250, attn_db=-0.2),
        # 2 windows where music played and ref captured it correctly
        _rms_log_line(ref=800, mic=2400, aec=200, attn_db=-21.6),
        _rms_log_line(ref=1100, mic=2800, aec=180, attn_db=-23.8),
    ]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "likely TTS or ambient" in r.detail
    assert "ref path proven healthy" in r.detail


def test_assess_aec_output_healthy_aec_work_is_ok():
    """Music playing through the loopback, ref strong, attenuation
    meaningful — the bridge is doing its job. ok with a summary."""
    lines = [
        _rms_log_line(ref=1200, mic=2400, aec=150, attn_db=-24.1) for _ in range(8)
    ]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "real AEC work" in r.detail


def test_assess_aec_output_drift_warnings_warn():
    """High count of `drained N stale ref frames (drift)` warnings
    indicates ref/mic clock skew or rate mismatch. Warn, don't fail."""
    drift_line = (
        "2026-05-16 17:00:00,000 aec-bridge WARNING drained 7 stale ref frames (drift)"
    )
    # Threshold is 30 in 90 s; 40 is comfortably over.
    journal = "\n".join([drift_line] * 40)
    r = doctor._assess_aec_bridge_output(journal)
    assert r.status == "warn"
    assert "ref-drift warnings" in r.detail


def test_assess_aec_output_single_healthy_window_suffices():
    """Boundary: exactly one healthy_ref window flips the silent-ref
    pattern from fail to ok. Documents the design choice — if the ref
    chain proved itself once in the window, we trust it."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(7)]
    lines.append(_rms_log_line(ref=300, mic=400, aec=80, attn_db=-14.0))
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"


def test_assess_aec_output_silent_ref_below_alarm_surfaces_in_summary():
    """When silent_ref_count is 1-4 (non-zero but below the fail
    threshold of 5), the OK summary appends a `silent-ref=N` note so
    intermittent ref glitches are visible before they tip into a real
    outage. Per PR #124 upstream — preserved in the refactor."""
    lines = [
        _rms_log_line(ref=1200, mic=2400, aec=150, attn_db=-24.1) for _ in range(6)
    ]
    # 3 mic-loud + ref-silent windows: above 0 but below the 5-count alarm.
    lines += [_rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4) for _ in range(3)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "silent-ref=3" in r.detail
    assert "below alarm" in r.detail


def test_loopback_playback_active_reads_proc_status(tmp_path):
    """Helper must report True for any non-closed subdev and False when
    every subdev is closed. Verifies the first-line strip-and-compare
    against the actual /proc/asound status file format (single word
    `closed` vs `state: RUNNING\\n…`)."""
    fake_root = tmp_path / "asound" / "Loopback" / "pcm0p"
    fake_root.mkdir(parents=True)
    sub_paths = []
    for sub in range(4):
        d = fake_root / f"sub{sub}"
        d.mkdir()
        status = d / "status"
        status.write_text("closed\n")
        sub_paths.append(str(status))

    with patch("glob.glob", return_value=sub_paths):
        # All closed → inactive.
        assert doctor._loopback_playback_active() is False
        # Flip sub2 to RUNNING → active.
        (fake_root / "sub2" / "status").write_text(
            "state: RUNNING\nowner_pid   : 12345\n"
        )
        assert doctor._loopback_playback_active() is True

    # No status files at all (e.g., snd-aloop not loaded) → inactive,
    # never raises.
    with patch("glob.glob", return_value=[]):
        assert doctor._loopback_playback_active() is False


# ----------------------------------------- DTLN-aec engine health assessment


def _dtln_loaded_line(size: int = 256) -> str:
    """Synthesize the bridge's successful-load log line in journal
    `--output=cat` format. Matches jasper/cli/aec_bridge.py:~675."""
    return (
        f"2026-05-23 12:47:29,197 aec-bridge INFO "
        f"DTLN-aec engine enabled: size={size}, udp out=127.0.0.1:9878"
    )


def _dtln_failed_line(reason: str = "No such file or directory") -> str:
    """Synthesize the bridge's failed-load log line."""
    return (
        f"2026-05-23 12:47:29,197 aec-bridge WARNING "
        f"JASPER_AEC_DTLN_ENABLED set but DTLN couldn't load: {reason}. "
        f"Continuing with AEC3 only."
    )


def test_assess_dtln_engine_loaded_returns_ok():
    """Happy path: bridge logged a successful engine-init line.
    Doctor reports the engine size for the operator to confirm."""
    r = doctor._assess_dtln_engine(_dtln_loaded_line(size=256))
    assert r.status == "ok"
    assert "loaded" in r.detail.lower()
    assert "size=256" in r.detail


def test_assess_dtln_engine_load_failed_returns_fail():
    """The regression we exist to catch: JASPER_AEC_DTLN_ENABLED=1
    but the engine couldn't load (e.g. /var/lib/jasper/dtln/*.onnx
    missing because install.sh's download failed and the manual SCP
    step didn't happen). Without this check, the operator would
    spend a week analyzing 'DTLN never fires' data without realizing
    the engine never ran."""
    r = doctor._assess_dtln_engine(
        _dtln_failed_line(reason="DTLN ONNX models missing in /var/lib/jasper/dtln")
    )
    assert r.status == "fail"
    assert "couldn't load" in r.detail
    assert "/var/lib/jasper/dtln" in r.detail  # actionable path
    assert "jasper-aec-bridge" in r.detail  # actionable next step


def test_assess_dtln_engine_no_marker_warns():
    """Bridge running but no engine-init marker in the journal
    window — probably means the bridge hasn't restarted since the
    env var was set. Warn with the actionable fix command."""
    r = doctor._assess_dtln_engine("some unrelated log lines\nbridge boot\n")
    assert r.status == "warn"
    assert "systemctl restart jasper-aec-bridge" in r.detail


def test_assess_dtln_engine_picks_most_recent_marker():
    """If the journal window straddles a bridge restart that fixed
    an earlier failure, the LATER successful-load line wins. Reverse
    iteration in _assess_dtln_engine ensures we evaluate newest-first."""
    journal = "\n".join(
        [
            _dtln_failed_line(reason="onnxruntime import failed"),
            "(... operator fixed the venv ...)",
            _dtln_loaded_line(size=256),
        ]
    )
    r = doctor._assess_dtln_engine(journal)
    assert r.status == "ok"


def test_check_dtln_skips_when_env_disabled(monkeypatch):
    """When JASPER_AEC_DTLN_ENABLED is unset (legacy dual-stream
    config), the whole check should skip cleanly without running
    journalctl. This is the common case for non-triple-stream
    installs and must not flap."""
    monkeypatch.delenv("JASPER_AEC_DTLN_ENABLED", raising=False)
    r = doctor.check_aec_bridge_dtln_engine()
    assert r.status == "ok"
    assert "skipped" in r.detail.lower()


def _install_fake_dtln_registry(monkeypatch, tmp_path: Path):
    from jasper.aec_engines import dtln_models

    expected = hashlib.sha256(b"model").hexdigest()

    class _FakeEntry:
        def __init__(self, size: int):
            self.size = size

        def files(self, base_dir=tmp_path):
            base = Path(base_dir)
            return [
                (
                    base / f"dtln_aec_{self.size}_1.onnx",
                    "https://example.invalid/1",
                    expected,
                ),
                (
                    base / f"dtln_aec_{self.size}_2.onnx",
                    "https://example.invalid/2",
                    expected,
                ),
            ]

    entries = {
        128: _FakeEntry(128),
        256: _FakeEntry(256),
    }
    monkeypatch.setattr(dtln_models, "DEFAULT_SIZE", 256)
    monkeypatch.setattr(dtln_models, "REGISTRY", tuple(entries.values()))
    monkeypatch.setattr(dtln_models, "by_size", lambda size: entries.get(size))
    monkeypatch.setenv("JASPER_DTLN_MODEL_DIR", str(tmp_path))


def test_check_dtln_fails_when_enabled_model_file_missing(monkeypatch, tmp_path: Path):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "model files are missing" in r.detail
    assert "dtln_aec_256_2.onnx" in r.detail
    assert "deploy/install.sh" in r.detail


def test_check_dtln_fails_when_enabled_model_hash_mismatches(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_256_2.onnx").write_bytes(b"wrong-model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "hashes do not match" in r.detail
    assert "dtln_aec_256_2.onnx" in r.detail
    assert "deploy/install.sh" in r.detail


def test_check_dtln_uses_configured_model_size(monkeypatch, tmp_path: Path):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "128")
    monkeypatch.setattr(
        doctor.aec,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive"),
    )
    (tmp_path / "dtln_aec_128_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_128_2.onnx").write_bytes(b"model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "ok"
    assert "bridge not running" in r.detail


def test_check_dtln_fails_when_configured_model_size_is_invalid(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "large")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "JASPER_AEC_DTLN_SIZE" in r.detail
    assert "not an integer" in r.detail


def test_check_dtln_fails_when_configured_model_size_is_not_registered(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "512")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "JASPER_AEC_DTLN_SIZE=512" in r.detail
    assert "not registered" in r.detail
    assert "128" in r.detail
    assert "256" in r.detail


# ---------------------------------------------------------------------------
# Audio profile runtime truth — shared classifier used by /aec and doctor
# ---------------------------------------------------------------------------


def test_audio_profile_doctor_check_reports_active_chip_profile(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": True,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=True,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_CHIP_AEC_ENABLED": "1",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS": "ready",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "udp:9887",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "udp:9888",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "ok"
    assert "requested=xvf_chip_aec" in result.detail
    assert "active=xvf_chip_aec" in result.detail
    assert "Chip AEC 150 beam via :9876" in result.detail


def test_aec_bridge_running_reports_chip_forwarding(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if cmd == ["systemctl", "is-enabled", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda *, bridge_active=None: {
            "audio_profile": {"active": "xvf_chip_aec"},
            "microphone": {"processing_mode": "Chip-AEC"},
            "chip_aec_gate": {"status": "approved", "source": "static"},
        },
    )

    result = doctor.aec.check_aec_bridge_running()

    assert result.status == "ok"
    assert "chip-AEC beam forwarding" in result.detail
    assert "WebRTC AEC3 bypassed" in result.detail
    assert "gate=approved/static" in result.detail
    assert "software AEC enabled" not in result.detail


def test_aec_bridge_reports_expected_commissioning_park(monkeypatch):
    from jasper.mics import xvf3800

    def fake_run(cmd, **kwargs):
        if cmd == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
        if cmd == ["systemctl", "is-enabled", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    monkeypatch.setattr(xvf3800, "capture_channels", lambda: 6)
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda *, bridge_active=None: {
            "audio_profile": {
                "state": "commission_required",
                "reason": "alignment artifact is missing",
                "action": "Run sudo jasper-aec-commission",
            },
        },
    )

    result = doctor.aec.check_aec_bridge_running()

    assert result.status == "warn"
    assert "intentionally parked" in result.detail
    assert "alignment artifact is missing" in result.detail
    assert "Run sudo jasper-aec-commission" in result.detail


def test_aec_bridge_reports_deferred_outputd_park_without_blaming_the_bridge(
    monkeypatch,
):
    # `deferred` is aec-init's ordering-guard park: the live outputd has not
    # loaded /var/lib/jasper/outputd.env. Reporting it as `fail` would name
    # jasper-aec-bridge and recommend `journalctl -u jasper-aec-bridge` plus a
    # reconciler restart — the first is empty of anything relevant and the second
    # just re-defers. It must be a warn carrying the reconciler's own words.
    from jasper.mics import xvf3800

    def fake_run(cmd, **kwargs):
        if cmd == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
        if cmd == ["systemctl", "is-enabled", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    monkeypatch.setattr(xvf3800, "capture_channels", lambda: 6)
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda *, bridge_active=None: {
            "audio_profile": {
                "state": "deferred",
                "reason": "jasper-outputd has not loaded the current output declaration",
                "action": "Wait for jasper-outputd to restart, then run the reconciler",
            },
        },
    )

    result = doctor.aec.check_aec_bridge_running()

    assert result.status == "warn"
    assert "intentionally parked" in result.detail
    # The reconciler's reason and action, verbatim.
    assert (
        "jasper-outputd has not loaded the current output declaration"
        in result.detail
    )
    assert (
        "Wait for jasper-outputd to restart, then run the reconciler"
        in result.detail
    )
    # None of the bridge-failure remedy leaks through.
    assert "journalctl -u jasper-aec-bridge" not in result.detail
    assert "jasper-aec-commission" not in result.detail


def test_intentional_bridge_parks_match_the_reconciler_dispositions():
    # Both members are load-bearing: dropping either turns a deliberate park into
    # a hard FAIL naming the wrong subsystem.
    assert doctor.aec._INTENTIONAL_PARKS == frozenset(
        {"commission_required", "deferred"}
    )
    reconciler = (
        Path(doctor.aec.__file__).resolve().parents[3]
        / "deploy"
        / "bin"
        / "jasper-aec-reconcile"
    ).read_text(encoding="utf-8")
    for disposition in doctor.aec._INTENTIONAL_PARKS:
        assert f'write_alignment_env \\\n            "{disposition}"' in reconciler


def test_audio_profile_doctor_check_warns_when_runtime_env_pending(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": True,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=True,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_STATUS": "commission_required",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_REASON": "alignment artifact is missing",
            "JASPER_AEC_CHIP_AEC_ALIGNMENT_ACTION": "Run sudo jasper-aec-commission",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "warn"
    assert "active=none" in result.detail
    assert "alignment artifact is missing" in result.detail
    assert "action=Run sudo jasper-aec-commission" in result.detail


def test_audio_profile_doctor_check_names_stale_saved_aec_card(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": False,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=False,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "L16K6Ch",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="Array",
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "warn"
    assert "Configured AEC mic L16K6Ch" in result.detail
    assert "detected XVF card Array" in result.detail


def test_audio_validation_advisory_ok_when_chip_aec_not_requested():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "missing",
            "status": "unknown",
            "artifact_path": "/var/lib/jasper/audio-validation",
            "reason": "artifact not found",
        },
        requested_profile="xvf_software_aec3",
    )

    assert result.status == "ok"
    assert "advisory" in result.detail


def test_audio_validation_warns_when_chip_aec_requested_and_missing():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "missing",
            "status": "unknown",
            "artifact_path": "/var/lib/jasper/audio-validation",
            "reason": "artifact not found",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert "sudo jasper-audio-validate --stdout" in result.detail
    assert "advisory" in result.detail


def test_audio_validation_suggests_hardware_runner_when_ready_for_passive_evidence():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_hardware_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout" in result.detail
    )
    assert "advisory" in result.detail


def test_audio_validation_suggests_hardware_runner_for_drift_delay_recommendation():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout" in result.detail
    )


def test_audio_validation_ok_for_known_supported_passive_chip_aec_validation():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
            "hardware": {
                "mic_id": "xvf3800",
                "dac_id": "hifiberry_dac8x",
            },
            "check_statuses": {
                "runtime_profile": "pass",
                "mic_detected": "pass",
                "runtime_env": "pass",
                "service_state": "pass",
                "dac_reference": "pass",
                "wake_legs": "pass",
                "bridge_counters": "warn",
                "outputd_reference_health": "pass",
                "bridge_counter_window": "pass",
                "chip_profile_readback": "pass",
                "chip_convergence": "pass",
                "measured_drift_delay": "not_run",
            },
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "ok"
    assert "known-supported xvf_chip_aec path" in result.detail
    assert "optional acoustic drift/delay probe" in result.detail


def test_audio_validation_still_warns_for_unknown_dac_drift_delay_recommendation():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
            "hardware": {
                "mic_id": "xvf3800",
                "dac_id": "apple_usb_c_dongle",
            },
            "check_statuses": {
                "runtime_profile": "pass",
                "mic_detected": "pass",
                "runtime_env": "pass",
                "service_state": "pass",
                "dac_reference": "pass",
                "wake_legs": "pass",
                "outputd_reference_health": "pass",
                "bridge_counter_window": "pass",
                "chip_profile_readback": "pass",
                "chip_convergence": "pass",
                "measured_drift_delay": "not_run",
            },
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout" in result.detail
    )


def test_audio_validation_readiness_filters_current_hardware(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda: {"audio_profile": {"requested": "xvf_chip_aec"}},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_audio_validation_filter_kwargs",
        lambda **kwargs: {
            "requested_profile": kwargs["requested_profile"],
            "mic_id": "xvf3800",
            "dac_id": "apple_usb_c_dongle",
        },
    )

    def fake_summary(**kwargs):
        captured.update(kwargs)
        return {
            "state": "current",
            "status": "pass",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        }

    monkeypatch.setattr(doctor.aec, "_audio_validation_summary", fake_summary)

    result = doctor.check_audio_validation_readiness()

    assert result.status == "ok"
    assert captured == {
        "requested_profile": "xvf_chip_aec",
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
    }


# DTLN engine — bridge stats snapshot surface (journal-independent)
# ---------------------------------------------------------------------------


def _dtln_stats(enabled: bool, loaded: bool, error=None, age_sec: float = 1.0):
    import time as _time

    return {
        "schema_version": 1,
        "updated_epoch_sec": _time.time() - age_sec,
        "leg_engines": {
            "dtln": {"enabled": enabled, "loaded": loaded, "error": error},
        },
    }


def test_assess_dtln_stats_loaded_returns_ok():
    import time as _time

    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=True, loaded=True),
        _time.time(),
    )
    assert r is not None and r.status == "ok"
    assert "stats snapshot" in r.detail


def test_assess_dtln_stats_load_failure_returns_fail_with_detail():
    import time as _time

    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=True, loaded=False, error="onnx missing"),
        _time.time(),
    )
    assert r is not None and r.status == "fail"
    assert "onnx missing" in r.detail
    assert "engine unavailable" in r.detail
    assert "could not load" not in r.detail
    assert ":9878" in r.detail  # names the unfed leg voice listens on


def test_assess_dtln_stats_bridge_started_without_leg_warns():
    import time as _time

    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=False, loaded=False),
        _time.time(),
    )
    assert r is not None and r.status == "warn"
    assert "systemctl restart jasper-aec-bridge" in r.detail
    # A hand-set JASPER_AEC_DTLN_ENABLED=1 under the chip-AEC profile is
    # NOT a stale-restart problem — the chip profile never loads DTLN.
    # The message must point at checking the active input profile, not
    # only at restarting the bridge.
    assert "input profile" in r.detail
    assert "xvf_chip_aec" in r.detail


def test_assess_dtln_stats_stale_or_legacy_falls_back():
    import time as _time

    now = _time.time()
    # Stale snapshot (dead/old bridge process) → journal fallback.
    assert (
        doctor.aec._assess_dtln_engine_from_stats(
            _dtln_stats(enabled=True, loaded=True, age_sec=120.0),
            now,
        )
        is None
    )
    # Pre-leg_engines bridge build → journal fallback.
    assert (
        doctor.aec._assess_dtln_engine_from_stats(
            {"updated_epoch_sec": now},
            now,
        )
        is None
    )


def test_check_dtln_prefers_stats_snapshot_over_journal(
    monkeypatch,
    tmp_path: Path,
):
    """End-to-end: with a fresh stats snapshot reporting a load
    failure, the check fails from the snapshot and never shells out
    to journalctl (whose 10-min window would miss an old failure)."""
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_256_2.onnx").write_bytes(b"model")
    stats_path = tmp_path / "aec_bridge_stats.json"
    stats_path.write_text(
        json.dumps(
            _dtln_stats(enabled=True, loaded=False, error="no onnxruntime"),
        )
    )
    monkeypatch.setenv("JASPER_AEC_BRIDGE_STATS_PATH", str(stats_path))

    def _fake_run(cmd, **kwargs):
        if cmd[0] == "systemctl":
            return SimpleNamespace(stdout="active", stderr="", returncode=0)
        raise AssertionError(f"unexpected subprocess: {cmd}")

    monkeypatch.setattr(doctor.aec, "_run", _fake_run)

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "no onnxruntime" in r.detail


# --- Optional enhanced AEC: requested-only advisory -----------------


def test_enhanced_aec_doctor_is_quiet_and_cheap_when_not_requested(monkeypatch):
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "read_intent",
        lambda: {"requested": False},
    )

    def unexpected_status(**_kwargs):
        raise AssertionError("status/fingerprint work must stay requested-only")

    monkeypatch.setattr(doctor.aec.enhanced_aec, "status", unexpected_status)

    result = doctor.check_enhanced_aec()

    assert result.status == "ok"
    assert "not requested" in result.detail


@pytest.mark.parametrize("state", ["installed", "not_needed", "installing"])
def test_enhanced_aec_doctor_accepts_non_actionable_states(
    monkeypatch,
    state,
):
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "read_intent",
        lambda: {"requested": True},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda: {"audio_profile": {"active": "xvf_software_aec3"}},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout="inactive\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "status",
        lambda **_kwargs: {"state": state, "detail": f"state={state}"},
    )

    result = doctor.check_enhanced_aec()

    assert result.status == "ok"
    assert result.detail == f"state={state}"


@pytest.mark.parametrize(
    "state",
    ["not_installed", "failed", "stale", "unavailable"],
)
def test_enhanced_aec_doctor_warns_only_after_request(monkeypatch, state):
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "read_intent",
        lambda: {"requested": True},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda: {"audio_profile": {"active": "xvf_software_aec3"}},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            stdout="inactive\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        doctor.aec.enhanced_aec,
        "status",
        lambda **_kwargs: {"state": state, "detail": f"state={state}"},
    )

    result = doctor.check_enhanced_aec()

    assert result.status == "warn"
    assert "standard echo cancellation remains available" in result.detail
    assert "/system/" in result.detail
