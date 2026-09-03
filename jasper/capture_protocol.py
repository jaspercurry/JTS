# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The capture-plan contract, owned by neither side of the capture driver.

A commissioning session decides its walk (``jasper.active_speaker.crossover_v2``)
and the wired driver carries it
(``jasper.web.correction_crossover_v2_wired``). The plan shape and the two
ceilings below are what those two halves must agree on, so they live here
rather than inside either.

Stdlib-only on purpose: the socket-activated wizard builds specs on a light
process, and both sides import this unconditionally.

The rest of the wire contract — ``CaptureSpec`` itself, its validation and its
consent-surface vocabulary — lives in
``jasper.active_speaker.crossover_v2.sweep_spec``, which imports these names
back so there is one definition of each.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Hard sanity ceiling on a capture plan's attempt budget (entries plus
# retakes). ``CapturePlan.max_attempts`` validation
# (``jasper.active_speaker.crossover_v2.sweep_spec``,
# ``jasper.active_speaker.angle_capture``) enforces it at build time, so a
# plan asking for more is refused before any tone plays. 32 covers the
# worst-case cloud-plan shape with headroom to spare — see
# ``jasper.active_speaker.crossover_v2.capture_plan``'s
# ``MAX_CLOUD_MEASURE_POSITIONS`` for the derivation.
MAX_CAPTURE_PLAN_ATTEMPTS = 32

# Sanity ceiling for a capture-session timeout budget — the longest a
# session should reasonably run. Used as the upper clamp for
# ``v2_first_begin_timeout_s`` and by any caller sizing a timeout against a
# session's own budget.
MAX_TTL_S = 3600

CAPTURE_PLAN_KEYS = ("schema_version", "capture_target", "max_attempts", "entries")
CAPTURE_PLAN_ENTRY_KEYS = ("index", "kind_label", "duration_ms", "screen")

# Per-capture allowance the DISPLAYED session-duration estimate adds on top of
# each entry's own ``duration_ms`` — reading the prompt, moving the mic, and
# tapping. It MIRRORS the capture page's ``WAKE_LOCK_PER_CAPTURE_OVERHEAD_MS``
# (``capture-page/js/main.js``), which is the surface a household actually
# reads the number on; server-side consent copy derives from the same value so
# the two cannot quote different durations for one session. Deliberately
# generous — this is a display promise, not a measurement. Drift between the
# two constants is pinned by test.
CAPTURE_PLAN_PER_CAPTURE_OVERHEAD_MS = 20_000


class CaptureSpecError(ValueError):
    """A capture spec violated the contract. Raised loudly at the boundary."""


