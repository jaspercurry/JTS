# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free capture-profile contracts for jasper-aec-tune."""

import math
from pathlib import Path
import subprocess
import sys
import threading
import time
import wave
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import numpy as np
import pytest

from jasper.cli import aec_tune
from jasper.mics import xvf3800


def _write_card(root: Path, card: str, channels: int) -> None:
    card_dir = root / card
    card_dir.mkdir(parents=True)
    (card_dir / "stream0").write_text(
        f"Playback:\n  Channels: 2\nCapture:\n  Channels: {channels}\n"
    )


def _use_asound_root(monkeypatch, root: Path) -> MagicMock:
    detect_runtime_profile = xvf3800.detect_runtime_profile
    detector = MagicMock(side_effect=lambda: detect_runtime_profile(asound_root=root))
    monkeypatch.setattr(
        aec_tune.xvf3800,
        "detect_runtime_profile",
        detector,
    )
    return detector


def _fake_capture_with_channels(monkeypatch, recorded_channels: int) -> None:
    def capture(
        duration_sec: float,
        ref_wav: Path,
        mic_wav: Path,
        mic_device: str,
        mic_channels: int,
    ) -> bool:
        del duration_sec, mic_device
        assert mic_channels == recorded_channels
        ref = np.full((2400, 2), 1000, dtype=np.int16)
        mic_shape = 800 if recorded_channels == 1 else (800, recorded_channels)
        mic = np.full(mic_shape, 1000, dtype=np.int16)
        aec_tune._write_wav(ref_wav, ref, 48000)
        aec_tune._write_wav(mic_wav, mic, aec_tune.SAMPLE_RATE)
        return True

    monkeypatch.setattr(aec_tune, "_capture_simultaneous", capture)


def _prepare_main(
    monkeypatch, tmp_path: Path, recorded_channels: int, channel_index: int
) -> tuple[MagicMock, MagicMock]:
    detector = _use_asound_root(monkeypatch, tmp_path)
    _fake_capture_with_channels(monkeypatch, recorded_channels)
    monkeypatch.setattr(aec_tune, "_camilla_get_volume", lambda: 0.0)
    monkeypatch.setattr(
        aec_tune,
        "_service_is_active",
        MagicMock(side_effect=lambda unit: unit == "jasper-voice.service"),
    )
    monkeypatch.setattr(aec_tune, "_stop_service", MagicMock())
    restart = MagicMock()
    monkeypatch.setattr(aec_tune, "_start_service", restart)
    monkeypatch.setattr(aec_tune.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "jasper-aec-tune",
            "--mic-channels",
            str(recorded_channels),
            "--mic-channel",
            str(channel_index),
        ],
    )
    return detector, restart


def test_default_mic_capture_falls_back_when_xvf_is_absent(
    monkeypatch, tmp_path: Path
) -> None:
    detector = _use_asound_root(monkeypatch, tmp_path)

    parser = aec_tune._argument_parser()
    args = parser.parse_args([])

    assert args.mic_device == "hw:CARD=Array,DEV=0"
    assert args.mic_channels == 2
    help_text = " ".join(parser.format_help().split())
    assert "default: hw:CARD=Array,DEV=0" in help_text
    assert "raw mics on 2-5. (default: 2)" in help_text
    detector.assert_called_once_with()


@pytest.mark.parametrize(
    "variant", xvf3800.FIRMWARE_VARIANTS, ids=lambda variant: variant.variant_id
)
def test_default_mic_capture_uses_detected_registry_variant(
    monkeypatch, tmp_path: Path, variant: xvf3800.FirmwareVariant
) -> None:
    _write_card(tmp_path, variant.alsa_card_name, variant.capture_channels)
    detector = _use_asound_root(monkeypatch, tmp_path)

    parser = aec_tune._argument_parser()
    args = parser.parse_args([])

    assert args.mic_device == f"hw:CARD={variant.alsa_card_name},DEV=0"
    assert args.mic_channels == variant.capture_channels
    help_text = " ".join(parser.format_help().split())
    assert f"default: hw:CARD={variant.alsa_card_name},DEV=0" in help_text
    assert f"raw mics on 2-5. (default: {variant.capture_channels})" in help_text
    detector.assert_called_once_with()


def test_explicit_mic_device_overrides_detected_default(
    monkeypatch, tmp_path: Path
) -> None:
    _write_card(tmp_path, "L16K6Ch", 6)
    detector = _use_asound_root(monkeypatch, tmp_path)

    args = aec_tune._argument_parser().parse_args(
        ["--mic-device", "plughw:CARD=BenchMic,DEV=1"]
    )

    assert args.mic_device == "plughw:CARD=BenchMic,DEV=1"
    assert args.mic_channels == 6
    detector.assert_called_once_with()


def test_explicit_mic_channels_override_detected_default(
    monkeypatch, tmp_path: Path
) -> None:
    _write_card(tmp_path, "L16K6Ch", 6)
    detector = _use_asound_root(monkeypatch, tmp_path)

    args = aec_tune._argument_parser().parse_args(["--mic-channels", "4"])

    assert args.mic_device == "hw:CARD=L16K6Ch,DEV=0"
    assert args.mic_channels == 4
    detector.assert_called_once_with()


