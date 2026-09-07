# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-voice's assistant output: every path that puts assistant audio
on the wire, and sole owner of the output gate and the duck transport.

Cues, dynamic text, mute clicks and the listening chirp each take an
episode from the gate, prime loudness context, duck, play, drain and
release it here. The wake loop keeps only the turn episode it
snapshot-compares through teardown; nothing here reads loop state.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Awaitable, Callable, Coroutine
from inspect import isawaitable

from jasper.log_event import log_event

from ..assistant_loudness import (
    active_voice_identity,
    tts_envelope_lufs_for_level,
)
from ..audio_io import (
    TtsPlayout,
    tts_wire_is_wide as _tts_wire_is_wide,
    wait_tts_drained_owned,
)
from ..camilla import CueDuck, Ducker
from ..config import Config
from ..cues import AudioCueManager
from ..tts_routing import (
    tts_socket_feeds_post_dsp_outputd,
    tts_socket_feeds_pre_dsp_fanin,
)
from ..volume_coordinator import VolumeCoordinator
from .earcons import (
    _generate_listening_chirp,
    _generate_mute_click,
    _synthetic_audio_profile,
)
from .output_gate import (
    AssistantOutputEpisode,
    AssistantOutputGate,
)

logger = logging.getLogger("jasper.voice_daemon")
INTERNAL_ERROR_CUE_SLUG = "internal_error"


async def _capture_cleanup_error(
    operation: Callable[[], object],
) -> BaseException | None:
    """Run one sync/async cleanup step and return any raised outcome."""

    try:
        outcome = operation()
        if isawaitable(outcome):
            await outcome
    except BaseException as error:  # noqa: BLE001 - one cleanup capture boundary
        return error
    return None


class FanInDucker:
    """Voice-session duck transport for the pre-DSP TTS topology.

    The voice loop still owns the duck/restore lifecycle; fan-in owns
    where the attenuation happens. This keeps TTS out of the attenuated
    program lane while sending the final mixed signal through CamillaDSP
    crossover/protection.
    """

    def __init__(self, socket_path: str, duck_db: float) -> None:
        self._socket_path = socket_path
        self._duck_db = duck_db
        self._ducked = False

    @property
    def is_ducked(self) -> bool:
        return self._ducked

    @property
    def locks_camilla_volume(self) -> bool:
        """Fan-in ducking leaves Camilla available as the master volume."""
        return False

    async def duck(self) -> None:
        if self._ducked:
            return
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._send_command,
                b"PROGRAM_DUCK_ON\nCLOSE\n",
            ),
            name="fanin-program-duck-on",
        )
        deferred_cancel = False
        current = asyncio.current_task()
        while not worker.done():
            try:
                await asyncio.wait({worker})
            except asyncio.CancelledError:
                if current is None or current.cancelling() == 0:
                    break
                deferred_cancel = True
                current.uncancel()
        if worker.cancelled():
            raise asyncio.CancelledError
        error = worker.exception()
        ok = worker.result() if error is None else False
        # Once the bounded worker ran, False/OSError and unexpected failures
        # are ambiguous: connect/send may have delivered ON before CLOSE or
        # the reported error. Conservatively own one idempotent OFF so every
        # caller's cleanup restores the remote state before releasing output.
        self._ducked = True
        if error is not None:
            if deferred_cancel:
                raise asyncio.CancelledError from None
            raise error
        if not ok:
            if deferred_cancel:
                raise asyncio.CancelledError
            return
        log_event(
            logger,
            "camilla.duck",
            on="true",
            transport="fanin",
            socket=self._socket_path,
            duck_db=f"{self._duck_db:.1f}",
        )
        if deferred_cancel:
            raise asyncio.CancelledError

    async def restore(self) -> None:
        if not self._ducked:
            return
        try:
            ok = await asyncio.to_thread(
                self._send_command, b"PROGRAM_DUCK_OFF\nCLOSE\n"
            )
            if ok:
                log_event(
                    logger,
                    "camilla.duck",
                    on="false",
                    transport="fanin",
                    socket=self._socket_path,
                )
        finally:
            self._ducked = False

    def _send_command(self, payload: bytes) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                sock.connect(self._socket_path)
                sock.sendall(payload)
            return True
        except OSError as e:
            log_event(
                logger,
                "camilla.duck_failed",
                transport="fanin",
                socket=self._socket_path,
                detail=str(e),
                level=logging.WARNING,
            )
            return False


