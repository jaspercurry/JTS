# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The validated sweep spec a commissioning capture session opens on.

One spec states everything a capture of one measurement kind needs: the
recording window, the mono/48 kHz format the analysis demands, the operator
acknowledgement, and — for a session-spanning walk — the
:class:`~jasper.capture_protocol.CapturePlan`. It is built by a per-kind builder
(:func:`build_crossover_sweep_spec` here), validated strictly and loudly at the
boundary, and re-validated at session open before a tone can play. The plan
shape itself is owned by :mod:`jasper.capture_protocol`.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from jasper.active_speaker.capture_geometry import (
    CLOUD_WALK_PLACEMENT_POLICY_ID,
    DRIVER_CAPTURE_GEOMETRIES,
    DRIVER_PLACEMENT_POLICY_ID,
    REFERENCE_AXIS_DRIVER_PLACEMENT_POLICY_ID,
    SUMMED_PLACEMENT_POLICY_ID,
    cloud_walk_acknowledgement_label,
    placement_acknowledgement_label,
    reference_axis_driver_acknowledgement_label,
    summed_acknowledgement_label,
)
from jasper.capture_protocol import (
    MAX_CAPTURE_PLAN_ATTEMPTS,
    CapturePlan,
    CapturePlanEntry,
    CaptureSpecError,
)

# --- Contract constants -------------------------------------------------------

SCHEMA_VERSION = 1
# The capture choreography a spec is stated against, independent of the JSON
# schema above: additive fields stay schema-compatible while a choreography
# change does not. A mismatch is a loud incompatibility, never a negotiated
# downgrade. It does NOT encode whether a session is session-spanning — that is
# carried by `capture_plan` presence alone. Persisted placement proofs may carry
# an older value (see active_speaker.capture_geometry).
CAPTURE_PROTOCOL_VERSION = 3


# The format the measurement analysis demands of every capture
# (`jasper/web/correction_runtime.py`: MAX_WAV_BODY_BYTES caps the upload).
REQUIRED_SAMPLE_RATE_HZ = 48000
REQUIRED_CHANNELS = 1

# Per-kind measurement-validity policy vocabulary.
CLEAN_CAPTURE_POLICIES = ("refuse", "warn")
CLOCK_DRIFT_MODES = ("ignore", "single_window", "critical")

# `default_setup.calibration.mode` vocabulary. There is no "none": a household
# record is only written after a calibration successfully established, so the
# hint is either present and actionable or absent entirely. It describes how the
# ORIGINAL calibration was established.
DEFAULT_SETUP_CALIBRATION_MODES = ("serial", "upload")

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


@dataclass(frozen=True)
class CaptureStimulus:
    """What the speaker plays during the capture window.

    ``label`` is display/telemetry only — never trusted for logic. A ``None``
    stimulus on the spec means a passive record (no playback).
    """

    played_by: str = "pi"
    label: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"played_by": self.played_by, "label": self.label}


