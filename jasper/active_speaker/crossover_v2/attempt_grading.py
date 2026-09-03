# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Constants of the cross-session tuning-attempt ledger (#2291 5b-ii).

:mod:`.admission` meters one position's bounded retries within a session; these
belong to the other meter, the ledger the S3 loop keeps across sessions.
"""

from __future__ import annotations

__all__ = [
    "ATTEMPT_REASON_NO_FLOOR",
    "PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB",
    "PRESCRIBED_NON_WORSENING_DB",
]


# A grading status, not a refusal: no ``REASON_REGISTRY`` entry, no copy.
ATTEMPT_REASON_NO_FLOOR = "ungraded_no_floor"

# dB of pooled spec residual (`flat_spec.spec_convergence_residual`), RAW
# pre-fit against LINEARIZED predicted sum; 0.5 is the model's own measured
# VERIFY tracking error on JTS3. A ledger boundary, never a refusal. See
# ADR-0227 #4.
PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5

#: dB. Non-worsening bar for PRESCRIBED branches; a ledger boundary between
#: ``improved`` and ``not_an_improvement``, never a refusal. See ADR-0227 #4.
PRESCRIBED_NON_WORSENING_DB: float = 0.0
