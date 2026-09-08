# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AudioCueManager — central object the daemon calls to play cues
and ensure the cache is fresh.

One instance per daemon, created at startup with the speaker's
hostname (for `{hostname}` substitution in templates), voice (for
TTS consistency with the rest of the assistant), TTS backend (for
regeneration), and TtsPlayout (for playback through the existing
audio chain).

Threading model:
  - `play(slug)` is async — it queues PCM onto TtsPlayout's existing
    write coroutine, so cue audio rides the same volume / ducking
    pipeline as Gemini Live audio.
  - `regenerate(...)` is SYNC because the underlying TTS HTTP call
    is blocking. Callers from asyncio code wrap it with
    `asyncio.to_thread(...)`. This keeps the CLI simple.

Failure semantics — every public method swallows IO / network errors
and logs them. The whole point of this subsystem is to make failure
modes audible; an exception thrown back into the failure handler
that called `play()` would defeat that.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import wave
from typing import Any, Callable

from ..assistant_loudness import AssistantLoudnessProfile, measure_pcm_24k_mono
from ..audio_io import wait_tts_drained_owned
from ..log_event import log_event
from .generator import (
    backend_model,
    cue_hash,
    cue_path,
    dynamic_text_path,
    prune_retired,
    prune_stale,
    write_cue,
    write_dynamic_text,
)
from .registry import CUES, CueDef, find as find_cue

logger = logging.getLogger(__name__)

_CUE_AUDIO_PROFILE_PROVIDER = "jts"
_CUE_AUDIO_PROFILE_UPDATED_AT = "static"

# Cue-delivery outcomes (closed set) — the terminal result of one play()/
# speak_text() attempt, tracked by _record() for /state and jasper-doctor.
OUTCOME_DELIVERED = "delivered"  # PCM written to the playout AND its computed
# drain deadline elapsed — NOT a confirmation the cue was actually heard.
OUTCOME_FALLBACK = "fallback"
OUTCOME_STALE = "stale"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
_OUTCOMES = (
    OUTCOME_DELIVERED, OUTCOME_FALLBACK, OUTCOME_STALE, OUTCOME_SKIPPED,
    OUTCOME_FAILED,
)

# Reasons (closed set), paired with an outcome by _record().
REASON_OK = "ok"  # delivered / fallback / stale
REASON_UNKNOWN_SLUG = "unknown_slug"
REASON_NO_PLAYOUT = "no_playout"
REASON_NO_CACHE = "no_cache"
REASON_READ_ERROR = "read_error"
REASON_WRITE_ERROR = "write_error"
REASON_NO_BACKEND = "no_backend"
REASON_SYNTHESIS_ERROR = "synthesis_error"
REASON_STALE_TEXT = "stale_text"

# `_record()`'s slug for dynamic speech (speak_text) — never the spoken text.
_DYNAMIC_TEXT_SLUG = "text"


def _preview(text: str, limit: int = 40) -> str:
    """Short, log-safe repr of dynamic cue text — first `limit` chars, so
    persistent INFO logs don't carry full (possibly personal) result content."""
    text = text or ""
    return repr(text if len(text) <= limit else text[:limit] + "…")


# Fallback wait for legacy/fake playout objects that predate
# TtsPlayout.wait_drained(). Real TtsPlayout implementations expose a
# sample-counted drain deadline, which is the source of truth for both
# the old sounddevice path and the outputd path.
_PLAY_DRAIN_BUFFER_SEC = 0.2


async def _wait_tts_drained(tts: Any) -> None:
    await wait_tts_drained_owned(tts, fallback_sec=_PLAY_DRAIN_BUFFER_SEC)


def _profile_token(value: str, fallback: str) -> str:
    token = "".join(ch if ch.isascii() and not ch.isspace() else "_" for ch in value)
    token = token.strip("_")
    return token or fallback