@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_mic_channel_count_is_rejected_before_capture(
    monkeypatch, tmp_path: Path, capsys, value: str
) -> None:
    detector = _use_asound_root(monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        aec_tune._argument_parser().parse_args(["--mic-channels", value])

    assert (
        "argument --mic-channels: must be greater than zero" in capsys.readouterr().err
    )
    detector.assert_called_once_with()


@pytest.mark.parametrize(
    ("recorded_channels", "channel_index"),
    [
        (1, -1),
        (1, 1),
        (2, 2),
    ],
)
def test_invalid_mic_channel_is_rejected_from_recorded_wav_and_voice_recovers(
    monkeypatch,
    tmp_path: Path,
    recorded_channels: int,
    channel_index: int,
) -> None:
    detector, restart = _prepare_main(
        monkeypatch, tmp_path, recorded_channels, channel_index
    )

    assert aec_tune.main() == 1

    restart.assert_called_once_with("jasper-voice.service")
    detector.assert_called_once_with()


def test_valid_mono_channel_is_diagnostic_only_and_voice_recovers(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    detector, restart = _prepare_main(monkeypatch, tmp_path, 1, 0)
    monkeypatch.setattr(aec_tune, "_correlate_and_find_lag", lambda mic, ref: (12, 0.5))
    apply_delay = MagicMock()
    monkeypatch.setattr(aec_tune, "_apply_volatile_delay", apply_delay)

    assert aec_tune.main() == 0

    assert "Diagnostic AUDIO_MGR_SYS_DELAY candidate = 12" in capsys.readouterr().out
    assert not hasattr(aec_tune, "DELAY_FILE")
    apply_delay.assert_not_called()
    restart.assert_called_once_with("jasper-voice.service")
    detector.assert_called_once_with()


def test_explicit_apply_uses_verified_volatile_write_and_voice_recovers(
    monkeypatch, tmp_path: Path
) -> None:
    detector, restart = _prepare_main(monkeypatch, tmp_path, 1, 0)
    monkeypatch.setattr(aec_tune, "_correlate_and_find_lag", lambda mic, ref: (12, 0.5))
    from jasper.xvf import xvf_host

    device = MagicMock()
    device.read.side_effect = [(9,), (12,)]
    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=device))
    sys.argv.append("--apply")

    assert aec_tune.main() == 0

    device.write.assert_called_once_with("AUDIO_MGR_SYS_DELAY", [12])
    assert device.read.call_args_list == [
        call("AUDIO_MGR_SYS_DELAY"),
        call("AUDIO_MGR_SYS_DELAY"),
    ]
    device.close.assert_called_once_with()
    restart.assert_called_once_with("jasper-voice.service")
    detector.assert_called_once_with()


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), 0.00099])
def test_apply_rejects_nonfinite_or_low_confidence_before_hardware(
    monkeypatch, confidence: float
) -> None:
    from jasper.xvf import xvf_host

    find = MagicMock()
    monkeypatch.setattr(xvf_host, "find", find)

    assert aec_tune._apply_volatile_delay(12, confidence) is False
    find.assert_not_called()


@pytest.mark.parametrize("lag", [-65, 257])
def test_apply_rejects_delay_outside_confirmed_range_before_hardware(
    monkeypatch, lag: int
) -> None:
    from jasper.xvf import xvf_host

    find = MagicMock()
    monkeypatch.setattr(xvf_host, "find", find)

    assert aec_tune._apply_volatile_delay(lag, 0.5) is False
    find.assert_not_called()


@pytest.mark.parametrize("lag", [-64, 256])
def test_apply_accepts_confirmed_range_boundaries(monkeypatch, lag: int) -> None:
    from jasper.xvf import xvf_host

    device = MagicMock()
    device.read.side_effect = [(0,), (lag,)]
    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=device))

    assert aec_tune._apply_volatile_delay(lag, aec_tune.MIN_APPLY_CONFIDENCE)
    device.write.assert_called_once_with("AUDIO_MGR_SYS_DELAY", [lag])
    device.close.assert_called_once_with()


def test_apply_fails_closed_when_device_is_missing(monkeypatch) -> None:
    from jasper.xvf import xvf_host

    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=None))

    assert aec_tune._apply_volatile_delay(12, 0.5) is False


def test_apply_refuses_write_when_prior_delay_cannot_be_read(monkeypatch) -> None:
    from jasper.xvf import xvf_host

    device = MagicMock()
    device.read.side_effect = OSError("USB read failed")
    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=device))

    assert aec_tune._apply_volatile_delay(12, 0.5) is False

    device.write.assert_not_called()
    device.close.assert_called_once_with()


