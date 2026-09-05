# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-doctor`` — importing this package registers every per-domain
check module and re-exports the harness, CLI and contract names its callers
use; a check itself is imported from the module that owns it."""
from __future__ import annotations

import importlib

from ._cli import main, render, render_json
from ._evidence import DOCTOR_CHECK_TIMEOUT_SECONDS
from ._harness import run_async
from ._registry import MODULE_ROSTER, registered_checks
from ._shared import CheckResult, REASON_DOCTOR_CRASHED

__all__ = [
    "CheckResult",
    "DOCTOR_CHECK_TIMEOUT_SECONDS",
    "REASON_DOCTOR_CRASHED",
    "main",
    "registered_checks",
    "render",
    "render_json",
    "run_async",
]

# Importing a domain module registers its checks; MODULE_ROSTER is the one list.
for _name in MODULE_ROSTER:
    importlib.import_module(f".{_name}", __package__)
del _name
