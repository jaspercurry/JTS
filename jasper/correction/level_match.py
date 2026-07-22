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
    of the relay ``status`` event. The relay ``event`` slot is last-write-wins
    and the phone streams into it continuously, so ramp control is robust by
    construction, not by one-shot posts: the phone's every level batch carries
    its own ``armed`` / ``aborted`` / ``agc_frozen`` / ``agc_unattested`` state
    as a SUPERSET envelope (a clobbered one-shot host event never strands the
    flow), Pi-side
    abort acks re-post each tick while the ramp exits, and the terminal ramp
    state is re-posted until the relay's ``/status`` echoes it back (the Pi's
    pull-token status includes ``host_event``, so the read-modify-write revert
    race is *observable*, not assumed away). The feed also rate-limits its
    status reads — the kernel tick is ~100 Hz, the HTTP cadence must not be.
  * A **run token** scopes the feed to one ramp run: the token rides the
    ``level_ramp`` capture spec, the phone echoes it in every batch, and the
    feed ignores events carrying another run's token — a *previous* run's
    persisted slot (its final abort superset, its stale samples) can no longer
    insta-cancel or mis-feed a retry. A same-token ``seq`` *regression* (the
    phone page reloaded mid-ramp and restarted its counter) is treated as a new
    stream rather than dropped as stale.
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
    message; a *non-uniform* change at the same geometry is acoustic; a large
    mean shift with real band scatter is a suspected level shift (never
    reported as "consistent").

Everything here is host-mediated (docs/extensibility.md §1) and hardware-free:
inject a fake relay reader + fake clock and the whole path is synthetically
testable. The on-device settle-cadence and iOS/Android AGC-freeze tuning are H1.

P3b wiring notes (deliberate, so they are not forgotten):
  * ``read_status`` at production must be a CACHED background poller snapshot —
    never a blocking ``RelayClient.status()`` per call (the feed rate-limits to
    ``min_read_interval_s``, but a sync 15 s-timeout HTTP call inside the
    kernel's event loop is still the wrong shape; poll on a thread, share a
    dict). Same for ``post_host_event``.
  * A cap result satisfying the kernel's strict evidence policy is stored as an
    explicitly labeled ``bounded_low_level`` lock, never a normal in-window
    lock. A cap result without that proof stays MAXED_OUT, whose UI copy must
    branch on ``ramp.agc_frozen``: with ``agc_frozen=False`` the evidence is
    AGC-compressed and "raise your analog amp" may be wrong.
