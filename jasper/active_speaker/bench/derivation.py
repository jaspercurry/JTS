# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Emitter-generated active-speaker config → an offline-renderable config.

**Stage: DERIVE.** Owns exactly one transform: take the YAML text
:func:`jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config`
produced, swap its live ALSA devices for file-backed ones, and hand back the
result plus the per-branch facts the loop needs to read the render. Pure — no
subprocess, no filesystem, no analysis, no numpy.

**Units.** Frequencies in hertz; every level in decibels (``clip_limit`` and
the program headroom gain are dBFS / dB as CamillaDSP spells them); channel
indices are zero-based frame positions in the interleaved playback stream.

What it owns
------------
* The device swap (``Alsa`` → ``WavFile`` capture / ``File`` playback,
  ``enable_rate_adjust`` forced false) and the geometry validation that must
  refuse *before* a render rather than produce a silently wrong one.
* The admission gate: every pipeline step is a ``Filter`` or ``Mixer``, none
  bypassed, every referenced filter of an allowlisted type, no async resampler.
* Branch discovery: which pipeline step is each driver's own, which channels it
  targets, and what its limiter's clip limit is.

What it does NOT own
--------------------
* **The allowlist and the async-resampler predicate.** Both are imported from
  :mod:`jasper.bass_extension.bench.derivation`, which defined them first. They
  are general offline-render admission facts — "filter types whose offline
  behavior is exactly reproducible from the config text alone" — not
  bass-specific ones, and importing rather than re-declaring them is what keeps
  the two benches from admitting different stages.
  :class:`~jasper.bass_extension.bench.derivation.ArtifactHeader` is imported
  for the same reason: one shape for "what the stimulus file's header says".
* **Truncating the graph.** Unlike that bench's ``derive_truncated_config``,
  this derivation edits the ``pipeline``, ``filters``, and ``mixers`` blocks
  **not at all** — it asserts them byte-identical to the source. A branch is
  isolated at *extraction* time, by pulling its channel out of the interleaved
  playback frame (:func:`jasper.bass_extension.bench.render.extract_channel`),
  which is strictly safer than pipeline surgery: the graph that renders is the
  graph the emitter wrote.
* Rendering, comparison, verdicts, and the stimulus itself.

Why the capture keeps no ``channels`` key
-----------------------------------------
``CaptureDeviceWavFile`` (pinned CamillaDSP v4.1.3, ``src/config/mod.rs``) is
``{filename, extra_samples?, labels?}`` under ``#[serde(deny_unknown_fields)]``
— it has **no** ``channels`` field, and capture geometry comes from the WAV's
own header. So the live ``channels`` value is validated against the stimulus
header here and recorded in the receipt, never emitted as a key. The playback
side is the opposite: ``PlaybackDevice::File`` *declares* a required
``channels``, so it is kept at the live pipeline width. Both citations are the
sibling bench's, verified against the same pinned source tree; see its module
docstring for the full chain.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from jasper.active_speaker.camilla_yaml import driver_baseline_limiter_name
from jasper.active_speaker.graph_safety import view_from_emitted_text
from jasper.bass_extension.bench.derivation import (
    ALLOWED_FILTER_TYPES,
    ArtifactHeader,
)
from jasper.bass_extension.bench.derivation import (
    DerivationError as _SiblingDerivationError,
)
from jasper.bass_extension.bench.derivation import (
    assert_no_async_resampler as _assert_no_async_resampler,
)

#: The program-domain gain the emitter folds baseline headroom, room-correction
#: boost, and linearization boost into. Read (never written) here so the loop
#: can account for the level move a boosting linearization causes between the
#: two arms of its A/B.
PROGRAM_HEADROOM_FILTER = "active_baseline_headroom"


class EmitDerivationError(ValueError):
    """A derived render config could not be constructed, or failed its gate."""


