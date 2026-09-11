# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-voice's wake-leg fan-in: which leg heard the wake word.

Owns the live per-leg detection state — the `LegRuntime` map, each leg's
recent score, the fuser-decided firing threshold, the shared OR-gate lock
and refractory latch, the capture rings and the acoustic condition the
thresholds key on — and turns a scored frame into at most one `WakeFire`
per user attempt.

A plain collaborator called BY `WakeLoop`, in the shape of
`voice/push_to_talk.py`: no Protocol, no adapter, no back-reference.
`WakeLoop` builds one at construction time, reads and writes its public
attributes directly, and owns everything a fire leads to — the pre-roll
freeze, the acquire buffer, peer arbitration, cues and the turn.

The leg *vocabulary* (tokens, ports, kinds) lives in `jasper.wake_legs`;
this module is the runtime that consumes it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jasper.log_event import log_event

from ..config import Config
from ..mic_capture import MicCapture
from ..wake_condition_context import AMBIENT_FLOOR_DBFS, classify_condition
from ..wake_conditions import DEFAULT_CONDITION
from ..wake_events import CAPTURE_POST_SEC, CAPTURE_PRE_SEC
from ..wake_fusion import WakeFuser
from ..wake_legs import LegSpec, wake_input_legs
from .wake_telemetry import LEG_DB, LegFireScore

logger = logging.getLogger("jasper.voice_daemon")

# Acoustic tail margin after the output owner has drained a turn.
WAKE_REFRACTORY_SEC = 0.2

# Per-leg score-freshness window. When a leg fires, another leg's most-
# recent score counts toward `fired_legs` (and the per-leg log line) only
# if it landed within this window — so a stream that stopped feeding (e.g.
# the bridge died) surfaces as "none" rather than lying with a stale
# score. 4x MicCapture's 80 ms frame period.
WAKE_STALE_SCORE_SEC = 0.32

# How often the WAKE loop recomputes the acoustic condition the fuser keys
# on. The fire gate reads a cached `condition`; this bounds its staleness
# while keeping the ring-noise-floor cost off the per-frame path
# (recompute ~1x/s, not ~12x/s/leg). Conditions — music starting, the room
# going quiet — change on a human timescale, so ~1 s is ample.
CONDITION_REFRESH_SEC = 1.0

# Per-leg wake-telemetry capture-ring depth, in frames. Sized to the
# (pre + post) capture window plus a safety margin: a 4 + 2 = 6 s window
# with ~2 s slack for the post-fire collection window, so a snapshot
# never runs off the end of the ring. One ring per leg is allocated at
# the run() wiring site and handed to its LegRuntime.
CAPTURE_RING_FRAMES = int(
    ((CAPTURE_PRE_SEC + CAPTURE_POST_SEC) * MicCapture.OUTPUT_RATE
     / MicCapture.OUTPUT_FRAME_SAMPLES) + 25
)


def _frame_rms_dbfs(frame) -> float | None:
    """Waveform RMS in dBFS for a single int16 mic frame.

    Cheap (≤80 µs per 1280-sample frame on Pi 5). Returns None on any
    error so callers fall through rather than crashing on a malformed
    frame.

    Reference: full-scale int16 is ±32768; RMS of full-scale sine
    ≈ 23170, so a -3 dBFS signal reads ~16384 RMS.
    """
    try:
        import numpy as _np  # local — keep module import cheap
        arr = _np.asarray(frame, dtype=_np.float32)
        if arr.size == 0:
            return None
        rms = float(_np.sqrt(_np.mean(arr * arr)))
        if rms <= 0.0:
            return -120.0  # digital silence floor
        return 20.0 * _np.log10(rms / 32768.0)
    except Exception:  # noqa: BLE001
        return None


def _ring_noise_floor_dbfs(ring, *, percentile: float = 25.0) -> float | None:
    """Ambient noise floor (dBFS) from a wake capture ring.

    A low percentile of the ring's per-frame RMS: the wake utterance is a
    minority of the ~6 s window, so the quieter frames approximate the room
    background. Computed once at fire time (never per frame), it splits
    "quiet" from "ambient" for the condition estimator. Returns None for an
    empty/absent ring or any error — telemetry must never break the wake
    fire path, and the caller treats None as "can't tell" (-> quiet).
    """
    if not ring:
        return None
    try:
        import numpy as _np  # local — keep module import cheap
        levels = [r for f in ring if (r := _frame_rms_dbfs(f)) is not None]
        if not levels:
            return None
        return float(_np.percentile(levels, percentile))
    except Exception:  # noqa: BLE001
        return None