"""

from __future__ import annotations

import logging
import os
import time
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
    RampLockKind,
    RampState,
)
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# Failures a relay/status reader can realistically raise. RelayError subclasses
# RuntimeError; json decode errors are ValueError; a buggy injected reader adds
# Type/Attribute/LookupError. Named (not blind) per the lint contract.
_FEED_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    LookupError,
)


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
    pre-ramp floor (context for the trust gate). ``lock_kind`` distinguishes an
    ordinary in-window lock, a manual lock, and the evidence-backed bounded-low
    cap policy. The settled SNR, preferred-window shortfall, and sample spread
    keep that degraded decision observable. ``agc_frozen`` records whether the
    reference is trustworthy (a ``False`` here means the lock came from the
    degraded manual-lock path and the drift rule is disabled for it).
    """

    geometry: str
    main_volume_db: float
    gain_map_db: float | None
    settled_mic_dbfs: float | None
    noise_floor_dbfs: float | None
    lock_kind: RampLockKind = RampLockKind.IN_WINDOW
    settled_snr_db: float | None = None
    window_shortfall_db: float | None = None
    settled_spread_db: float | None = None
    agc_frozen: bool = True
    schema_version: int = LEVEL_EVENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "geometry": self.geometry,
            "lock_kind": self.lock_kind.value,
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
            "settled_snr_db": (
                round(self.settled_snr_db, 2)
                if self.settled_snr_db is not None
                else None
            ),
            "window_shortfall_db": (
                round(self.window_shortfall_db, 2)
                if self.window_shortfall_db is not None
                else None
            ),
            "settled_spread_db": (
                round(self.settled_spread_db, 2)
                if self.settled_spread_db is not None
                else None
            ),
            "agc_frozen": self.agc_frozen,
        }

    @classmethod
    def from_ramp(cls, geometry: str, data: RampData) -> MeasurementLevelLock:
        """Build a lock from a terminal ``LOCKED`` ramp result.

        ``agc_frozen`` here is sourced from ``data.agc_trusted``, not the raw
        wire-level ``data.agc_frozen`` — the two agree for an ordinary
        browser-attested run, but an unattested (iOS/WebKit) run that passed
        the empirical slope check has ``agc_frozen=False`` at the wire level
        (by design, for mixed-version safety) while ``agc_trusted`` is True.
        Sourcing from ``agc_trusted`` is what makes a verified-unattested lock
        behave identically to an attested one for every downstream consumer of
        this field (the drift check below, the bounded-low-lock policy).
        """
        volume = (
            data.locked_main_volume_db
            if data.locked_main_volume_db is not None
            else data.current_main_volume_db
        )
        return cls(
            geometry=geometry,
            main_volume_db=float(volume),
            lock_kind=data.lock_kind or RampLockKind.IN_WINDOW,
            gain_map_db=data.gain_map_db,
            settled_mic_dbfs=data.settled_mic_dbfs,
            noise_floor_dbfs=data.noise_floor_dbfs,
            settled_snr_db=data.settled_snr_db,
            window_shortfall_db=data.window_shortfall_db,
            settled_spread_db=data.settled_spread_db,
            agc_frozen=data.agc_trusted,
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
            lock_kind=lock.lock_kind.value,
            gain_map_db=(
                f"{lock.gain_map_db:.1f}" if lock.gain_map_db is not None else ""
            ),
            agc_frozen=lock.agc_frozen,
        )

    def discard(self, geometry: str) -> None:
        """Forget one invalidated geometry without disturbing sibling locks."""

        self._locks.pop(str(geometry), None)

    def get(self, geometry: str) -> MeasurementLevelLock | None:
        return self._locks.get(geometry)

    def snapshot(self) -> dict[str, Any]:
        return {geo: lock.to_dict() for geo, lock in self._locks.items()}


# --- drift check (raw band levels; uniform-shift rule) -----------------------


class DriftVerdict(str, Enum):
    OK = "ok"
    AMP_MOVED = "amp_moved"  # uniform shift at same geometry → offer re-level
    # Large mean shift WITH real band scatter: probably a level change layered
    # on an acoustic one — never reported as "consistent" (the review's
    # fall-through-to-OK hole), but phrased more cautiously than AMP_MOVED.
    LEVEL_SHIFT_SUSPECTED = "level_shift_suspected"
    ACOUSTIC = "acoustic"  # non-uniform change at same geometry (not a level drift)
    GEOMETRY_CHANGED = "geometry_changed"  # expected shift; do NOT flag as drift
    UNKNOWN = "unknown"  # can't decide (missing / mismatched bands / AGC ref)


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
                round(self.mean_shift_db, 2) if self.mean_shift_db is not None else None
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
        path; the drift rule is disabled (``UNKNOWN``) — never trust an
        AGC-compressed level as a reference map.
      * same geometry, |mean Δ| > ``uniform_shift_db`` AND every band within
        ``band_tolerance_db`` of the mean → the whole response moved uniformly →
        ``AMP_MOVED`` (offer re-level).
      * same geometry, |mean Δ| > ``uniform_shift_db`` with band scatter beyond
        the tolerance → ``LEVEL_SHIFT_SUSPECTED``: real re-measures carry ≥2 dB
        scatter, so a genuine amp move rarely reads perfectly uniform — this
        quadrant must never fall through to an "everything is consistent" OK.
      * same geometry, a large but non-uniform change with a small mean →
        ``ACOUSTIC`` (a room / placement change, not a level drift).
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

    uniform = max_dev <= band_tolerance_db
    if abs(mean_shift) > uniform_shift_db:
        if uniform:
            return DriftResult(
                verdict=DriftVerdict.AMP_MOVED,
                mean_shift_db=mean_shift,
                max_band_deviation_db=max_dev,
                message=(
                    f"the whole response shifted {mean_shift:+.1f} dB uniformly "
                    "— the amplifier or volume likely moved; re-level before "
                    "trusting this measurement"
                ),
            )
        return DriftResult(
            verdict=DriftVerdict.LEVEL_SHIFT_SUSPECTED,
            mean_shift_db=mean_shift,
            max_band_deviation_db=max_dev,
            message=(
                f"the response moved {mean_shift:+.1f} dB overall but not "
                "uniformly — likely a level change combined with an acoustic "
                "change; consider re-leveling before trusting comparisons"
            ),
        )

    if not uniform:
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

