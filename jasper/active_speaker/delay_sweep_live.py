# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The delay sweep's production seams: CamillaDSP, and the admitted producer.

:mod:`jasper.active_speaker.delay_sweep` decides and grades; it takes its four
side effects injected. This binds those four to the real box, and the division
of labour is the point:

* **This module owns the graph.** It reads the running graph, applies each
  coordinate's runtime-only patch under the DSP writer lock, and puts the live
  graph back. ``set_active_config_raw`` leaves CamillaDSP's persisted
  ``config_file_path`` alone (ADR-0193), so ``reload()`` is a complete restore
  and a crash or ``kill -9`` restores by doing nothing.
* **:class:`~jasper.active_speaker.commissioning_capture_producer.SummedCaptureProducer`
  owns everything audible.** Excitation admission, the SPL and dBFS caps, the
  protection evidence, playback, capture, and the analysis that produces the
  null depth. No private player exists here and none may be added: a sweep that
  played its own tone would bypass the admission every other summed capture in
  this repo goes through.

The producer already understood this walk before it had a runner — its
``evidence_kind`` vocabulary carries ``delay_null`` and its analysis passes
``expect_null=True`` for it. What was missing was the caller. The acoustic block
it publishes is read straight back out of the evidence store and handed to the
walk unchanged, because that block already IS the capture shape ``null_walk``
gates on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

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
)
from jasper.audio_measurement.evidence_identity import NormalizedActiveRawIdentity
from jasper.dsp_apply import dsp_writer_lock
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

WRITER_LOCK_SOURCE = "active_speaker.delay_sweep"

REFUSE_NO_LIVE_GRAPH = "delay_sweep_no_live_graph"
REFUSE_LOAD = "delay_sweep_load_refused"
REFUSE_ANALYSIS = "delay_sweep_analysis_missing"


def _uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


@dataclass
class LiveDelaySweepHost:
    """One sweep's binding to the running box, held for the walk's duration.

    ``capture_ordinal`` counts within a coordinate's required repeat set, which
    is what the producer checks each operation against; the walk asks for
    ``plan.repeats`` captures at each coordinate and each pose, so the ordinal
    is reset per coordinate rather than run across the whole sweep.
    """

    cam: Any
    producer: Any
    run_store: Any
    run_handle: Any
    target: Any
    plan: DelaySweepPlan
    placement_fingerprint: str
    # The two isolated per-driver target fingerprints the run already proved.
    # Supplied rather than derived: their owner is the completed fixed-axis
    # driver evidence (`CompleteIsolatedDriverEvidence`), and re-deriving them
    # here would be a second, weaker answer to a question that run settled.
    driver_target_fingerprints: tuple[str, str]
    config_dir: str | Path
    _entry_config_path: str | None = field(default=None, init=False)
    _ordinal: int = field(default=0, init=False)
    _coordinate_us: float = field(default=0.0, init=False)

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

    async def read_live_graph(self) -> Mapping[str, Any]:
        """The RUNNING graph, and the durable path this sweep must restore to.

        ``get_active_config_raw`` rather than the persisted file: a sweep that
        follows an audition would otherwise patch the durable anchor instead of
        what is actually playing.
        """

        raw = await self.cam.get_active_config_raw(best_effort=False)
        if not raw:
            raise DelaySweepRefused(
                REFUSE_NO_LIVE_GRAPH, "CamillaDSP is not running a graph to measure"
            )
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            raise DelaySweepRefused(REFUSE_GRAPH, "the running graph is not a mapping")
        # The restore anchor. The walk reads the live graph exactly once, before
        # it applies anything, so this records the path the sweep entered on.
        self._entry_config_path = str(
            await self.cam.get_config_file_path(best_effort=False) or ""
        )
        return parsed

    async def apply_graph(self, graph: Mapping[str, Any]) -> None:
        """Load one coordinate's graph, runtime-only, under the writer lock.

        The graph arrives already proven by ``reverse_null_graph`` — both lanes
        bound, and ``devices.volume_limit`` at or below the 0 dB ceiling. This
        serializes and loads it; it re-checks nothing, because a second, weaker
        copy of that proof here is how the two drift apart.
        """

        text = yaml.safe_dump(dict(graph), sort_keys=True)
        async with dsp_writer_lock(self.config_dir, source=WRITER_LOCK_SOURCE):
            if not await self.cam.set_active_config_raw(text, best_effort=False):
                raise DelaySweepRefused(
                    REFUSE_LOAD, "CamillaDSP refused the measurement graph"
                )

    async def restore_graph(self) -> dict[str, Any]:
        """Put the applied graph back by reloading the untouched persisted file.

        Every swap in the sweep was ``set_active_config_raw``, which never
        repoints ``config_file_path``, so the durable anchor is still the
        household's graph and ``reload()`` is the whole restore.
        """

        if self._entry_config_path is None:
            return {"restored": False, "reason": "nothing was ever applied"}
        async with dsp_writer_lock(self.config_dir, source=WRITER_LOCK_SOURCE):
            reloaded = await self.cam.reload(best_effort=False)
        log_event(
            logger,
            "active_speaker.delay_sweep",
            action="restore",
            result="reloaded" if reloaded else "refused",
            level=logging.INFO if reloaded else logging.CRITICAL,
            config_path=self._entry_config_path,
        )
        return {"restored": bool(reloaded), "config_path": self._entry_config_path}

    def begin_coordinate(self, relative_delay_us: float) -> None:
        """Start a coordinate's repeat set. Called by the walk before it applies."""

        self._coordinate_us = float(relative_delay_us)
        self._ordinal = 0

    async def measure(self, *, pose_deg: int | None, delay_us: float) -> Mapping[str, Any]:
        """One admitted capture through the producer, returned as its acoustic block.

        ``delay_us`` is the operation's non-negative DSP magnitude; the signed
        coordinate the operation is bound to is the walk's, carried on the host
        by :meth:`begin_coordinate`. The producer is handed the fresh read-back
        of the graph this host already applied — it never applies one itself.
        """

        del delay_us  # the signed coordinate is the operation's binding, not this
        target_fingerprint = delay_point_target_fingerprint(
            self.target, self.plan.spec, self._coordinate_us
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
            relative_delay_us=self._coordinate_us,
            null_walk_spec=self.plan.spec,
            issuance_id=_uuid_hex(),
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
            relative_delay_us=self._coordinate_us,
            pose_deg="" if pose_deg is None else pose_deg,
            null_depth_db=acoustic.get("null_depth_db"),
            verdict=acoustic.get("verdict"),
        )
        return acoustic

    async def _live_context(self) -> CommissioningLiveContext:
        """A fresh observation of the graph this host applied, for the producer."""

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
        return CommissioningFreshReadback(
            graph=NormalizedActiveRawIdentity(parsed),
            active_raw=raw,
            config_path=self._entry_config_path or "",
            listening_volume_db=float(await self.cam.get_volume_db()),
            # The sweep binds its delay in the graph it applied, not through the
            # producer's candidate-confirmation door.
            delay_confirmation=None,
        )


__all__ = [
    "REFUSE_ANALYSIS",
    "REFUSE_LOAD",
    "REFUSE_NO_LIVE_GRAPH",
    "LiveDelaySweepHost",
    "WRITER_LOCK_SOURCE",
]