def test_apply_fails_closed_on_readback_mismatch_and_closes_device(
    monkeypatch,
) -> None:
    from jasper.xvf import xvf_host

    device = MagicMock()
    device.read.side_effect = [(7,), (13,), (7,)]
    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=device))

    assert aec_tune._apply_volatile_delay(12, 0.5) is False
    assert device.write.call_args_list == [
        call("AUDIO_MGR_SYS_DELAY", [12]),
        call("AUDIO_MGR_SYS_DELAY", [7]),
    ]
    device.close.assert_called_once_with()


def test_apply_fails_closed_on_write_error_and_closes_device(monkeypatch) -> None:
    from jasper.xvf import xvf_host

    device = MagicMock()
    device.read.side_effect = [(7,), (7,)]
    device.write.side_effect = [OSError("USB write failed"), None]
    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=device))

    assert aec_tune._apply_volatile_delay(12, 0.5) is False
    assert device.write.call_args_list == [
        call("AUDIO_MGR_SYS_DELAY", [12]),
        call("AUDIO_MGR_SYS_DELAY", [7]),
    ]
    assert device.read.call_args_list == [
        call("AUDIO_MGR_SYS_DELAY"),
        call("AUDIO_MGR_SYS_DELAY"),
    ]
    device.close.assert_called_once_with()


def test_apply_reports_uncertain_state_when_rollback_fails(monkeypatch) -> None:
    from jasper.xvf import xvf_host

    device = MagicMock()
    device.read.side_effect = [(7,), (13,)]
    device.write.side_effect = [None, OSError("rollback write failed")]
    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=device))

    assert aec_tune._apply_volatile_delay(12, 0.5) is False

    assert device.write.call_args_list == [
        call("AUDIO_MGR_SYS_DELAY", [12]),
        call("AUDIO_MGR_SYS_DELAY", [7]),
    ]
    device.close.assert_called_once_with()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_active_mode_rejects_invalid_duck_before_runtime_side_effects(
    monkeypatch, tmp_path: Path, value: str
) -> None:
    _use_asound_root(monkeypatch, tmp_path)
    get_volume = MagicMock()
    stop = MagicMock()
    monkeypatch.setattr(aec_tune, "_camilla_get_volume", get_volume)
    monkeypatch.setattr(aec_tune, "_stop_service", stop)
    monkeypatch.setattr(
        sys,
        "argv",
        ["jasper-aec-tune", "--inject-noise", "--duck-by", value],
    )

    with pytest.raises(SystemExit):
        aec_tune.main()

    get_volume.assert_not_called()
    stop.assert_not_called()


def test_active_mode_rejects_nonfinite_current_volume_before_side_effects(
    monkeypatch, tmp_path: Path
) -> None:
    _use_asound_root(monkeypatch, tmp_path)
    monkeypatch.setattr(aec_tune, "_camilla_get_volume", lambda: math.nan)
    stop = MagicMock()
    popen = MagicMock()
    monkeypatch.setattr(aec_tune, "_stop_service", stop)
    monkeypatch.setattr(aec_tune.subprocess, "Popen", popen)
    monkeypatch.setattr(sys, "argv", ["jasper-aec-tune", "--inject-noise"])

    assert aec_tune.main() == 1
    stop.assert_not_called()
    popen.assert_not_called()


def _own_the_tune_fader():
    """Install the process fader owner over the tree's clamped Camilla door.

    `_camilla_set_volume` declares through the owner since wave 5b, and
    `aec_tune.main()` registers one. These tests drive the helper directly, so
    they install the same shape the registration does: doors bound to
    `primary_controller()`, which is where `_coerce_main_volume_db` lives —
    so the ceiling this file pins is still reached through the door under test.
    """
    from jasper.camilla import primary_controller
    from jasper.volume_owner import VolumeOwner, install_volume_owner

    fader = primary_controller()
    install_volume_owner(
        VolumeOwner(
            set_fader_db=lambda db: fader.set_volume_db(db, best_effort=True),
            get_fader_db=lambda: fader.get_volume_db(best_effort=True),
        )
    )


def test_camilla_set_volume_requires_finite_matching_readback(monkeypatch) -> None:
    volume = SimpleNamespace(
        set_main_volume=MagicMock(),
        main_volume=MagicMock(return_value=math.nan),
    )
    client = SimpleNamespace(
        volume=volume,
        connect=MagicMock(),
        disconnect=MagicMock(),
    )
    monkeypatch.setitem(
        sys.modules,
        "camilladsp",
        SimpleNamespace(CamillaClient=lambda _host, _port: client),
    )
    _own_the_tune_fader()

    with pytest.raises(aec_tune.CamillaVolumeError, match="not finite"):
        aec_tune._camilla_set_volume(-20.0)

    volume.set_main_volume.assert_called_once_with(-20.0)
    client.disconnect.assert_called_once_with()