StatusReader = Callable[[], dict[str, Any]]
HostEventPoster = Callable[[dict[str, Any]], Any]


def parse_level_batch(
    event: dict[str, Any],
    *,
    run_token: str = "",
    on_schema_mismatch: Callable[[Any], None] | None = None,
) -> list[LevelSample]:
    """Extract the phone's batched level samples from a relay ``status`` event.

    The phone posts ``{"level_batch": {"schema": N, "run_token": "...",
    "samples": [ {...}, ... ], "agc_frozen": bool, "armed": bool,
    "aborted": bool}}`` over the existing ``event`` envelope. Unknown /
    malformed payloads yield an empty list (the kernel treats a tick with no
    samples as "nothing new"), never an exception — the transport crosses the
    untrusted relay. A non-empty ``run_token`` scopes parsing to one ramp run:
    batches carrying a different (or no) token are another run's stale slot and
    are ignored entirely. A schema mismatch is reported through
    ``on_schema_mismatch`` when given (the feed latches its warning — a stale
    slot re-read every poll must not re-warn every tick), else logged at DEBUG.
    """
    batch = event.get("level_batch")
    if not isinstance(batch, dict):
        return []
    if run_token and str(batch.get("run_token") or "") != run_token:
        return []  # another run's slot — not ours
    raw_samples = batch.get("samples")
    if not isinstance(raw_samples, list):
        return []
    schema = batch.get("schema")
    if schema is not None and schema != LEVEL_EVENT_SCHEMA_VERSION:
        # A phone on a newer/older schema: refuse to misread it.
        if on_schema_mismatch is not None:
            on_schema_mismatch(schema)
        else:
            logger.debug(
                "level_batch schema mismatch: got %r expected %d",
                schema,
                LEVEL_EVENT_SCHEMA_VERSION,
            )
        return []
    out: list[LevelSample] = []
    # The phone's per-event agc_frozen/agc_unattested/abort envelope is a
    # superset that survives a lost host-event round trip; apply the
    # batch-level flags to any sample that omitted them.
    batch_agc = batch.get("agc_frozen")
    batch_unattested = batch.get("agc_unattested")
    for raw in raw_samples:
        if not isinstance(raw, dict):
            continue
        try:
            sample = LevelSample.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            continue
        agc_frozen = False if (batch_agc is False and "agc_frozen" not in raw) else sample.agc_frozen
        agc_unattested = (
            True
            if (batch_unattested is True and "agc_unattested" not in raw)
            else sample.agc_unattested
        )
        if agc_frozen != sample.agc_frozen or agc_unattested != sample.agc_unattested:
            sample = LevelSample(
                seq=sample.seq,
                t_client_ms=sample.t_client_ms,
                rms_dbfs=sample.rms_dbfs,
                peak_dbfs=sample.peak_dbfs,
                clip=sample.clip,
                agc_frozen=agc_frozen,
                agc_unattested=agc_unattested,
            )
        out.append(sample)
    return out