@dataclass(frozen=True, slots=True)
class BranchStep:
    """One driver branch as the emitted graph actually declares it.

    Read off the graph text, never taken from the preset's own claim about
    what it asked for: the whole point of this bench is that the emitter's
    output is the subject under test.

    Args:
      role: the driver role (``"woofer"``, ``"tweeter"``, …).
      channels: the frame indices this branch's pipeline step targets. A mono
        layout gives one; a stereo layout gives the left/right pair, which
        carry identical filters.
      names: the branch's filter chain, in pipeline order.
      limiter_clip_limit_dbfs: the branch limiter's ``clip_limit``. CamillaDSP's
        ``Limiter`` soft clip is **always on**, not a threshold that engages
        only above the limit, so this is the reference level the loop's
        soft-clip budget is measured against (see
        :mod:`~jasper.active_speaker.bench.compare`).
    """

    role: str
    channels: tuple[int, ...]
    names: tuple[str, ...]
    limiter_clip_limit_dbfs: float

    @property
    def extract_channel(self) -> int:
        """The frame index the loop pulls this branch's samples from.

        The lowest of :attr:`channels`. Every channel in the set carries the
        identical filter chain (they are one pipeline step), so which one is
        read is arbitrary — it is fixed to the lowest so a report is
        reproducible rather than dependent on set iteration order.
        """

        return self.channels[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "channels": list(self.channels),
            "extract_channel": self.extract_channel,
            "names": list(self.names),
            "limiter_clip_limit_dbfs": self.limiter_clip_limit_dbfs,
        }


@dataclass(frozen=True, slots=True)
class DerivedRenderConfig:
    """One emitted config made renderable offline, plus what it renders into.

    ``receipt`` is a plain JSON-serializable mapping — a bundle file, never a
    field on a closed evidence schema.
    """

    yaml_text: str
    branches: tuple[BranchStep, ...]
    playback_channels: int
    sample_rate_hz: int
    program_headroom_db: float
    receipt: dict[str, Any]

    def branch(self, role: str) -> BranchStep:
        for step in self.branches:
            if step.role == role:
                return step
        raise EmitDerivationError(f"no branch for role {role!r} in the derived config")


