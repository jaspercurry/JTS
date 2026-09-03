# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The validated sweep spec a commissioning capture session opens on.

One spec states everything a capture of one measurement kind needs: the
recording window, the mono/48 kHz format the analysis demands, the operator
consent surface, and — for a session-spanning walk — the
:class:`~jasper.capture_protocol.CapturePlan` the session follows. It is
built by a per-kind builder (:func:`build_crossover_sweep_spec` here),
validated strictly and loudly at the boundary, and handed to the session
open, which re-runs :meth:`CaptureSpec.validate` before a tone can play.

The plan shape itself — :class:`~jasper.capture_protocol.CapturePlan`,
:class:`~jasper.capture_protocol.CapturePlanEntry`,
:class:`~jasper.capture_protocol.CaptureSpecError` and the attempt ceiling —
is owned by :mod:`jasper.capture_protocol` and imported back here, so a
session builder can state a plan without this module.

Two boundaries are load-bearing and tested:

  1. **Kind-agnostic.** ``kind`` is an open string. The schema validates every
     other field but never enumerates kinds, so a new kind that fills the same
     fields validates with zero schema changes.
  2. **The consent surface is an allowlisted token vocabulary, not markup.**
     ``theme`` carries *tokens* that a renderer maps to fixed values, never raw
     CSS; ``screen`` is a list of known component types with escaped text. A
     builder refuses to emit anything outside the vocabulary, so a bug never
     ships a payload a renderer would have to reject.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from jasper.capture_protocol import (
    MAX_CAPTURE_PLAN_ATTEMPTS,
    CapturePlan,
    CapturePlanEntry,
    CaptureSpecError,
)
from jasper.capture_protocol import as_int as _as_int

# --- Contract constants -------------------------------------------------------

SCHEMA_VERSION = 1
# The capture choreography a spec is stated against, independent of the JSON
# schema above: additive fields stay schema-compatible while a choreography
# change (setup binding, level stream, session-spanning plans) does not. Every
# builder emits this value and a mismatch is a loud incompatibility, never a
# negotiated downgrade.
#
# What this integer does NOT encode: whether a session is session-spanning.
# That is carried by `capture_plan` presence alone (a `capture_plan` spec runs
# the plan loop; a plan-free spec runs one capture). Do not reintroduce a
# protocol-number test for plan-ness.
#
# Persisted placement proofs may carry an older value (see
# active_speaker.capture_geometry).
CAPTURE_PROTOCOL_VERSION = 3


# The format the measurement analysis demands of every capture
# (`jasper/web/correction_setup.py`: REQUIRED_SAMPLE_RATE, MAX_WAV_BODY_BYTES).
REQUIRED_SAMPLE_RATE_HZ = 48000
REQUIRED_CHANNELS = 1

# Theme is a TOKEN allowlist: a renderer maps each token to a fixed value and
# never interprets one as raw CSS.
THEME_ACCENTS = ("sage", "beige", "clay")
THEME_FONTS = ("figtree", "outfit")
DEFAULT_THEME = {"accent": "sage", "font": "figtree"}

# The consent surface's component vocabulary. A renderer draws exactly these
# types; anything else is rejected on both sides.
UI_COMPONENT_TYPES = ("heading", "steps", "level_meter", "button", "note")
UI_BUTTON_ACTIONS = ("begin_capture", "retry", "stop")
UI_METER_SOURCES = ("mic",)

# Per-kind measurement-validity policy vocabulary.
CLEAN_CAPTURE_POLICIES = ("refuse", "warn")
CLOCK_DRIFT_MODES = ("ignore", "single_window", "critical")

# `default_setup.calibration.mode` vocabulary: "serial" for a vendor lookup,
# "upload" for a bring-your-own file. There is no "none" — a household record
# is only ever written after a calibration successfully established, so the
# hint is either present and actionable or absent entirely (`default_setup`
# stays `None`). It describes how the ORIGINAL calibration was established.
DEFAULT_SETUP_CALIBRATION_MODES = ("serial", "upload")
DEFAULT_SETUP_CALIBRATION_KEYS = (
    "mode", "model", "serial_display", "calibration_id", "resolvable",
)

# The speaker is the only stimulus player; the microphone never plays anything.
STIMULUS_PLAYERS = ("pi",)