def phone_reported_abort(event: dict[str, Any], *, run_token: str = "") -> str | None:
    """Return the phone's abort reason if its event superset carries one.

    The phone's level batch carries its own abort state (the race-note superset),
    so a lost one-shot abort host-event doesn't strand the Pi. With a
    ``run_token`` set, ONLY a matching-token batch abort counts — a previous
    run's persisted abort superset must not insta-cancel a retry. The legacy
    top-level ``aborted`` (the classic capture page's form) is honored only when
    no token is in play, because it cannot be scoped to a run.
    """
    batch = event.get("level_batch")
    if isinstance(batch, dict):
        token_ok = not run_token or str(batch.get("run_token") or "") == run_token
        if token_ok and batch.get("aborted"):
            return str(batch.get("abort_reason") or "phone_aborted")
    if not run_token and event.get("aborted"):
        return str(event.get("abort_reason") or event.get("reason") or "phone_aborted")
    return None


def phone_reported_armed(event: dict[str, Any], *, run_token: str = "") -> bool:
    """True when the phone's superset (or the classic armed event) says armed.

    Token-scoped like the abort: with a run token set, only a matching batch's
    ``armed`` counts, so a previous run's stale slot cannot arm a new ramp.
    """
    batch = event.get("level_batch")
    if isinstance(batch, dict):
        token_ok = not run_token or str(batch.get("run_token") or "") == run_token
        if token_ok and batch.get("armed"):
            return True
    return bool(not run_token and event.get("armed"))


