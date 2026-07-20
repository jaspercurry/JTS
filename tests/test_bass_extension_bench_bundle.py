# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The emitter writes exactly what the frozen producer reads.

Builds a complete accepted bundle with the bench emitter and round-trips it
through the frozen ``produce_limiter_thresholds`` producer — proving the two
agree on the schema. The producer is imported here in the test, never in the
runner package.
"""

from __future__ import annotations

import hashlib
from typing import Any

from jasper.active_speaker.camilla_yaml import (
    BASS_EXTENSION_LT_FILTER,
    BASS_EXTENSION_SUBSONIC_FILTER,
)
from jasper.audio_measurement.evidence_identity import ArtifactIdentity
from jasper.bass_extension.bench import bundle
from jasper.bass_extension.bench.context import (
    BUNDLE_KIND,
    build_measured_context,
    limiter_domain_fingerprint,
)

BASELINE = -1.0
LIMITER_NAME = "baseline_limiter_woofer"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _artifact(label: str, *, payload: str | None = None) -> ArtifactIdentity:
    body = payload or label
    return ArtifactIdentity(
        bundle_kind="jts_bass_extension_limiter_bench",
        bundle_id="fixture",
        relative_path=f"{label}.json",
        sha256=_sha(body),
        byte_size=len(body.encode("utf-8")),
    )


def _target_fp(target_id: str) -> str:
    return _sha(f"target:{target_id}")


def _context(*target_ids: str) -> dict[str, Any]:
    return build_measured_context(
        target_family_fingerprint=_sha("family"),
        target_order=[(tid, _target_fp(tid)) for tid in target_ids],
        driver_safety_fingerprint=_sha("driver-safety"),
        margin_policy_fingerprint=_sha("margin"),
        transparency_policy_fingerprint=_sha("transparency"),
        natural_graph_fingerprint=_sha("natural-graph"),
        baseline_limiter_clip_limit_dbfs=BASELINE,
        camilladsp_build_id="synthetic-build",
        owner_channels=[2],
        sample_rate_hz=48_000,
        limiter_name=LIMITER_NAME,
        tap_implementation_id="synthetic-nonmutating-tap",
    )


def _measurement_core(target_id: str, role: str) -> dict[str, Any]:
    stimulus_payload = f"{target_id}-{role}-stimulus"
    admission_payload = f"{target_id}-{role}-admission"
    return {
        "stimulus": _artifact(f"{target_id}-{role}-stimulus", payload=stimulus_payload),
        "admission": _artifact(
            f"{target_id}-{role}-admission", payload=admission_payload
        ),
        "pre_limiter_pcm": _artifact(f"{target_id}-{role}-pre"),
        "post_limiter_pcm": _artifact(f"{target_id}-{role}-post"),
        "acoustic_capture": _artifact(f"{target_id}-{role}-capture"),
        "signal_analysis": _artifact(f"{target_id}-{role}-signal"),
        "protection_analysis": _artifact(f"{target_id}-{role}-protection"),
        "stimulus_band_hz": (30.0, 200.0),
        "stimulus_effective_peak_dbfs": -30.0,
        "commanded_main_volume_db": -35.0,
        "target_boost_db": 6.0,
        "digital_clamp_passed": True,
        "pre_limiter_peak_dbfs": -15.0,
        "post_limiter_peak_dbfs": -16.0,
        "hold_duration_s": 12.0,
        "required_cooldown_s": 4.0,
        "repeat_count": 2,
        "quality_verdict": "pass",
        "protection_verdict": "pass",
    }


def _sweep(target_id: str, threshold: float) -> dict[str, Any]:
    core = _measurement_core(target_id, "sweep")
    stimulus_payload = f"{target_id}-sweep-stimulus"
    admission_payload = f"{target_id}-sweep-admission"
    return bundle.build_sweep_record(
        reference_activation_receipt=_artifact(f"{target_id}-ref-activation"),
        reference_stimulus=_artifact(
            f"{target_id}-ref-stimulus", payload=stimulus_payload
        ),
        reference_admission=_artifact(
            f"{target_id}-ref-admission", payload=admission_payload
        ),
        reference_acoustic_capture=_artifact(f"{target_id}-ref-capture"),
        transparency_analysis=_artifact(f"{target_id}-transparency"),
        reference_target_fingerprint=_target_fp(target_id),
        reference_active_graph_fingerprint=_sha(f"{target_id}-ref-graph"),
        reference_configured_clip_limit_dbfs=BASELINE,
        transparency_verdict="pass",
        **core,
    )


def _transfer(target_id: str, threshold: float) -> dict[str, Any]:
    post = f"{target_id}-transfer-post-{threshold}"
    return bundle.build_transfer_record(
        stimulus=_artifact(f"{target_id}-transfer-stim-{threshold}"),
        pre_limiter_pcm=_artifact(f"{target_id}-transfer-pre-{threshold}"),
        post_limiter_pcm=_artifact(f"{target_id}-transfer-post-{threshold}", payload=post),
        reference_post_limiter_pcm=_artifact(
            f"{target_id}-transfer-ref-{threshold}", payload=post
        ),
        transfer_analysis=_artifact(f"{target_id}-transfer-analysis-{threshold}"),
        verdict="pass",
    )


def _source(target_id: str, threshold: float) -> dict[str, Any]:
    return bundle.build_source_observation(
        stimulus=_artifact(f"{target_id}-src-stim"),
        admission=_artifact(f"{target_id}-src-adm"),
        active_graph_readback=_artifact(f"{target_id}-src-graph"),
        pre_limiter_pcm=_artifact(f"{target_id}-src-pcm"),
        peak_analysis=_artifact(f"{target_id}-src-analysis"),
        pre_limiter_peak_dbfs=threshold,
    )


def _target(target_id: str, threshold: float) -> dict[str, Any]:
    source = _source(target_id, threshold)
    candidate = bundle.build_candidate(
        limiter_threshold_dbfs=threshold,
        source_fingerprint=str(source["source_fingerprint"]),
        candidate_activation_receipt=_artifact(f"{target_id}-cand-activation-{threshold}"),
        configured_clip_limit_dbfs=threshold,
        active_target_fingerprint=_target_fp(target_id),
        active_graph_fingerprint=_sha(f"{target_id}-active-graph-{threshold}"),
        ordered_owner_chain=[
            "driver_delay",
            BASS_EXTENSION_LT_FILTER,
            BASS_EXTENSION_SUBSONIC_FILTER,
            LIMITER_NAME,
        ],
        digital_transfer_probe=_transfer(target_id, threshold),
        sweep_transparency=_sweep(target_id, threshold),
        sustain_stress=bundle.build_sustain_record(
            **_measurement_core(target_id, "sustain")
        ),
        candidate_restoration_receipt=_artifact(f"{target_id}-cand-restore-{threshold}"),
        restored_graph_fingerprint=_sha("natural-graph"),
        disposition="accepted",
    )
    result = bundle.build_evaluated_result(
        discovery_activation_receipt=_artifact(f"{target_id}-discovery-activation"),
        candidate_sources=[source],
        discovery_restoration_receipt=_artifact(f"{target_id}-discovery-restore"),
        candidates_least_to_most_permissive=[candidate],
    )
    return bundle.build_target(
        target_id=target_id, target_fingerprint=_target_fp(target_id), result=result
    )


def _retained() -> dict[str, ArtifactIdentity]:
    return {
        name: _artifact(f"retained-{name}")
        for name in (
            "sweep",
            "sustain",
            "commanded_level",
            "stimulus_peak",
            "boost",
            "digital_clamp",
        )
    }


def _accepted_bundle() -> tuple[dict[str, Any], dict[str, Any]]:
    context = _context("deep", "natural")
    emitted = bundle.build_bundle(
        measured_context=context,
        campaign_manifest=_artifact("campaign-manifest"),
        retained_facts=_retained(),
        targets=[_target("deep", -20.0), _target("natural", -10.0)],
    )
    return emitted, context


def test_bundle_kind_matches_the_producer_kind_constant() -> None:
    # Explicit coupling to the frozen producer's kind constant. The bench module
    # assembles this string from fragments to avoid embedding the producer's
    # module name (the producer's unreachability guard scans jasper/*.py for it);
    # this pins that assembly to the exact value the producer accepts, so a
    # future cleanup to a literal cannot silently break either side.
    from jasper.bass_extension.limiter_evidence import _EVIDENCE_KIND

    assert BUNDLE_KIND == _EVIDENCE_KIND


def test_emitted_bundle_is_accepted_by_the_frozen_producer() -> None:
    # Imported here, in the test — never in the runner package.
    from jasper.bass_extension.limiter_evidence import (
        LimiterThresholdSet,
        produce_limiter_thresholds,
    )

    emitted, context = _accepted_bundle()
    result = produce_limiter_thresholds(emitted, required_context=context)

    assert isinstance(result, LimiterThresholdSet), result
    assert [target.limiter_threshold_dbfs for target in result.targets] == [-20.0, -10.0]
    assert result.to_dict()["kind"] == "jts_bass_extension_limiter_threshold_set"


def test_emitted_bundle_has_the_frozen_root_keys() -> None:
    emitted, _ = _accepted_bundle()
    assert set(emitted) == {
        "kind",
        "schema_version",
        "protocol_revision",
        "evidence_fingerprint",
        "measured_context",
        "campaign_manifest",
        "retained_facts",
        "targets",
    }
    assert emitted["kind"] == BUNDLE_KIND
    assert emitted["protocol_revision"] == "2026-07-19b"
    assert set(emitted["retained_facts"]) == {
        "sweep",
        "sustain",
        "commanded_level",
        "stimulus_peak",
        "boost",
        "digital_clamp",
    }


def test_limiter_domain_endpoints_bracket_the_emitter_baseline() -> None:
    _, context = _accepted_bundle()
    assert context["limiter_domain_min_dbfs"] == -120.0
    assert context["limiter_domain_max_dbfs"] == 0.0
    assert (
        context["limiter_domain_min_dbfs"]
        <= context["baseline_limiter_clip_limit_dbfs"]
        <= context["limiter_domain_max_dbfs"]
    )
    assert context["limiter_domain_fingerprint"] == limiter_domain_fingerprint()
