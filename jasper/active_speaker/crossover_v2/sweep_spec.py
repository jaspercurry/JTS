# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The validated capture spec a commissioning capture session opens on.

A spec carries the session-spanning
:class:`~jasper.playback_state.capture_protocol.CapturePlan` the wired walk
follows. It is validated strictly and loudly at the boundary, and re-validated
at session open before a tone can play. The plan shape itself is owned by
:mod:`jasper.playback_state.capture_protocol`.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from jasper.playback_state.capture_protocol import (
    MAX_CAPTURE_PLAN_ATTEMPTS,
    CapturePlan,
    CapturePlanEntry,
    CaptureSpecError,
)


@dataclass(frozen=True)
class DefaultSetupCalibration:
    """A household's remembered measurement-mic calibration, as an OPTIONAL
    hint — never binding.

    ``mode`` is how the ORIGINAL calibration was established (``"serial"`` or
    ``"upload"``). There is no ``"none"``: a household record is only written
    after a calibration successfully established, so the hint is either present
    and actionable or absent entirely.

    ``resolvable`` is a SEPARATE, freshly-checked flag from the fact that the
    hint exists: ``calibration_id`` is re-resolved against the calibration store
    at hint-build time and the flag is set only when THAT resolves cleanly,
    rather than trusting the earlier resolve that built the other fields.
    """

    mode: str
    model: str = ""
    serial_display: str = ""
    calibration_id: str = ""
    resolvable: bool = False


# schema_version 1 is the pre-entries shape; 2 is additive (per-capture
# heterogeneity). A plan's schema_version and its `entries` presence are kept in
# strict lockstep by `_validate_capture_plan_entries`, so a reader never has to
# re-derive one from the other.
CAPTURE_PLAN_SCHEMA_VERSIONS = (1, 2)
CAPTURE_PLAN_ENTRIES_SCHEMA_VERSION = 2
# Per-entry presentation copy is OPAQUE — the schema bounds its size and
# value types, never its keys/vocabulary — but a size ceiling keeps a spec
# from carrying an oversized payload.
MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES = 4096


# --- The spec -----------------------------------------------------------------


@dataclass(frozen=True)
class CaptureSpec:
    """The capture plan one commissioning session walks."""

    capture_plan: CapturePlan

    def validate(self) -> CaptureSpec:
        """Strict, loud validation. Returns self so callers can chain."""
        _validate_capture_plan(self.capture_plan)
        return self


# --- Validation helpers -------------------------------------------------------


def _validate_capture_plan(capture_plan: CapturePlan) -> None:
    if capture_plan.schema_version not in CAPTURE_PLAN_SCHEMA_VERSIONS:
        raise CaptureSpecError(
            "capture_plan.schema_version must be one of "
            f"{CAPTURE_PLAN_SCHEMA_VERSIONS}"
        )
    for name, value in (
        ("capture_target", capture_plan.capture_target),
        ("max_attempts", capture_plan.max_attempts),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise CaptureSpecError(f"capture_plan.{name} must be an integer")
    if not 1 <= capture_plan.capture_target <= capture_plan.max_attempts:
        raise CaptureSpecError(
            "capture_plan.capture_target must be in 1..max_attempts"
        )
    if capture_plan.max_attempts > MAX_CAPTURE_PLAN_ATTEMPTS:
        raise CaptureSpecError(
            f"capture_plan.max_attempts must be <= {MAX_CAPTURE_PLAN_ATTEMPTS}"
        )
    _validate_capture_plan_entries(capture_plan)


def _validate_capture_plan_entries(capture_plan: CapturePlan) -> None:
    """Reciprocal contract: schema_version 2 <=> entries present.

    A plan that carries entries must cover every index ``0..capture_target-1``
    exactly once — contiguous, unique — so the session runner can always resolve
    "the entry for capture N" with no gaps.
    """
    entries = capture_plan.entries
    if entries is None:
        if capture_plan.schema_version >= CAPTURE_PLAN_ENTRIES_SCHEMA_VERSION:
            raise CaptureSpecError(
                f"capture_plan.schema_version {CAPTURE_PLAN_ENTRIES_SCHEMA_VERSION} "
                "requires entries"
            )
        return
    if capture_plan.schema_version < CAPTURE_PLAN_ENTRIES_SCHEMA_VERSION:
        raise CaptureSpecError(
            "capture_plan.entries requires capture_plan.schema_version >= "
            f"{CAPTURE_PLAN_ENTRIES_SCHEMA_VERSION}"
        )
    if not isinstance(entries, tuple):
        raise CaptureSpecError("capture_plan.entries must be a tuple")
    seen_indexes: set[int] = set()
    for position, entry in enumerate(entries):
        if not isinstance(entry, CapturePlanEntry):
            raise CaptureSpecError(
                f"capture_plan.entries[{position}] must be a CapturePlanEntry"
            )
        if isinstance(entry.index, bool) or not isinstance(entry.index, int):
            raise CaptureSpecError(
                f"capture_plan.entries[{position}].index must be an integer"
            )
        if entry.index in seen_indexes:
            raise CaptureSpecError(
                f"duplicate capture_plan.entries index: {entry.index}"
            )
        seen_indexes.add(entry.index)
        _validate_capture_plan_entry_screen(entry.screen, position)
    if seen_indexes != set(range(capture_plan.capture_target)):
        raise CaptureSpecError(
            "capture_plan.entries must cover indexes 0..capture_target-1 "
            "exactly, contiguous and unique"
        )


def _validate_capture_plan_entry_screen(
    screen: Mapping[str, str] | None, position: int
) -> None:
    if screen is None:
        return
    if not isinstance(screen, Mapping):
        raise CaptureSpecError(
            f"capture_plan.entries[{position}].screen must be an object or null"
        )
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in screen.items()
    ):
        raise CaptureSpecError(
            f"capture_plan.entries[{position}].screen must map strings to strings"
        )
    if (
        len(json.dumps(screen, separators=(",", ":")))
        > MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES
    ):
        raise CaptureSpecError(
            f"capture_plan.entries[{position}].screen exceeds "
            f"{MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES} bytes"
        )
