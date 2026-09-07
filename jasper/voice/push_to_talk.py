# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-voice's push-to-talk collaborator: the manual-mic runtime map,
which source (if any) is active, whether this daemon is push-to-talk-only,
and the hold-cap decision.

A plain collaborator called BY `WakeLoop` — no Protocol, no adapter, no
host. `WakeLoop` builds one `PushToTalk` at construction time and reads
and writes its public attributes directly; nothing here reads loop state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from jasper.log_event import log_event

from ..audio_io import MicCapture

logger = logging.getLogger("jasper.voice_daemon")

# Head-room a push-to-talk turn must leave the model to start answering
# after the button closes the user's input.
#
# `_idle_watchdog`'s pre-response timer is anchored at TURN OPEN, not at
# end-of-input, so the hold and the model's first-chunk latency share one
# `JASPER_IDLE_TIMEOUT_SEC` envelope. Its own docstring puts that latency
# at "3-5 s, sometimes longer" for Live API providers; 6 s covers the
# documented range with margin. Whatever the hold cap ends up being, it
# must fire this far below `idle_timeout_sec` or the watchdog reaps the
# turn before the model can speak — and the teardown cancels
# `_play_responses` BEFORE calling `end_input`, so the user gets no
# answer at all rather than a short one.
PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC = 6.0

# Floor for the derived push-to-talk hold cap, so an operator who sets a
# very low JASPER_IDLE_TIMEOUT_SEC still gets a usable button rather than
# a turn that closes before they finish a sentence. Below this the
# watchdog may win; `_ptt_input_cap_sec` says so once, loudly.
PTT_MIN_INPUT_CAP_SEC = 5.0

# Liveness-tick cadence for a push-to-talk-only speaker, which has no
# primary mic stream to prove the async loop is iterating. Must stay well
# under `Heartbeat`'s stale threshold (jasper/watchdog.py) or the unit's
# WatchdogSec=30s would reap a healthy daemon; 2 s leaves 2.5x margin
# while costing one wakeup per interval. The relationship is pinned by
# test_ptt_keepalive_stays_inside_heartbeat_stale_threshold.
#
# A tick proves the loop is iterating, not that the accessory is still
# delivering audio: `UdpMicCapture.frames()` has no timeout, so a dead
# sender (remote battery, out of range, adapter stall) blocks its
# manual-mic task forever while the tick keeps patting the watchdog — a
# dead remote reads as a healthy speaker. Issue #2243. A frame timeout
# here is not the fix: silence is a push-to-talk device's steady state,
# so frame flow cannot tell idle from dead. Connection state can, and
# that is the accessory reconciler's to publish.
PTT_KEEPALIVE_INTERVAL_SEC = 2.0

# Hard cap on user audio length within a single turn. Once the user
# has been speaking continuously for this long without an
# end-of-utterance silence, force-close the turn. Defends against
# stuck-on TVs / loud monologues that could otherwise hold the
# turn open indefinitely. Generous (30 s) so verbose questions and
# dictation-style use cases aren't clipped.
HARD_RECORDING_CAP_SEC = 30.0


class ManualMicRuntime:
    """Live state for one push-to-talk mic source.

    Manual mic sources are opened by the daemon from
    ``Config.manual_mic_sources`` and selected per turn by source id. They are
    session-audio-only: no wake detector, no wake-event capture ring, and no
    pre-roll from the room mic.
    """

    __slots__ = ("source_id", "mic", "device")

    def __init__(self, source_id: str, mic: MicCapture, device: str):
        self.source_id = source_id
        self.mic = mic
        self.device = device


async def keepalive_ticks() -> "AsyncIterator[None]":
    """Yield a liveness tick every PTT_KEEPALIVE_INTERVAL_SEC.

    Stands in for the primary mic's frame stream on a speaker that has
    no always-listening mic. `Heartbeat` only pats systemd when the
    progress sentinel is younger than its stale threshold, so the
    interval must stay comfortably under that or `WatchdogSec=30s`
    would reap a healthy daemon.

    Ticks UNCONDITIONALLY, exactly like a mic's `frames()`. Shutdown
    is the consumer's job: `run()`'s loop checks `_stop_event` on
    every iteration and, if a turn is in flight, awaits `_end_turn()`
    before returning — duck restore, `end_input`, turn telemetry, the
    done-listening chirp. A stop check HERE would end the iteration
    first, so `run()`'s stop branch never runs and a SIGTERM mid-hold
    would leave the music ducked and the turn unfinished.
    """
    while True:
        yield None
        await asyncio.sleep(PTT_KEEPALIVE_INTERVAL_SEC)


