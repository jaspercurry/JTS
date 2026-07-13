# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jasper.audio_measurement import playback
from jasper.correction import playback as correction_playback


class _ExitedProcess:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_play_wav_uses_stable_argv_and_returns_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")
    calls = []

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return _ExitedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    caplog.set_level(logging.INFO, logger=playback.__name__)

    result = await playback.play_wav(
        wav_path,
        alsa_device="test_pcm",
        timeout_s=2.0,
    )

    assert calls[0][0] == (
        "aplay",
        "-D",
        "test_pcm",
        "-q",
        str(wav_path),
    )
    assert calls[0][1]["stdout"] is asyncio.subprocess.DEVNULL
    assert result == playback.PlaybackResult(
        wav_path=wav_path,
        alsa_device="test_pcm",
        returncode=0,
    )
    assert "event=audio_measurement.playback" in caplog.text
    assert "result=completed" in caplog.text


@pytest.mark.asyncio
async def test_play_wav_timeout_is_typed_and_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")

    class Process:
        stderr = None

        def __init__(self) -> None:
            self.returncode = None
            self.killed = False
            self.terminated = asyncio.Event()

        async def wait(self):
            await self.terminated.wait()
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9
            self.terminated.set()

    process = Process()

    async def create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(playback.PlaybackError) as caught:
        await playback.play_wav(
            wav_path,
            alsa_device="test_pcm",
            timeout_s=0.001,
        )

    assert caught.value.code is playback.PlaybackFailureCode.TIMEOUT
    assert caught.value.cleanup_state is (
        playback.PlaybackCleanupState.KILLED_AND_REAPED
    )
    assert process.killed is True


@pytest.mark.asyncio
async def test_play_wav_unconfirmed_cleanup_is_bounded_and_observable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")

    class Process:
        stderr = None
        returncode = None

        def __init__(self) -> None:
            self.never_exits = asyncio.Event()
            self.killed = False

        async def wait(self):
            await self.never_exits.wait()

        def kill(self):
            self.killed = True

    process = Process()

    async def create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(playback, "_PROCESS_CLEANUP_TIMEOUT_S", 0.01)
    caplog.set_level(logging.WARNING, logger=playback.__name__)

    with pytest.raises(playback.PlaybackError) as caught:
        await asyncio.wait_for(
            playback.play_wav(
                wav_path,
                alsa_device="test_pcm",
                timeout_s=0.001,
            ),
            timeout=0.2,
        )

    assert caught.value.code is playback.PlaybackFailureCode.TIMEOUT
    assert caught.value.cleanup_state is (
        playback.PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED
    )
    assert process.killed is True
    assert "cleanup_state=kill_sent_reap_unconfirmed" in caplog.text


@pytest.mark.asyncio
async def test_process_wait_failure_is_not_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")

    class Process:
        stderr = None
        returncode = None

        async def wait(self):
            raise RuntimeError("wait backend broke")

        def kill(self):
            self.returncode = -9

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(playback.PlaybackError) as caught:
        await playback.play_wav(
            wav_path,
            alsa_device="test_pcm",
            timeout_s=1.0,
        )

    assert caught.value.code is playback.PlaybackFailureCode.WAIT_FAILED
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "wait backend broke"


@pytest.mark.asyncio
async def test_play_wav_nonzero_diagnostic_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")

    async def create(*_args, **kwargs):
        assert kwargs["stderr"] is asyncio.subprocess.PIPE
        return _ExitedProcess(returncode=7, stderr=b"x" * 20_000 + b"TAIL")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(playback.PlaybackError) as caught:
        await playback.play_wav(
            wav_path,
            alsa_device="test_pcm",
            timeout_s=1.0,
        )

    error = caught.value
    assert error.code is playback.PlaybackFailureCode.PROCESS_FAILED
    assert error.returncode == 7
    assert error.diagnostic_tail.endswith("TAIL")
    assert len(error.diagnostic_tail.encode()) <= playback._DIAGNOSTIC_TAIL_BYTES


