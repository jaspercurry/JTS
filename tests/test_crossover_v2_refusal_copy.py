# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""#2291 Phase 5c-ii: the moved names resolve to ONE object, not a copy.

The slice was a pure relocation absorbed by a re-export, so the thing that can
silently go wrong is not a missing name — an importer would crash and CI would
say so — but a *second definition*: the flow keeping its own copy of a constant
the package now owns, or a later edit re-introducing one. Two copies of
``REASON_REGISTRY`` that agree today and drift in six months is precisely the
single-source-of-truth failure this campaign exists to remove, and no ordinary
test would notice, because both copies would answer every question correctly
until the day one of them changed.

So these assert IDENTITY (``is``), never equality. Equality passes with two
copies; identity is what says there is one.

Wave 0c closed the refusal_copy door for every name the flow does not read
itself: those consumers now import from the owner, so the flow has no binding
at all. That is the same guarantee one step stronger — one definition reached
ONE way — and it is pinned by absence rather than by identity. The names it
applies to are in :data:`NO_LONGER_REACHED_THROUGH_THE_FLOW`.

The rosters below are written out rather than derived from the modules they
check. A guard that asks the source it is guarding what to guard proves nothing.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from jasper.active_speaker import crossover_v2_flow as flow
from jasper.active_speaker.crossover_v2 import (
    attempt_grading,
    capture_dispatch,
    refusal_copy,
    spatial,
)

#: Every name #2291 Phase 5c-ii moved out of the flow, by the module that owns
#: it now. The flow re-exports the ones it still reads itself; the rest are
#: listed in :data:`NO_LONGER_REACHED_THROUGH_THE_FLOW` and are reachable only
#: from the owner. This roster stays COMPLETE either way — the re-declaration
#: guard at the bottom of this file checks all of it.
MOVED_NAMES: dict[str, tuple[str, ...]] = {
    "refusal_copy": (
        "DELTA_PROBE_REASON_BY_VERDICT",
        "NON_RETRIABLE_CODES",
        "PhaseVerdict",
        "REASON_AGC_BEHAVIORAL_FAIL",
        "REASON_ANCHOR_AMBIGUOUS",
        "REASON_APPLY_FAILED",
        "REASON_CHANNEL_MAP_MISMATCH",
        "REASON_CLIPPED",
        "REASON_CLOUD_GEOMETRY_LOCKED",
        "REASON_CORRECTION_LEVEL_SHORTFALL",
        "REASON_CORRECTION_MEASURED_REGRESSION",
        "REASON_CORRECTION_MODEL_ERROR",
        "REASON_CORRECTION_ROLLBACK_FAILED",
        "REASON_CORRECTION_SPATIALLY_COSTLY",
        "REASON_CORRECTION_UNPROVEN_BOOST",
        "REASON_DELAY_EXCEEDS_SEARCH_WINDOW",
        "REASON_DELAY_IMPLAUSIBLE",
        "REASON_DRIFT_BASELINES_DISAGREE",
        "REASON_INTERNAL_ERROR",
        "REASON_LOCATE_FAILED",
        "REASON_NOISY_ROOM_LINEARITY",
        "REASON_PILOT_LEVEL_COLLAPSE",
        "REASON_PROGRAM_PROFILE_INCOMPLETE",
        "REASON_PROGRAM_PROFILE_MISSING",
        "REASON_PROGRAM_PROFILE_NOT_CONFIRMED",
        "REASON_PROGRAM_UNPLAYABLE",
        "REASON_PROTECTION_NOT_SEPARABLE",
        "REASON_PROTECTION_SWEEP_TOO_LOW",
        "REASON_REGISTRY",
        "REASON_CAPTURE_TIMEOUT",
        "REASON_REVIEW_HOLD_TIMEOUT",
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
        "correction_rollback_failed_message",
        "locate_failed_diagnosis",
        "locate_failed_message",
        "reason_diagnosis",
        "reason_message",
        "round_restore_reason",
        "verify_inconclusive_cause",
        "verify_inconclusive_diagnosis",
        "verify_inconclusive_message",
    ),
    "spatial": (
        "CLOUD_CLOSE_AWAITING_CONFIRM",
        "CLOUD_CLOSE_NONE",
        "CLOUD_CLOSE_RUNNING",
        "GEOMETRY_RETRY_POSITIONS",
    ),
    "attempt_grading": (
        "ATTEMPT_REASON_NO_FLOOR",
        "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB",
    ),
    "capture_dispatch": (
        "SWEEP_LOCATE_CONFIDENCE_FLOOR",
        "SWEEP_SCHEDULE_RESIDUAL_CEILING_MS",
        "_gate_disclosure",
        "_gate_floor_source",
        "_gate_record",
        "_gate_window_ms",
        "_pilot_by_role",
        "_pilot_diag_fields",
        "_pilot_transfer_by_role",
        "_sweep_locate_confidence_ok",
        "_sweep_schedule_diag_fields",
        "_sweep_schedule_ok",
    ),
}