def test_the_tune_cli_write_sees_the_zero_db_ceiling(monkeypatch) -> None:
    """One hardware door, so the ceiling cannot be routed around.

    This module used to build its own ``CamillaClient`` and call
    ``set_main_volume`` directly, which meant its writes never reached
    ``_coerce_main_volume_db`` — two doors for one clamp. What lands at the
    hardware is now the clamped value, and the readback confirm then refuses
    rather than reporting a level the speaker never played.
    """
    volume = SimpleNamespace(
        set_main_volume=MagicMock(),
        main_volume=MagicMock(return_value=0.0),
    )
    client = SimpleNamespace(
        volume=volume,
        connect=MagicMock(),
        disconnect=MagicMock(),
    )
    monkeypatch.setitem(
        sys.modules,
        "camilladsp",
        SimpleNamespace(CamillaClient=lambda _host, _port: client),
    )
    _own_the_tune_fader()

    with pytest.raises(aec_tune.CamillaVolumeError):
        aec_tune._camilla_set_volume(6.0)

    volume.set_main_volume.assert_called_once_with(0.0)


def test_systemctl_state_stop_and_start_are_all_bounded(monkeypatch) -> None:
    run = MagicMock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout="active\n"),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=3, stdout="inactive\n"),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout="active\n"),
        ]
    )
    monkeypatch.setattr(aec_tune.subprocess, "run", run)

    assert aec_tune._service_is_active("jasper-voice.service")
    aec_tune._stop_service("jasper-voice.service", "capture endpoint")
    aec_tune._start_service("jasper-voice.service")

    assert [item.args[0] for item in run.call_args_list] == [
        ["systemctl", "is-active", "jasper-voice.service"],
        ["systemctl", "stop", "jasper-voice.service"],
        ["systemctl", "is-active", "jasper-voice.service"],
        ["systemctl", "start", "jasper-voice.service"],
        ["systemctl", "is-active", "jasper-voice.service"],
    ]
    assert all(
        item.kwargs["timeout"] == aec_tune.SYSTEMCTL_TIMEOUT_SEC
        for item in run.call_args_list
    )


def test_active_mode_does_not_play_until_duck_is_verified_and_restores_volume(
    monkeypatch, tmp_path: Path
) -> None:
    _prepare_main(monkeypatch, tmp_path, 1, 0)
    set_volume = MagicMock(
        side_effect=[aec_tune.CamillaVolumeError("readback mismatch"), None]
    )
    popen = MagicMock()
    monkeypatch.setattr(aec_tune, "_camilla_set_volume", set_volume)
    monkeypatch.setattr(aec_tune.subprocess, "Popen", popen)
    sys.argv.append("--inject-noise")

    assert aec_tune.main() == 1
    assert set_volume.call_args_list == [
        call(-20.0),
        call(0.0),
    ]
    popen.assert_not_called()


def test_active_noise_uses_canonical_correction_fanin_lane(
    monkeypatch, tmp_path: Path
) -> None:
    _prepare_main(monkeypatch, tmp_path, 1, 0)
    monkeypatch.setattr(aec_tune, "_camilla_set_volume", MagicMock())
    monkeypatch.setattr(aec_tune, "_correlate_and_find_lag", lambda mic, ref: (12, 0.5))
    play_proc = MagicMock()
    play_proc.wait.return_value = 0
    play_proc.poll.return_value = 0
    popen = MagicMock(return_value=play_proc)
    monkeypatch.setattr(aec_tune.subprocess, "Popen", popen)
    sys.argv.append("--inject-noise")

    assert aec_tune.main() == 0

    argv = popen.call_args.args[0]
    assert argv[argv.index("-D") + 1] == "correction_substream"
    assert "jasper_out" not in " ".join(argv)


def test_active_capture_exception_reaps_aplay_restores_volume_and_voice(
    monkeypatch, tmp_path: Path
) -> None:
    _detector, restart = _prepare_main(monkeypatch, tmp_path, 1, 0)
    monkeypatch.setattr(
        aec_tune,
        "_capture_simultaneous",
        MagicMock(side_effect=RuntimeError("capture exploded")),
    )
    set_volume = MagicMock()
    monkeypatch.setattr(aec_tune, "_camilla_set_volume", set_volume)
    play_proc = MagicMock()
    play_proc.poll.return_value = None
    play_proc.wait.return_value = 0
    monkeypatch.setattr(aec_tune.subprocess, "Popen", MagicMock(return_value=play_proc))
    sys.argv.append("--inject-noise")

    assert aec_tune.main() == 1
    play_proc.terminate.assert_called_once_with()
    play_proc.wait.assert_called_once_with(timeout=aec_tune.PROCESS_EXIT_GRACE_SEC)
    assert set_volume.call_args_list == [
        call(-20.0),
        call(0.0),
    ]
    restart.assert_called_once_with("jasper-voice.service")


def test_keyboard_interrupt_during_capture_still_restarts_voice(
    monkeypatch, tmp_path: Path
) -> None:
    _detector, restart = _prepare_main(monkeypatch, tmp_path, 1, 0)
    monkeypatch.setattr(
        aec_tune,
        "_capture_simultaneous",
        MagicMock(side_effect=KeyboardInterrupt),
    )

    assert aec_tune.main() == 130
    restart.assert_called_once_with("jasper-voice.service")


