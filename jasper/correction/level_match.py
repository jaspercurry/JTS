# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction-side adapter for the relay-closed level-match ramp (P2).

The pure staircase / settle / lock math lives in the shared kernel
(:mod:`jasper.audio_measurement.ramp`). This module is the correction-layer glue
that the kernel deliberately does not know about, per
``docs/HANDOFF-correction-revision-plan.md`` §3.1:

  * :class:`RelayLevelFeed` — the ``next_samples`` source the kernel awaits each
    tick, reading the phone's **batched, client-timestamped** level samples out
    of the relay ``status`` event, and posting the Pi's ramp-control signals back
    as **latched, idempotent** host events (the read-modify-write race note: a Pi
    host-event post and a phone level post interleave over the same last-write-
    wins ``event`` slot, so a stop/hold signal is re-posted until the phone echoes
    it, and every phone level batch carries the phone's own abort/armed state so a
    lost round trip never strands the flow).
  * :class:`MeasurementLevelLock` + :class:`LevelLockStore` — the lock is scoped
    **per mic-geometry step, not blanket per-session** (near-field baffle vs
    listening position differ ~15–25 dB at the mic for the same played level, so
    one lock reused across geometries blows past the window or starves SNR). The
    store keys on the geometry.
  * :func:`check_level_drift` — the drift check, computed on **raw
    (pre-``normalize_to_band``) band magnitudes** because normalization erases
    exactly the uniform shift the check exists to catch, and split by cause: a
    *uniform* per-band dB shift at the same geometry means the amp/volume moved
    (offer re-level); a geometry *change* expects a shift and must not fire that
    message; a *non-uniform* change at the same geometry is acoustic.

Everything here is host-mediated (docs/extensibility.md §1) and hardware-free:
inject a fake relay client + fake clock and the whole path is synthetically
testable. The on-device settle-cadence and iOS/Android AGC-freeze tuning are H1.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from jasper.audio_measurement.ramp import (
    LEVEL_EVENT_SCHEMA_VERSION,
    LevelSample,
    MeasurementRamp,
    RampController,
    RampData,
    RampState,
)
from jasper.log_event import log_event

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float, *, lo: float, hi: float) -> float:
    """Deploy-time knob reader (alignment._env_threshold pattern)."""
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return default
        if lo <= value <= hi:
            return value
    return default


# --- geometry ----------------------------------------------------------------


class MicGeometry(str, Enum):
    """The mic placement a lock is scoped to.

    Near-field (Layer A — phone at the baffle) and listening position (Layer B)
    differ by roughly 15–25 dB at the mic for the same played level, so a lock
    for one geometry must never be reused for the other. The flow re-ramps on
    every geometry transition (cheap once the kernel exists).
    """

    LISTENING_POSITION = "listening_position"
    NEAR_FIELD_DRIVER = "near_field_driver"


# --- per-geometry lock -------------------------------------------------------


@dataclass(frozen=True)
class MeasurementLevelLock:
    """A locked measurement level for ONE mic geometry.

    ``main_volume_db`` is the digital level the ramp settled on. ``gain_map_db``
    is the recovered chain gain ``G`` (``settled_mic_dbfs - main_volume_db``);
    together they say "at this geometry, this volume put the mic at
    ``main_volume_db + gain_map_db`` dBFS". ``noise_floor_dbfs`` is the phone's
    pre-ramp floor (context for the trust gate). ``agc_frozen`` records whether
    the reference is trustworthy (a ``False`` here means the lock came from the
    degraded manual-lock path and the drift rule is disabled for it).
    """

    geometry: str
    main_volume_db: float
    gain_map_db: float | None
    settled_mic_dbfs: float | None
    noise_floor_dbfs: float | None
    agc_frozen: bool = True
    schema_version: int = LEVEL_EVENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "geometry": self.geometry,
            "main_volume_db": round(self.main_volume_db, 2),
            "gain_map_db": (
                round(self.gain_map_db, 2) if self.gain_map_db is not None else None
            ),
            "settled_mic_dbfs": (
                round(self.settled_mic_dbfs, 2)
                if self.settled_mic_dbfs is not None
                else None
            ),
            "noise_floor_dbfs": (
                round(self.noise_floor_dbfs, 2)
                if self.noise_floor_dbfs is not None
                else None
            ),
            "agc_frozen": self.agc_frozen,
        }

    @classmethod
    def from_ramp(
        cls, geometry: str, data: RampData
    ) -> MeasurementLevelLock:
        """Build a lock from a terminal (LOCKED / MAXED_OUT) ramp result."""
        volume = (
            data.locked_main_volume_db
            if data.locked_main_volume_db is not None
            else data.current_main_volume_db
        )
        return cls(
            geometry=geometry,
            main_volume_db=float(volume),
            gain_map_db=data.gain_map_db,
            settled_mic_dbfs=data.settled_mic_dbfs,
            noise_floor_dbfs=data.noise_floor_dbfs,
            agc_frozen=data.agc_frozen,
        )


