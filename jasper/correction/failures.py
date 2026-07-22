# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Closed homeowner-facing failure vocabulary for Room correction.

Diagnostic strings stay in logs and evidence.  The Room page receives only
stable codes, bounded copy, retryability, and an optional owner-supplied
recovery action.
"""
from __future__ import annotations

from typing import Any, Mapping

SPEAKER_SETUP_INCOMPLETE = "speaker_setup_incomplete"
SPEAKER_READINESS_UNAVAILABLE = "speaker_readiness_unavailable"
MEASUREMENT_IN_PROGRESS = "measurement_in_progress"
MEASUREMENT_SETUP_INVALID = "measurement_setup_invalid"
SPEAKER_MEASUREMENT_UNSAFE = "speaker_measurement_unsafe"
MICROPHONE_SETUP_UNAVAILABLE = "microphone_setup_unavailable"
PHONE_CAPTURE_UNAVAILABLE = "phone_capture_unavailable"
MEASUREMENT_STOPPED = "measurement_stopped"
TEST_SIGNAL_UNAVAILABLE = "test_signal_unavailable"
MEASUREMENT_ANALYSIS_FAILED = "measurement_analysis_failed"
MEASUREMENT_EVIDENCE_UNSAFE = "measurement_evidence_unsafe"
CORRECTION_UPDATE_FAILED = "correction_update_failed"
CORRECTION_RESTORE_FAILED = "correction_restore_failed"
CORRECTION_AUTO_REVERT_FAILED = "correction_auto_revert_failed"
TUNING_BUSY = "tuning_busy"
TUNING_SPEND_LIMIT = "tuning_spend_limit"
TUNING_UNAVAILABLE = "tuning_unavailable"
TUNING_REQUEST_FAILED = "tuning_request_failed"
TUNING_PROPOSAL_REJECTED = "tuning_proposal_rejected"
UNKNOWN_FAILURE = "unknown_failure"

_FAILURE_COPY: dict[str, tuple[str, bool]] = {
    SPEAKER_SETUP_INCOMPLETE: ("Finish speaker setup first.", False),
    SPEAKER_READINESS_UNAVAILABLE: (
        "Speaker setup could not be checked. Try again.",
        True,
    ),
    MEASUREMENT_IN_PROGRESS: (
        "A measurement is already in progress. Finish or stop it before "
        "starting again.",
        True,
    ),
    MEASUREMENT_SETUP_INVALID: (
        "The measurement setup changed. Review the microphone choices and "
        "try again.",
        True,
    ),
    SPEAKER_MEASUREMENT_UNSAFE: (
        "The speaker is not ready to measure safely. Review speaker setup, "
        "then try again.",
        False,
    ),
    MICROPHONE_SETUP_UNAVAILABLE: (
        "The saved microphone setup is unavailable. Choose the microphone "
        "again.",
        True,
    ),
    PHONE_CAPTURE_UNAVAILABLE: (
        "Phone capture could not be opened. Try again or use this device.",
        True,
    ),
    MEASUREMENT_STOPPED: ("Measurement stopped.", True),
    TEST_SIGNAL_UNAVAILABLE: (
        "The speaker could not play the test sound. Try again.",
        True,
    ),
    MEASUREMENT_ANALYSIS_FAILED: (
        "The speaker could not finish this measurement. Try measuring again.",
        True,
    ),
    MEASUREMENT_EVIDENCE_UNSAFE: (
        "This measurement did not pass its safety checks. Measure again.",
        True,
    ),
    CORRECTION_UPDATE_FAILED: (
        "The correction could not be applied. Check the current correction "
        "before trying again.",
        True,
    ),
    CORRECTION_RESTORE_FAILED: (
        "The previous sound could not be confirmed restored. The correction "
        "may still be applied.",
        True,
    ),
    CORRECTION_AUTO_REVERT_FAILED: (
        "That measured worse, but the correction could not be removed "
        "automatically. It is STILL APPLIED. Use Reset to remove it.",
        True,
    ),
    TUNING_BUSY: (
        "The tuning assistant just ran. Wait a moment, then try again.",
        True,
    ),
    TUNING_SPEND_LIMIT: (
        "The daily assistant budget is reached. Try again after the daily "
        "rollover.",
        False,
    ),
    TUNING_UNAVAILABLE: (
        "The tuning assistant is not set up yet.",
        False,
    ),
    TUNING_REQUEST_FAILED: (
        "The tuning assistant could not continue. Try again.",
        True,
    ),
    TUNING_PROPOSAL_REJECTED: (
        "That suggestion was not applied because it did not pass the "
        "speaker's safety checks.",
        True,
    ),
    UNKNOWN_FAILURE: (
        "The speaker could not continue this step. Try again.",
        True,
    ),
}

FAILURE_CODES = frozenset(_FAILURE_COPY)

ROOM_RETRY_ACTION = {
    "label": "Check again",
    "href": "/correction/room/",
}


def public_failure(
    code: str,
    *,
    recovery_action: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one validated public failure block from the closed catalog."""
    try:
        text, retryable = _FAILURE_COPY[code]
    except KeyError as exc:
        raise ValueError(f"unsupported Room failure code: {code}") from exc
    out: dict[str, Any] = {
        "code": code,
        "text": text,
        "retryable": retryable,
        "recovery_action": None,
    }
    if recovery_action is not None:
        label = str(recovery_action.get("label") or "").strip()
        href = str(recovery_action.get("href") or "").strip()
        if (
            not label
            or not href.startswith("/")
            or href.startswith("//")
            or "\\" in href
            or any(ord(char) < 0x20 for char in href)
        ):
            raise ValueError("invalid Room recovery action")
        out["recovery_action"] = {"label": label, "href": href}
    return out


def session_failure(diagnostic: Any) -> dict[str, Any]:
    """Map a stored diagnostic session error to bounded homeowner copy."""
    message = str(diagnostic or "").strip().casefold()
    if message == "measurement stopped":
        code = MEASUREMENT_STOPPED
    elif message.startswith((
        "sweep generation failed:",
        "sweep playback failed:",
        "repeat sweep playback failed:",
        "verify sweep playback failed:",
    )):
        code = TEST_SIGNAL_UNAVAILABLE
    elif message.startswith((
        "analysis failed:",
        "repeat analysis failed:",
        "verify analysis failed:",
        "peq design failed:",
    )):
        code = MEASUREMENT_ANALYSIS_FAILED
    elif message.startswith((
        "yaml emit failed:",
        "camilladsp reload failed:",
    )):
        code = CORRECTION_UPDATE_FAILED
    elif message.startswith((
        "camilladsp rejected the base config",
        "reset reload failed:",
    )):
        code = CORRECTION_RESTORE_FAILED
    else:
        code = UNKNOWN_FAILURE
    return public_failure(code)


def measurement_evidence_failure(
    confidence_report: Any,
) -> dict[str, Any] | None:
    """Return the typed apply blocker for failed confidence evidence."""
    findings = (
        confidence_report.get("findings")
        if isinstance(confidence_report, Mapping)
        else None
    )
    if not isinstance(findings, list) or not any(
        isinstance(finding, Mapping) and finding.get("severity") == "fail"
        for finding in findings
    ):
        return None
    return public_failure(MEASUREMENT_EVIDENCE_UNSAFE)
