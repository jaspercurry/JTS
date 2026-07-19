# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Session-scoped fixed measurement volume for the crossover conductor (Wave 2).

The v2 crossover measurement flow
(docs/crossover-measurement-productization-design.md §5.5) replaces the per-step
ramp/lock machinery with ONE fixed measurement volume held for the whole
session: snapshot the household volume on open, set the fixed measurement volume,
restore it exactly once on close/abandon. Per-driver level differences live in
the program's per-segment digital gains (§5.5), not in re-leveling the speaker.

This module owns that plan. It reuses the proven fail-closed latch from
``CrossoverLevelLease`` — durable intent written BEFORE the first volume
mutation, set-and-confirm through an independent readback (the shared
:func:`jasper.active_speaker.volume_latch.set_and_confirm_volume`), and
restore-exactly-once — but adds the lifecycle a *session* needs that a per-step
lease does not:

* a durable ``opened_at`` timestamp and a hard wall-clock ceiling (default
  1800 s ≈ 2× the relay TTL), so a walked-away user cannot pin the speaker at
  measurement volume indefinitely;
* abandon as a defined event set — explicit close, a session-death hook the
  flow (Wave 5) calls, and the wall-clock ceiling — each draining the same
  restore-once path;
* self-owned stale-active handling via the timestamp. ``CrossoverLevelLease``'s
  ``recover_unresolved_volume_safety`` refuses ``active`` states (it relies on a
  process restart hydrating them as unresolved); this plan must NOT rely on a
  restart to flip states, so a hydrated ``active`` state past the ceiling
  force-drains restore here, and falls back to the emergency floor + latched
  ``unresolved`` (the volume_recovery path) when readback cannot confirm.

The fixed measurement volume is not hard-coded: :func:`session_measurement_volume_db`
DERIVES it per profile from the active drivers' excitation ceilings, and the
SAME value feeds both the program composer's downstream gain and program
admission — one definition path (SSOT), so caps are enforced regardless of its
value.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