class LevelLockStore:
    """Session-scoped store of the current lock per mic geometry.

    Not one value for the whole session — a dict keyed by geometry, so a
    near-field lock and a listening-position lock coexist and neither clobbers
    the other. In-memory; the correction session owns its lifetime.
    """

    def __init__(self) -> None:
        self._locks: dict[str, MeasurementLevelLock] = {}

    def put(self, lock: MeasurementLevelLock) -> None:
        self._locks[lock.geometry] = lock
        log_event(
            logger,
            "level_lock_stored",
            geometry=lock.geometry,
            main_volume_db=f"{lock.main_volume_db:.1f}",
            gain_map_db=(
                f"{lock.gain_map_db:.1f}" if lock.gain_map_db is not None else ""
            ),
            agc_frozen=lock.agc_frozen,
        )

    def get(self, geometry: str) -> MeasurementLevelLock | None:
        return self._locks.get(geometry)

    def snapshot(self) -> dict[str, Any]:
        return {geo: lock.to_dict() for geo, lock in self._locks.items()}


# --- drift check (raw band levels; uniform-shift rule) -----------------------


class DriftVerdict(str, Enum):
    OK = "ok"
    AMP_MOVED = "amp_moved"  # uniform shift at same geometry → offer re-level
    ACOUSTIC = "acoustic"  # non-uniform change at same geometry (not a level drift)
    GEOMETRY_CHANGED = "geometry_changed"  # expected shift; do NOT flag as drift
    UNKNOWN = "unknown"  # can't decide (missing / mismatched bands)


@dataclass(frozen=True)
class DriftResult:
    verdict: DriftVerdict
    mean_shift_db: float | None
    max_band_deviation_db: float | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "mean_shift_db": (
                round(self.mean_shift_db, 2)
                if self.mean_shift_db is not None
                else None
            ),
            "max_band_deviation_db": (
                round(self.max_band_deviation_db, 2)
                if self.max_band_deviation_db is not None
                else None
            ),
            "message": self.message,
        }


