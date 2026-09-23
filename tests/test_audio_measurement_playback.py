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
import subprocess
import sys
import threading
import wave
from pathlib import Path

import pytest

from jasper.audio_measurement import playback
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from tests._log_events import event_fields, event_records

from ._async_wait import wait_signalled


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
    fields = event_fields(caplog, "audio_measurement.playback")
    assert fields["result"] == "completed"


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


async def test_verified_wav_open_cancellation_survives_late_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
    original_close = playback._VerifiedWavSource.close

    def delayed_open(*args, **kwargs):
        source = original_open(*args, **kwargs)
        opened.append(source)
        started.set()
        release.wait(timeout=5)
        return source

    async def consume() -> None:
        async with playback.verified_wav_source(tmp_path, artifact):
            raise AssertionError("cancelled open yielded a source")

    def close_then_fail(source):
        original_close(source)
        raise OSError("late snapshot close failed")

    monkeypatch.setattr(playback, "_open_verified_wav_source", delayed_open)
    monkeypatch.setattr(playback._VerifiedWavSource, "close", close_then_fail)
    caplog.set_level(logging.INFO, logger=playback.__name__)
    task = asyncio.create_task(consume())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False
    release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert opened[0].closed is True
    assert any(
        "suppressed verified WAV cleanup failure" in note
        for note in caught.value.__notes__
    )
    fields = event_fields(caplog, "audio_measurement.verified_wav_source")
    assert fields["result"] == "cleanup_failed"


async def test_verified_wav_close_failure_preserves_active_body_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wav_path = tmp_path / "stimulus.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8_000)
        wav.writeframes(b"\0\0" * 8_000)
    artifact = _artifact_identity(wav_path)
    original_close = playback._VerifiedWavSource.close
    primary = RuntimeError("primary body failure")

    def close_then_fail(source):
        original_close(source)
        raise OSError("snapshot close failed")

    monkeypatch.setattr(playback._VerifiedWavSource, "close", close_then_fail)
    caplog.set_level(logging.INFO, logger=playback.__name__)

    with pytest.raises(RuntimeError) as caught:
        async with playback.verified_wav_source(tmp_path, artifact):
            raise primary

    assert caught.value is primary
    assert any(
        "suppressed verified WAV cleanup failure" in note
        for note in caught.value.__notes__
    )
    fields = event_fields(caplog, "audio_measurement.verified_wav_source")
    assert fields["result"] == "cleanup_failed"


async def test_verified_wav_close_failure_is_typed_without_primary_error(
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
    original_close = playback._VerifiedWavSource.close

    def close_then_fail(source):
        original_close(source)
        raise OSError("snapshot close failed")

    monkeypatch.setattr(playback._VerifiedWavSource, "close", close_then_fail)

    ambient = RuntimeError("ambient caller error")
    try:
        raise ambient
    except RuntimeError as handled:
        with pytest.raises(playback.WavSourceError) as caught:
            async with playback.verified_wav_source(tmp_path, artifact):
                pass
        assert handled is ambient
        assert not hasattr(handled, "__notes__")

    assert caught.value.code is playback.WavSourceFailureCode.CLEANUP_FAILED
    assert isinstance(caught.value.__cause__, OSError)


async def test_verified_wav_internal_parse_cleanup_preserves_invalid_wav(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wav_path = tmp_path / "malformed.wav"
    wav_path.write_bytes(b"not a wave")
    artifact = _artifact_identity(wav_path)
    original_dup = playback.os.dup
    original_close = playback.os.close
    duplicate_fds: set[int] = set()
    failed = False

    def observed_dup(fd):
        duplicate = original_dup(fd)
        duplicate_fds.add(duplicate)
        return duplicate

    def close_then_fail(fd):
        nonlocal failed
        original_close(fd)
        if fd in duplicate_fds and not failed:
            failed = True
            raise OSError("parse duplicate close failed")

    monkeypatch.setattr(playback.os, "dup", observed_dup)
    monkeypatch.setattr(playback.os, "close", close_then_fail)
    caplog.set_level(logging.INFO, logger=playback.__name__)

    with pytest.raises(playback.WavSourceError) as caught:
        async with playback.verified_wav_source(tmp_path, artifact):
            pass

    assert failed is True
    assert caught.value.code is playback.WavSourceFailureCode.INVALID_WAV
    assert any(
        "suppressed verified WAV cleanup failure" in note
        for note in caught.value.__notes__
    )
    assert event_records(caplog, "audio_measurement.verified_wav_source")


async def test_verified_wav_directory_close_failure_closes_open_file(
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
    original_open = playback.os.open
    original_close = playback.os.close
    directory_fds: set[int] = set()
    file_fds: list[int] = []
    failed = False

    def observed_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if flags & os.O_DIRECTORY:
            directory_fds.add(fd)
        else:
            file_fds.append(fd)
        return fd

    def close_then_fail(fd):
        nonlocal failed
        original_close(fd)
        if fd in directory_fds and not failed:
            failed = True
            raise OSError("directory close failed")

    monkeypatch.setattr(playback.os, "open", observed_open)
    monkeypatch.setattr(playback.os, "close", close_then_fail)

    with pytest.raises(playback.WavSourceError) as caught:
        async with playback.verified_wav_source(tmp_path, artifact):
            pass

    assert failed is True
    assert caught.value.code is playback.WavSourceFailureCode.CLEANUP_FAILED
    assert file_fds
    with pytest.raises(OSError):
        os.fstat(file_fds[-1])


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
    fields = event_fields(caplog, "audio_measurement.playback")
    assert fields["cleanup_state"] == "kill_sent_reap_unconfirmed"


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
    fields = event_fields(caplog, "audio_measurement.playback")
    assert fields["failure_code"] == "start_failed"


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


def test_neutral_surface_requires_owner_policy() -> None:
    play_signature = inspect.signature(playback.play_wav)

    assert play_signature.parameters["alsa_device"].default is inspect.Parameter.empty
    assert not hasattr(playback, "DEFAULT_ALSA_DEVICE")


def test_shared_playback_holds_no_powerful_host_reference() -> None:
    # The import graph, in a fresh interpreter: this module must never be the
    # thing that drags the DSP controller into a measurement process.
    probe = (
        "import sys, jasper.audio_measurement.playback;"
        "print('jasper.camilla' in sys.modules)"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", probe], text=True, stderr=subprocess.STDOUT
    )
    assert out.strip() == "False"


@pytest.mark.parametrize("reaped", [True, False])
async def test_wav_cancel_reports_observed_child_cleanup(tmp_path, monkeypatch, reaped):
    started, stopped = asyncio.Event(), asyncio.Event()
    wav = tmp_path / "stimulus.wav"
    wav.write_bytes(b"RIFF")

    class Process:
        stderr = None
        returncode = None

        async def wait(self):
            started.set()
            await stopped.wait()
            return self.returncode

        def kill(self):
            if reaped:
                self.returncode = -9
                stopped.set()

    async def spawn(*args, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(playback, "_PROCESS_CLEANUP_TIMEOUT_S", 0.01)
    task = asyncio.create_task(playback.play_wav(wav, alsa_device="null", timeout_s=10))
    await wait_signalled(started, "process wait() started", producer=task)
    task.cancel()
    with pytest.raises(playback.WavPlaybackCancelled) as error:
        await task
    assert error.value.observation.as_dict() == {
        "emission": "possible", "failure_code": None,
        "cleanup_state": "killed_and_reaped" if reaped else "kill_sent_reap_unconfirmed",
        "returncode": -9 if reaped else None,
    }
