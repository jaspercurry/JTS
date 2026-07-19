# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The kind-agnostic capture-spec contract (phone-mic relay, build step 1).

The Pi sends an **opaque** JSON `capture_spec` to the relay; the relay stores it
without parsing it; the static capture page interprets it and renders the UI it
describes as DATA. This module owns that contract on the Pi side:

  - the frozen `CaptureSpec` dataclass and its `to_dict()` / `from_dict()`,
  - strict, loud validation at the boundary (a malformed spec fails fast — no
    Postel-style liberality, per the extensibility doctrine),
  - the small allowlists that the page renderer mirrors so that **server-driven
    UI is data, not code** (theme tokens, component types, button actions), and
  - `build_room_sweep_spec(...)`, the first per-kind builder.

Two boundaries are load-bearing and tested:

  1. **Kind-agnostic.** `kind` is an open string. The schema validates every
     other field but never enumerates kinds, so a brand-new kind that fills the
     same fields validates with zero schema changes (and the relay, which never
     parses the spec at all, needs none either). See `docs/phone-mic-relay-plan`
     §6 + §15.
  2. **UI is an allowlisted token vocabulary, not markup.** `theme` carries
     *tokens* (e.g. ``accent="sage"``) that the page maps to fixed CSS-variable
     values — never raw CSS. `screen` is a list of known component types with
     escaped text. The page is the real enforcer (it holds the mic + E2E key and
     must not trust the spec, which crosses the untrusted relay), but the Pi
     refuses to *emit* anything outside the vocabulary so a bug never ships a
     payload the page would have to reject. See plan §8.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

# --- Contract constants -------------------------------------------------------

SCHEMA_VERSION = 1
# Phone page/Pi behavior protocol. This is independent of CaptureSpec's JSON
# schema: additive spec fields may remain schema-compatible while choreography
# changes (for example, setup binding or a level stream) require a matching
# public page before the Pi is allowed to play a tone.
#
# Protocol 3 (SPEC W2.3) is the session-spanning capture protocol: one relay
# session covers a driver's whole repeat SET, choreographed by a `capture_plan`
# (below). The Pi validates and runs v3 sessions; `build_crossover_sweep_spec`
# itself stays dormant-by-default (`capture_plan=None`), but the Wave-2 Pi
# host path (`jasper/web/correction_setup.py`'s driver-sweep relay-capture
# handler) now DOES pass one unconditionally in code — no env gate. The
# Wave-2 capture page (capture-page/js/main.js) implements the v3 loop and
# advertises protocol 3, so once the Worker and page are DEPLOYED, a Pi
# build carrying the host flip goes live for real; against an older
# deployed page the v3 spec fails the page-identity check loudly before any
# tone can play. The coordinator's deploy sequencing (worker → page publish
# → the Pi host flip last) is the rollout gate — there is no code-level
# flag. Summed/verification/level_ramp builders are untouched and stay on
# protocol 1/2.
CAPTURE_PROTOCOL_VERSION = 1
SESSION_SPANNING_CAPTURE_PROTOCOL_VERSION = 3
SUPPORTED_CAPTURE_PROTOCOL_VERSIONS = (1, 2, 3)

# Hard ceiling on a capture plan's attempt budget. Each admission attempt's
# blob rides its own relay key (capture_index = attempt - 1), so the storable
# blob indexes are EXACTLY 0..MAX_CAPTURE_PLAN_ATTEMPTS-1: the Worker applies
# this same value to indexes with a strict inequality (index >= cap rejected).
# Keep in lockstep with `MAX_CAPTURE_PLAN_ATTEMPTS` in relay/src/worker.js
# (pinned by tests/test_capture_relay_spec.py).
MAX_CAPTURE_PLAN_ATTEMPTS = 8

# The capture/upload contract mirrors the existing Pi backend so a relay-pulled
# WAV drops into the same analysis as today's same-origin upload
# (`jasper/web/correction_setup.py`: REQUIRED_SAMPLE_RATE, MAX_WAV_BODY_BYTES).
REQUIRED_SAMPLE_RATE_HZ = 48000
REQUIRED_CHANNELS = 1
DEFAULT_MAX_UPLOAD_BYTES = 32 * 1024 * 1024  # 32 MiB; matches MAX_WAV_BODY_BYTES
# A hard validation ceiling well above the default so a builder bug cannot mint a
# spec that would let a leaked upload token push gigabytes through the relay.
HARD_MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# Theme is a TOKEN allowlist. The page maps each token to a fixed CSS value; it
# never interprets a token as raw CSS. Keep these in lockstep with the page
# renderer's allowlist (capture-page/js/render.js).
THEME_ACCENTS = ("sage", "beige", "clay")
THEME_FONTS = ("figtree", "outfit")
DEFAULT_THEME = {"accent": "sage", "font": "figtree"}

# Server-driven-UI component vocabulary. The page renderer draws exactly these
# types; anything else is rejected on both sides.
UI_COMPONENT_TYPES = ("heading", "steps", "level_meter", "button", "note")
UI_BUTTON_ACTIONS = ("begin_capture", "retry", "stop")
UI_METER_SOURCES = ("mic",)
CALIBRATION_MODEL_KEYS = ("key", "label", "aliases")

# Per-kind measurement-validity policy vocabulary (consumed in build steps 6+).
CLEAN_CAPTURE_POLICIES = ("refuse", "warn")
CLOCK_DRIFT_MODES = ("ignore", "single_window", "critical")

# `default_setup.calibration.mode` vocabulary — mirrors the phone relay's own
# setup.calibration.mode values (jasper/web/correction_setup.py's
# `_relay_calibration_from_setup`): "serial" for a vendor lookup, "upload"
# for a bring-your-own file. There is no "none" here — a household record is
# only ever written after a calibration successfully established, so the
# hint is either present and actionable or absent entirely (`default_setup`
# stays `None`). This vocabulary describes how the ORIGINAL calibration was
# established, not what the phone echoes back — the phone's one-tap "Using
# {mic} — confirm" (gated on `resolvable`, below) replies with its OWN
# `setup.calibration.mode = "stored"`, a third value `_relay_calibration_from_setup`
# accepts but that never appears in this outbound hint.
DEFAULT_SETUP_CALIBRATION_MODES = ("serial", "upload")
DEFAULT_SETUP_CALIBRATION_KEYS = (
    "mode", "model", "serial_display", "calibration_id", "resolvable",
)

# The Pi is the only stimulus player today; the phone never plays anything.
STIMULUS_PLAYERS = ("pi",)

OUTPUT_FORMATS = ("wav",)
RETURN_URL_SCHEMES = ("http", "https")


class CaptureSpecError(ValueError):
    """A capture spec violated the contract. Raised loudly at the boundary."""


# --- Sub-records --------------------------------------------------------------


@dataclass(frozen=True)
class CaptureConstraints:
    """Browser `getUserMedia` audio constraints for a measurement-clean capture.

    All default ``False``: echo cancellation, auto gain, noise suppression, and
    (Safari) voice isolation each silently *flatten the very level/spectral
    differences the measurement exists to find*, so for measurement we demand
    they be off. The page also verifies the *realized* settings after
    `getUserMedia` (step 6) because WebKit has historically ignored
    ``echoCancellation:false``.

    Serializes to the camelCase keys the browser constraint object uses.
    """

    echo_cancellation: bool = False
    auto_gain_control: bool = False
    noise_suppression: bool = False
    voice_isolation: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "echoCancellation": self.echo_cancellation,
            "autoGainControl": self.auto_gain_control,
            "noiseSuppression": self.noise_suppression,
            "voiceIsolation": self.voice_isolation,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CaptureConstraints:
        return cls(
            echo_cancellation=_as_bool(data, "echoCancellation"),
            auto_gain_control=_as_bool(data, "autoGainControl"),
            noise_suppression=_as_bool(data, "noiseSuppression"),
            voice_isolation=_as_bool(data, "voiceIsolation"),
        )


