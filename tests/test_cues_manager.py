from __future__ import annotations

import asyncio
import os
import wave

import pytest

from jasper.cues import AudioCueManager, CUES
from jasper.cues.generator import (
    TTSResult,
    WAV_CHANNELS,
    WAV_RATE,
    WAV_SAMPLE_WIDTH,
    cue_filename,
    cue_hash,
    cue_path,
)
from jasper.cues.registry import find


# --- Fakes ---


class _FakeBackend:
    def __init__(self, samples_24k: int = 240) -> None:
        self._pcm = b"\x00\x00" * samples_24k
        self.calls: list[str] = []

    def synthesise(self, text: str) -> TTSResult:
        self.calls.append(text)
        return TTSResult(pcm_24k=self._pcm)


class _FakeTtsPlayout:
    """Captures bytes that would be played back. Mirrors TtsPlayout's
    `async def write(pcm: bytes)` shape — that's the only method
    AudioCueManager touches."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.fail_with: Exception | None = None

    async def write(self, pcm: bytes) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.writes.append(pcm)


def _hand_write_wav(path: str, pcm_24k: bytes) -> None:
    """Helper: write a valid 24kHz mono 16-bit WAV at `path` (matches
    the format the generator now writes)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as f:
        f.setnchannels(WAV_CHANNELS)
        f.setsampwidth(WAV_SAMPLE_WIDTH)
        f.setframerate(WAV_RATE)
        f.writeframes(pcm_24k)


# --- regenerate ---


def test_regenerate_skips_when_cached(tmp_path):
    backend = _FakeBackend()
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path),
        hostname="jts.local",
        voice="Aoede",
        backend=backend,
    )
    written1 = mgr.regenerate()
    assert set(written1) == {c.slug for c in CUES}
    # Second pass: everything cached → no new writes.
    backend.calls.clear()
    written2 = mgr.regenerate()
    assert written2 == []
    assert backend.calls == []


def test_regenerate_force_re_renders_even_when_cached(tmp_path):
    backend = _FakeBackend()
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path),
        hostname="jts.local",
        voice="Aoede",
        backend=backend,
    )
    mgr.regenerate()
    backend.calls.clear()
    written = mgr.regenerate(force=True)
    assert set(written) == {c.slug for c in CUES}
    assert len(backend.calls) == len(CUES)


def test_regenerate_single_slug_only(tmp_path):
    backend = _FakeBackend()
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path),
        hostname="jts.local",
        voice="Aoede",
        backend=backend,
    )
    written = mgr.regenerate(slug="spend_cap_reached")
    assert written == ["spend_cap_reached"]
    assert len(backend.calls) == 1


def test_regenerate_unknown_slug_raises(tmp_path):
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="jts.local",
        voice="Aoede", backend=_FakeBackend(),
    )
    with pytest.raises(ValueError):
        mgr.regenerate(slug="this_does_not_exist")


def test_regenerate_without_backend_raises(tmp_path):
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="jts.local", voice="Aoede",
    )
    with pytest.raises(RuntimeError):
        mgr.regenerate()


def test_regenerate_after_hostname_change_writes_new_file_and_prunes(tmp_path):
    """The whole point of content-addressable caching: editing
    JASPER_MANAGEMENT_URL → next regenerate writes a new file with a
    different hash AND prunes the old one."""
    backend = _FakeBackend()
    mgr_old = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="old.local",
        voice="Aoede", backend=backend,
    )
    mgr_old.regenerate(slug="spend_cap_reached")
    cue = find("spend_cap_reached")
    old_file = cue_filename(cue, "old.local", "Aoede")
    assert (tmp_path / old_file).exists()

    mgr_new = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="new.local",
        voice="Aoede", backend=backend,
    )
    mgr_new.regenerate(slug="spend_cap_reached")
    new_file = cue_filename(cue, "new.local", "Aoede")
    assert (tmp_path / new_file).exists()
    assert old_file != new_file
    # Stale file pruned.
    assert not (tmp_path / old_file).exists()


# --- introspection ---


def test_status_reports_cached_and_not_cached(tmp_path):
    backend = _FakeBackend()
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="jts.local",
        voice="Aoede", backend=backend,
    )
    s_before = mgr.status()
    assert all(entry["cached"] is False for entry in s_before)
    mgr.regenerate()
    s_after = mgr.status()
    assert all(entry["cached"] is True for entry in s_after)
    # Each entry has the rendered text and expected filename.
    for entry in s_after:
        assert entry["expected_filename"].startswith(entry["slug"] + "-")
        assert entry["rendered_text"]
        assert entry["description"]


# --- play ---


def test_play_queues_pcm_to_tts_playout_when_cached(tmp_path):
    backend = _FakeBackend(samples_24k=240)
    tts = _FakeTtsPlayout()
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="jts.local", voice="Aoede",
        backend=backend, tts_playout=tts,
    )
    mgr.regenerate()

    ok = asyncio.run(mgr.play("spend_cap_reached"))
    assert ok is True
    assert len(tts.writes) == 1
    # WAVs are at Gemini's native 24kHz mono — 240 samples = 480 bytes.
    # TtsPlayout upsamples to 48k internally; the manager doesn't.
    assert len(tts.writes[0]) == 480


def test_play_returns_false_with_no_tts_playout(tmp_path):
    backend = _FakeBackend()
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="jts.local", voice="Aoede",
        backend=backend, tts_playout=None,
    )
    mgr.regenerate()
    assert asyncio.run(mgr.play("spend_cap_reached")) is False


def test_play_unknown_slug_returns_false(tmp_path):
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="jts.local", voice="Aoede",
        backend=_FakeBackend(), tts_playout=_FakeTtsPlayout(),
    )
    assert asyncio.run(mgr.play("not_a_real_slug")) is False


def test_play_falls_back_to_stale_when_expected_hash_missing(tmp_path):
    """If the expected hash file is missing but a stale-hash file
    exists for the same slug, play that. Stale > silent."""
    cue = find("spend_cap_reached")
    # Hand-write a "stale" file under a hash that doesn't match the
    # current (hostname, voice) inputs.
    stale_path = tmp_path / f"{cue.slug}-stale01.wav"
    _hand_write_wav(str(stale_path), b"\x00\x00" * 100)

    tts = _FakeTtsPlayout()
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="current.local", voice="Aoede",
        tts_playout=tts,
    )
    # Sanity: expected path is a different filename.
    assert mgr.expected_path(cue) != str(stale_path)
    ok = asyncio.run(mgr.play("spend_cap_reached"))
    assert ok is True
    assert len(tts.writes) == 1


def test_play_returns_false_when_no_cache_and_no_stale(tmp_path):
    """Empty sounds dir + no stale fallback → silent failure but no
    exception. Same UX we have today, but the warning surfaces in
    logs / jasper-doctor so an operator can see the cause."""
    tts = _FakeTtsPlayout()
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="jts.local", voice="Aoede",
        tts_playout=tts,
    )
    ok = asyncio.run(mgr.play("spend_cap_reached"))
    assert ok is False
    assert tts.writes == []


def test_play_swallows_tts_write_exception(tmp_path):
    """A broken audio chain must not throw out of the failure
    handler that called `play()`."""
    backend = _FakeBackend()
    tts = _FakeTtsPlayout()
    tts.fail_with = RuntimeError("ALSA hates us today")
    mgr = AudioCueManager(
        sounds_dir=str(tmp_path), hostname="jts.local", voice="Aoede",
        backend=backend, tts_playout=tts,
    )
    mgr.regenerate()
    ok = asyncio.run(mgr.play("spend_cap_reached"))
    assert ok is False