from .excitation_safety_plan import resolve_driver_excitation_ceilings
from .volume_latch import (
    EMERGENCY_MEASUREMENT_VOLUME_DB,
    GetMainVolumeDb,
    SetMainVolumeDb,
    set_and_confirm_volume,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STATE_KIND = "jts_crossover_session_volume"

# The wall-clock ceiling on an open session's held measurement volume. ~2x the
# relay TTL: long enough that no legitimate CHECK->MEASURE->apply->VERIFY run is
# cut short, short enough that a walked-away user's speaker returns to household
# volume within a bounded window even if no close/session-death event fires.
DEFAULT_WALL_CLOCK_CEILING_S = 1800.0

# The codified measurement reference level: the fixed session volume when no
# driver cap binds below it. -20 dB gives the least-sensitive driver a usable
# acoustic measurement level while leaving 20 dB of digital+DSP margin under the
# 0 dB ceiling. PROVISIONAL pending W6 bench validation — the value is a design
# input (2026-07-18 W2 gate ruling), not yet a hardware-measured one; W6 may
# retune it against measured SNR at ~1 m.
MEASUREMENT_REFERENCE_VOLUME_DB = -20.0

_DEFAULT_STATE_PATH = Path(
    "/var/lib/jasper/active_speaker_crossover_session_volume.json"
)


class SessionVolumePlanError(RuntimeError):
    """The session volume plan cannot form or resolve a safe transition."""


class SessionVolumeOpenResult(str, Enum):
    """Outcome of opening a fixed measurement-volume session."""

    OPENED = "opened"
    # The measurement volume could not be confirmed; the plan drained to the
    # emergency floor and latched unresolved (volume_recovery path).
    EMERGENCY_ATTENUATED = "emergency_attenuated"
    # Neither the measurement volume nor the emergency floor could be confirmed.
    FAILED = "failed"


class SessionVolumeRestoreResult(str, Enum):
    """Outcome of draining the one durable restore intent."""

    EXACT_RESTORED = "exact_restored"
    EMERGENCY_ATTENUATED = "emergency_attenuated"
    ALREADY_RESOLVED = "already_resolved"
    FAILED = "failed"


def session_measurement_volume_db(
    safety_profile: Mapping[str, Any],
    target_fingerprints: Iterable[str],
) -> float:
    """The fixed session measurement volume DERIVED from the profile's ceilings.

    The session volume's job is to let the LEAST-sensitive (highest-cap) driver
    reach a usable measurement level with digital headroom, while more-sensitive
    drivers attenuate DOWN to their own caps via per-segment digital gains —
    attenuating downward is always satisfiable, so every driver's cap is
    enforceable at this volume (2026-07-18 W2 gate ruling)::

        session_volume = min(MEASUREMENT_REFERENCE_VOLUME_DB, max over drivers
                             of resolve_driver_excitation_ceilings()[1])

    Worked example — woofer cap 0.0 dBFS, tweeter (compression driver) cap
    -65 dBFS: V = min(-20, max(0, -65)) = **-20 dB**. The tweeter's program
    channel attenuates to -45 dB digital (effective -45 + -20 = -65 = its cap);
    the woofer can reach up to -26 dBFS effective at the -6 dB digital guard —
    versus -70 dBFS under a (wrong) ``min(caps)`` rule, which would pin the
    least-sensitive driver ~40 dB under its ceiling and collapse its measurement
    SNR. Because the program's per-segment effective peak is ``segment_peak_dbfs
    + session_volume`` (the program graph adds no headroom beyond the main
    volume — see ``emit_active_speaker_program_config``), admission enforces
    every driver's cap directly against this value: it is only an INPUT to that
    admission, the single definition path (SSOT), so the caps hold regardless of
    the derived number.

    Fail-closed floor: the derived volume must sit ABOVE the emergency
    attenuation floor (:data:`EMERGENCY_MEASUREMENT_VOLUME_DB`, -60 dB). A
    profile whose highest cap is at or below the floor cannot be measured at a
    safe volume at all — every driver would need its stimulus pushed into the
    emergency-quiet regime — so the session refuses to open with the typed
    ``profile_unmeasurable_at_safe_volume`` error rather than opening a
    zero-SNR session. (This invariant would also have caught the inverted
    ``min(caps)`` derivation at runtime.)

    Raises if no targets are given or a ceiling cannot be resolved (fail-closed:
    an underivable session volume is a refusal, never a guessed default).
    """
    caps: list[float] = []
    for target_fingerprint in target_fingerprints:
        # program_admission=True: this session volume exists ONLY to serve
        # the v2 conductor's CHECK/MEASURE programs (this module's own
        # docstring) -- always the proven-HP path.
        _band, maximum_peak = resolve_driver_excitation_ceilings(
            safety_profile, target_fingerprint, program_admission=True
        )
        caps.append(float(maximum_peak))
    if not caps:
        raise SessionVolumePlanError(
            "cannot derive a session measurement volume with no driver targets"
        )
    volume = min(MEASUREMENT_REFERENCE_VOLUME_DB, max(caps))
    if not math.isfinite(volume) or volume > 0.0:
        raise SessionVolumePlanError(
            "derived session measurement volume must be finite and non-positive"
        )
    if not volume > EMERGENCY_MEASUREMENT_VOLUME_DB:
        raise SessionVolumePlanError(
            "profile_unmeasurable_at_safe_volume: every driver cap sits at or "
            f"below the {EMERGENCY_MEASUREMENT_VOLUME_DB:g} dB emergency floor; "
            "the profile cannot be measured at a safe session volume"
        )
    return volume


@dataclass(frozen=True)
class _State:
    status: str  # "active" | "unresolved"
    reason: str | None
    opened_at: float
    wall_clock_ceiling_s: float
    measurement_volume_db: float | None
    original_main_volume_db: float | None


def _finite_nonpositive(value: Any) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) > 0.0
    ):
        return None
    return float(value)


def _malformed(reason: str) -> _State:
    return _State(
        status="unresolved",
        reason=reason,
        opened_at=0.0,
        wall_clock_ceiling_s=DEFAULT_WALL_CLOCK_CEILING_S,
        measurement_volume_db=None,
        original_main_volume_db=None,
    )