def test_active_capture_owner_services_stop_and_restore_in_dependency_order(
    monkeypatch, tmp_path: Path
) -> None:
    _prepare_main(monkeypatch, tmp_path, 1, 0)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(aec_tune, "_service_is_active", lambda _unit: True)
    monkeypatch.setattr(
        aec_tune,
        "_stop_service",
        lambda unit, _label: events.append(("stop", unit)),
    )
    monkeypatch.setattr(
        aec_tune,
        "_start_service",
        lambda unit: events.append(("start", unit)),
    )
    monkeypatch.setattr(aec_tune, "_correlate_and_find_lag", lambda mic, ref: (12, 0.5))

    assert aec_tune.main() == 0

    assert events == [
        ("stop", "jasper-voice.service"),
        ("stop", "jasper-aec-bridge.service"),
        ("start", "jasper-aec-bridge.service"),
        ("start", "jasper-voice.service"),
    ]


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [(RuntimeError("systemctl failed"), 1), (KeyboardInterrupt(), 130)],
)
def test_service_is_restored_when_stop_fails_after_unit_may_have_stopped(
    monkeypatch,
    tmp_path: Path,
    failure: BaseException,
    expected_status: int,
) -> None:
    _prepare_main(monkeypatch, tmp_path, 1, 0)
    monkeypatch.setattr(
        aec_tune,
        "_service_is_active",
        lambda unit: unit == "jasper-voice.service",
    )
    monkeypatch.setattr(aec_tune, "_stop_service", MagicMock(side_effect=failure))
    restore = MagicMock()
    monkeypatch.setattr(aec_tune, "_start_service", restore)

    assert aec_tune.main() == expected_status
    restore.assert_called_once_with("jasper-voice.service")


def test_restart_failure_overrides_successful_diagnostic(
    monkeypatch, tmp_path: Path
) -> None:
    _detector, restart = _prepare_main(monkeypatch, tmp_path, 1, 0)
    restart.side_effect = RuntimeError("start failed")
    monkeypatch.setattr(aec_tune, "_correlate_and_find_lag", lambda mic, ref: (12, 0.5))

    assert aec_tune.main() == 1


def test_bridge_restore_timeout_does_not_skip_voice_restore(
    monkeypatch, tmp_path: Path
) -> None:
    _prepare_main(monkeypatch, tmp_path, 1, 0)
    monkeypatch.setattr(aec_tune, "_service_is_active", lambda _unit: True)
    monkeypatch.setattr(aec_tune, "_stop_service", MagicMock())
    monkeypatch.setattr(aec_tune, "_correlate_and_find_lag", lambda mic, ref: (12, 0.5))
    restored: list[str] = []

    def restore(unit: str) -> None:
        restored.append(unit)
        if unit == "jasper-aec-bridge.service":
            raise subprocess.TimeoutExpired(["systemctl", "start", unit], 10.0)

    monkeypatch.setattr(aec_tune, "_start_service", restore)

    assert aec_tune.main() == 1
    assert restored == ["jasper-aec-bridge.service", "jasper-voice.service"]


# ---------------------------------------------------------------------------
# The reference leg: jasper-outputd's UDP speaker monitor (U4/P7-2).
#
# Since P7-2 the passive reference is NOT an `arecord` child on the aloop
# dsnoop tap — it is a bound socket drained on a thread. These pin the wire
# contract and the lifecycle that replaced the second child.
# ---------------------------------------------------------------------------


def _stub_reference_socket(monkeypatch, payload: bytes | None) -> MagicMock:
    """Replace the socket + drain with a stub, so no real port is bound.

    Returns the stub socket the caller can assert was closed. `payload=None`
    means the monitor delivered nothing.

    Also pins the target resolution. `_capture_simultaneous` resolves it for
    real, which reads the HOST's env files — so without this these tests pass
    on a laptop and fail on a parked Pi, where the reconciler has written an
    empty `JASPER_OUTPUTD_REFERENCE_UDP_TARGET`. The value is irrelevant here
    (the socket is a stub); not depending on the host is the point.
    """
    monkeypatch.delenv(aec_tune.REFERENCE_UDP_TARGET_ENV, raising=False)
    monkeypatch.setattr(
        aec_tune,
        "merged_env_files",
        lambda: {aec_tune.REFERENCE_UDP_TARGET_ENV: "127.0.0.1:9891"},
    )
    sock = MagicMock()
    monkeypatch.setattr(aec_tune, "_open_reference_socket", lambda host, port: sock)

    def drain(_sock, _deadline, out: list[bytes], _stop) -> None:
        if payload is not None:
            out.append(payload)

    monkeypatch.setattr(aec_tune, "_drain_reference_socket", drain)
    return sock


