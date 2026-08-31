# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Constants of the cross-session tuning-attempt ledger (#2291 5b-ii).

:mod:`.admission` meters one position's bounded retries within a session;
these values belong to the OTHER meter — the tuning-attempt ledger the S3
loop keeps across sessions. The identity and grading ladders that used to
decide with them live inline at their one call site,
``CrossoverV2Session._grade_verify_attempt``; what stays importable here is
the vocabulary other surfaces read. None of it is household-facing text:
:data:`ATTEMPT_REASON_NO_FLOOR` is a grading status with no
``REASON_REGISTRY`` entry and no copy, and the two thresholds are policy
numbers, not sentences.
"""

from __future__ import annotations

__all__ = [
    "ATTEMPT_REASON_NO_FLOOR",
    "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB",
    "PRESCRIBED_NON_WORSENING_DB",
]


# A grading status, deliberately not a synthetic kernel decision. The kernel
# requires a real FloorStats and the store returns ``None`` until an offline
# repeat study adopts one, so the honest live result is ungraded — no invented
# floor and no improvement claim.
ATTEMPT_REASON_NO_FLOOR = "ungraded_no_floor"

# How much the correction must improve ITS OWN two-branch model before a
# spec-failing prediction is recorded as a material improvement
# (linearization-integrity PR-L4 item 2). Both numbers are the pooled spec
# residual (`flat_spec.spec_convergence_residual`) of the RAW pre-fit and the
# LINEARIZED predicted sum, graded through the identical evaluator, in dB.
#
# **It decides a LEDGER value, not an apply.** Until the nanny burn-down
# (docs/measurement-loop-doctrine.md deviation (c)) falling short of this
# number REFUSED the round at the confirm seam; now it chooses between
# `accountability.LEDGER_IMPROVED` and `LEDGER_NOT_AN_IMPROVEMENT`, and the
# measured round decides what happens next. Every derivation below is
# unchanged by that — the question the number answers is the same one.
#
# It only bites when the prediction ALREADY fails the spec — a prediction
# that meets it needs no improvement argument, and judging an in-spec result on
# "how much did it improve" would read the flattest speakers worst. So the
# question this threshold answers is narrow: *we can already see this will not
# reach spec — is it at least clearly moving the right way?*
#
# 0.5 dB, and the derivation changed with the frame (PR-L4 review B1). While
# this compared the model against the measured in-room cloud, the threshold had
# to absorb the whole cross-frame gap and was set at `SPEC_BANDS[0]`'s 1.5 dB
# for that reason — which, as the review demonstrated, made the verdict a
# function of the ROOM rather than the correction. Now that both terms are the
# same instrument (same branches, same grid, same evaluator, differing ONLY by
# the emitted filters) the comparison carries no measurement noise at all, so
# the threshold is a product-policy floor instead of a noise margin.
#
# 0.5 dB is that floor because it is this model's own measured tracking error:
# `crossover_v2.intervention.plan_linearization` records the complex-correction
# model tracking the real
# VERIFY summation to ~0.5 dB on JTS3 (the zero-phase model it replaced
# mistracked by ~2.0 dB). An improvement smaller than the gap between what we
# model and what the hardware realizes is not an improvement we can honestly
# claim, so it is not recorded as one.
PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5

#: The pre-Apply improvement bar for a candidate carrying PRESCRIBED branches:
#: non-worsening (PR-B, conductor ruling 2026-08-20).
#:
#: Its sibling above — :data:`PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB`, 0.5 dB —
#: is field evidence about the FIT and keeps its original subject untouched.
#: This one exists because that figure is a POOLED-RMS improvement and a
#: per-driver prescription is by construction a narrow high-Q filter aimed at
#: ONE banked feature: 0.077-0.152 dB pooled on realistic fixtures even when it
#: is exactly right, so the fitted bar would file the whole class as no
#: improvement before its first hardware exercise rather than judge it. The
#: reader that chooses between the two is the flow's ``_assert_accountable``,
#: and the gate it hands the chosen number to never branches on either.
#:
#: 0.0 rather than "no bar at all": a model cannot settle whether a narrow cut
#: helps, but it CAN say a proposal is predicted to make the speaker worse, and
#: that is worth writing down. It is a LEDGER boundary, not a stop — neither
#: bar refuses since the nanny burn-down (docs/measurement-loop-doctrine.md
#: deviation (c)) — deciding ``improved`` against ``not_an_improvement``.
PRESCRIBED_NON_WORSENING_DB: float = 0.0
