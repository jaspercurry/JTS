# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from jasper.audio_measurement.evidence_identity import ArtifactIdentity, json_fingerprint
from jasper.bass_extension.limiter_evidence import (
    LIMITER_EVIDENCE_PROTOCOL_REVISION,
    LIMITER_EVIDENCE_SCHEMA_VERSION,
    LimiterEvidenceRefusal,
    LimiterRefusalReason,
    LimiterThresholdSet,
    produce_limiter_thresholds,
)


# All values below are parser fixtures, not audio-safety evidence or defaults.
DOMAIN_MIN = -90.0
DOMAIN_MAX = -2.0
BASELINE = -5.0
DEEP_THRESHOLD = -20.0
NATURAL_THRESHOLD = -10.0
DETECTOR_REFERENCE = "instantaneous_float_sample_peak_dbfs_re_unity_at_limiter_input"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _artifact(label: str, *, payload: str | None = None) -> dict[str, Any]:
    payload_label = payload or label
    return ArtifactIdentity(
        bundle_kind="synthetic_limiter_test",
        bundle_id="fixture",
        relative_path=f"{label}.json",
        sha256=_sha(payload_label),
        byte_size=len(payload_label.encode("utf-8")),
    ).to_dict()


def _target_identity(target_id: str) -> dict[str, str]:
    return {"target_id": target_id, "target_fingerprint": _sha(f"target:{target_id}")}


def _context(*target_ids: str) -> dict[str, Any]:
    return {
        "target_family_fingerprint": _sha("family"),
        "target_order": [_target_identity(target_id) for target_id in target_ids],
        "driver_safety_fingerprint": _sha("driver-safety"),
        "margin_policy_fingerprint": _sha("margin"),
        "transparency_policy_fingerprint": _sha("transparency"),
        "natural_graph_fingerprint": _sha("natural-graph"),
        "baseline_limiter_clip_limit_dbfs": BASELINE,
        "limiter_domain_min_dbfs": DOMAIN_MIN,
        "limiter_domain_max_dbfs": DOMAIN_MAX,
        "limiter_domain_fingerprint": _sha("limiter-domain"),
        "camilladsp_build_id": "synthetic-build",
        "owner_channels": [2],
        "sample_rate_hz": 48_000,
        "limiter_name": "baseline_limiter_woofer",
        "limiter_type": "Limiter",
        "soft_clip": True,
        "tap_implementation_id": "synthetic-nonmutating-tap",
        "detector_reference": DETECTOR_REFERENCE,
    }


def _source(target_id: str, threshold: float, ordinal: int = 0) -> dict[str, Any]:
    core = {
        "stimulus": _artifact(f"{target_id}-source-stimulus-{ordinal}"),
        "admission": _artifact(f"{target_id}-source-admission-{ordinal}"),
        "active_graph_readback": _artifact(f"{target_id}-source-graph-{ordinal}"),
        "pre_limiter_pcm": _artifact(f"{target_id}-source-pcm-{ordinal}"),
        "peak_analysis": _artifact(f"{target_id}-source-analysis-{ordinal}"),
        "pre_limiter_peak_dbfs": threshold,
    }
    return {
        "source_fingerprint": json_fingerprint(core, field_name="synthetic source"),
        **core,
    }