def test_reference_wav_decodes_the_wire_as_48k_stereo_s16(tmp_path: Path) -> None:
    """The :9891 wire is headerless little-endian interleaved stereo int16.

    Pinned against the producer's own guard,
    `the_reference_datagram_is_exactly_one_s16_stereo_period` in
    `rust/jasper-outputd/src/main.rs`: one datagram is one narrowed playout
    period, `period_frames * 2 channels * 2 bytes`, no header to skip. If this
    tool ever mis-framed it, the correlation would still produce a plausible
    number — which is exactly why the decode is asserted rather than assumed.
    """
    frames = np.array([[1, -2], [3, -4], [5, -6]], dtype="<i2")
    wire = frames.tobytes()
    ref_wav = tmp_path / "ref.wav"

    aec_tune._write_reference_wav(ref_wav, [wire])

    samples, rate, channels = aec_tune._read_wav_int16(ref_wav)
    assert (rate, channels) == (48000, 2)
    assert samples.tolist() == frames.tolist()
    # BYTE-TRANSPARENT, asserted on the bytes. Both the wire and a WAV payload
    # are little-endian S16, so this stage must pass the payload through
    # untouched. Defense in depth, not a gap the value check leaves open: a
    # real `.byteswap()` trips BOTH assertions, and merely respelling the
    # decode dtype is transparent in values AND bytes (so it is no bug and
    # neither assertion should fire). This one states the property the wire
    # actually has, in the units the wire actually has it in.
    with wave.open(str(ref_wav), "rb") as handle:
        assert handle.readframes(handle.getnframes()) == wire


def test_the_reference_contract_matches_outputd_the_producer() -> None:
    """48 kHz stereo is jasper-outputd's fact, not this tool's free parameter.

    Cross-language pin against the producer's own source. Asserting only
    `rate == REFERENCE_RATE` would be self-referential — moving the constant
    would move both sides of the comparison and stay green — so the constant is
    checked against `rust/jasper-outputd/src/types.rs`, where the value is
    fixed and `Config::from_env` refuses any other rate.
    """
    types_rs = (
        Path(__file__).resolve().parents[1]
        / "rust"
        / "jasper-outputd"
        / "src"
        / "types.rs"
    ).read_text()

    assert f"pub const SAMPLE_RATE: u32 = {aec_tune.REFERENCE_RATE:_};" in types_rs, (
        "aec_tune.REFERENCE_RATE must equal jasper-outputd's core sample rate; "
        "the reference datagrams are that daemon's playout periods"
    )
    assert f"pub const CHANNELS: u16 = {aec_tune.REFERENCE_CHANNELS};" in types_rs, (
        "aec_tune.REFERENCE_CHANNELS must equal jasper-outputd's channel count; "
        "the reference is stereo whatever the sink's width"
    )

    # ONE wire fact, so pin EVERY Python declarer of it against the same
    # producer literals. The bridge and the tuner bind the same monitor; if
    # they could disagree about its geometry, only one of them would be right.
    from jasper.cli import aec_bridge

    assert (aec_bridge.REF_RATE, aec_bridge.REF_CHANNELS) == (
        aec_tune.REFERENCE_RATE,
        aec_tune.REFERENCE_CHANNELS,
    ), (
        "jasper-aec-bridge and jasper-aec-tune read the SAME outputd speaker "
        "monitor; their declared rate/channels must not diverge"
    )


def test_reference_wav_drops_a_partial_trailing_frame(tmp_path: Path) -> None:
    """A short tail is trimmed, not reshaped into an exception."""
    ref_wav = tmp_path / "ref.wav"
    whole = np.array([[7, 8]], dtype="<i2").tobytes()

    aec_tune._write_reference_wav(ref_wav, [whole, b"\x01"])

    samples, _rate, _channels = aec_tune._read_wav_int16(ref_wav)
    assert samples.tolist() == [[7, 8]]


def test_reference_datagrams_survive_a_real_loopback_round_trip(
    tmp_path: Path,
) -> None:
    """Bind, receive, and decode for real — the three helpers composed.

    Ephemeral port, so this can never collide with a parallel worker or with
    the production monitor. This is the one test that proves the socket
    options, the drain loop, and the decode agree with each other rather than
    each agreeing with a stub.
    """
    import socket as socket_module

    sock = aec_tune._open_reference_socket("127.0.0.1", 0)
    try:
        target = sock.getsockname()
        period = np.array([[100, -100], [200, -200]], dtype="<i2")
        sender = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
        try:
            sender.sendto(period.tobytes(), target)
            sender.sendto(period.tobytes(), target)
        finally:
            sender.close()

        chunks: list[bytes] = []
        aec_tune._drain_reference_socket(
            sock,
            time.monotonic() + aec_tune.REFERENCE_POLL_SEC * 4,
            chunks,
            threading.Event(),
        )
    finally:
        sock.close()

    assert len(chunks) == 2
    ref_wav = tmp_path / "ref.wav"
    aec_tune._write_reference_wav(ref_wav, chunks)
    samples, rate, _channels = aec_tune._read_wav_int16(ref_wav)
    assert rate == aec_tune.REFERENCE_RATE
    assert samples.tolist() == period.tolist() * 2