@dataclass(frozen=True)
class CaptureStimulus:
    """What the Pi plays during the capture window.

    ``played_by`` is always ``"pi"`` today (the phone is the microphone, never a
    player). ``label`` is display/telemetry only — never trusted for logic. A
    ``None`` stimulus on the spec means a passive record (no Pi playback), e.g. a
    noise-floor capture.
    """

    played_by: str = "pi"
    label: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"played_by": self.played_by, "label": self.label}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CaptureStimulus:
        return cls(
            played_by=str(data.get("played_by", "pi")),
            label=str(data.get("label", "")),
        )


@dataclass(frozen=True)
class CaptureValidity:
    """Per-kind measurement-validity policy (plan §9).

    Carried *as data* in the spec so a single spec per kind drives both the page
    (``clean_capture`` / ``allow_capability_fallback``) and the Pi
    (``require_alignment`` / ``clock_drift``). The full enforcement lands in
    build step 6; step 1 fixes the vocabulary so the schema never has to change.

      - ``clean_capture``: ``"refuse"`` or ``"warn"`` if the browser did not
        honor the EC/AGC/NS=false constraints.
      - ``allow_capability_fallback``: if clean capture is impossible on this
        device (some iOS builds cannot honor ``echoCancellation:false``), the
        page degrades **gracefully and labeled** rather than dead-ending the
        phone. Pairs with ``clean_capture="refuse"`` to mean "refuse the clean
        path, offer the labeled fallback."
      - ``require_alignment``: the Pi's cross-correlation alignment confidence is
        a hard gate (a weak/ambiguous peak fails loud).
      - ``clock_drift``: per-kind handling of independent mic/playback clock
        drift. ``"ignore"`` for magnitude FR and level work; ``"single_window"``
        for timing comparisons that must stay within one recording; ``"critical"``
        reserved for the strictest sync paths.
    """

    clean_capture: str = "refuse"
    allow_capability_fallback: bool = True
    require_alignment: bool = True
    clock_drift: str = "ignore"

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean_capture": self.clean_capture,
            "allow_capability_fallback": self.allow_capability_fallback,
            "require_alignment": self.require_alignment,
            "clock_drift": self.clock_drift,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CaptureValidity:
        return cls(
            clean_capture=str(data.get("clean_capture", "refuse")),
            allow_capability_fallback=_as_bool(
                data, "allow_capability_fallback", default=True
            ),
            require_alignment=_as_bool(data, "require_alignment", default=True),
            clock_drift=str(data.get("clock_drift", "ignore")),
        )


@dataclass(frozen=True)
class CaptureAcknowledgement:
    """Required operator acknowledgement before a capture may arm playback."""

    id: str
    binding_id: str
    label: str
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "binding_id": self.binding_id,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CaptureAcknowledgement:
        allowed = {"schema_version", "id", "binding_id", "label"}
        extra = set(data) - allowed
        if extra:
            raise CaptureSpecError(
                f"acknowledgement has unknown keys: {sorted(extra)}"
            )
        for key in ("id", "binding_id", "label"):
            if not isinstance(data.get(key), str):
                raise CaptureSpecError(f"acknowledgement.{key} must be a string")
        return cls(
            schema_version=_as_int(data, "schema_version", default=1),
            id=str(data.get("id") or ""),
            binding_id=str(data.get("binding_id") or ""),
            label=str(data.get("label") or ""),
        )


@dataclass(frozen=True)
class DefaultSetupCalibration:
    """A household's remembered measurement-mic calibration, offered to the
    phone page as an OPTIONAL prefill hint — never binding.

    Populated from ``jasper.correction.household_mic`` (Wave-2 household-mic
    persistence) when a prior session on this speaker established a
    calibration. The capture page (2026-07 Wave-2 batch,
    ``capture-page/js/main.js``'s ``renderCalibrationConfirm``) reads this
    field and shows a one-tap "Using {label} · {serial_display} — one tap to
    confirm" screen with a "Use a different microphone" fallback to the full
    picker. Confirm SUBMITS ``setup.calibration = {mode: "stored",
    calibration_id, model}`` (``model`` is display-only there) for the Pi to
    resolve via the household-mic record — but ONLY when the hint carries
    ``resolvable: true``, the marker minted by the ``mode="stored"`` branch
    of ``_relay_calibration_from_setup`` in
    ``jasper/web/correction_setup.py`` when the ``calibration_id`` currently
    resolves on disk. A hint without the marker (an older Pi build predating
    ``stored`` mode, or a household calibration that has since gone missing
    from disk) still ships the rest of the hint, so the page renders its
    plain full picker instead — the pre-Wave-2 behavior, safe in every
    deploy order; a page-side rejection of a gone-stale stored record (the
    record changed in the narrow window between spec mint and tap) falls
    back to the same picker rather than dead-ending. An OLDER capture page
    (pre-Wave-2) still ignores this field entirely — it parses the spec as
    an opaque JSON object and never rejects unknown top-level keys (see
    ``capture-page/js/transport-integrity.js``'s ``verifyAndParseCaptureSpec``,
    which only checks it is a non-array object) — so shipping this field is
    safe against any deployed page, old or new.

    ``resolvable`` is a SEPARATE, freshly-checked flag from the fact that
    this hint exists at all: the Pi re-resolves ``calibration_id`` against
    the calibration store a second time at spec-build time (see
    ``jasper.web.correction_setup._default_setup_calibration_for_spec``) and
    only sets it ``True`` when THAT resolves cleanly, rather than trusting
    that an earlier resolve (used to build the hint's other fields) is still
    good. Defaults ``False`` and is omitted from the wire JSON in that
    case — the existing 4-key shape is unchanged for every caller that
    predates this field.
    """

    mode: str
    model: str = ""
    serial_display: str = ""
    calibration_id: str = ""
    resolvable: bool = False

    def to_dict(self) -> dict[str, str | bool]:
        data: dict[str, str | bool] = {
            "mode": self.mode,
            "model": self.model,
            "serial_display": self.serial_display,
            "calibration_id": self.calibration_id,
        }
        if self.resolvable:
            data["resolvable"] = True
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DefaultSetupCalibration:
        if not isinstance(data, Mapping):
            raise CaptureSpecError("default_setup.calibration must be an object")
        extra = set(data) - set(DEFAULT_SETUP_CALIBRATION_KEYS)
        if extra:
            raise CaptureSpecError(
                f"default_setup.calibration has unknown keys: {sorted(extra)}"
            )
        return cls(
            mode=str(data.get("mode") or ""),
            model=str(data.get("model") or ""),
            serial_display=str(data.get("serial_display") or ""),
            calibration_id=str(data.get("calibration_id") or ""),
            resolvable=_as_bool(data, "resolvable", default=False),
        )


CAPTURE_PLAN_KEYS = ("schema_version", "capture_target", "max_attempts", "entries")
CAPTURE_PLAN_ENTRY_KEYS = ("index", "kind_label", "duration_ms", "screen")
# schema_version 1 is the pre-entries shape (no `entries`, byte-identical to
# the original v3 contract); 2 is additive — per-capture heterogeneity
# (crossover-measurement-productization-design.md §5.7). A plan's
# schema_version and its `entries` presence are kept in strict lockstep by
# validation (`_validate_capture_plan_entries`) so a reader never has to
# re-derive one from the other.
CAPTURE_PLAN_SCHEMA_VERSIONS = (1, 2)
CAPTURE_PLAN_ENTRIES_SCHEMA_VERSION = 2
# Per-entry presentation copy is OPAQUE like `presentation_variant` — the
# schema bounds its size and value types, never its keys/vocabulary — but a
# size ceiling keeps a spec from smuggling an oversized payload through the
# relay's opaque-spec contract.
MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES = 4096


