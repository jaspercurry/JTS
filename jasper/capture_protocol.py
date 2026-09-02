# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The capture-plan contract, owned by neither side of the capture transport.

A commissioning session decides its walk (``jasper.active_speaker.crossover_v2``)
and a transport carries it (``jasper.capture_relay`` plus ``relay/src/worker.js``
and the capture page). The plan shape and the two ceilings below are what those
two halves must agree on, so they live here rather than inside either — the
relay package is a parked island and no live measurement path may import from
it, while a plan builder still has to know what a plan may legally say.

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

# Hard ceiling on a capture plan's attempt budget. Each admission attempt's
# blob rides its own relay key (capture_index = attempt - 1), so the storable
# blob indexes are EXACTLY 0..MAX_CAPTURE_PLAN_ATTEMPTS-1: the Worker applies
# this same value to indexes with a strict inequality (index >= cap rejected).
# Keep in lockstep with `MAX_CAPTURE_PLAN_ATTEMPTS` in relay/src/worker.js
# (pinned by tests/test_capture_relay_spec.py).
#
# 32 is sized from PR-3b's DECLARED choreography defaults — design inputs from
# docs/historical/linearization-campaign-2026-07.md § PR-3b, not measurements.
# Worst-case ENTRY count at that section's documented maxima:
#     CHECK 1 + MEASURE 1 + (N-1) cloud-measure at max N=12 => 11
#             + M=6 cloud-verify + 2 geometry-retry positions          = 21
# `max_attempts` doubles as the RETRY budget (a retaken capture spends an
# attempt but not a `capture_target` slot), so the cap must cover entries PLUS
# retakes: 21 + 11 = 32, i.e. ~52 % retake headroom over the worst-case entry
# count.
#
# As SHIPPED (PR-3b, N=9 after adjudication 3a): the live FULL-tier cloud plan
# is capture_target=16 against `crossover_v2_flow.cloud_plan_max_attempts()` =
# 23, i.e. ~45 % headroom — tighter than the 167 % the pre-cloud 3-entry plan
# carried, and deliberately so. The express tier (flow-simplification §1.1) is
# a strictly smaller draw on the same ceiling: capture_target=7 against 14.
# Longer sets get proportionally fewer retakes
# each, which is the intended direction: a 21-position session that needs 11
# retakes has a problem retries will not fix. (The 3-entry plan itself still
# exists only as the 1-entry re-verify re-arm, which keeps
# `CAPTURE_PLAN_MAX_ATTEMPTS` = 8.) `repeat_admission` allows 100 %
# (MAX_ATTEMPTS=4 audible vs MAX_RESERVATIONS=8 total reservations).
#
# Two consequences of the raise, named here rather than discovered later:
#   - A session may now authorize 32 blob keys instead of 8, so the storage a
#     leaked upload_token can push inside one TTL rises from 8x to 32x that
#     session's `max_upload_bytes`. The per-blob size cap, the per-session rate
#     limit, and the <=1 h TTL are unchanged and remain the real bounds.
#   - The Worker caps the OPAQUE spec at `MAX_SPEC_BYTES` (64 KiB), so a
#     32-entry plan has ~2 KiB of spec budget per entry. That is far above the
#     product prompt copy PR-3b emits (a title + body) but BELOW the per-entry
#     `MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES` ceiling of 4 KiB, so a plan that
#     spent the full per-entry screen ceiling on every entry would be refused
#     by the relay at registration (413 `capture_spec_too_large`). The
#     product-sized regime is pinned by tests/test_capture_relay_spec.py.
MAX_CAPTURE_PLAN_ATTEMPTS = 32

# The longest link the relay Worker grants (``MAX_TTL_S`` in
# relay/src/worker.js, pinned in lockstep by tests/test_capture_relay_session.py).
#
# The Worker CLAMPS an over-large request rather than refusing it, so a caller
# that asks for more is not an error there — it is a silent disagreement here.
# ``open_capture`` publishes the requested ``ttl_s`` to the phone as
# ``time_budget.session_s``, which is the number the household is told the link
# lives for; a request the Worker quietly cut back would make that disclosure a
# lie. So a caller sizing a TTL from its own budget clamps against this, and the
# published number stays the granted one. It is a mirror of a separately
# released artifact, exactly like ``LEGACY_MAX_CAPTURE_PLAN_ATTEMPTS``, not a
# knob to tune from this side.
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
    each admitted attempt's blob rides its own relay key
    (``capture_index = attempt - 1``).

    - ``capture_target`` — accepted captures required to finish the set
      (e.g. 3 driver repeats).
    - ``max_attempts`` — total admission attempts the set may consume,
      including rejected/retried ones (e.g. 4). Bounded by
      ``MAX_CAPTURE_PLAN_ATTEMPTS`` so a plan can never authorize a blob index
      past the Worker's key ceiling.
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
