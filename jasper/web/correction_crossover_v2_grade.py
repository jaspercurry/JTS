# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Crossover session grade vocabulary and grading."""

from __future__ import annotations

from typing import Any, Mapping


#: The vocabulary of ``crossover_v2.post_apply_grade.state``.
GRADE_NOT_APPLIED = "not_applied"
GRADE_UNVERIFIED = "unverified"


def grade_inputs(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """The durable ``state`` fields the status block publishes beside the grade."""
    state = state or {}
    return {"applied": bool(state.get("applied")), "candidate": state.get("candidate")}


def post_apply_grade(
    state: Mapping[str, Any] | None, *, applied_profile: Any, inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The grade the status block publishes as ``post_apply_grade``. ``inputs``
    are :func:`grade_inputs` of ``state``, built here when the caller has not.

    Durable state records no post-apply check, so an applied correction is
    ``unverified``. ``applied_profile`` is accepted for the status block's call
    and not read.
    """
    inputs = grade_inputs(state) if inputs is None else inputs
    return {"state": GRADE_UNVERIFIED if inputs["applied"] else GRADE_NOT_APPLIED}
