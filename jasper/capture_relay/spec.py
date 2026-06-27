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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

# --- Contract constants -------------------------------------------------------

SCHEMA_VERSION = 1

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
UI_BUTTON_ACTIONS = ("begin_capture", "retry")
UI_METER_SOURCES = ("mic",)

# Per-kind measurement-validity policy vocabulary (consumed in build steps 6+).
CLEAN_CAPTURE_POLICIES = ("refuse", "warn")
CLOCK_DRIFT_MODES = ("ignore", "single_window", "critical")

# The Pi is the only stimulus player today; the phone never plays anything.
STIMULUS_PLAYERS = ("pi",)

OUTPUT_FORMATS = ("wav",)


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
    sample_rate_hz: int = REQUIRED_SAMPLE_RATE_HZ
    channels: int = REQUIRED_CHANNELS
    output_format: str = "wav"
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    schema_version: int = SCHEMA_VERSION

    # -- serialization --

    def to_dict(self) -> dict[str, Any]:
        """The opaque JSON the relay stores and the page fetches.

        Shape mirrors `docs/phone-mic-relay-plan.md` §6, plus additive
        `schema_version` and `validity` fields.
        """
        return {
            "schema_version": self.schema_version,
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
        stimulus_raw = data.get("stimulus")
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
            max_upload_bytes=_as_int(
                data, "max_upload_bytes", default=DEFAULT_MAX_UPLOAD_BYTES
            ),
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
        _validate_theme(self.theme)
        _validate_screen(self.screen)
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
    position: int | None = None,
    total_positions: int | None = None,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> CaptureSpec:
    """Build the `kind="room_sweep"` capture spec (plan §6, build step 1).

    The record window is ``pre_roll + stimulus + post_roll`` so the Pi-played log
    sweep is guaranteed to land fully inside the phone's recording. ``pre_roll``
    must be at least the relay-poll latency plus margin — the Pi only plays the
    sweep *after* it sees the phone's ``armed`` flag, and the pre-roll absorbs
    the up-to-one-poll gap (this is a race to avoid, not a sync subtlety; see
    plan §5). Magnitude frequency response is drift-insensitive, so
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
    duration_ms = pre_roll_ms + stimulus_duration_ms + post_roll_ms

    if position is not None and total_positions:
        heading_text = f"Room measurement — position {position} of {total_positions}"
    else:
        heading_text = "Room measurement"
    seconds = round(stimulus_duration_ms / 1000)

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
        max_upload_bytes=max_upload_bytes,
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
    """
    duration_ms = pre_roll_ms + stimulus_duration_ms + post_roll_ms
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
    stimulus_duration_ms: int = 10000,
    pre_roll_ms: int = 800,
    post_roll_ms: int = 700,
    accent: str = "sage",
    font: str = "figtree",
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> CaptureSpec:
    """`kind="crossover_sweep"` — per-driver frequency response for active
    crossover work. Same acoustic shape as `room_sweep` (a clean log sweep,
    magnitude FR, drift-insensitive), but the copy names the driver under test
    (server-driven UI), so the household measures each driver in turn.
    """
    duration_ms = pre_roll_ms + stimulus_duration_ms + post_roll_ms
    seconds = round(stimulus_duration_ms / 1000)
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
                    "Hold the phone close to the speaker baffle",
                    f"Tap Start, then stay quiet for about {seconds} seconds",
                    "Keep the phone still until the sweep finishes",
                ]
            ),
            ui_level_meter("mic"),
            ui_button("Start", action="begin_capture"),
            ui_note("Keep the screen on — leaving this page stops the recording."),
        ),
        max_upload_bytes=max_upload_bytes,
    ).validate()


# The kinds JTS ships a builder for today. The relay never sees this list — it is
# Pi-side only. Adding a kind appends one builder above; the relay and page need
# no change.
SHIPPED_KINDS = (
    "room_sweep",
    "balance_burst",
    "sync_marker",
    "crossover_sweep",
)

BUILDERS = {
    "room_sweep": build_room_sweep_spec,
    "balance_burst": build_balance_burst_spec,
    "sync_marker": build_sync_marker_spec,
    "crossover_sweep": build_crossover_sweep_spec,
}