OUTPUT_FORMATS = ("wav",)


# --- Sub-records --------------------------------------------------------------


@dataclass(frozen=True)
class CaptureConstraints:
    """The capture device's processing switches, for a measurement-clean take.

    All default ``False``: echo cancellation, auto gain, noise suppression and
    voice isolation each silently *flatten the very level/spectral differences
    the measurement exists to find*, so for measurement we demand they be off.
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
    """What the speaker plays during the capture window.

    ``label`` is display/telemetry only — never trusted for logic. A ``None``
    stimulus on the spec means a passive record (no playback), e.g. a
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
    """Per-kind measurement-validity policy, carried as data on the spec.

      - ``clean_capture``: ``"refuse"`` or ``"warn"`` if the capture device did
        not honor the EC/AGC/NS=false constraints.
      - ``allow_capability_fallback``: if a clean capture is impossible on this
        device, degrade **gracefully and labeled** rather than dead-ending.
        Pairs with ``clean_capture="refuse"`` to mean "refuse the clean path,
        offer the labeled fallback."
      - ``require_alignment``: the owning analysis has a hard alignment gate (a
        weak/ambiguous result fails loud). False means alignment is absent or
        observation-only; it must not be set speculatively before a calibrated
        production gate exists.
      - ``clock_drift``: per-kind handling of independent mic/playback clock
        drift. ``"ignore"`` for magnitude FR and level work; ``"single_window"``
        for timing comparisons that must stay within one recording;
        ``"critical"`` reserved for the strictest sync paths. Deliberately
        per-flow: a timing marker and an acoustic sweep do not share a
        meaningful confidence scale.
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
    """A household's remembered measurement-mic calibration, as an OPTIONAL
    prefill hint — never binding.

    Populated from ``jasper.correction.household_mic`` when a prior session on
    this speaker established a calibration.

    ``resolvable`` is a SEPARATE, freshly-checked flag from the fact that this
    hint exists at all: ``calibration_id`` is re-resolved against the
    calibration store at spec-build time (see
    ``jasper.web.correction_setup._default_setup_calibration_for_spec``) and
    the flag is set only when THAT resolves cleanly, rather than trusting that
    an earlier resolve — used to build the hint's other fields — is still good.
    Defaults ``False`` and is omitted from the wire JSON in that case.
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


# schema_version 1 is the pre-entries shape (no `entries`, byte-identical to
# the original v3 contract); 2 is additive — per-capture heterogeneity
# (crossover-measurement-productization-design.md §5.7). A plan's
# schema_version and its `entries` presence are kept in strict lockstep by
# validation (`_validate_capture_plan_entries`) so a reader never has to
# re-derive one from the other.
CAPTURE_PLAN_SCHEMA_VERSIONS = (1, 2)
CAPTURE_PLAN_ENTRIES_SCHEMA_VERSION = 2
# Per-entry presentation copy is OPAQUE — the schema bounds its size and
# value types, never its keys/vocabulary — but a size ceiling keeps a spec
# from carrying an oversized payload.
MAX_CAPTURE_PLAN_ENTRY_SCREEN_BYTES = 4096


# --- Consent-surface builders (data, never markup) ----------------------------