def check_level_drift(
    reference_band_db: Sequence[float],
    current_band_db: Sequence[float],
    *,
    same_geometry: bool,
    agc_frozen: bool = True,
    uniform_shift_db: float | None = None,
    band_tolerance_db: float | None = None,
) -> DriftResult:
    """Classify the level relationship between two RAW per-band magnitude arrays.

    The inputs are **raw** band levels (``raw_magnitude_db`` from the replay
    artifacts, aggregated to matching bands) — NOT ``normalize_to_band``-ed
    curves, whose 200–1000 Hz band-mean is forced to 0 dB, erasing exactly the
    uniform shift this check exists to catch. Both arrays must describe the SAME
    bands in the same order.

    Rules (§3.1 drift check):
      * ``same_geometry`` False → a level shift is EXPECTED (near-field vs
        listening position differ 15–25 dB); return ``GEOMETRY_CHANGED`` and never
        flag it as an amp move.
      * ``agc_frozen`` False → the reference came from the degraded manual-lock
        path; the drift rule is disabled (``UNKNOWN``), never trust an
        AGC-compressed level as a reference map.
      * same geometry, |mean Δ| > ``uniform_shift_db`` AND every band within
        ``band_tolerance_db`` of the mean → the whole response moved uniformly →
        ``AMP_MOVED`` (offer re-level).
      * same geometry, a large but NON-uniform change → ``ACOUSTIC`` (a room /
        placement change, not a level drift).
      * otherwise ``OK``.

    Thresholds default to the deploy-time knobs (H1 supplies real numbers).
    """
    if uniform_shift_db is None:
        uniform_shift_db = _env_float(
            "JASPER_RAMP_DRIFT_UNIFORM_DB", 3.0, lo=0.0, hi=24.0
        )
    if band_tolerance_db is None:
        band_tolerance_db = _env_float(
            "JASPER_RAMP_DRIFT_BAND_TOL_DB", 2.0, lo=0.0, hi=24.0
        )

    ref = [float(x) for x in reference_band_db]
    cur = [float(x) for x in current_band_db]
    if not ref or len(ref) != len(cur):
        return DriftResult(
            verdict=DriftVerdict.UNKNOWN,
            mean_shift_db=None,
            max_band_deviation_db=None,
            message="drift check needs matching raw band arrays",
        )

    deltas = [c - r for c, r in zip(cur, ref)]
    mean_shift = sum(deltas) / len(deltas)
    max_dev = max(abs(dv - mean_shift) for dv in deltas)

    if not agc_frozen:
        return DriftResult(
            verdict=DriftVerdict.UNKNOWN,
            mean_shift_db=mean_shift,
            max_band_deviation_db=max_dev,
            message=(
                "level reference is AGC-compressed (agc_frozen=false); drift "
                "detection is disabled for this measurement"
            ),
        )

    if not same_geometry:
        return DriftResult(
            verdict=DriftVerdict.GEOMETRY_CHANGED,
            mean_shift_db=mean_shift,
            max_band_deviation_db=max_dev,
            message=(
                "mic geometry changed since the reference — a level shift is "
                "expected and is not an amplifier drift"
            ),
        )

    if abs(mean_shift) > uniform_shift_db and max_dev <= band_tolerance_db:
        return DriftResult(
            verdict=DriftVerdict.AMP_MOVED,
            mean_shift_db=mean_shift,
            max_band_deviation_db=max_dev,
            message=(
                f"the whole response shifted {mean_shift:+.1f} dB uniformly — the "
                "amplifier or volume likely moved; re-level before trusting this "
                "measurement"
            ),
        )

    if max_dev > band_tolerance_db and abs(mean_shift) <= uniform_shift_db:
        return DriftResult(
            verdict=DriftVerdict.ACOUSTIC,
            mean_shift_db=mean_shift,
            max_band_deviation_db=max_dev,
            message=(
                "the response changed shape (not a uniform level shift) — a room "
                "or placement change, not an amplifier drift"
            ),
        )

    return DriftResult(
        verdict=DriftVerdict.OK,
        mean_shift_db=mean_shift,
        max_band_deviation_db=max_dev,
        message="level is consistent with the reference",
    )


# --- relay feed: batched level samples in, latched ramp control out ----------
#
# The relay ``event`` slot is last-write-wins and the phone streams level batches
# into it continuously, so a single Pi host-event post is routinely reverted by
# the next phone post. Two mechanisms make ramp control robust against that race:
# (1) the Pi RE-POSTS its ramp-control signal on each tick (RelayLevelFeed.
#     post_ramp_signal is idempotent — re-posting the same field is harmless), and
# (2) the phone's every level batch carries its own abort/armed state as a
#     SUPERSET envelope (parse via phone_reported_abort), so a clobbered one-shot
#     host event never strands the flow.

StatusReader = Callable[[], dict[str, Any]]
HostEventPoster = Callable[[dict[str, Any]], Any]