def _snapshot_ring(ring: deque, n_frames: int) -> bytes | None:
    """Concatenate the last `n_frames` of the ring, or None when it is
    empty (e.g. the AEC OFF leg in single-stream mode)."""
    if not ring:
        return None
    # Fewer than n_frames early in startup, before the ring fills.
    take = min(len(ring), n_frames)
    frames = list(ring)[-take:]
    # Each frame is a numpy int16 array.
    return b"".join(f.tobytes() for f in frames)


def _tail_frame_rms_dbfs(ring: "deque | None") -> float | None:
    """RMS in dBFS of the most-recent frame in `ring`, or None when the
    ring is empty or missing."""
    if ring is None or not ring:
        return None
    return _frame_rms_dbfs(ring[-1])


class LegRuntime:
    """Live state for one wake-detection leg.

    The set of legs is declared in `jasper.wake_legs`; adding a leg is a
    registry entry plus a config-driven construction in
    `jasper.voice.daemon_main`.
    """

    __slots__ = (
        "spec", "mic", "detector", "capture_ring",
        "shadow_vad", "recent_score", "recent_score_at",
    )

    def __init__(self, spec, mic, detector, capture_ring, shadow_vad=None):
        self.spec = spec
        self.mic = mic
        self.detector = detector
        self.capture_ring = capture_ring
        # Session-state shadow VAD — set only on the AEC-OFF leg. When
        # present, the leg loop scores it during SESSION for telemetry
        # (`_shadow_vad_score_raw`); other legs idle in SESSION.
        self.shadow_vad = shadow_vad
        # Most-recent raw wake score + the loop-clock time it was set.
        # Read at fire time so the wake event carries every leg's recent
        # peak, and to gate `fired_legs` on freshness.
        self.recent_score = 0.0
        self.recent_score_at = 0.0


# Which Config field carries each wake leg's mic device string. Kept here, a
# voice-daemon construction concern, rather than on the jasper.wake_legs
# registry, which stays a pure cross-process identity table. The token and
# field name deliberately skew: the chip-direct leg's token is "off" but its
# device var is cfg.mic_device_raw, the operator-facing "raw" vocabulary
# (JASPER_MIC_DEVICE_RAW). The reconciler sets and clears these vars from the
# JASPER_WAKE_LEG_* booleans; an empty string means the leg is not configured.
_LEG_DEVICE_ATTR: dict[str, str] = {
    "on": "mic_device",
    "off": "mic_device_raw",
    "dtln": "mic_device_dtln",
    "chip_aec_150": "mic_device_chip_aec_150",
    "chip_aec_210": "mic_device_chip_aec_210",
}


def configured_wake_legs(
    cfg: Config,
    *,
    wake_detection_supported: bool = True,
) -> list[tuple[LegSpec, str]]:
    """Decide which wake legs to build and each one's device string.

    Pure (no I/O) so it is unit-testable; run() layers mic-open and
    AsyncExitStack lifecycle on top. The "on" (AEC3/primary) leg carries
    session audio and the Tier-1 heartbeat and is normally always built; the
    AEC reconciler owns making its device present, or parking voice. Optional
    "off"/"dtln" legs are built only when their device var is non-empty, so
    voice never opens a UDP listener nobody feeds.

    Two things produce an empty plan — a box with real voice input (a paired
    remote's button) but no always-listening stream to detect wake on:

    * the install profile does not grant ``Capability.WAKE_DETECTION``; the
      caller reads the marker and passes ``wake_detection_supported``, and
      ``Config`` stays env-only;
    * ``jasper-aec-reconcile`` published "no local mic" while an accessory
      offers a manual source (ADR-0217).

    Building the primary leg anyway would open a card that is not present,
    ``run()`` would re-raise ``InputDeviceUnavailable``, and the daemon would
    park before reaching the accessory sources (issue #2205).

    Both facts in the second case are read from their writers, never guessed:
    ``local_mic_present`` from ``jasper-aec-reconcile``, owner of the
    voice-input gate (``JASPER_LOCAL_MIC_PRESENT``) — ``Config`` defaults it
    to the literal ``"Array"`` and the reconciler writes a real candidate name
    on no-mic paths, so deriving it from ``cfg.mic_device`` misreads a real
    box; ``manual_mic_sources`` from ``jasper-accessory-reconcile``
    (``JASPER_MANUAL_MIC_SOURCES``).

    Only an explicit ``False`` drops the leg. ``None`` — the reconciler never
    ran, or did not resolve a custom device — keeps current behaviour, which
    keeps "this speaker has no room mic" distinguishable from "the room mic
    should be here and isn't": the second still raises and parks loudly rather
    than downgrading a mic-bearing speaker to push-to-talk.
    """
    if not wake_detection_supported:
        return []
    if cfg.local_mic_present is False and cfg.manual_mic_sources:
        return []
    legs: list[tuple[LegSpec, str]] = []
    for spec in wake_input_legs():
        device = getattr(cfg, _LEG_DEVICE_ATTR[spec.token])
        if spec.token == "on" or device:
            legs.append((spec, device))
    return legs


