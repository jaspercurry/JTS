# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Session-scoped fixed measurement volume for the crossover session (Wave 2).

The v2 crossover measurement flow
(docs/historical/crossover-measurement-productization-design.md §5.5)
replaces the per-step ramp/lock machinery with ONE fixed measurement volume
held for the whole session: snapshot the household volume on open, set the
fixed measurement volume, restore it exactly once on close/abandon. Per-driver level differences live in
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
  ``unresolved`` (the volume_recovery path) when readback cannot confirm;
* a PER-STIMULUS re-proof of the volume it opened
  (:meth:`SessionVolumePlan.hold_measurement_volume`, #2925). "Held for the
  whole session" is the intent; whether the fader actually stayed there is a
  separate fact, and the graph swap around each stimulus used to answer no —
  its duck released to the household level rather than the declared one
  (#2929; wave 6d then stopped the measurement swap ducking at all, and the
  bound that defect broke lives in :func:`jasper.volume_owner.duck_release_target_db`).
  A per-step lease never needed this because it re-set the volume every step.
  The re-proof stays as the tripwire that would catch the next such writer.

The fixed measurement volume is not hard-coded: :func:`session_measurement_volume_db`
DERIVES it as ``min`` of two halves — a REFERENCE level (measured by the
seat-SPL leveling step when a box has run it, else the codified default) and the
active drivers' excitation CEILING — and the SAME value feeds both the program
composer's downstream gain and program admission — one definition path (SSOT),
so caps are enforced regardless of its value.
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
# (:meth:`SessionVolumePlan.set_wall_clock_ceiling_s`). The walked-away
# guarantee is "a speaker nobody is standing at returns to household volume
# within a bounded window"; a longer measurement may legitimately widen that
# window (the crossover v2 cloud walks up to 16 prompted captures) but nothing may
# remove the bound. One hour is the outer edge of a plausible guided
# measurement and comfortably inside "someone would have come back by now".
MAX_WALL_CLOCK_CEILING_S = 3600.0

# The codified measurement reference level: the fixed session volume when no
# driver cap binds below it AND no measured reference has been banked. -20 dB
# gives the least-sensitive driver a usable acoustic measurement level while
# leaving 20 dB of digital+DSP margin under the 0 dB ceiling. It is a design
# input (2026-07-18 W2 gate ruling), not a hardware-measured one — which is
# exactly why it is now a DEFAULT rather than the only answer: a box that has
# run the seat-SPL leveling step
# (:mod:`jasper.active_speaker.seat_level_ramp`) replaces it with the volume
# that measured the operator's chosen seat SPL on that speaker in that room.
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

    Three answers, not two, and the third is why: an owner-backed door can
    RECORD a household level that a higher-ranked claim keeps off the fader.
    Reading that as a failed write is what makes a ladder walk to its emergency
    rung and declare the floor over a perfectly good level — so deferral gets
    its own answer and the ladder stops on it.
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

    One reader of the seat-SPL leveling step's one written fact
    (:func:`jasper.active_speaker.seat_level_reference.seat_level_reference_volume_db`).
    Absent, unreadable, or implausible state resolves to
    :data:`MEASUREMENT_REFERENCE_VOLUME_DB`, so a box that never ran the step
    behaves exactly as it did before the step existed.
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

    ``program_admission=True``: these caps serve the v2 session's CHECK/MEASURE
    programs, where they set each driver's composed segment level and are
    enforced per channel at admission — always the proven-HP path. The leveling
    step reads the same call, for the permitted band it validates on the way
    and to DISCLOSE the caps beside the volume it derives
    (:func:`unsegmented_stimulus_ceiling_db`).
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

    This is the caps half of :func:`session_measurement_volume_db` and **only**
    that. It works there because a v2 program carries a per-segment digital
    gain per driver: every driver other than the highest-cap one is attenuated
    DOWN to its own cap inside the program, so `max` is the volume at which the
    least-sensitive driver can finally be measured.

    **It is NOT "the loudest volume every driver permits".** On the repo's own
    woofer/compression-driver fixture it returns ``0.0`` dB while the tweeter's
    cap sits at −65 — a number only a composed program's segment gains make
    sense of. Anything playing one signal through the whole graph with no
    per-driver segment gain must use :func:`unsegmented_stimulus_ceiling_db`
    instead, which bounds that signal by the digital headroom it actually has.
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

    "Has room" is a DIGITAL question and only a digital question. A signal
    played through the whole graph carries no per-driver segment gain, so
    nothing attenuates it down to any one driver's ledger — and what runs out
    is headroom: the first branch to reach full scale is where the volume
    stops, and nothing stops it earlier::

        ceiling = MAX_TEST_LEVEL_DBFS - binding_peak_dbfs

    :data:`~jasper.active_speaker.calibration_level.MAX_TEST_LEVEL_DBFS` is
    0 dBFS, the same global loudest-admissible test level every driver target
    is clamped to — one owner for that bound, not a second private constant.
    Which peak binds depends on whether the caller can say what each driver's
    branch actually receives.

    **Full-band bound (no ``branch_peaks_dbfs``)** — every branch is assumed to
    see the whole stimulus peak, so ``binding_peak_dbfs`` is that peak. This is
    the answer for every caller that passes no branch peaks, and the fallback
    whenever the branch facts are incomplete.

    **Per-branch bound (``branch_peaks_dbfs`` given)** — the caller has rendered
    the ACTUAL stimulus through the ACTUAL live graph and measured what each
    driver's branch receives, so ``binding_peak_dbfs`` is ``max`` over the
    branches: the loudest branch is the one that clips first. It moves in BOTH
    directions — a branch whose chain boosts (a peaking EQ with positive gain)
    reports a peak ABOVE the full-band figure and binds TIGHTER than the
    full-band bound does.

    **Declared per-driver caps do not bound this volume — they are DISCLOSED,
    and that holds even for a published one.** Since the 2026-08-23 owner
    ruling ``level_duration_limits.max_effective_peak_dbfs`` is asked for only
    where a manufacturer publishes a level limit, so a value on the record IS a
    datasheet fact and it does still bind — but per DRIVER, at admission and in
    the composed segment level, not on the main volume of one un-segmented
    signal. The volume this function returns is bounded by headroom alone.

    Why even a published cap does not bind here: this number is the volume at
    which a signal admits *digitally*, and a per-driver level limit cannot be
    enforced on a signal that carries no per-driver gain — clamping the whole
    speaker to the tightest driver's figure is what made a −65 protocol default
    the operative ceiling and pinned a 75 dB SPL seat target at 68.3 dB with
    ~30 dB of digital headroom unused (owner ruling: "It's a fixed-gain amp —
    we do ALL volume via software... No limit unless we have no headroom to
    give"). What bounds the driver instead is the mic-measured commissioning
    SPL stop, live, on the way up.

    So the caps are resolved, and what they WOULD have
    refused is named on ``event=active_speaker.unsegmented_ceiling_bound``:
    each driver's cap, what its branch actually receives at this ceiling, how
    far past its cap that lands, and the decibels the caps were leaving unused.
    Resolving them still raises when a driver's ledger cannot be read, because
    that same call resolves the permitted BAND — the wrong-frequency-range
    invariant, which is untouched and still refuses.

    What still stops this volume: full scale above, the per-driver soft-clip
    limiters and ``devices.volume_limit`` in the emitted graph, the ramp's own
    ``HARD_CEILING_DBFS`` rail, and — live, on measured samples — the
    commissioning SPL stop.

    ``branch_peaks_dbfs`` is keyed by ``target_fingerprint`` and is a claim about
    one specific stimulus through one specific graph. It is never reusable: a
    different WAV, or the same WAV after the graph is re-applied, needs a fresh
    render. :mod:`jasper.active_speaker.branch_peak` owns producing it and
    refuses rather than guessing.

    **Fail-conservative, never fail-open.** A branch peak that is missing for any
    active driver, non-finite, or unusable falls back to the full-band bound and
    names ``reason=branch_peaks_incomplete``. An incomplete render can only ever
    make this quieter, since a branch with no fact would otherwise stop
    constraining the ceiling.

    Mic-independent by construction: no calibration, sensitivity, or measured
    level enters this number, so a mis-scaled microphone cannot move it.
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
    disclosure below cannot disagree about which branches resolved.
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
    partial render would bound the speaker by only the branches that happened
    to resolve, which is exactly the fail-open direction this must never take.
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

    Returns one log-safe token per driver —
    ``<fingerprint>:cap=<x>,branch=<y>,at_ceiling=<z>,past_cap=<+d>`` — and the
    ceiling the declared caps alone would have produced, so an operator can see
    both what each driver receives at this volume and how much room the caps
    were leaving on the table. ``past_cap`` is signed: positive means this
    volume drives that driver past its declared figure.
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

    The session volume's job is to let the LEAST-sensitive (highest-cap) driver
    reach a usable measurement level with digital headroom, while more-sensitive
    drivers attenuate DOWN to their own caps via per-segment digital gains —
    attenuating downward is always satisfiable, so every driver's cap is
    enforceable at this volume (2026-07-18 W2 gate ruling)::

        session_volume = min(measurement_reference_volume_db(),
                             loudest_driver_cap_dbfs())

    The reference half is operator-derivable (the seat-SPL leveling step banks a
    measured value; absent one it is the codified -20 dB default) and the caps
    half is not: whatever the reference says, ``min`` means every driver's
    excitation ceiling still binds.

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
    # ``declared_sensitivities`` (the declaration's per-role datasheet values)
    # rides into the caps derivation so the caps entering max(caps) are the SAME
    # derived caps admission enforces.
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
    sentences. Three doors ask this today -- ``jasper-angle-capture stage``,
    ``jasper-seat-level`` and ``jasper-audition`` -- and they must get the SAME
    answer from the SAME file, which is why the question lives here beside the
    plan rather than in either door.

    **Two different facts are being asked about, and each is asked of its own
    owner.** Answering both from one place is what makes this a door and not a
    second source of truth:

    * **Is a measurement LIVE right now?** That is the open measurement window,
      and its cross-process copy lives in jasper-control
      (``jasper.control.measurement_hold``) — the same self-expiring hold
      ``measurement_window()`` takes for every measuring surface, web and CLI
      alike. Asking jasper-control means this door refuses a *crossover-web*
      session too, which the volume statefile alone could not distinguish from
      a crashed one. Acting under a live measurement would either queue an
      instruction for a session past the point of taking one, or fight it for
      the very volume it is holding.
    * **Did a previous measurement leave the volume UNRESOLVED?** That is a
      volume-recovery fact, not a liveness fact, and it stays with
      :class:`SessionVolumePlan`'s durable statefile — it outlives every
      process, which is exactly the point of it. Staging into it is staging
      for a session that will refuse to start, so the refusal belongs here
      where the operator can act on it.

    The two other exclusion layers the crossover flow has —
    ``correction_setup._crossover_blocking_phase`` and ``_begin_relay_capture``'s
    single relay slot — are module-globals of the ``jasper-correction-web``
    process. A CLI cannot see either, and a copy of them here would be a second
    answer that is wrong the moment the real one changes.

    **When jasper-control cannot be reached**, the liveness question falls back
    to the durable statefile — ``needs_recovery`` minus ``stale_active``, which
    is precisely the answer this door gave before the hold existed. That is a
    documented degradation with one authority and one fallback, not two
    competing answers: the box that owns the live fact is unreachable, so the
    most conservative record still readable is used instead.

    A **stale** active state -- one past its own wall-clock ceiling -- is NOT a
    live session and does not refuse: that is the crashed-session shape the
    flow's own open path force-drains
    (``reconcile_session_volume_for_new_session``), and blocking on it would
    make a crash permanently un-stageable from this door.

    Returns the refusal detail (a sentence), or ``None`` when the coast is
    clear -- including when there is no state file at all, which is the ordinary
    idle speaker.
    """
    # The path is EXPLICIT, and that is load-bearing rather than tidy: a
    # ``SessionVolumePlan()`` with no ``state_path`` reads no file at all and
    # would answer "idle" for every speaker on earth. A guard that cannot see
    # the state it guards is worse than no guard, because it reads as one.
    # The public name, resolved as a module global at CALL time, so a test (or
    # an operator override) that repoints it is seen here.
    plan = SessionVolumePlan(
        state_path=(
            DEFAULT_SESSION_VOLUME_STATE_PATH if state_path is None else state_path
        )
    )
    # Unresolved first: it is a pure file read, it is mutually exclusive with
    # the active state (they are different values of one ``status`` field), and
    # it is the more actionable of the two sentences.
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
    # ``needs_recovery`` minus the unresolved arm (already answered above) is
    # "durably active and not opened by this process", and a freshly built plan
    # in a CLI process has opened nothing. ``stale_active`` drops the crashed
    # shape the flow force-drains. Together: exactly the pre-hold answer.
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


class VolumeDoor(Protocol):
    """How this plan reaches the fader — the ONE injected door, three questions.

    The plan owns no CamillaDSP and never will. What changed is the shape of
    what it owns instead: a ``(set, get)`` pair let every caller decide for
    itself what a write meant, and the two things this plan writes are not the
    same thing. Opening ESTABLISHES a measurement level nothing may be admitted
    against until it is confirmed; draining RESTORES the household level the
    session snapshotted. Naming them apart is what lets one binding write
    through a ranked claim and another write straight at the fader, without the
    plan learning which it got.

    **Both verbs answer the same question, and it is the fader's:** *is the
    fader confirmed at this level?* Not *did a write go out*, and not *is some
    intent recorded* — the exact→emergency ladder and its durable latch are
    built on a real readback, and a door that answered anything softer would
    let :meth:`SessionVolumePlan._drain_restore` clear a resolved marker over a
    speaker still sitting at measurement level.
    """

    async def read_household_level_db(self) -> float | None:
        """The level to come back to, or ``None`` when it cannot be read.

        Taken once, at open, before the first mutation — the snapshot the whole
        walked-away guarantee restores toward. ``None`` is an ordinary answer
        (an unreadable fader at open), and the plan falls through to the
        emergency floor rather than inventing a level.
        """
        raise NotImplementedError

    async def establish_measurement_level_db(self, level_db: float) -> bool:
        """Put the fader on the session's measurement level; confirmed?"""
        raise NotImplementedError

    async def restore_household_level_db(self, level_db: float) -> RestoreOutcome:
        """Put the fader back on a household level. Landed, deferred, failed?

        Three answers because an owner-backed door genuinely has three. A
        ``DEFERRED`` is NOT a failure and must not be treated as one: the level
        is recorded and lands when the higher-ranked claim releases, so the
        ladder stops rather than searching for a level that would also defer —
        and, worse, standing as the declared one.
        """
        raise NotImplementedError


@dataclass(frozen=True)
class FaderVolumeDoor:
    """The direct door: set-and-confirm straight at the fader.

    Both verbs land on the same
    :func:`~jasper.active_speaker.volume_latch.set_and_confirm_volume`, because
    at this door they genuinely are one act — it is the CALLER's authority that
    differs, not the write. Callers that arbitrate their fader through
    :class:`~jasper.volume_owner.VolumeOwner` bind a door that does; callers in
    a process with no owner to arbitrate through bind this one, and
    :func:`~jasper.volume_owner.volume_owner` answering ``None`` there is a
    fact about that process rather than a defect.
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
        """Never defers: this door writes the fader itself, so there is no
        arbitration to lose and no third answer to give."""
        confirmed = await set_and_confirm_volume(
            level_db, self.set_main_volume_db, self.get_main_volume_db
        )
        return RestoreOutcome.LANDED if confirmed else RestoreOutcome.FAILED


class SessionVolumePlan:
    """Owner of one session-scoped fixed measurement volume + its restore latch.

    Owns no CamillaDSP: every method that mutates volume takes one
    :class:`VolumeDoor` (mirroring ``CrossoverLevelLease``, which took the
    callables raw). The process-global production instance injects a durable
    state path; test instances stay in-memory unless they opt into one.
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

    def set_wall_clock_ceiling_s(self, ceiling_s: float) -> None:
        """Re-arm the ceiling the NEXT :meth:`open` will stamp into state.

        The ceiling is a property of the measurement about to run, not of the
        process: a full-tier 15-capture crossover commission legitimately takes longer than
        the 3-entry flow this default was sized for, and the caller that built
        the plan is the only one that knows which. Setting it before ``open``
        keeps the durable state self-describing — ``stale_active`` and the
        force-drain read the ceiling the OPEN session recorded, so an
        already-open session is unaffected and a hydrated one keeps whatever it
        was opened with.

        Fail-closed: a non-finite or non-positive value is refused rather than
        silently disabling the walked-away guarantee, and the value is capped at
        the module's own :data:`MAX_WALL_CLOCK_CEILING_S` so no caller can
        stretch it without end.
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
        """Drop the in-memory intent FIRST, then record that on disk.

        The order is load-bearing, and #2925's hearing-safety lens is what
        found it. Persisting first left one uncovered shape: a drain that had
        already CONFIRMED its volume on the hardware, then met an OSError
        writing the marker (read-only or full ``/var/lib/jasper``), raised out
        of here with the plan still ``active``, ``_opened_this_process`` still
        true and ``measurement_volume_db`` intact — so :meth:`assert_ready`
        passed and the plan still named the declared volume onto a speaker
        sitting at the emergency floor. Measured at −60.0 → −12.5: **+47.5
        dB**. Clearing first means the very next reader sees a plan that owns
        nothing. No reader can still WRITE that number:
        :meth:`hold_measurement_volume` is the last one and no longer writes at
        all, so through it the same stale answer refuses a capture instead of
        raising the fader.

        The persist is then guarded exactly as :meth:`_mark_unresolved` guards
        its own, and for the mirror-image reason. There the prior intent
        SURVIVING on disk is what keeps a restart fail-closed; here it is what
        makes a restart offer recovery for a volume already restored. That is
        the safe direction — the re-drain re-asserts the household snapshot,
        which is the right target whether or not the fader is already there
        (in this very case it is not: the fader is at the emergency floor, and
        the re-drain moves it UP to the household level, never to the
        measurement one) — and it is loud rather than silent, which is the
        whole point: the alternative is a process that can still put the
        measurement volume back up, unasked, at the next capture. The re-drain
        instead runs behind the recovery screen, or automatically once the
        wall-clock ceiling has passed
        (``correction_crossover_v2.enforce_session_volume_ceiling_if_stale``,
        which needs no click) — both target the household snapshot, and
        :meth:`assert_ready` refuses every hold either way.
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
        """Re-prove this session's measurement volume for ONE stimulus (#2925).

        Setting a volume once is not the same as it staying set: every
        crossover-v2 MEASURE-phase stimulus is bracketed in a graph
        load/restore pair, and that pair's own duck used to release the fader
        to the household level instead of the declared one — which is how a
        whole overnight campaign of sweeps played 8.712 dB below the volume
        this plan had confirmed. So a capture path asks HERE, per stimulus,
        rather than trusting :meth:`open`.

        #2929 removed that writer at its source, and wave 6d then stopped the
        measurement swap ducking at all — so a healthy routed capture reads in
        tolerance and this method writes nothing. It stays as the tripwire —
        the declared volume is still re-proven per stimulus, and a repair now
        means something genuinely moved the fader.

        Returns the proven fader reading, or ``None`` when this plan does not
        currently own a volume to hold — nothing open, latched ``unresolved``,
        crash-hydrated, or past its wall-clock ceiling. The caller then leaves
        the fader alone. That is deliberately not a refusal: it is
        :meth:`assert_ready`'s question, already asked and answered by
        ``play_program`` on the routed path, and a second gate would be a
        second owner.

        Raises :class:`~jasper.active_speaker.volume_latch.MeasurementFaderDrift`
        when the fader cannot be proven at the declared level — the caller
        refuses the capture rather than banking a measurement taken at an
        unknown level. The hold does not write: :meth:`open` established the
        level, and a second writer per stimulus is what wave 5 removes.

        **Under the restore lock**, which is the whole reason this lives on the
        plan rather than at the capture seam. ``_mark_unresolved`` copies
        ``measurement_volume_db`` forward, so a plan whose restore could not be
        confirmed still REPORTS the declared volume; a hold that read that
        field without serializing against the drains could re-raise the fader
        onto a speaker that had just been left at the emergency floor, and
        leave it there with no owner. Serialized, the readiness check either
        precedes the drain (the volume is genuinely still ours) or follows it
        (state cleared, or unresolved → ``None``).

        Serializing is necessary and was not sufficient: a drain can fail
        PARTWAY, after confirming the fader and before recording it, and that
        shape reached a passing :meth:`assert_ready`. :meth:`_clear_resolved`
        now closes it by dropping the in-memory intent before it persists —
        see there for the measured +47.5 dB it prevents.
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
        now?", asked by :meth:`hold_measurement_volume`. Reading
        ``measurement_volume_db`` directly instead is the +47.5 dB hazard
        :meth:`_clear_resolved` documents: ``_mark_unresolved`` copies that
        field forward, so a plan whose restore could not be confirmed still
        REPORTS the declared volume while owning nothing.

        Synchronous, and every read it makes is synchronous, so on one event
        loop it cannot observe a half-applied drain: the drains mutate this
        state with no ``await`` between dropping the intent and recording it.
        Its caller establishes "no drain is mid-flight" by HOLDING
        ``_restore_lock`` across the read.
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
        and the wall-clock ceiling. Unlike ``CrossoverLevelLease``, this NEVER
        refuses an ``active`` state — a stale-active session is force-drained here
        (the ceiling is enforced by the timestamp, not by a process restart).
        Restores the exact original if confirmable, else the emergency floor;
        latches unresolved only when neither confirms.

        **A DEFERRED rung stops the walk** — see :class:`RestoreOutcome`. The
        ladder is a search for a level the fader will take, and a deferral is
        not a refusal to take one: it is a higher-ranked claim holding the
        fader with this level already recorded behind it. Searching past it
        would leave the emergency floor standing as the owner's declared
        household level.

        **Disclosed, and accepted: a PURE IO failure with no claim held still
        leaves the floor standing.** When the door is owner-backed and every
        rung genuinely fails to confirm, the emergency rung's declaration is
        the last one made, so the owner's standing household level is the
        floor. That is fail-QUIET rather than fail-loud, and it is the right
        direction — the fader could not be moved at all, the state is latched
        ``unresolved``, and the recovery screen offers. A speaker whose fader
        will not respond is not one to leave declaring a listening level.
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
                    # STOP. Not a rung that failed — a level that is RECORDED
                    # and will land when the claim above it releases. Walking
                    # on would declare the emergency floor over a perfectly
                    # good household level and leave the floor standing as the
                    # owner's answer, which is the louder mistake by 47 dB.
                    # Nothing is latched and no recovery is offered: there is
                    # nothing for a person to repair, and the session that
                    # outranks this drain is still running.
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

        Unlike ``CrossoverLevelLease.recover_unresolved_volume_safety`` (which
        refuses ``active`` states), this drains a stale-active state too — the
        plan owns its stale-active handling via the timestamp.
        """
        return await self._drain_restore(door, reason="volume_recovery")


DEFAULT_SESSION_VOLUME_STATE_PATH = _DEFAULT_STATE_PATH