@dataclass(frozen=True)
class CapturePlanEntry:
    """One capture's identity/timing/copy inside a heterogeneous v3 plan.

    Wave 3 (crossover-measurement-productization-design.md §5.7) extends the
    session-spanning ``CapturePlan`` from "N repeats of ONE spec" to N
    captures that may each be a DIFFERENT kind of measurement (e.g. a
    conductor-model CHECK -> MEASURE -> VERIFY sequence) inside the SAME
    relay session.

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
            index=_as_int(data, "index"),
            kind_label=str(data.get("kind_label") or ""),
            duration_ms=_as_int(data, "duration_ms"),
            screen=(
                {str(k): str(v) for k, v in screen_raw.items()}
                if isinstance(screen_raw, Mapping)
                else None
            ),
        )


@dataclass(frozen=True)
class CapturePlan:
    """Session-spanning capture plan (capture protocol v3, SPEC W2.3).

    One relay session covers a driver's whole repeat SET instead of one
    capture per session: the phone requests each capture with an authenticated
    ``begin_capture {index, attempt}`` event, the Pi admits it (budget stays
    Pi-owned — ``repeat_admission`` — never phone-decided), and each admitted
    attempt's blob rides its own relay key (``capture_index = attempt - 1``).

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
            capture_target=_as_int(data, "capture_target"),
            max_attempts=_as_int(data, "max_attempts"),
            schema_version=_as_int(data, "schema_version", default=1),
            entries=entries,
        )

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


# --- Server-driven-UI builders (data, never markup) ---------------------------


def build_theme(accent: str = "sage", font: str = "figtree") -> dict[str, str]:
    """A theme = allowlisted *tokens* the page maps to fixed CSS variables."""
    return {"accent": accent, "font": font}


def ui_heading(text: str) -> dict[str, str]:
    return {"type": "heading", "text": str(text)}


def ui_steps(items: Sequence[str]) -> dict[str, Any]:
    return {"type": "steps", "items": [str(item) for item in items]}


def ui_level_meter(source: str = "mic") -> dict[str, str]:
    return {"type": "level_meter", "source": str(source)}


def ui_button(label: str, action: str = "begin_capture") -> dict[str, str]:
    return {"type": "button", "label": str(label), "action": str(action)}


def ui_note(text: str) -> dict[str, str]:
    return {"type": "note", "text": str(text)}


# --- The spec -----------------------------------------------------------------