def _load_state(path: Path | None) -> _State | None:
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return _malformed("session_volume_state_unreadable")
    if (
        not isinstance(raw, Mapping)
        or raw.get("kind") != STATE_KIND
        or raw.get("schema_version") != SCHEMA_VERSION
    ):
        return _malformed("session_volume_state_malformed")
    if raw.get("status") == "resolved":
        return None
    status = raw.get("status")
    if status not in {"active", "unresolved"}:
        return _malformed("session_volume_state_malformed")
    opened_at = raw.get("opened_at")
    if isinstance(opened_at, bool) or not isinstance(opened_at, (int, float)):
        return _malformed("session_volume_state_malformed")
    ceiling = raw.get("wall_clock_ceiling_s")
    if (
        isinstance(ceiling, bool)
        or not isinstance(ceiling, (int, float))
        or not math.isfinite(float(ceiling))
        or float(ceiling) <= 0.0
    ):
        ceiling = DEFAULT_WALL_CLOCK_CEILING_S
    # Hydrate the raw status as-is (do NOT flip active -> unresolved on load: the
    # ceiling, checked from opened_at, is what retires a stale active session —
    # never a bare process restart).
    return _State(
        status=str(status),
        reason=(None if raw.get("reason") is None else str(raw.get("reason"))),
        opened_at=float(opened_at),
        wall_clock_ceiling_s=float(ceiling),
        measurement_volume_db=_finite_nonpositive(raw.get("measurement_volume_db")),
        original_main_volume_db=_finite_nonpositive(
            raw.get("original_main_volume_db")
        ),
    )


