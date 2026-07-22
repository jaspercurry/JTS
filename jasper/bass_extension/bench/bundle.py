# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure emitter for the frozen replayable evidence bundle.

This module shapes exactly the bundle schema pinned by
``docs/bass-extension-waves/limiter-evidence-protocol.md`` "Replayable accepted
bundle" — the shape the frozen pure producer consumes. It is pure: it takes
already-recorded :class:`~jasper.audio_measurement.evidence_identity.ArtifactIdentity`
handles (the runner's sink writes the PCM/capture/analysis bytes and returns
these) plus the measured scalars, and returns the bundle ``dict``. It writes no
files, computes only content fingerprints, and never runs the producer.

The runner is the single caller. The bench tests round-trip a synthetic bundle
built here through the frozen producer to prove the emitter writes exactly what
the producer reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from jasper.audio_measurement.evidence_identity import ArtifactIdentity, json_fingerprint

from .context import (
    BUNDLE_KIND,
    BUNDLE_PROTOCOL_REVISION,
    BUNDLE_SCHEMA_VERSION,
)

_RETAINED_FACT_NAMES: tuple[str, ...] = (
    "sweep",
    "sustain",
    "commanded_level",
    "stimulus_peak",
    "boost",
    "digital_clamp",
)


def _id(artifact: ArtifactIdentity) -> dict[str, object]:
    return artifact.to_dict()


def build_source_observation(
    *,
    stimulus: ArtifactIdentity,
    admission: ArtifactIdentity,
    active_graph_readback: ArtifactIdentity,
    pre_limiter_pcm: ArtifactIdentity,
    peak_analysis: ArtifactIdentity,
    pre_limiter_peak_dbfs: float,
) -> dict[str, object]:
    """One discovery source observation, with its content fingerprint."""

    core = {
        "stimulus": _id(stimulus),
        "admission": _id(admission),
        "active_graph_readback": _id(active_graph_readback),
        "pre_limiter_pcm": _id(pre_limiter_pcm),
        "peak_analysis": _id(peak_analysis),
        "pre_limiter_peak_dbfs": pre_limiter_peak_dbfs,
    }
    return {
        "source_fingerprint": json_fingerprint(
            core, field_name="limiter candidate source"
        ),
        **core,
    }


def build_transfer_record(
    *,
    stimulus: ArtifactIdentity,
    pre_limiter_pcm: ArtifactIdentity,
    post_limiter_pcm: ArtifactIdentity,
    reference_post_limiter_pcm: ArtifactIdentity,
    transfer_analysis: ArtifactIdentity,
    verdict: str,
) -> dict[str, object]:
    """The isolated ``digital_transfer_probe`` record."""

    return {
        "stimulus": _id(stimulus),
        "pre_limiter_pcm": _id(pre_limiter_pcm),
        "post_limiter_pcm": _id(post_limiter_pcm),
        "reference_post_limiter_pcm": _id(reference_post_limiter_pcm),
        "transfer_analysis": _id(transfer_analysis),
        "verdict": verdict,
    }


def _measurement_core(
    *,
    stimulus: ArtifactIdentity,
    admission: ArtifactIdentity,
    pre_limiter_pcm: ArtifactIdentity,
    post_limiter_pcm: ArtifactIdentity,
    acoustic_capture: ArtifactIdentity,
    signal_analysis: ArtifactIdentity,
    protection_analysis: ArtifactIdentity,
    stimulus_band_hz: tuple[float, float],
    stimulus_effective_peak_dbfs: float,
    commanded_main_volume_db: float,
    target_boost_db: float,
    digital_clamp_passed: bool,
    pre_limiter_peak_dbfs: float,
    post_limiter_peak_dbfs: float,
    hold_duration_s: float,
    required_cooldown_s: float,
    repeat_count: int,
    quality_verdict: str,
    protection_verdict: str,
) -> dict[str, object]:
    return {
        "stimulus": _id(stimulus),
        "admission": _id(admission),
        "pre_limiter_pcm": _id(pre_limiter_pcm),
        "post_limiter_pcm": _id(post_limiter_pcm),
        "acoustic_capture": _id(acoustic_capture),
        "signal_analysis": _id(signal_analysis),
        "protection_analysis": _id(protection_analysis),
        "stimulus_band_hz": [stimulus_band_hz[0], stimulus_band_hz[1]],
        "stimulus_effective_peak_dbfs": stimulus_effective_peak_dbfs,
        "commanded_main_volume_db": commanded_main_volume_db,
        "target_boost_db": target_boost_db,
        "digital_clamp_passed": digital_clamp_passed,
        "pre_limiter_peak_dbfs": pre_limiter_peak_dbfs,
        "post_limiter_peak_dbfs": post_limiter_peak_dbfs,
        "hold_duration_s": hold_duration_s,
        "required_cooldown_s": required_cooldown_s,
        "repeat_count": repeat_count,
        "quality_verdict": quality_verdict,
        "protection_verdict": protection_verdict,
    }


def build_sustain_record(**kwargs: object) -> dict[str, object]:
    """The ``sustain_stress`` measurement record."""

    return _measurement_core(**kwargs)  # type: ignore[arg-type]