@dataclass(frozen=True)
class CaptureSpec:
    """A kind-agnostic, opaque-to-the-relay capture spec.

    Build one with a per-kind builder (`build_room_sweep_spec`), serialize with
    `to_dict()` for the relay, and reconstruct/validate inbound JSON with
    `from_dict()`. `validate()` is called by `from_dict()` and may be called
    explicitly after a builder.
    """

    kind: str
    duration_ms: int
    pre_roll_ms: int
    post_roll_ms: int
    constraints: CaptureConstraints = field(default_factory=CaptureConstraints)
    stimulus: CaptureStimulus | None = None
    validity: CaptureValidity = field(default_factory=CaptureValidity)
    theme: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_THEME))
    screen: tuple[Mapping[str, Any], ...] = ()
    calibration_models: tuple[Mapping[str, Any], ...] = ()
    sample_rate_hz: int = REQUIRED_SAMPLE_RATE_HZ
    channels: int = REQUIRED_CHANNELS
    output_format: str = "wav"
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    return_url: str = ""
    # Whether the phone page should preflight its guided setup (notably vendor
    # mic serial lookup) through the Pi before advancing to the Start step.
    setup_validation: bool = False
    # Opaque setup binding used by flows that retain browser-side setup identity
    # (notably Active crossover). Modern Room follow-up links instead carry
    # Pi-owned placement fields and authenticate the realized level-check mic.
    setup_binding_id: str = ""
    # Whether the guided setup asks for the room position count. Crossover level
    # matching uses the same setup flow without that room-only question.
    setup_collect_positions: bool = False
    # Optional Pi-owned placement progress. The public capture page already
    # consumes these fields when a capture-only Room link has no persisted
    # browser setup; carrying them in the signed spec keeps position authority
    # on the speaker.
    position: int | None = None
    total_positions: int | None = None
    # Optional kind-owned presentation variant. It may change capture-page copy
    # only; the owning flow still controls sequencing, timeouts, and admission.
    # The shared schema validates its shape without enumerating per-kind values.
    presentation_variant: str = ""
    acknowledgement: CaptureAcknowledgement | None = None
    # Optional per-run nonce (additive, empty for kinds that don't use it). The
    # level_ramp flow mints one per ramp run; the phone echoes it in every
    # level_batch so the Pi's feed can distinguish THIS run's events from a
    # previous run's persisted relay slot (see level_match.RelayLevelFeed).
    run_token: str = ""
    # Optional household-mic prefill hint (Wave-2 persistence). See
    # `DefaultSetupCalibration` — never binding, ignored by the current page.
    default_setup_calibration: DefaultSetupCalibration | None = None
    # Session-spanning capture plan (protocol v3, SPEC W2.3). `None` for every
    # shipped builder today — presence requires (and is required by) protocol 3.
    capture_plan: CapturePlan | None = None
    capture_protocol_version: int = CAPTURE_PROTOCOL_VERSION
    schema_version: int = SCHEMA_VERSION

    # -- serialization --

    def to_dict(self) -> dict[str, Any]:
        """The opaque JSON the relay stores and the page fetches.

        Shape mirrors `docs/phone-mic-relay-plan.md` §6, plus additive
        `schema_version`, `validity`, and `run_token` fields.
        """
        return {
            "schema_version": self.schema_version,
            "capture_protocol_version": self.capture_protocol_version,
            "kind": self.kind,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "duration_ms": self.duration_ms,
            "pre_roll_ms": self.pre_roll_ms,
            "post_roll_ms": self.post_roll_ms,
            "constraints": self.constraints.to_dict(),
            "stimulus": self.stimulus.to_dict() if self.stimulus else None,
            "validity": self.validity.to_dict(),
            "calibration_models": [
                {
                    "key": str(model["key"]),
                    "label": str(model["label"]),
                    "aliases": [str(alias) for alias in model.get("aliases", ())],
                }
                for model in self.calibration_models
            ],
            "ui": {
                "theme": dict(self.theme),
                "screen": [dict(component) for component in self.screen],
            },
            "return_url": self.return_url,
            "setup_validation": self.setup_validation,
            "setup_binding_id": self.setup_binding_id,
            "setup_collect_positions": self.setup_collect_positions,
            **(
                {
                    "position": self.position,
                    "total_positions": self.total_positions,
                }
                if self.position is not None
                else {}
            ),
            **(
                {"presentation_variant": self.presentation_variant}
                if self.presentation_variant
                else {}
            ),
            "acknowledgement": (
                self.acknowledgement.to_dict() if self.acknowledgement else None
            ),
            "run_token": self.run_token,
            **(
                {
                    "default_setup": {
                        "calibration": self.default_setup_calibration.to_dict()
                    }
                }
                if self.default_setup_calibration is not None
                else {}
            ),
            **(
                {"capture_plan": self.capture_plan.to_dict()}
                if self.capture_plan is not None
                else {}
            ),
            "output": {"format": self.output_format},
            "max_upload_bytes": self.max_upload_bytes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CaptureSpec:
        """Reconstruct + validate a spec from inbound JSON. Raises on any drift."""
        if not isinstance(data, Mapping):
            raise CaptureSpecError("capture spec must be a JSON object")
        ui = data.get("ui") or {}
        if not isinstance(ui, Mapping):
            raise CaptureSpecError("ui must be an object")
        theme = ui.get("theme") or {}
        screen = ui.get("screen") or []
        if not isinstance(theme, Mapping):
            raise CaptureSpecError("ui.theme must be an object")
        if not isinstance(screen, Sequence) or isinstance(screen, (str, bytes)):
            raise CaptureSpecError("ui.screen must be a list")
        output = data.get("output") or {}
        if not isinstance(output, Mapping):
            raise CaptureSpecError("output must be an object")
        calibration_models = data.get("calibration_models") or []
        if not isinstance(calibration_models, Sequence) or isinstance(
            calibration_models, (str, bytes)
        ):
            raise CaptureSpecError("calibration_models must be a list")
        setup_validation = data.get("setup_validation", False)
        stimulus_raw = data.get("stimulus")
        acknowledgement_raw = data.get("acknowledgement")
        if acknowledgement_raw is not None and not isinstance(
            acknowledgement_raw, Mapping
        ):
            raise CaptureSpecError("acknowledgement must be an object or null")
        default_setup_raw = data.get("default_setup")
        default_setup_calibration: DefaultSetupCalibration | None = None
        if default_setup_raw is not None:
            if not isinstance(default_setup_raw, Mapping):
                raise CaptureSpecError("default_setup must be an object")
            extra_default_setup = set(default_setup_raw) - {"calibration"}
            if extra_default_setup:
                raise CaptureSpecError(
                    f"default_setup has unknown keys: {sorted(extra_default_setup)}"
                )
            calibration_raw = default_setup_raw.get("calibration")
            if calibration_raw is not None:
                default_setup_calibration = DefaultSetupCalibration.from_dict(
                    calibration_raw
                )
        capture_plan_raw = data.get("capture_plan")
        if capture_plan_raw is not None and not isinstance(capture_plan_raw, Mapping):
            raise CaptureSpecError("capture_plan must be an object or null")
        spec = cls(
            kind=str(data.get("kind", "")),
            duration_ms=_as_int(data, "duration_ms"),
            pre_roll_ms=_as_int(data, "pre_roll_ms"),
            post_roll_ms=_as_int(data, "post_roll_ms"),
            constraints=CaptureConstraints.from_dict(data.get("constraints") or {}),
            stimulus=(
                CaptureStimulus.from_dict(stimulus_raw)
                if isinstance(stimulus_raw, Mapping)
                else None
            ),
            validity=CaptureValidity.from_dict(data.get("validity") or {}),
            theme={str(k): str(v) for k, v in theme.items()},
            screen=tuple(
                {str(k): v for k, v in component.items()}
                for component in screen
                if isinstance(component, Mapping)
            ),
            calibration_models=tuple(
                dict(model)
                for model in calibration_models
                if isinstance(model, Mapping)
            ),
            sample_rate_hz=_as_int(data, "sample_rate_hz", default=REQUIRED_SAMPLE_RATE_HZ),
            channels=_as_int(data, "channels", default=REQUIRED_CHANNELS),
            output_format=str(output.get("format", "wav")),
            max_upload_bytes=_as_int(
                data, "max_upload_bytes", default=DEFAULT_MAX_UPLOAD_BYTES
            ),
            return_url=str(data.get("return_url") or ""),
            setup_validation=setup_validation,
            setup_binding_id=str(data.get("setup_binding_id") or ""),
            setup_collect_positions=_as_bool(
                data, "setup_collect_positions", default=False
            ),
            position=(
                _as_int(data, "position")
                if data.get("position") is not None
                else None
            ),
            total_positions=(
                _as_int(data, "total_positions")
                if data.get("total_positions") is not None
                else None
            ),
            presentation_variant=data.get("presentation_variant", ""),
            acknowledgement=(
                CaptureAcknowledgement.from_dict(acknowledgement_raw)
                if isinstance(acknowledgement_raw, Mapping)
                else None
            ),
            run_token=str(data.get("run_token") or ""),
            default_setup_calibration=default_setup_calibration,
            capture_plan=(
                CapturePlan.from_dict(capture_plan_raw)
                if isinstance(capture_plan_raw, Mapping)
                else None
            ),
            capture_protocol_version=_as_int(
                data,
                "capture_protocol_version",
                default=CAPTURE_PROTOCOL_VERSION,
            ),
            schema_version=_as_int(data, "schema_version", default=SCHEMA_VERSION),
        )
        # Guard against a screen entry that was not a Mapping (dropped above).
        if len(spec.screen) != len(screen):
            raise CaptureSpecError("every ui.screen entry must be an object")
        if len(spec.calibration_models) != len(calibration_models):
            raise CaptureSpecError("every calibration_models entry must be an object")
        spec.validate()
        return spec

    # -- validation --

    def validate(self) -> CaptureSpec:
        """Strict, loud validation. Returns self so callers can chain."""
        if not self.kind or not isinstance(self.kind, str):
            raise CaptureSpecError("kind must be a non-empty string")
        if self.capture_protocol_version not in SUPPORTED_CAPTURE_PROTOCOL_VERSIONS:
            raise CaptureSpecError(
                "capture_protocol_version must be one of "
                f"{SUPPORTED_CAPTURE_PROTOCOL_VERSIONS}, "
                f"got {self.capture_protocol_version}"
            )
        # NB: kinds are deliberately NOT enumerated — a new kind needs no schema
        # change. We validate the *shape*, never the *vocabulary* of kind.
        if self.sample_rate_hz != REQUIRED_SAMPLE_RATE_HZ:
            raise CaptureSpecError(
                f"sample_rate_hz must be {REQUIRED_SAMPLE_RATE_HZ}, "
                f"got {self.sample_rate_hz}"
            )
        if self.channels != REQUIRED_CHANNELS:
            raise CaptureSpecError(
                f"channels must be {REQUIRED_CHANNELS} (mono), got {self.channels}"
            )
        for name, value in (
            ("duration_ms", self.duration_ms),
            ("pre_roll_ms", self.pre_roll_ms),
            ("post_roll_ms", self.post_roll_ms),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise CaptureSpecError(f"{name} must be an integer")
        if self.duration_ms <= 0:
            raise CaptureSpecError("duration_ms must be positive")
        if self.pre_roll_ms < 0 or self.post_roll_ms < 0:
            raise CaptureSpecError("pre_roll_ms / post_roll_ms must be >= 0")
        if self.duration_ms < self.pre_roll_ms + self.post_roll_ms:
            raise CaptureSpecError(
                "duration_ms must be >= pre_roll_ms + post_roll_ms so the "
                "stimulus window fits inside the recording"
            )
        if self.output_format not in OUTPUT_FORMATS:
            raise CaptureSpecError(
                f"output.format must be one of {OUTPUT_FORMATS}, "
                f"got {self.output_format!r}"
            )
        if not isinstance(self.setup_validation, bool):
            raise CaptureSpecError("setup_validation must be a boolean")
        if not isinstance(self.setup_collect_positions, bool):
            raise CaptureSpecError("setup_collect_positions must be a boolean")
        _validate_run_token(self.run_token)
        _validate_acknowledgement(
            self.acknowledgement,
            capture_protocol_version=self.capture_protocol_version,
        )
        _validate_capture_plan(
            self.capture_plan,
            capture_protocol_version=self.capture_protocol_version,
        )
        if self.setup_binding_id and not re.fullmatch(
            r"[A-Za-z0-9_-]{12,160}", self.setup_binding_id
        ):
            raise CaptureSpecError(
                "setup_binding_id must be 12..160 URL-safe characters"
            )
        if self.setup_collect_positions and not self.setup_validation:
            raise CaptureSpecError(
                "setup_collect_positions requires setup_validation=true"
            )
        if (self.position is None) != (self.total_positions is None):
            raise CaptureSpecError(
                "position and total_positions must be supplied together"
            )
        if self.position is not None:
            if (
                not isinstance(self.position, int)
                or isinstance(self.position, bool)
                or not isinstance(self.total_positions, int)
                or isinstance(self.total_positions, bool)
            ):
                raise CaptureSpecError(
                    "position and total_positions must be integers"
                )
            if (
                self.position <= 0
                or self.total_positions <= 0
                or self.position > self.total_positions
            ):
                raise CaptureSpecError(
                    "position must be within 1..total_positions"
                )
        if not isinstance(self.presentation_variant, str) or (
            self.presentation_variant
            and not re.fullmatch(
                r"[a-z][a-z0-9_-]{0,63}", self.presentation_variant
            )
        ):
            raise CaptureSpecError(
                "presentation_variant must be an empty or 1..64-character slug"
            )
        if (
            not isinstance(self.max_upload_bytes, int)
            or isinstance(self.max_upload_bytes, bool)
            or self.max_upload_bytes <= 0
            or self.max_upload_bytes > HARD_MAX_UPLOAD_BYTES
        ):
            raise CaptureSpecError(
                f"max_upload_bytes must be in 1..{HARD_MAX_UPLOAD_BYTES}, "
                f"got {self.max_upload_bytes}"
            )
        if self.stimulus is not None and self.stimulus.played_by not in STIMULUS_PLAYERS:
            raise CaptureSpecError(
                f"stimulus.played_by must be one of {STIMULUS_PLAYERS}, "
                f"got {self.stimulus.played_by!r}"
            )
        _validate_validity(self.validity)
        _validate_calibration_models(self.calibration_models)
        _validate_default_setup_calibration(self.default_setup_calibration)
        _validate_theme(self.theme)
        _validate_screen(self.screen)
        if self.acknowledgement is not None and not any(
            component.get("type") == "button"
            and component.get("action") == "begin_capture"
            for component in self.screen
        ):
            raise CaptureSpecError(
                "acknowledgement requires a begin_capture button"
            )
        _validate_return_url(self.return_url)
        return self

    def with_screen(self, *components: Mapping[str, Any]) -> CaptureSpec:
        """Return a copy whose `screen` is the given components (validated)."""
        return replace(self, screen=tuple(components)).validate()

    def with_return_url(self, return_url: str) -> CaptureSpec:
        """Return a copy carrying the local Pi URL the phone should return to."""
        return replace(self, return_url=str(return_url or "")).validate()


# --- Validation helpers -------------------------------------------------------


def _validate_validity(validity: CaptureValidity) -> None:
    if validity.clean_capture not in CLEAN_CAPTURE_POLICIES:
        raise CaptureSpecError(
            f"validity.clean_capture must be one of {CLEAN_CAPTURE_POLICIES}, "
            f"got {validity.clean_capture!r}"
        )
    if validity.clock_drift not in CLOCK_DRIFT_MODES:
        raise CaptureSpecError(
            f"validity.clock_drift must be one of {CLOCK_DRIFT_MODES}, "
            f"got {validity.clock_drift!r}"
        )
    if not isinstance(validity.allow_capability_fallback, bool):
        raise CaptureSpecError("validity.allow_capability_fallback must be a bool")
    if not isinstance(validity.require_alignment, bool):
        raise CaptureSpecError("validity.require_alignment must be a bool")


def _validate_calibration_models(models: Sequence[Mapping[str, Any]]) -> None:
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes)):
        raise CaptureSpecError("calibration_models must be a list")
    seen: set[str] = set()
    for index, model in enumerate(models):
        if not isinstance(model, Mapping):
            raise CaptureSpecError(f"calibration_models[{index}] must be an object")
        extra = set(model) - set(CALIBRATION_MODEL_KEYS)
        if extra:
            raise CaptureSpecError(
                f"calibration_models[{index}] has unknown keys: {sorted(extra)}"
            )
        key = model.get("key")
        label = model.get("label")
        aliases = model.get("aliases", ())
        if not isinstance(key, str) or not key:
            raise CaptureSpecError(f"calibration_models[{index}].key must be a string")
        if key in seen:
            raise CaptureSpecError(f"duplicate calibration model key: {key}")
        seen.add(key)
        if not isinstance(label, str) or not label:
            raise CaptureSpecError(
                f"calibration_models[{index}].label must be a string"
            )
        if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
            raise CaptureSpecError(
                f"calibration_models[{index}].aliases must be a list"
            )
        if not all(isinstance(alias, str) for alias in aliases):
            raise CaptureSpecError(
                f"calibration_models[{index}].aliases must be a list of strings"
            )


