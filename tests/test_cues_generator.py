from __future__ import annotations

import os
import wave

import pytest

from jasper.cues import CUES, CueDef
from jasper.cues.generator import (
    TTSResult,
    WAV_CHANNELS,
    WAV_RATE,
    WAV_SAMPLE_WIDTH,
    cue_filename,
    cue_hash,
    cue_path,
    prune_stale,
    render_template,
    write_cue,
)
from jasper.cues.registry import find


# --- Registry ---


def test_registry_has_known_cues():
    slugs = {c.slug for c in CUES}
    assert "spend_cap_reached" in slugs
    assert "cant_connect" in slugs


def test_find_returns_cue_or_none():
    assert find("spend_cap_reached") is not None
    assert find("does_not_exist") is None


def test_template_uses_hostname_for_spend_cap():
    cue = find("spend_cap_reached")
    assert cue is not None
    rendered = render_template(cue, "jts.local")
    assert "jts.local" in rendered


def test_template_no_hostname_for_cant_connect():
    """cant_connect doesn't include a URL — no point telling the user
    to visit a page when the issue is that we can't reach the
    network at all."""
    cue = find("cant_connect")
    assert cue is not None
    rendered = render_template(cue, "ignored.local")
    assert "ignored.local" not in rendered


def test_cues_are_provider_agnostic():
    """No cue may name a specific provider — the project may switch
    voice backends, and audio files baked with provider names would
    mislead users post-switch (per the project's design memory)."""
    forbidden = ("google", "gemini", "openai", "anthropic")
    for cue in CUES:
        text = cue.template.lower()
        for word in forbidden:
            assert word not in text, (
                f"cue {cue.slug!r} mentions {word!r} — keep messages "
                "provider-agnostic"
            )


# --- Hash ---


def test_hash_is_deterministic():
    cue = CUES[0]
    h1 = cue_hash(cue, "jts.local", "Aoede")
    h2 = cue_hash(cue, "jts.local", "Aoede")
    assert h1 == h2
    assert len(h1) == 8


def test_hash_changes_with_hostname():
    cue = CUES[0]
    a = cue_hash(cue, "jts.local", "Aoede")
    b = cue_hash(cue, "jasper.local", "Aoede")
    assert a != b


def test_hash_changes_with_voice():
    cue = CUES[0]
    a = cue_hash(cue, "jts.local", "Aoede")
    b = cue_hash(cue, "jts.local", "Charon")
    assert a != b


def test_hash_changes_with_template_text():
    """Editing the template text must invalidate the cache. We verify
    via two cues that share everything else but differ in text."""
    a = cue_hash(CUES[0], "jts.local", "Aoede")
    b = cue_hash(CUES[1], "jts.local", "Aoede")
    assert a != b


def test_filename_has_slug_and_hash():
    cue = CUES[0]
    name = cue_filename(cue, "jts.local", "Aoede")
    assert name.startswith(cue.slug + "-")
    assert name.endswith(".wav")


# --- Write + WAV format ---


class _FakeBackend:
    """Returns a deterministic 24kHz int16 PCM stream so write_cue is
    testable without hitting the network."""

    def __init__(self, samples_24k: int = 240) -> None:
        # `samples_24k` int16 zero samples = `samples_24k * 2` bytes
        # of silence at 24kHz. Small but nonzero so resample / WAV
        # write paths are exercised.
        self._pcm = b"\x00\x00" * samples_24k
        self.calls: list[str] = []

    def synthesise(self, text: str) -> TTSResult:
        self.calls.append(text)
        return TTSResult(pcm_24k=self._pcm)


def _read_wav(path: str) -> tuple[int, int, int, int]:
    with wave.open(path, "rb") as f:
        return (
            f.getnchannels(),
            f.getsampwidth(),
            f.getframerate(),
            f.getnframes(),
        )


