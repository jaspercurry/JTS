# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The acoustic context one measurement capture was actually taken under.

**Why this exists — the config label is not the graph.** A CHECK/MEASURE
program is played through a transient per-driver ROUTING graph
(:func:`jasper.active_speaker.camilla_yaml.emit_active_speaker_program_config`
— each program channel straight to its driver's physical output, crossover /
delay / linearization deliberately OMITTED), loaded INLINE with
``SetConfig`` and unloaded again the moment the sweep ends. That loader
"deliberately leaves the persisted ``config_file_path`` unchanged" — the
words are :meth:`jasper.camilla.CamillaController.get_active_config_raw`'s
own — so a capture taken through the routing graph and a capture taken
through the standing applied graph report the **same** ``config_path`` while
their transfer functions differ by +7…+15 dB per branch. On 2026-08-19 a
forensic session on jts3 compared levels across those two graphs for hours
with nothing on disk able to tell them apart, and reconstructed the night's
−27.5 dB fader out of the journal because no capture recorded it either.

So a retained capture records, read from the ONE live owner of each fact at
the instant the stimulus is emitted:

=========================  ====================================================
field                      live owner
=========================  ====================================================
``main_volume_db``         :meth:`jasper.camilla.CamillaController.get_volume_db`
``session_volume_db``      :attr:`~jasper.active_speaker.session_volume_plan.SessionVolumePlan.measurement_volume_db`
``graph.kind``             the playback branch that performed (or skipped) the swap
``graph.config_path``      :meth:`~jasper.camilla.CamillaController.get_config_file_path`
``graph.fingerprint``      :func:`~jasper.active_speaker.commissioning_admission.running_graph_fingerprint` over :meth:`~jasper.camilla.CamillaController.get_active_config_raw`
``stimulus.*``             the :class:`~jasper.audio_measurement.program.ExcitationProgram` and its published WAV artifact
=========================  ====================================================

``graph.config_path`` is recorded precisely BECAUSE it is the misleading
label: side by side with ``kind`` and ``fingerprint`` it shows what the
statefile claimed versus what was running. ``kind`` is the only field that
can never read ``null`` — it is structural knowledge held by the branch that
loaded (or did not load) the graph, not a probe that can fail.

``graph.fingerprint`` compares to OTHER capture provenance and to nothing
else. It is not the same quantity as a round receipt's
``entry_graph_fingerprint``, which is the applied profile record's
``candidate_fingerprint`` (``correction_crossover_v2._active_graph_fingerprint``)
— a different namespace answering a different question.

Nothing here may ever break a capture. Every read is individually guarded, and
:func:`record_capture_provenance` wraps the lot in a blind belt; an unreadable
surface leaves its field ``None`` and contributes its name to one WARN
``event=active_speaker.capture_provenance`` line.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from jasper.log_event import log_event

logger = logging.getLogger(__name__)

# The two values ``CaptureProvenance.graph_kind`` takes. Unrelated to
# ``commissioning_host.CommissioningGraphKind`` ("normal"/"reverse"/"delay"),
# which names a commissioning attempt's polarity, not a capture's topology.

#: The transient per-driver routing graph a CHECK/MEASURE program is played
#: through — one program channel to one driver's physical output, with the
#: crossover, delays and linearization left OUT. Loaded under the DSP writer
#: lock for the length of one sweep and restored after.
GRAPH_KIND_PROGRAM_ROUTING = "program_routing"

#: No graph was loaded: the standing graph on the speaker IS the system under
#: test. Every summed-sweep phase (entry baseline, verify, and both cloud
#: position groups) plays this way.
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
        """The sidecar block, written under one ``provenance`` key so that
        nothing the sidecar already carries moves or changes."""
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

    The two seams are different closures on different call stacks — the host
    emits the stimulus, the phone uploads its recording some seconds later and
    the analyze seam retains it — and the flow's ``play`` seam returns ``None``,
    so the fact cannot be handed back through it. One recorder per measurement
    session bridges the gap, exactly as ``PlaybackStartSignal`` already bridges
    "audio starts now" from the same play seam to the relay runner.

    :meth:`take` CONSUMES. The plan runner arms and plays each capture before
    consuming it (``on_armed`` → the play seam, then ``consume_capture`` → the
    analyze seam), so a second read with no play in between is not a second
    capture of the same stimulus — it is a capture this recorder cannot speak
    for, and it gets ``None`` rather than the previous capture's context. Stale
    provenance on a forensic clip is worse than absent: absent is visibly absent.
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