def _validate_default_setup_calibration(
    default_setup_calibration: DefaultSetupCalibration | None,
) -> None:
    if default_setup_calibration is None:
        return
    if default_setup_calibration.mode not in DEFAULT_SETUP_CALIBRATION_MODES:
        raise CaptureSpecError(
            "default_setup.calibration.mode must be one of "
            f"{DEFAULT_SETUP_CALIBRATION_MODES}, "
            f"got {default_setup_calibration.mode!r}"
        )
    if not default_setup_calibration.calibration_id:
        raise CaptureSpecError(
            "default_setup.calibration.calibration_id is required"
        )


def _validate_theme(theme: Mapping[str, str]) -> None:
    accent = theme.get("accent")
    font = theme.get("font")
    if accent not in THEME_ACCENTS:
        raise CaptureSpecError(
            f"ui.theme.accent must be an allowlisted token {THEME_ACCENTS}, "
            f"got {accent!r}"
        )
    if font not in THEME_FONTS:
        raise CaptureSpecError(
            f"ui.theme.font must be an allowlisted token {THEME_FONTS}, "
            f"got {font!r}"
        )
    extra = set(theme) - {"accent", "font"}
    if extra:
        raise CaptureSpecError(f"ui.theme has unknown keys: {sorted(extra)}")


def _validate_screen(screen: Sequence[Mapping[str, Any]]) -> None:
    if not isinstance(screen, Sequence) or isinstance(screen, (str, bytes)):
        raise CaptureSpecError("ui.screen must be a list")
    for index, component in enumerate(screen):
        if not isinstance(component, Mapping):
            raise CaptureSpecError(f"ui.screen[{index}] must be an object")
        ctype = component.get("type")
        if ctype not in UI_COMPONENT_TYPES:
            raise CaptureSpecError(
                f"ui.screen[{index}].type must be one of {UI_COMPONENT_TYPES}, "
                f"got {ctype!r}"
            )
        if ctype in ("heading", "note"):
            if not isinstance(component.get("text"), str):
                raise CaptureSpecError(f"ui.screen[{index}].text must be a string")
        elif ctype == "steps":
            items = component.get("items")
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                raise CaptureSpecError(f"ui.screen[{index}].items must be a list")
            if not all(isinstance(item, str) for item in items):
                raise CaptureSpecError(
                    f"ui.screen[{index}].items must be a list of strings"
                )
        elif ctype == "level_meter":
            if component.get("source") not in UI_METER_SOURCES:
                raise CaptureSpecError(
                    f"ui.screen[{index}].source must be one of {UI_METER_SOURCES}"
                )
        elif ctype == "button":
            if not isinstance(component.get("label"), str):
                raise CaptureSpecError(f"ui.screen[{index}].label must be a string")
            if component.get("action") not in UI_BUTTON_ACTIONS:
                raise CaptureSpecError(
                    f"ui.screen[{index}].action must be one of {UI_BUTTON_ACTIONS}, "
                    f"got {component.get('action')!r}"
                )


def _validate_run_token(run_token: str) -> None:
    if not isinstance(run_token, str):
        raise CaptureSpecError("run_token must be a string")
    if not run_token:
        return
    if len(run_token) > 64 or not all(
        ch.isalnum() or ch in "-_" for ch in run_token
    ):
        raise CaptureSpecError(
            "run_token must be <= 64 URL-safe characters (alnum, '-', '_')"
        )


def _validate_acknowledgement(
    acknowledgement: CaptureAcknowledgement | None,
    *,
    capture_protocol_version: int,
) -> None:
    if acknowledgement is None:
        return
    if capture_protocol_version < 2:
        raise CaptureSpecError("acknowledgement requires capture protocol 2")
    if acknowledgement.schema_version != 1:
        raise CaptureSpecError("acknowledgement.schema_version must be 1")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", acknowledgement.id):
        raise CaptureSpecError("acknowledgement.id is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,96}", acknowledgement.binding_id):
        raise CaptureSpecError("acknowledgement.binding_id is invalid")
    if not acknowledgement.label or len(acknowledgement.label) > 360:
        raise CaptureSpecError("acknowledgement.label must be 1..360 characters")


