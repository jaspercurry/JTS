# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


from __future__ import annotations

import ast
import pathlib

import pytest

from jasper.active_speaker.angle_capture import WALK_REFUSAL_REASONS
from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import (
    refusal_copy,
)

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
        "REASON_DELAY_IMPLAUSIBLE",
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
        "REASON_CAPTURE_TIMEOUT",
        "REASON_SNR_FLOOR",
        "REASON_USER_STOPPED",
        "REASON_VERIFY_CROSSOVER_REGION",
        "REASON_VERIFY_INCONCLUSIVE",
        "REASON_VERIFY_LEVEL_SHIFT",
        "REASON_VERIFY_OUT_OF_TOLERANCE",
        "REASON_VOLUME_UNRESOLVED",
        "ReasonSpec",
        "RetryableReasonCopy",
        "SCREEN_KIND_REASONS",
        "TEMPLATE_FIX_AND_RETRY",
        "TEMPLATE_HARD_STOP",
        "TEMPLATE_SESSION_RESTART",
        "TEMPLATE_SILENT_AUTO_RETRY",
        "TEMPLATE_VERIFY_FAIL",
        "TEMPLATE_VOLUME_RECOVERY",
        "TRANSIENT_AUTO_RETRY_CODES",
        "_retriable_reason",
        "_screen_refusal_code",
        "locate_failed_diagnosis",
        "locate_failed_message",
        "reason_diagnosis",
        "reason_message",
        "verify_inconclusive_cause",
        "verify_inconclusive_diagnosis",
        "verify_inconclusive_message",
    ),
    "spatial": (
        "GEOMETRY_RETRY_POSITIONS",
    ),
    "crossover_v2_flow": (
            "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB",
        "PRESCRIBED_NON_WORSENING_DB",
    ),
    "capture_dispatch": (
        "_gate_disclosure",
        "_gate_floor_source",
        "_gate_record",
        "_gate_window_ms",
        "_pilot_by_role",
        "_pilot_diag_fields",
        "_pilot_transfer_by_role",
        "_sweep_schedule_diag_fields",
        "_sweep_schedule_ok",
    ),
}

FLOW_OWNED: frozenset[str] = frozenset(MOVED_NAMES["crossover_v2_flow"])


def test_the_flow_defines_none_of_the_moved_names_itself():
    src = pathlib.Path(flow.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    moved = {s for names in MOVED_NAMES.values() for s in names} - FLOW_OWNED

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


def test_nothing_but_the_flow_declares_the_names_the_flow_owns():
    """The single-owner pin for :data:`FLOW_OWNED`, as absence across the tree.

    For a name the PACKAGE owns, "one definition" is pinned by identity against
    that owner. These three have no package module — the ``attempt_grading``
    fold put them where the flow applies them — so the same guarantee has to be
    read the other way round: no OTHER module may declare the name at all.
    Walked at any nesting depth, over every product module rather than the
    handful that import them, because the copy this suite exists to prevent
    would appear in whichever module found it easier to restate 0.5 than to
    import it.
    """

    product = pathlib.Path(flow.__file__).parents[1]
    owner = pathlib.Path(flow.__file__).resolve()

    declared: list[str] = []
    for path in sorted(product.rglob("*.py")):
        if path.resolve() == owner:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            else:
                continue
            declared += [
                f"{path.relative_to(product.parent)}:{node.lineno}:{n}"
                for n in names if n in FLOW_OWNED
            ]

    assert declared == [], (
        "a second declaration of a name crossover_v2_flow owns: "
        f"{sorted(declared)}"
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


@pytest.mark.parametrize("layer", ["base", "tune", "room"])
def test_upstream_mismatch_reasons_are_retired(layer):
    assert f"measurement_candidate_{layer}_mismatch" not in refusal_copy.REASON_REGISTRY