def test_write_cue_writes_24k_mono_16bit_wav(tmp_path):
    cue = CUES[0]
    backend = _FakeBackend(samples_24k=240)
    path = write_cue(cue, "jts.local", "Aoede", str(tmp_path), backend)
    assert os.path.isfile(path)
    chans, sw, rate, frames = _read_wav(path)
    # 24kHz mono 16-bit — same shape as Gemini Live's streaming
    # audio. TtsPlayout assumes this format and upsamples to 48k
    # internally; double-upsampling here would play at half speed.
    assert chans == WAV_CHANNELS == 1
    assert sw == WAV_SAMPLE_WIDTH == 2
    assert rate == WAV_RATE == 24000
    assert frames == 240  # exactly the input — no resample


def test_write_cue_filename_matches_hash(tmp_path):
    cue = CUES[0]
    backend = _FakeBackend()
    path = write_cue(cue, "jts.local", "Aoede", str(tmp_path), backend)
    expected = cue_path(str(tmp_path), cue, "jts.local", "Aoede")
    assert path == expected


def test_write_cue_passes_rendered_text_to_backend(tmp_path):
    cue = CUES[0]
    backend = _FakeBackend()
    write_cue(cue, "jts.local", "Aoede", str(tmp_path), backend)
    assert backend.calls == ["Hey, I've reached today's spend cap. "
                             "Visit jts.local to manage."]


def test_write_cue_creates_sounds_dir(tmp_path):
    """Sounds dir doesn't have to exist beforehand — write_cue creates
    it. Matters because /var/lib/jasper/sounds may not exist on a
    fresh install before the first regen."""
    nested = tmp_path / "nested" / "sounds"
    cue = CUES[0]
    backend = _FakeBackend()
    path = write_cue(cue, "jts.local", "Aoede", str(nested), backend)
    assert os.path.isfile(path)


# --- Pruning stale files ---


def test_prune_stale_removes_old_hash_files(tmp_path):
    cue = CUES[0]
    # Create three files: two stale, one we want to keep.
    keep_hash = "abcd1234"
    (tmp_path / f"{cue.slug}-old00001.wav").write_bytes(b"WAV1")
    (tmp_path / f"{cue.slug}-old00002.wav").write_bytes(b"WAV2")
    (tmp_path / f"{cue.slug}-{keep_hash}.wav").write_bytes(b"KEEP")
    # Also drop in an unrelated file that should NOT be touched.
    (tmp_path / "other_cue-zz.wav").write_bytes(b"OTHER")

    removed = prune_stale(str(tmp_path), cue, keep_hash)
    assert removed == 2
    assert (tmp_path / f"{cue.slug}-{keep_hash}.wav").exists()
    assert not (tmp_path / f"{cue.slug}-old00001.wav").exists()
    assert not (tmp_path / f"{cue.slug}-old00002.wav").exists()
    assert (tmp_path / "other_cue-zz.wav").exists()


def test_prune_stale_handles_missing_dir(tmp_path):
    cue = CUES[0]
    missing = tmp_path / "does-not-exist"
    assert prune_stale(str(missing), cue, "anything") == 0


# --- Dynamic text (timer announcements + future variable cues) ---


def test_dynamic_text_path_includes_text_in_hash(tmp_path):
    """Different text → different cache file. Critical because
    `Your timer for 30 seconds is up.` and `Your timer for 5 minutes
    is up.` must NOT collide on the same WAV."""
    from jasper.cues.generator import dynamic_text_path
    a = dynamic_text_path(str(tmp_path), "Your timer for 30 seconds is up.", "Aoede")
    b = dynamic_text_path(str(tmp_path), "Your timer for 5 minutes is up.", "Aoede")
    assert a != b


def test_dynamic_text_path_includes_voice_in_hash(tmp_path):
    """Different voice → different cache file, so swapping providers
    invalidates baked WAVs automatically."""
    from jasper.cues.generator import dynamic_text_path
    a = dynamic_text_path(str(tmp_path), "Test text.", "Aoede")
    b = dynamic_text_path(str(tmp_path), "Test text.", "marin")
    assert a != b


def test_write_dynamic_text_idempotent_when_cached(tmp_path):
    """Second call with same args does NOT re-synthesise — that's the
    whole point of caching. Catches regressions where the cache check
    falls through and burns a Gemini API call per fire."""
    from jasper.cues.generator import write_dynamic_text
    backend = _FakeBackend(samples_24k=240)
    path1 = write_dynamic_text(
        "hello world", "Aoede", str(tmp_path), backend,
    )
    path2 = write_dynamic_text(
        "hello world", "Aoede", str(tmp_path), backend,
    )
    assert path1 == path2
    assert len(backend.calls) == 1  # only first call hit the backend