#: The refusal_copy names the flow still reads ITSELF, and therefore still
#: re-exports. Wave 0c pointed every other consumer at the owner, so for the
#: rest of the roster the flow has no binding at all — a STRONGER single-source
#: guarantee than a re-export, because one definition is reached ONE way and
#: there is nothing that could drift.
#:
#: Written out for the same reason ``MOVED_NAMES`` is: a guard that asks the
#: flow which names it happens to import proves nothing. The absent set is
#: derived from these two hand-kept rosters and never from the flow, so a name
#: that quietly stops being re-exported fails the identity test, and one that
#: quietly comes back fails the absence test.
STILL_READ_BY_THE_FLOW: frozenset[str] = frozenset({
    "NON_RETRIABLE_CODES",
    "PhaseVerdict",
    "REASON_CLOUD_GEOMETRY_LOCKED",
    "REASON_CORRECTION_ROLLBACK_FAILED",
    "REASON_LOCATE_FAILED",
    "REASON_REGISTRY",
    "REASON_VERIFY_INCONCLUSIVE",
    "REASON_VERIFY_LEVEL_SHIFT",
    "REASON_VERIFY_OUT_OF_TOLERANCE",
    "_screen_refusal_code",
    "reason_diagnosis",
    "reason_message",
    "round_restore_reason",
})

#: The complement, over the door wave 0c closed.
NO_LONGER_REACHED_THROUGH_THE_FLOW: tuple[str, ...] = tuple(
    s for s in MOVED_NAMES["refusal_copy"] if s not in STILL_READ_BY_THE_FLOW
)

_OWNERS = {
    "refusal_copy": refusal_copy,
    "spatial": spatial,
    "attempt_grading": attempt_grading,
    "capture_dispatch": capture_dispatch,
}


@pytest.mark.parametrize(
    ("owner_name", "symbol"),
    [
        (o, s)
        for o, names in MOVED_NAMES.items()
        for s in names
        if s not in NO_LONGER_REACHED_THROUGH_THE_FLOW
    ],
)
def test_the_flow_re_export_is_the_owning_module_s_object(owner_name, symbol):
    """``flow.NAME is owner.NAME`` — one definition, reached two ways."""

    owner = _OWNERS[owner_name]
    assert hasattr(owner, symbol), f"{owner_name} lost {symbol}"
    assert hasattr(flow, symbol), f"the flow stopped re-exporting {symbol}"
    assert getattr(flow, symbol) is getattr(owner, symbol), (
        f"flow.{symbol} is a COPY of {owner_name}.{symbol}, not the same object"
    )


def test_the_flow_does_not_reach_the_names_its_consumers_import_directly():
    """One definition reached ONE way — the door is gone, not merely unused.

    The owner still holds every name; the flow has no binding for them, so the
    second copy this suite exists to prevent has nowhere to appear.
    """

    lost = [s for s in NO_LONGER_REACHED_THROUGH_THE_FLOW
            if not hasattr(refusal_copy, s)]
    assert lost == [], f"refusal_copy lost {lost}"

    reopened = [s for s in NO_LONGER_REACHED_THROUGH_THE_FLOW if hasattr(flow, s)]
    assert reopened == [], (
        f"the flow reaches {reopened} again — either repoint the consumer that "
        "needed it, or move the name back to the identity roster above"
    )


def test_the_flow_defines_none_of_the_moved_names_itself():
    """The re-export is the flow's only binding for each moved name.

    ``flow.X is owner.X`` above would still hold for one tick if someone added a
    module-level ``X = owner.X`` assignment, and would keep holding until the
    owner's value changed. This reads the flow's SOURCE instead of its runtime
    namespace: no ``def``/``class``/assignment may re-declare a moved name. The
    only permitted binding is the ``from ... import X as X`` door.

    Walked at ANY nesting depth, not just module level (#2291 Phase 5c-iii,
    closing the #2391 gate's note N3). The traversal was ``tree.body`` while
    this docstring already promised "no ``def``/``class``/assignment" without
    qualification. No coverage gap existed — the identity test above catches a
    nested re-declaration the moment it shadows, which the gate proved with a
    planted nested shape — so this aligns the guard to its own stated claim
    rather than fixing a hole.
    """

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
