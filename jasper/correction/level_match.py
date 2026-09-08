# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction-side adapter for the level-match ramp (P2).

The pure staircase / settle / lock math lives in the shared kernel
(:mod:`jasper.audio_measurement.ramp`). This module is the correction-layer glue
that the kernel deliberately does not know about:

  * :class:`LevelStatusFeed` — the ``next_samples`` source the kernel awaits
    each tick, reading the measurement page's **batched, client-timestamped**
    level samples out of the ``status`` event. The ``event`` slot is
    last-write-wins and the page streams into it continuously, so ramp control
    is robust by construction, not by one-shot posts: every level batch carries
    its own ``armed`` / ``aborted`` / ``agc_frozen`` / ``agc_unattested`` state
    as a SUPERSET envelope (a clobbered one-shot host event never strands the
    flow), Pi-side abort acks re-post each tick while the ramp exits, and the
    terminal ramp state is re-posted until ``/status`` echoes it back, so the
    read-modify-write revert race is *observable*, not assumed away. The feed
    also rate-limits its status reads — the kernel tick is ~100 Hz, the HTTP
    cadence must not be.
  * A **run token** scopes the feed to one ramp run: the token rides the
    ``level_ramp`` capture spec, the page echoes it in every batch, and the
    feed ignores events carrying another run's token — a *previous* run's
    persisted slot (its final abort superset, its stale samples) can no longer
    insta-cancel or mis-feed a retry. A same-token ``seq`` *regression* (the
    page reloaded mid-ramp and restarted its counter) is treated as a new
    stream rather than dropped as stale.
  * :class:`MeasurementLevelLock` + :class:`LevelLockStore` — the lock is scoped
    **per mic-geometry step, not blanket per-session** (near-field baffle vs
    listening position differ ~15–25 dB at the mic for the same played level, so
    one lock reused across geometries blows past the window or starves SNR). The
    store keys on the geometry.

Everything here is host-mediated (docs/extensibility.md §1) and hardware-free:
inject a fake reader + fake clock and the whole path is synthetically testable.

A cap result satisfying the kernel's strict evidence policy is stored as an
explicitly labeled ``bounded_low_level`` lock, never a normal in-window lock.
A cap result without that proof stays MAXED_OUT, whose UI copy must branch on
``ramp.agc_frozen``: with ``agc_frozen=False`` the evidence is AGC-compressed
and "raise your analog amp" may be wrong.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
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

# Failures a status reader can realistically raise: json decode errors are
# ValueError, and a buggy injected reader adds Type/Attribute/LookupError.
# Named (not blind) per the lint contract.
_FEED_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    LookupError,
)


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


# --- level feed: batched level samples in, latched ramp control out ----------

StatusReader = Callable[[], dict[str, Any]]
HostEventPoster = Callable[[dict[str, Any]], Any]


def parse_level_batch(
    event: dict[str, Any],
    *,
    run_token: str = "",
    on_schema_mismatch: Callable[[Any], None] | None = None,
) -> list[LevelSample]:
    """Extract the batched level samples from a ``status`` event.

    The measurement page posts ``{"level_batch": {"schema": N, "run_token": "...",
    "samples": [ {...}, ... ], "agc_frozen": bool, "armed": bool,
    "aborted": bool}}`` over the existing ``event`` envelope. Unknown /
    malformed payloads yield an empty list (the kernel treats a tick with no
    samples as "nothing new"), never an exception — the payload is browser
    input. A non-empty ``run_token`` scopes parsing to one ramp run:
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


class LevelStatusFeed:
    """Turns status polling into the kernel's ``next_samples`` source.

    Each ``next_samples()`` reads the freshest status (via the injected
    ``read_status``, rate-limited to ``min_read_interval_s`` so the kernel's
    ~100 Hz tick never becomes an HTTP cadence), dedupes samples by ``seq``
    (the last-write-wins slot re-delivers the same batch until the phone posts
    a newer one), treats a same-token seq *regression* as a fresh stream (page
    reload), watches for a phone-reported abort, and returns only the new
    :class:`LevelSample` s. Warnings are latched — an unreadable feed or a stale
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
                    "status read failed during ramp (latched — further "
                    "failures are silent until recovery)",
                    exc_info=True,
                )
            return {}
        if self._read_failing:
            self._read_failing = False
            logger.info("status read recovered")
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
        """The ramp state currently echoed in the status host_event, if any.

        ``/status`` includes ``host_event``, so a terminal post that a
        concurrent page post reverted is detectable. Observability only: a
        single confirmed read-back is NOT durable (the next batch post can
        revert it), so the terminal re-post schedule always runs to its bounded
        end regardless of what this returns."""
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


class LevelMatchSession:
    """Wires the kernel ramp to the measurement page for ONE geometry step.

    Host-mediated: the caller injects the volume get/set, the tone
    play/cancel, and the status-read / host-event-post — this class owns
    only the ramp orchestration and the per-geometry lock persistence. It never
    imports the correction daemon or touches CamillaDSP directly.
    """

    # Pre-ramp armed gate: how long the household gets to tap Start before the
    # run is abandoned without ever touching volume or tone.
    DEFAULT_ARMED_TIMEOUT_S = 90.0
    ARMED_POLL_S = 0.25
    # Terminal host-event re-posting: attempts × spacing bound the "page still
    # metering with a hot mic" window after a post revert race. The FULL
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
        # the kernel publishes a terminal state but is still completing the
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
        feed = LevelStatusFeed(
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