def _measurement(target_id: str, role: str, *, sweep: bool) -> dict[str, Any]:
    stimulus_payload = f"{target_id}-{role}-stimulus-payload"
    admission_payload = f"{target_id}-{role}-admission-payload"
    result: dict[str, Any] = {
        "stimulus": _artifact(f"{target_id}-{role}-stimulus", payload=stimulus_payload),
        "admission": _artifact(f"{target_id}-{role}-admission", payload=admission_payload),
        "pre_limiter_pcm": _artifact(f"{target_id}-{role}-pre"),
        "post_limiter_pcm": _artifact(f"{target_id}-{role}-post"),
        "acoustic_capture": _artifact(f"{target_id}-{role}-capture"),
        "signal_analysis": _artifact(f"{target_id}-{role}-signal-analysis"),
        "protection_analysis": _artifact(f"{target_id}-{role}-protection-analysis"),
        "stimulus_band_hz": [30.0, 200.0],
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
    if sweep:
        result.update(
            {
                "reference_activation_receipt": _artifact(
                    f"{target_id}-{role}-reference-activation"
                ),
                "reference_stimulus": _artifact(
                    f"{target_id}-{role}-reference-stimulus",
                    payload=stimulus_payload,
                ),
                "reference_admission": _artifact(
                    f"{target_id}-{role}-reference-admission",
                    payload=admission_payload,
                ),
                "reference_acoustic_capture": _artifact(
                    f"{target_id}-{role}-reference-capture"
                ),
                "transparency_analysis": _artifact(
                    f"{target_id}-{role}-transparency-analysis"
                ),
                "reference_target_fingerprint": _sha(f"target:{target_id}"),
                "reference_active_graph_fingerprint": _sha(
                    f"{target_id}-reference-graph"
                ),
                "reference_configured_clip_limit_dbfs": BASELINE,
                "transparency_verdict": "pass",
            }
        )
    return result


def _candidate(
    target_id: str,
    source: dict[str, Any],
    threshold: float,
    *,
    transparency: str = "pass",
) -> dict[str, Any]:
    transfer_payload = f"{target_id}-transfer-post-{threshold}"
    sweep = _measurement(target_id, "sweep", sweep=True)
    sweep["transparency_verdict"] = transparency
    return {
        "limiter_threshold_dbfs": threshold,
        "source_fingerprint": source["source_fingerprint"],
        "candidate_activation_receipt": _artifact(
            f"{target_id}-candidate-activation-{threshold}"
        ),
        "configured_clip_limit_dbfs": threshold,
        "active_target_fingerprint": _sha(f"target:{target_id}"),
        "active_graph_fingerprint": _sha(f"{target_id}-active-graph-{threshold}"),
        "ordered_owner_chain": [
            "driver_delay",
            "bass_ext_lt",
            "bass_ext_subsonic",
            "baseline_limiter_woofer",
        ],
        "digital_transfer_probe": {
            "stimulus": _artifact(f"{target_id}-transfer-stimulus-{threshold}"),
            "pre_limiter_pcm": _artifact(f"{target_id}-transfer-pre-{threshold}"),
            "post_limiter_pcm": _artifact(
                f"{target_id}-transfer-post-{threshold}", payload=transfer_payload
            ),
            "reference_post_limiter_pcm": _artifact(
                f"{target_id}-transfer-reference-{threshold}", payload=transfer_payload
            ),
            "transfer_analysis": _artifact(f"{target_id}-transfer-analysis-{threshold}"),
            "verdict": "pass",
        },
        "sweep_transparency": sweep,
        "sustain_stress": _measurement(target_id, "sustain", sweep=False),
        "candidate_restoration_receipt": _artifact(
            f"{target_id}-candidate-restoration-{threshold}"
        ),
        "restored_graph_fingerprint": _sha("natural-graph"),
        "disposition": (
            "accepted" if transparency == "pass" else "limiter_transparency_failed"
        ),
    }


def _target(target_id: str, threshold: float) -> dict[str, Any]:
    source = _source(target_id, threshold)
    return {
        **_target_identity(target_id),
        "result": {
            "disposition": "evaluated",
            "discovery_activation_receipt": _artifact(f"{target_id}-discovery-activation"),
            "candidate_sources": [source],
            "discovery_restoration_receipt": _artifact(
                f"{target_id}-discovery-restoration"
            ),
            "candidates_least_to_most_permissive": [
                _candidate(target_id, source, threshold)
            ],
        },
    }


def _evidence(context: dict[str, Any], targets: list[dict[str, Any]]) -> dict[str, Any]:
    root: dict[str, Any] = {
        "kind": "jts_bass_extension_limiter_evidence",
        "schema_version": LIMITER_EVIDENCE_SCHEMA_VERSION,
        "protocol_revision": LIMITER_EVIDENCE_PROTOCOL_REVISION,
        "measured_context": deepcopy(context),
        "campaign_manifest": _artifact("campaign-manifest"),
        "retained_facts": {
            name: {"status": "replaced", "artifact": _artifact(f"retained-{name}")}
            for name in (
                "sweep",
                "sustain",
                "commanded_level",
                "stimulus_peak",
                "boost",
                "digital_clamp",
            )
        },
        "targets": targets,
    }
    return _refresh(root)


def _refresh(bundle: dict[str, Any]) -> dict[str, Any]:
    bundle.pop("evidence_fingerprint", None)
    bundle["evidence_fingerprint"] = json_fingerprint(
        bundle, field_name="synthetic limiter bundle"
    )
    return bundle


@pytest.fixture
def accepted() -> tuple[dict[str, Any], dict[str, Any]]:
    context = _context("deep", "natural")
    bundle = _evidence(
        context,
        [_target("deep", DEEP_THRESHOLD), _target("natural", NATURAL_THRESHOLD)],
    )
    return bundle, context


def _refusal(
    bundle: object,
    context: object,
    reason: LimiterRefusalReason,
) -> LimiterEvidenceRefusal:
    result = produce_limiter_thresholds(bundle, required_context=context)
    assert isinstance(result, LimiterEvidenceRefusal)
    assert result.reason is reason
    assert result.evidence_paths == tuple(sorted(set(result.evidence_paths)))
    return result


def test_accepted_bundle_is_deterministic_and_selects_only_measured_values(
    accepted: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    bundle, context = accepted

    first = produce_limiter_thresholds(bundle, required_context=context)
    second = produce_limiter_thresholds(bundle, required_context=context)
    round_tripped = produce_limiter_thresholds(
        json.loads(json.dumps(bundle)),
        required_context=json.loads(json.dumps(context)),
    )

    assert isinstance(first, LimiterThresholdSet)
    assert first == second == round_tripped
    assert [target.limiter_threshold_dbfs for target in first.targets] == [
        DEEP_THRESHOLD,
        NATURAL_THRESHOLD,
    ]
    assert [
        target.source_fingerprint for target in first.targets
    ] == [
        bundle["targets"][0]["result"]["candidate_sources"][0]["source_fingerprint"],
        bundle["targets"][1]["result"]["candidate_sources"][0]["source_fingerprint"],
    ]
    assert set(first.to_dict()) == {
        "schema_version",
        "kind",
        "evidence_fingerprint",
        "required_context_fingerprint",
        "targets",
    }
    assert first.to_dict()["kind"] == "jts_bass_extension_limiter_threshold_set"


def test_refusal_serialization_shape(accepted: tuple[dict[str, Any], dict[str, Any]]) -> None:
    bundle, context = accepted
    del bundle["campaign_manifest"]
    refusal = _refusal(bundle, context, LimiterRefusalReason.MISSING)

    assert refusal.to_dict() == {
        "schema_version": LIMITER_EVIDENCE_SCHEMA_VERSION,
        "kind": "jts_bass_extension_limiter_evidence_refusal",
        "reason": "missing",
        "evidence_paths": ["$evidence.campaign_manifest"],
    }


@pytest.mark.parametrize(
    "mutation, expected_path",
    [
        (lambda bundle: bundle.pop("campaign_manifest"), "$evidence.campaign_manifest"),
        (lambda bundle: bundle["targets"].clear(), "$evidence.targets"),
        (
            lambda bundle: bundle["targets"][0]["result"]["candidate_sources"][0].pop(
                "pre_limiter_pcm"
            ),
            "$evidence.targets[0].result.candidate_sources[0].pre_limiter_pcm",
        ),
    ],
)
def test_missing_evidence_refuses_with_named_path(
    accepted: tuple[dict[str, Any], dict[str, Any]],
    mutation: Callable[[dict[str, Any]], object],
    expected_path: str,
) -> None:
    bundle, context = accepted
    mutation(bundle)
    refusal = _refusal(bundle, context, LimiterRefusalReason.MISSING)
    assert expected_path in refusal.evidence_paths


def test_stale_context_names_both_trust_boundaries(
    accepted: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    bundle, context = accepted
    context["natural_graph_fingerprint"] = _sha("new-natural-graph")

    refusal = _refusal(bundle, context, LimiterRefusalReason.STALE)
    assert refusal.evidence_paths == (
        "$evidence.measured_context.natural_graph_fingerprint",
        "$required_context.natural_graph_fingerprint",
    )


def _candidate_at(bundle: dict[str, Any], target: int = 0, candidate: int = 0) -> dict[str, Any]:
    return bundle["targets"][target]["result"]["candidates_least_to_most_permissive"][
        candidate
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda bundle, context: bundle.update(kind="wrong"),
        lambda bundle, context: bundle.update(schema_version="1"),
        lambda bundle, context: bundle.update(protocol_revision="wrong"),
        lambda bundle, context: bundle.update(unexpected=True),
        lambda bundle, context: bundle["campaign_manifest"].update(sha256="bad"),
        lambda bundle, context: _candidate_at(bundle).update(
            configured_clip_limit_dbfs=DEEP_THRESHOLD + 1.0
        ),
        lambda bundle, context: _candidate_at(bundle).update(
            active_target_fingerprint=_sha("wrong-target")
        ),
        lambda bundle, context: _candidate_at(bundle).update(
            active_graph_fingerprint=_candidate_at(bundle)["sweep_transparency"][
                "reference_active_graph_fingerprint"
            ]
        ),
        lambda bundle, context: _candidate_at(bundle).update(
            ordered_owner_chain=[
                "bass_ext_subsonic",
                "bass_ext_lt",
                "baseline_limiter_woofer",
            ]
        ),
        lambda bundle, context: _candidate_at(bundle).update(
            restored_graph_fingerprint=_sha("wrong-restoration")
        ),
        lambda bundle, context: _candidate_at(bundle).update(
            disposition="limiter_transparency_failed"
        ),
        lambda bundle, context: _candidate_at(bundle)["sweep_transparency"].update(
            reference_target_fingerprint=_sha("wrong-reference-target")
        ),
        lambda bundle, context: _candidate_at(bundle)["sweep_transparency"].update(
            reference_stimulus=_artifact("wrong-reference-stimulus")
        ),
        lambda bundle, context: context.update(limiter_domain_min_dbfs=DOMAIN_MAX),
    ],
)
def test_inconsistent_evidence_never_raises_or_defaults(
    accepted: tuple[dict[str, Any], dict[str, Any]],
    mutation: Callable[[dict[str, Any], dict[str, Any]], object],
) -> None:
    bundle, context = accepted
    mutation(bundle, context)
    _refresh(bundle)
    _refusal(bundle, context, LimiterRefusalReason.INCONSISTENT)


@pytest.mark.parametrize("bad", [None, True, [], object(), {"kind": object()}])
def test_arbitrary_top_level_objects_return_typed_refusal(bad: object) -> None:
    result = produce_limiter_thresholds(bad, required_context=bad)
    assert isinstance(result, LimiterEvidenceRefusal)
    assert result.reason in {LimiterRefusalReason.MISSING, LimiterRefusalReason.INCONSISTENT}


@pytest.mark.parametrize("bad", [[], {}])
def test_malformed_result_disposition_names_its_exact_path(
    accepted: tuple[dict[str, Any], dict[str, Any]], bad: object
) -> None:
    bundle, context = accepted
    bundle["targets"][0]["result"]["disposition"] = bad
    _refresh(bundle)

    refusal = _refusal(bundle, context, LimiterRefusalReason.INCONSISTENT)
    assert "$evidence.targets[0].result.disposition" in refusal.evidence_paths


@pytest.mark.parametrize("disposition", ["refused", "aborted"])
def test_early_stop_is_out_of_envelope_not_missing(
    accepted: tuple[dict[str, Any], dict[str, Any]],
    disposition: str,
) -> None:
    bundle, context = accepted
    bundle["targets"][0]["result"] = {
        "disposition": disposition,
        "stop_receipt": _artifact(f"{disposition}-receipt"),
        "partial_artifacts": [],
    }
    _refresh(bundle)

    refusal = _refusal(bundle, context, LimiterRefusalReason.OUT_OF_ENVELOPE)
    assert "$evidence.targets[0].result.disposition" in refusal.evidence_paths


def test_transparency_failures_without_acceptance_are_out_of_envelope(
    accepted: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    bundle, context = accepted
    candidate = _candidate_at(bundle)
    candidate["sweep_transparency"]["transparency_verdict"] = "fail"
    candidate["disposition"] = "limiter_transparency_failed"
    _refresh(bundle)

    refusal = _refusal(bundle, context, LimiterRefusalReason.OUT_OF_ENVELOPE)
    assert (
        "$evidence.targets[0].result.candidates_least_to_most_permissive"
        in refusal.evidence_paths
    )


@pytest.mark.parametrize(
    "record, field, failure",
    [
        ("digital_transfer_probe", "verdict", "fail"),
        ("sweep_transparency", "quality_verdict", "fail"),
        ("sweep_transparency", "protection_verdict", "fail"),
        ("sweep_transparency", "digital_clamp_passed", False),
        ("sustain_stress", "quality_verdict", "fail"),
        ("sustain_stress", "protection_verdict", "fail"),
        ("sustain_stress", "digital_clamp_passed", False),
    ],
)
def test_evaluated_measurement_failure_is_inconsistent(
    accepted: tuple[dict[str, Any], dict[str, Any]],
    record: str,
    field: str,
    failure: str | bool,
) -> None:
    bundle, context = accepted
    _candidate_at(bundle)[record][field] = failure
    _refresh(bundle)

    refusal = _refusal(bundle, context, LimiterRefusalReason.INCONSISTENT)
    assert refusal.evidence_paths == (
        "$evidence.targets[0].result.candidates_least_to_most_permissive[0].disposition",
    )


@pytest.mark.parametrize("threshold", [-95.0, -3.0])
def test_out_of_domain_or_over_baseline_candidate_refuses(
    threshold: float,
) -> None:
    context = _context("deep")
    bundle = _evidence(context, [_target("deep", threshold)])

    refusal = _refusal(bundle, context, LimiterRefusalReason.OUT_OF_ENVELOPE)
    assert any(path.endswith("limiter_threshold_dbfs") for path in refusal.evidence_paths)


def test_unused_out_of_envelope_source_refuses(
    accepted: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    bundle, context = accepted
    bundle["targets"][0]["result"]["candidate_sources"].append(
        _source("deep", DOMAIN_MIN - 1.0, ordinal=1)
    )
    _refresh(bundle)

    refusal = _refusal(bundle, context, LimiterRefusalReason.OUT_OF_ENVELOPE)
    assert (
        "$evidence.targets[0].result.candidate_sources[1].pre_limiter_peak_dbfs"
        in refusal.evidence_paths
    )


def test_candidate_order_and_duplicate_sources_are_inconsistent(
    accepted: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    bundle, context = accepted
    result = bundle["targets"][0]["result"]
    result["candidate_sources"].append(deepcopy(result["candidate_sources"][0]))
    result["candidates_least_to_most_permissive"].append(
        deepcopy(result["candidates_least_to_most_permissive"][0])
    )
    _refresh(bundle)
    _refusal(bundle, context, LimiterRefusalReason.INCONSISTENT)


def test_candidate_after_first_acceptance_is_out_of_envelope() -> None:
    context = _context("deep")
    target = _target("deep", DEEP_THRESHOLD)
    second_source = _source("deep", NATURAL_THRESHOLD, ordinal=1)
    target["result"]["candidate_sources"].append(second_source)
    target["result"]["candidates_least_to_most_permissive"].append(
        _candidate("deep", second_source, NATURAL_THRESHOLD)
    )
    bundle = _evidence(context, [target])

    refusal = _refusal(bundle, context, LimiterRefusalReason.OUT_OF_ENVELOPE)
    assert "$evidence.targets[0].result.candidates_least_to_most_permissive[1]" in (
        refusal.evidence_paths
    )


def test_candidate_at_reference_setting_requires_the_reference_graph() -> None:
    context = _context("natural")
    target = _target("natural", BASELINE)
    bundle = _evidence(context, [target])
    _refusal(bundle, context, LimiterRefusalReason.INCONSISTENT)

    candidate = target["result"]["candidates_least_to_most_permissive"][0]
    candidate["active_graph_fingerprint"] = candidate["sweep_transparency"][
        "reference_active_graph_fingerprint"
    ]
    _refresh(bundle)
    result = produce_limiter_thresholds(bundle, required_context=context)
    assert isinstance(result, LimiterThresholdSet)
    assert result.targets[0].limiter_threshold_dbfs == BASELINE


def test_family_order_violation_is_out_of_envelope() -> None:
    context = _context("deep", "natural")
    bundle = _evidence(
        context,
        [_target("deep", NATURAL_THRESHOLD), _target("natural", DEEP_THRESHOLD)],
    )
    _refusal(bundle, context, LimiterRefusalReason.OUT_OF_ENVELOPE)


def test_refusal_precedence_is_missing_then_stale_then_inconsistent_then_out(
    accepted: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    bundle, context = accepted
    del bundle["campaign_manifest"]
    context["natural_graph_fingerprint"] = _sha("stale")
    bundle["kind"] = "wrong"
    bundle["targets"][0]["result"] = {
        "disposition": "aborted",
        "stop_receipt": _artifact("stop"),
        "partial_artifacts": [],
    }
    _refusal(bundle, context, LimiterRefusalReason.MISSING)

    bundle["campaign_manifest"] = _artifact("campaign-manifest")
    _refresh(bundle)
    _refusal(bundle, context, LimiterRefusalReason.STALE)

    context["natural_graph_fingerprint"] = _sha("natural-graph")
    _refusal(bundle, context, LimiterRefusalReason.INCONSISTENT)


class _ExplodingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise RuntimeError(f"unexpected item read: {key}")

    def __iter__(self):
        raise RuntimeError("unexpected iteration")

    def __len__(self) -> int:
        raise RuntimeError("unexpected length read")


def test_hostile_mapping_refusal_names_the_input_root(
    accepted: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    bundle, context = accepted

    evidence_refusal = _refusal(
        _ExplodingMapping(), context, LimiterRefusalReason.INCONSISTENT
    )
    assert evidence_refusal.evidence_paths == ("$evidence",)

    context_refusal = _refusal(
        bundle, _ExplodingMapping(), LimiterRefusalReason.INCONSISTENT
    )
    assert context_refusal.evidence_paths == ("$required_context",)


def test_cyclic_input_refusal_names_the_input_root(
    accepted: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    bundle, context = accepted
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    evidence_refusal = _refusal(cyclic, context, LimiterRefusalReason.INCONSISTENT)
    assert evidence_refusal.evidence_paths == ("$evidence",)

    context_refusal = _refusal(bundle, cyclic, LimiterRefusalReason.INCONSISTENT)
    assert context_refusal.evidence_paths == ("$required_context",)


def test_internal_producer_error_is_not_mislabeled_as_input_refusal(
    accepted: tuple[dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, context = accepted

    def explode(_evidence: object, _required_context: object) -> LimiterThresholdSet:
        raise RuntimeError("synthetic internal defect")

    monkeypatch.setitem(produce_limiter_thresholds.__globals__, "_produce", explode)
    with pytest.raises(RuntimeError, match="synthetic internal defect"):
        produce_limiter_thresholds(bundle, required_context=context)


def test_module_is_unreachable_from_production_paths() -> None:
    """The limiter-evidence producer must have no PRODUCTION caller.

    Wave 4 Rev 9 authorizes exactly one consumer — ``ladder.py``'s hardware-free
    synthetic dry run — and only via a FUNCTION-LOCAL import, never at module (or
    class) scope, so no eagerly-imported production path can reach the producer.
    This guard enforces that intent with an AST check instead of a blunt
    substring scan: the contract-authorized function-local import is allowed,
    while any module/class-scope import of the producer in any ``jasper`` file
    still fails. (Operator-approved 2026-07-23 amendment reconciling this frozen
    guard with the Rev 9 mandate; ``ladder.py``'s own test additionally AST-pins
    its import as function-local-only, so the two guards are belt-and-braces.)
    """
    root = Path(__file__).resolve().parents[1]
    module = root / "jasper" / "bass_extension" / "limiter_evidence.py"
    for path in (root / "jasper").rglob("*.py"):
        if path == module:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        function_local: set[int] = set()
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(func):
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        function_local.add(id(node))
        for node in ast.walk(tree):
            imports_producer = (
                isinstance(node, ast.ImportFrom)
                and (
                    "limiter_evidence" in (node.module or "")
                    or any(
                        a.name in {"produce_limiter_thresholds", "limiter_evidence"}
                        for a in node.names
                    )
                )
            ) or (
                isinstance(node, ast.Import)
                and any("limiter_evidence" in a.name for a in node.names)
            )
            if imports_producer:
                assert id(node) in function_local, (
                    f"{path}: the limiter-evidence producer may be imported only "
                    "function-locally (never at module/class scope) — Wave 4 Rev 9"
                )


def test_module_imports_are_pure_and_hardware_free() -> None:
    root = Path(__file__).resolve().parents[1]
    module = root / "jasper" / "bass_extension" / "limiter_evidence.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
    assert imports == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "enum",
        "jasper.audio_measurement.evidence_identity",
        "math",
        "re",
        "typing",
    }
