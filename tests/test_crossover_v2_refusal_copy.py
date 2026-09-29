# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


from __future__ import annotations

import ast
import pathlib
from importlib import import_module

import pytest

from jasper.active_speaker.angle_capture import WALK_REFUSAL_REASONS
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import refusal_copy, take_reading
from jasper.audio_measurement import evidence_reasons

MOVED_NAMES: dict[str, tuple[str, ...]] = {
    "refusal_copy": (
        "NON_RETRIABLE_CODES",
        "PhaseVerdict",
        "REASON_AGC_BEHAVIORAL_FAIL",
        "REASON_ANCHOR_AMBIGUOUS",
        "REASON_APPLY_FAILED",
        "REASON_CHANNEL_MAP_MISMATCH",
        "REASON_CLIPPED",
        "REASON_CLOUD_GEOMETRY_LOCKED",
        "REASON_DELAY_EXCEEDS_SEARCH_WINDOW",
        "REASON_DRIFT_BASELINES_DISAGREE",
        "REASON_INTERNAL_ERROR",
        "REASON_LOCATE_FAILED",
        "REASON_NOISY_ROOM_LINEARITY",
        "REASON_PILOT_LEVEL_COLLAPSE",
        "REASON_PROGRAM_MEASUREMENT_INPUTS_INVALID",
        "REASON_PROGRAM_UNPLAYABLE",
        "REASON_PROTECTION_NOT_SEPARABLE",
        "REASON_PROTECTION_SWEEP_TOO_LOW",
        "REASON_REGISTRY",
        "REASON_SNR_FLOOR",
        "REASON_USER_STOPPED",
        "REASON_VERIFY_CROSSOVER_REGION",
        "REASON_VERIFY_INCONCLUSIVE",
        "REASON_VERIFY_LEVEL_SHIFT",
        "REASON_VOLUME_UNRESOLVED",
        "ReasonSpec",
        "RetryableReasonCopy",
        "TEMPLATE_FIX_AND_RETRY",
        "TEMPLATE_HARD_STOP",
        "TEMPLATE_SESSION_RESTART",
        "TEMPLATE_SILENT_AUTO_RETRY",
        "TEMPLATE_VERIFY_FAIL",
        "TEMPLATE_VOLUME_RECOVERY",
        "TRANSIENT_AUTO_RETRY_CODES",
        "_retriable_reason",
        "reason_message",
    ),
    "spatial": ("GEOMETRY_RETRY_POSITIONS",),
    "capture_dispatch": (
        "_gate_window_ms",
        "_pilot_transfer_by_role",
        "_sweep_schedule_diag_fields",
        "_sweep_schedule_ok",
    ),
}


