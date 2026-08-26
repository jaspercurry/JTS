# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared fail-closed main-fader discipline: set-and-confirm, and hold.

The correction crossover flow owns a durable "restore the listening volume"
latch: it writes intent BEFORE mutating volume, sets the target and confirms it
through an independent readback, and restores exactly once. Two consumers now
need that confirm-readback core — the per-step ``CrossoverLevelLease``
(``jasper.web.correction_crossover_backend``) and the session-scoped
:class:`jasper.active_speaker.session_volume_plan.SessionVolumePlan` (Wave 2).

This leaf owns the one implementation so neither grows a copy-paste twin. It is
deliberately pure of state — the primitives plus the two shared constants (the
readback tolerance and the emergency attenuation floor). Each consumer owns its
OWN durable state schema and lifecycle — the schemas differ (the lease carries
source/role; the session plan carries an ``opened_at`` + wall-clock ceiling), so
only the fader primitives are genuinely shared:

* :func:`fader_matches` — the ONE "do these two fader dB values agree?" test.
* :func:`read_fader_db` — the ONE "a usable number, or nothing" normalization.
* :func:`set_and_confirm_volume` — set, then prove through a fresh readback.
* :func:`hold_fader_at` — prove the fader still sits where a
  measurement session declared it, refusing the capture when it does not
  (issue #2925). It reads; it never writes.

**Why "hold" exists (#2925).** Setting a volume once is not the same as it
STAYING set. A crossover-v2 stimulus is bracketed in a graph load/restore pair,
and the fader was landing somewhere other than the declared level across it: a
whole overnight campaign of measuring sweeps played 8.712 dB below the volume
its session plan had confirmed — and, because the excitation-safety ledger
admits a program against the DECLARED volume, that ledger was wrong for every
one of them. It was wrong quiet that night; the same seam reversed is loud. So
a declared measurement volume is re-proven per stimulus, not once per session.

**What actually moved the fader (#2929 — the mechanism #2925 named wrongly).**
Not CamillaDSP. It does NOT reset ``main_volume`` on a config replace: the
fader is process state that survives the reload (``jasper.camilla``'s
``_graph_mutation``, whose whole fade-down/fade-back-up design depends on
that), CamillaDSP's ``devices`` schema has no field that stores a volume at
all, and it rejects unknown keys outright. Read at the pinned version, tag
``v4.1.3`` (``05e9cfc``), 2026-08-24: ``src/config/mod.rs:806`` is
``#[serde(deny_unknown_fields)]`` on ``struct Devices`` and none of its 18
fields is a fader level; the daemon binary constructs
``ProcessingParameters::new`` once, at ``src/bin.rs:1119`` (the tree's only
other use is the Criterion harness ``benches/pipeline.rs``); and
``src/pipeline.rs:270`` re-seeds a rebuilt pipeline from the LIVE volume. The
mover was JTS's own
swap duck: ``_duck_release_target_db`` released to ``min(canonical, released)``
with no reference of its own, and ``canonical`` is the HOUSEHOLD target
(``percent_to_db(listening_level)``). A measurement volume is louder than the
household level, so the household value won that ``min`` on every routed
capture. Both dB values #2925 recorded as "the config's" are exactly
``percent_to_db`` outputs — −9.59596 is level 81, −18.181818 is level 64 —
which is what identified the real writer. #2929 gives the swap an explicit
release reference (the level the session plan owns), so the fader now lands on
the declared volume by construction.

That is what let wave 5 make the hold a TRIPWIRE and nothing else: it reads,
and it never writes. In a healthy routed session it reads in tolerance. A
``disagreed`` line is an anomaly worth investigating, and the capture that
follows it is refused rather than fought for.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Awaitable, Callable

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# The independent readback must land within this tolerance of the target for a
# volume mutation to count as confirmed. Ported verbatim from the crossover
# lease so both consumers agree on "confirmed".
READBACK_TOLERANCE_DB = 0.05

# The attenuated fallback a restore path drops to when it cannot confirm the
# exact original volume. A measurement session that cannot prove it restored the
# household's volume must leave the speaker safely quiet, not loud.
EMERGENCY_MEASUREMENT_VOLUME_DB = -60.0

SetMainVolumeDb = Callable[[float], Awaitable[Any]]
GetMainVolumeDb = Callable[[], Awaitable[Any]]

#: Errors a fader read/write is allowed to fail with. Named rather than blind.
#:
#: **The door contract this states.** Every consumer here — and
#: :class:`jasper.volume_owner.VolumeOwner`, which shares it rather than
#: keeping a second copy — takes its setter and getter by injection, and those
#: callables must report failure rather than raise a transport error of their
#: own. In practice that means binding ``CamillaController``'s methods with
#: ``best_effort=True``, which returns ``False``/``None`` instead of raising
#: ``CamillaUnavailable``; that class is deliberately NOT in this tuple, and
#: nothing in the tree wraps it into a ``RuntimeError`` on the way here.
#: Naming ``CamillaUnavailable`` would mean importing ``jasper.camilla``, which
#: both this leaf and the owner are imported BY.
FADER_IO_ERRORS = (OSError, RuntimeError, TimeoutError, ValueError)


class MeasurementFaderDrift(RuntimeError):
    """The main fader is not at the volume a measurement session declared.

    Raised by :func:`hold_fader_at` when a drifted fader could not be
    proven at the declared level. It is a REFUSAL, not a report: the caller must not
    emit the stimulus, because a capture taken at an unknown level is not a
    measurement and the excitation-safety ledger that admitted the program was
    computed against the declared volume, not this one.

    ``observed_db`` is ``None`` when the fader could not be read at all — the
    same refusal, since an unreadable fader is equally unprovable.
    """

    def __init__(
        self,
        *,
        expected_db: float,
        observed_db: float | None,
        context: str = "",
    ) -> None:
        seen = "unreadable" if observed_db is None else f"{observed_db:.6f} dB"
        where = f" during {context}" if context else ""
        super().__init__(
            f"the main fader is not at the declared measurement volume{where}: "
            f"expected {float(expected_db):.6f} dB, read {seen}"
        )
        self.expected_db = float(expected_db)
        self.observed_db = None if observed_db is None else float(observed_db)
        self.context = context


async def read_fader_db(get_main_volume_db: GetMainVolumeDb) -> float | None:
    """One live read, normalized to "a usable number, or nothing".

    The ONE place a fader reading is turned into ``float | None``, so
    "could not read" has a single spelling. A ``None``, a bool, a non-numeric
    or a non-finite reading all normalize to ``None``, and so does a raise from
    :data:`FADER_IO_ERRORS` — every consumer here is fail-closed and an
    unreadable fader must never render as a value.
    """
    try:
        value = await get_main_volume_db()
    except FADER_IO_ERRORS:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def fader_matches(
    observed: Any,
    expected_db: float,
    *,
    tolerance_db: float = READBACK_TOLERANCE_DB,
) -> bool:
    """The one "do these two fader dB values agree?" test.

    ``True`` only for a real, finite number within ``tolerance_db`` of
    ``expected_db``. A ``None``, a bool, a non-numeric or a non-finite reading
    is a disagreement, never a pass — every caller here is fail-closed, and
    "could not read" must never render as "matches".
    """
    if (
        isinstance(observed, bool)
        or not isinstance(observed, (int, float))
        or not math.isfinite(float(observed))
    ):
        return False
    return abs(float(observed) - float(expected_db)) <= tolerance_db


async def set_and_confirm_volume(
    target_db: float,
    set_main_volume_db: SetMainVolumeDb,
    get_main_volume_db: GetMainVolumeDb,
    *,
    tolerance_db: float = READBACK_TOLERANCE_DB,
) -> bool:
    """Set the main volume and confirm it through an independent readback.

    Returns ``True`` only when the setter did not report failure AND a fresh
    readback lands within ``tolerance_db`` of ``target_db``. Any setter/readback
    error, a ``False`` setter return, or a non-finite/mismatched readback yields
    ``False`` — the fail-closed contract a restore latch depends on.
    """
    try:
        applied = await set_main_volume_db(float(target_db))
        if applied is False:
            return False
        observed = await get_main_volume_db()
    except FADER_IO_ERRORS:
        return False
    return fader_matches(observed, target_db, tolerance_db=tolerance_db)


async def hold_fader_at(
    expected_db: float,
    get_main_volume_db: GetMainVolumeDb,
    *,
    context: str = "",
    tolerance_db: float = READBACK_TOLERANCE_DB,
) -> float:
    """Prove the fader is at ``expected_db``, or refuse. It never writes.

    The per-stimulus half of the measurement-volume discipline (#2925 T1-1),
    and the ONE implementation both the routed MEASURE path and the summed
    VERIFY path use — the defect this closes lived precisely in the gap between
    those two paths, so a per-phase copy would rebuild it. Pure and stateless
    by design: WHICH volume to hold, and whether a session still owns one, is
    :meth:`~jasper.active_speaker.session_volume_plan.SessionVolumePlan.hold_measurement_volume`'s
    question, and that is what a capture path calls.

    **It proves; it does not repair.** Establishing the measurement volume is
    :meth:`~jasper.active_speaker.session_volume_plan.SessionVolumePlan.open`'s
    job, done once per session with set-and-confirm. A per-stimulus repair here
    was a SECOND writer moving the fader behind the session's back, which is
    what wave 5 collapses; and since #2929's release reference lands each swap
    on the declared level, the drift it used to paper over is largely gone.
    What is left is a foreign writer moving the fader mid-session — and the
    honest answer to that is to refuse the capture, not to fight for the fader.

    Order, all fail-closed:

    1. Read the live fader. An unreadable read is treated as a disagreement,
       so it does not pass.
    2. Already within ``tolerance_db``: return it. The happy path costs one
       read and writes nothing.
    3. Otherwise take a further INDEPENDENT read. A single failed or racing
       round-trip is not a level that cannot be established, and this is also
       what keeps ``observed_db`` a reading JTS actually took.
    4. Still not there: raise :class:`MeasurementFaderDrift`. The caller refuses
       the capture rather than banking it.

    **What the refusal line tells a support read.** ``observed_db`` is empty
    ONLY when the fader could not be read — that is the one clean
    discriminator, and it is a real reading rather than an inference because
    the proving re-read above is unconditional.

    Returns the proven fader reading.
    """

    target = float(expected_db)

    observed = await read_fader_db(get_main_volume_db)
    if observed is not None and fader_matches(
        observed, target, tolerance_db=tolerance_db
    ):
        # THE LIVENESS HALF, and it is why "no repair lines" can be read as
        # evidence at all (#2929). Since the release now lands the fader on the
        # declared level by construction, the healthy run's repair pair is
        # ABSENT — and absence is exactly what a hold that never ran also looks
        # like (the #2198 instrument-silence lesson). This line is what makes
        # the two distinguishable: it says the hold RAN and found the level
        # already established. Same `event=` as the drift lines, discriminated
        # by `result=`, so one vocabulary answers one question. INFO rather
        # than DEBUG because it is an acceptance criterion a household run has
        # to be readable against without a log-level change, and it is bounded
        # by captures per session (~16), not by time.
        log_event(
            logger,
            "active_speaker.measurement_fader_drift",
            result="held",
            context=context,
            expected_db=f"{target:.6f}",
            observed_db=f"{observed:.6f}",
            delta_db=f"{observed - target:.6f}",
            tolerance_db=f"{float(tolerance_db):.6f}",
        )
        return observed

    log_event(
        logger,
        "active_speaker.measurement_fader_drift",
        level=logging.WARNING,
        result="disagreed",
        context=context,
        expected_db=f"{target:.6f}",
        observed_db="" if observed is None else f"{observed:.6f}",
        delta_db="" if observed is None else f"{observed - target:.6f}",
        tolerance_db=f"{float(tolerance_db):.6f}",
    )
    # UNCONDITIONAL. The refusal's ``observed_db`` is the only thing
    # distinguishing "the fader read fine and is at the wrong level" from "the
    # fader could not be read", and reporting the first read's outcome for both
    # states an observation JTS never made — the ``locate_failed`` #2085 class
    # this whole discipline exists to close. It is also a second chance for a
    # round-trip that failed or raced, so one missed read is not a refusal.
    proven = await read_fader_db(get_main_volume_db)
    if proven is None or not fader_matches(
        proven, target, tolerance_db=tolerance_db
    ):
        log_event(
            logger,
            "active_speaker.measurement_fader_drift",
            level=logging.ERROR,
            result="refused",
            context=context,
            expected_db=f"{target:.6f}",
            observed_db="" if proven is None else f"{proven:.6f}",
        )
        raise MeasurementFaderDrift(
            expected_db=target, observed_db=proven, context=context,
        )
    log_event(
        logger,
        "active_speaker.measurement_fader_drift",
        level=logging.WARNING,
        result="held",
        context=context,
        expected_db=f"{target:.6f}",
        observed_db=f"{proven:.6f}",
        reread="true",
    )
    return proven