def build_sweep_record(
    *,
    reference_activation_receipt: ArtifactIdentity,
    reference_stimulus: ArtifactIdentity,
    reference_admission: ArtifactIdentity,
    reference_acoustic_capture: ArtifactIdentity,
    transparency_analysis: ArtifactIdentity,
    reference_target_fingerprint: str,
    reference_active_graph_fingerprint: str,
    reference_configured_clip_limit_dbfs: float,
    transparency_verdict: str,
    **core_kwargs: object,
) -> dict[str, object]:
    """The ``sweep_transparency`` record (measurement core + paired reference)."""

    record = _measurement_core(**core_kwargs)  # type: ignore[arg-type]
    record.update(
        {
            "reference_activation_receipt": _id(reference_activation_receipt),
            "reference_stimulus": _id(reference_stimulus),
            "reference_admission": _id(reference_admission),
            "reference_acoustic_capture": _id(reference_acoustic_capture),
            "transparency_analysis": _id(transparency_analysis),
            "reference_target_fingerprint": reference_target_fingerprint,
            "reference_active_graph_fingerprint": reference_active_graph_fingerprint,
            "reference_configured_clip_limit_dbfs": (
                reference_configured_clip_limit_dbfs
            ),
            "transparency_verdict": transparency_verdict,
        }
    )
    return record


def build_candidate(
    *,
    limiter_threshold_dbfs: float,
    source_fingerprint: str,
    candidate_activation_receipt: ArtifactIdentity,
    configured_clip_limit_dbfs: float,
    active_target_fingerprint: str,
    active_graph_fingerprint: str,
    ordered_owner_chain: Sequence[str],
    digital_transfer_probe: Mapping[str, object],
    sweep_transparency: Mapping[str, object],
    sustain_stress: Mapping[str, object],
    candidate_restoration_receipt: ArtifactIdentity,
    restored_graph_fingerprint: str,
    disposition: str,
) -> dict[str, object]:
    """One complete candidate record."""

    return {
        "limiter_threshold_dbfs": limiter_threshold_dbfs,
        "source_fingerprint": source_fingerprint,
        "candidate_activation_receipt": _id(candidate_activation_receipt),
        "configured_clip_limit_dbfs": configured_clip_limit_dbfs,
        "active_target_fingerprint": active_target_fingerprint,
        "active_graph_fingerprint": active_graph_fingerprint,
        "ordered_owner_chain": list(ordered_owner_chain),
        "digital_transfer_probe": dict(digital_transfer_probe),
        "sweep_transparency": dict(sweep_transparency),
        "sustain_stress": dict(sustain_stress),
        "candidate_restoration_receipt": _id(candidate_restoration_receipt),
        "restored_graph_fingerprint": restored_graph_fingerprint,
        "disposition": disposition,
    }


def build_evaluated_result(
    *,
    discovery_activation_receipt: ArtifactIdentity,
    candidate_sources: Sequence[Mapping[str, object]],
    discovery_restoration_receipt: ArtifactIdentity,
    candidates_least_to_most_permissive: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "disposition": "evaluated",
        "discovery_activation_receipt": _id(discovery_activation_receipt),
        "candidate_sources": [dict(source) for source in candidate_sources],
        "discovery_restoration_receipt": _id(discovery_restoration_receipt),
        "candidates_least_to_most_permissive": [
            dict(candidate) for candidate in candidates_least_to_most_permissive
        ],
    }


def build_stopped_result(
    *,
    disposition: str,
    stop_receipt: ArtifactIdentity,
    partial_artifacts: Sequence[ArtifactIdentity],
) -> dict[str, object]:
    """A ``refused`` / ``aborted`` early-stop result, preserving partials."""

    if disposition not in {"refused", "aborted"}:
        raise ValueError("stopped result disposition must be refused or aborted")
    return {
        "disposition": disposition,
        "stop_receipt": _id(stop_receipt),
        "partial_artifacts": [_id(artifact) for artifact in partial_artifacts],
    }


def build_target(
    *, target_id: str, target_fingerprint: str, result: Mapping[str, object]
) -> dict[str, object]:
    return {
        "target_id": target_id,
        "target_fingerprint": target_fingerprint,
        "result": dict(result),
    }


def build_bundle(
    *,
    measured_context: Mapping[str, object],
    campaign_manifest: ArtifactIdentity,
    retained_facts: Mapping[str, ArtifactIdentity],
    targets: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Assemble the root bundle and stamp its ``evidence_fingerprint``.

    ``retained_facts`` maps each of the six fact names to the retained-artifact
    identity; every entry is recorded with ``status='replaced'`` per the frozen
    schema (this revision does not accept self-asserted reuse).
    """

    missing = set(_RETAINED_FACT_NAMES) - set(retained_facts)
    if missing:
        raise ValueError(f"retained_facts missing: {sorted(missing)}")
    facts = {
        name: {"status": "replaced", "artifact": _id(retained_facts[name])}
        for name in _RETAINED_FACT_NAMES
    }
    root: dict[str, object] = {
        "kind": BUNDLE_KIND,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "protocol_revision": BUNDLE_PROTOCOL_REVISION,
        "measured_context": dict(measured_context),
        "campaign_manifest": _id(campaign_manifest),
        "retained_facts": facts,
        "targets": [dict(target) for target in targets],
    }
    root["evidence_fingerprint"] = json_fingerprint(
        root, field_name="bass extension bench bundle"
    )
    return root
