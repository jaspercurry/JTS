# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-doctor`` — re-exports the harness, CLI and contract names its
callers use; a check itself is imported from the module that owns it.

Importing this package deliberately does NOT import the per-domain check
modules: ``registered_checks`` imports the ones the requested scope needs,
so a ``--core`` run pays for ``CORE_MODULES`` alone (ADR-0233 rule 5)."""
from __future__ import annotations

from ._cli import main, render, render_json
from ._evidence import DOCTOR_CHECK_TIMEOUT_SECONDS
from ._harness import run_async
from ._registry import CORE_MODULES, MODULE_ROSTER, registered_checks
from ._shared import CheckResult, REASON_DOCTOR_CRASHED

__all__ = [
    "CORE_MODULES",
    "CheckResult",
    "DOCTOR_CHECK_TIMEOUT_SECONDS",
    "MODULE_ROSTER",
    "REASON_DOCTOR_CRASHED",
    "main",
    "registered_checks",
    "render",
    "render_json",
    "run_async",
]