def parse_level_batch(event: dict[str, Any]) -> list[LevelSample]:
    """Extract the phone's batched level samples from a relay ``status`` event.

    The phone posts ``{"level_batch": {"schema": N, "samples": [ {...}, ... ],
    "agc_frozen": bool, "aborted": bool, "armed": bool}}`` over the existing
    ``event`` envelope. Unknown / malformed payloads yield an empty list (the
    kernel treats a tick with no samples as "nothing new"), never an exception —
    the transport crosses the untrusted relay.
    """
    batch = event.get("level_batch")
    if not isinstance(batch, dict):
        return []
    raw_samples = batch.get("samples")
    if not isinstance(raw_samples, list):
        return []
    schema = batch.get("schema")
    if schema is not None and schema != LEVEL_EVENT_SCHEMA_VERSION:
        # A phone on a newer/older schema: refuse to misread it. Empty this tick.
        logger.warning(
            "level_batch schema mismatch: got %r expected %d",
            schema,
            LEVEL_EVENT_SCHEMA_VERSION,
        )
        return []
    out: list[LevelSample] = []
    # The phone's per-event agc_frozen/abort envelope is a superset that survives
    # a lost host-event round trip; apply the batch-level agc_frozen to any sample
    # that omitted it.
    batch_agc = batch.get("agc_frozen")
    for raw in raw_samples:
        if not isinstance(raw, dict):
            continue
        try:
            sample = LevelSample.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            continue
        if batch_agc is False and "agc_frozen" not in raw:
            sample = LevelSample(
                seq=sample.seq,
                t_client_ms=sample.t_client_ms,
                rms_dbfs=sample.rms_dbfs,
                peak_dbfs=sample.peak_dbfs,
                clip=sample.clip,
                agc_frozen=False,
            )
        out.append(sample)
    return out


def phone_reported_abort(event: dict[str, Any]) -> str | None:
    """Return the phone's abort reason if its event superset carries one.

    The phone's level batch carries its own abort state (the race-note superset),
    so a lost one-shot abort host-event doesn't strand the Pi. Also honors the
    top-level ``aborted`` the existing capture page posts.
    """
    batch = event.get("level_batch")
    if isinstance(batch, dict) and batch.get("aborted"):
        return str(batch.get("abort_reason") or "phone_aborted")
    if event.get("aborted"):
        return str(event.get("abort_reason") or event.get("reason") or "phone_aborted")
    return None


class RelayLevelFeed:
    """Turns relay polling into the kernel's ``next_samples`` source.

    Each ``next_samples()`` reads the freshest relay status (via the injected
    ``read_status``), dedupes samples by ``seq`` (the last-write-wins slot re-
    delivers the same batch until the phone posts a newer one), watches for a
    phone-reported abort, and returns only the new :class:`LevelSample` s. Ramp
    control host events are posted latched/idempotent via ``post_host_event``.
    """

    def __init__(
        self,
        *,
        read_status: StatusReader,
        post_host_event: HostEventPoster | None = None,
    ) -> None:
        self._read_status = read_status
        self._post_host_event = post_host_event
        self._last_seq = -1
        self.aborted_reason: str | None = None

    def _event(self) -> dict[str, Any]:
        try:
            status = self._read_status() or {}
        except Exception:  # noqa: BLE001 — a transient relay read must not crash
            logger.warning("relay status read failed during ramp", exc_info=True)
            return {}
        event = status.get("event") if isinstance(status, dict) else None
        return event if isinstance(event, dict) else {}

    async def next_samples(self) -> list[LevelSample]:
        event = self._event()
        abort = phone_reported_abort(event)
        if abort:
            self.aborted_reason = abort
            return []
        samples = parse_level_batch(event)
        fresh = [s for s in samples if s.seq > self._last_seq]
        if fresh:
            self._last_seq = max(s.seq for s in fresh)
        return fresh

    def post_ramp_signal(self, key: str, value: Any) -> None:
        """Post a latched, idempotent ramp-control host event (best-effort)."""
        if self._post_host_event is None:
            return
        try:
            self._post_host_event({"ramp": {key: value}})
        except Exception:  # noqa: BLE001 — the phone superset is the backstop
            logger.warning("ramp host-event post failed (%s)", key, exc_info=True)


