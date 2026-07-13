# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import logging
import os
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from jasper.audio_measurement import playback
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.correction import playback as correction_playback


class _ExitedProcess:
    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        return self.returncode


def _artifact_identity(
    path: Path,
    *,
    relative_path: str | None = None,
    byte_size: int | None = None,
) -> ArtifactIdentity:
    raw = path.read_bytes()
    return ArtifactIdentity(
        bundle_kind="test_measurement",
        bundle_id="session-1",
        relative_path=relative_path or path.name,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_size=len(raw) if byte_size is None else byte_size,
    )


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
async def test_verified_wav_uses_same_open_content_bound_fd_after_path_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_path = tmp_path / "stimulus.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(b"\0\0" * 8_000)
    artifact = _artifact_identity(wav_path)
    calls = []

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        inherited_fd = kwargs["pass_fds"][0]
        assert os.pread(inherited_fd, 4, 0) == b"RIFF"
        return _ExitedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    async with playback.verified_wav_source(tmp_path, artifact) as source:
        source_fd = source.fd
        wav_path.unlink()
        result = await playback.play_verified_wav(
            source,
            alsa_device="test_pcm",
            timeout_s=2.0,
        )

    assert calls[0][0][-1] == f"/proc/self/fd/{source_fd}"
    assert calls[0][1]["pass_fds"] == (source_fd,)
    assert result.wav_path == wav_path


@pytest.mark.asyncio
async def test_verified_wav_emits_immutable_snapshot_despite_in_place_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_path = tmp_path / "stimulus.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(b"\0\0" * 8_000)
    artifact = _artifact_identity(wav_path)

    async def create(*_args, **kwargs):
        inherited_fd = kwargs["pass_fds"][0]
        mutated = bytearray(wav_path.read_bytes())
        mutated[-1] ^= 0x01
        wav_path.write_bytes(mutated)
        emitted = os.pread(inherited_fd, artifact.byte_size, 0)
        assert hashlib.sha256(emitted).hexdigest() == artifact.sha256
        assert hashlib.sha256(wav_path.read_bytes()).hexdigest() != artifact.sha256
        return _ExitedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    async with playback.verified_wav_source(tmp_path, artifact) as source:
        with pytest.raises(OSError) as immutable_error:
            os.pwrite(source.fd, b"x", artifact.byte_size - 1)
        assert immutable_error.value.errno in {errno.EBADF, errno.EPERM}
        await playback.play_verified_wav(
            source,
            alsa_device="test_pcm",
            timeout_s=2.0,
        )


@pytest.mark.asyncio
async def test_verified_wav_refuses_changed_malformed_symlink_and_oversized_sources(
    tmp_path: Path,
) -> None:
    changed = tmp_path / "changed.wav"
    with wave.open(str(changed), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(b"\0\0" * 8_000)
    changed_identity = _artifact_identity(changed)
    changed.write_bytes(changed.read_bytes()[:-1] + b"x")
    with pytest.raises(playback.WavSourceError) as changed_error:
        async with playback.verified_wav_source(tmp_path, changed_identity):
            pass
    assert changed_error.value.code is playback.WavSourceFailureCode.CONTENT_MISMATCH

    malformed = tmp_path / "malformed.wav"
    malformed.write_bytes(b"not-wave")
    with pytest.raises(playback.WavSourceError) as malformed_error:
        async with playback.verified_wav_source(
            tmp_path,
            _artifact_identity(malformed),
        ):
            pass
    assert malformed_error.value.code is playback.WavSourceFailureCode.INVALID_WAV

    target = tmp_path / "target.wav"
    target.write_bytes(changed_identity.byte_size * b"\0")
    link = tmp_path / "linked.wav"
    link.symlink_to(target.name)
    link_identity = ArtifactIdentity(
        bundle_kind="test_measurement",
        bundle_id="session-1",
        relative_path=link.name,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        byte_size=target.stat().st_size,
    )
    with pytest.raises(playback.WavSourceError) as link_error:
        async with playback.verified_wav_source(tmp_path, link_identity):
            pass
    assert link_error.value.code is playback.WavSourceFailureCode.UNSAFE_PATH

    oversized = _artifact_identity(
        malformed,
        byte_size=playback.MAX_VERIFIED_WAV_BYTES + 1,
    )
    with pytest.raises(playback.WavSourceError) as oversized_error:
        async with playback.verified_wav_source(tmp_path, oversized):
            pass
    assert oversized_error.value.code is playback.WavSourceFailureCode.RESOURCE_LIMIT


@pytest.mark.asyncio
async def test_verified_wav_open_drains_cancellation_and_closes_late_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wav_path = tmp_path / "stimulus.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(b"\0\0" * 8_000)
    artifact = _artifact_identity(wav_path)
    original_open = playback._open_verified_wav_source
    started = threading.Event()
    release = threading.Event()
    opened = []

    def delayed_open(*args, **kwargs):
        source = original_open(*args, **kwargs)
        opened.append(source)
        started.set()
        release.wait(timeout=5)
        return source

    async def consume() -> None:
        async with playback.verified_wav_source(tmp_path, artifact):
            raise AssertionError("cancelled open yielded a source")

    monkeypatch.setattr(playback, "_open_verified_wav_source", delayed_open)
    task = asyncio.create_task(consume())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert opened[0].closed is True


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
    assert (
        inspect.signature(correction_playback.play_sweep)
        .parameters["alsa_device"]
        .default
        == "correction_substream"
    )
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
