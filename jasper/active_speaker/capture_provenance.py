# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The acoustic context one measurement capture was actually taken under.

**The config label is not the graph.** A CHECK/MEASURE program plays through a
transient per-driver ROUTING graph (each program channel straight to its
driver's physical output, crossover / delay / linearization OMITTED) loaded
INLINE with ``SetConfig``, and that loader leaves the persisted
``config_file_path`` unchanged -- so a capture taken through the routing graph
and one taken through the standing applied graph report the SAME
``config_path`` through radically different transfer functions.
``graph.config_path`` is recorded precisely BECAUSE it is the misleading label:
beside ``kind`` and ``fingerprint`` it shows what the statefile claimed versus
what was running. ``kind`` is the only field that can never read ``null`` -- it
is structural knowledge held by the branch that loaded (or did not load) the
graph, not a probe that can fail. ``graph.fingerprint`` compares to OTHER
capture provenance and to nothing else; it is not a round receipt's
``entry_graph_fingerprint``.

Every field is read from the ONE live owner of that fact at the instant the
stimulus is emitted. :func:`volume_fields_agree` compares ``main_volume_db``
against ``session_volume_db`` (#2925 T1-2: an overnight campaign carried the
two two lines apart disagreeing by 8.712 dB, found five days later by fitting
the banked curves) and is a DISCLOSURE, not a gate -- nothing here may break a
capture. The fail-CLOSED half is the playback path's volume hold, which shares
one tolerance with this comparison so the two cannot disagree about "agree".

Every read is individually guarded and :func:`record_capture_provenance` wraps
the lot in a blind belt; an unreadable surface leaves its field ``None`` and
contributes its name to one WARN ``event=active_speaker.capture_provenance``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from jasper.log_event import log_event

from .volume_latch import fader_matches

logger = logging.getLogger(__name__)

# The two values ``CaptureProvenance.graph_kind`` takes. Unrelated to
# ``commissioning_host.CommissioningGraphKind``, which names a commissioning
# attempt's polarity, not a capture's topology.

#: The transient per-driver routing graph a CHECK/MEASURE program is played
#: through, with the crossover, delays and linearization left OUT. Loaded under
#: the DSP writer lock for the length of one sweep and restored after.
GRAPH_KIND_PROGRAM_ROUTING = "program_routing"

#: No graph was loaded: the standing graph on the speaker IS the system under
#: test. Every summed-sweep phase plays this way.
GRAPH_KIND_APPLIED = "applied"


@dataclass(frozen=True)
class CaptureProvenance:
    """What was live while one capture's stimulus played."""

    graph_kind: str
    main_volume_db: float | None = None
    session_volume_db: float | None = None
    graph_config_path: str | None = None
    graph_fingerprint: str | None = None
    stimulus_program_id: str | None = None
    stimulus_phase: str | None = None
    stimulus_wav_sha256: str | None = None
    stimulus_peak_dbfs: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """The sidecar block, written under one ``provenance`` key."""
        return {
            "main_volume_db": self.main_volume_db,
            "session_volume_db": self.session_volume_db,
            "graph": {
                "kind": self.graph_kind,
                "config_path": self.graph_config_path,
                "fingerprint": self.graph_fingerprint,
            },
            "stimulus": {
                "program_id": self.stimulus_program_id,
                "phase": self.stimulus_phase,
                "wav_sha256": self.stimulus_wav_sha256,
                "peak_dbfs": self.stimulus_peak_dbfs,
            },
        }


class CaptureProvenanceRecorder:
    """The one-capture handoff from the play seam to the retention seam.

    The two seams are different closures on different call stacks and the play
    seam returns ``None``, so the fact cannot be handed back through it.

    :meth:`take` CONSUMES. A second read with no play in between is a capture
    this recorder cannot speak for, and gets ``None`` rather than the previous
    capture's context: stale provenance on a forensic clip is worse than absent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: CaptureProvenance | None = None

    def record(self, provenance: CaptureProvenance) -> None:
        with self._lock:
            self._pending = provenance

    def take(self) -> CaptureProvenance | None:
        with self._lock:
            pending, self._pending = self._pending, None
        return pending


def volume_fields_agree(provenance: CaptureProvenance) -> bool:
    """Do this record's two volume fields tell the same story? (#2925 T1-2)

    ``True`` when the live fader read matches the volume the session declared,
    within the same readback tolerance ``SessionVolumePlan.open`` confirmed that
    volume under, so a wider disagreement means the capture is at a level the
    session never confirmed. ``True`` also when ``session_volume_db`` is
    ``None``: no measurement volume was open, so the record makes no claim. A
    ``None`` ``main_volume_db`` against a declared volume IS a disagreement --
    the record claims a level it could not read.
    """
    if provenance.session_volume_db is None:
        return True
    return fader_matches(provenance.main_volume_db, provenance.session_volume_db)


#: What a guarded provenance read may swallow. A named tuple of concrete types
#: rather than a blind ``except Exception``: the CamillaDSP surface is already
#: closed, so what remains is a fingerprint helper's ``RuntimeError`` and the
#: shape errors a test double can produce. A genuinely unexpected type SHOULD
#: fall through to ``record_capture_provenance``'s blind belt rather than be
#: absorbed here, where it would silently null one field and hide the reason.
_READ_ERRORS = (OSError, RuntimeError, TypeError, ValueError, AttributeError)


def _stimulus_peak_dbfs(program: Any) -> float | None:
    """The loudest stimulus segment's declared digital peak, in dBFS.

    ``ProgramSegment.gain_db`` is the digital gain applied to the unit-peak
    stimulus, so for a unit-peak stimulus it IS that segment's peak dBFS in the
    rendered WAV. Scoped to ``stimulus_segments()``: the courtesy prelude is
    derived never to exceed its channel's loudest stimulus, so including it
    could only mask a render bug. Session volume is NOT folded in -- it is its
    own field, from its own owner.
    """
    segments = program.stimulus_segments()
    peaks = [float(segment.gain_db) for segment in segments]
    return max(peaks) if peaks else None


async def record_capture_provenance(
    recorder: CaptureProvenanceRecorder | None,
    *,
    open_cam: Callable[[], Any],
    graph_kind: str,
    program: Any,
    phase: str,
    artifact: Any = None,
    read_volume_plan: Callable[[], Any] | None = None,
) -> None:
    """Observe, and hand the result to ``recorder``. The never-break-a-capture belt.

    The entry point a playback path should call. Deliberately BLIND, because the
    contract it enforces is not "handle the known failures" but "a forensic
    sidecar for an operator-enabled ring may never cost a household its
    measurement". ``BaseException`` still passes: a cancelled measurement must
    stay cancelled. A ``None`` recorder is one more thing it absorbs, so no
    playback branch has to remember a pre-check.

    The controller and the volume plan arrive as ZERO-ARGUMENT RESOLVERS so that
    obtaining them happens inside the belt too: passed as values they would be
    evaluated in the caller's argument list, outside the try, on the play path.
    """
    if recorder is None:
        return
    try:
        cam = open_cam()
        volume_plan = read_volume_plan() if read_volume_plan is not None else None
        recorder.record(
            await observe_capture_provenance(
                cam=cam,
                graph_kind=graph_kind,
                program=program,
                phase=phase,
                artifact=artifact,
                volume_plan=volume_plan,
            )
        )
    except Exception:  # noqa: BLE001 - see the docstring: blind is the contract
        log_event(
            logger,
            "active_speaker.capture_provenance",
            level=logging.WARNING,
            result="failed",
            graph_kind=graph_kind,
            exc_info=True,
        )


async def observe_capture_provenance(
    *,
    cam: Any,
    graph_kind: str,
    program: Any,
    phase: str,
    artifact: Any = None,
    volume_plan: Any = None,
) -> CaptureProvenance:
    """Read the live context for the stimulus that is about to play.

    Call this as late as possible -- for the program branch, AFTER the routing
    graph is loaded and inside the same writer lock, because
    ``get_active_config_raw`` is what distinguishes the two graphs and it answers
    the applied graph right up until the load lands. Each read is guarded on its
    own and the unreadable names are collected into a single WARN;
    :func:`record_capture_provenance` is the blind outer belt a playback path
    should call.
    """
    unreadable: list[str] = []

    async def probe(name: str, read: Any) -> Any:
        """One live read. A raise AND a ``None`` both mean "could not read"."""
        try:
            value = await read()
        except _READ_ERRORS:
            unreadable.append(name)
            return None
        if value is None:
            unreadable.append(name)
        return value

    main_volume_db = await probe(
        "main_volume_db", lambda: cam.get_volume_db(best_effort=True)
    )
    config_path = await probe(
        "graph.config_path", lambda: cam.get_config_file_path(best_effort=True)
    )

    async def read_fingerprint() -> Any:
        from .commissioning_admission import running_graph_fingerprint

        return running_graph_fingerprint(
            await cam.get_active_config_raw(best_effort=True)
        )

    fingerprint = await probe("graph.fingerprint", read_fingerprint)

    # NOT a probe: ``measurement_volume_db`` is ``None`` whenever no session
    # volume is open, which is an answer, not a failed read.
    session_volume_db: float | None = None
    if volume_plan is not None:
        try:
            session_volume_db = volume_plan.measurement_volume_db
        except _READ_ERRORS:
            unreadable.append("session_volume_db")

    # ``phase`` is the CAPTURE's phase, passed in, never ``program.phase``:
    # ``programs.program_for_phase`` answers several phases with one composed
    # object by identity, so the object's own phase names only one of them.
    program_id: str | None = None
    peak_dbfs: float | None = None
    try:
        program_id = str(program.program_id)
    except _READ_ERRORS:
        unreadable.append("stimulus.program_id")
    try:
        peak_dbfs = _stimulus_peak_dbfs(program)
    except _READ_ERRORS:
        unreadable.append("stimulus.peak_dbfs")

    # The published program WAV's own content hash. Absent on a caller with no
    # published artifact; that is not a failure worth naming.
    sha = getattr(artifact, "sha256", None)
    wav_sha256 = sha if isinstance(sha, str) and sha else None

    if unreadable:
        log_event(
            logger,
            "active_speaker.capture_provenance",
            level=logging.WARNING,
            result="incomplete",
            graph_kind=graph_kind,
            unreadable=",".join(unreadable),
        )
    observed = CaptureProvenance(
        graph_kind=graph_kind,
        main_volume_db=main_volume_db,
        session_volume_db=session_volume_db,
        graph_config_path=config_path,
        graph_fingerprint=fingerprint,
        stimulus_program_id=program_id,
        stimulus_phase=phase,
        stimulus_wav_sha256=wav_sha256,
        stimulus_peak_dbfs=peak_dbfs,
    )
    if not volume_fields_agree(observed):
        # The tripwire on the two fields that printed the 2026-08-24 defect from
        # its first capture (#2925 T1-2). TWO ways to reach it: the fader moved
        # between the play path's hold proving it and this read, or the hold
        # PROVED NOTHING because the plan owned no volume to hold (its companion
        # WARN is ``crossover_v2_capture_volume_unheld``). So name both values,
        # not only the delta.
        log_event(
            logger,
            "active_speaker.capture_provenance",
            level=logging.WARNING,
            result="volume_disagreement",
            graph_kind=graph_kind,
            phase=phase,
            # ``repr``, not a float format: this branch is reached BECAUSE a
            # value failed the numeric agreement test, so it may not be a number
            # at all -- formatting it as one would raise inside the observation,
            # where the blind belt would report a provenance failure instead of
            # the disagreement this line exists to name.
            main_volume_db=repr(main_volume_db),
            session_volume_db=repr(session_volume_db),
        )
    return observed