# --- Provider TTS backends (smoke tests; real network calls are
# verified on-Pi during deploy, not here) ---


def test_gemini_tts_default_model_is_3_1():
    """Sanity guard: we explicitly moved off 2.5-preview-tts because
    of FinishReason.OTHER instability. Don't let a regression silently
    pin us back to the old model."""
    from jasper.cues.generator import (
        GEMINI_TTS_MODEL, GeminiTTSGenerator,
    )
    g = GeminiTTSGenerator(api_key="x", voice="Aoede")
    assert GEMINI_TTS_MODEL == "gemini-3.1-flash-tts-preview"
    assert g.model == "gemini-3.1-flash-tts-preview"


def test_gemini_tts_retries_on_empty_content(monkeypatch):
    """Core fix: when `_attempt` reports no content (the production
    failure mode, FinishReason.OTHER content=None), synthesise loops
    and tries again. Three failures then a success → returns OK with
    one logged retry warning per failure."""
    from jasper.cues.generator import GeminiTTSGenerator, TTSResult
    g = GeminiTTSGenerator(api_key="x", voice="Aoede")
    calls = {"n": 0}

    def fake_attempt(text):
        calls["n"] += 1
        if calls["n"] < 4:
            return ("finish=OTHER_content=None", None)
        return ("ok", TTSResult(pcm_24k=b"\x00\x00" * 240))

    monkeypatch.setattr(g, "_attempt", fake_attempt)
    # Don't actually sleep between retries during tests.
    monkeypatch.setattr(
        "jasper.cues.generator.time.sleep", lambda *_: None,
    )
    result = g.synthesise("anything")
    assert calls["n"] == 4
    assert result.pcm_24k.startswith(b"\x00\x00")


def test_gemini_tts_raises_after_max_attempts(monkeypatch):
    """If every attempt comes back empty, we surface a clean error
    with the last status (so logs show what Gemini said) instead of
    a vague AttributeError."""
    from jasper.cues.generator import (
        GeminiTTSGenerator, TTS_MAX_ATTEMPTS,
    )
    g = GeminiTTSGenerator(api_key="x", voice="Aoede")
    monkeypatch.setattr(
        g, "_attempt",
        lambda text: ("finish=OTHER_content=None", None),
    )
    monkeypatch.setattr(
        "jasper.cues.generator.time.sleep", lambda *_: None,
    )
    with pytest.raises(RuntimeError, match="finish=OTHER"):
        g.synthesise("anything")


def test_openai_tts_construction_and_model():
    """Smoke: OpenAITTSGenerator constructs without doing any IO and
    pins the recommended model. Real synthesis is verified on-Pi."""
    from jasper.cues.generator import (
        OPENAI_TTS_MODEL, OpenAITTSGenerator,
    )
    g = OpenAITTSGenerator(api_key="x", voice="marin")
    assert g.model == OPENAI_TTS_MODEL == "gpt-4o-mini-tts"


def test_grok_tts_construction_and_model():
    """Smoke: GrokTTSGenerator constructs without doing any IO."""
    from jasper.cues.generator import (
        GROK_TTS_MODEL, GrokTTSGenerator,
    )
    g = GrokTTSGenerator(api_key="x", voice="eve")
    assert g.model == GROK_TTS_MODEL


def test_provider_tts_generators_reject_empty_credentials():
    """All three backends require api_key + voice up front so a
    misconfigured Pi fails loudly at startup rather than silently at
    first cue synthesis."""
    from jasper.cues.generator import (
        GeminiTTSGenerator, GrokTTSGenerator, OpenAITTSGenerator,
    )
    for cls in (GeminiTTSGenerator, OpenAITTSGenerator, GrokTTSGenerator):
        with pytest.raises(ValueError):
            cls(api_key="", voice="anything")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            cls(api_key="x", voice="")  # type: ignore[arg-type]