def as_int(data: Mapping[str, Any], key: str, *, default: int | None = None) -> int:
    if key not in data or data.get(key) is None:
        if default is None:
            raise CaptureSpecError(f"{key} is required")
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise CaptureSpecError(f"{key} must be an integer, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class CapturePlanEntry:
    """One capture's identity/timing/copy inside a heterogeneous v3 plan.

    Wave 3 (crossover-measurement-productization-design.md §5.7) extends the
    session-spanning ``CapturePlan`` from "N repeats of ONE spec" to N
    captures that may each be a DIFFERENT kind of measurement (e.g. a
    session-model CHECK -> MEASURE -> VERIFY sequence) inside the SAME
    transport session.

    ``index`` is 0-based (``0..capture_target-1``) — deliberately distinct
    from the wire protocol's 1-based ``begin_capture.index`` (SPEC W2.3);
    :meth:`CapturePlan.entry_for_index` does that 1-based -> 0-based lookup
    so callers never repeat the arithmetic.

    - ``kind_label`` — a short slug naming what this capture measures (e.g.
      ``"check"`` / ``"measure"`` / ``"verify"``). Display/telemetry only,
      like ``CaptureStimulus.label`` — never trusted for logic.
    - ``duration_ms`` — THIS capture's DECLARED acoustic length (the design
      doc's CHECK ~25s / MEASURE ~20s / VERIFY ~15s can differ per index).
      Presentation + analysis data — phone-side progress/countdown copy and
      the analysis side's per-entry locator windows (design §5.7) — NEVER a
      hard deadline: the session runner's recording+upload backstop stays
      its own session-level ``timeout_s`` for every plan, entries or not.
    - ``screen`` — optional phone-side prompt copy for this capture (a
      string-to-string mapping such as ``{"title": ..., "body": ...}``).
      Opaque like ``presentation_variant``: the schema bounds size and value
      types, never the keys — the capture page decides what to render.
    """

    index: int
    kind_label: str
    duration_ms: int
    screen: Mapping[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "kind_label": self.kind_label,
            "duration_ms": self.duration_ms,
        }
        if self.screen is not None:
            data["screen"] = dict(self.screen)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CapturePlanEntry:
        if not isinstance(data, Mapping):
            raise CaptureSpecError("capture_plan.entries[] must be an object")
        extra = set(data) - set(CAPTURE_PLAN_ENTRY_KEYS)
        if extra:
            raise CaptureSpecError(
                f"capture_plan.entries[] has unknown keys: {sorted(extra)}"
            )
        screen_raw = data.get("screen")
        if screen_raw is not None and not isinstance(screen_raw, Mapping):
            raise CaptureSpecError(
                "capture_plan.entries[].screen must be an object or null"
            )
        return cls(
            index=as_int(data, "index"),
            kind_label=str(data.get("kind_label") or ""),
            duration_ms=as_int(data, "duration_ms"),
            screen=(
                {str(k): str(v) for k, v in screen_raw.items()}
                if isinstance(screen_raw, Mapping)
                else None
            ),
        )


@dataclass(frozen=True)
class CapturePlan:
    """Session-spanning capture plan (SPEC W2.3).

    One transport session covers a driver's whole repeat SET instead of one
    capture per session: the capture source requests each capture with an
    authenticated ``begin_capture {index, attempt}`` event, the Pi admits it
    (budget stays Pi-owned — ``repeat_admission`` — never source-decided), and
    each admitted attempt indexes its own recording
    (``capture_index = attempt - 1``).

    - ``capture_target`` — accepted captures required to finish the set
      (e.g. 3 driver repeats).
    - ``max_attempts`` — total admission attempts the set may consume,
      including rejected/retried ones (e.g. 4). Bounded by
      ``MAX_CAPTURE_PLAN_ATTEMPTS`` so a plan can never authorize an attempt
      index past the sanity ceiling.
    - ``entries`` (schema_version 2, additive) — one ``CapturePlanEntry`` per
      capture index for a HETEROGENEOUS plan (§5.7). ``None``
      (schema_version 1) is the pre-entries shape: "N repeats of ONE spec",
      byte-identical to the original v3 contract.

    Carried as DATA in the spec so a single spec drives both sides."""

    capture_target: int
    max_attempts: int
    schema_version: int = 1
    entries: tuple[CapturePlanEntry, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "capture_target": self.capture_target,
            "max_attempts": self.max_attempts,
        }
        if self.entries is not None:
            data["entries"] = [entry.to_dict() for entry in self.entries]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CapturePlan:
        if not isinstance(data, Mapping):
            raise CaptureSpecError("capture_plan must be an object")
        extra = set(data) - set(CAPTURE_PLAN_KEYS)
        if extra:
            raise CaptureSpecError(
                f"capture_plan has unknown keys: {sorted(extra)}"
            )
        entries_raw = data.get("entries")
        entries: tuple[CapturePlanEntry, ...] | None = None
        if entries_raw is not None:
            if not isinstance(entries_raw, Sequence) or isinstance(
                entries_raw, (str, bytes)
            ):
                raise CaptureSpecError("capture_plan.entries must be a list")
            entries = tuple(
                CapturePlanEntry.from_dict(item) for item in entries_raw
            )
        return cls(
            capture_target=as_int(data, "capture_target"),
            max_attempts=as_int(data, "max_attempts"),
            schema_version=as_int(data, "schema_version", default=1),
            entries=entries,
        )

    def estimated_minutes(self) -> int:
        """Whole minutes this plan is DISPLAYED as taking, ``0`` when unknown.

        Deliberately the phone's own arithmetic, not a second estimate: the
        capture page already shows this number in its wake-lock fallback hint
        (``capture-page/js/main.js``'s ``wakeLockHintText`` — every entry's
        ``duration_ms`` plus :data:`CAPTURE_PLAN_PER_CAPTURE_OVERHEAD_MS` of
        allowance, ``ceil`` to minutes, floored at 1). Server-side consent copy
        that quotes a duration MUST derive it here rather than hand-writing a
        prettier figure, or the household reads two different promises about
        the same session (flow-simplification §1.1).

        This is a conservative DISPLAY number — audio plus a generous
        per-capture allowance for reading a prompt, moving the mic, and
        tapping — never a measurement of real wall clock.
        """
        if not self.entries:
            return 0
        total_ms = sum(
            int(entry.duration_ms) + CAPTURE_PLAN_PER_CAPTURE_OVERHEAD_MS
            for entry in self.entries
        )
        return max(1, -(-total_ms // 60_000))

    def entry_for_index(self, index: int) -> CapturePlanEntry | None:
        """The entry for a 1-based ``begin_capture.index`` (SPEC W2.3).

        ``entries`` is keyed 0-based; the wire protocol is 1-based. Returns
        ``None`` for a plan with no entry table (schema_version 1) or —
        never reachable once ``validate()`` has run on the owning spec — an
        index with no match."""
        if self.entries is None:
            return None
        for entry in self.entries:
            if entry.index == index - 1:
                return entry
        return None