def _validate_capture_plan(
    capture_plan: CapturePlan | None,
    *,
    capture_protocol_version: int,
) -> None:
    if capture_plan is None:
        if capture_protocol_version >= SESSION_SPANNING_CAPTURE_PROTOCOL_VERSION:
            raise CaptureSpecError(
                "capture protocol 3 requires a capture_plan"
            )
        return
    if capture_protocol_version < SESSION_SPANNING_CAPTURE_PROTOCOL_VERSION:
        raise CaptureSpecError("capture_plan requires capture protocol 3")
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

    v1 payloads without entries stay exactly as strict as before this field
    existed (``entries is None`` and ``schema_version == 1`` is the only
    legal pre-Wave-3 shape). A plan that DOES carry entries must cover every
    index ``0..capture_target-1`` exactly once — contiguous, unique — so the
    session runner can always resolve "the entry for capture N" with no gaps.
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
        if isinstance(entry.duration_ms, bool) or not isinstance(
            entry.duration_ms, int
        ):
            raise CaptureSpecError(
                f"capture_plan.entries[{position}].duration_ms must be an integer"
            )
        if entry.duration_ms <= 0:
            raise CaptureSpecError(
                f"capture_plan.entries[{position}].duration_ms must be positive"
            )
        if not isinstance(entry.kind_label, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,31}", entry.kind_label
        ):
            raise CaptureSpecError(
                f"capture_plan.entries[{position}].kind_label must be a short "
                "lowercase slug"
            )
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


def _validate_return_url(return_url: str) -> None:
    if not isinstance(return_url, str):
        raise CaptureSpecError("return_url must be a string")
    if not return_url:
        return
    if len(return_url) > 2048 or any(
        ord(ch) < 32 or ord(ch) == 127 for ch in return_url
    ):
        raise CaptureSpecError("return_url must be a clean absolute URL")
    parsed = urlsplit(return_url)
    if parsed.scheme not in RETURN_URL_SCHEMES:
        raise CaptureSpecError(
            f"return_url scheme must be one of {RETURN_URL_SCHEMES}"
        )
    if not parsed.netloc or not parsed.hostname:
        raise CaptureSpecError("return_url must include a host")
    if parsed.username or parsed.password:
        raise CaptureSpecError("return_url must not include credentials")
    if parsed.fragment:
        raise CaptureSpecError("return_url must not include a URL fragment")