class RelayLevelFeed:
    """Turns relay polling into the kernel's ``next_samples`` source.

    Each ``next_samples()`` reads the freshest relay status (via the injected
    ``read_status``, rate-limited to ``min_read_interval_s`` so the kernel's
    ~100 Hz tick never becomes an HTTP cadence), dedupes samples by ``seq``
    (the last-write-wins slot re-delivers the same batch until the phone posts
    a newer one), treats a same-token seq *regression* as a fresh stream (page
    reload), watches for a phone-reported abort, and returns only the new
    :class:`LevelSample` s. Warnings are latched — a down relay or a stale
    mismatched-schema slot logs once per state change, not per tick.
    """

    def __init__(
        self,
        *,
        read_status: StatusReader,
        post_host_event: HostEventPoster | None = None,
        run_token: str = "",
        monotonic: Callable[[], float] = time.monotonic,
        min_read_interval_s: float = 0.25,
    ) -> None:
        self._read_status = read_status
        self._post_host_event = post_host_event
        self.run_token = run_token
        self._monotonic = monotonic
        self._min_read_interval_s = min_read_interval_s
        self._last_read_time: float | None = None
        self._last_seq = -1
        self._read_failing = False
        self._warned_schema: Any = None
        self.aborted_reason: str | None = None

    def _on_schema_mismatch(self, schema: Any) -> None:
        if schema != self._warned_schema:
            self._warned_schema = schema
            logger.warning(
                "level_batch schema mismatch: got %r expected %d (latched — "
                "further identical mismatches are silent)",
                schema,
                LEVEL_EVENT_SCHEMA_VERSION,
            )

    def _event(self) -> dict[str, Any]:
        try:
            status = self._read_status() or {}
        except _FEED_ERRORS:
            if not self._read_failing:
                self._read_failing = True
                logger.warning(
                    "relay status read failed during ramp (latched — further "
                    "failures are silent until recovery)",
                    exc_info=True,
                )
            return {}
        if self._read_failing:
            self._read_failing = False
            logger.info("relay status read recovered")
        event = status.get("event") if isinstance(status, dict) else None
        return event if isinstance(event, dict) else {}

    def check_armed(self) -> bool:
        """Read the slot once (rate-limited) and report the phone's armed state.

        Used by the adapter's pre-ramp gate; does not consume samples (seq dedup
        starts with the first ``next_samples`` call)."""
        if not self._may_read():
            return False
        return phone_reported_armed(self._event(), run_token=self.run_token)

    def _may_read(self) -> bool:
        now = self._monotonic()
        if (
            self._last_read_time is not None
            and now - self._last_read_time < self._min_read_interval_s
        ):
            return False
        self._last_read_time = now
        return True

    async def next_samples(self) -> list[LevelSample]:
        if not self._may_read():
            return []
        event = self._event()
        abort = phone_reported_abort(event, run_token=self.run_token)
        if abort:
            self.aborted_reason = abort
            return []
        samples = parse_level_batch(
            event,
            run_token=self.run_token,
            on_schema_mismatch=self._on_schema_mismatch,
        )
        if samples:
            newest = max(s.seq for s in samples)
            if newest < self._last_seq:
                # Same-token seq regression: the phone page reloaded and
                # restarted its counter — a new stream, not stale data.
                log_event(
                    logger,
                    "level_feed_stream_reset",
                    newest_seq=newest,
                    last_seq=self._last_seq,
                )
                self._last_seq = -1
        fresh = [s for s in samples if s.seq > self._last_seq]
        if fresh:
            self._last_seq = max(s.seq for s in fresh)
        return fresh

    def post_ramp_signal(self, key: str, value: Any) -> None:
        """Post a latched, idempotent ramp-control host event (best-effort).

        Callers re-invoke this per tick / per re-post attempt; posting the same
        field repeatedly is harmless by design (the whole point — a one-shot
        into the read-modify-write slot can be silently reverted)."""
        if self._post_host_event is None:
            return
        try:
            self._post_host_event({"ramp": {key: value, "run_token": self.run_token}})
        except _FEED_ERRORS:
            logger.warning("ramp host-event post failed (%s)", key, exc_info=True)

    def read_back_ramp_state(self) -> str:
        """The ramp state currently echoed in the relay's host_event, if any.

        The Pi's pull-token ``/status`` includes ``host_event`` (worker.js
        ``getStatus``), so a terminal post that a phone putMeta race reverted
        is detectable. Observability only: a single confirmed read-back is NOT
        durable (the next phone batch post can revert it), so the terminal
        re-post schedule always runs to its bounded end regardless of what
        this returns."""
        try:
            status = self._read_status() or {}
        except _FEED_ERRORS:
            return ""
        host_event = status.get("host_event") if isinstance(status, dict) else None
        if not isinstance(host_event, dict):
            return ""
        ramp = host_event.get("ramp")
        if not isinstance(ramp, dict):
            return ""
        if self.run_token and str(ramp.get("run_token") or "") != self.run_token:
            return ""
        return str(ramp.get("state") or "")


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
        # A bounded-low result is LOCKED but explicitly labeled in lock_kind;
        # MAXED_OUT remains a failed attempt and never creates a lock.
        return self.ramp.state is RampState.LOCKED

    @property
    def bounded_low_level(self) -> bool:
        """True only for the evidence-backed degraded cap lock."""
        return self.ramp.lock_kind is RampLockKind.BOUNDED_LOW_LEVEL

    def snapshot(self) -> dict[str, Any]:
        return {
            "geometry": self.geometry,
            "ramp": self.ramp.snapshot(),
            "lock": self.lock.to_dict() if self.lock else None,
            "aborted_reason": self.aborted_reason,
        }


# --- ramp terminal refusal copy -----------------------------------------------
#
# The 2026-07-16 jts3 finding: a level-match ramp terminal that didn't lock
# (``agc_suspected`` and friends) reached the phone terminal and the
# ``capture_relay.adapter_failed`` log as a bare ``ValueError`` carrying the
# ramp's raw code — never translated, never explaining anything. Every
# refusal now names its reason (the project rule): this is the single place
# a ramp terminal's ``(error, error_detail)`` pair becomes homeowner copy.
# Both the relay web adapter (``jasper.web.correction_setup``) and the Room
# envelope (``jasper.correction.envelope``) call ``describe_ramp_refusal`` —
# neither hand-rolls its own copy.


