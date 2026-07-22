# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The bench campaign orchestrator.

Composes the manifest, the fail-closed activation seam, the injected
measurement collaborators, and the pure bundle emitter to run the frozen
limiter-evidence campaign end to end and write the replayable bundle. It owns
**only** campaign sequencing and the temporary-graph-activation lifecycle; it
reimplements no measurement gating, admission, graph proof, or evidence
identity — each stays owned where it already lives.

Boundaries:

* It never calls the pure evidence producer, ``apply_bass_extension``,
  ``bypass_bass_extension``, ``recover_pending_bass_extension_apply``, or any
  profile writer. It writes one on-disk bundle and nothing else.
* The measurement collaborators (``measurement_window``, the CamillaDSP
  controller, the floor control, and the per-role play/capture/analyze
  executor) are injected via :class:`BenchDeps`; the hardware-free tests supply
  mocks. Their real bindings are assembled by the operator CLI.

Per target: run the discovery pass (activate the proposed natural graph with the
baseline limiter, prove the read-back, run the admitted sweep + sustain, collect
the pre-limiter peaks as the candidate inventory, restore); then the candidate
pass. Each candidate runs two phases in one window: activate the natural graph at
the **baseline** limiter (the paired reference — a live-graph mutation the runner
owns), capture the reference sweep, restore; then activate the candidate
``clip_limit``, run the transfer + candidate sweep + sustain, restore. Stop at the
first accepted candidate. Any operator Stop or protocol abort ends the target
through the ``aborted``/``refused`` arm with its partial artifacts preserved.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from jasper.audio_measurement.evidence_identity import ArtifactIdentity

from . import bundle
from .activation import (
    ActivationProof,
    snapshot_predecessor,
    temporary_bass_activation,
)
from .manifest import CampaignManifest
from .sink import BundleSink


class BenchAborted(Exception):
    """The operator pressed Stop or a protocol abort condition fired."""


class Stop:
    """Cooperative Stop control (mirrors the bench-experiment stop flag)."""

    def __init__(self) -> None:
        self._stopped = False

    @property
    def stopped(self) -> bool:
        return self._stopped

    def stop(self) -> None:
        self._stopped = True

    def check(self) -> None:
        if self._stopped:
            raise BenchAborted("operator Stop requested")