def _cue_source_profile(
    *,
    model: str,
    voice: str,
    pcm: bytes,
    fallback_source_lufs: float = -24.0,
    fallback_peak_dbfs: float = -6.0,
) -> AssistantLoudnessProfile:
    """Describe the PCM source loudness for a cached/spoken cue.

    The final gain decision still belongs to fan-in/outputd. This profile only
    tells that owner what loudness/peak this exact cue WAV starts with, so cues
    do not have to borrow the active live-assistant profile.
    """
    model = _profile_token(model, "cue")
    voice = _profile_token(voice, "voice")
    try:
        measurement = measure_pcm_24k_mono(pcm)
        source_lufs = measurement.source_lufs
        source_peak_dbfs = measurement.source_peak_dbfs
        confidence = 1.0
    except (ImportError, RuntimeError, ValueError) as e:
        log_event(
            logger,
            "cue.source_profile",
            result="fallback",
            model=model,
            voice=voice,
            exc_type=type(e).__name__,
            err=str(e),
            level=logging.WARNING,
        )
        source_lufs = fallback_source_lufs
        source_peak_dbfs = fallback_peak_dbfs
        confidence = 0.0
    return AssistantLoudnessProfile(
        provider=_CUE_AUDIO_PROFILE_PROVIDER,
        model=model,
        voice=voice,
        source_lufs=round(float(source_lufs), 2),
        source_peak_dbfs=round(float(source_peak_dbfs), 2),
        confidence=confidence,
        updated_at=_CUE_AUDIO_PROFILE_UPDATED_AT,
        method="cue_wav",
    )