@dataclass(frozen=True)
class RampRefusal:
    """One ramp terminal's homeowner-facing translation.

    ``code`` is the stable, machine-readable reason (the ramp's own terminal
    ``error`` string — already a short snake_case code for the known cases
    like ``agc_suspected``, e.g. for `reason=` log fields). ``user_message``
    is the sentence shown to the household.
    """

    code: str
    user_message: str


class LevelMatchRefused(RuntimeError):
    """The level-match ramp ended without a lock; carries the homeowner copy.

    Raised by the relay web adapter instead of a bare ``ValueError(detail)``
    (see the module docstring above). ``str(exc)`` is the homeowner message,
    so an unmigrated ``str(exc)`` caller still reads sensibly; ``.code`` and
    ``.user_message`` are the structured pair for callers that want them
    (log ``reason=``, the phone terminal's ``error`` field).
    """

    def __init__(self, refusal: RampRefusal) -> None:
        super().__init__(refusal.user_message)
        self.code = refusal.code
        self.user_message = refusal.user_message


# Known short ramp codes -> homeowner copy. Exact match only — most other
# ramp terminals already carry a full sentence in `error` (handled by the
# prefix table and the generic fallback below).
_RAMP_REFUSAL_COPY: dict[str, str] = {
    "agc_suspected": (
        "The microphone kept adjusting its own level during the check, so "
        "this measurement can't be trusted. Turn off the mic's automatic "
        "gain if you can, or try a different device or browser, then retry."
    ),
    "agc_indeterminate": (
        "The speaker couldn't collect enough evidence that the microphone's "
        "level held steady during the check. Keep the phone still and the "
        "room quiet, then retry."
    ),
    "no usable phone samples": (
        "The speaker never heard a usable reading from the phone's "
        "microphone. Check the phone is close enough and its microphone "
        "isn't blocked, then retry."
    ),
    "tone ended before the ramp completed": (
        "The test tone stopped before the check finished. Retry the level "
        "check."
    ),
    "clip detected": (
        "The microphone picked up a sound too loud to measure safely. Move "
        "the phone or turn the volume down, then retry."
    ),
    "phone never armed": (
        "The phone page never started the check. Reopen the measurement "
        "link and tap Start."
    ),
}

# (prefix, canonical_code, message) — matched in order for terminals whose
# `error` carries a parameterized detail (a duration, a dB value) after a
# fixed lead-in. The canonical snake_case code is what `RampRefusal.code`
# (and therefore the `reason=` log key) carries, so `reason=safety_timeout`
# groups across runs instead of one key per duration; the verbatim
# parameterized error stays visible in the user_message parenthetical.
_RAMP_REFUSAL_PREFIX_COPY: tuple[tuple[str, str, str], ...] = (
    (
        "safety timeout",
        "safety_timeout",
        "The check took too long and stopped for safety. Retry the level "
        "check.",
    ),
    (
        "phone feed lost",
        "phone_feed_lost",
        "The speaker lost the phone's microphone feed partway through the "
        "check. Keep the capture page open, then retry.",
    ),
    (
        "safe cap reached below target window",
        "safe_cap_below_window",
        "The microphone is still too quiet at the safe volume limit. Raise "
        "the external amplifier a little, then retry.",
    ),
    (
        # Matches ramp.py's existing `reason="non_finite_original"` log field
        # for this terminal — one group key across both surfaces.
        "non-finite pre-ramp main_volume",
        "non_finite_original",
        "The speaker's starting volume was invalid. Retry the level check.",
    ),
)

_NOT_LOCKED_MESSAGE = "The measurement level was not reached. Retry the level check."


