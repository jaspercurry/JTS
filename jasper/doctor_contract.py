# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The jasper-doctor output contract (ADR-0232 rule 3).

The row shape, the status vocabulary, and the harness-generated reason codes
— everything a *consumer* of a doctor report needs. Stdlib-only and free of
any `jasper.cli.doctor` import, so jasper-control can build and count
contract rows without dragging in the check package, whose ``__init__``
imports every domain module.
"""
from __future__ import annotations

from dataclasses import dataclass

CHECK_STATUSES = frozenset({"ok", "warn", "fail", "skipped"})

# Reason codes for rows NO check produced — synthesized by the doctor
# harness or by jasper-control around a report it did not run itself.
REASON_CHECK_CRASHED = "check_crashed"

REASON_CHECK_TIMED_OUT = "check_timed_out"

REASON_NOT_INSTALLED = "not_installed_for_profile"

REASON_CONFIG_ERROR = "config_error"

REASON_DOCTOR_CRASHED = "doctor_crashed"

REASON_SNAPSHOT_PENDING = "snapshot_pending"

REASON_SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"

REASON_REFRESH_FAILED = "refresh_failed"


@dataclass
class CheckResult:
    name: str
    # One of CHECK_STATUSES. Choosing between `ok` and `skipped` for a check
    # that did no work is a decision procedure over what was OBSERVED:
    #   - intentionally off by operator intent (a disabled provider, grouping
    #     off): `ok` with a reason — the off state WAS observed and is the
    #     correct state;
    #   - the role or hardware makes the check inapplicable on this box (a
    #     streambox with no mic, no USB gadget): `skipped`;
    #   - the evidence source is unreachable, so nothing was observed at all
    #     (no systemd, an unreadable statefile): `skipped`.
    # `skipped` counts toward neither fails nor warns and renders dim.
    status: str
    detail: str = ""
    # True when THIS result's meaning is "the speaker emits nothing right
    # now". Only a check that can prove that sets it. Legal on a `warn` or a
    # `fail` only (a result claiming silence while reporting `ok`
    # contradicts itself); it makes the summary LEAD with the silence and
    # changes neither severity nor exit code.
    speaker_silent: bool = False
    # Machine-stable snake_case code naming WHICH branch produced this
    # result — required on every warn/fail, optional on ok/skipped. `detail`
    # is the human sentence and its wording may change freely; `reason` is
    # what tests and other automation pin. Each domain module declares its
    # own closed vocabulary as module-level REASON_* constants.
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in CHECK_STATUSES:
            raise ValueError(f"invalid check status: {self.status!r}")
        if self.status in ("warn", "fail") and not self.reason:
            raise ValueError(f"{self.status} result needs a reason: {self.name!r}")
        if self.speaker_silent and self.status not in ("warn", "fail"):
            raise ValueError(
                f"speaker_silent needs a warn/fail status: {self.name!r}"
            )


def check_row(result: CheckResult) -> dict:
    """One row of the flat /system-diagnostics schema."""
    return {
        "name": result.name,
        "status": result.status,
        "detail": result.detail,
        "reason": result.reason,
        "speaker_silent": result.speaker_silent,
    }


def summarize(results: list[CheckResult]) -> dict:
    """The report-level counts: `fails`, `warns`, `speaker_silent`.

    `speaker_silent` has the same warn/fail-only scope the field itself
    does, which `CheckResult.__post_init__` already enforces."""
    return {
        "fails": sum(1 for r in results if r.status == "fail"),
        "warns": sum(1 for r in results if r.status == "warn"),
        "speaker_silent": any(r.speaker_silent for r in results),
    }
