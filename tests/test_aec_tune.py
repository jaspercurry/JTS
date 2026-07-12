# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free capture-profile contracts for jasper-aec-tune."""

import logging
import math
from pathlib import Path
import subprocess
import sys
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
    stop = MagicMock(return_value=True)
    restart = MagicMock(return_value=True)
    monkeypatch.setattr(aec_tune, "_stop_service_if_running", stop)
    monkeypatch.setattr(aec_tune, "_restart_service", restart)
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
    ("recorded_channels", "channel_index", "message"),
    [
        (1, -1, "invalid for the recorded mono WAV; only channel 0 is available"),
        (1, 1, "invalid for the recorded mono WAV; only channel 0 is available"),
        (2, 2, "invalid for the recorded 2-channel WAV; choose 0 through 1"),
    ],
)
def test_invalid_mic_channel_is_rejected_from_recorded_wav_and_voice_recovers(
    monkeypatch,
    tmp_path: Path,
    caplog,
    recorded_channels: int,
    channel_index: int,
    message: str,
) -> None:
    detector, restart = _prepare_main(
        monkeypatch, tmp_path, recorded_channels, channel_index
    )

    with caplog.at_level(logging.ERROR, logger="jasper.aec_tune"):
        assert aec_tune.main() == 1

    assert message in caplog.text
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
    device.read.return_value = (12,)
    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=device))
    sys.argv.append("--apply")

    assert aec_tune.main() == 0

    device.write.assert_called_once_with("AUDIO_MGR_SYS_DELAY", [12])
    device.read.assert_called_once_with("AUDIO_MGR_SYS_DELAY")
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
    device.read.return_value = (lag,)
    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=device))

    assert aec_tune._apply_volatile_delay(lag, aec_tune.MIN_APPLY_CONFIDENCE)
    device.write.assert_called_once_with("AUDIO_MGR_SYS_DELAY", [lag])
    device.close.assert_called_once_with()


def test_apply_fails_closed_when_device_is_missing(monkeypatch) -> None:
    from jasper.xvf import xvf_host

    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=None))

    assert aec_tune._apply_volatile_delay(12, 0.5) is False


def test_apply_fails_closed_on_readback_mismatch_and_closes_device(
    monkeypatch,
) -> None:
    from jasper.xvf import xvf_host

    device = MagicMock()
    device.read.return_value = (13,)
    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=device))

    assert aec_tune._apply_volatile_delay(12, 0.5) is False
    device.write.assert_called_once_with("AUDIO_MGR_SYS_DELAY", [12])
    device.close.assert_called_once_with()


def test_apply_fails_closed_on_write_error_and_closes_device(monkeypatch) -> None:
    from jasper.xvf import xvf_host

    device = MagicMock()
    device.write.side_effect = OSError("USB write failed")
    monkeypatch.setattr(xvf_host, "find", MagicMock(return_value=device))

    assert aec_tune._apply_volatile_delay(12, 0.5) is False
    device.read.assert_not_called()
    device.close.assert_called_once_with()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_active_mode_rejects_invalid_duck_before_runtime_side_effects(
    monkeypatch, tmp_path: Path, value: str
) -> None:
    _use_asound_root(monkeypatch, tmp_path)
    get_volume = MagicMock()
    stop = MagicMock()
    monkeypatch.setattr(aec_tune, "_camilla_get_volume", get_volume)
    monkeypatch.setattr(aec_tune, "_stop_service_if_running", stop)
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
    monkeypatch.setattr(aec_tune, "_stop_service_if_running", stop)
    monkeypatch.setattr(aec_tune.subprocess, "Popen", popen)
    monkeypatch.setattr(sys, "argv", ["jasper-aec-tune", "--inject-noise"])

    assert aec_tune.main() == 1
    stop.assert_not_called()
    popen.assert_not_called()


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

    with pytest.raises(aec_tune.CamillaVolumeError, match="not finite"):
        aec_tune._camilla_set_volume(-20.0)

    volume.set_main_volume.assert_called_once_with(-20.0)
    client.disconnect.assert_called_once_with()


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


def test_restart_failure_overrides_successful_diagnostic(
    monkeypatch, tmp_path: Path
) -> None:
    _detector, restart = _prepare_main(monkeypatch, tmp_path, 1, 0)
    restart.return_value = False
    monkeypatch.setattr(aec_tune, "_correlate_and_find_lag", lambda mic, ref: (12, 0.5))

    assert aec_tune.main() == 1


def test_partial_arecord_start_terminates_and_reaps_first_child(
    monkeypatch, tmp_path: Path
) -> None:
    ref_proc = MagicMock()
    ref_proc.poll.return_value = None
    ref_proc.wait.return_value = 0
    monkeypatch.setattr(
        aec_tune.subprocess,
        "Popen",
        MagicMock(side_effect=[ref_proc, OSError("mic Popen failed")]),
    )

    with pytest.raises(OSError, match="mic Popen failed"):
        aec_tune._capture_simultaneous(
            1.0,
            tmp_path / "ref.wav",
            tmp_path / "mic.wav",
            "hw:Mic",
            2,
        )

    ref_proc.terminate.assert_called_once_with()
    ref_proc.wait.assert_called_once_with(timeout=aec_tune.PROCESS_EXIT_GRACE_SEC)


def test_successful_arecord_children_are_bounded_and_reaped(
    monkeypatch, tmp_path: Path
) -> None:
    ref_wav = tmp_path / "ref.wav"
    mic_wav = tmp_path / "mic.wav"
    ref_wav.write_bytes(b"r" * 1025)
    mic_wav.write_bytes(b"m" * 1025)
    ref_proc = MagicMock()
    mic_proc = MagicMock()
    for proc in (ref_proc, mic_proc):
        proc.wait.return_value = 0
        proc.poll.return_value = 0
    monkeypatch.setattr(
        aec_tune.subprocess,
        "Popen",
        MagicMock(side_effect=[ref_proc, mic_proc]),
    )

    assert aec_tune._capture_simultaneous(1.0, ref_wav, mic_wav, "hw:Mic", 2)

    expected_capture_timeout = 1 + 1 + aec_tune.PROCESS_EXIT_GRACE_SEC
    for proc in (ref_proc, mic_proc):
        assert proc.wait.call_args_list == [
            call(timeout=expected_capture_timeout),
            call(timeout=aec_tune.PROCESS_EXIT_GRACE_SEC),
        ]
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()


def test_keyboard_interrupt_reaps_both_arecord_children(
    monkeypatch, tmp_path: Path
) -> None:
    ref_proc = MagicMock()
    mic_proc = MagicMock()
    ref_proc.wait.side_effect = [KeyboardInterrupt, 0]
    ref_proc.poll.return_value = None
    mic_proc.poll.return_value = None
    mic_proc.wait.return_value = 0
    monkeypatch.setattr(
        aec_tune.subprocess,
        "Popen",
        MagicMock(side_effect=[ref_proc, mic_proc]),
    )

    with pytest.raises(KeyboardInterrupt):
        aec_tune._capture_simultaneous(
            1.0,
            tmp_path / "ref.wav",
            tmp_path / "mic.wav",
            "hw:Mic",
            2,
        )

    mic_proc.terminate.assert_called_once_with()
    ref_proc.terminate.assert_called_once_with()
    assert ref_proc.wait.call_count == 2
    mic_proc.wait.assert_called_once_with(timeout=aec_tune.PROCESS_EXIT_GRACE_SEC)


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
