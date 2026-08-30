# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The delay sweep's production seams: CamillaDSP, and the admitted producer.

:mod:`jasper.active_speaker.delay_sweep` decides and grades; it takes its four
side effects injected. This binds those four to the real box, and the division
of labour is the point:

* **This module owns the graph and the hold.** It takes the measurement hold for
  the sweep's duration, reads the running graph, stages the zero-relative
  predecessor, applies each coordinate's runtime-only patch under the DSP writer
  lock, and puts back the exact graph it displaced.
* **:class:`~jasper.active_speaker.commissioning_capture_producer.SummedCaptureProducer`
  owns everything audible.** Excitation admission, the SPL and dBFS caps, the
  protection evidence, playback, capture, and the analysis that produces the
  null depth. No private player exists here and none may be added: a sweep that
  played its own tone would bypass the admission every other summed capture in
  this repo goes through.

Two things the producer REQUIRES of a ``delay_null`` caller, both learned the
hard way — it refuses every capture without them:

* a real :class:`~jasper.audio_measurement.delay_graph.DelayCandidateConfirmation`
  on the fresh read-back. ``_capture`` computes ``delay_current =
  readback.delay_confirmation is not None`` for this evidence kind, so ``None``
  fails the protection-evidence currency check and admission refuses with
  ``PROTECTION_EVIDENCE_STALE``. The confirmation is produced by proving each
  applied graph against the zero-relative snapshot staged at sweep start.
* a host-proved ``bass_profile_summary``. ``_protection_evidence`` refuses with
  ``graph_authority_unproven`` when it is absent. It is supplied by the caller
  rather than derived here: it is the graph authority's own evidence, and a
  second, weaker derivation is how two answers to one question start to drift.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from jasper.active_speaker.alignment_walk import DRIVER_DELAY_WALK_SCOPE
from jasper.active_speaker.commissioning_evidence import (
    delay_point_target_fingerprint,
    evidence_attempt_target_id,
)
from jasper.active_speaker.commissioning_host import RegionCaptureOperation
from jasper.active_speaker.commissioning_runtime import (
    CommissioningFreshReadback,
    CommissioningLiveContext,
)
from jasper.active_speaker.delay_sweep import (
    REFUSE_GRAPH,
    DelaySweepPlan,
    DelaySweepRefused,
    DelaySweepSeams,
    reverse_null_graph,
)
from jasper.audio_measurement.delay_graph import (
    DelayGraphSnapshot,
    DelayLaneBinding,
    confirm_delay_candidate,
)
from jasper.audio_measurement.evidence_identity import NormalizedActiveRawIdentity
from jasper.audio_measurement.null_walk import DspPredecessor
from jasper.dsp_apply import dsp_writer_lock
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

WRITER_LOCK_SOURCE = "active_speaker.delay_sweep"
HOLD_OWNER = "active-speaker-delay-sweep"

REFUSE_NO_LIVE_GRAPH = "delay_sweep_no_live_graph"
REFUSE_LOAD = "delay_sweep_load_refused"
REFUSE_ANALYSIS = "delay_sweep_analysis_missing"
REFUSE_RESTORE = "delay_sweep_restore_failed"
REFUSE_POSES = "delay_sweep_poses_unsupported"
REFUSE_HOLD = "delay_sweep_hold_unavailable"
REFUSE_VOLUME = "delay_sweep_volume_unreadable"


