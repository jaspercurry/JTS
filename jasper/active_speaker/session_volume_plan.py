# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Session-scoped fixed measurement volume for the crossover session.

ONE fixed volume held for the whole session — snapshot the household level on
open, set the measurement level, restore exactly once on close/abandon — with
per-driver level differences living in the program's per-segment digital gains
(docs/crossover-measurement-productization-design.md §5.5). Beyond the
``CrossoverLevelLease`` latch it reuses, a session adds a durable ``opened_at``
and wall-clock ceiling, abandon as a defined event set, self-owned stale-active
handling, and a per-stimulus re-proof of the volume it opened.
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
from typing import Any, Iterable, Mapping, Protocol

from jasper.atomic_io import atomic_write_text
from jasper.control.measurement_hold import read_measurement_hold
from jasper.log_event import log_event

from .calibration_level import MAX_TEST_LEVEL_DBFS
from .excitation_safety_plan import resolve_driver_excitation_ceilings
from .seat_level_reference import seat_level_reference_volume_db
from .volume_latch import (
    EMERGENCY_MEASUREMENT_VOLUME_DB,
    GetMainVolumeDb,
    SetMainVolumeDb,
    hold_fader_at,
    read_fader_db,
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

# The hard cap on ANY ceiling, however a caller scales it
# (:meth:`SessionVolumePlan.set_wall_clock_ceiling_s`). A longer measurement may
# widen the walked-away window; nothing may remove the bound. One hour is the
# outer edge of a plausible guided measurement.
MAX_WALL_CLOCK_CEILING_S = 3600.0

# The codified measurement reference level: the fixed session volume when no
# driver cap binds below it AND no measured reference has been banked. -20 dB
# gives the least-sensitive driver a usable acoustic measurement level while
# leaving 20 dB of digital+DSP margin under the 0 dB ceiling. A design input,
# not a hardware-measured one, which is why a box that has run the seat-SPL
# leveling step replaces it with that step's measured volume.
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
    #: The household level is RECORDED and a higher-ranked claim holds the
    #: fader. Nothing failed: the level lands when that claim releases, so the
    #: drain stops here rather than searching, and nothing is latched. Not a
    #: success either — the intent is still open, because the session that
    #: outranks this drain is still running.
    DEFERRED = "deferred"
    FAILED = "failed"


class RestoreOutcome(str, Enum):
    """What a :class:`VolumeDoor`'s restore verb actually achieved.

    Three answers because an owner-backed door can RECORD a household level a
    higher-ranked claim keeps off the fader; reading that as a failed write is
    what walks the ladder to its emergency rung over a perfectly good level.
    """

    #: The fader carries this level now (ducks included — a duck is an
    #: attenuation over a level in effect, not a different level).
    LANDED = "landed"
    #: Recorded, not written, because a higher-ranked LEVEL claim holds the
    #: fader. The owner lands it when that claim releases.
    DEFERRED = "deferred"
    #: Not in effect and not deferred — the write did not confirm.
    FAILED = "failed"


def measurement_reference_volume_db(
    *, reference_state_path: str | Path | None = None
) -> float:
    """The reference half of the session volume: measured if banked, else -20.

    Absent, unreadable, or implausible state resolves to
    :data:`MEASUREMENT_REFERENCE_VOLUME_DB`.
    """
    measured = seat_level_reference_volume_db(state_path=reference_state_path)
    return MEASUREMENT_REFERENCE_VOLUME_DB if measured is None else measured


def _driver_caps_dbfs(
    safety_profile: Mapping[str, Any],
    target_fingerprints: Iterable[str],
    *,
    declared_sensitivities: Mapping[str, float] | None = None,
) -> list[float]:
    """Every active driver's admitted effective-peak cap, one derivation path.

    ``program_admission=True``: these caps set each driver's composed segment
    level and are enforced per channel at admission — always the proven-HP path.
    """
    caps: list[float] = []
    for target_fingerprint in target_fingerprints:
        _band, maximum_peak = resolve_driver_excitation_ceilings(
            safety_profile,
            target_fingerprint,
            program_admission=True,
            declared_sensitivities=declared_sensitivities,
        )
        caps.append(float(maximum_peak))
    if not caps:
        raise SessionVolumePlanError(
            "cannot derive a session measurement volume with no driver targets"
        )
    return caps


def loudest_driver_cap_dbfs(
    safety_profile: Mapping[str, Any],
    target_fingerprints: Iterable[str],
    *,
    declared_sensitivities: Mapping[str, float] | None = None,
) -> float:
    """``max(caps)`` — the cap of the LOUDEST-permitted driver. Read the warning.

    The caps half of :func:`session_measurement_volume_db` and **only** that:
    it works there because a v2 program attenuates every other driver DOWN to
    its own cap per segment. **It is NOT "the loudest volume every driver
    permits"** — on the repo's woofer/compression-driver fixture it returns
    ``0.0`` dB while the tweeter's cap sits at −65. One signal through the whole
    graph with no per-segment gain must use
    :func:`unsegmented_stimulus_ceiling_db` instead.
    """
    return max(
        _driver_caps_dbfs(
            safety_profile,
            target_fingerprints,
            declared_sensitivities=declared_sensitivities,
        )
    )


def unsegmented_stimulus_ceiling_db(
    safety_profile: Mapping[str, Any],
    target_fingerprints: Iterable[str],
    *,
    stimulus_peak_dbfs: float,
    declared_sensitivities: Mapping[str, float] | None = None,
    branch_peaks_dbfs: Mapping[str, float] | None = None,
) -> float:
    """The loudest main volume at which ONE un-segmented signal still has room.

    "Has room" is a purely DIGITAL question — a signal through the whole graph
    carries no per-driver segment gain, so what runs out is headroom::

        ceiling = MAX_TEST_LEVEL_DBFS - binding_peak_dbfs

    Without ``branch_peaks_dbfs`` every branch is assumed to see the whole
    stimulus peak. With it, ``binding_peak_dbfs`` is ``max`` over the branches
    the caller rendered through the ACTUAL live graph — which binds TIGHTER
    than the full-band bound whenever a branch chain boosts.

    **Declared per-driver caps do not bound this volume — they are DISCLOSED**
    on ``event=active_speaker.unsegmented_ceiling_bound``, even a published one:
    a per-driver level limit cannot be enforced on a signal carrying no
    per-driver gain, and clamping the whole speaker to the tightest driver's
    figure pinned a 75 dB SPL seat target at 68.3 dB with ~30 dB of digital
    headroom unused. They still bind per DRIVER at admission and in the composed
    segment level, and resolving them still raises when a ledger cannot be read
    (that same call resolves the permitted BAND, which does refuse). What stops
    this volume instead: full scale, the emitted graph's limiters and
    ``devices.volume_limit``, the ramp's ``HARD_CEILING_DBFS``, and — live, on
    measured samples — the commissioning SPL stop.

    ``branch_peaks_dbfs`` is keyed by ``target_fingerprint`` and is a claim about
    one stimulus through one graph; it is never reusable.
    :mod:`jasper.active_speaker.branch_peak` owns producing it.

    **Fail-conservative, never fail-open**: any missing, non-finite or unusable
    branch peak falls back to the full-band bound under
    ``reason=branch_peaks_incomplete``, which can only make this quieter.
    Mic-independent by construction — no calibration, sensitivity or measured
    level enters it.
    """
    if not math.isfinite(stimulus_peak_dbfs):
        raise SessionVolumePlanError(
            f"stimulus peak must be finite, got {stimulus_peak_dbfs!r}"
        )
    # Materialised once and handed to the helper as the SAME list, so the caps
    # come back in a known one-to-one order with these fingerprints (and a
    # generator argument is not consumed twice).
    fingerprints = list(target_fingerprints)
    caps = _driver_caps_dbfs(
        safety_profile,
        fingerprints,
        declared_sensitivities=declared_sensitivities,
    )
    peak = float(stimulus_peak_dbfs)
    rendered = _binding_branch_peak_dbfs(fingerprints, branch_peaks_dbfs)
    # Which bound was used is a fact about the RENDER, never a comparison of
    # the two numbers: a branch whose peak happens to equal the stimulus peak
    # would otherwise report the bound it did not take.
    incomplete = rendered is None and branch_peaks_dbfs is not None
    binding = peak if rendered is None else rendered
    ceiling = MAX_TEST_LEVEL_DBFS - binding
    detail, declared_cap_ceiling = _declared_cap_disclosure(
        fingerprints,
        caps,
        branch_peaks_dbfs if not incomplete else None,
        stimulus_peak_dbfs=peak,
        ceiling_db=ceiling,
    )
    log_event(
        logger,
        "active_speaker.unsegmented_ceiling_bound",
        bound="per_branch" if rendered is not None else "full_band",
        # Named rather than inferred from ``bound``: silently serving the
        # conservative bound reads as "the graph is just this tight" and sends
        # an operator hunting the wrong number.
        reason="branch_peaks_incomplete" if incomplete else "",
        ceiling_db=f"{ceiling:.2f}",
        binding_peak_dbfs=f"{binding:.2f}",
        stimulus_peak_dbfs=f"{peak:.2f}",
        declared_cap_ceiling_db=f"{declared_cap_ceiling:.2f}",
        headroom_over_declared_caps_db=f"{ceiling - declared_cap_ceiling:+.2f}",
        drivers=detail,
    )
    return ceiling


def _usable_branch_peak(value: Any) -> float | None:
    """One rendered branch peak, or ``None`` when it is not a usable number.

    The single owner of "is this branch fact usable", so the bound and the
    disclosure cannot disagree about which branches resolved.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    peak = float(value)
    return peak if math.isfinite(peak) else None


def _binding_branch_peak_dbfs(
    fingerprints: list[str],
    branch_peaks_dbfs: Mapping[str, float] | None,
) -> float | None:
    """``max(branch_peak_d)`` — the branch that clips first — or ``None``.

    ``None`` is the fail-conservative answer the caller turns back into the
    full-band bound. EVERY active driver must carry a finite branch peak: a
    partial render would bound the speaker by only the branches that resolved.
    """
    if not branch_peaks_dbfs:
        return None
    peaks: list[float] = []
    for fingerprint in fingerprints:
        branch_peak = _usable_branch_peak(branch_peaks_dbfs.get(fingerprint))
        if branch_peak is None:
            return None
        peaks.append(branch_peak)
    return max(peaks) if peaks else None


def _declared_cap_disclosure(
    fingerprints: list[str],
    caps: list[float],
    branch_peaks_dbfs: Mapping[str, float] | None,
    *,
    stimulus_peak_dbfs: float,
    ceiling_db: float,
) -> tuple[str, float]:
    """The receipt for the caps this ceiling is NOT bounded by.

    One log-safe token per driver —
    ``<fingerprint>:cap=<x>,branch=<y>,at_ceiling=<z>,past_cap=<+d>`` — plus the
    ceiling the declared caps alone would have produced. ``past_cap`` is signed:
    positive means this volume drives that driver past its declared figure.
    """
    peaks = branch_peaks_dbfs or {}
    parts: list[str] = []
    cap_ceilings: list[float] = []
    for fingerprint, cap in zip(fingerprints, caps):
        rendered = _usable_branch_peak(peaks.get(fingerprint))
        received = stimulus_peak_dbfs if rendered is None else rendered
        at_ceiling = received + ceiling_db
        cap_ceilings.append(cap - received)
        shown = "none" if rendered is None else f"{rendered:.2f}"
        parts.append(
            f"{fingerprint}:cap={cap:.2f},branch={shown}"
            f",at_ceiling={at_ceiling:.2f},past_cap={at_ceiling - cap:+.2f}"
        )
    return " ".join(parts), min(cap_ceilings)


def session_measurement_volume_db(
    safety_profile: Mapping[str, Any],
    target_fingerprints: Iterable[str],
    *,
    declared_sensitivities: Mapping[str, float] | None = None,
    reference_state_path: str | Path | None = None,
) -> float:
    """The fixed session measurement volume DERIVED from the profile's ceilings.

    Its job is to let the LEAST-sensitive (highest-cap) driver reach a usable
    measurement level, while more-sensitive drivers attenuate DOWN to their own
    caps per segment — always satisfiable, so every cap stays enforceable::

        session_volume = min(measurement_reference_volume_db(),
                             loudest_driver_cap_dbfs())

    ``max`` over caps rather than ``min``: with a woofer at 0.0 dBFS and a
    compression-driver tweeter at -65, ``min(caps)`` would pin the woofer ~40 dB
    under its ceiling and collapse its SNR. Admission enforces every driver's
    cap against this value (the program graph adds no headroom beyond the main
    volume), so this is only an INPUT to that enforcement — one definition path.

    Fail-closed floor: the derived volume must sit ABOVE
    :data:`EMERGENCY_MEASUREMENT_VOLUME_DB`; a profile whose highest cap is at
    or below it cannot be measured at a safe volume at all and the session
    refuses to open. Raises if no targets are given or a ceiling cannot be
    resolved — an underivable session volume is a refusal, never a default.
    """
    # ``declared_sensitivities`` rides into the caps derivation so the caps
    # entering max(caps) are the SAME derived caps admission enforces.
    ceiling = loudest_driver_cap_dbfs(
        safety_profile,
        target_fingerprints,
        declared_sensitivities=declared_sensitivities,
    )
    volume = min(
        measurement_reference_volume_db(reference_state_path=reference_state_path),
        ceiling,
    )
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


def live_measurement_session(
    *,
    state_path: Path | None = None,
    now: float | None = None,
    action: str = "staging a walk",
) -> str | None:
    """The reason an operator door may not act right now, or ``None``.

    ``action`` names what the caller was about to do and closes both refusal
    sentences. Two facts, each asked of its own owner: whether a measurement is
    LIVE (jasper-control's self-expiring hold, so a crossover-web session
    refuses too) and whether a previous one left the volume UNRESOLVED (this
    plan's durable statefile, which outlives every process). When
    jasper-control cannot be reached, liveness falls back to the statefile
    (``needs_recovery`` minus ``stale_active``).

    A **stale** active state — one past its own wall-clock ceiling — is NOT a
    live session and does not refuse: that is the crashed-session shape the
    flow's own open path force-drains.

    ``None`` when the coast is clear, including when there is no state file.
    """
    # The path is EXPLICIT: a ``SessionVolumePlan()`` with no ``state_path``
    # reads no file at all and would answer "idle" for every speaker. Resolved
    # as a module global at CALL time, so a repointed path is seen here.
    plan = SessionVolumePlan(
        state_path=(
            DEFAULT_SESSION_VOLUME_STATE_PATH if state_path is None else state_path
        )
    )
    # Unresolved first: a pure file read, mutually exclusive with the active
    # state, and the more actionable of the two sentences.
    if plan.unresolved_volume_safety is not None:
        return (
            "a previous measurement left the speaker's measurement volume "
            "unresolved; recover it at http://jts.local/correction/crossover/ "
            f"before {action}"
        )
    if not _a_measurement_is_live(plan=plan, now=now):
        return None
    return (
        "a measurement session is already running (the speaker is held at its "
        f"measurement volume); finish or stop it before {action}"
    )


def _a_measurement_is_live(
    *,
    plan: "SessionVolumePlan",
    now: float | None,
) -> bool:
    """Is a measurement holding the speaker right now?

    jasper-control's hold is the authority; the durable statefile is the
    fallback when it cannot be reached. See :func:`live_measurement_session`
    for why the two facts are split that way.
    """
    hold = read_measurement_hold()
    if hold is not None:
        return bool(hold.get("active"))
    # ``needs_recovery`` minus the unresolved arm (answered above) is "durably
    # active and not opened by this process"; ``stale_active`` drops the crashed
    # shape the flow force-drains.
    return plan.needs_recovery and not plan.stale_active(now)


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
    # Hydrate the raw status as-is: do NOT flip active -> unresolved on load.
    # The ceiling, checked from opened_at, is what retires a stale session.
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


class VolumeDoor(Protocol):
    """How this plan reaches the fader — the ONE injected door, three questions.

    Opening ESTABLISHES a measurement level nothing may be admitted against
    until it is confirmed; draining RESTORES the snapshotted household level.
    Naming them apart lets one binding write through a ranked claim and another
    straight at the fader without the plan learning which it got.

    **Both verbs answer the fader's question**: *is the fader confirmed at this
    level?* — not *did a write go out*. Anything softer would let
    :meth:`SessionVolumePlan._drain_restore` clear a resolved marker over a
    speaker still sitting at measurement level.
    """

    async def read_household_level_db(self) -> float | None:
        """The level to come back to, or ``None`` when it cannot be read.

        Taken once, at open, before the first mutation. ``None`` is an ordinary
        answer and the plan falls through to the emergency floor rather than
        inventing a level.
        """
        raise NotImplementedError

    async def establish_measurement_level_db(self, level_db: float) -> bool:
        """Put the fader on the session's measurement level; confirmed?"""
        raise NotImplementedError

    async def restore_household_level_db(self, level_db: float) -> RestoreOutcome:
        """Put the fader back on a household level. Landed, deferred, failed?

        ``DEFERRED`` is NOT a failure: the level is recorded and lands when the
        higher-ranked claim releases, so the ladder stops rather than searching.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class FaderVolumeDoor:
    """The direct door: set-and-confirm straight at the fader.

    Both verbs land on the same
    :func:`~jasper.active_speaker.volume_latch.set_and_confirm_volume` — at this
    door they are one act. Callers that arbitrate through
    :class:`~jasper.volume_owner.VolumeOwner` bind a door that does; a process
    with no owner to arbitrate through binds this one.
    """

    set_main_volume_db: SetMainVolumeDb
    get_main_volume_db: GetMainVolumeDb

    async def read_household_level_db(self) -> float | None:
        return await read_fader_db(self.get_main_volume_db)

    async def establish_measurement_level_db(self, level_db: float) -> bool:
        return await set_and_confirm_volume(
            level_db, self.set_main_volume_db, self.get_main_volume_db
        )

    async def restore_household_level_db(self, level_db: float) -> RestoreOutcome:
        """Never defers: this door writes the fader itself."""
        confirmed = await set_and_confirm_volume(
            level_db, self.set_main_volume_db, self.get_main_volume_db
        )
        return RestoreOutcome.LANDED if confirmed else RestoreOutcome.FAILED


class SessionVolumePlan:
    """Owner of one session-scoped fixed measurement volume + its restore latch.

    Owns no CamillaDSP: every method that mutates volume takes one
    :class:`VolumeDoor`. A plan built with no ``state_path`` is in-memory only.
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
        # ``active`` state leaves this False: the durable status is NOT flipped
        # (the ceiling governs staleness), but the volume is not ready until it
        # is recovered and reopened.
        self._opened_this_process = False

    def set_wall_clock_ceiling_s(self, ceiling_s: float) -> None:
        """Re-arm the ceiling the NEXT :meth:`open` will stamp into state.

        The ceiling belongs to the measurement about to run, not to the process.
        ``stale_active`` and the force-drain read the ceiling the OPEN session
        recorded, so an already-open or hydrated session keeps its own.
        Fail-closed: a non-finite or non-positive value is refused, and the
        value is capped at :data:`MAX_WALL_CLOCK_CEILING_S`.
        """
        value = float(ceiling_s)
        if not math.isfinite(value) or value <= 0.0:
            raise SessionVolumePlanError(
                f"wall-clock ceiling must be finite and positive, got {ceiling_s!r}"
            )
        self._wall_clock_ceiling_s = min(value, MAX_WALL_CLOCK_CEILING_S)

    # --- read surfaces -------------------------------------------------------

    @property
    def wall_clock_ceiling_s(self) -> float:
        """The ceiling the next :meth:`open` will stamp into durable state."""
        return self._wall_clock_ceiling_s

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

        A latched ``unresolved`` state, OR a durably ``active`` state this
        process did not open. **A recovery screen must key on THIS property**,
        not ``unresolved_volume_safety`` alone: a crash-hydrated active state
        inside its ceiling surfaces no unresolved payload, so a screen keyed on
        ``unresolved`` would show nothing while the speaker sits at measurement
        volume with no owner. Drain via :meth:`recover_unresolved`.
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

        The assertion ``play_program`` acquires. A missing, stale, or unresolved
        state all block; drain through :meth:`recover_unresolved` first.
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
            # fail-closed.
            log_event(
                logger,
                "correction.session_volume_persist_failed",
                level=logging.CRITICAL,
                reason=reason,
            )

    def _clear_resolved(self) -> None:
        """Drop the in-memory intent FIRST, then record that on disk.

        The order is load-bearing (hearing, AGENTS.md #1). Persisting first left
        a drain that had CONFIRMED its volume on the hardware and then met an
        OSError writing the marker still ``active`` with the declared volume
        intact, so :meth:`assert_ready` passed over a speaker sitting at the
        emergency floor — measured at −60.0 → −12.5, **+47.5 dB**. Clearing
        first means the next reader sees a plan that owns nothing.

        The persist is then guarded as :meth:`_mark_unresolved` guards its own,
        for the mirror-image reason: a surviving intent makes a restart offer
        recovery for a volume already restored, and that re-drain targets the
        household snapshot, never the measurement one.
        """
        self._state = None
        self._opened_this_process = False
        try:
            self._persist({"status": "resolved"})
        except OSError:
            log_event(
                logger,
                "correction.session_volume_persist_failed",
                level=logging.CRITICAL,
                reason="session_volume_resolved_marker_unwritten",
            )

    # --- lifecycle -----------------------------------------------------------

    async def open(
        self,
        measurement_volume_db: float,
        door: VolumeDoor,
    ) -> SessionVolumeOpenResult:
        """Snapshot the household volume, then set the fixed measurement volume.

        Writes the durable ``active`` intent BEFORE the first volume mutation, so
        a crash or lost setter response hydrates as a recoverable state. On
        confirm failure the plan drains to the exact original (else the emergency
        floor) and latches unresolved. Refuses to open over an unresolved or
        stale-active state.
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
            observed = await door.read_household_level_db()
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
        if await door.establish_measurement_level_db(volume):
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
            door, reason="measurement_volume_set_unconfirmed",
        )
        if drained is SessionVolumeRestoreResult.EMERGENCY_ATTENUATED:
            return SessionVolumeOpenResult.EMERGENCY_ATTENUATED
        return SessionVolumeOpenResult.FAILED

    async def hold_measurement_volume(
        self,
        get_main_volume_db: GetMainVolumeDb,
        *,
        context: str = "",
    ) -> float | None:
        """Re-prove this session's measurement volume for ONE stimulus.

        Setting a volume once is not the same as it staying set, so a capture
        path asks HERE, per stimulus, rather than trusting :meth:`open`. It
        reads and never writes — a second writer per stimulus is exactly what
        this replaced.

        Returns the proven fader reading, or ``None`` when this plan owns no
        volume to hold (nothing open, unresolved, crash-hydrated, or past its
        ceiling), which leaves the fader alone rather than refusing — that is
        :meth:`assert_ready`'s question, already answered by ``play_program``.
        Raises :class:`~jasper.active_speaker.volume_latch.MeasurementFaderDrift`
        when the fader cannot be proven at the declared level.

        **Under the restore lock**, which is why this lives on the plan rather
        than at the capture seam: ``_mark_unresolved`` copies
        ``measurement_volume_db`` forward, so an unserialized hold could raise
        the fader on a speaker just left at the emergency floor.
        """
        async with self._restore_lock:
            volume = self._owned_volume_db_now()
            if volume is None:
                return None
            return await hold_fader_at(
                volume, get_main_volume_db, context=context,
            )

    def _owned_volume_db_now(self) -> float | None:
        """The volume this plan OWNS as of this instant, or ``None``.

        The one guarded answer to "may this session command a volume right
        now?". Reading ``measurement_volume_db`` directly instead is the
        +47.5 dB hazard :meth:`_clear_resolved` documents.

        Synchronous throughout, so on one event loop it cannot observe a
        half-applied drain; its caller holds ``_restore_lock`` across the read.
        """
        try:
            self.assert_ready()
        except SessionVolumePlanError:
            return None
        state = self._state
        # Unreachable via assert_ready; fail closed anyway.
        return None if state is None else state.measurement_volume_db

    async def _drain_restore(
        self,
        door: VolumeDoor,
        *,
        reason: str,
    ) -> SessionVolumeRestoreResult:
        """Resolve the one durable intent through confirmed readback (restore-once).

        Every abandon event funnels here: explicit close, the session-death hook,
        and the wall-clock ceiling. A stale-active session is force-drained here
        (the ceiling is enforced by the timestamp, never by a process restart).
        Restores the exact original if confirmable, else the emergency floor;
        latches unresolved only when neither confirms.

        **A DEFERRED rung stops the walk** — see :class:`RestoreOutcome`.
        Searching past it would leave the emergency floor standing as the
        owner's declared household level.

        Disclosed and accepted: when every rung genuinely fails to confirm, the
        emergency rung's declaration is the last one made, so the floor stands.
        The state is latched ``unresolved`` and recovery is offered.
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
                outcome = await door.restore_household_level_db(target)
                if outcome is RestoreOutcome.DEFERRED:
                    # STOP. Not a rung that failed — a level RECORDED behind a
                    # higher-ranked claim. Walking on would leave the emergency
                    # floor standing as the owner's answer. Nothing is latched:
                    # the session that outranks this drain is still running.
                    log_event(
                        logger,
                        "correction.session_volume_restore_deferred",
                        level=logging.INFO,
                        recovery=recovery,
                        reason=reason,
                        to_db=f"{target:.2f}",
                    )
                    return SessionVolumeRestoreResult.DEFERRED
                if outcome is not RestoreOutcome.LANDED:
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
        door: VolumeDoor,
        *,
        reason: str = "session_closed",
    ) -> SessionVolumeRestoreResult:
        """Restore the household volume exactly once and resolve (idempotent)."""
        return await self._drain_restore(door, reason=reason)

    async def abandon(
        self,
        door: VolumeDoor,
        *,
        reason: str = "session_abandoned",
    ) -> SessionVolumeRestoreResult:
        """Session-death observation hook (Wave 5 calls it) — drains restore-once."""
        return await self._drain_restore(door, reason=reason)

    async def enforce_ceiling(
        self,
        door: VolumeDoor,
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
            door, reason="wall_clock_ceiling_exceeded",
        )

    async def recover_unresolved(
        self,
        door: VolumeDoor,
    ) -> SessionVolumeRestoreResult:
        """The volume_recovery path: drain a latched unresolved OR stale-active state.

        A stale-active state drains too — the plan owns that via the timestamp.
        """
        return await self._drain_restore(door, reason="volume_recovery")


DEFAULT_SESSION_VOLUME_STATE_PATH = _DEFAULT_STATE_PATH
