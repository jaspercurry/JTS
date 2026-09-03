# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Acoustic context one capture's stimulus actually played under.

A CHECK/MEASURE program's ROUTING graph loads INLINE via ``SetConfig``, leaving ``config_file_path`` unchanged -- ``graph.config_path`` can mislabel the graph actually running; ``graph.kind`` can never be ``null``.

Observed only under capture retention, not every capture. :func:`volume_fields_agree` compares ``main_volume_db``/``session_volume_db``; #2925 T1-2: two reads two lines apart once disagreed 8.712 dB, unnoticed five days. A failed guarded read nulls its field and joins one WARN ``event=active_speaker.capture_provenance``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from jasper.log_event import log_event

from .volume_latch import fader_matches

logger = logging.getLogger(__name__)

#: Transient per-driver routing graph, loaded under the DSP writer lock for
#: one sweep and restored after.
GRAPH_KIND_PROGRAM_ROUTING = "program_routing"

#: No graph loaded: the standing graph is the system under test.
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
    """One-capture handoff, play seam to retention seam. :meth:`take` CONSUMES: a second read with no play between gets ``None``, never stale provenance."""

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
    """Do this record's two volume fields tell the same story? (#2925 T1-2) ``True`` within ``SessionVolumePlan.open``'s tolerance, or when ``session_volume_db`` is ``None``; ``None`` ``main_volume_db`` against a declared volume IS a disagreement."""
    if provenance.session_volume_db is None:
        return True
    return fader_matches(provenance.main_volume_db, provenance.session_volume_db)


#: Concrete types a guarded read may swallow; an unlisted type falls through to ``record_capture_provenance``'s blind belt.
_READ_ERRORS = (OSError, RuntimeError, TypeError, ValueError, AttributeError)


def _stimulus_peak_dbfs(program: Any) -> float | None:
    """Loudest stimulus segment's declared digital peak, dBFS. Scoped to ``stimulus_segments()`` (excludes the courtesy prelude); session volume is NOT folded in."""
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
    """Observe, and hand the result to ``recorder``. Never-break-a-capture belt: deliberately BLIND to ``Exception``; ``BaseException`` still propagates (a cancelled measurement stays cancelled). ``None`` recorder is a no-op. ``open_cam``/``read_volume_plan`` are ZERO-ARGUMENT RESOLVERS so obtaining them also happens inside the try."""
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
    """Read the live context for the stimulus about to play. Call as late as possible -- for the program branch, AFTER the routing graph loads, inside the same writer lock: ``get_active_config_raw`` still answers the applied graph until the load lands."""
    unreadable: list[str] = []

    async def probe(name: str, read: Any) -> Any:
        """One live read; a raise or a ``None`` both mean "could not read"."""
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

    # NOT a probe: None here is an answer (no session volume open).
    session_volume_db: float | None = None
    if volume_plan is not None:
        try:
            session_volume_db = volume_plan.measurement_volume_db
        except _READ_ERRORS:
            unreadable.append("session_volume_db")

    # ``phase`` is the CAPTURE's phase, passed in, never ``program.phase`` (one composed object can answer for several phases).
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

    # Published program WAV's content hash; absent with no published artifact.
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
        # #2925 T1-2 tripwire. Companion WARN when the hold owned nothing to hold: ``crossover_v2_capture_volume_unheld``.
        log_event(
            logger,
            "active_speaker.capture_provenance",
            level=logging.WARNING,
            result="volume_disagreement",
            graph_kind=graph_kind,
            phase=phase,
            # repr, not a float format: a value here may not be numeric.
            main_volume_db=repr(main_volume_db),
            session_volume_db=repr(session_volume_db),
        )
    return observed