class AudioCueManager:
    def __init__(
        self,
        sounds_dir: str,
        hostname: str,
        voice: str,
        backend: Any | None = None,
        tts_playout: Any | None = None,
    ):
        self._sounds_dir = sounds_dir
        self._hostname = hostname
        self._voice = voice
        self._backend = backend
        # Cache-key model: the backend's actual synthesis model, so a
        # JASPER_GEMINI_TTS_MODEL flip (or a provider TTS-default bump)
        # invalidates baked WAVs the same way a voice change does.
        # Falls back to the legacy constant when backend is None
        # (playback-only manager) — `play()`'s any-cached-version
        # fallback keeps cues audible either way.
        self._model = backend_model(backend)
        self._tts = tts_playout
        self._outcome_counts: dict[str, int] = {outcome: 0 for outcome in _OUTCOMES}
        self._last_outcome: dict[str, Any] | None = None

    def attach_tts(self, tts_playout: Any) -> None:
        """Set the playback target after construction. Useful when
        the manager is built before the daemon's TtsPlayout is open
        (so timer tools / cue regen can use the synthesis path
        without waiting for ALSA to come up)."""
        self._tts = tts_playout

    # --- delivery health (for /state + jasper-doctor) ---

    def _record(self, outcome: str, reason: str, slug: str) -> None:
        """Track one play()/speak_text() terminal result. `slug` is the
        registry slug, or `_DYNAMIC_TEXT_SLUG` for dynamic speech — never
        the spoken text itself."""
        self._outcome_counts[outcome] += 1
        self._last_outcome = {
            "outcome": outcome, "reason": reason, "slug": slug, "at": time.time(),
        }

    def snapshot(self) -> dict[str, Any]:
        """Bounded cue-delivery health: counts by outcome plus the last
        outcome, for /state and jasper-doctor. Never the cue/dynamic text
        itself — see `_record`."""
        last = None
        if self._last_outcome is not None:
            last = {
                "outcome": self._last_outcome["outcome"],
                "reason": self._last_outcome["reason"],
                "slug": self._last_outcome["slug"],
                "age_seconds": max(
                    0.0, round(time.time() - self._last_outcome["at"], 1),
                ),
            }
        return {"counts": dict(self._outcome_counts), "last": last}

    # --- introspection ---

    def expected_path(self, cue: CueDef) -> str:
        return cue_path(
            self._sounds_dir, cue, self._hostname, self._voice, self._model,
        )

    def is_cached(self, cue: CueDef) -> bool:
        return os.path.isfile(self.expected_path(cue))

    def status(self) -> list[dict]:
        """Snapshot for ``jasper-cues list``. Each
        entry is {slug, rendered_text, expected_filename, cached,
        description}."""
        out = []
        for cue in CUES:
            from .generator import render_template
            out.append({
                "slug": cue.slug,
                "rendered_text": render_template(cue, self._hostname),
                "expected_filename": os.path.basename(self.expected_path(cue)),
                "cached": self.is_cached(cue),
                "description": cue.description,
            })
        return out

    # --- regeneration ---

    def regenerate(
        self, slug: str | None = None, force: bool = False,
    ) -> list[str]:
        """Synthesise missing cues (or just `slug` if given). With
        `force=True`, re-render even when the file is already cached.
        Returns the list of slugs that were newly written.

        Raises ValueError on an unknown slug; raises RuntimeError if
        no TTS backend was configured.

        Per-cue synthesis failures (network errors, TTS API failures)
        are ISOLATED: a single cue that exhausts its retries is logged
        at WARNING and skipped, so its siblings still get a chance to
        generate on the same pass. Gemini TTS failures are often
        text-specific rather than a categorical outage, so aborting the
        whole batch on the first bad cue would needlessly leave later,
        healthy cues uncached (and thus silent at play time)."""
        if self._backend is None:
            raise RuntimeError(
                "AudioCueManager has no TTS backend — can't regenerate. "
                "Construct with a backend (or use a fake one in tests)."
            )
        if slug is not None:
            cue = find_cue(slug)
            if cue is None:
                raise ValueError(f"unknown cue slug: {slug!r}")
            cues: tuple[CueDef, ...] = (cue,)
        else:
            cues = CUES

        written: list[str] = []
        failed: list[str] = []
        for cue in cues:
            if not force and self.is_cached(cue):
                logger.debug("cue %s already cached, skipping", cue.slug)
                continue
            try:
                write_cue(
                    cue, self._hostname, self._voice,
                    self._sounds_dir, self._backend,
                )
                prune_stale(
                    self._sounds_dir, cue,
                    cue_hash(cue, self._hostname, self._voice, self._model),
                )
            except Exception as e:  # noqa: BLE001
                failed.append(cue.slug)
                log_event(
                    logger,
                    "cue.regenerate_failed",
                    result="skipped_cue",
                    slug=cue.slug,
                    exc_type=type(e).__name__,
                    err=str(e),
                    level=logging.WARNING,
                )
                continue
            written.append(cue.slug)
        if slug is None:
            prune_retired(self._sounds_dir, {c.slug for c in CUES})
        if failed:
            logger.warning(
                "cue regenerate: %d/%d cue(s) failed and were skipped "
                "(%s); siblings still generated. Stale/any-cached fallback "
                "keeps failed cues audible where a prior WAV exists.",
                len(failed), len(cues), ", ".join(failed),
            )
        return written

    # --- playback ---

    async def play(self, slug: str) -> bool:
        """Queue the named cue's audio onto TtsPlayout. Returns True
        on success, False on any error (no TtsPlayout, no cached
        file, IO error). Never raises — failure-path callers must
        be able to call this without further error handling."""
        outcome, reason, ok = await self._play_inner(slug)
        self._record(outcome, reason, slug)
        return ok

    async def _play_inner(self, slug: str) -> tuple[str, str, bool]:
        """One play attempt as (outcome, reason, ok), fallback delegation
        included, so `play()` writes exactly one record per attempt."""
        cue = find_cue(slug)
        if cue is None:
            logger.warning("cue play: unknown slug %r", slug)
            return OUTCOME_FAILED, REASON_UNKNOWN_SLUG, False
        if self._tts is None:
            logger.warning("cue play: no TtsPlayout configured (slug=%s)", slug)
            return OUTCOME_FAILED, REASON_NO_PLAYOUT, False

        # Prefer the current-hash file. If missing, fall back to ANY
        # cached version under the same slug — stale audio beats
        # silent failure (per the project's silent-failure-is-bad
        # design memo). The mismatch usually means a config change
        # since the last regen; the daemon's startup task will fix
        # it on the next restart.
        path = self.expected_path(cue)
        stale_used = False
        if not os.path.isfile(path):
            stale = self.find_any_cached(cue)
            if stale is None and cue.fallback is not None:
                log_event(
                    logger, "cue.play_fallback", slug=slug, fallback=cue.fallback,
                )
                outcome, reason, ok = await self._play_inner(cue.fallback)
                if ok:
                    return OUTCOME_FALLBACK, REASON_OK, True
                return outcome, reason, False
            if stale is None:
                logger.warning(
                    "cue play: no cached file for %s and no stale "
                    "fallback; user gets silence. Run "
                    "`jasper-cues regenerate` to fix.", slug,
                )
                return OUTCOME_FAILED, REASON_NO_CACHE, False
            logger.info(
                "cue play: expected file missing, using stale %s",
                os.path.basename(stale),
            )
            path = stale
            stale_used = True

        try:
            pcm, audio_duration_sec = self._read_wav_pcm(path)
        except (OSError, wave.Error) as e:
            logger.warning("cue play: could not read %s: %s", path, e)
            return OUTCOME_FAILED, REASON_READ_ERROR, False

        try:
            await self._write_pcm(self._tts, pcm, model=f"cue-{cue.slug}")
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "cue play: TtsPlayout.write failed (slug=%s): %s", slug, e,
            )
            return OUTCOME_FAILED, REASON_WRITE_ERROR, False
        finally:
            await _wait_tts_drained(self._tts)
        logger.info(
            "cue play: %s (%d bytes pcm, audio=%.1fs)",
            slug, len(pcm), audio_duration_sec,
        )
        return (
            OUTCOME_STALE if stale_used else OUTCOME_DELIVERED, REASON_OK, True,
        )

    async def prerender_text(self, text: str) -> bool:
        """Synthesise + cache `text` ahead of time without playing it.

        Used by callers that know they'll speak `text` later and want
        the eventual `speak_text(...)` call to be a cache hit (so the
        audio fires instantly with no synthesis-attempt latency
        eating the user's expected timing — the timer fire path
        especially). Idempotent: returns True without re-rendering
        if already cached. Returns False on any failure (no backend,
        synthesis error); never raises.
        """
        if self._backend is None:
            return False
        path = dynamic_text_path(
            self._sounds_dir, text, self._voice, self._model,
        )
        if os.path.isfile(path):
            return True
        try:
            await asyncio.to_thread(
                write_dynamic_text,
                text, self._voice, self._sounds_dir, self._backend,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "cue prerender_text: synthesis failed (text=%s): %s",
                _preview(text), e,
            )
            return False

    async def speak_text(self, text: str) -> bool:
        return await self._speak_text(text)

    async def speak_text_guarded(
        self,
        text: str,
        should_play: Callable[[], bool],
    ) -> bool:
        return await self._speak_text(text, should_play=should_play)

    async def _speak_text(
        self,
        text: str,
        *,
        should_play: Callable[[], bool] | None = None,
    ) -> bool:
        """Render arbitrary `text` via TTS and play through TtsPlayout.

        Used for dynamic content — timer fire announcements with the
        elapsed duration, etc. — where a static CueDef would have to
        enumerate every variant. Cached by hash of (text, voice, model)
        so repeated identical phrases reuse the same WAV across daemon
        restarts.

        First synthesis takes ~1 s of network round-trip to Gemini TTS;
        subsequent plays of the same text are instant (cache hit).

        Failure semantics match `play()` — never raises; returns False
        on any error (no backend, no TtsPlayout, network failure, IO
        error). Callers should not need extra error handling.
        """
        outcome, reason, ok = await self._speak_inner(
            text, should_play=should_play,
        )
        self._record(outcome, reason, _DYNAMIC_TEXT_SLUG)
        return ok

    async def _speak_inner(
        self,
        text: str,
        *,
        should_play: Callable[[], bool] | None,
    ) -> tuple[str, str, bool]:
        """One speak_text attempt as (outcome, reason, ok) — see
        `_play_inner`."""
        if self._tts is None:
            logger.warning("cue speak_text: no TtsPlayout configured")
            return OUTCOME_FAILED, REASON_NO_PLAYOUT, False
        if self._backend is None:
            logger.warning("cue speak_text: no TTS backend configured")
            return OUTCOME_FAILED, REASON_NO_BACKEND, False

        path = dynamic_text_path(
            self._sounds_dir, text, self._voice, self._model,
        )
        if should_play is not None and not should_play():
            logger.info("cue speak_text: skipped stale dynamic text")
            return OUTCOME_SKIPPED, REASON_STALE_TEXT, False
        if not os.path.isfile(path):
            try:
                await asyncio.to_thread(
                    write_dynamic_text,
                    text, self._voice, self._sounds_dir, self._backend,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("cue speak_text: synthesis failed: %s", e)
                return OUTCOME_FAILED, REASON_SYNTHESIS_ERROR, False

        try:
            pcm, audio_duration_sec = self._read_wav_pcm(path)
        except (OSError, wave.Error) as e:
            logger.warning("cue speak_text: could not read %s: %s", path, e)
            return OUTCOME_FAILED, REASON_READ_ERROR, False
        if should_play is not None and not should_play():
            logger.info("cue speak_text: skipped stale dynamic text")
            return OUTCOME_SKIPPED, REASON_STALE_TEXT, False

        try:
            await self._write_pcm(self._tts, pcm, model="dynamic-text")
        except Exception as e:  # noqa: BLE001
            logger.warning("cue speak_text: TtsPlayout.write failed: %s", e)
            return OUTCOME_FAILED, REASON_WRITE_ERROR, False
        finally:
            await _wait_tts_drained(self._tts)
        # Dynamic cue text (research results, timer labels) can be personal and
        # the journal is persistent — log a short preview + length at INFO, full
        # text only at DEBUG.
        logger.info(
            "cue speak_text: %s (%d chars, %d bytes pcm, audio=%.1fs)",
            _preview(text), len(text), len(pcm), audio_duration_sec,
        )
        logger.debug("cue speak_text full text: %r", text)
        return OUTCOME_DELIVERED, REASON_OK, True

    async def _write_pcm(self, tts: Any, pcm: bytes, *, model: str) -> None:
        """Hand cue PCM to `tts`, preferring the profiled segment API where
        the playout offers one. Raises on write failure."""
        write_segment = getattr(tts, "write_segment", None)
        if callable(write_segment):
            await write_segment(
                pcm,
                segment_kind="cue",
                source_profile=_cue_source_profile(
                    model=model, voice=self._voice, pcm=pcm,
                ),
            )
        else:
            await tts.write(pcm)

    def find_any_cached(self, cue: CueDef) -> str | None:
        """A cached WAV for `cue`'s slug at any hash, if one exists — the
        same stale-cache lookup `play()` performs when the current-hash
        file is missing."""
        if not os.path.isdir(self._sounds_dir):
            return None
        prefix = f"{cue.slug}-"
        for entry in sorted(os.listdir(self._sounds_dir)):
            if entry.startswith(prefix) and entry.endswith(".wav"):
                return os.path.join(self._sounds_dir, entry)
        return None

    # --- internals ---

    def _read_wav_pcm(self, path: str) -> "tuple[bytes, float]":
        """Strip the WAV header, return (raw PCM bytes, duration in
        seconds). Duration comes from the WAV header so we don't need
        to assume what rate the generator wrote at."""
        with wave.open(path, "rb") as f:
            nframes = f.getnframes()
            rate = f.getframerate()
            pcm = f.readframes(nframes)
            duration_sec = nframes / float(rate) if rate else 0.0
        return pcm, duration_sec