#: What a guarded provenance read is allowed to swallow. Deliberately a named
#: tuple of concrete types rather than a blind ``except Exception``: the
#: CamillaDSP surface is already closed (``CamillaController._call`` converts
#: everything it can hit into ``CamillaUnavailable``, and ``best_effort=True``
#: turns that into ``None``), so what remains is a fingerprint helper's
#: ``RuntimeError`` and the shape errors a test double or a future caller can
#: produce. A genuinely unexpected exception type SHOULD fall through to
#: ``record_capture_provenance``'s blind belt rather than be absorbed here,
#: where absorbing it would silently null one field and hide the reason.
_READ_ERRORS = (OSError, RuntimeError, TypeError, ValueError, AttributeError)


def _stimulus_peak_dbfs(program: Any) -> float | None:
    """The loudest stimulus segment's declared digital peak, in dBFS.

    ``ProgramSegment.gain_db`` is "the digital gain applied to the unit-peak
    stimulus" (the composer's own words), so for a unit-peak stimulus it IS
    that segment's peak dBFS in the rendered WAV — the same quantity
    ``program_admission._channel_declared_peak_dbfs`` reads for its
    per-channel manifest check. Scoped to ``stimulus_segments()`` for that
    function's stated reason: the courtesy prelude is derived never to exceed
    its channel's loudest stimulus, so including it could only mask a
    render bug.

    Session volume is NOT folded in — it is its own field, from its own owner.
    """
    segments = program.stimulus_segments()
    peaks = [float(segment.gain_db) for segment in segments]
    return max(peaks) if peaks else None


async def record_capture_provenance(
    recorder: CaptureProvenanceRecorder,
    *,
    cam: Any,
    graph_kind: str,
    program: Any,
    artifact: Any = None,
    volume_plan: Any = None,
) -> None:
    """Observe, and hand the result to ``recorder``. The never-break-a-capture belt.

    The entry point a playback path should call. :func:`observe_capture_provenance`
    guards each read against the types it can actually meet; THIS is the outer
    belt, and it is deliberately blind, because the contract it enforces is not
    "handle the known failures" but "a forensic sidecar for an
    operator-enabled ring may never cost a household its measurement" — and an
    exception type nobody predicted is exactly the case that promise is for.
    ``BaseException`` still passes: a cancelled measurement must stay cancelled.

    Living here rather than at the call site keeps that one promise in one
    place, however many playback branches come to record through it.
    """
    try:
        recorder.record(
            await observe_capture_provenance(
                cam=cam,
                graph_kind=graph_kind,
                program=program,
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
    artifact: Any = None,
    volume_plan: Any = None,
) -> CaptureProvenance:
    """Read the live context for the stimulus that is about to play.

    Call this as late as possible — for the program branch that means AFTER
    the routing graph is loaded and inside the same writer lock, because
    ``get_active_config_raw`` is what distinguishes the two graphs and it
    answers the applied graph right up until the load lands.

    Each read is guarded on its own, so one dead surface cannot blank the rest,
    and the names of whatever could not be read are collected into a single
    WARN rather than one line per field. Those guards name the types a read can
    actually meet; :func:`record_capture_provenance` is the blind outer belt,
    and is what a playback path should call.
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

    program_id: str | None = None
    phase: str | None = None
    peak_dbfs: float | None = None
    try:
        program_id = str(program.program_id)
        phase = str(program.phase)
    except _READ_ERRORS:
        unreadable.append("stimulus.program_id")
    try:
        peak_dbfs = _stimulus_peak_dbfs(program)
    except _READ_ERRORS:
        unreadable.append("stimulus.peak_dbfs")

    # The published program WAV's own content hash — the identity the evidence
    # bundle already minted for these exact bytes. Absent on a caller that has
    # no published artifact; that is not a failure worth naming.
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
    return CaptureProvenance(
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