def test_drain_returns_quietly_when_the_socket_closes_under_it() -> None:
    """A closed socket ends the drain rather than raising out of the thread."""
    sock = aec_tune._open_reference_socket("127.0.0.1", 0)
    sock.close()
    chunks: list[bytes] = []

    aec_tune._drain_reference_socket(
        sock, time.monotonic() + 5.0, chunks, threading.Event()
    )

    assert chunks == []


def test_the_stop_event_ends_the_drain_without_waiting_out_the_window() -> None:
    """Interrupt and mic-start failure must not sit out the capture window.

    `jasper-aec-tune` is a foreground operator CLI; a Ctrl-C that appears to
    hang for the rest of a five-second capture reads as a wedge.

    The deadline is a few poll intervals out — long enough that only the stop
    event can explain a prompt return, short enough that a guard which stopped
    working fails HERE in seconds rather than being cut off by the suite's
    global hang timeout minutes later.
    """
    window_sec = aec_tune.REFERENCE_POLL_SEC * 6
    sock = aec_tune._open_reference_socket("127.0.0.1", 0)
    stop = threading.Event()
    stop.set()
    chunks: list[bytes] = []
    try:
        started = time.monotonic()
        aec_tune._drain_reference_socket(sock, started + window_sec, chunks, stop)
        elapsed = time.monotonic() - started
    finally:
        sock.close()

    assert elapsed < aec_tune.REFERENCE_POLL_SEC, (
        f"a set stop event must end the drain at once; took {elapsed:.2f}s of "
        f"a {window_sec:.1f}s window"
    )
    assert chunks == []


def test_capture_signals_the_drain_before_joining_it(monkeypatch, tmp_path) -> None:
    """The stop is SET on the way out, on the failure path too.

    A `join` that precedes the signal is the same wait this exists to remove,
    so the order is asserted, not just the call.
    """
    # Socket close is pinned by its own test; this one is about ordering.
    _stub_reference_socket(monkeypatch, payload=b"\x00\x00\x00\x00")
    monkeypatch.setattr(aec_tune, "_drain_reference_socket", lambda *a: None)

    stop_events: list[threading.Event] = []
    set_at_join: list[bool] = []

    class RecordingThread(threading.Thread):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            # The drain's 4th positional arg is the stop event.
            stop_events.append(kw["args"][3])

        def join(self, timeout=None):  # type: ignore[override]
            set_at_join.append(stop_events[0].is_set())
            return super().join(timeout)

    monkeypatch.setattr(aec_tune.threading, "Thread", RecordingThread)
    monkeypatch.setattr(
        aec_tune.subprocess, "Popen", MagicMock(side_effect=OSError("mic failed"))
    )

    with pytest.raises(OSError, match="mic failed"):
        aec_tune._capture_simultaneous(
            1.0, tmp_path / "ref.wav", tmp_path / "mic.wav", "hw:Mic", 2
        )

    assert set_at_join == [True], "the drain must be signalled BEFORE it is joined"


def test_an_unarmed_reference_target_names_the_reconciler(monkeypatch) -> None:
    """An EMPTY target is `jasper-aec-reconcile` saying outputd is not publishing.

    The reconciler is the single writer of the key and leaves it empty on its
    parked branches, so this is a distinct, actionable state — not the same
    thing as "nobody was playing music".
    """
    monkeypatch.setenv(aec_tune.REFERENCE_UDP_TARGET_ENV, "")

    with pytest.raises(aec_tune.TuneError, match="jasper-aec-reconcile"):
        aec_tune._resolve_reference_udp_target()


def test_reference_target_prefers_the_env_files_over_the_shipped_default(
    monkeypatch,
) -> None:
    """The box's own env files win over the default; an explicit shell wins over both."""
    monkeypatch.delenv(aec_tune.REFERENCE_UDP_TARGET_ENV, raising=False)
    monkeypatch.setattr(
        aec_tune,
        "merged_env_files",
        lambda: {aec_tune.REFERENCE_UDP_TARGET_ENV: "10.0.0.9:19191"},
    )
    assert aec_tune._resolve_reference_udp_target() == ("10.0.0.9", 19191)

    monkeypatch.setenv(aec_tune.REFERENCE_UDP_TARGET_ENV, "127.0.0.1:12345")
    assert aec_tune._resolve_reference_udp_target() == ("127.0.0.1", 12345)


def test_reference_target_falls_back_to_the_shipped_default(monkeypatch) -> None:
    monkeypatch.delenv(aec_tune.REFERENCE_UDP_TARGET_ENV, raising=False)
    monkeypatch.setattr(aec_tune, "merged_env_files", dict)

    assert aec_tune._resolve_reference_udp_target() == ("127.0.0.1", 9891)
    assert aec_tune.DEFAULT_REFERENCE_UDP_TARGET == "127.0.0.1:9891"


@pytest.mark.parametrize("raw", ["9891", "127.0.0.1:", "127.0.0.1:nope", ":9891"])
def test_a_malformed_reference_target_is_rejected(monkeypatch, raw: str) -> None:
    monkeypatch.setenv(aec_tune.REFERENCE_UDP_TARGET_ENV, raw)

    with pytest.raises(aec_tune.TuneError):
        aec_tune._resolve_reference_udp_target()