class FloorControl(Protocol):
    """Fade the speaker to the safe floor and confirm it is there."""

    async def to_floor(self) -> None: ...

    async def assert_at_floor(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TargetPlan:
    """One sealed-family target the runner activates and measures."""

    target_id: str
    target_fingerprint: str
    graph_raw_text: str
    limiter_name: str
    owner_channels: tuple[int, ...]
    profile_summary: Mapping[str, Any]
    baseline_clip_limit_dbfs: float


@dataclass(frozen=True, slots=True)
class DiscoveryProbe:
    """One discovery source observation's measured pieces."""

    stimulus: ArtifactIdentity
    admission: ArtifactIdentity
    active_graph_readback: ArtifactIdentity
    pre_limiter_pcm: ArtifactIdentity
    peak_analysis: ArtifactIdentity
    pre_limiter_peak_dbfs: float


@dataclass(frozen=True, slots=True)
class ReferenceSweepCapture:
    """The paired reference sweep captured at the BASELINE limiter (phase 1).

    The reference *activation* and its receipt are the runner's (a live-graph
    mutation it owns); the executor only captures the acoustic reference. The
    reference stimulus/admission must match the candidate sweep's by SHA — the
    executor plays the same admitted request in both phases.
    """

    reference_stimulus: ArtifactIdentity
    reference_admission: ArtifactIdentity
    reference_acoustic_capture: ArtifactIdentity


@dataclass(frozen=True, slots=True)
class CandidateMeasurements:
    """The candidate-side records measured at one candidate setting (phase 2).

    ``sweep_core`` is the candidate sweep's measurement-core kwargs; the runner
    combines it with the runner-owned reference activation/receipt and the
    phase-1 :class:`ReferenceSweepCapture` into the full ``sweep_transparency``
    record. The runner classifies the disposition from the verdicts these
    records carry — the executor never decides accept / advance / stop.
    """

    digital_transfer_probe: Mapping[str, object]
    sweep_core: Mapping[str, object]
    sustain_stress: Mapping[str, object]
    transparency_analysis: ArtifactIdentity
    transparency_verdict: str
    active_graph_fingerprint: str
    ordered_owner_chain: Sequence[str]
    configured_clip_limit_dbfs: float


class RoleExecutor(Protocol):
    """Plays admitted stimuli, captures near-field, and analyzes to records.

    The real binding rides the existing admission chain, ramp/safe_playback,
    located playback, capture relay, and the analysis kernels; the hardware-free
    tests mock it. It records its artifacts through ``sink``. It owns the
    acoustic capture + analysis; the runner owns every live-graph activation
    (including the paired reference activation) and the disposition decision.
    """

    async def run_discovery(
        self,
        *,
        target: TargetPlan,
        active_graph_readback: ArtifactIdentity,
        sink: BundleSink,
        stop: Stop,
    ) -> Sequence[DiscoveryProbe]: ...

    async def run_reference_sweep(
        self,
        *,
        target: TargetPlan,
        reference_readback: ArtifactIdentity,
        sink: BundleSink,
        stop: Stop,
    ) -> ReferenceSweepCapture: ...

    async def run_candidate(
        self,
        *,
        target: TargetPlan,
        candidate_setting_dbfs: float,
        candidate_readback: ArtifactIdentity,
        reference: ReferenceSweepCapture,
        sink: BundleSink,
        stop: Stop,
    ) -> CandidateMeasurements: ...


@dataclass(slots=True)
class BenchDeps:
    """The injected collaborators the campaign composes."""

    open_window: Callable[[], AbstractAsyncContextManager[Any]]
    controller: Any  # CamillaController-shaped (used by the activation seam)
    floor: FloorControl
    executor: RoleExecutor
    stop: Stop = field(default_factory=Stop)


async def _readback_receipt(
    sink: BundleSink,
    *,
    role: str,
    target_id: str,
    active_config_raw: str,
    graph_fingerprint: str,
    configured_clip_limit_dbfs: float,
) -> ArtifactIdentity:
    """Record one activation/restoration read-back receipt artifact."""

    return sink.write_json(
        f"{target_id}/{role}.json",
        {
            "role": role,
            "target_id": target_id,
            "active_graph_fingerprint": graph_fingerprint,
            "configured_clip_limit_dbfs": configured_clip_limit_dbfs,
            "active_config_raw": active_config_raw,
        },
        kind="jts_bass_extension_bench_receipt",
    )


def _classify_candidate(
    measured: CandidateMeasurements, sweep_record: Mapping[str, object]
) -> str:
    """Return the frozen 2026-07-19b disposition for one measured candidate.

    An honest transfer / quality / protection / clamp failure stops the target
    (``refused`` — no evaluated candidate is emitted for a failed pass); a
    transparency-only failure advances (``limiter_transparency_failed``); an
    all-pass candidate is ``accepted``.
    """

    base_pass = (
        measured.digital_transfer_probe.get("verdict") == "pass"
        and sweep_record.get("quality_verdict") == "pass"
        and sweep_record.get("protection_verdict") == "pass"
        and sweep_record.get("digital_clamp_passed") is True
        and measured.sustain_stress.get("quality_verdict") == "pass"
        and measured.sustain_stress.get("protection_verdict") == "pass"
        and measured.sustain_stress.get("digital_clamp_passed") is True
    )
    if base_pass and measured.transparency_verdict == "pass":
        return "accepted"
    if base_pass and measured.transparency_verdict == "fail":
        return "limiter_transparency_failed"
    return "refused"


async def _run_target(
    deps: BenchDeps,
    target: TargetPlan,
    *,
    natural_graph_fingerprint: str,
    sink: BundleSink,
) -> dict[str, object]:
    """Run discovery + candidate passes for one target; return its result dict.

    On Stop or any activation/measurement failure, returns the ``aborted`` /
    ``refused`` arm with a stop receipt and whatever partial artifacts were
    recorded.
    """

    partials: list[ArtifactIdentity] = []

    def _stop_result(reason: str, disposition: str = "aborted") -> dict[str, object]:
        receipt = sink.write_json(
            f"{target.target_id}/stop.json",
            {"target_id": target.target_id, "reason": reason},
            kind="jts_bass_extension_bench_stop",
        )
        return bundle.build_stopped_result(
            disposition=disposition, stop_receipt=receipt, partial_artifacts=partials
        )

    baseline_proof = ActivationProof(
        limiter_name=target.limiter_name,
        owner_channels=target.owner_channels,
        profile_summary=target.profile_summary,
        expected_clip_limit_dbfs=target.baseline_clip_limit_dbfs,
    )

    try:
        deps.stop.check()
        # --- Discovery pass -------------------------------------------------
        async with deps.open_window():
            predecessor = await snapshot_predecessor(deps.controller)
            probes: list[DiscoveryProbe] = []
            discovery_activation: ArtifactIdentity | None = None
            async with temporary_bass_activation(
                deps.controller,
                graph_raw_text=target.graph_raw_text,
                candidate_clip_limit_dbfs=None,
                proof=baseline_proof,
                predecessor=predecessor,
                to_floor=deps.floor.to_floor,
                assert_at_floor=deps.floor.assert_at_floor,
            ) as readback:
                discovery_activation = await _readback_receipt(
                    sink,
                    role="discovery_activation",
                    target_id=target.target_id,
                    active_config_raw=readback.active_config_raw,
                    graph_fingerprint=readback.graph_fingerprint,
                    configured_clip_limit_dbfs=readback.configured_clip_limit_dbfs,
                )
                partials.append(discovery_activation)
                probes = list(
                    await deps.executor.run_discovery(
                        target=target,
                        active_graph_readback=discovery_activation,
                        sink=sink,
                        stop=deps.stop,
                    )
                )
                deps.stop.check()
            discovery_restoration = await _readback_receipt(
                sink,
                role="discovery_restoration",
                target_id=target.target_id,
                active_config_raw=predecessor.active_config_raw,
                graph_fingerprint=predecessor.graph_fingerprint,
                configured_clip_limit_dbfs=target.baseline_clip_limit_dbfs,
            )
            partials.append(discovery_restoration)

        if not probes:
            return _stop_result("discovery produced no candidate inventory", "refused")

        # Candidate inventory: the DISTINCT pre-limiter peaks at/below baseline,
        # least-to-most permissive. sample_peak_dbfs floors near-silent captures
        # at exactly -120 dBFS, so two quiet probes can collide — the protocol
        # orders the *distinct* measured candidates, and duplicate settings would
        # make the producer refuse the whole campaign as inconsistent.
        distinct: dict[float, DiscoveryProbe] = {}
        for probe in probes:
            if probe.pre_limiter_peak_dbfs <= target.baseline_clip_limit_dbfs:
                distinct.setdefault(probe.pre_limiter_peak_dbfs, probe)
        if not distinct:
            return _stop_result("no eligible candidate at or below baseline", "refused")
        ordered = [distinct[setting] for setting in sorted(distinct)]

        source_records: list[dict[str, object]] = []
        candidate_records: list[dict[str, object]] = []
        source_fp_by_setting: dict[float, str] = {}
        for probe in ordered:
            source = bundle.build_source_observation(
                stimulus=probe.stimulus,
                admission=probe.admission,
                active_graph_readback=probe.active_graph_readback,
                pre_limiter_pcm=probe.pre_limiter_pcm,
                peak_analysis=probe.peak_analysis,
                pre_limiter_peak_dbfs=probe.pre_limiter_peak_dbfs,
            )
            source_records.append(source)
            source_fp_by_setting[probe.pre_limiter_peak_dbfs] = str(
                source["source_fingerprint"]
            )

        # --- Candidate pass (least-to-most permissive, stop at first accepted).
        for probe in ordered:
            deps.stop.check()
            setting = probe.pre_limiter_peak_dbfs
            async with deps.open_window():
                predecessor = await snapshot_predecessor(deps.controller)

                # Phase 1: reference activation at the baseline limiter.
                async with temporary_bass_activation(
                    deps.controller,
                    graph_raw_text=target.graph_raw_text,
                    candidate_clip_limit_dbfs=None,
                    proof=baseline_proof,
                    predecessor=predecessor,
                    to_floor=deps.floor.to_floor,
                    assert_at_floor=deps.floor.assert_at_floor,
                ) as ref_readback:
                    reference_activation = await _readback_receipt(
                        sink,
                        role=f"reference_activation_{setting:g}",
                        target_id=target.target_id,
                        active_config_raw=ref_readback.active_config_raw,
                        graph_fingerprint=ref_readback.graph_fingerprint,
                        configured_clip_limit_dbfs=ref_readback.configured_clip_limit_dbfs,
                    )
                    partials.append(reference_activation)
                    reference_graph_fingerprint = ref_readback.graph_fingerprint
                    reference = await deps.executor.run_reference_sweep(
                        target=target,
                        reference_readback=reference_activation,
                        sink=sink,
                        stop=deps.stop,
                    )
                    deps.stop.check()

                # Phase 2: candidate activation at the candidate clip_limit.
                candidate_proof = ActivationProof(
                    limiter_name=target.limiter_name,
                    owner_channels=target.owner_channels,
                    profile_summary=target.profile_summary,
                    expected_clip_limit_dbfs=setting,
                )
                async with temporary_bass_activation(
                    deps.controller,
                    graph_raw_text=target.graph_raw_text,
                    candidate_clip_limit_dbfs=setting,
                    proof=candidate_proof,
                    predecessor=predecessor,
                    to_floor=deps.floor.to_floor,
                    assert_at_floor=deps.floor.assert_at_floor,
                ) as cand_readback:
                    candidate_activation = await _readback_receipt(
                        sink,
                        role=f"candidate_activation_{setting:g}",
                        target_id=target.target_id,
                        active_config_raw=cand_readback.active_config_raw,
                        graph_fingerprint=cand_readback.graph_fingerprint,
                        configured_clip_limit_dbfs=cand_readback.configured_clip_limit_dbfs,
                    )
                    partials.append(candidate_activation)
                    measured = await deps.executor.run_candidate(
                        target=target,
                        candidate_setting_dbfs=setting,
                        candidate_readback=candidate_activation,
                        reference=reference,
                        sink=sink,
                        stop=deps.stop,
                    )
                    deps.stop.check()

                restoration_receipt = await _readback_receipt(
                    sink,
                    role=f"candidate_restoration_{setting:g}",
                    target_id=target.target_id,
                    active_config_raw=predecessor.active_config_raw,
                    graph_fingerprint=predecessor.graph_fingerprint,
                    configured_clip_limit_dbfs=target.baseline_clip_limit_dbfs,
                )
                partials.append(restoration_receipt)

            # Assemble the full paired sweep record: candidate core + the
            # runner-owned reference activation + the phase-1 reference capture.
            sweep_record = bundle.build_sweep_record(
                reference_activation_receipt=reference_activation,
                reference_stimulus=reference.reference_stimulus,
                reference_admission=reference.reference_admission,
                reference_acoustic_capture=reference.reference_acoustic_capture,
                transparency_analysis=measured.transparency_analysis,
                reference_target_fingerprint=target.target_fingerprint,
                reference_active_graph_fingerprint=reference_graph_fingerprint,
                reference_configured_clip_limit_dbfs=target.baseline_clip_limit_dbfs,
                transparency_verdict=measured.transparency_verdict,
                **measured.sweep_core,
            )

            disposition = _classify_candidate(measured, sweep_record)
            if disposition == "refused":
                # An honest transfer/quality/protection/clamp failure ends the
                # target; no evaluated candidate is emitted for a failed pass.
                return _stop_result(
                    f"candidate {setting:g} failed a required verdict", "refused"
                )

            candidate_records.append(
                bundle.build_candidate(
                    limiter_threshold_dbfs=setting,
                    source_fingerprint=source_fp_by_setting[setting],
                    candidate_activation_receipt=candidate_activation,
                    configured_clip_limit_dbfs=measured.configured_clip_limit_dbfs,
                    active_target_fingerprint=target.target_fingerprint,
                    active_graph_fingerprint=measured.active_graph_fingerprint,
                    ordered_owner_chain=measured.ordered_owner_chain,
                    digital_transfer_probe=measured.digital_transfer_probe,
                    sweep_transparency=sweep_record,
                    sustain_stress=measured.sustain_stress,
                    candidate_restoration_receipt=restoration_receipt,
                    restored_graph_fingerprint=natural_graph_fingerprint,
                    disposition=disposition,
                )
            )
            if disposition == "accepted":
                # Stop at the first accepted candidate. If none accepts, the
                # evaluated result carries the transparency-failed candidates and
                # the producer classifies it out-of-envelope — we do not discard
                # honestly-measured candidates behind a refused stop.
                break

        assert discovery_activation is not None
        return bundle.build_evaluated_result(
            discovery_activation_receipt=discovery_activation,
            candidate_sources=source_records,
            discovery_restoration_receipt=discovery_restoration,
            candidates_least_to_most_permissive=candidate_records,
        )
    except BenchAborted as exc:
        return _stop_result(str(exc), "aborted")


async def run_campaign(
    deps: BenchDeps,
    *,
    manifest: CampaignManifest,
    measured_context: Mapping[str, object],
    targets: Sequence[TargetPlan],
    retained_facts: Mapping[str, ArtifactIdentity],
    sink: BundleSink,
) -> dict[str, object]:
    """Run the campaign for every target and emit the replayable bundle.

    ``targets`` is deepest-through-natural, the same order as
    ``measured_context['target_order']``. Every target runs; a target that stops
    early records its ``refused``/``aborted`` result and the campaign continues
    so the operator retains every partial. Returns the emitted bundle dict (also
    written to ``sink``).
    """

    natural_graph_fingerprint = str(measured_context["natural_graph_fingerprint"])
    manifest_artifact = sink.write_json(
        "campaign_manifest.json",
        manifest.to_dict(),
        kind="jts_bass_extension_bench_campaign_manifest",
    )

    target_results: list[dict[str, object]] = []
    for target in targets:
        result = await _run_target(
            deps, target, natural_graph_fingerprint=natural_graph_fingerprint, sink=sink
        )
        target_results.append(
            bundle.build_target(
                target_id=target.target_id,
                target_fingerprint=target.target_fingerprint,
                result=result,
            )
        )

    emitted = bundle.build_bundle(
        measured_context=measured_context,
        campaign_manifest=manifest_artifact,
        retained_facts=retained_facts,
        targets=target_results,
    )
    sink.write_json("bundle.json", emitted, kind="jts_bass_extension_bench_bundle")
    return emitted
