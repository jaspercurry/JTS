# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The measured candidate an applied profile's ``source`` block names."""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["measured_candidate_fingerprint"]


def measured_candidate_fingerprint(source: Any) -> str:
    """The candidate fingerprint a profile's ``source`` block names, or ``""``.

    Absence is ordinary: a profile levelled by the guided captures names none.
    """
    if not isinstance(source, Mapping):
        return ""
    value = source.get("measured_candidate_fingerprint")
    return value if isinstance(value, str) else ""