def describe_ramp_refusal(
    error: str | None, detail: str | None = None
) -> RampRefusal:
    """Translate one ramp terminal's raw ``(error, error_detail)`` into
    homeowner copy — the single place this mapping lives.

    ``error`` is ``RampData.error`` (a stable short code for some terminals,
    e.g. ``agc_suspected``; a full sentence for others, e.g. ``"clip
    detected"`` or ``"phone feed lost (no samples for 8s)"``). A falsy
    ``error`` (``outcome.locked`` is False but the ramp never set one) is the
    generic "not locked" case. Parameterized terminals (a duration, a dB
    value baked into the string) are normalized to one canonical snake_case
    ``code`` per family via the prefix table, so ``reason=`` log keys group
    across runs; the verbatim parameterized error is preserved in the
    user_message parenthetical instead. An unrecognized code always gets a
    generic fallback that INCLUDES the raw code, never a silently-generic
    message — most unrecognized codes are already a plain sentence, so
    echoing it verbatim (title-cased, period-terminated) reads naturally.
    ``detail`` — when the ramp attached one (``RampData.error_detail`` —
    currently only ``agc_suspected``'s measured slopes/step count) — is
    appended in parentheses so retries and support conversations can point
    at the exact evidence, without changing the stable ``code`` used for log
    grouping.
    """
    raw = str(error or "").strip()
    if not raw:
        return RampRefusal(code="not_locked", user_message=_NOT_LOCKED_MESSAGE)
    code = raw
    message: str | None = _RAMP_REFUSAL_COPY.get(raw)
    extras: list[str] = []
    if message is None:
        for prefix, family_code, prefix_message in _RAMP_REFUSAL_PREFIX_COPY:
            if raw.startswith(prefix):
                code = family_code
                message = prefix_message
                # The code is normalized; keep the verbatim, parameterized
                # error visible to the household/support conversation.
                extras.append(raw)
                break
    if message is None:
        text = raw[0].upper() + raw[1:]
        message = text if text.endswith((".", "!", "?")) else f"{text}."
    detail_text = str(detail or "").strip()
    if detail_text:
        extras.append(detail_text)
    if extras:
        message = f"{message} ({'; '.join(extras)})"
    return RampRefusal(code=code, user_message=message)