# --- the session adapter ------------------------------------------------------


@dataclass
class LevelMatchOutcome:
    """The result of one geometry's level-match ramp."""

    geometry: str
    ramp: RampData
    lock: MeasurementLevelLock | None
    aborted_reason: str | None = None

    @property
    def locked(self) -> bool:
        return self.ramp.state in (RampState.LOCKED, RampState.MAXED_OUT)

    def snapshot(self) -> dict[str, Any]:
        return {
            "geometry": self.geometry,
            "ramp": self.ramp.snapshot(),
            "lock": self.lock.to_dict() if self.lock else None,
            "aborted_reason": self.aborted_reason,
        }


class LevelMatchSession:
    """Wires the kernel ramp to the relay for ONE geometry step.

    Host-mediated: the caller injects the volume get/set, the tone
    play/cancel, and the relay status-read / host-event-post — this class owns
    only the ramp orchestration and the per-geometry lock persistence. It never
    imports the correction daemon or touches CamillaDSP directly.
    """

    def __init__(
        self,
        *,
        session_id: str,
        store: LevelLockStore,
        config: MeasurementRamp | None = None,
    ) -> None:
        self.session_id = session_id
        self.store = store
        self.config = config or MeasurementRamp.from_env()
        self._controller: RampController | None = None

    async def run_for_geometry(
        self,
        geometry: str,
        *,
        get_main_volume_db: Callable[[], Awaitable[float]],
        set_main_volume_db: Callable[[float], Awaitable[Any]],
        play_continuous_tone: Callable[[], Awaitable[Any]],
        cancel_tone: Callable[[], None],
        read_status: StatusReader,
        post_host_event: HostEventPoster | None,
        noise_floor_dbfs: float | None,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> LevelMatchOutcome:
        """Ramp + lock the measurement level for ``geometry``.

        A terminal LOCKED / MAXED_OUT stores a :class:`MeasurementLevelLock` under
        the geometry key; ABORTED / CANCELLED / ERROR store nothing (the original
        listening level is restored by the kernel). A phone-reported abort seen in
        the feed cancels the ramp cleanly.
        """
        feed = RelayLevelFeed(
            read_status=read_status, post_host_event=post_host_event
        )
        controller = self._controller = RampController(
            session_id=self.session_id, config=self.config
        )

        async def next_samples() -> list[LevelSample]:
            samples = await feed.next_samples()
            if feed.aborted_reason is not None:
                # Latched cancel — re-posted each tick until the kernel exits.
                feed.post_ramp_signal("abort_ack", feed.aborted_reason)
                await controller.cancel()
            return samples

        data = await controller.run(
            get_main_volume_db=get_main_volume_db,
            set_main_volume_db=set_main_volume_db,
            play_continuous_tone=play_continuous_tone,
            cancel_tone=cancel_tone,
            next_samples=next_samples,
            noise_floor_dbfs=noise_floor_dbfs,
            clock=clock,
            sleep=sleep,
        )

        lock: MeasurementLevelLock | None = None
        if data.state in (RampState.LOCKED, RampState.MAXED_OUT):
            lock = MeasurementLevelLock.from_ramp(geometry, data)
            self.store.put(lock)
            # Latched terminal host event so the phone can stop the meter.
            feed.post_ramp_signal("state", data.state.value)

        outcome = LevelMatchOutcome(
            geometry=geometry,
            ramp=data,
            lock=lock,
            aborted_reason=feed.aborted_reason,
        )
        log_event(
            logger,
            "level_match_done",
            session=self.session_id,
            geometry=geometry,
            state=data.state.value,
            locked_db=(
                f"{data.locked_main_volume_db:.1f}"
                if data.locked_main_volume_db is not None
                else ""
            ),
        )
        return outcome

    async def lock_now(self) -> bool:
        """Manual lock (the user tapped Lock) — trust the user."""
        return await self._controller.lock() if self._controller else False

    async def cancel(self) -> bool:
        return await self._controller.cancel() if self._controller else False