def _parse(text: str) -> dict[str, Any]:
    if type(text) is not str or not text.strip():
        raise EmitDerivationError("emitted config text is empty")
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise EmitDerivationError(f"emitted config text did not parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise EmitDerivationError("emitted config text is not a mapping")
    return parsed


def _positive_int(value: Any, what: str) -> int:
    if type(value) is not int or value <= 0:
        raise EmitDerivationError(f"{what} is not a positive integer")
    return value


def _finite_float(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EmitDerivationError(f"{what} is not a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise EmitDerivationError(f"{what} is not finite")
    return number


def assert_no_async_resampler(devices: Mapping[str, Any]) -> None:
    """The sibling bench's async-resampler refusal, re-raised as OUR error type.

    The predicate is imported rather than re-declared (one owner of "an async
    resampler is not offline-reproducible"), but its exception is that module's
    ``DerivationError``, and every caller of this module is documented to catch
    :class:`EmitDerivationError`. Letting a foreign exception type out of one
    admission path and not the others would mean a refusal that escapes the
    loop's own handling — so it is translated at the boundary, here, once.
    """

    try:
        _assert_no_async_resampler(devices)
    except _SiblingDerivationError as exc:
        raise EmitDerivationError(str(exc)) from exc


def _assert_stage_allowlist(
    pipeline: Sequence[Any], filters: Mapping[str, Any]
) -> None:
    """Every step is an unbypassed ``Filter``/``Mixer`` over allowlisted types.

    Enforced over the WHOLE pipeline, because the whole pipeline renders: this
    derivation truncates nothing, so there is no narrower "retained prefix" to
    scope the gate to. A stage outside
    :data:`~jasper.bass_extension.bench.derivation.ALLOWED_FILTER_TYPES` is one
    whose offline behavior is not exactly reproducible from the config text, and
    a render containing one would grade an unreproducible transform as if it
    were the filters under test.
    """

    for step in pipeline:
        if not isinstance(step, Mapping):
            raise EmitDerivationError("pipeline step is not a mapping")
        step_type = step.get("type")
        if step_type not in ("Filter", "Mixer"):
            raise EmitDerivationError(
                f"pipeline step type {step_type!r} is not Filter or Mixer"
            )
        if step.get("bypassed"):
            raise EmitDerivationError("pipeline step is bypassed")
        if step_type != "Filter":
            continue
        names = step.get("names")
        if not isinstance(names, list):
            raise EmitDerivationError("Filter step names is not a list")
        for name in names:
            spec = filters.get(name)
            ftype = spec.get("type") if isinstance(spec, Mapping) else None
            if ftype is None:
                raise EmitDerivationError(f"filter {name!r} has no definition")
            if ftype not in ALLOWED_FILTER_TYPES:
                raise EmitDerivationError(
                    f"filter {name!r} has type {ftype!r}, outside the offline "
                    "render stage allowlist"
                )


def _assert_graph_preserved(source_text: str, derived: Mapping[str, Any]) -> None:
    """``filters``/``mixers``/``pipeline`` are identical to the source.

    Re-parses the ORIGINAL TEXT rather than comparing against the mapping this
    module already parsed: the derived blocks are the same objects by
    reference (this derivation never edits them), so comparing them to that
    mapping would compare each object to itself — an ``==`` that can never be
    false. Re-parsing from the immutable source is genuinely independent, and
    it is what would catch a future in-place mutation by this module or a
    caller. Same argument, and same shape, as the sibling bench's
    ``_assert_filters_mixers_identical``.
    """

    reparsed = _parse(source_text)
    for key in ("filters", "mixers", "pipeline"):
        if reparsed.get(key) != derived.get(key):
            raise EmitDerivationError(
                f"derived {key} block differs from the emitted {key} block"
            )


def _branch_steps(
    text: str, pipeline: Sequence[Any], roles: Sequence[str]
) -> tuple[BranchStep, ...]:
    """Discover each role's own pipeline step, by its baseline limiter's name.

    The limiter is the marker rather than the channel set, because a channel
    set is ambiguous by construction: the program-domain headroom step targets
    ``[0, 1]``, which a 1-way stereo preset's single driver step also targets.
    Every driver chain ends with ``driver_baseline_limiter_name(role)`` — a
    name the EMITTER owns and re-exports — so matching on it identifies the
    branch exactly and, as a side effect, proves the limiter is still there.

    ``view_from_emitted_text`` is used for the filter definitions because it
    deliberately parses only the JTS emitter's own dialect and not CamillaDSP's
    re-serialized one: reading the clip limit through it makes emitter drift a
    refusal here rather than a surprise on device.
    """

    view = view_from_emitted_text(text)
    steps: list[BranchStep] = []
    for role in roles:
        limiter = driver_baseline_limiter_name(role)
        matches = [
            step
            for step in pipeline
            if isinstance(step, Mapping)
            and step.get("type") == "Filter"
            and isinstance(step.get("names"), list)
            and limiter in step["names"]
        ]
        if len(matches) != 1:
            raise EmitDerivationError(
                f"expected exactly one pipeline step carrying {limiter!r}, "
                f"found {len(matches)}"
            )
        step = matches[0]
        raw_channels = step.get("channels")
        if not isinstance(raw_channels, list) or not raw_channels:
            raise EmitDerivationError(
                f"branch step for role {role!r} has no channels list"
            )
        channels = tuple(
            sorted(
                _nonnegative_int(channel, f"{role} branch channel")
                for channel in raw_channels
            )
        )
        definition = view.filters.get(limiter)
        if definition is None or definition.type != "Limiter":
            raise EmitDerivationError(
                f"{limiter!r} is not a Limiter in the emitted graph"
            )
        clip_limit = _finite_float(
            definition.params.get("clip_limit"), f"{limiter}.clip_limit"
        )
        if clip_limit > 0.0:
            raise EmitDerivationError(f"{limiter!r} clip_limit is above 0 dBFS")
        steps.append(
            BranchStep(
                role=str(role),
                channels=channels,
                names=tuple(str(name) for name in step["names"]),
                limiter_clip_limit_dbfs=clip_limit,
            )
        )
    return tuple(steps)


def _nonnegative_int(value: Any, what: str) -> int:
    if type(value) is not int or value < 0:
        raise EmitDerivationError(f"{what} is not a non-negative integer")
    return value


def _program_headroom_db(text: str) -> float:
    """The emitted ``active_baseline_headroom`` gain, dB (``0.0`` when absent).

    Absent is legitimate — the driver-domain (follower) graph carries no
    program-domain headroom at all — and ``0.0`` is the correct reading of that
    absence, since there is then no pre-split attenuation for the A/B's two
    arms to differ by.
    """

    view = view_from_emitted_text(text)
    definition = view.filters.get(PROGRAM_HEADROOM_FILTER)
    if definition is None:
        return 0.0
    if definition.type != "Gain":
        raise EmitDerivationError(
            f"{PROGRAM_HEADROOM_FILTER!r} is not a Gain in the emitted graph"
        )
    return _finite_float(
        definition.params.get("gain"), f"{PROGRAM_HEADROOM_FILTER}.gain"
    )


def capture_geometry(emitted_text: str) -> tuple[int, int]:
    """``(sample_rate_hz, capture_channels)`` the emitted config expects.

    The loop needs both BEFORE it can build a stimulus file, and the stimulus's
    own header is then what :func:`derive_offline_render_config` validates
    against — so this reads the same two live values from the same block, and
    the derivation's check stays an independent re-proof rather than a
    tautology (it compares the artifact's header, not this return value).
    """

    devices = _parse(emitted_text).get("devices")
    if not isinstance(devices, Mapping):
        raise EmitDerivationError("emitted config has no devices mapping")
    capture = devices.get("capture")
    if not isinstance(capture, Mapping):
        raise EmitDerivationError("emitted devices.capture is not a mapping")
    return (
        _positive_int(devices.get("samplerate"), "devices.samplerate"),
        _positive_int(capture.get("channels"), "devices.capture.channels"),
    )


def _derive_devices(
    live_devices: Mapping[str, Any],
    *,
    capture_filename: str,
    capture_header: ArtifactHeader,
    playback_filename: str,
    processing_precision: str,
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    """Swap the devices block; refuse before any render on a geometry mismatch.

    Returns ``(derived_devices, receipt_diff, sample_rate_hz, playback_channels)``.
    """

    live_capture = live_devices.get("capture")
    live_playback = live_devices.get("playback")
    if not isinstance(live_capture, Mapping):
        raise EmitDerivationError("emitted devices.capture is not a mapping")
    if not isinstance(live_playback, Mapping):
        raise EmitDerivationError("emitted devices.playback is not a mapping")

    sample_rate = _positive_int(live_devices.get("samplerate"), "devices.samplerate")
    capture_channels = _positive_int(
        live_capture.get("channels"), "devices.capture.channels"
    )
    playback_channels = _positive_int(
        live_playback.get("channels"), "devices.playback.channels"
    )

    if capture_header.sample_rate_hz != sample_rate:
        raise EmitDerivationError(
            f"stimulus sample rate ({capture_header.sample_rate_hz}) does not "
            f"match devices.samplerate ({sample_rate})"
        )
    if capture_header.channels != capture_channels:
        raise EmitDerivationError(
            f"stimulus channel count ({capture_header.channels}) does not match "
            f"devices.capture.channels ({capture_channels})"
        )
    if capture_header.bits_per_sample != 16:
        raise EmitDerivationError(
            "stimulus is not 16-bit PCM "
            f"(got {capture_header.bits_per_sample}-bit)"
        )

    derived_capture: dict[str, Any] = {
        "type": "WavFile",
        "filename": capture_filename,
    }
    if "labels" in live_capture:
        derived_capture["labels"] = live_capture["labels"]
    derived_playback: dict[str, Any] = {
        "type": "File",
        "channels": playback_channels,
        "filename": playback_filename,
        "format": processing_precision,
    }

    derived: dict[str, Any] = {}
    for key, value in live_devices.items():
        if key == "capture":
            derived[key] = derived_capture
        elif key == "playback":
            derived[key] = derived_playback
        elif key == "enable_rate_adjust":
            derived[key] = False
        else:
            derived[key] = value
    derived.setdefault("enable_rate_adjust", False)

    diff = {
        "capture_type_live": live_capture.get("type"),
        "capture_type_derived": "WavFile",
        "capture_device_key_live": live_capture.get("device"),
        "capture_format_key_live": live_capture.get("format"),
        "capture_channels_live_validated_only": capture_channels,
        "playback_type_live": live_playback.get("type"),
        "playback_type_derived": "File",
        "playback_device_key_live": live_playback.get("device"),
        "playback_format_live": live_playback.get("format"),
        "playback_format_derived": processing_precision,
        "playback_channels": playback_channels,
        "enable_rate_adjust_live": live_devices.get("enable_rate_adjust"),
        "enable_rate_adjust_derived": False,
    }
    return derived, diff, sample_rate, playback_channels


def derive_offline_render_config(
    emitted_text: str,
    *,
    roles: Sequence[str],
    capture_filename: str,
    capture_header: ArtifactHeader,
    playback_filename: str,
    processing_precision: str,
) -> DerivedRenderConfig:
    """Make one emitter-generated config renderable offline.

    ``emitted_text`` MUST be the exact text
    :func:`jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config`
    returned. This function never reads a config from disk and never talks to a
    running daemon: the subject of the bench is what the emitter wrote, so
    anything else would be grading a different artifact.

    ``roles`` is the preset's driver roles
    (:func:`jasper.active_speaker.profile.required_driver_roles`) — the roles a
    branch is wanted for, in report order. Each one must resolve to exactly one
    pipeline step or this refuses.

    Raises:
      EmitDerivationError: on any refusal. Every failure path here is a refusal
        before a render, never a degraded render.
    """

    live = _parse(emitted_text)
    devices = live.get("devices")
    pipeline = live.get("pipeline")
    filters = live.get("filters")
    if not isinstance(devices, Mapping):
        raise EmitDerivationError("emitted config has no devices mapping")
    if not isinstance(pipeline, list):
        raise EmitDerivationError("emitted config has no pipeline list")
    if not isinstance(filters, Mapping):
        raise EmitDerivationError("emitted config has no filters mapping")
    if not roles:
        raise EmitDerivationError("roles must be non-empty")

    assert_no_async_resampler(devices)
    _assert_stage_allowlist(pipeline, filters)

    derived_devices, device_diff, sample_rate, playback_channels = _derive_devices(
        devices,
        capture_filename=capture_filename,
        capture_header=capture_header,
        playback_filename=playback_filename,
        processing_precision=processing_precision,
    )

    derived: dict[str, Any] = {}
    for key, value in live.items():
        derived[key] = derived_devices if key == "devices" else value
    _assert_graph_preserved(emitted_text, derived)

    branches = _branch_steps(emitted_text, pipeline, roles)
    for branch in branches:
        if branch.channels[-1] >= playback_channels:
            raise EmitDerivationError(
                f"branch {branch.role!r} targets channel {branch.channels[-1]} "
                f"outside the {playback_channels}-channel playback frame"
            )

    yaml_text = yaml.safe_dump(derived, sort_keys=False)
    receipt: dict[str, Any] = {
        "roles": [branch.role for branch in branches],
        "branches": [branch.to_dict() for branch in branches],
        "device_diff": device_diff,
        "processing_precision": processing_precision,
        "sample_rate_hz": sample_rate,
        "program_headroom_db": _program_headroom_db(emitted_text),
    }
    return DerivedRenderConfig(
        yaml_text=yaml_text,
        branches=branches,
        playback_channels=playback_channels,
        sample_rate_hz=sample_rate,
        program_headroom_db=float(receipt["program_headroom_db"]),
        receipt=receipt,
    )


__all__ = [
    "PROGRAM_HEADROOM_FILTER",
    "BranchStep",
    "DerivedRenderConfig",
    "EmitDerivationError",
    "assert_no_async_resampler",
    "capture_geometry",
    "derive_offline_render_config",
]