class PushToTalk:
    """Runtime state for a daemon's push-to-talk mic sources.

    Constructed once by `WakeLoop.__init__` from the resolved manual-mic
    runtimes and whether any wake legs were opened; `WakeLoop` reads and
    writes the public attributes directly for the life of the daemon.
    """

    def __init__(
        self, mics: list[ManualMicRuntime], *, have_wake_legs: bool,
    ) -> None:
        self.sources: dict[str, ManualMicRuntime] = {
            runtime.source_id: runtime for runtime in mics
        }
        self.active_source: str | None = None
        # Push-to-talk-only is a DERIVED runtime state, not a declared or
        # config-inferred one: this daemon resolved zero wake legs and holds
        # at least one manual mic source, so every turn it can ever open is a
        # button turn. Derived from what was actually opened rather than
        # inferred from config — `cfg.mic_device` defaults to the literal
        # "Array", so "empty mic_device" never fires on a real box — and it
        # composes with the mic-unplugged case without knowing anything about
        # install tiers.
        self.only: bool = not have_wake_legs and bool(self.sources)
        self._cap_warned: bool = False

    def input_cap_sec(self, idle_timeout_sec: float) -> float:
        """How long a held button may hold the user's input open.

        Not simply ``HARD_RECORDING_CAP_SEC``. ``_idle_watchdog``'s
        pre-response timer is anchored at turn open and fires at
        ``JASPER_IDLE_TIMEOUT_SEC`` (default 20 s) when no model chunk has
        arrived — and none can while input is still open, because
        ``last_activity_at()`` tracks *model* activity and stays at the
        turn-start value. So the 30 s cap is unreachable at the shipped
        default: the watchdog wins by ~10 s, and because ``_end_turn`` cancels
        ``_play_responses`` before it calls ``end_input``, the user gets no
        answer at all rather than a truncated one.

        Deriving the cap from the same ``idle_timeout_sec`` the watchdog uses
        keeps the two in step when an operator retunes either, and aims to
        leave the model ``PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC`` to start
        speaking after the cap closes input.

        ``PTT_MIN_INPUT_CAP_SEC`` can defeat that aim, because the floor is a
        constant while the watchdog is not: a low enough ``idle_timeout_sec``
        walks the watchdog down through the floor (see that constant's
        comment). Two degraded bands result, each warned once per daemon:

        * ``cap < idle_timeout < cap + allowance`` — the cap still fires
          first but leaves the model less than the allowance, so a slow first
          chunk loses the answer. ``event=manual_mic.idle_timeout_too_low``.
        * ``idle_timeout <= cap`` — the watchdog fires first and the cap is
          unreachable, so every long hold loses its answer whatever the chunk
          speed. ``event=manual_mic.hold_cap_unreachable``.

        The floor stands in both — a usable button beats one that closes
        mid-sentence — because the remedy is the operator's timeout, which
        both events name in ``needs_sec``.

        ``idle_timeout_sec`` is guaranteed > 0 (``Config._validate`` rejects
        anything else at daemon start), so there is no "watchdog disabled"
        case to reason about.
        """
        headroom = idle_timeout_sec - PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC
        cap = min(HARD_RECORDING_CAP_SEC, max(PTT_MIN_INPUT_CAP_SEC, headroom))
        needs = cap + PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC
        # One latch for both: the two bands are mutually exclusive by
        # construction, and `idle_timeout_sec` is fixed for the daemon's
        # life, so at most one can ever apply.
        if not self._cap_warned:
            common: dict[str, Any] = {
                "cap_sec": f"{cap:.1f}",
                "idle_timeout_sec": f"{float(idle_timeout_sec):.1f}",
                "needs_sec": f"{needs:.1f}",
            }
            if cap >= idle_timeout_sec:
                self._cap_warned = True
                log_event(
                    logger,
                    "manual_mic.hold_cap_unreachable",
                    **common,
                    detail=(
                        "the idle watchdog reaps a held button BEFORE the "
                        "hold cap can close input, so every long hold ends "
                        "with no answer at all; raise "
                        "JASPER_IDLE_TIMEOUT_SEC to at least needs_sec"
                    ),
                    level=logging.WARNING,
                )
            elif needs > idle_timeout_sec:
                self._cap_warned = True
                log_event(
                    logger,
                    "manual_mic.idle_timeout_too_low",
                    **common,
                    detail=(
                        "raise JASPER_IDLE_TIMEOUT_SEC to at least "
                        "needs_sec; a long hold may end with no answer"
                    ),
                    level=logging.WARNING,
                )
        return cap

    def hold_cap_exceeded(
        self, elapsed: float, idle_timeout_sec: float,
    ) -> float | None:
        """Forward one push-to-talk frame. The button owns end-of-input.

        Reached from ``_handle_session_frame`` when
        ``_manual_endpoint_this_turn`` is set. Deliberately does NOT run
        local Silero:

        * ``END_OF_UTTERANCE_SILENCE_SEC`` would call ``end_input()``
          after 0.8 s of detected silence — while the user is still
          holding the button. Release already does exactly that, so
          Silero here is a second writer of "input is over" and the
          faster of the two wins.
        * ``NO_SPEECH_ABORT_SEC`` would end the turn outright 5 s into a
          held button. A user who presses and then gathers their thought
          is not a false wake; there was no wake to be false.

        What replaces them is ``_ptt_input_cap_sec``, and something must:
        a button held but never released (wedged under a cushion, a
        release event the accessory never sends) would otherwise hold the
        duck, the LLM session, and the mic open until ``_idle_watchdog``
        reaps the turn — and that teardown cancels ``_play_responses``
        before asking the model anything, so the user hears nothing.
        Closing input at the cap turns that into an answer to what was
        said so far.

        The cap only covers a button whose frames keep arriving: this method
        is its sole evaluator and runs per frame, so if the frames stop
        instead — BLE drop mid-hold, adapter killed — the cap never runs. The
        source's ``frames()`` is an untimed queue read
        (``UdpMicCapture.frames``) and the primary mic loop does not feed a
        button turn, so ``_idle_watchdog`` reaps it; ``_end_turn`` still
        finalises it through the ``_manual_endpoint_this_turn`` term in its
        ``end_input()`` gate, and the operator gets
        ``event=turn.silent_response reason=hold_timeout`` rather than the
        wake-turn ``reason=recording_timeout``.
        """
        cap = self.input_cap_sec(idle_timeout_sec)
        if elapsed >= cap:
            # `_input_ended` gates re-entry: once set, subsequent
            # held-button frames are dropped by `_handle_session_frame`'s
            # input-closed branch and never reach here, so this fires at
            # most once per turn.
            log_event(
                logger,
                "manual_mic.hold_cap",
                source=self.active_source or "primary",
                cap_sec=f"{cap:.1f}",
                idle_timeout_sec=f"{float(idle_timeout_sec):.1f}",
                level=logging.WARNING,
            )
            return cap
        return None