class LevelMatchSession:
    """Wires the kernel ramp to the relay for ONE geometry step.

    Host-mediated: the caller injects the volume get/set, the tone
    play/cancel, and the relay status-read / host-event-post — this class owns
    only the ramp orchestration and the per-geometry lock persistence. It never
    imports the correction daemon or touches CamillaDSP directly.
    """

    # Pre-ramp armed gate: how long the phone gets to tap Start before the run
    # is abandoned without ever touching volume or tone.
    DEFAULT_ARMED_TIMEOUT_S = 90.0
    ARMED_POLL_S = 0.25
    # Terminal host-event re-posting: attempts × spacing bound the "phone still
    # metering with a hot mic" window after a putMeta revert race. The FULL
    # schedule always runs (no early exit on a confirmed echo) — see the
    # re-post loop in run_for_geometry for the revert-race rationale.
    TERMINAL_POST_ATTEMPTS = 5
    TERMINAL_POST_SPACING_S = 0.75

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
        # Lifecycle cancellation is wider than RampController.cancel(): the
        # retained session exists while waiting for the phone to arm and after
        # the kernel publishes a terminal state but is still completing relay
        # acknowledgement. Stop must await both edges, never infer cleanup from
        # the public RampState alone.
        self._cancel_requested = False

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
        run_token: str = "",
        wait_for_armed: bool = True,
        armed_timeout_s: float | None = None,
    ) -> LevelMatchOutcome:
        """Ramp + lock the measurement level for ``geometry``.

        Waits (bounded) for the phone's ``armed`` superset before any volume or
        tone change — a premature call must not burn a full tone climb against a
        phone nobody tapped Start on. Only a terminal LOCKED stores a
        :class:`MeasurementLevelLock` under the geometry key. A trustworthy,
        stable cap result may lock as explicitly degraded
        ``bounded_low_level`` evidence; an insufficient cap result remains
        MAXED_OUT and stores no lock. ABORTED / CANCELLED / ERROR likewise store
        nothing and restore the original listening level. A phone-reported abort
        seen in the feed cancels the ramp cleanly. ``run_token`` must match the
        token minted into this run's ``build_level_ramp_spec`` so the feed is
        scoped to this run.
        """
        feed = RelayLevelFeed(
            read_status=read_status,
            post_host_event=post_host_event,
            run_token=run_token,
            monotonic=clock,
        )
        controller = self._controller = RampController(
            session_id=self.session_id, config=self.config
        )
        data: RampData | None

        if wait_for_armed:
            timeout = (
                self.DEFAULT_ARMED_TIMEOUT_S
                if armed_timeout_s is None
                else armed_timeout_s
            )
            armed_deadline = clock() + timeout
            while True:
                if self._cancel_requested:
                    data = RampData(state=RampState.CANCELLED)
                    log_event(
                        logger,
                        "level_match_done",
                        session=self.session_id,
                        geometry=geometry,
                        state=RampState.CANCELLED.value,
                        reason="cancelled_before_phone_armed",
                    )
                    break
                if feed.check_armed():
                    data = None
                    break
                if clock() >= armed_deadline:
                    outcome = LevelMatchOutcome(
                        geometry=geometry,
                        ramp=RampData(
                            state=RampState.ERROR,
                            error="phone never armed",
                        ),
                        lock=None,
                    )
                    log_event(
                        logger,
                        "level_match_done",
                        level=logging.WARNING,
                        session=self.session_id,
                        geometry=geometry,
                        state=RampState.ERROR.value,
                        reason="phone_never_armed",
                    )
                    return outcome
                await sleep(self.ARMED_POLL_S)
        else:
            data = None

        # Cancellation owns admission even when the phone's armed update and
        # Stop arrive in the same scheduler turn. RampController.run() resets
        # its own kernel-local cancel flag, so this lifecycle check must happen
        # immediately before entering the volume/tone owner.
        if data is None and self._cancel_requested:
            data = RampData(state=RampState.CANCELLED)

        if data is None:
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
        if data.state is RampState.LOCKED:
            lock = MeasurementLevelLock.from_ramp(geometry, data)
            self.store.put(lock)

        # Terminal ramp state → phone. The event slot is a read-modify-write
        # race (§3.1): the worker's postEvent/postHostEvent each write back the
        # WHOLE session meta from their own request-start read, so a phone
        # batch post that read the meta just before our terminal write reverts
        # host_event when it lands. Always run the FULL bounded re-post
        # schedule — never stop on a single confirmed read-back. Breaking on
        # first echo left exactly one revert window with nobody re-posting,
        # and a phone that then never sees the terminal runs to its own
        # deadline and reports a false timeout (2026-07-15 JTS3 tweeter ramp:
        # locked at 33.8 s, echo confirmed on attempt ~2, latch stopped, a
        # batch clobbered it back, phone showed "did not finish ... timeout").
        # The read-back is observability now, not an exit condition.
        # All terminal states are posted — a Pi-side CANCELLED/ERROR must also
        # stop the phone's metering, not just LOCKED/MAXED_OUT.
        terminal_echoed = False
        for _attempt in range(self.TERMINAL_POST_ATTEMPTS):
            feed.post_ramp_signal("state", data.state.value)
            await sleep(self.TERMINAL_POST_SPACING_S)
            if feed.read_back_ramp_state() == data.state.value:
                terminal_echoed = True

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
            lock_kind=(data.lock_kind.value if data.lock_kind is not None else ""),
            locked_db=(
                f"{data.locked_main_volume_db:.1f}"
                if data.locked_main_volume_db is not None
                else ""
            ),
            terminal_echoed=terminal_echoed,
        )
        return outcome

    async def lock_now(self) -> bool:
        """Manual lock (the user tapped Lock) — trust the user."""
        return await self._controller.lock() if self._controller else False

    async def cancel(self) -> bool:
        # The owning MeasurementSession retains this object only while its task
        # is live, so True means "lifecycle cancellation accepted; await the
        # owner". This remains true before arming and after terminal RampState,
        # when hard task cancellation would respectively hang or interrupt the
        # exact listening-volume restore.
        self._cancel_requested = True
        if self._controller is not None:
            await self._controller.cancel()
        return True