@dataclass(frozen=True)
class WakeFire:
    """One accepted wake, handed back to `WakeLoop` to turn into a turn.

    `at_monotonic` is the fire instant, taken inside the OR-gate critical
    section so the turn timeline's wake anchor is not skewed by the
    telemetry work that follows it. `wake_event` is the `WakeTelemetry`
    payload, absent when the caller asked for no event capture.
    """

    at_monotonic: float
    score: float
    rms_dbfs: float | None
    wake_event: dict | None


class WakeLegs:
    """The OR-gated wake-detection legs and the one fire they agree on.

    Any leg crossing its threshold fires the wake event; a shared
    refractory latch plus `fire_lock` guarantees one user attempt = one
    wake event regardless of which leg(s) crossed first. Secondary legs
    are wake-detection-only — `WakeLoop` keeps the primary "on" stream as
    the canonical session audio source.
    """

    def __init__(
        self,
        legs: "list[LegRuntime]",
        *,
        music_dbfs: Callable[[], float | None],
    ) -> None:
        # Wake-detection legs, keyed by jasper.wake_legs token. Assembled
        # by jasper.voice.daemon_main, which opens each leg's mic under the
        # AsyncExitStack and builds its detector, capture ring and — for
        # "off" — a session shadow VAD.
        self.legs: dict[str, LegRuntime] = {
            leg.spec.token: leg for leg in legs
        }
        # A configured leg without a LEG_DB telemetry mapping would raise an
        # uncaught KeyError in the wake hot path, where telemetry must be
        # fail-soft; fail at startup instead of at fire time.
        _unmapped = [tok for tok in self.legs if tok not in LEG_DB]
        if _unmapped:
            raise RuntimeError(
                f"wake legs missing a LEG_DB telemetry mapping: "
                f"{sorted(_unmapped)} (add them to LEG_DB in "
                "voice/wake_telemetry.py)"
            )
        # The "on" leg is absent on a push-to-talk-only speaker: no
        # always-listening microphone, so `configured_wake_legs` planned no
        # legs. Every read site reachable in that mode is None-tolerant, and
        # the ring still gets a real deque so no reader special-cases it.
        _on = self.legs.get("on")
        self.capture_ring_on = (
            _on.capture_ring if _on is not None
            else deque(maxlen=CAPTURE_RING_FRAMES)
        )
        # Shared OR-gate lock across the parallel leg loops. Held only for
        # the critical section that sets refractory_until + reads the
        # other legs' recent scores. Without this, two legs could race to
        # fire the same wake event simultaneously.
        self.fire_lock: asyncio.Lock = asyncio.Lock()
        # The fire-decision seam: the single place a leg's fire threshold
        # is decided, so per-condition thresholds and any corroboration /
        # veto land here rather than in the parallel leg loops.
        # `condition` is the acoustic condition the fuser keys on.
        self.fuser: WakeFuser = WakeFuser()
        self.condition: str = DEFAULT_CONDITION
        # Loop-clock timestamp of the last condition recompute; 0.0 forces
        # a refresh on the first WAKE frame.
        self.condition_refreshed_at: float = 0.0
        # Extended past every fire and past each turn's teardown, so the
        # detector cannot re-fire on the TTS tail.
        self.refractory_until: float = 0.0
        # last_wake_at is daemon-lifetime and never nulled.
        self.last_wake_at: float | None = None
        # Derived by refresh_condition; `session_status` reads these as None
        # while the mic is not feeding the refresh (muted or a measurement
        # hold), since the refresh stops ticking then.
        self.idle_rms_dbfs: float | None = None
        self.input_last_above_floor_at: float | None = None
        self._music_dbfs = music_dbfs

    def reset_leg(self, source: str) -> None:
        """Drop every score and buffer this leg accumulated across a gap.

        A no-op for a source that is not a wake leg (a push-to-talk mic),
        which shares the caller's gap path.
        """
        rt = self.legs.get(source)
        if rt is None:
            return
        rt.detector.reset()
        rt.recent_score = rt.recent_score_at = 0.0
        if rt.capture_ring is not None:
            rt.capture_ring.clear()
        if rt.shadow_vad is not None:
            rt.shadow_vad.reset()

    def clear_rings(self) -> None:
        for rt in self.legs.values():
            if rt.capture_ring is not None:
                rt.capture_ring.clear()

    def snapshot(self, leg: str, n_frames: int) -> bytes | None:
        """Snapshot the trailing wake-event window for one configured leg."""
        runtime = self.legs.get(leg)
        if runtime is None:
            return None
        return _snapshot_ring(runtime.capture_ring, n_frames)

    def refresh_condition(self, now_loop: float) -> None:
        """Refresh `condition` (the acoustic condition the fuser keys on) at
        most once per CONDITION_REFRESH_SEC, so the per-frame fire gate works
        off a ~1 s-fresh condition without paying the ring-noise-floor cost
        every frame."""
        if (now_loop - self.condition_refreshed_at) < CONDITION_REFRESH_SEC:
            return
        # Stamp the timer BEFORE the recompute so a persistent failure retries
        # at ~1 Hz (not every frame). Keep the recompute fail-soft: the wake
        # path must never break because of ancillary condition estimation.
        self.condition_refreshed_at = now_loop
        try:
            noise_floor_dbfs = _ring_noise_floor_dbfs(self.capture_ring_on)
            self.condition = classify_condition(
                music_dbfs=self._music_dbfs(),
                noise_floor_dbfs=noise_floor_dbfs,
            ).condition
            self.idle_rms_dbfs = noise_floor_dbfs
            if noise_floor_dbfs is not None and noise_floor_dbfs > AMBIENT_FLOOR_DBFS:
                self.input_last_above_floor_at = time.time()
        except Exception:  # noqa: BLE001
            # Keep the last good condition: an unguarded raise here would
            # propagate out of the frame loop and stop wake detection.
            pass

    async def score_frame(
        self,
        frame,
        *,
        leg: str = "on",
        mic_muted: bool = False,
        capture_event: bool = False,
    ) -> WakeFire | None:
        """Score one frame on the named leg. Legs:
          - 'on'   → post-AEC3 BEST_A (primary, the session audio source)
          - 'off'  → chip-direct raw mic (no AEC)
          - 'dtln' → DTLN-aec output
          - 'chip_aec_150' / 'chip_aec_210' → the XVF3800 hardware-AEC ASR
                     beams (profile-selected and hardware-conditional)

        Always tracks the leg's recent peak. If the threshold is crossed
        AND this leg wins the OR-gate race against the other legs, returns
        one `WakeFire` with ALL legs' recent scores attached; otherwise
        None.

        Refractory here plus the caller's acquiring gate ensure one user
        attempt = one wake event, regardless of which leg(s) fire first."""
        # Refractory early-out before scoring: the previous wake's TTS may
        # still be bleeding into the mic.
        now_loop = asyncio.get_event_loop().time()
        if now_loop < self.refractory_until:
            return None

        # Keep the condition the fuser keys on fresh (~1x/s) so the
        # per-frame gate below works off a live condition.
        self.refresh_condition(now_loop)

        # Track the raw score regardless of threshold so another leg, when it
        # fires, can pull this leg's most-recent peak into the wake event.
        rt = self.legs.get(leg)
        if rt is None:
            return None  # unknown / unconfigured leg
        detector = rt.detector
        score = detector.score_frame(frame)
        rt.recent_score = score
        rt.recent_score_at = now_loop

        firing_threshold = self.fuser.effective_threshold(
            leg, self.condition, detector.threshold,
        )
        if score < firing_threshold:
            return None

        # Win the OR-gate race against the other legs' loops. The lock covers
        # only the critical section; the rest of the wake flow runs unlocked so
        # both loops stay responsive.
        async with self.fire_lock:
            if asyncio.get_event_loop().time() < self.refractory_until:
                # The other leg won the race while we awaited the
                # lock. Bow out — only one wake event per user attempt.
                return None
            # Win. Set refractory IMMEDIATELY so the other leg's next
            # frame backs off cleanly. The caller extends it again once the
            # fire has been served or refused.
            self.refractory_until = now_loop + WAKE_REFRACTORY_SEC
            fire_at_monotonic = time.monotonic()
            # `fired_legs` is which leg(s) crossed threshold at fire time.
            # A non-firing leg counts only if its most-recent score is
            # FRESH (within WAKE_STALE_SCORE_SEC, so a stream that stopped
            # feeding doesn't lie with a stale score) AND above that leg's
            # own threshold. `trigger_kind` records the winner.
            fired_set = {leg}
            for _name, _other in self.legs.items():
                if _name == leg:
                    continue
                if (now_loop - _other.recent_score_at) > WAKE_STALE_SCORE_SEC:
                    continue
                if _other.recent_score >= self.fuser.effective_threshold(
                    _name, self.condition, _other.detector.threshold,
                ):
                    fired_set.add(_name)
            fired_legs = ",".join(sorted(fired_set))

        # Reset ALL detectors after a wake fires. openWakeWord's
        # prediction smoothing keeps recent-activation state across
        # calls; without resetting, the post-fire baseline stays
        # elevated and music vocals or TTS-tail bleed can false-fire on
        # the next listening window. Every leg was elevated by the same
        # user utterance, so reset them all.
        for _other in self.legs.values():
            _other.detector.reset()

        # Per-leg score summary for the log — ONLY the legs this install
        # actually built, so a single-stream or non-chip-AEC install emits
        # no fields for legs it isn't running. "none" means an ACTIVE leg
        # whose last score is stale (its UDP stream dried up), distinct
        # from an unconfigured leg, which is simply absent.
        _score_fields: dict[str, Any] = {}
        for _n, _lr in self.legs.items():
            if _n != leg and (
                _lr.recent_score_at == 0.0
                or (now_loop - _lr.recent_score_at) > WAKE_STALE_SCORE_SEC
            ):
                _score_fields[f"score_{_n}"] = "none"
            else:
                _score_fields[f"score_{_n}"] = f"{_lr.recent_score:.2f}"
        log_event(
            logger,
            "wake.detected",
            leg=leg,
            **_score_fields,
            threshold=f"{firing_threshold:.2f}",
            fired=fired_legs,
        )
        # Marks the wake pipeline alive even if this attempt isn't served.
        self.last_wake_at = time.time()

        wake_event = None
        if capture_event:
            condition_ctx = classify_condition(
                music_dbfs=self._music_dbfs(),
                noise_floor_dbfs=_ring_noise_floor_dbfs(self.capture_ring_on),
            )
            wake_event = dict(
                leg=leg,
                score=score,
                now_loop=now_loop,
                legs={
                    _name: LegFireScore(
                        score=_rt.recent_score,
                        score_at=_rt.recent_score_at,
                        # Instantaneous mic RMS at fire-time from the last
                        # frame in this leg's capture ring — separates
                        # low-energy FPs from real attempts in offline
                        # review.
                        mic_rms_dbfs=_tail_frame_rms_dbfs(
                            _rt.capture_ring,
                        ),
                    )
                    for _name, _rt in self.legs.items()
                },
                firing_threshold=firing_threshold,
                fired_legs=fired_legs,
                condition=condition_ctx,
                mic_muted=mic_muted,
            )
        return WakeFire(
            at_monotonic=fire_at_monotonic,
            score=score,
            # Tertiary tiebreaker for the peering ranking function. SNR would
            # rank better but needs rolling-noise-floor state nothing tracks;
            # the ranker falls through to RMS when SNR is missing.
            rms_dbfs=_frame_rms_dbfs(frame),
            wake_event=wake_event,
        )
