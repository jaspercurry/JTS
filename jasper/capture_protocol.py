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

The rest of the wire contract — ``CaptureSpec`` itself and its validation —
lives in ``jasper.active_speaker.crossover_v2.sweep_spec``, which imports these
names back so there is one definition of each.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Metadata/index budget; WAVs are stored per take. Supports 11 poses × 3 candidates × 3 levels.
MAX_CAPTURE_PLAN_ATTEMPTS = 128

# Sanity ceiling for a capture-session timeout budget — the longest a
# session should reasonably run. Used as the upper clamp for
# ``v2_first_begin_timeout_s`` and by any caller sizing a timeout against a
# session's own budget.
MAX_TTL_S = 3600


class CaptureSpecError(ValueError):
    """A capture spec violated the contract. Raised loudly at the boundary."""


@dataclass(frozen=True)
class CapturePlanEntry:
    """One capture's identity/timing/copy inside a heterogeneous v3 plan.

    Wave 3 (crossover-measurement-productization-design.md §5.7) extends the
    session-spanning ``CapturePlan`` from "N repeats of ONE spec" to N
    captures that may each be a DIFFERENT kind of measurement (e.g. a
    session-model CHECK -> MEASURE -> VERIFY sequence) inside the SAME
    transport session.

    ``index`` is 0-based (``0..capture_target-1``) — deliberately distinct
    from the wire protocol's 1-based ``begin_capture.index`` (SPEC W2.3).

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
      Opaque: the schema bounds size and value types, never the keys — the
      capture page decides what to render.
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