@dataclass(frozen=True)
class CaptureValidity:
    """Per-kind measurement-validity policy, carried as data on the spec.

      - ``clean_capture``: ``"refuse"`` or ``"warn"`` if the capture device did
        not honor the EC/AGC/NS=false constraints.
      - ``allow_capability_fallback``: if a clean capture is impossible on this
        device, degrade gracefully and LABELED rather than dead-ending.
      - ``require_alignment``: the owning analysis has a hard alignment gate.
        False means alignment is absent or observation-only, and it must not be
        set before a calibrated production gate exists.
      - ``clock_drift``: per-kind handling of independent mic/playback clock
        drift. ``"ignore"`` for magnitude FR and level work; ``"single_window"``
        for timing comparisons that must stay within one recording;
        ``"critical"`` for the strictest sync paths. Per-flow because a timing
        marker and an acoustic sweep do not share a confidence scale.
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


@dataclass(frozen=True)
class DefaultSetupCalibration:
    """A household's remembered measurement-mic calibration, as an OPTIONAL
    prefill hint — never binding.

    ``resolvable`` is a SEPARATE, freshly-checked flag from the fact that the
    hint exists: ``calibration_id`` is re-resolved against the calibration store
    at spec-build time and the flag is set only when THAT resolves cleanly,
    rather than trusting the earlier resolve that built the other fields.
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
    """A kind-agnostic capture spec, built by a per-kind builder."""

    kind: str
    duration_ms: int
    pre_roll_ms: int
    post_roll_ms: int
    constraints: CaptureConstraints = field(default_factory=CaptureConstraints)
    stimulus: CaptureStimulus | None = None
    validity: CaptureValidity = field(default_factory=CaptureValidity)
    sample_rate_hz: int = REQUIRED_SAMPLE_RATE_HZ
    channels: int = REQUIRED_CHANNELS
    output_format: str = "wav"
    acknowledgement: CaptureAcknowledgement | None = None
    # Optional household-mic prefill hint — never binding.
    default_setup_calibration: DefaultSetupCalibration | None = None
    # Session-spanning capture plan. Presence — and ONLY presence — selects the
    # plan loop over the single-capture path.
    capture_plan: CapturePlan | None = None
    capture_protocol_version: int = CAPTURE_PROTOCOL_VERSION
    schema_version: int = SCHEMA_VERSION

    # -- serialization --

    def to_dict(self) -> dict[str, Any]:
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
        # Kinds are deliberately NOT enumerated: validate the *shape*, never the
        # *vocabulary* of kind.
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
        return self


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
    # signal — there is no protocol-number coupling.
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
    ambient_duration_ms: int = 0,
    capture_plan: CapturePlan | None = None,
    guided_captures: int = 0,
    default_setup_calibration: DefaultSetupCalibration | None = None,
) -> CaptureSpec:
    """`kind="crossover_sweep"` — per-driver frequency response for active
    crossover work: a clean log sweep, magnitude FR, drift-insensitive.

    ``stimulus_duration_ms`` defaults to the KERNEL-side sweep length the
    active-crossover flow actually plays (``driver_acoustics.DEFAULT_DURATION_S``)
    rather than a second, forked sweep constant. The capture sweep is written
    and deconvolved from that one length and the deconvolution reference is
    regenerated from the played ``sweep_meta``, so the spec must not advertise a
    different duration: the recording window is sized from this.

    ``duration_ms`` is the HARD recording deadline, and its clock starts when the
    capture arms — before the sweep completes the speaker must load the
    commissioning config, generate the sweep WAV, play the full sweep, release
    the fan-in lane and roll the transient graph back. The acoustic window is
    therefore FLOORED by ``hard_timeout_ms``: the normal stop is the sweep
    completing, and the deadline is only the backstop.

    ``capture_plan`` opts the spec into a session-spanning walk. It requires an
    ``acknowledgement_binding``, because placement gates run per capture.

    ``guided_captures`` (> 0) declares a GUIDED SPATIAL CLOUD of that many
    prompted CAPTURES — the count the household counts down, NOT the smaller
    number of distinct mic positions the session thinks in. It, not plan
    presence, selects the walk acknowledgement, because the stationary one
    promises "I will not move it", which a cloud asks the household to break.
    ``0`` keeps the stationary acknowledgement.

    ``default_setup_calibration`` is the OPTIONAL household-mic prefill hint. A
    ``crossover_sweep`` capture has no calibration-picker screen of its own, so
    without the hint every capture logged
    ``crossover_v2_uncalibrated_capture`` even with a resolvable stored mic. It
    is applied silently when nothing has already been chosen for the session.
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
    is_driver = str(driver_role or "").strip().lower() not in {"", "summed"}
    geometry = str(driver_capture_geometry or "").strip().lower()
    if is_driver and geometry not in DRIVER_CAPTURE_GEOMETRIES:
        raise CaptureSpecError("driver capture geometry is unsupported")
    walk = int(guided_captures or 0)
    if walk < 0:
        raise CaptureSpecError("guided_captures must not be negative")
    if walk and is_driver:
        raise CaptureSpecError("guided_captures is a summed-capture shape")
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
        acknowledgement=acknowledgement,
        capture_plan=capture_plan,
        default_setup_calibration=default_setup_calibration,
    ).validate()

