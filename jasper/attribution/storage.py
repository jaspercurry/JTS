# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Where findings live: one ordinary artifact in the session bundle.

Bundle-lifetime retention (Q-C, #1866), implemented by choosing the container
rather than writing a retention loop: ``bundles.enforce_retention`` evicts the
bundle whole, findings and the evidence they cite together.
"""

from __future__ import annotations


class FindingStorageError(RuntimeError):
    """A finding set's publish path could not be formed."""


def findings_relative_path(capture_session_id: str, phase: str) -> str:
    """The **publish** path for one phase's finding set.

    Per phase, not per session: the pre-apply and post-apply groups close at
    different times and the store is write-once, so a shared path would
    collide. ``publish_json_artifact`` takes this SHORT path and prefixes it
    with its artifact namespace itself.
    """

    if not capture_session_id or not phase:
        raise FindingStorageError("capture_session_id and phase are required")
    return f"crossover_v2/{capture_session_id}/findings_{phase}.json"


__all__ = [
    "FindingStorageError",
    "findings_relative_path",
]
