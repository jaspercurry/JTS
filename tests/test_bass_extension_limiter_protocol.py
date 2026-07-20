# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "docs" / "bass-extension-waves" / "limiter-evidence-protocol.md"
WAVE_4 = ROOT / "docs" / "bass-extension-waves" / "wave-4-commissioning-backend.md"
PLAN = ROOT / "docs" / "HANDOFF-bass-extension-plan.md"


def test_protocol_pins_detector_campaign_and_evidence_artifacts() -> None:
    text = PROTOCOL.read_text(encoding="utf-8")

    for promise in (
        "instantaneous floating-point sample at the input",
        "driver_baseline_limiter_name",
        "membership,\nnot order",
        "instantaneous_float_sample_peak_dbfs_re_unity_at_limiter_input",
        "no separate RMS/envelope detector",
        "current tree has no owner that chooses a detector probe",
        "campaign_manifest",
        "digital_transfer_probe",
        "candidate_activation_receipt",
        "candidate_restoration_receipt",
        "pre_limiter_pcm",
        "reference_post_limiter_pcm",
        "Bare booleans do\nnot stand in for measurements",
        "Each value has `status` exactly\n`replaced`",
    ):
        assert promise in text

    forbidden_quantity = re.compile(
        r"(?<![A-Za-z_])(?:-|−|\+)?\d+(?:\.\d+)?\s*"
        r"(?:dB(?:FS)?|Hz|s|seconds?|ms|samples?|repeats?)\b|"
        r"(?<![A-Za-z_])(?:-|−|\+)?\d+(?:\.\d+)?\s*%"
    )
    assert forbidden_quantity.search(text) is None
    named_quantity_literal = re.compile(
        r"(?i)(?:amplitude|threshold|duration|cooldown|frequency|level|peak|"
        r"hold|limiter|"
        r"repeat(?: count)?)\s*(?:=|:|of|is)?\s*(?:-|−|\+)?\d"
    )
    assert named_quantity_literal.search(text) is None


def test_protocol_pins_total_refusal_and_determinism_contract() -> None:
    text = PROTOCOL.read_text(encoding="utf-8")

    for promise in (
        "produce_limiter_thresholds(evidence,\n*, required_context)",
        "separate trusted boundary",
        "LIMITER_EVIDENCE_SCHEMA_VERSION = 1",
        'LIMITER_EVIDENCE_PROTOCOL_REVISION = "2026-07-19b"',
        "frozen `TargetLimiterThreshold(target_id, target_fingerprint,",
        "frozen `LimiterThresholdSet(evidence_fingerprint,",
        "frozen `LimiterEvidenceRefusal(reason, evidence_paths)`",
        "Unknown fields are inconsistent",
        "over the root\nwithout `evidence_fingerprint`",
        "sorted, duplicate-free paths",
        "1. `missing`",
        "2. `stale`",
        "3. `inconsistent`",
        "4. `out_of_envelope`",
        "virtual roots `$evidence` and `$required_context`",
        "Malformed top-level inputs refuse at their virtual root",
        "non-object top-level input is `inconsistent`",
        "no import or call from `jasper.bass_extension.__init__`",
        "`limiter_threshold_dbfs` is **not established**",
        "must end that target as\n`refused` or `aborted`",
        "is `inconsistent`, not `out_of_envelope`",
        "target-level refusal/abort",
    ):
        assert promise in text

    for field in (
        "target_family_fingerprint",
        "target_order",
        "driver_safety_fingerprint",
        "margin_policy_fingerprint",
        "transparency_policy_fingerprint",
        "natural_graph_fingerprint",
        "baseline_limiter_clip_limit_dbfs",
        "limiter_domain_min_dbfs",
        "limiter_domain_max_dbfs",
        "limiter_domain_fingerprint",
        "camilladsp_build_id",
        "owner_channels",
        "sample_rate_hz",
        "limiter_name",
        "limiter_type",
        "soft_clip",
        "tap_implementation_id",
        "detector_reference",
        "discovery_activation_receipt",
        "result",
        "stop_receipt",
        "partial_artifacts",
        "candidate_sources",
        "discovery_restoration_receipt",
        "candidates_least_to_most_permissive",
        "source_fingerprint",
        "active_graph_readback",
        "peak_analysis",
        "configured_clip_limit_dbfs",
        "active_target_fingerprint",
        "active_graph_fingerprint",
        "ordered_owner_chain",
        "reference_acoustic_capture",
        "reference_activation_receipt",
        "reference_stimulus",
        "reference_admission",
        "reference_target_fingerprint",
        "reference_active_graph_fingerprint",
        "reference_configured_clip_limit_dbfs",
        "transparency_analysis",
        "transparency_verdict",
        "restored_graph_fingerprint",
        "stimulus_band_hz",
        "stimulus_effective_peak_dbfs",
        "commanded_main_volume_db",
        "target_boost_db",
        "digital_clamp_passed",
        "pre_limiter_peak_dbfs",
        "post_limiter_peak_dbfs",
        "hold_duration_s",
        "required_cooldown_s",
        "repeat_count",
        "quality_verdict",
        "protection_verdict",
    ):
        assert f"`{field}`" in text

    assert "transfer or\n   quality/protection failure" not in text


def test_revision_authorizes_only_the_isolated_skeleton() -> None:
    wave = WAVE_4.read_text(encoding="utf-8")
    plan = PLAN.read_text(encoding="utf-8")

    assert "Revision 8 (2026-07-19) — production implementation blocked" in wave
    assert "reviewed bench runner/temporary\n> activation owner is not present yet" in wave
    assert "sole prerequisite-skeleton allowlist authorized by Revision 8" in wave
    assert "`jasper/bass_extension/limiter_evidence.py`" in wave
    assert "`tests/test_bass_extension_limiter_evidence.py`" in wave
    assert "must remain unimported and uncalled by all\nproduction paths" in wave
    assert "contract rev 8 freezes limiter protocol revision `2026-07-19b`" in plan
    assert "reviewed bench runner/temporary activation owner" in plan