def _as_bool(data: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise CaptureSpecError(f"{key} must be a boolean, got {type(value).__name__}")
    return value


def _as_int(data: Mapping[str, Any], key: str, *, default: int | None = None) -> int:
    if key not in data or data.get(key) is None:
        if default is None:
            raise CaptureSpecError(f"{key} is required")
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise CaptureSpecError(f"{key} must be an integer, got {type(value).__name__}")
    return value


# --- Per-kind builders --------------------------------------------------------


def build_room_sweep_spec(
    *,
    stimulus_duration_ms: int = 10000,
    pre_roll_ms: int = 800,
    post_roll_ms: int = 700,
    hard_timeout_ms: int = 30000,
    position: int | None = None,
    total_positions: int | None = None,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    calibration_models: Sequence[Mapping[str, Any]] | None = None,
    guided_setup: bool = True,
    setup_binding_id: str = "",
    presentation_variant: str = "",
) -> CaptureSpec:
    """Build the `kind="room_sweep"` capture spec (plan §6, build step 1).

    ``duration_ms`` is the hard recording timeout, not the usual stop condition:
    the phone records until the Pi reports ``sweep_complete`` through the relay,
    then keeps ``post_roll_ms`` of room tail. ``pre_roll_ms`` remains part of the
    spec for compatibility/documentation, but the race is now prevented more
    directly: the phone starts recording before it posts ``armed`` and the Pi only
    plays after seeing that event. Magnitude frequency response is drift-insensitive, so
    ``clock_drift="ignore"``; clean capture is mandatory (EC/AGC/NS flatten the
    response we measure) so ``clean_capture="refuse"`` — paired with a labeled
    device-capability fallback so a strict iPhone is never dead-ended.

    The ``ui`` is server-driven: the heading, steps, and button label all ship
    from here, so copy/choreography changes ride a Pi update with no web deploy.
    ``position`` / ``total_positions`` tailor the copy for the multi-position
    correction flow.
    """
    if stimulus_duration_ms <= 0:
        raise CaptureSpecError("stimulus_duration_ms must be positive")
    if pre_roll_ms < 0 or post_roll_ms < 0:
        raise CaptureSpecError("pre_roll_ms / post_roll_ms must be >= 0")
    if presentation_variant not in {"", "trust_repeat"}:
        raise CaptureSpecError(
            "room_sweep presentation_variant must be empty or trust_repeat"
        )
    duration_ms = max(
        pre_roll_ms + stimulus_duration_ms + post_roll_ms,
        int(hard_timeout_ms),
    )

    if position is not None and total_positions:
        heading_text = f"Room measurement — position {position} of {total_positions}"
    else:
        heading_text = "Room measurement"
    seconds = round(stimulus_duration_ms / 1000)
    if calibration_models is None and guided_setup:
        from jasper.audio_measurement.calibration import supported_model_options

        calibration_models = supported_model_options()
    elif calibration_models is None:
        calibration_models = ()

    spec = CaptureSpec(
        kind="room_sweep",
        duration_ms=duration_ms,
        pre_roll_ms=pre_roll_ms,
        post_roll_ms=post_roll_ms,
        constraints=CaptureConstraints(),  # all false → measurement-clean
        stimulus=CaptureStimulus(played_by="pi", label="log sweep 20 Hz – 20 kHz"),
        validity=CaptureValidity(
            clean_capture="refuse",
            allow_capability_fallback=True,
            require_alignment=True,
            clock_drift="ignore",
        ),
        theme=build_theme(accent=accent, font=font),
        screen=(
            ui_heading(heading_text),
            ui_steps(
                [
                    "Stand at your listening position",
                    "Hold the phone up at ear height",
                    f"Tap Start, then stay quiet for about {seconds} seconds",
                ]
            ),
            ui_level_meter("mic"),
            ui_button("Start", action="begin_capture"),
            ui_note("Keep the screen on — leaving this page stops the recording."),
        ),
        calibration_models=tuple(calibration_models),
        max_upload_bytes=max_upload_bytes,
        # Mic choice + calibration are session setup, not per-position work.
        # The first level-check link validates and freezes them on the Pi; later
        # position links are intentionally capture-only and report the realized
        # device for the Pi's identity check before playback and after upload.
        setup_validation=guided_setup,
        setup_binding_id=setup_binding_id,
        position=position,
        total_positions=total_positions,
        presentation_variant=presentation_variant,
    )
    return spec.validate()


# The sibling builders below are the plan §14 step-8 generalization. Each is a
# new measurement KIND added with **zero relay change** (the relay is opaque) and
# **zero page-renderer change** (every screen reuses the closed component
# vocabulary — `ui_heading` / `ui_steps` / `ui_level_meter` / `ui_button` /
# `ui_note`). The only per-kind differences are copy (server-driven) and the
# validity policy, both carried as DATA in the spec. Pinned by
# tests/test_capture_relay_kinds.py.


def build_balance_burst_spec(
    *,
    stimulus_duration_ms: int = 2400,
    pre_roll_ms: int = 800,
    post_roll_ms: int = 600,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> CaptureSpec:
    """`kind="balance_burst"` — left/right level balance.

    Clean capture is mandatory: auto-gain would normalize away the very L/R level
    difference being measured (`clean_capture="refuse"`). It is a level
    comparison, not an arrival-timing one, so alignment is not required and clock
    drift is irrelevant (`require_alignment=False`, `clock_drift="ignore"`).
    """
    duration_ms = pre_roll_ms + stimulus_duration_ms + post_roll_ms
    return CaptureSpec(
        kind="balance_burst",
        duration_ms=duration_ms,
        pre_roll_ms=pre_roll_ms,
        post_roll_ms=post_roll_ms,
        constraints=CaptureConstraints(),
        stimulus=CaptureStimulus(played_by="pi", label="left then right level bursts"),
        validity=CaptureValidity(
            clean_capture="refuse",
            allow_capability_fallback=True,
            require_alignment=False,
            clock_drift="ignore",
        ),
        theme=build_theme(accent=accent, font=font),
        screen=(
            ui_heading("Speaker balance"),
            ui_steps(
                [
                    "Sit centred between the two speakers",
                    "Hold the phone up at ear height",
                    "Tap Start and stay still while each side plays",
                ]
            ),
            ui_level_meter("mic"),
            ui_button("Start", action="begin_capture"),
            ui_note("Keep the screen on — leaving this page stops the recording."),
        ),
        max_upload_bytes=max_upload_bytes,
    ).validate()


def build_sync_marker_spec(
    *,
    stimulus_duration_ms: int = 2000,
    pre_roll_ms: int = 800,
    post_roll_ms: int = 600,
    hard_timeout_ms: int = 30000,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> CaptureSpec:
    """`kind="sync_marker"` — left/right arrival-time delta.

    Both L and R markers land inside ONE recording so the independent mic/playback
    clock drift is common-mode and cancels (`clock_drift="single_window"`, §9) —
    the timing answer comes from comparing the two markers within the single
    capture, never across separate captures. Arrival alignment is the signal, so
    `require_alignment=True`. The window must contain both markers (the Pi plays
    them at ~0.5 s and ~1.5 s); `stimulus_duration_ms` spans that.

    ``duration_ms`` is the phone's HARD recording deadline whose clock starts at
    ``armed`` (its ``waitForSweepComplete`` throws when it expires), so — like
    ``room_sweep`` and ``crossover_sweep`` — the acoustic window is floored by
    ``hard_timeout_ms``. The pre-floor value (3 400 ms) left ~1.4 s for the Pi's
    entire armed-poll → playback → ``sweep_complete``-post round trip, which
    killed every sync relay capture; the normal stop is the Pi's
    ``sweep_complete`` relay event (published by ``sync_flow.
    relay_run_and_consume``), the deadline is only the backstop.
    """
    duration_ms = max(
        pre_roll_ms + stimulus_duration_ms + post_roll_ms,
        int(hard_timeout_ms),
    )
    return CaptureSpec(
        kind="sync_marker",
        duration_ms=duration_ms,
        pre_roll_ms=pre_roll_ms,
        post_roll_ms=post_roll_ms,
        constraints=CaptureConstraints(),
        stimulus=CaptureStimulus(played_by="pi", label="left/right sync markers"),
        validity=CaptureValidity(
            clean_capture="refuse",
            allow_capability_fallback=True,
            require_alignment=True,
            clock_drift="single_window",
        ),
        theme=build_theme(accent=accent, font=font),
        screen=(
            ui_heading("Speaker sync"),
            ui_steps(
                [
                    "Sit at your listening position",
                    "Hold the phone up at ear height",
                    "Tap Start and stay quiet for the two clicks",
                ]
            ),
            ui_level_meter("mic"),
            ui_button("Start", action="begin_capture"),
            ui_note("Keep the screen on — leaving this page stops the recording."),
        ),
        max_upload_bytes=max_upload_bytes,
    ).validate()


def build_crossover_sweep_spec(
    *,
    driver_label: str = "driver",
    driver_role: str = "driver",
    driver_capture_geometry: str = "near_field",
    acknowledgement_binding: str = "",
    stimulus_duration_ms: int | None = None,
    pre_roll_ms: int = 800,
    post_roll_ms: int = 700,
    hard_timeout_ms: int = 30000,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ambient_duration_ms: int = 0,
    capture_plan: CapturePlan | None = None,
    default_setup_calibration: DefaultSetupCalibration | None = None,
) -> CaptureSpec:
    """`kind="crossover_sweep"` — per-driver frequency response for active
    crossover work. Same acoustic shape as `room_sweep` (a clean log sweep,
    magnitude FR, drift-insensitive), but the copy names the driver under test
    (server-driven UI), so the household measures each driver in turn.

    ``stimulus_duration_ms`` defaults to the **kernel-side** sweep length the
    active-crossover flow actually plays — ``driver_acoustics.DEFAULT_DURATION_S``
    — rather than a second, forked sweep constant. The Pi's driver/summed capture
    sweep is written and deconvolved from that one length
    (``web_measurement.capture_sweep_meta`` / ``write_driver_sweep_wav``), and the
    deconvolution reference is regenerated from the played ``sweep_meta``, so the
    spec must not advertise a different duration to the phone (its recording copy
    is sized from this). Sourcing it here keeps ONE sweep definition; a mismatch
    would only mis-size the phone's copy, never the deconvolution basis.

    ``duration_ms`` is the phone's HARD recording deadline and its clock starts
    at ``armed`` — before ``sweep_complete`` can arrive the Pi must see ``armed``
    on its ~0.75 s status poll, load the commissioning config, generate the
    sweep WAV, play the full sweep, release the fan-in lane, roll the transient
    graph back, and post through the relay. So, exactly like ``room_sweep``, the
    acoustic window is **floored by ``hard_timeout_ms``**: the normal stop is
    the Pi's ``sweep_complete`` relay event and the deadline is only the
    backstop — never the working margin. Pinned by
    ``tests/test_capture_relay_kinds.py``.

    ``capture_plan`` opts the spec into the session-spanning capture protocol
    (v3, SPEC W2.3): one relay session for the driver's whole repeat set, the
    phone requesting each capture with an authenticated ``begin_capture``
    event. **Default (``None``) is byte-identical to the pre-plan builder** —
    ``jasper/web/correction_setup.py``'s driver-sweep relay-capture handler
    is the one production caller that passes it (unconditionally, for every
    driver capture; summed/verification callers never do). The Wave-2
    capture page implements the matching v3 loop; the coordinator's deploy
    sequencing (worker → page publish → the Pi host flip last), not a code
    flag, gates the rollout — see the module docstring. A plan requires an
    ``acknowledgement_binding`` (placement gates run per capture, exactly as
    today).

    ``default_setup_calibration`` is the OPTIONAL household-mic prefill hint
    (Wave-2 persistence, ``jasper.correction.household_mic`` — same field
    ``build_level_ramp_spec`` already carries). W6.12: unlike ``level_ramp``,
    a ``crossover_sweep`` capture (legacy per-driver OR the v2 capture-plan
    session) has NO calibration-picker screen of its own — the legacy flow's
    calibration comes from the ``level_ramp`` level-match page the household
    visits FIRST in the same phone tab (its choice survives in the page's
    module state across the in-tab hash navigation into the driver sweeps
    that follow); v2 has no such preceding page, so its capture always
    carried ``setup.calibration.mode == "none"`` and every v2 capture logged
    ``crossover_v2_uncalibrated_capture`` even when the household had a
    resolvable stored mic. The capture page applies this hint SILENTLY (no
    extra tap — a v2 session is designed around a minimal, fixed tap count)
    when nothing has already been chosen for this page load. Omitted by
    default so every existing caller (including the two legacy handlers in
    ``jasper/web/correction_setup.py``, which resolve calibration through the
    preceding level-match page instead) stays byte-identical.
    """
    if stimulus_duration_ms is None:
        # Lazy import: the kernel module pulls numpy/scipy, and the socket-
        # activated wizard builds specs on a light process.
        from jasper.active_speaker.driver_acoustics import DEFAULT_DURATION_S

        stimulus_duration_ms = int(round(DEFAULT_DURATION_S * 1000))
    if ambient_duration_ms < 0:
        raise CaptureSpecError("ambient_duration_ms must be >= 0")
    duration_ms = max(
        pre_roll_ms + ambient_duration_ms + stimulus_duration_ms + post_roll_ms,
        int(hard_timeout_ms),
    )
    from jasper.active_speaker.capture_geometry import (
        DRIVER_CAPTURE_GEOMETRIES,
        DRIVER_PLACEMENT_POLICY_ID,
        REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
        SUMMED_PLACEMENT_POLICY_ID,
        driver_placement_instruction,
        placement_acknowledgement_label,
        reference_axis_driver_acknowledgement_label,
        reference_axis_driver_placement_instruction,
        summed_acknowledgement_label,
        summed_placement_instruction,
    )

    seconds = round(stimulus_duration_ms / 1000)
    is_driver = str(driver_role or "").strip().lower() not in {"", "summed"}
    geometry = str(driver_capture_geometry or "").strip().lower()
    if is_driver and geometry not in DRIVER_CAPTURE_GEOMETRIES:
        raise CaptureSpecError("driver capture geometry is unsupported")
    placement_instruction = (
        (
            reference_axis_driver_placement_instruction(driver_role)
            if geometry == "reference_axis"
            else driver_placement_instruction(driver_role)
        )
        if is_driver
        else summed_placement_instruction()
    )
    button_label = (
        (
            f"The mic is fixed on-axis — measure {driver_label}"
            if geometry == "reference_axis"
            else f"I’ve positioned the mic — measure {driver_label}"
        )
        if is_driver
        else "The mic is fixed on-axis — measure the combined drivers"
    )
    acknowledgement = (
        CaptureAcknowledgement(
            id=(
                (
                    REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID
                    if geometry == "reference_axis"
                    else DRIVER_PLACEMENT_POLICY_ID
                )
                if is_driver
                else SUMMED_PLACEMENT_POLICY_ID
            ),
            binding_id=acknowledgement_binding,
            label=(
                (
                    reference_axis_driver_acknowledgement_label(driver_role)
                    if geometry == "reference_axis"
                    else placement_acknowledgement_label(driver_role)
                )
                if is_driver
                else summed_acknowledgement_label()
            ),
        )
        if acknowledgement_binding
        else None
    )
    if capture_plan is not None and acknowledgement is None:
        raise CaptureSpecError(
            "a crossover capture_plan requires an acknowledgement_binding"
        )
    return CaptureSpec(
        kind="crossover_sweep",
        duration_ms=duration_ms,
        pre_roll_ms=pre_roll_ms,
        post_roll_ms=post_roll_ms,
        constraints=CaptureConstraints(),
        stimulus=CaptureStimulus(
            played_by="pi", label=f"log sweep — {driver_label}"
        ),
        validity=CaptureValidity(
            clean_capture="refuse",
            allow_capability_fallback=True,
            require_alignment=True,
            clock_drift="ignore",
        ),
        theme=build_theme(accent=accent, font=font),
        screen=(
            ui_heading(f"Crossover — {driver_label}"),
            ui_steps(
                [
                    placement_instruction,
                    (
                        "Tap Start and stay quiet while JTS measures the room "
                        f"noise, then plays about {seconds} seconds of sweep"
                        if ambient_duration_ms
                        else f"Tap Start, then stay quiet for about {seconds} seconds"
                    ),
                    "Keep the phone still until the sweep finishes",
                ]
            ),
            ui_level_meter("mic"),
            ui_button(button_label, action="begin_capture"),
            ui_button("Stop", action="stop"),
            ui_note("Keep the screen on — leaving this page stops the recording."),
        ),
        max_upload_bytes=max_upload_bytes,
        acknowledgement=acknowledgement,
        capture_plan=capture_plan,
        default_setup_calibration=default_setup_calibration,
        capture_protocol_version=(
            SESSION_SPANNING_CAPTURE_PROTOCOL_VERSION
            if capture_plan is not None
            else 2
            if acknowledgement
            else CAPTURE_PROTOCOL_VERSION
        ),
    ).validate()


def build_level_ramp_spec(
    *,
    geometry_label: str = "listening position",
    placement_instruction: str = "",
    tone_frequency_hz: float = 1000.0,
    hard_timeout_ms: int = 75000,
    pre_roll_ms: int = 400,
    post_roll_ms: int = 400,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    run_token: str = "",
    calibration_models: Sequence[Mapping[str, Any]] | None = None,
    setup_binding_id: str = "",
    setup_collect_positions: bool = False,
    default_setup_calibration: DefaultSetupCalibration | None = None,
) -> CaptureSpec:
    """`kind="level_ramp"` — the relay-closed level-match ramp (§3.1, P2).

    Unlike the sweep kinds this capture does NOT upload a WAV to analyze: the Pi
    plays a quiet-start staircase of band-limited noise while the phone streams
    **batched, client-timestamped mic-level samples** over the relay ``event``
    channel, and the Pi's :class:`~jasper.audio_measurement.ramp.RampController`
    settles into the safe window and locks. ``duration_ms`` is therefore a hard
    phone-side *timeout* sized ABOVE the Pi's own derived safety timeout
    (``MeasurementRamp.safety_timeout``, ≈56 s at defaults) so the Pi's stop is
    always the real one; the phone otherwise stops streaming when the Pi posts a
    terminal ``ramp`` host event (re-posted until the relay echoes it back —
    the ``event`` slot is a read-modify-write race).

    ``run_token`` is the per-run nonce (mint one per ramp run; pass the same
    value to ``LevelMatchSession.run_for_geometry``): the phone echoes it in
    every level batch so a previous run's persisted relay slot can never
    insta-cancel or mis-feed a retry.

    Clean capture is mandatory — auto-gain would flatten the very level the ramp
    maps. The page additionally requires explicit realized
    ``autoGainControl=false`` and refuses before the tone when a browser cannot
    prove it; ``allow_capability_fallback`` does not authorize a degraded
    automatic level result. It is a
    level comparison, not a timing one, so alignment is not required and clock
    drift is irrelevant (``require_alignment=False``, ``clock_drift="ignore"``).
    ``geometry_label`` tailors the heading for the near-field (baffle) vs
    listening-position step. ``placement_instruction`` optionally supplies the
    exact Pi-owned geometry copy; the page renders that same instruction after
    microphone setup instead of inventing a second placement description.

    ``default_setup_calibration`` is the OPTIONAL household-mic prefill hint
    (Wave-2 persistence, ``jasper.correction.household_mic``) — see
    ``DefaultSetupCalibration``. Omitted by default so existing callers are
    unaffected; the room and crossover level-match handlers in
    ``jasper/web/correction_setup.py`` pass one when a household record
    exists.
    """
    duration_ms = max(pre_roll_ms + post_roll_ms + 1000, int(hard_timeout_ms))
    if calibration_models is None:
        from jasper.audio_measurement.calibration import supported_model_options

        calibration_models = supported_model_options()
    return CaptureSpec(
        kind="level_ramp",
        duration_ms=duration_ms,
        pre_roll_ms=pre_roll_ms,
        post_roll_ms=post_roll_ms,
        constraints=CaptureConstraints(),  # all false → measurement-clean
        stimulus=CaptureStimulus(
            played_by="pi",
            label=f"{float(tone_frequency_hz):g} Hz level-match tone",
        ),
        run_token=run_token,
        validity=CaptureValidity(
            clean_capture="refuse",
            allow_capability_fallback=True,
            require_alignment=False,
            clock_drift="ignore",
        ),
        theme=build_theme(accent=accent, font=font),
        screen=(
            ui_heading(f"Level match — {geometry_label}"),
            ui_steps(
                [
                    placement_instruction
                    or f"Place the microphone at the {geometry_label}",
                    "Tap Start — the speaker rises slowly from quiet",
                    "Stay still; it locks the level automatically",
                ]
            ),
            ui_level_meter("mic"),
            ui_button("Start level check", action="begin_capture"),
            ui_button("Stop", action="stop"),
            ui_note("Keep the screen on — leaving this page stops the level match."),
        ),
        calibration_models=tuple(calibration_models),
        setup_validation=True,
        setup_binding_id=(setup_binding_id or (f"level-{run_token}" if run_token else "")),
        setup_collect_positions=setup_collect_positions,
        default_setup_calibration=default_setup_calibration,
        max_upload_bytes=max_upload_bytes,
        capture_protocol_version=2,
    ).validate()


# The kinds JTS ships a builder for today. The relay never sees this list — it is
# Pi-side only. Adding a kind appends one builder above; the relay and page need
# no change.
SHIPPED_KINDS = (
    "room_sweep",
    "balance_burst",
    "sync_marker",
    "crossover_sweep",
    "level_ramp",
)

BUILDERS = {
    "room_sweep": build_room_sweep_spec,
    "balance_burst": build_balance_burst_spec,
    "sync_marker": build_sync_marker_spec,
    "crossover_sweep": build_crossover_sweep_spec,
    "level_ramp": build_level_ramp_spec,
}