async def _await_output_cleanup_owned(
    operation: Coroutine,
    *,
    task_name: str,
) -> None:
    """Defer repeated caller cancellation until one cleanup completes."""

    cleanup = asyncio.create_task(operation, name=task_name)
    deferred_cancel = False
    current = asyncio.current_task()
    while not cleanup.done():
        try:
            await asyncio.wait({cleanup})
        except asyncio.CancelledError:
            if current is None or current.cancelling() == 0:
                break
            deferred_cancel = True
            current.uncancel()
    if cleanup.cancelled():
        raise asyncio.CancelledError
    error = cleanup.exception()
    if error is not None:
        if deferred_cancel:
            raise asyncio.CancelledError from None
        raise error
    if deferred_cancel:
        raise asyncio.CancelledError


class AssistantOutput:
    """Every path that puts assistant audio on the wire.

    Owns the output gate, the duck transport, the cue manager and the
    pre-baked earcons. ``stamp_stage`` is the wake loop's turn-timeline
    stamp; the listening chirp calls it so the stamp keeps its order
    against the write beneath it.
    """

    def __init__(
        self,
        cfg: Config,
        tts: TtsPlayout,
        ducker: Ducker | FanInDucker,
        cues: AudioCueManager | None,
        volume_coordinator: VolumeCoordinator,
        *,
        stamp_stage: Callable[[str], None],
    ) -> None:
        self._cfg = cfg
        self._tts = tts
        self._ducker = ducker
        self._cues = cues
        self._volume_coordinator = volume_coordinator
        self._stamp_stage = stamp_stage
        self._output_gate = AssistantOutputGate()
        # One admission authority for assistant audio, asked twice: the gate
        # refuses an episode that has not started yet, this hook refuses the
        # bytes of one that already had (issue #1913).
        tts.set_emission_admission(self.admission_refusal)
        # One-shot latch for the "cue requested but no cue manager"
        # WARN in play_cue — see that method for why it must not be
        # silent, and why it logs once rather than per-cue.
        self._warned_cues_unconfigured = False

        # Pre-render generated earcons once; synthesis is pure, so caching
        # the PCM keeps the cost off hot paths. Same shape
        # `TtsPlayout.write()` accepts: 24 kHz mono at the box's wire
        # width. The bake width comes from the SAME resolution the playout
        # writes at, asked once here, so the recipe's float render is
        # quantized on the wire's grid rather than flattened onto the S16
        # grid and promoted afterwards.
        earcon_wide = _tts_wire_is_wide()
        self._earcon_wide = earcon_wide
        self._chirp_on_pcm: bytes = _generate_listening_chirp(
            going_on=True, wide=earcon_wide,
        )
        self._chirp_off_pcm: bytes = _generate_listening_chirp(
            going_on=False, wide=earcon_wide,
        )
        self._chirp_on_profile = _synthetic_audio_profile(
            model="synthetic-listening-chirp",
            voice="wake_start",
            pcm=self._chirp_on_pcm,
            wide=earcon_wide,
        )
        self._chirp_off_profile = _synthetic_audio_profile(
            model="synthetic-listening-chirp",
            voice="turn_end",
            pcm=self._chirp_off_pcm,
            wide=earcon_wide,
        )
        self._mute_click_on_pcm: bytes = _generate_mute_click(
            going_on=True, wide=earcon_wide,
        )
        self._mute_click_off_pcm: bytes = _generate_mute_click(
            going_on=False, wide=earcon_wide,
        )
        self._mute_click_on_profile = _synthetic_audio_profile(
            model="synthetic-mute-click",
            voice="unmute",
            pcm=self._mute_click_on_pcm,
            wide=earcon_wide,
        )
        self._mute_click_off_profile = _synthetic_audio_profile(
            model="synthetic-mute-click",
            voice="mute",
            pcm=self._mute_click_off_pcm,
            wide=earcon_wide,
        )

    @property
    def gate(self) -> AssistantOutputGate:
        return self._output_gate

    def admission_refusal(self) -> str | None:
        """The one answer to "may assistant audio be heard right now?".

        Wired into `TtsPlayout.set_emission_admission`, so every emitter is
        asked when its bytes would leave, not when its task started (issue
        #1913). `MeasurementHold.pause_response` is the only caller that
        closes admission.
        """
        if self._output_gate.admission_paused:
            return "measurement_active"
        return None

    async def drain_inflight(self, *, timeout_sec: float) -> bool:
        """Wait out assistant audio that was already playing when PAUSE
        landed, so its tail cannot enter the window's first capture.

        `MeasurementHold` caps `timeout_sec` at
        `MEASUREMENT_INFLIGHT_DRAIN_SEC`. On timeout the pause and
        cleanup ownership stay armed and the detailed response reports
        ``drained=false`` while preserving ``result=ok`` for old callers.
        Strict callers refuse to capture; the correction caller may keep its
        explicit proceed-anyway policy and still sends RESUME.

        Returns without yielding to the event loop when the gate is idle.
        """
        if not self._output_gate.is_active:
            return True
        active_kind = self._output_gate.active_kind or "unknown"
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_sec):
                drained = await self._output_gate.drain_paused(timeout_sec)
        except TimeoutError:
            drained = False
        waited_ms = int((time.monotonic() - started) * 1000)
        if drained:
            log_event(
                logger,
                "measurement.inflight_drained",
                active_kind=active_kind,
                waited_ms=waited_ms,
            )
            return True
        log_event(
            logger,
            "measurement.inflight_drain_timeout",
            active_kind=self._output_gate.active_kind or active_kind,
            waited_ms=waited_ms,
            bound_sec=timeout_sec,
            detail=(
                "assistant audio still playing; the measurement window is "
                "armed but the caller must not begin a strict capture"
            ),
            level=logging.WARNING,
        )
        return False

    async def play_cue_admitted(self, slug: str) -> str:
        """Public wrapper for `play_cue`, callable via the control
        socket so external clients (jasper-control HTTP, the
        `jasper-cues play` CLI) can play cues through the daemon's
        fan-in-backed TtsPlayout.

        Answers `measurement_active` while a room-correction measurement
        window is open. Refusal is structural either way — the episode
        below cannot be admitted — so this early read exists only to keep
        that distinct code on the wire, which a plain `busy` would hide."""
        if not slug:
            return "missing_slug"
        if self._cues is None:
            return "cues_not_configured"
        from ..cues.registry import find as _find
        if _find(slug) is None:
            return "unknown_slug"
        refusal = self.admission_refusal()
        if refusal is not None:
            log_event(logger, "cue.skipped", reason=refusal, slug=slug)
            return refusal
        if self._output_gate.is_active:
            log_event(
                logger,
                "cue.play_busy",
                slug=slug,
                active_kind=self._output_gate.active_kind,
            )
            return "busy"
        episode = await self._output_gate.begin_if_idle("admin")
        if episode is None:
            log_event(
                logger,
                "cue.play_busy",
                slug=slug,
                active_kind=self._output_gate.active_kind,
            )
            return "busy"
        played = await self._play_cue_owned(slug, episode)
        return "ok" if played else "play_failed"

    async def play_dynamic_text(self, text: str) -> bool:
        """Speak arbitrary `text` through the cue manager, with
        snapshot-based duck/restore around the playback.

        Uses `CueDuck` rather than the daemon's `Ducker` because a cue is
        a brief, passive interruption: the user isn't adjusting volume
        mid-cue, so "music returns to exactly where it was" matters more
        than the remote-twist-wins behaviour `Ducker` is designed for.
        See `jasper/camilla.py:CueDuck`."""
        if self._cues is None:
            logger.warning("dynamic text play skipped: cues unavailable")
            return False
        # Ahead of prerender, which is a paid synthesis plus a disk write:
        # a closed window cannot admit the episode that would speak it.
        refusal = self.admission_refusal()
        if refusal is not None:
            log_event(logger, "dynamic_text.skipped", reason=refusal)
            return False
        try:
            if not await self._cues.prerender_text(text):
                logger.warning("dynamic text play failed: prerender failed")
                return False
        except Exception as e:  # noqa: BLE001
            logger.warning("dynamic text play failed: prerender failed: %s", e)
            return False
        episode = await self._output_gate.begin_if_idle("proactive")
        if episode is None:
            log_event(
                logger,
                "dynamic_text.skipped",
                reason=self.admission_refusal() or "output_active",
                active_kind=self._output_gate.active_kind,
            )
            return False

        def _episode_current() -> bool:
            return self._output_gate.is_current(episode)

        async def _speak() -> bool:
            return bool(await self._cues.speak_text_guarded(text, _episode_current))

        restore: Callable[[], Awaitable[None]] | None = None
        try:
            await self._prepare_feedback_loudness_context(kind="dynamic_text")
            if isinstance(self._ducker, FanInDucker):
                restore = self._ducker.restore
                played = False
                try:
                    await self._ducker.duck()
                    played = await _speak()
                except Exception as e:  # noqa: BLE001
                    logger.warning("dynamic text play failed: %s", e)
                return played
            owner = getattr(self._volume_coordinator, "volume_owner", None)
            if owner is None:
                # No fader owner — degrade to unducked playback rather
                # than crash. The user hears the cue over un-ducked music
                # which is loud but recoverable; better than silence.
                try:
                    return await _speak()
                except Exception as e:  # noqa: BLE001
                    logger.warning("dynamic text play failed: %s", e)
                    return False
            cue_duck = CueDuck(owner, self._cfg.duck_db)

            async def _restore_cue_duck() -> None:
                # Deliberately neutral context: CueDuck.__aexit__ only writes
                # its snapshot and never suppresses an exception. The caller
                # retains the original cancellation/error while the cleanup
                # task owns restore to a known outcome.
                await cue_duck.__aexit__(None, None, None)

            restore = _restore_cue_duck
            await cue_duck.__aenter__()
            try:
                return await _speak()
            except Exception as e:  # noqa: BLE001
                logger.warning("dynamic text play failed: %s", e)
                return False
        finally:
            try:
                if restore is None:
                    await self._finish_output_episode_after_drain(episode)
                else:
                    await self.finish_ducked_episode_after_drain(
                        episode,
                        restore,
                        cleanup_label="dynamic text",
                    )
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
                logger.warning("dynamic text drain cleanup failed: %s", e)

    async def play_cue(
        self,
        slug: str,
        *,
        episode: AssistantOutputEpisode | None = None,
    ) -> bool:
        """Best-effort cue playback, ducking music via CamillaDSP for the
        cue's duration. Without ducking the cue is drowned out by playing
        music; TTS-side level math alone cannot make it audible over a
        non-ducked stream.

        No tracker/volume-coordinator manipulation: those exist for
        multi-second voice sessions where the user may adjust volume mid-turn,
        and a ~6 s cue is short enough for plain duck/restore.

        The cue plays even if ducking fails — the usual cause is camilla
        restarting, in which case music is not playing through camilla either,
        so the cue is unducked but audible, and silence on a wake-blocking
        condition is the worse outcome. Ducker.restore short-circuits when the
        duck did not latch, so the finally is unconditional.

        ``episode`` is for the one caller that cannot let this method take
        its own admission: the research cancel timeout has to end the turn
        episode blocking the cue and take the cue's in the same lock hold
        (`AssistantOutputGate.hand_over_if_current`), or a queued turn
        wins the gap and the wake goes unanswered. A handed-in episode is
        this method's to release on EVERY exit, the unconfigured-cues one
        included — otherwise it leaks the gate and the speaker goes deaf to
        every later cue."""
        if self._cues is None:
            if episode is not None:
                await self._output_gate.end(episode)
            # Cues are how the user hears why the speaker did not respond.
            # With no cue manager the speaker is silent on every failure, so
            # make that state diagnosable in the journal. Once per daemon
            # lifetime: the condition is static config.
            if not self._warned_cues_unconfigured:
                self._warned_cues_unconfigured = True
                log_event(
                    logger,
                    "cue.skipped",
                    reason="cues_unconfigured",
                    slug=slug,
                    note=(
                        "no cue manager; failure cues will be SILENT for "
                        "this daemon run (check cue backend/API keys at "
                        "startup logs)"
                    ),
                    level=logging.WARNING,
                )
            return False
        if episode is None:
            episode = await self._output_gate.begin_if_idle("admin")
        if episode is None:
            log_event(
                logger,
                "cue.skipped",
                reason=self.admission_refusal() or "output_active",
                slug=slug,
                active_kind=self._output_gate.active_kind,
            )
            return False
        return await self._play_cue_owned(slug, episode)

    async def _play_cue_owned(
        self,
        slug: str,
        episode: AssistantOutputEpisode,
    ) -> bool:
        ducker = self._ducker
        played = False
        try:
            await self._prepare_feedback_loudness_context(kind="cue", slug=slug)
            try:
                await ducker.duck()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "cue %s: duck failed (cue will play unducked): %s",
                    slug, e,
                )
            try:
                played = await self._cues.play(slug)
            except Exception as e:  # noqa: BLE001
                logger.warning("cue %s play failed: %s", slug, e)
        finally:
            try:
                await self.finish_ducked_episode_after_drain(
                    episode,
                    ducker.restore,
                    cleanup_label=f"cue {slug}",
                )
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
                logger.warning("cue %s drain cleanup failed: %s", slug, e)
        return played

    async def finish_ducked_episode_after_drain(
        self,
        episode: AssistantOutputEpisode,
        restore: Callable[[], Awaitable[None]],
        *,
        cleanup_label: str,
    ) -> None:
        """Own physical drain, duck restore, and exact gate release as one."""

        async def _drain_restore_and_release() -> None:
            drain_base_error: BaseException | None = None
            try:
                drain_error = await _capture_cleanup_error(
                    lambda: wait_tts_drained_owned(self._tts),
                )
                if isinstance(drain_error, Exception):
                    logger.warning(
                        "%s drain cleanup failed: %s",
                        cleanup_label,
                        drain_error,
                    )
                else:
                    drain_base_error = drain_error
            finally:
                restore_base_error: BaseException | None = None
                try:
                    restore_error = await _capture_cleanup_error(restore)
                    if isinstance(restore_error, Exception):
                        logger.warning(
                            "%s restore failed: %s",
                            cleanup_label,
                            restore_error,
                        )
                    else:
                        restore_base_error = restore_error
                finally:
                    await self._output_gate.end(episode)
                if restore_base_error is not None:
                    raise restore_base_error
            if drain_base_error is not None:
                raise drain_base_error

        await _await_output_cleanup_owned(
            _drain_restore_and_release(),
            task_name=f"output-cleanup-{episode.kind}-{episode.id}",
        )

    async def _finish_output_episode_after_drain(
        self,
        episode: AssistantOutputEpisode,
    ) -> None:
        """Retain output ownership until accepted PCM is physically silent.

        Socket writes running in worker threads cannot be revoked by task
        cancellation. Once a cue path may have queued PCM, repeated caller
        cancellation is therefore deferred by an ``asyncio.wait`` ownership
        loop until one cleanup task reaches the playout deadline and releases
        the exact episode token.
        """

        async def _drain_and_release() -> None:
            try:
                await wait_tts_drained_owned(self._tts)
            finally:
                await self._output_gate.end(episode)

        await _await_output_cleanup_owned(
            _drain_and_release(),
            task_name=f"output-drain-{episode.kind}-{episode.id}",
        )

    async def play_mute_click(self, *, going_on: bool) -> None:
        """Best-effort. If the TTS stream isn't open or write fails,
        the visual feedback on the web UI is enough — never raise."""
        episode = await self._output_gate.begin_if_idle("feedback")
        if episode is None:
            log_event(
                logger,
                "mute_click.skipped",
                reason=self.admission_refusal() or "output_active",
                active_kind=self._output_gate.active_kind,
            )
            return
        try:
            await self._prepare_feedback_loudness_context(kind="mute_click")
            pcm = (
                self._mute_click_on_pcm
                if going_on else self._mute_click_off_pcm
            )
            profile = (
                self._mute_click_on_profile
                if going_on else self._mute_click_off_profile
            )
            try:
                await self._tts.write_segment(
                    pcm,
                    segment_kind="cue",
                    source_profile=profile,
                    pcm_wide=self._earcon_wide,
                )
            finally:
                await wait_tts_drained_owned(self._tts)
        except Exception as e:  # noqa: BLE001
            logger.warning("mic mute click failed: %s", e)
        finally:
            # A multi-command IPC write can fail after an accepted prefix.
            # The shared cleanup retains this episode over that physical tail.
            await self._finish_output_episode_after_drain(episode)


    async def _prepare_feedback_loudness_context(
        self,
        *,
        kind: str,
        slug: str | None = None,
    ) -> None:
        """Prime fan-in/outputd loudness context for standalone feedback.

        Reactive/proactive cues and mute clicks play outside the turn flow
        that would otherwise prepare this context, so they prime it here:
        content loudness before ducking, or the listening-level-derived
        silence target when the room is quiet. Failure is non-fatal — a cue at
        the wrong loudness beats a silent failure path.
        """
        try:
            await self.prepare_loudness()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as e:
            fields: dict[str, object] = {
                "kind": kind,
                "exc_type": type(e).__name__,
                "err": str(e),
                "level": logging.WARNING,
            }
            if slug:
                fields["slug"] = slug
            log_event(logger, "feedback_loudness.prepare_failed", **fields)


    async def listening_chirp(self, *, going_on: bool) -> None:
        """Best-effort. If the TTS stream isn't ready, the wake or
        end-of-turn happens anyway — never raise. PCM is pre-rendered
        in __init__ to keep this off the wake hot path."""
        try:
            pcm = self._chirp_on_pcm if going_on else self._chirp_off_pcm
            profile = (
                self._chirp_on_profile
                if going_on else self._chirp_off_profile
            )
            if going_on:
                self._stamp_stage("cue")
            await self._tts.write_segment(
                pcm,
                segment_kind="chirp",
                source_profile=profile,
                pcm_wide=self._earcon_wide,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("listening chirp failed: %s", e)

    async def prepare_loudness(self) -> None:
        provider, model, voice = active_voice_identity(self._cfg)
        tts_envelope = tts_envelope_lufs_for_level(
            self._volume_coordinator.get_listening_level(),
        )
        prepare_kwargs = {
            "provider": provider,
            "model": model,
            "voice": voice,
            "tts_envelope_lufs": tts_envelope,
        }
        # Attach the absolute volume context when the active TTS route
        # interprets it: the pre-DSP fan-in mix (solo/leader) or the confirmed
        # post-DSP outputd mix (a reconciled passive member). The same wire
        # message is sent either way — the post-DSP consumer owns the
        # structural downstream-is-zero fact. Ambiguous/legacy routes stay off.
        route_consumes_context = getattr(
            self._cfg, "duck_transport", ""
        ) == "fanin" and (
            tts_socket_feeds_pre_dsp_fanin(os.environ)
            or tts_socket_feeds_post_dsp_outputd(os.environ)
        )
        context_reader = (
            getattr(self._volume_coordinator, "effective_volume_context", None)
            if route_consumes_context
            else None
        )
        if callable(context_reader):
            try:
                volume_context = await context_reader()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                log_event(
                    logger,
                    "assistant_loudness.context_unavailable",
                    exc_type=type(exc).__name__,
                    detail=str(exc),
                    level=logging.WARNING,
                )
            else:
                prepare_kwargs.update(
                    canonical_volume_db=volume_context.canonical_db,
                    downstream_volume_db=volume_context.downstream_db,
                    context_tts_envelope_lufs=(
                        volume_context.tts_envelope_lufs
                    ),
                    muted=volume_context.muted,
                    context_stamp_boot_ns=volume_context.stamp_boot_ns,
                )
        await self._tts.prepare_assistant_context(
            **prepare_kwargs,
        )

    async def begin_turn_episode(
        self,
        current: AssistantOutputEpisode | None,
    ) -> AssistantOutputEpisode:
        """The turn's episode: the caller's own if it still holds the
        gate, otherwise a fresh one. The wake loop keeps the field so its
        teardown compares the snapshot it took, not a live read."""
        if current is not None and self._output_gate.is_current(current):
            return current
        return await self._output_gate.begin_turn()

    async def cancel_timeout_cue(
        self,
        surrendered: AssistantOutputEpisode | None,
    ) -> None:
        """Answer a wake the confirmation window's opener never released.

        The opener holds the turn episode, which `play_cue`'s own "admin"
        admission cannot preempt — it would skip the cue and leave this
        wake silent. Take that ownership away rather than lend the cue the
        opener's episode: nothing cancels the opener here, so it resumes
        into its own teardown, whose duck restore and gate release would
        cut a cue playing on a turn-kind episode. Both teardown paths
        re-ask `is_current` at each of their output actions, so a
        surrendered opener writes nothing and releases nothing. The
        succession is one lock hold, not an end followed by a begin: a
        `begin_turn` waiter queued on the idle signal would otherwise take
        the gate in between and the wake would go unanswered (NN-6).

        The handover refuses in two cases, and then nothing was ended: the
        opener's episode is still current — it still owns output, and its
        duck and its gate release are still its own to do — or it was
        already gone, and whoever holds the gate now owns them instead.
        Both leave `play_cue` to ask for its own admission.
        """
        cue_episode = (
            await self._output_gate.hand_over_if_current(
                surrendered, "admin",
            )
            if surrendered is not None else None
        )
        played = await self.play_cue(
            INTERNAL_ERROR_CUE_SLUG, episode=cue_episode,
        )
        if cue_episode is not None and not played:
            # Only on the handover, and only when no cue took the gate to
            # duck and restore: this arm ended the episode whose teardown
            # would have handed the opener's duck back, so it hands it
            # back itself. On a refusal the duck is not this arm's to
            # touch.
            await self._ducker.restore()
