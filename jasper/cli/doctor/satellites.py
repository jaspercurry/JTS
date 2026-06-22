# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — satellites domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

from ._registry import doctor_check
from ._shared import CheckResult

@doctor_check(order=68, group="satellites")
def check_dial_heartbeat() -> CheckResult:
    """Hit jasper-control's /dial/status. The dial firmware doesn't
    send a true periodic heartbeat — `last_seen_at` only updates when
    the user touches the dial (encoder turn, button press) or when
    the dial fires a one-shot dlog line at boot. So a connected-but-
    idle dial is indistinguishable from an offline one. We can only
    flag "never seen since the daemon started"; an old age is expected
    and not a warning."""
    from ...control import client as control
    label = "dial activity"
    try:
        data = control.get_dial_status(timeout=3)
    except (control.ControlError, ValueError) as e:
        return CheckResult(
            label, "warn",
            f"jasper-control /dial/status unreachable: {e}. "
            f"`systemctl status jasper-control`.",
        )
    last_seen_at = data.get("last_seen_at")
    if last_seen_at is None:
        return CheckResult(
            label, "warn",
            "no dial seen since jasper-control started. If you don't "
            "have a dial, ignore. If you do, check that it's on Wi-Fi "
            "and resolving us via mDNS-SD.",
        )
    age = data.get("age_seconds")
    ip = data.get("last_seen_ip")
    return CheckResult(
        label, "ok",
        f"last contact from {ip} {int(age) if age else '<1'}s ago "
        f"(activity, not heartbeat — an idle dial won't show recent age)",
    )