def test_the_flow_defines_none_of_the_moved_names_itself():
    src = pathlib.Path(flow.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    moved = {s for names in MOVED_NAMES.values() for s in names}

    redefined: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in moved:
                redefined.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in moved:
                    redefined.append(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id in moved:
                redefined.append(node.target.id)

    assert redefined == [], (
        "the flow re-declares names the crossover_v2 package owns: "
        f"{sorted(redefined)}"
    )


@pytest.mark.parametrize("code", sorted(WALK_REFUSAL_REASONS | {
    "measurement_candidate_required", "measurement_candidate_invalid",
    "measurement_scope_invalid", "measurement_filters_invalid", "measurement_branch_channels",
    "retries_spent", "placement_required", "retry_gain_missing", "take_stopped", "cancelled",
    "measurement_door_session_live", "measurement_door_no_volume_owner", "measurement_door_volume_not_open",
    "wired_capture_failed", "program_not_composed",
}))
def test_graph_and_walk_refusals_have_household_copy_and_retry_policy(code):
    spec = refusal_copy.REASON_REGISTRY[code]
    assert spec.code == code
    assert isinstance(spec.message, str) and spec.message.strip()
    assert spec.retry_budget == 0


@pytest.mark.parametrize("code", ["measurement_candidate_required", "measurement_unregistered"])
def test_refusal_copy_lookup_returns_fallback_copy_and_an_independent_action(code):
    message, action = refusal_copy.refusal_copy_for(code)
    spec = refusal_copy.REASON_REGISTRY.get(code, refusal_copy.REASON_REGISTRY[refusal_copy.REASON_INTERNAL_ERROR])
    assert message is spec.message
    assert action == spec.next_action
    if action is not None:
        action["id"] = "changed"
        assert refusal_copy.refusal_copy_for(code)[1]["id"] == spec.next_action["id"]


# The readers own most of these codes outside evidence_reasons, so the module scan below cannot see them.
@pytest.mark.parametrize("code, action", [
    (take_reading.REFUSE_TAKE_BAND_TOO_NARROW, "choose_window"),
    (take_reading.REFUSE_COMPARE_NO_COMMON_BAND, "name_comparand"),
    (take_reading.REFUSE_COMPARE_RATES_DIFFER, "name_comparand"),
    (take_reading.REFUSE_PREVIEW_UNREADABLE, "name_comparand"),
    (take_reading.REFUSE_COMPARE_NO_COMPARAND, "name_comparand"),
    ("measurement_captures_missing", "measure_again"),
    ("dsp_replay_window_unavailable", "choose_window"),
    ("bass_replay_manifest_predates_adr_0359", "render_again"),
    (evidence_reasons.REASON_COVERAGE_SHORT, "measure_common_band"),
])
def test_a_round_view_refusal_resolves_to_its_registry_action(code, action):
    spec = refusal_copy.REASON_REGISTRY[code]
    assert (spec.code, spec.next_action and spec.next_action["id"]) == (code, action)


@pytest.mark.parametrize("layer", ["base", "tune", "room"])
def test_upstream_mismatch_reasons_are_retired(layer):
    assert f"measurement_candidate_{layer}_mismatch" not in refusal_copy.REASON_REGISTRY


@pytest.mark.parametrize("module_name, prefixes", [
    ('jasper.audio_measurement.evidence_reasons', ''),
    ('jasper.audio_measurement.rear_evidence', 'REASON_'),
    ('jasper.audio_measurement.interference_nulls', 'REASON_'),
    ('jasper.audio_measurement.room_limits', 'REASON_'),
    ('jasper.audio_measurement.timing_verification', 'REASON_'),
    ('jasper.active_speaker.crossover_v2.rear_views', ('REASON_', 'REFUSE_')),
    ('jasper.active_speaker.crossover_v2.feature_classifier', ('CAPTURE_', 'CAPTURES_', 'NO_ADMISSIBLE_', 'NO_FEATURES_', 'PROGRAM_MISSING', 'ROUND_SHAPE_')),
    ('jasper.active_speaker.crossover_v2.feature_classifier.captures', ('CAPTURE_', 'CAPTURES_', 'NO_ADMISSIBLE_', 'NO_FEATURES_', 'PROGRAM_MISSING', 'ROUND_SHAPE_')),
    ('jasper.active_speaker.round_verdicts', 'REASON_'),
    ('jasper.active_speaker.round_view_artifacts', 'REASON_'),
    ('jasper.cli.round_views._common', 'REASON_'),
    ('jasper.cli.round_views.repeat', 'REASON_'),
    ('jasper.active_speaker.crossover_v2.round_views.directivity', 'REASON_'),
])
def test_every_analysis_reason_is_one_evidence_code_with_a_next_action(module_name, prefixes):
    constants = {name: value for name, value in vars(evidence_reasons).items()
                 if name.isupper() and isinstance(value, str)}
    assert len(constants) == len(set(constants.values()))
    codes = {value for name, value in vars(import_module(module_name)).items()
             if name.isupper() and name.startswith(prefixes) and isinstance(value, str)}
    assert codes and codes <= set(constants.values())
    for code in codes:
        spec = refusal_copy.REASON_REGISTRY[code]
        assert spec.code == code and spec.message and spec.next_action