def test_a_silent_reference_monitor_names_outputd(monkeypatch, tmp_path: Path) -> None:
    """Zero datagrams is its own diagnosis, not a generic "capture failed"."""
    sock = _stub_reference_socket(monkeypatch, payload=None)
    mic_proc = MagicMock()
    mic_proc.wait.return_value = 0
    mic_proc.poll.return_value = 0
    monkeypatch.setattr(aec_tune.subprocess, "Popen", MagicMock(return_value=mic_proc))

    with pytest.raises(aec_tune.TuneError, match="no reference datagrams"):
        aec_tune._capture_simultaneous(
            1.0, tmp_path / "ref.wav", tmp_path / "mic.wav", "hw:Mic", 2
        )

    sock.close.assert_called_once_with()


def test_mic_start_failure_still_closes_the_reference_socket(
    monkeypatch, tmp_path: Path
) -> None:
    """The socket and its thread are owned even when the surviving child never starts.

    This is the P7-2 shape of the old two-`arecord` partial-start test: the
    reference leg is opened first, so a mic-start failure must not leak it.
    """
    sock = _stub_reference_socket(monkeypatch, payload=b"\x00\x00\x00\x00")
    monkeypatch.setattr(
        aec_tune.subprocess,
        "Popen",
        MagicMock(side_effect=OSError("mic Popen failed")),
    )

    with pytest.raises(OSError, match="mic Popen failed"):
        aec_tune._capture_simultaneous(
            1.0, tmp_path / "ref.wav", tmp_path / "mic.wav", "hw:Mic", 2
        )

    sock.close.assert_called_once_with()


def test_successful_capture_bounds_and_reaps_the_single_mic_child(
    monkeypatch, tmp_path: Path
) -> None:
    ref_wav = tmp_path / "ref.wav"
    mic_wav = tmp_path / "mic.wav"
    mic_wav.write_bytes(b"m" * 1025)
    sock = _stub_reference_socket(
        monkeypatch, payload=np.zeros((600, 2), dtype="<i2").tobytes()
    )
    mic_proc = MagicMock()
    mic_proc.wait.return_value = 0
    mic_proc.poll.return_value = 0
    popen = MagicMock(return_value=mic_proc)
    monkeypatch.setattr(aec_tune.subprocess, "Popen", popen)

    assert aec_tune._capture_simultaneous(1.0, ref_wav, mic_wav, "hw:Mic", 2)

    # ONE child now — the reference is a socket, not a second arecord.
    assert popen.call_count == 1
    argv = popen.call_args.args[0]
    assert argv[:4] == ["arecord", "-q", "-D", "hw:Mic"]
    expected_capture_timeout = 1 + 1 + aec_tune.PROCESS_EXIT_GRACE_SEC
    assert mic_proc.wait.call_args_list == [
        call(timeout=expected_capture_timeout),
        call(timeout=aec_tune.PROCESS_EXIT_GRACE_SEC),
    ]
    mic_proc.terminate.assert_not_called()
    mic_proc.kill.assert_not_called()
    sock.close.assert_called_once_with()


def test_keyboard_interrupt_reaps_the_mic_child_and_closes_the_socket(
    monkeypatch, tmp_path: Path
) -> None:
    sock = _stub_reference_socket(monkeypatch, payload=b"\x00\x00\x00\x00")
    mic_proc = MagicMock()
    mic_proc.wait.side_effect = [KeyboardInterrupt, 0]
    mic_proc.poll.return_value = None
    monkeypatch.setattr(aec_tune.subprocess, "Popen", MagicMock(return_value=mic_proc))

    with pytest.raises(KeyboardInterrupt):
        aec_tune._capture_simultaneous(
            1.0, tmp_path / "ref.wav", tmp_path / "mic.wav", "hw:Mic", 2
        )

    mic_proc.terminate.assert_called_once_with()
    assert mic_proc.wait.call_count == 2
    sock.close.assert_called_once_with()


def test_timed_out_aplay_is_terminated_killed_reaped_and_volume_restored(
    monkeypatch, tmp_path: Path
) -> None:
    _prepare_main(monkeypatch, tmp_path, 1, 0)
    set_volume = MagicMock()
    monkeypatch.setattr(aec_tune, "_camilla_set_volume", set_volume)
    play_proc = MagicMock()
    play_proc.poll.return_value = None
    play_proc.wait.side_effect = [
        subprocess.TimeoutExpired("aplay", 1),
        subprocess.TimeoutExpired("aplay", 1),
        0,
    ]
    monkeypatch.setattr(aec_tune.subprocess, "Popen", MagicMock(return_value=play_proc))
    sys.argv.append("--inject-noise")

    assert aec_tune.main() == 1
    play_proc.terminate.assert_called_once_with()
    play_proc.kill.assert_called_once_with()
    assert play_proc.wait.call_count == 3
    assert set_volume.call_args_list[-1] == call(0.0)