@pytest.mark.asyncio
async def test_play_wav_startup_failure_is_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")

    async def create(*_args, **_kwargs):
        raise FileNotFoundError("aplay missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    caplog.set_level(logging.WARNING, logger=playback.__name__)

    with pytest.raises(playback.PlaybackError) as caught:
        await playback.play_wav(
            wav_path,
            alsa_device="test_pcm",
            timeout_s=1.0,
        )

    assert caught.value.code is playback.PlaybackFailureCode.START_FAILED
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    assert "failure_code=start_failed" in caplog.text


@pytest.mark.asyncio
async def test_play_wav_refuses_missing_file_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def create(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(playback.PlaybackError) as caught:
        await playback.play_wav(
            tmp_path / "missing.wav",
            alsa_device="test_pcm",
            timeout_s=1.0,
        )

    assert caught.value.code is playback.PlaybackFailureCode.MISSING_FILE
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("alsa_device", ["", "  "])
async def test_play_wav_rejects_empty_device(
    tmp_path: Path,
    alsa_device: str,
) -> None:
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")

    with pytest.raises(playback.PlaybackError) as caught:
        await playback.play_wav(
            wav_path,
            alsa_device=alsa_device,
            timeout_s=1.0,
        )
    assert caught.value.code is playback.PlaybackFailureCode.INVALID_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_s", [0.0, -1.0, float("inf"), float("nan")])
async def test_play_wav_rejects_invalid_timeout(
    tmp_path: Path,
    timeout_s: float,
) -> None:
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")

    with pytest.raises(playback.PlaybackError) as caught:
        await playback.play_wav(
            wav_path,
            alsa_device="test_pcm",
            timeout_s=timeout_s,
        )
    assert caught.value.code is playback.PlaybackFailureCode.INVALID_REQUEST


def test_tone_generation_preserves_legacy_filename_and_bytes(tmp_path: Path) -> None:
    path = playback.ensure_sine_wav(
        freq_hz=1000.0,
        duration_s=1.0,
        dbfs=-12.0,
        sample_rate=48000,
        cache_dir=tmp_path,
    )

    assert path.name == "tone_1000Hz_1000ms_120dbm_48000Hz.wav"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "646aa0c55bb926e0e8801a519d45a1b6a50a19d70df86058fe351c2167a84c5b"
    )


def test_chunked_generation_preserves_current_15_second_consumer_bytes(
    tmp_path: Path,
) -> None:
    path = playback.ensure_sine_wav(
        freq_hz=1000.0,
        duration_s=15.0,
        dbfs=-12.0,
        sample_rate=48000,
        cache_dir=tmp_path,
    )

    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "9b85c6a2a3bea0021112d112544e3cfe05683b654ba889de118d248a1cf665d4"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("freq_hz", 0.0),
        ("freq_hz", float("inf")),
        ("duration_s", 0.0),
        ("duration_s", float("nan")),
        ("duration_s", 90.001),
        ("duration_s", 100.0),
        ("dbfs", 0.1),
        ("sample_rate", True),
        ("sample_rate", 0),
        ("sample_rate", playback.MAX_TONE_SAMPLE_RATE + 1),
    ],
)
def test_tone_validation_precedes_cache_creation(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    cache_dir = tmp_path / "not-created"
    kwargs: dict[str, object] = {
        "freq_hz": 1000.0,
        "duration_s": 1.0,
        "dbfs": -12.0,
        "sample_rate": 48000,
        "cache_dir": cache_dir,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        playback.ensure_sine_wav(**kwargs)  # type: ignore[arg-type]
    assert cache_dir.exists() is False


def test_finer_parameters_receive_collision_free_cache_keys(tmp_path: Path) -> None:
    first = playback.ensure_sine_wav(
        freq_hz=1000.1,
        duration_s=0.0105,
        dbfs=-12.05,
        sample_rate=48000,
        cache_dir=tmp_path,
    )
    second = playback.ensure_sine_wav(
        freq_hz=1000.2,
        duration_s=0.0105,
        dbfs=-12.05,
        sample_rate=48000,
        cache_dir=tmp_path,
    )

    assert first != second
    assert first.name.startswith("tone_exact_")
    assert second.name.startswith("tone_exact_")
    assert first.read_bytes() != second.read_bytes()


def test_tone_rejects_nyquist_frequency_before_cache_creation(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "not-created"

    with pytest.raises(ValueError, match="Nyquist"):
        playback.ensure_sine_wav(
            freq_hz=24000.0,
            duration_s=1.0,
            dbfs=-12.0,
            sample_rate=48000,
            cache_dir=cache_dir,
        )
    assert cache_dir.exists() is False


def test_tone_duration_is_bounded_even_at_a_low_sample_rate(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "not-created"

    with pytest.raises(ValueError, match="at most 90"):
        playback.ensure_sine_wav(
            freq_hz=100.0,
            duration_s=100.0,
            dbfs=-12.0,
            sample_rate=8000,
            cache_dir=cache_dir,
        )
    assert cache_dir.exists() is False


def test_partial_cached_tone_is_replaced_atomically(tmp_path: Path) -> None:
    path = tmp_path / "tone_1000Hz_1000ms_120dbm_48000Hz.wav"
    path.write_bytes(b"partial")

    rebuilt = playback.ensure_sine_wav(
        freq_hz=1000.0,
        duration_s=1.0,
        dbfs=-12.0,
        sample_rate=48000,
        cache_dir=tmp_path,
    )

    with wave.open(str(rebuilt), "rb") as generated:
        assert generated.getnframes() == 48000
        assert generated.getframerate() == 48000
    assert not list(tmp_path.glob(".*.tmp"))


def test_generation_failure_keeps_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "tone_1000Hz_1000ms_120dbm_48000Hz.wav"
    path.write_bytes(b"partial")

    def fail_write(self, _data):
        raise OSError("disk full")

    monkeypatch.setattr(wave.Wave_write, "writeframesraw", fail_write)

    with pytest.raises(OSError, match="disk full"):
        playback.ensure_sine_wav(
            freq_hz=1000.0,
            duration_s=1.0,
            dbfs=-12.0,
            sample_rate=48000,
            cache_dir=tmp_path,
        )
    assert path.read_bytes() == b"partial"
    assert not list(tmp_path.glob(".*.tmp"))


def test_concurrent_generation_leaves_one_valid_deterministic_wav(
    tmp_path: Path,
) -> None:
    def generate() -> Path:
        return playback.ensure_sine_wav(
            freq_hz=777.0,
            duration_s=0.1,
            dbfs=-18.0,
            sample_rate=16000,
            cache_dir=tmp_path,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda _index: generate(), range(2)))

    assert paths[0] == paths[1]
    assert hashlib.sha256(paths[0].read_bytes()).hexdigest() == (
        "f30ebddf1245b4bd79b605d9fe84d64eeda3ff81f40273c0292278130be355d4"
    )
    assert not list(tmp_path.glob(".*.tmp"))


def test_neutral_surface_requires_owner_policy() -> None:
    play_signature = inspect.signature(playback.play_wav)
    tone_signature = inspect.signature(playback.ensure_sine_wav)

    assert play_signature.parameters["alsa_device"].default is inspect.Parameter.empty
    assert tone_signature.parameters["cache_dir"].default is inspect.Parameter.empty
    assert not hasattr(playback, "DEFAULT_ALSA_DEVICE")
    assert not hasattr(playback, "DEFAULT_TONE_DIR")


def test_correction_compatibility_surface_preserves_types_and_defaults() -> None:
    assert correction_playback.SweepPlaybackError is playback.SweepPlaybackError
    assert correction_playback.PlaybackError is playback.PlaybackError
    assert correction_playback.PlaybackFailureCode is playback.PlaybackFailureCode
    assert issubclass(correction_playback.TonePlayer, playback.TonePlayer)
    assert inspect.signature(correction_playback.play_sweep).parameters[
        "alsa_device"
    ].default == "correction_substream"
    assert inspect.signature(correction_playback._ensure_tone_wav).parameters[
        "cache_dir"
    ].default == Path("/var/lib/jasper/correction/tones")


@pytest.mark.asyncio
async def test_correction_wrapper_preserves_missing_file_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError, match="sweep WAV not found"):
        await correction_playback.play_sweep(tmp_path / "missing.wav")


@pytest.mark.asyncio
async def test_correction_wrapper_preserves_startup_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_path = tmp_path / "sweep.wav"
    wav_path.write_bytes(b"RIFF")

    async def create(*_args, **_kwargs):
        raise FileNotFoundError("aplay missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(FileNotFoundError, match="aplay missing"):
        await correction_playback.play_sweep(wav_path)


@pytest.mark.asyncio
async def test_continuous_tone_nonzero_exit_is_typed_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_path = tmp_path / "tone.wav"
    wav_path.write_bytes(b"RIFF")

    async def create(*_args, **kwargs):
        assert kwargs["stderr"] is asyncio.subprocess.PIPE
        return _ExitedProcess(returncode=2, stderr=b"driver unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)

    with pytest.raises(playback.PlaybackError) as caught:
        await playback.TonePlayer(wav_path, alsa_device="test_pcm").play()

    assert caught.value.code is playback.PlaybackFailureCode.PROCESS_FAILED
    assert caught.value.diagnostic_tail == "driver unavailable"


@pytest.mark.asyncio
async def test_continuous_tone_startup_failure_is_typed_and_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wav_path = tmp_path / "tone.wav"
    wav_path.write_bytes(b"RIFF")

    async def create(*_args, **_kwargs):
        raise OSError("no aplay")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    caplog.set_level(logging.INFO, logger=playback.__name__)

    with pytest.raises(playback.PlaybackError) as caught:
        await playback.TonePlayer(wav_path, alsa_device="test_pcm").play()

    assert caught.value.code is playback.PlaybackFailureCode.START_FAILED
    assert "failure_code=start_failed" in caplog.text


@pytest.mark.asyncio
async def test_continuous_tone_cancel_reaps_and_logs_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wav_path = tmp_path / "tone.wav"
    wav_path.write_bytes(b"RIFF")
    wait_started = asyncio.Event()
    killed = asyncio.Event()

    class Process:
        stderr = None
        returncode = None

        async def wait(self):
            wait_started.set()
            await killed.wait()
            return self.returncode

        def kill(self):
            self.returncode = -9
            killed.set()

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    caplog.set_level(logging.INFO, logger=playback.__name__)
    player = playback.TonePlayer(wav_path, alsa_device="test_pcm")
    task = asyncio.create_task(player.play())
    await wait_started.wait()

    player.cancel()
    await task

    assert player.cancelled is True
    assert "result=started" in caplog.text
    assert "result=cancelled" in caplog.text
    assert "cleanup_state=killed_and_reaped" in caplog.text


@pytest.mark.asyncio
async def test_continuous_tone_unconfirmed_cancel_cleanup_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_path = tmp_path / "tone.wav"
    wav_path.write_bytes(b"RIFF")
    wait_started = asyncio.Event()

    class Process:
        stderr = None
        returncode = None

        def __init__(self) -> None:
            self.never_exits = asyncio.Event()

        async def wait(self):
            wait_started.set()
            await self.never_exits.wait()

        def kill(self):
            pass

    async def create(*_args, **_kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(playback, "_PROCESS_CLEANUP_TIMEOUT_S", 0.01)
    player = playback.TonePlayer(wav_path, alsa_device="test_pcm")
    task = asyncio.create_task(player.play())
    await wait_started.wait()

    player.cancel()
    with pytest.raises(playback.PlaybackError) as caught:
        await asyncio.wait_for(task, timeout=0.2)

    assert caught.value.code is playback.PlaybackFailureCode.CLEANUP_FAILED
    assert caught.value.cleanup_state is (
        playback.PlaybackCleanupState.KILL_SENT_REAP_UNCONFIRMED
    )


def test_shared_playback_holds_no_powerful_host_reference() -> None:
    source = inspect.getsource(playback)
    assert "jasper.camilla" not in source
    assert "CamillaController" not in source
