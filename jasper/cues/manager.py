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
import wave
from typing import Any

from .generator import (
    cue_hash,
    cue_path,
    prune_stale,
    write_cue,
)
from .registry import CUES, CueDef, find as find_cue

logger = logging.getLogger(__name__)


# Extra time to wait after the computed audio duration so ALSA's
# pipeline / dmix buffer fully empties before un-ducking. 200ms is
# generous for the dongle's typical ~85ms dmix buffer.
_PLAY_DRAIN_BUFFER_SEC = 0.2


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
        self._tts = tts_playout

    # --- introspection ---

    def expected_path(self, cue: CueDef) -> str:
        return cue_path(self._sounds_dir, cue, self._hostname, self._voice)

    def is_cached(self, cue: CueDef) -> bool:
        return os.path.isfile(self.expected_path(cue))

    def status(self) -> list[dict]:
        """Snapshot for `jasper-cues list` and `jasper-doctor`. Each
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
        no TTS backend was configured. Per-cue exceptions DURING
        synthesis (network errors, TTS API failures) are surfaced —
        callers in non-blocking contexts (startup background task)
        catch them and log."""
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
        for cue in cues:
            if not force and self.is_cached(cue):
                logger.debug("cue %s already cached, skipping", cue.slug)
                continue
            write_cue(
                cue, self._hostname, self._voice,
                self._sounds_dir, self._backend,
            )
            prune_stale(
                self._sounds_dir, cue,
                cue_hash(cue, self._hostname, self._voice),
            )
            written.append(cue.slug)
        return written

    # --- playback ---

    async def play(self, slug: str) -> bool:
        """Queue the named cue's audio onto TtsPlayout. Returns True
        on success, False on any error (no TtsPlayout, no cached
        file, IO error). Never raises — failure-path callers must
        be able to call this without further error handling."""
        cue = find_cue(slug)
        if cue is None:
            logger.warning("cue play: unknown slug %r", slug)
            return False
        if self._tts is None:
            logger.warning("cue play: no TtsPlayout configured (slug=%s)", slug)
            return False

        # Prefer the current-hash file. If missing, fall back to ANY
        # cached version under the same slug — stale audio beats
        # silent failure (per the project's silent-failure-is-bad
        # design memo). The mismatch usually means a config change
        # since the last regen; the daemon's startup task will fix
        # it on the next restart.
        path = self.expected_path(cue)
        if not os.path.isfile(path):
            stale = self._find_any_cached(cue)
            if stale is None:
                logger.warning(
                    "cue play: no cached file for %s and no stale "
                    "fallback; user gets silence. Run "
                    "`jasper-cues regenerate` to fix.", slug,
                )
                return False
            logger.info(
                "cue play: expected file missing, using stale %s",
                os.path.basename(stale),
            )
            path = stale

        try:
            pcm, audio_duration_sec = self._read_wav_pcm(path)
        except (OSError, wave.Error) as e:
            logger.warning("cue play: could not read %s: %s", path, e)
            return False

        try:
            await self._tts.write(pcm)
        except Exception as e:  # noqa: BLE001
            logger.warning("cue play: TtsPlayout.write failed (slug=%s): %s", slug, e)
            return False
        # TtsPlayout.write() goes through sounddevice's BLOCKING
        # write under asyncio.to_thread — by the time it returns,
        # all bytes have been pushed into the stream buffer at
        # playback rate (so write() takes ~audio_duration_sec wall
        # clock). All that's left is the small ALSA / dmix tail
        # buffer; sleep just that long before returning so callers
        # that wrap us with duck/restore don't un-duck mid-tail.
        await asyncio.sleep(_PLAY_DRAIN_BUFFER_SEC)
        logger.info(
            "cue play: %s (%d bytes pcm, audio=%.1fs)",
            slug, len(pcm), audio_duration_sec,
        )
        return True

    # --- internals ---

    def _find_any_cached(self, cue: CueDef) -> str | None:
        if not os.path.isdir(self._sounds_dir):
            return None
        prefix = f"{cue.slug}-"
        for entry in sorted(os.listdir(self._sounds_dir)):
            if entry.startswith(prefix) and entry.endswith(".wav"):
                return os.path.join(self._sounds_dir, entry)
        return None

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
