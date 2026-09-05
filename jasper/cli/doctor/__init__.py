# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-doctor`` — importing this package registers every per-domain
check module and re-exports the harness, CLI and contract names its callers
use; a check itself is imported from the module that owns it."""
from __future__ import annotations

import importlib

from ._cli import (
    _doctor_config_from_env,
    _error_payload,
    _json_payload,
    main,
    render,
    render_json,
)
from ._evidence import DOCTOR_CHECK_TIMEOUT_SECONDS
from ._harness import (
    _RunnableDoctorCheck,
    _build_doctor_checks,
    _doctor_skip_detail,
    _profile_skip_result,
    _run_runnable_with_timeout,
    run_async,
)
from ._registry import MODULE_ROSTER, registered_checks
from ._shared import (
    CheckResult,
    REASON_DOCTOR_CRASHED,
    _check_name,
    _run_async_doctor_check,
    _run_doctor_check,
)

__all__ = [
    "CheckResult",
    "DOCTOR_CHECK_TIMEOUT_SECONDS",
    "REASON_DOCTOR_CRASHED",
    "_RunnableDoctorCheck",
    "_build_doctor_checks",
    "_check_name",
    "_doctor_config_from_env",
    "_doctor_skip_detail",
    "_error_payload",
    "_json_payload",
    "_profile_skip_result",
    "_run_async_doctor_check",
    "_run_doctor_check",
    "_run_runnable_with_timeout",
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