@dataclass
class LiveDelaySweepHost:
    """One sweep's binding to the running box, held for the walk's duration."""

    cam: Any
    producer: Any
    run_store: Any
    run_handle: Any
    target: Any
    plan: DelaySweepPlan
    placement_fingerprint: str
    # The two isolated per-driver target fingerprints the run already proved.
    # Supplied rather than derived: their owner is the completed fixed-axis
    # driver evidence, and re-deriving them here would be a second, weaker
    # answer to a question that run settled.
    driver_target_fingerprints: tuple[str, str]
    # Host-proved bass-extension graph authority. The producer refuses without
    # it; see the module docstring for why it is not derived here.
    bass_profile_summary: Mapping[str, Any]
    topology_id: str
    config_dir: str | Path
    _entry_raw: str | None = field(default=None, init=False)
    _snapshot: DelayGraphSnapshot | None = field(default=None, init=False)
    _candidate: Any = field(default=None, init=False)
    _ordinal: int = field(default=0, init=False)
    _held: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if len(self.plan.poses_deg) != 1:
            # Nothing in this host moves the microphone, and the producer stamps
            # every capture with one reference-axis geometry and this host's
            # single placement. A multi-pose plan would therefore record N
            # identical on-axis sets under N pose labels and let the verdict
            # report angle robustness it never measured. A host that CAN move
            # the mic (the lab arm) is what unlocks poses.
            raise DelaySweepRefused(
                REFUSE_POSES,
                "this host measures one pose; it cannot move the microphone, so "
                "a multi-pose plan would fabricate the angle-robustness check",
            )

    def seams(self) -> DelaySweepSeams:
        return DelaySweepSeams(
            read_live_graph=self.read_live_graph,
            apply_graph=self.apply_graph,
            restore_graph=self.restore_graph,
            measure=self.measure,
            session_claim=self.session_claim,
            begin_coordinate=self.begin_coordinate,
        )

    def session_claim(self) -> str | None:
        """The repo's one answer to "may an operator door act right now"."""

        from jasper.active_speaker.session_volume_plan import live_measurement_session

        return live_measurement_session(action="sweeping inter-driver delay")

    # ---------------------------------------------------------------- graph --

    async def read_live_graph(self) -> Mapping[str, Any]:
        """Take the hold, read what is running, and stage the F1 predecessor.

        The walk calls this exactly once, before it applies anything, which is
        what makes it the right place for both of the sweep's entry obligations.

        The hold is TAKEN, not merely checked: ``session_claim`` above asks
        whether somebody else holds the box, but without taking it a seat-level
        or angle-capture door could start mid-sweep, move the fader, and
        invalidate every coordinate already banked.

        ``get_active_config_raw`` rather than the persisted file: a sweep that
        follows an audition must measure — and later restore — what is actually
        playing, not the durable anchor underneath it.
        """

        from jasper.control.measurement_hold import (
            MeasurementHoldConflict,
            MeasurementHoldRequestError,
            acquire,
        )

        try:
            acquire(HOLD_OWNER)
        except (MeasurementHoldConflict, MeasurementHoldRequestError) as exc:
            raise DelaySweepRefused(REFUSE_HOLD, str(exc)) from exc
        self._held = True

        raw = await self.cam.get_active_config_raw(best_effort=False)
        if not raw:
            raise DelaySweepRefused(
                REFUSE_NO_LIVE_GRAPH, "CamillaDSP is not running a graph to measure"
            )
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            raise DelaySweepRefused(REFUSE_GRAPH, "the running graph is not a mapping")
        self._entry_raw = raw
        await self._stage_zero_relative(parsed)
        return parsed

    async def _stage_zero_relative(self, live_graph: Mapping[str, Any]) -> None:
        """Apply the both-lanes-zero graph and freeze it as the walk's F1 anchor.

        :class:`DelayGraphSnapshot` is defined against a zero-relative
        predecessor, so the snapshot has to be taken from a graph that is
        actually running with both bound delay slots at numeric zero — which is
        exactly the sweep's own zero coordinate.
        """

        zero = reverse_null_graph(
            live_graph,
            inverted_role=self.plan.inverted_role,
            branch_roles=self.plan.branch_roles,
            delay_role=None,
            delay_us=0.0,
            role_channels=self.plan.role_channels,
        )
        await self.apply_graph(zero)
        readback = await self._read_running_graph()
        spec = self.plan.spec
        self._snapshot = DelayGraphSnapshot(
            spec,
            scope=DRIVER_DELAY_WALK_SCOPE,
            topology_id=self.topology_id,
            positive_lane=self._lane(spec.positive_delay_target),
            negative_lane=self._lane(spec.negative_delay_target),
            # The snapshot reads the graph out of the payload's `active_raw`
            # slot; the rest of the payload is the host's own rollback identity.
            predecessor=DspPredecessor({"active_raw": readback}),
        )

    def _lane(self, role: str) -> DelayLaneBinding:
        from jasper.active_speaker.camilla_yaml import (
            driver_baseline_gain_name,
            driver_delay_name,
        )

        return DelayLaneBinding(
            target=role,
            filter_name=driver_delay_name(role),
            identity_filter_name=driver_baseline_gain_name(role),
            channels=tuple(self.plan.role_channels[role]),
        )

    async def apply_graph(self, graph: Mapping[str, Any]) -> None:
        """Load one graph, runtime-only, under the writer lock.

        The graph arrives already proven by ``reverse_null_graph`` — both lanes
        bound, and ``devices.volume_limit`` at or below the 0 dB ceiling. This
        re-checks nothing, because a second, weaker copy of that proof here is
        how the two drift apart.

        ``duck=False``: the fader bracket exists to keep a graph swap from being
        loud against a household programme, and this sweep holds the measurement
        hold, so there is none. Taking the default would also pay ~0.94 s on
        every one of up to 27 coordinates and — because the duck release is
        best-effort — could strand the box a fixed step down for the rest of the
        sweep, which the producer's within-capture volume check cannot see.
        """

        text = yaml.safe_dump(dict(graph), sort_keys=True)
        async with dsp_writer_lock(self.config_dir, source=WRITER_LOCK_SOURCE):
            if not await self.cam.set_active_config_raw(
                text, best_effort=False, duck=False
            ):
                raise DelaySweepRefused(
                    REFUSE_LOAD, "CamillaDSP refused the measurement graph"
                )

    async def restore_graph(self) -> dict[str, Any]:
        """Put back the exact graph this sweep displaced, then drop the hold.

        NOT ``reload()``: that loads the persisted config file, which is the
        household anchor rather than what was running. The read path exists
        precisely because an audition may be live, and ending a sweep by
        silently replacing an operator's audition with the durable anchor would
        be a different graph than the one we took away.
        """

        restored = False
        detail = "nothing was ever applied"
        try:
            if self._entry_raw is not None:
                async with dsp_writer_lock(self.config_dir, source=WRITER_LOCK_SOURCE):
                    restored = bool(
                        await self.cam.set_active_config_raw(
                            self._entry_raw, best_effort=False, duck=False
                        )
                    )
                detail = "entry graph reloaded" if restored else "restore refused"
                log_event(
                    logger,
                    "active_speaker.delay_sweep",
                    action="restore",
                    result="restored" if restored else "refused",
                    level=logging.INFO if restored else logging.CRITICAL,
                )
        finally:
            self._release_hold()
        return {"restored": restored, "detail": detail}

    def _release_hold(self) -> None:
        if not self._held:
            return
        from jasper.control.measurement_hold import (
            MeasurementHoldConflict,
            MeasurementHoldRequestError,
            release,
        )

        self._held = False
        try:
            release(HOLD_OWNER)
        except (MeasurementHoldConflict, MeasurementHoldRequestError, OSError):
            # A hold that cannot be released lapses on its own TTL; losing the
            # graph restore over it would be the worse trade.
            logger.warning("event=active_speaker.delay_sweep hold_release_failed")

    async def _read_running_graph(self) -> dict[str, Any]:
        raw = await self.cam.get_active_config_raw(best_effort=False)
        if not raw:
            raise DelaySweepRefused(
                REFUSE_NO_LIVE_GRAPH, "CamillaDSP stopped running a graph mid-sweep"
            )
        normalized = await self.cam.normalize_config_raw(raw, best_effort=False)
        parsed = yaml.safe_load(normalized or raw)
        if not isinstance(parsed, dict):
            raise DelaySweepRefused(
                REFUSE_GRAPH, "the running graph did not normalize to a mapping"
            )
        return parsed

    # -------------------------------------------------------------- capture --

    def begin_coordinate(self, relative_delay_us: float) -> None:
        """Start a coordinate's repeat set, before its graph is applied."""

        self._candidate = self.plan.spec.dsp_candidate(relative_delay_us)
        self._ordinal = 0

    async def measure(self, *, pose_deg: int | None, delay_us: float) -> Mapping[str, Any]:
        """One admitted capture through the producer, as its acoustic block."""

        del delay_us  # the signed coordinate is the operation's binding
        if self._candidate is None or self._snapshot is None:
            raise DelaySweepRefused(
                REFUSE_GRAPH, "no coordinate is staged for this capture"
            )
        coordinate = self._candidate.relative_delay_us
        target_fingerprint = delay_point_target_fingerprint(
            self.target, self.plan.spec, coordinate
        )
        attempt = self.run_store.reserve_attempt(
            self.run_handle,
            target_id=evidence_attempt_target_id("delay_null", target_fingerprint),
            target_fingerprint=target_fingerprint,
            # One coordinate keeps one durable attempt across its repeat set.
            reuse_existing=True,
        )
        operation = RegionCaptureOperation(
            plan_fingerprint=self.producer.plan_fingerprint,
            target=self.target,
            attempt=attempt,
            evidence_kind="delay_null",
            placement_fingerprint=self.placement_fingerprint,
            driver_target_fingerprints=self.driver_target_fingerprints,
            lower_channels=tuple(
                self.plan.role_channels[self.plan.spec.negative_delay_target]
            ),
            upper_channels=tuple(
                self.plan.role_channels[self.plan.spec.positive_delay_target]
            ),
            capture_ordinal=self._ordinal,
            required_capture_count=self.plan.repeats * len(self.plan.poses_deg),
            relative_delay_us=coordinate,
            null_walk_spec=self.plan.spec,
            issuance_id=uuid.uuid4().hex,
        )
        self._ordinal += 1

        context = await self._live_context()
        result = await self.producer.capture(operation, context)
        analysis = self.producer.evidence_store.reopen_json_artifact(
            result.payload.capture.analysis_input_artifact
        )
        acoustic = analysis.get("acoustic") if isinstance(analysis, Mapping) else None
        if not isinstance(acoustic, Mapping):
            raise DelaySweepRefused(
                REFUSE_ANALYSIS, "the admitted capture published no acoustic block"
            )
        log_event(
            logger,
            "active_speaker.delay_sweep",
            action="captured",
            relative_delay_us=coordinate,
            pose_deg="" if pose_deg is None else pose_deg,
            null_depth_db=acoustic.get("null_depth_db"),
            verdict=acoustic.get("verdict"),
        )
        return acoustic

    async def _live_context(self) -> CommissioningLiveContext:
        readback = await self._fresh_readback()
        return CommissioningLiveContext(
            graph=readback.graph,
            active_raw=readback.active_raw,
            config_path=readback.config_path,
            listening_volume_db=readback.listening_volume_db,
            delay_confirmation=readback.delay_confirmation,
            fresh_readback=self._fresh_readback,
            bass_profile_summary=readback.bass_profile_summary,
        )

    async def _fresh_readback(self) -> CommissioningFreshReadback:
        """A fresh observation of the graph this host applied, proven.

        ``normalize_config_raw`` is deliberately called OUTSIDE the writer lock:
        the live-graph boundary reaches it from inside that lock on other paths,
        and taking it here would contend on the underlying flock.
        """

        raw = await self.cam.get_active_config_raw(best_effort=False)
        if not raw:
            raise DelaySweepRefused(
                REFUSE_NO_LIVE_GRAPH, "CamillaDSP stopped running a graph mid-sweep"
            )
        parsed = await self._read_running_graph()
        volume = await self.cam.get_volume_db()
        if volume is None:
            raise DelaySweepRefused(
                REFUSE_VOLUME, "CamillaDSP would not report the listening volume"
            )
        assert self._snapshot is not None and self._candidate is not None
        confirmation = confirm_delay_candidate(
            self._snapshot,
            self._candidate,
            parsed,
            expected_snapshot_fingerprint=self._snapshot.fingerprint,
            expected_scope=DRIVER_DELAY_WALK_SCOPE,
            expected_topology_id=self.topology_id,
            expected_crossover_fc_hz=self.plan.spec.crossover_fc_hz,
        )
        return CommissioningFreshReadback(
            graph=NormalizedActiveRawIdentity(parsed),
            active_raw=raw,
            config_path="",
            listening_volume_db=float(volume),
            delay_confirmation=confirmation,
            bass_profile_summary=self.bass_profile_summary,
        )


__all__ = [
    "HOLD_OWNER",
    "REFUSE_ANALYSIS",
    "REFUSE_HOLD",
    "REFUSE_LOAD",
    "REFUSE_NO_LIVE_GRAPH",
    "REFUSE_POSES",
    "REFUSE_RESTORE",
    "REFUSE_VOLUME",
    "LiveDelaySweepHost",
    "WRITER_LOCK_SOURCE",
]