def build_theme(accent: str = "sage", font: str = "figtree") -> dict[str, str]:
    """A theme = allowlisted *tokens* a renderer maps to fixed values."""
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
    """A kind-agnostic capture spec.

    Build one with a per-kind builder (:func:`build_crossover_sweep_spec`),
    serialize with ``to_dict()``, and reconstruct/validate inbound JSON with
    ``from_dict()``. ``validate()`` is called by ``from_dict()`` and may be
    called explicitly after a builder.
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
    sample_rate_hz: int = REQUIRED_SAMPLE_RATE_HZ
    channels: int = REQUIRED_CHANNELS
    output_format: str = "wav"
    acknowledgement: CaptureAcknowledgement | None = None
    # Optional household-mic prefill hint. See `DefaultSetupCalibration` —
    # never binding.
    default_setup_calibration: DefaultSetupCalibration | None = None
    # Session-spanning capture plan: one session covers a driver's whole repeat
    # SET. Presence — and ONLY presence — selects the plan loop over the
    # single-capture path.
    capture_plan: CapturePlan | None = None
    capture_protocol_version: int = CAPTURE_PROTOCOL_VERSION
    schema_version: int = SCHEMA_VERSION

    # -- serialization --

    def to_dict(self) -> dict[str, Any]:
        """The spec as JSON, for persistence and for the round-trip pin."""
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
            "ui": {
                "theme": dict(self.theme),
                "screen": [dict(component) for component in self.screen],
            },
            "acknowledgement": (
                self.acknowledgement.to_dict() if self.acknowledgement else None
            ),
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
            sample_rate_hz=_as_int(data, "sample_rate_hz", default=REQUIRED_SAMPLE_RATE_HZ),
            channels=_as_int(data, "channels", default=REQUIRED_CHANNELS),
            output_format=str(output.get("format", "wav")),
            acknowledgement=(
                CaptureAcknowledgement.from_dict(acknowledgement_raw)
                if isinstance(acknowledgement_raw, Mapping)
                else None
            ),
            default_setup_calibration=default_setup_calibration,
            capture_plan=(
                CapturePlan.from_dict(capture_plan_raw)
                if isinstance(capture_plan_raw, Mapping)
                else None
            ),
            # REQUIRED on the wire, with no default — a spec that states no
            # protocol is incompatible, not legacy. The dataclass field still
            # defaults, so builders stay ergonomic; the strictness belongs on
            # the parse boundary, not the constructor.
            capture_protocol_version=_as_int(data, "capture_protocol_version"),
            schema_version=_as_int(data, "schema_version", default=SCHEMA_VERSION),
        )
        # Guard against a screen entry that was not a Mapping (dropped above).
        if len(spec.screen) != len(screen):
            raise CaptureSpecError("every ui.screen entry must be an object")
        spec.validate()
        return spec

    # -- validation --

    def validate(self) -> CaptureSpec:
        """Strict, loud validation. Returns self so callers can chain."""
        if not self.kind or not isinstance(self.kind, str):
            raise CaptureSpecError("kind must be a non-empty string")
        if self.capture_protocol_version != CAPTURE_PROTOCOL_VERSION:
            raise CaptureSpecError(
                "capture_protocol_version must be "
                f"{CAPTURE_PROTOCOL_VERSION}, "
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
        _validate_acknowledgement(self.acknowledgement)
        _validate_capture_plan(self.capture_plan)
        if self.stimulus is not None and self.stimulus.played_by not in STIMULUS_PLAYERS:
            raise CaptureSpecError(
                f"stimulus.played_by must be one of {STIMULUS_PLAYERS}, "
                f"got {self.stimulus.played_by!r}"
            )
        _validate_validity(self.validity)
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
        return self

    def with_screen(self, *components: Mapping[str, Any]) -> CaptureSpec:
        """Return a copy whose `screen` is the given components (validated)."""
        return replace(self, screen=tuple(components)).validate()


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


def _validate_acknowledgement(
    acknowledgement: CaptureAcknowledgement | None,
) -> None:
    if acknowledgement is None:
        return
    if acknowledgement.schema_version != 1:
        raise CaptureSpecError("acknowledgement.schema_version must be 1")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", acknowledgement.id):
        raise CaptureSpecError("acknowledgement.id is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,96}", acknowledgement.binding_id):
        raise CaptureSpecError("acknowledgement.binding_id is invalid")
    if not acknowledgement.label or len(acknowledgement.label) > 360:
        raise CaptureSpecError("acknowledgement.label must be 1..360 characters")


def _validate_capture_plan(capture_plan: CapturePlan | None) -> None:
    # `capture_plan` is optional and its PRESENCE is the only session-spanning
    # signal — there is no protocol-number coupling. (Before the protocol-1/2
    # deletion this was a biconditional with protocol 3; with one protocol both
    # halves were vacuous, and requiring a plan would have broken every
    # plan-free flow — room_sweep, level_ramp, sync_marker.)
    if capture_plan is None:
        return
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


def _as_bool(data: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise CaptureSpecError(f"{key} must be a boolean, got {type(value).__name__}")
    return value


# Household-facing names for the commission tiers. The ids themselves
# (``crossover_v2_flow.TIER_FULL`` / ``TIER_EXPRESS`` / ``TIER_REMOTE``) are the
# flow's vocabulary; this is the only place they become copy, and an id with no
# entry here contributes no line rather than leaking a raw slug onto a consent
# screen.
#
# String literals rather than the flow's constants: ``crossover_v2_flow``
# imports this module's caller, so reaching back for them would close an import
# cycle. Kept in step with ``crossover_envelope_v2._TIER_LABELS`` by test.
_GUIDED_TIER_LABELS = {
    "full": "Full measurement",
    "express": "Quick tune",
    "remote": "Remote automated",
}


def _guided_tier_step(
    guided_tier: str, walk: int, capture_plan: CapturePlan | None,
) -> str:
    """The one tier line a guided consent screen adds, or ``""``.

    Both numbers are DERIVED — the capture count from the plan the household
    is about to walk, the duration from :meth:`CapturePlan.estimated_minutes`.
    Nothing here is hand-written, so this line can never quote a session other
    than the one about to run.

    **Both numbers are THIS SESSION's, which since the two-stage split is one
    stage of two.** The line therefore says so: an unqualified "Full
    measurement: 10 measurements, about 8 minutes" beside a chooser that quoted
    16 and 11 reads as a contradiction, when in fact it is the honest half. The
    chooser (``_tier_action``) owns the whole-journey figure; this owns the
    session in front of the household, and neither is allowed to state the
    other's. It stays deliberately silent about WHICH stage this is, because
    both stages render this same line.
    """
    label = _GUIDED_TIER_LABELS.get(str(guided_tier or "").strip().lower())
    if not label or not walk or capture_plan is None:
        return ""
    minutes = capture_plan.estimated_minutes()
    if not minutes:
        return ""
    return (
        f"{label}, this session: {walk} measurements, about {minutes} minutes"
    )


def _courtesy_beeps_step(announced: tuple[int, ...], walk: int) -> str:
    """WHAT THE SPEAKER DOES, for the session in front of the household.

    ``announced`` is the 1-based captures of this plan that open on the
    courtesy prelude, derived by the caller from the plan's own index → phase
    map (``crossover_v2_flow.announced_capture_indexes``). It is a *value*
    rather than a rule this module re-derives, because which phases announce is
    the measurement flow's decision and this module owns only how to say it.

    Three shapes are stateable: every capture, the first alone, and the first
    and the last. Shipped sessions produce the middle one (stage 2's walk); the
    other two are the shapes a plan change reaches — the first-and-last is what
    stage 1 renders with the lateral walk re-armed (its entry baseline plays
    stage 2's anchor object and therefore announces too), and every-capture is
    the pre-trim rule's own. **Anything else raises.** A consent screen that
    cannot describe what the speaker will do must not be rendered with a
    sentence that is nearly right — this is the exact defect the 2026-08-18 gate
    round found, where "The first measurement has three short beeps" was shipped
    against a stage 1 that beeps twice.

    "has"/"tones" are load-bearing and survive from #1979 — see the call site.
    """
    if not announced or announced[0] < 1 or announced[-1] > walk:
        raise CaptureSpecError(
            "announced_captures must be a non-empty subset of this plan's "
            f"1..{walk} captures, got {announced!r}"
        )
    loudness = (
        " — loud, but no louder than JTS needs to hear itself over the room"
    )
    if len(announced) == walk:
        return (
            "Each measurement has three short beeps, a pause, and then rising "
            f"tones{loudness}"
        )
    if announced == (1,):
        opener = "The first measurement has"
    elif announced == (1, walk):
        opener = "The first and last measurements each have"
    else:
        raise CaptureSpecError(
            f"no consent copy states beeps on captures {announced!r} of {walk}"
        )
    return (
        f"{opener} three short beeps and a pause; every measurement has "
        f"rising tones{loudness}"
    )


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
    ambient_duration_ms: int = 0,
    capture_plan: CapturePlan | None = None,
    guided_captures: int = 0,
    guided_tier: str = "",
    walk_shape: str = "",
    announced_captures: Sequence[int] = (),
    reverify_lead: str = "",
    default_setup_calibration: DefaultSetupCalibration | None = None,
) -> CaptureSpec:
    """`kind="crossover_sweep"` — per-driver frequency response for active
    crossover work: a clean log sweep, magnitude FR, drift-insensitive, with
    consent copy that names the driver under test so the household measures
    each driver in turn.

    ``stimulus_duration_ms`` defaults to the **kernel-side** sweep length the
    active-crossover flow actually plays —
    ``driver_acoustics.DEFAULT_DURATION_S`` — rather than a second, forked
    sweep constant. The driver/summed capture sweep is written and deconvolved
    from that one length (``web_measurement.capture_sweep_meta`` /
    ``write_driver_sweep_wav``), and the deconvolution reference is regenerated
    from the played ``sweep_meta``, so the spec must not advertise a different
    duration: the recording window is sized from this. Sourcing it here keeps
    ONE sweep definition.

    ``duration_ms`` is the HARD recording deadline, and its clock starts when
    the capture arms — before the sweep completes the speaker must load the
    commissioning config, generate the sweep WAV, play the full sweep, release
    the fan-in lane and roll the transient graph back. So the acoustic window
    is **floored by ``hard_timeout_ms``**: the normal stop is the sweep
    completing and the deadline is only the backstop, never the working margin.

    ``capture_plan`` opts the spec into a session-spanning walk: one session for
    the driver's whole repeat set. A plan requires an
    ``acknowledgement_binding``, because placement gates run per capture.

    ``guided_captures`` (> 0) declares that this summed session is a GUIDED
    SPATIAL CLOUD of that many prompted CAPTURES — the count the household
    counts down ("Measurement 4 of N"), NOT the smaller number of distinct mic
    positions the session thinks in. ``N`` is per SESSION, so the two-stage
    commission's two stages count separately; the shipped per-stage numbers
    come from ``crossover_v2_flow.tier_display_info()`` and are not restated
    here, where a plan change cannot reach them. The consent copy is written
    against captures because that is what the household is promising about: one
    held-still sweep each. It selects the consent surface to match — placement
    instruction, steps, button, and acknowledgement policy/label all describe a
    walk instead of a stationary mic — because the stationary copy makes a
    whole-session promise ("I will not move it") that a cloud asks the
    household to break on the very next screen. Per-sweep stillness stays
    promised in every shape, because that one is still true. ``0`` keeps the
    stationary copy, and that path stays reachable on purpose: the 1-entry
    re-verify re-arm really does keep the mic still for its whole session.

    ``guided_tier`` names WHICH guided instrument the household is consenting
    to. It adds exactly one line to the consent steps — the tier and the plan's
    own DERIVED duration (:meth:`CapturePlan.estimated_minutes`) — so a
    household can tell a quick tune from a full measurement before the first
    tone rather than by counting prompts afterwards. Only meaningful alongside
    ``guided_captures``; ignored otherwise.

    ``walk_shape`` is the ORIENTATION half of the guided consent screen: how far
    the walk reaches from the mark and that each position is prompted, in ONE
    sentence below the steps, so a household knows the shape of the session
    before the first tone instead of discovering it one prompt at a time.
    Supplied by the caller that owns the plan —
    :func:`~jasper.active_speaker.crossover_v2_flow.cloud_walk_shape`, derived
    from the same table the per-entry screens are built from — because this
    builder must not grow a second description of a walk it does not own.

    Deliberately ONE sentence, not a ``Sequence[str]`` of every position: a
    ten-item enumeration under a 73-word placement block is a wall, and a
    household cannot act on the last prompted move while standing at the first.
    The intent — no surprises — is kept by the sentence; the spoon-feeding is
    the per-entry screens' job. Empty renders nothing, and like ``guided_tier``
    it is only meaningful alongside ``guided_captures``.

    ``reverify_lead`` is an OPT-IN first step for the 1-entry re-verify re-arm:
    the recovery is one sweep back at the mark, and the 2026-07-27 hardware
    session abandoned it because no screen said so.

    ``default_setup_calibration`` is the OPTIONAL household-mic prefill hint
    (``jasper.correction.household_mic``). A ``crossover_sweep`` capture has no
    calibration-picker screen of its own, so without the hint every capture
    logged ``crossover_v2_uncalibrated_capture`` even when the household had a
    resolvable stored mic. It is applied silently when nothing has already been
    chosen for the session.
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
        CLOUD_WALK_PLACEMENT_POLICY_ID,
        DRIVER_CAPTURE_GEOMETRIES,
        DRIVER_PLACEMENT_POLICY_ID,
        REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
        SUMMED_PLACEMENT_POLICY_ID,
        cloud_walk_acknowledgement_label,
        cloud_walk_placement_instruction,
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
    # Plan SHAPE, not plan presence, selects the summed consent copy: a guided
    # cloud asks the household to move the mic between captures, so the
    # stationary policy's "I will not move it" promise would be false on the
    # very first screen. ``guided_captures == 0`` (every pre-cloud caller,
    # including the 1-entry re-verify re-arm, whose stationary promise is still
    # TRUE) keeps the byte-identical stationary copy and policy id.
    walk = int(guided_captures or 0)
    if walk < 0:
        raise CaptureSpecError("guided_captures must not be negative")
    if walk and is_driver:
        raise CaptureSpecError("guided_captures is a summed-capture shape")
    placement_instruction = (
        (
            reference_axis_driver_placement_instruction(driver_role)
            if geometry == "reference_axis"
            else driver_placement_instruction(driver_role)
        )
        if is_driver
        else cloud_walk_placement_instruction() if walk
        else summed_placement_instruction()
    )
    button_label = (
        (
            f"The mic is fixed on-axis — measure {driver_label}"
            if geometry == "reference_axis"
            else f"I’ve positioned the mic — measure {driver_label}"
        )
        if is_driver
        else "The mic is on the mark — start measuring" if walk
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
                else CLOUD_WALK_PLACEMENT_POLICY_ID if walk
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
                else cloud_walk_acknowledgement_label(walk) if walk
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
    steps: list[str] = []
    if reverify_lead:
        # §2.4 — the cheap thing, said first and loudest.
        steps.append(str(reverify_lead))
    tier_line = _guided_tier_step(guided_tier, walk, capture_plan)
    if tier_line:
        steps.append(tier_line)
    if walk:
        # WHAT TO BRING, before the session rather than during it (#1941 R2).
        # The 2026-07-30 field session reached the first prompted distance
        # holding neither a tape measure nor a stand, because nothing had said
        # to fetch either — expectation-setting that arrives at the moment it
        # is needed has already failed.
        #
        # The tape measure is stated as the CONSEQUENCE of a fact the
        # placement step used to carry unattached ("each one named with a
        # distance"): a fact that motivates an action reads once and sticks,
        # where the same fact floating in a placement paragraph reads as
        # trivia. The stand is a RECOMMENDATION, not a requirement — the
        # owner's ruling on #1941 Q3, and the honest shape: we cannot detect a
        # hand-held mic, so refusing to proceed is not on the table, and the
        # acknowledgement contract (``cloud_walk_acknowledgement_label``)
        # deliberately does not promise one. Its "why" is one clause and is
        # physical rather than exhortative, because "use a tripod!" without a
        # reason is the kind of instruction a household skips.
        steps.append(
            "Bring a tape measure or ruler — every position is named with a "
            "distance from the mark. A stand or tripod for the microphone is "
            "worth using: a hand and body near the capsule change what it "
            "hears, and a stand repeats a position better than a hand does"
        )
    steps.append(placement_instruction)
    if walk:
        # What the SPEAKER does, said before the first tone (work order D7 /
        # issue #1804). The household has been told how long it takes, what to
        # bring, and where to stand; this is the fourth thing an orientation
        # screen owes them — what they are about to hear — because an
        # unexplained burst of beeps at measurement level is the moment a
        # first-time household stops the session.
        #
        # It sits BEFORE "tap Start and stay quiet" rather than after the
        # whole block (#1941 R1): those seconds of silence are the seconds
        # this sentence describes, so a household that reads in order learns
        # what the noise will be before being asked to sit through it.
        #
        # Deliberately states no duration of its own: ``seconds`` is the
        # LONGEST plan entry's whole capture window (quiet window + beeps +
        # tone), which the step below already quotes honestly as the time to
        # stay quiet. Reusing it here would advertise a 40-second "tone" that
        # is nothing of the kind. "three" mirrors
        # ``jasper.audio_measurement.program.COURTESY_TONE_BEEP_COUNT``,
        # spelled out because a household counts beeps, and pinned by test.
        #
        # It says "HAS" and "TONES", and both words are load-bearing (#1979).
        # It used to say "is … a rising tone", which was false for the two
        # captures a household hears FIRST:
        #
        #   * "is" read as an exhaustive description of the whole measurement,
        #     but CHECK opens on a 12 s room-noise window
        #     (``program.DEFAULT_CHECK_AMBIENT_S``) BEFORE the beeps. "has"
        #     names the elements and their order without claiming nothing
        #     precedes them. This screen does not restate the window because
        #     per-phase narration belongs to the live capture surface, not to
        #     the consent screen.
        #   * no program in the session plays exactly ONE rising tone: CHECK
        #     plays four pilot chirps and no sweep at all, MEASURE plays two
        #     pilots then six sweeps (2 drivers × ``MEASURE_REPEAT_COUNT``),
        #     and a prompted cloud position plays two pilots then one sweep.
        #     The plural is the one shape true of all three; a count here would
        #     need three different sentences for three different phases.
        #
        # WHICH measurements beep is now DERIVED, not stated: since 2026-08-18
        # the courtesy prelude announces a SESSION rather than a capture
        # (``crossover_v2.programs.courtesy_prelude_for_phase``), so the beeps
        # and the tones are two different populations and the caller hands this
        # builder the first one. A hand-written "The first measurement…" was
        # shipped and was FALSE for stage 1, whose entry baseline announces too
        # — a consent screen that over-promises what the speaker will do is the
        # same defect as one that under-promises it, and a sentence that cannot
        # be checked against the plan is how either survives review.
        steps.append(
            _courtesy_beeps_step(tuple(int(i) for i in announced_captures), walk)
        )
    steps.extend(
        [
            (
                "Tap Start and stay quiet while JTS measures the room "
                f"noise, then plays about {seconds} seconds of sweep"
                if ambient_duration_ms
                else f"Tap Start, then stay quiet for about {seconds} seconds"
            ),
            # Per-sweep stillness is true in EVERY shape — it is the
            # whole-session promise the cloud breaks. The guided
            # wording adds what happens between sweeps so the household
            # is not surprised by the first move prompt.
            (
                "Keep the microphone still until each sweep finishes, then "
                "follow the on-screen prompt to move it"
                if walk
                else "Keep the microphone still until the sweep finishes"
            ),
        ]
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
            # A SUMMED capture measures the speaker, not a named driver, so
            # ``driver_label`` there is whatever the caller had to hand — the
            # v2 cloud passed the literal "crossover" and the household read
            # "Crossover — crossover" (flow-simplification §2.3). Name what is
            # about to happen instead; the per-driver flows keep their label,
            # which is genuinely informative for them.
            ui_heading(
                f"Crossover — {driver_label}" if is_driver else "Tune your speaker"
            ),
            ui_steps(steps),
            # The shape of the walk, in one sentence, between the steps and the
            # Start button (work order D7's intent, #1941 R1's presentation).
            # A ``note`` rather than a seventh step: it is not an instruction —
            # nothing here is for the household to DO — and the renderer's
            # component vocabulary is a closed allowlist, so this composes from
            # the existing types rather than widening it.
            #
            # It replaced a ``ui_note`` lead plus a SECOND ``ui_steps`` list of
            # every prompted position. Two stacked lists is the defect the
            # owner reported: the eye cannot tell which one it is meant to act
            # on, and the second was a walk's worth of moves the household
            # could not act on yet. Absent for every caller that passes no
            # shape, so no screen grows an empty section.
            *((ui_note(str(walk_shape)),) if walk and walk_shape else ()),
            # (A mic level meter does not belong here: every crossover
            # consent screen — the v2 cloud, the legacy per-driver sweeps,
            # and the 1-entry re-verify alike — feeds ``updateLevelMeters``
            # only from the level-ramp protocol, so the component would
            # never move and would read as a broken mic. The
            # ``ui_level_meter`` BUILDER stays — the level-ramp flow still
            # uses it.)
            ui_button(button_label, action="begin_capture"),
            ui_button("Stop", action="stop"),
            ui_note("Keep the screen on — leaving this page stops the recording."),
        ),
        acknowledgement=acknowledgement,
        capture_plan=capture_plan,
        default_setup_calibration=default_setup_calibration,
    ).validate()