class SessionVolumePlan:
    """Owner of one session-scoped fixed measurement volume + its restore latch.

    Owns no CamillaDSP: every method that mutates volume takes the set/get main
    volume callables (mirroring ``CrossoverLevelLease``). The process-global
    production instance injects a durable state path; test instances stay
    in-memory unless they opt into one.
    """

    def __init__(
        self,
        *,
        state_path: str | Path | None = None,
        wall_clock_ceiling_s: float = DEFAULT_WALL_CLOCK_CEILING_S,
        emergency_volume_db: float = EMERGENCY_MEASUREMENT_VOLUME_DB,
        clock: Any = time.time,
    ) -> None:
        self._state_path = Path(state_path) if state_path is not None else None
        self._wall_clock_ceiling_s = float(wall_clock_ceiling_s)
        self._emergency_volume_db = float(emergency_volume_db)
        self._clock = clock
        self._restore_lock = asyncio.Lock()
        self._state = _load_state(self._state_path)
        # True only when THIS instance opened the active volume. A crash-hydrated
        # ``active`` state (a different process, or a restart) leaves this False:
        # the durable status is NOT flipped (the ceiling governs staleness), but
        # the volume cannot be treated as ready until it is recovered + reopened.
        self._opened_this_process = False

    # --- read surfaces -------------------------------------------------------

    @property
    def measurement_volume_db(self) -> float | None:
        return self._state.measurement_volume_db if self._state else None

    @property
    def unresolved_volume_safety(self) -> dict[str, Any] | None:
        state = self._state
        if state is None or state.status != "unresolved":
            return None
        return {
            "status": "unresolved",
            "reason": state.reason or "session_volume_restore_unconfirmed",
            "original_main_volume_db": state.original_main_volume_db,
            "emergency_volume_db": self._emergency_volume_db,
        }

    @property
    def needs_recovery(self) -> bool:
        """True when the durable state must be drained before a new session.

        Two branches: a latched ``unresolved`` state, OR a durably ``active``
        state this process did not open (crash/restart hydration). **W5's
        recovery screen must key on THIS property, not
        ``unresolved_volume_safety`` alone** — the crash-hydrated-active state
        within the wall-clock ceiling surfaces NO unresolved payload (its
        durable status is deliberately not flipped on restart; the ceiling
        governs staleness), so a screen keyed only on ``unresolved`` would show
        nothing while the speaker sits at measurement volume with no owner.
        Drain via :meth:`recover_unresolved`.
        """
        state = self._state
        if state is None:
            return False
        if state.status == "unresolved":
            return True
        return not self._opened_this_process

    def stale_active(self, now: float | None = None) -> bool:
        """True iff an ``active`` session has outlived the wall-clock ceiling."""
        state = self._state
        if state is None or state.status != "active":
            return False
        current = float(self._clock() if now is None else now)
        return (current - state.opened_at) > state.wall_clock_ceiling_s

    def assert_ready(self, now: float | None = None) -> None:
        """Require an open, confirmed, non-stale measurement volume.

        The session-volume assertion ``play_program`` acquires: raises unless the
        plan is ``active`` and within the wall-clock ceiling. A missing, stale, or
        unresolved state all block (fail-closed) — a stale/unresolved plan must be
        drained through :meth:`recover_unresolved` first.
        """
        state = self._state
        if state is None:
            raise SessionVolumePlanError(
                "no measurement volume is open; open the session volume plan first"
            )
        if state.status != "active":
            raise SessionVolumePlanError(
                "the measurement volume is not confirmed safe; restore it or apply "
                "emergency attenuation before playing a program"
            )
        if not self._opened_this_process:
            raise SessionVolumePlanError(
                "a measurement volume is durably active but was not opened in this "
                "process (crash/restart); recover it and open a fresh session first"
            )
        if self.stale_active(now):
            raise SessionVolumePlanError(
                "the measurement volume session has exceeded its wall-clock "
                "ceiling; drain it before playing a program"
            )

    # --- durable state I/O ---------------------------------------------------

    def _persist(self, payload: Mapping[str, Any]) -> None:
        if self._state_path is None:
            return
        atomic_write_text(
            self._state_path,
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": STATE_KIND,
                    **dict(payload),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            mode=0o640,
            group_from_parent=True,
        )

    def _persist_state(self, state: _State) -> None:
        self._persist(
            {
                "status": state.status,
                "reason": state.reason,
                "opened_at": state.opened_at,
                "wall_clock_ceiling_s": state.wall_clock_ceiling_s,
                "measurement_volume_db": state.measurement_volume_db,
                "original_main_volume_db": state.original_main_volume_db,
                "emergency_volume_db": self._emergency_volume_db,
            }
        )

    def _mark_unresolved(self, reason: str) -> None:
        base = self._state or _malformed(reason)
        state = _State(
            status="unresolved",
            reason=str(reason),
            opened_at=base.opened_at,
            wall_clock_ceiling_s=base.wall_clock_ceiling_s,
            measurement_volume_db=base.measurement_volume_db,
            original_main_volume_db=base.original_main_volume_db,
        )
        self._state = state
        try:
            self._persist_state(state)
        except OSError:
            # A prior active intent stays on disk, so a restart still hydrates
            # fail-closed (as unresolved via the ceiling / malformed guard).
            log_event(
                logger,
                "correction.session_volume_persist_failed",
                level=logging.CRITICAL,
                reason=reason,
            )

    def _clear_resolved(self) -> None:
        self._persist({"status": "resolved"})
        self._state = None
        self._opened_this_process = False

    # --- lifecycle -----------------------------------------------------------

    async def open(
        self,
        measurement_volume_db: float,
        set_main_volume_db: SetMainVolumeDb,
        get_main_volume_db: GetMainVolumeDb,
    ) -> SessionVolumeOpenResult:
        """Snapshot the household volume, then set the fixed measurement volume.

        Writes the durable ``active`` intent (with ``opened_at``) BEFORE the first
        volume mutation, so a crash or lost setter response hydrates as a
        recoverable state rather than a forgotten one. On confirm failure the
        plan drains to the exact original (else the emergency floor) and latches
        unresolved. Refuses to open over an unresolved or stale-active state —
        recover that first.
        """
        volume = _finite_nonpositive(measurement_volume_db)
        if volume is None:
            raise SessionVolumePlanError(
                "measurement volume must be finite and non-positive"
            )
        if self._state is not None:
            raise SessionVolumePlanError(
                "a prior session volume state is unresolved; recover it before "
                "opening a new measurement volume"
            )
        try:
            observed = await get_main_volume_db()
        except (OSError, RuntimeError, TimeoutError, ValueError):
            observed = None
        original = _finite_nonpositive(observed)
        opened_at = float(self._clock())
        state = _State(
            status="active",
            reason=None,
            opened_at=opened_at,
            wall_clock_ceiling_s=self._wall_clock_ceiling_s,
            measurement_volume_db=volume,
            original_main_volume_db=original,
        )
        # Write BEFORE the first mutation.
        self._persist_state(state)
        self._state = state
        if await set_and_confirm_volume(
            volume, set_main_volume_db, get_main_volume_db
        ):
            self._opened_this_process = True
            log_event(
                logger,
                "correction.session_volume_opened",
                measurement_volume_db=f"{volume:.2f}",
                original_main_volume_db=(
                    None if original is None else f"{original:.2f}"
                ),
                wall_clock_ceiling_s=f"{self._wall_clock_ceiling_s:.0f}",
            )
            return SessionVolumeOpenResult.OPENED
        # Setter could not be confirmed: the live volume is unknown — drain it.
        drained = await self._drain_restore(
            set_main_volume_db,
            get_main_volume_db,
            reason="measurement_volume_set_unconfirmed",
        )
        if drained is SessionVolumeRestoreResult.EMERGENCY_ATTENUATED:
            return SessionVolumeOpenResult.EMERGENCY_ATTENUATED
        return SessionVolumeOpenResult.FAILED

    async def _drain_restore(
        self,
        set_main_volume_db: SetMainVolumeDb,
        get_main_volume_db: GetMainVolumeDb,
        *,
        reason: str,
    ) -> SessionVolumeRestoreResult:
        """Resolve the one durable intent through confirmed readback (restore-once).

        Every abandon event funnels here: explicit close, the session-death hook,
        and the wall-clock ceiling. Unlike ``CrossoverLevelLease``, this NEVER
        refuses an ``active`` state — a stale-active session is force-drained here
        (the ceiling is enforced by the timestamp, not by a process restart).
        Restores the exact original if confirmable, else the emergency floor;
        latches unresolved only when neither confirms.
        """
        async with self._restore_lock:
            state = self._state
            if state is None:
                return SessionVolumeRestoreResult.ALREADY_RESOLVED
            candidates: list[tuple[str, float]] = []
            original = state.original_main_volume_db
            if original is not None:
                candidates.append(("exact", original))
            candidates.append(("emergency", self._emergency_volume_db))
            for recovery, target in candidates:
                if not await set_and_confirm_volume(
                    target, set_main_volume_db, get_main_volume_db
                ):
                    continue
                self._clear_resolved()
                log_event(
                    logger,
                    "correction.session_volume_restored",
                    level=(logging.INFO if recovery == "exact" else logging.ERROR),
                    recovery=recovery,
                    reason=reason,
                    to_db=f"{target:.2f}",
                )
                return (
                    SessionVolumeRestoreResult.EXACT_RESTORED
                    if recovery == "exact"
                    else SessionVolumeRestoreResult.EMERGENCY_ATTENUATED
                )
            self._mark_unresolved("session_volume_restore_unconfirmed")
            log_event(
                logger,
                "correction.session_volume_restore_failed",
                level=logging.CRITICAL,
                reason=reason,
            )
            return SessionVolumeRestoreResult.FAILED

    async def close(
        self,
        set_main_volume_db: SetMainVolumeDb,
        get_main_volume_db: GetMainVolumeDb,
        *,
        reason: str = "session_closed",
    ) -> SessionVolumeRestoreResult:
        """Restore the household volume exactly once and resolve (idempotent)."""
        return await self._drain_restore(
            set_main_volume_db, get_main_volume_db, reason=reason
        )

    async def abandon(
        self,
        set_main_volume_db: SetMainVolumeDb,
        get_main_volume_db: GetMainVolumeDb,
        *,
        reason: str = "session_abandoned",
    ) -> SessionVolumeRestoreResult:
        """Session-death observation hook (Wave 5 calls it) — drains restore-once."""
        return await self._drain_restore(
            set_main_volume_db, get_main_volume_db, reason=reason
        )

    async def enforce_ceiling(
        self,
        set_main_volume_db: SetMainVolumeDb,
        get_main_volume_db: GetMainVolumeDb,
        now: float | None = None,
    ) -> SessionVolumeRestoreResult | None:
        """Force-drain an active session that has outlived the wall-clock ceiling.

        Enforced both live (the flow may call this) and on hydration (a hydrated
        active state past the ceiling is force-drained here). Returns ``None``
        when nothing is stale.
        """
        if not self.stale_active(now):
            return None
        return await self._drain_restore(
            set_main_volume_db,
            get_main_volume_db,
            reason="wall_clock_ceiling_exceeded",
        )

    async def recover_unresolved(
        self,
        set_main_volume_db: SetMainVolumeDb,
        get_main_volume_db: GetMainVolumeDb,
    ) -> SessionVolumeRestoreResult:
        """The volume_recovery path: drain a latched unresolved OR stale-active state.

        Unlike ``CrossoverLevelLease.recover_unresolved_volume_safety`` (which
        refuses ``active`` states), this drains a stale-active state too — the
        plan owns its stale-active handling via the timestamp.
        """
        return await self._drain_restore(
            set_main_volume_db,
            get_main_volume_db,
            reason="volume_recovery",
        )


DEFAULT_SESSION_VOLUME_STATE_PATH = _DEFAULT_STATE_PATH
