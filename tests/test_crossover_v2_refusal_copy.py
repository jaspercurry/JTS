# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0


from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable
from importlib import import_module

import pytest

import jasper
from jasper.active_speaker.angle_capture import WALK_REFUSAL_REASONS
from jasper.active_speaker import crossover_v2_flow as flow, measured_crossover_candidate, wizard_client
from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStoreErrorCode
from jasper.active_speaker.crossover_v2 import (
    _prescription_common, corner_admissibility, evidence_packet, intervention, refusal_copy, take_reading,
)
from jasper.active_speaker.crossover_v2.evidence_packet import offline_reads
from jasper.active_speaker.crossover_v2.room_views import incumbent_room
from jasper.active_speaker.measurement_programs import DRIVER_NOT_OFFERED, LAYOUT_NOT_OFFERED, POSES_NAME_A_LAYOUT
from jasper.audio_measurement import evidence_reasons
from jasper.bass_extension.dynamic import DYNAMIC_BASS_REFUSAL_REASONS
from jasper.cli import _refusal, audition, mic_calibration
from jasper.cli import round as round_cli
from jasper.cli.round_views._common import refused_by_name

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
    (take_reading.REFUSE_BASS_COMPARAND_VIEW_NOT_FILED, "name_comparand"),
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
    ('jasper.active_speaker.crossover_v2.feature_classifier', ('NO_ADMISSIBLE_', 'NO_FEATURES_', 'ROUND_SHAPE_')),
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


_BASE_SET = {"set_id": "a", "base": True, "capture_basis": {}}

#: Codes a gap or a refusal forwards through a variable: no raise site names them, so the scan below cannot see them.
_FORWARDED_CODES = {
    evidence_packet.NO_CANDIDATE_TAKES, evidence_packet.REPEAT_FLOOR_UNMEASURED,
    evidence_packet.REPEAT_FLOOR_UNREADABLE, evidence_packet.REPEAT_FLOOR_UNUSABLE,
    offline_reads.FIELD_MALFORMED, offline_reads.SOURCE_UNREADABLE, intervention.NonFiniteTrimError.refusal_reason,
    # A run with no base set, and one with two.
    *(incumbent_room(None, {"sets": sets})[1] for sets in ([], [_BASE_SET, {**_BASE_SET, "set_id": "b"}])),
    # The evidence store's codes reach ``RoundViewsError`` as ``exc.code.value``, the bass descriptor's reach
    # ``refuse`` as ``exc.reason``, and a pinned corner's two reach the topology refusal as ``reason``.
    *(code.value for code in CommissioningEvidenceStoreErrorCode), *DYNAMIC_BASS_REFUSAL_REASONS,
    corner_admissibility.FC_REJECT_BELOW_DECLARED_FLOOR, corner_admissibility.FC_REJECT_ABOVE_LOWER_DRIVER_BAND,
    # ``jasper-round``'s three plan refusals carry their code as a class attribute, and its wizard client
    # answers with each ``REASON_*`` it names.
    LAYOUT_NOT_OFFERED, POSES_NAME_A_LAYOUT, DRIVER_NOT_OFFERED,
    *(value for name, value in vars(wizard_client).items() if name.startswith("REASON_")),
}
#: Each helper that names a code in a gap or a refusal: the position of the argument that carries it, and the keywords
#: that do. ``refuse`` is what each judge's ``*PrescriptionRefused`` aliases raise, and ``_refuse`` what the candidate's
#: field checks raise; ``failed`` and ``refused`` are every CLI's refusal record, and ``_wizard_failure`` is
#: ``jasper-round``'s when the wizard named no code. An exception class needs no row here:
#: :func:`_classes_a_cli_catches` finds it by its constructor.
_CODE_ARGUMENTS: dict[Callable[..., object], tuple[int | None, tuple[str, ...]]] = {
    **dict.fromkeys((
        evidence_reasons.unavailable, refused_by_name, _prescription_common.refuse, measured_crossover_candidate._refuse,
    ), (0, ())),
    refusal_copy.CrossoverV2Refused: (None, ("code",)),
    _refusal.failed: (1, ("reason", "code")), _refusal.refused: (0, ("reason", "code")),
    round_cli._wizard_failure: (1, ()),
}


