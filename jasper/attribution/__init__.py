# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mechanism attribution — naming which physics owns a response feature.

Per ``docs/historical/attribution-stage-plan.md`` (#1866). There is no resident
daemon and no detector yet: attribution runs at analysis time, inside the
process that already owns the analysis, and holds no state between calls.
Findings are optional evidence artifacts — nothing in the deterministic flow is
gated on one existing.

No re-exports here: every caller imports the submodule it needs directly
(``jasper.attribution.findings``, ``jasper.attribution.mechanisms``, etc.).
"""

from __future__ import annotations