def _module_name(path: pathlib.Path) -> str:
    parts = path.relative_to(pathlib.Path(jasper.__file__).parent.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _resolved(node: ast.expr, namespace: dict[str, object]) -> object:
    """What ``node`` names in a module: a literal, a module constant or an attribute chain of
    them. ``None`` for a local or any other expression."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return namespace.get(node.id)
    if isinstance(node, ast.Attribute):
        return getattr(_resolved(node.value, namespace), node.attr, None)
    return None


def _loop_bindings(
    node: ast.AST, parents: dict[ast.AST, ast.AST], namespace: dict[str, object],
) -> list[dict[str, object]]:
    """Each way the ``for`` loops around ``node`` bind their targets, for the loops that iterate what
    the module defines. A loop over a local binds nothing."""
    bindings: list[dict[str, object]] = [{}]
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            try:
                values = list(eval(compile(ast.Expression(node.iter), "<scan>", "eval"), dict(namespace)))
            except NameError:
                continue
            bindings = [{**each, node.target.id: value} for each in bindings for value in values]
    return bindings


def _named_codes(node: ast.expr, namespace: dict[str, object], bindings: list[dict[str, object]]) -> set[str]:
    """The codes ``node`` names: a literal or a module constant, either branch of a conditional, and an
    f-string once for each value its loop takes (``f"{name}_invalid"``)."""
    if isinstance(node, ast.IfExp):
        return _named_codes(node.body, namespace, bindings) | _named_codes(node.orelse, namespace, bindings)
    if isinstance(node, ast.JoinedStr):
        try:
            return {eval(compile(ast.Expression(node), "<scan>", "eval"), {**namespace, **each}) for each in bindings}
        except NameError:
            return set()
    code = _resolved(node, namespace)
    return {code} if isinstance(code, str) else set()


def _code_arguments(call: ast.Call, position: int | None, keywords: tuple[str, ...]) -> list[ast.expr]:
    positional = [] if position is None else call.args[position:position + 1]
    return [*positional, *(each.value for each in call.keywords if each.arg in keywords)]


def _classes_a_cli_catches(
    trees: dict[pathlib.Path, ast.Module], aliases: dict[str, str],
) -> dict[Callable[..., object], tuple[int | None, tuple[str, ...]]]:
    """Each exception class whose constructor takes its code first, as ``reason`` or ``code``, and that a
    ``jasper/cli`` module names in an ``except``: the codes it carries reach a CLI's refusal record."""
    cli = pathlib.Path(jasper.__file__).parent / "cli"
    caught = {getattr(name, "id", getattr(name, "attr", None))
              for path, tree in trees.items() if path.is_relative_to(cli) for handler in ast.walk(tree)
              if isinstance(handler, ast.ExceptHandler) and handler.type is not None
              for name in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type])}
    found: dict[Callable[..., object], tuple[int | None, tuple[str, ...]]] = {}
    for path, tree in trees.items():
        for node in (each for each in ast.walk(tree) if isinstance(each, ast.ClassDef)):
            init = next((each for each in node.body if isinstance(each, ast.FunctionDef) and each.name == "__init__"), None)
            first = init.args.args[1].arg if init and len(init.args.args) > 1 else None
            if first in ("reason", "code") and {node.name, *(alias for alias, name in aliases.items() if name == node.name)} & caught:
                found[getattr(import_module(_module_name(path)), node.name)] = (0, (first,))
    return found


def _codes_raised_by_name() -> dict[str, str]:
    """Each code that a raise site or a gap names outright, and the first place it does."""
    root = pathlib.Path(jasper.__file__).parent
    trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in sorted(root.rglob("*.py"))}
    # ``AlignmentPrescriptionRefused = BlendPrescriptionRefused``: an alias raises the same codes.
    aliases = {target.id: node.value.id for tree in trees.values() for node in tree.body
               if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
               for target in node.targets if isinstance(target, ast.Name)}
    arguments = {**_CODE_ARGUMENTS, **_classes_a_cli_catches(trees, aliases)}
    names = {entry.__name__ for entry in arguments}
    names |= {alias for alias, name in aliases.items() if name in names}
    raised: dict[str, str] = {}
    for path, tree in trees.items():
        calls = [call for call in ast.walk(tree) if isinstance(call, ast.Call)
                 and getattr(call.func, "id", getattr(call.func, "attr", None)) in names]
        if not calls:
            continue
        module = _module_name(path)
        namespace = vars(import_module(module))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for call in calls:
            entry = next((each for each in arguments if _resolved(call.func, namespace) is each), None)
            found = [] if entry is None else _code_arguments(call, *arguments[entry])
            bindings = _loop_bindings(call, parents, namespace) if any(isinstance(each, ast.JoinedStr) for each in found) else [{}]
            for code in {code for argument in found for code in _named_codes(argument, namespace, bindings)}:
                raised.setdefault(code, f"{module}:{call.lineno}")
    return raised


def test_every_code_a_gap_or_refusal_names_has_registry_copy_and_a_next_action():
    """Guards a recurrence: the scan behind #5928 found dozens of codes raised through the gap
    shape, the evidence exception, the prescription refusals, the round bank or a CLI's
    ``failed()`` and ``refused()`` with no registry row, so a reader got a code with neither
    copy nor a next action. A code that reaches a gap or a refusal through a variable is listed
    above. An exception class is found by its constructor when a ``jasper/cli`` module names it in
    an ``except``; a class a CLI reaches only through a base class is not seen."""
    raised = _codes_raised_by_name()
    assert {"gate_sweep_mixed_graphs", evidence_reasons.TAKE_CURVES_NOT_BANKED, "alignment_no_crossover_region",
            "driver_filter_malformed", "prescription_polarity_invalid", "composition_base_required",
            "already_banked", "baseline_config_validation_failed", "walk_refused", "not_root",
            mic_calibration.REFUSE_NONE_REGISTERED, audition.NOT_RESTORED,
            # Each class found by its constructor, and ``jasper-round``'s wizard fallbacks.
            "audition_restore_failed", "authored_status_required", "candidate_malformed", "key_unset",
            "not_downloaded", "round_set_unknown", "run_refused",
            # The candidate's field checks: a literal, a module constant, an f-string over a loop, and a gate.
            "delay_us_invalid", "room_correction_invalid", "linearization_invalid", "tweeter_unprotected",
            } <= set(raised), "the scan no longer reads a literal, a name, an attribute, an alias, a keyword, a class and an f-string"
    lacking = {code: raised.get(code, "forwarded") for code in raised.keys() | _FORWARDED_CODES
               if not ((spec := refusal_copy.REASON_REGISTRY.get(code)) and spec.message and spec.next_action)}
    assert not lacking
