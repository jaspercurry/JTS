# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime setting vocabulary, precedence, and buffer rules.
Route-policy vocabulary and statefile constants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence, TypedDict

from jasper.audio_hardware.dac import latency_floor_for
from jasper.audio_runtime_overrides import (
    DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    RuntimeOverrideEntry,
)
from jasper.usbgadget import UAC2_CARD_NAME


OUTPUTD_PERIOD_KEY = "JASPER_OUTPUTD_PERIOD_FRAMES"
OUTPUTD_DAC_BUFFER_KEY = "JASPER_OUTPUTD_DAC_BUFFER_FRAMES"
OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER = 2
DEFAULT_OUTPUTD_PERIOD_FRAMES = 1024
DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES = 3072
# How the two defaults above are NAMED to an operator: nothing writes them, so
# there is no file to edit — they are outputd's own compile-time defaults
# (rust/jasper-outputd/src/config.rs, pinned equal by
# test_packaged_outputd_defaults_match_the_rust_daemon).
PACKAGED_OUTPUTD_DEFAULT_SOURCE = "packaged outputd default"
FANIN_INPUT_BUFFER_KEY = "JASPER_FANIN_INPUT_BUFFER_FRAMES"
DEFAULT_FANIN_INPUT_BUFFER_FRAMES = 4096
# RETIRED: fan-in never read this selector. Keep the unset until deployed
# boxes no longer carry it, then remove both together.
RETIRED_FANIN_INPUT_RESAMPLER_KEY = "JASPER_FANIN_INPUT_RESAMPLER"
FANIN_INPUT_RESAMPLER_LANE_KEY = "JASPER_FANIN_INPUT_RESAMPLER_LANE"
FANIN_INPUT_RESAMPLER_TARGET_KEY = "JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES"
FANIN_INPUT_RESAMPLER_MAX_ADJUST_KEY = "JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM"
FANIN_INPUT_RESAMPLER_CUSHION_KEY = "JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES"
FANIN_INPUT_RESAMPLER_RING_KEY = "JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES"
FANIN_USB_DIRECT_PERIOD_KEY = "JASPER_FANIN_USB_DIRECT_PERIOD_FRAMES"
FANIN_USB_DIRECT_DEVICE = f"hw:{UAC2_CARD_NAME}"
DEFAULT_FANIN_USB_DIRECT_PERIOD_FRAMES = 256
MIN_FANIN_USB_DIRECT_PERIOD_FRAMES = 32
MAX_FANIN_USB_DIRECT_PERIOD_FRAMES = 1024
FANIN_USB_DIRECT_MIN_BUFFER_FRAMES = 768
FANIN_USB_DIRECT_MIN_BUFFER_PERIODS = 3
DEFAULT_USB_LOW_LATENCY_RESAMPLER_TARGET_FRAMES = 512
DEFAULT_USB_LOW_LATENCY_RESAMPLER_MAX_ADJUST_PPM = 500
DEFAULT_USB_LOW_LATENCY_RESAMPLER_CUSHION_FRAMES = 1536
DEFAULT_USB_LOW_LATENCY_RESAMPLER_RING_FRAMES = 4096


OUTPUTD_LATENCY_KEYS = (
    "JASPER_CAMILLA_CHUNKSIZE",
    "JASPER_CAMILLA_TARGET_LEVEL",
    OUTPUTD_PERIOD_KEY,
    OUTPUTD_DAC_BUFFER_KEY,
)
AUDIO_RUNTIME_OVERRIDE_KEYS = frozenset(
    OUTPUTD_LATENCY_KEYS + (FANIN_INPUT_BUFFER_KEY,)
)


SourceKind = Literal[
    "operator_env",
    "generated_env",
    "device_profile",
    "packaged_default",
    "lab_override",
]


MAX_LOW_LATENCY_CORRECTION_GROUP_DELAY_FRAMES = 512
AUDIO_ROUTE_PROFILE_KEY = "JASPER_AUDIO_ROUTE_PROFILE"
ROUTE_CORRECTED_48K = "corrected_48k"
ROUTE_USB_LOW_LATENCY_48K = "usb_low_latency_48k"
USB_LOW_LATENCY_SOURCE_ID = "usbsink"
ROUTE_CONFIG_HASH_SCHEMA_VERSION = 5
UAC2_LOW_LATENCY_EXPECTED_ATTRS = {
    "c_sync": "async",
    "req_number": "2",
    "c_hs_bint": "1",
}

BASE_ENV_PROCESS_FALLBACK_KEYS = frozenset(
    AUDIO_RUNTIME_OVERRIDE_KEYS
    | {
        AUDIO_ROUTE_PROFILE_KEY,
        FANIN_USB_DIRECT_PERIOD_KEY,
    }
)

RouteMode = Literal[
    "solo",
    "active_leader",
    "active_follower",
    "invalid_grouping",
    "unknown",
]

VALID_ROUTE_MODES = {
    "solo",
    "active_leader",
    "active_follower",
    "invalid_grouping",
    "unknown",
}

VALID_AUDIO_ROUTE_PROFILES = {
    ROUTE_CORRECTED_48K,
    ROUTE_USB_LOW_LATENCY_48K,
}


class EmitSoundConfigKwargs(TypedDict, total=False):
    """Subset of ``emit_sound_config`` kwargs owned by runtime routing."""

    room_peqs_right: Any
    channel_delays_ms: Any
    playback_pipe_path: str | None
    # Ring (shm_ring) coupling names its CamillaDSP capture/playback devices via
    # ALSA ioplug devices (jts_ring_capture, plus jts_ring_playback or — on an
    # armed roleful box — jts_ring_active_playback), so BOTH device and format
    # ride the coupling kwargs.
    capture_device: str
    capture_format: str
    playback_device: str
    playback_format: str
    chunksize: int
    target_level: int
    queuelimit: int


@dataclass(frozen=True)
class RuntimeSetting:
    """One resolved runtime knob with provenance and drift notes."""

    key: str
    value: int | str
    source_kind: SourceKind
    source: str
    unit: str = ""
    override_value: str | None = None
    generated_value: str | None = None
    operator_value: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "value": self.value,
            "source_kind": self.source_kind,
            "source": self.source,
        }
        if self.unit:
            out["unit"] = self.unit
        if self.override_value is not None:
            out["override_value"] = self.override_value
        if self.operator_value is not None:
            out["operator_value"] = self.operator_value
        if self.generated_value is not None:
            out["generated_value"] = self.generated_value
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out


@dataclass(frozen=True)
class RuntimeEnvAction:
    """One reconciler env-file action decided by the runtime plan."""

    action: Literal["set", "unset"]
    key: str
    value: str = ""

    def to_dict(self) -> dict[str, str]:
        out = {"action": self.action, "key": self.key}
        if self.action == "set":
            out["value"] = self.value
        return out


def minimum_outputd_buffer_frames(period_frames: int) -> int:
    """Minimum outputd ALSA buffer for one period, matching Rust validation."""

    return period_frames * OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER


def outputd_buffer_pair_error(
    *,
    buffer_name: str,
    buffer_frames: int,
    period_name: str,
    period_frames: int,
) -> str | None:
    """Return Rust-shaped detail when an outputd buffer/period pair is invalid."""

    min_buffer_frames = minimum_outputd_buffer_frames(period_frames)
    if buffer_frames >= min_buffer_frames:
        return None
    return (
        f"{buffer_name}={buffer_frames} must be >= "
        f"{OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER} x {period_name}={period_frames} "
        "(minimum ALSA jitter margin)"
    )


def outputd_dac_buffer_pair_error(
    *,
    period_frames: int,
    dac_buffer_frames: int,
) -> str | None:
    """Return the DAC-buffer invariant error that maps to outputd exit 78."""

    return outputd_buffer_pair_error(
        buffer_name=OUTPUTD_DAC_BUFFER_KEY,
        buffer_frames=dac_buffer_frames,
        period_name=OUTPUTD_PERIOD_KEY,
        period_frames=period_frames,
    )


OUTPUTD_ENV_LAYER = 1


def _pair_provenance(
    *,
    buffer_key: str,
    buffer_frames: int,
    buffer_layer: int | None,
    period_frames: int,
    period_layer: int | None,
    labels: tuple[str, str],
    override_entries: Mapping[str, RuntimeOverrideEntry],
    override_label: str,
) -> str:
    """Name where each half of a failing buffer/period pair actually came from.

    The Rust-shaped detail names the two KEYS, which is enough for the daemon
    (it reads one merged environment) and not enough for an operator, who has to
    edit one of several layers. Naming the layer is what turns the refusal into
    an action, and it matters most in the two cases that read as a
    contradiction:

    - the reconciler UNSETS a key from ``outputd.env`` whenever ``jasper.env``
      owns it, so an operator told only "this key is wrong" looks in the
      reconciler-owned file, finds the key absent, and is stuck;
    - a value the LAB OVERRIDE STORE owns is WRITTEN INTO ``outputd.env`` by the
      latency-floor pass (``outputd_latency_floor_actions`` emits ``set`` when
      the store holds the key), so deleting that line is futile — the next
      reconcile writes it straight back. Naming only the file would send an
      operator into exactly that loop.

    Neither is hypothetical: the second is jts.local's live #2489 state, whose
    store entry carries its own ``created_at`` and ``reason``. Those are quoted
    here because they are the self-explanation that resolves the case on sight.
    """

    def store_entry(
        key: str, frames: int, layer: int | None
    ) -> RuntimeOverrideEntry | None:
        """The store entry that EXPLAINS this value, or None.

        Attribution requires the value to match: a store entry that disagrees
        with what is on disk describes a DIFFERENT value, and claiming it as the
        origin would be a wrong attribution rather than a missing one.
        """
        if layer != OUTPUTD_ENV_LAYER:
            return None
        entry = override_entries.get(key)
        if entry is None or entry.value.strip() != str(frames):
            return None
        return entry

    def where(key: str, frames: int, layer: int | None) -> str:
        if layer is None:
            return PACKAGED_OUTPUTD_DEFAULT_SOURCE
        entry = store_entry(key, frames, layer)
        if entry is None:
            return labels[layer]
        fields = ", ".join(
            f"{name}={value}"
            for name, value in (
                ("created_at", entry.created_at),
                ("reason", entry.reason),
            )
            if value
        )
        # The store path the caller ACTUALLY read, never the production
        # constant: naming a file that was not consulted is the wrong-origin
        # failure this provenance exists to prevent.
        origin = (
            f"{labels[layer]}, written there from the override store {override_label}"
        )
        return f"{origin} ({fields})" if fields else origin

    halves = (
        (buffer_key, buffer_frames, buffer_layer),
        (OUTPUTD_PERIOD_KEY, period_frames, period_layer),
    )
    detail = ", ".join(
        f"{key}={frames} comes from {where(key, frames, layer)}"
        for key, frames, layer in halves
    )
    # At least one half is always layer-owned, because the packaged defaults are
    # mutually coherent by contract
    # (test_packaged_outputd_buffer_defaults_are_mutually_coherent) — so there is
    # always a named source here, and no "this is a build defect" branch to carry
    # at runtime.
    stored = [key for key, frames, layer in halves if store_entry(key, frames, layer)]
    if stored:
        # Name the actual key(s), not a `<key>` placeholder the operator has to
        # translate — the remediation should be runnable as printed.
        clear = " && ".join(
            f"jasper-audio-config overrides-clear {key}" for key in stored
        )
        return (
            f"{detail}; clearing the {labels[OUTPUTD_ENV_LAYER]} line alone is undone by "
            f"the next reconcile — clear the override with `{clear}`"
        )
    return (
        f"{detail}; correct or remove the losing line in the file named above — "
        "the reconciler will refuse to write this candidate until the pair is coherent"
    )


def outputd_env_buffer_pair_error(
    *,
    base_env: Mapping[str, str] | None = None,
    outputd_env: Mapping[str, str] | None = None,
    base_label: str | None = None,
    outputd_label: str | None = None,
    override_entries: Mapping[str, RuntimeOverrideEntry] | None = None,
    override_label: str = DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
) -> str | None:
    """Validate effective outputd buffer/period pairs for env-file writers.

    Precedence mirrors the service contract: packaged defaults, then
    ``/etc/jasper/jasper.env``, then the reconciler-owned ``outputd.env``.
    The check order mirrors Rust's outputd config validator so logs name the
    same first failing pair the daemon would reject with EX_CONFIG.

    Pass BOTH ``base_label`` and ``outputd_label`` — the paths the two mappings
    were read from — to append :func:`_pair_provenance`, which names the layer
    each half of the failing pair came from. With either omitted the returned
    string stays the bare Rust mirror, byte for byte, which is what
    ``test_python_outputd_buffer_contract_matches_rust_validator`` compares
    against the daemon's own message. Callers that HAVE the paths should always
    pass them: the mirror alone cannot tell an operator which file to edit.

    ``override_entries`` (the lab override store, keyed by env key) is what lets
    the provenance distinguish a line an operator wrote in ``outputd.env`` from
    one the latency-floor pass copied there out of the store. The store is NOT a
    precedence layer here — outputd never reads it — but it is the ORIGIN of
    some values that reach ``outputd.env``, and only naming it makes the refusal
    actionable.
    """

    values = [dict(base_env or {}), dict(outputd_env or {})]
    labels = (
        (base_label, outputd_label)
        if base_label is not None and outputd_label is not None
        else None
    )
    entries = dict(override_entries or {})
    period_frames, period_error, period_layer = _effective_outputd_positive_int(
        OUTPUTD_PERIOD_KEY,
        default=DEFAULT_OUTPUTD_PERIOD_FRAMES,
        layers=values,
    )
    if period_error is not None:
        return period_error
    dac_buffer_frames, dac_error, dac_layer = _effective_outputd_positive_int(
        OUTPUTD_DAC_BUFFER_KEY,
        default=DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
        layers=values,
    )
    if dac_error is not None:
        return dac_error
    detail = outputd_dac_buffer_pair_error(
        period_frames=period_frames,
        dac_buffer_frames=dac_buffer_frames,
    )
    if detail is None or labels is None:
        return detail
    return f"{detail}; " + _pair_provenance(
        buffer_key=OUTPUTD_DAC_BUFFER_KEY,
        buffer_frames=dac_buffer_frames,
        buffer_layer=dac_layer,
        period_frames=period_frames,
        period_layer=period_layer,
        labels=labels,
        override_entries=entries,
        override_label=override_label,
    )


def resolve_outputd_period_setting(
    *,
    base_env: Mapping[str, str],
    override_env: Mapping[str, str],
    generated_env: Mapping[str, str],
    base_label: str,
    override_label: str,
    generated_label: str,
    profile_id: str,
) -> RuntimeSetting:
    """THE outputd period derivation: operator env, lab override, DAC floor.

    One derivation with two callers — :func:`build_audio_runtime_plan`'s
    settings list and the lab-override/floor explain paths — so a consumer that
    only needs the plan's INTENDED period cannot answer it differently. What
    outputd will actually load is :func:`outputd_period_frames_as_loaded`.
    """
    return resolve_profile_floor_int(
        key=OUTPUTD_PERIOD_KEY,
        default=DEFAULT_OUTPUTD_PERIOD_FRAMES,
        floor_value=getattr(
            latency_floor_for(profile_id) if profile_id else None,
            "outputd_period_frames",
            None,
        ),
        base_env=base_env,
        override_env=override_env,
        generated_env=generated_env,
        base_label=base_label,
        override_label=override_label,
        generated_label=generated_label,
        profile_id=profile_id,
    )


def positive_int(raw: str | None) -> tuple[int | None, str | None]:
    if raw is None:
        return None, None
    text = str(raw).strip().strip("'\"")
    if not text:
        return None, "empty"
    try:
        value = int(text)
    except ValueError:
        return None, "not an integer"
    if value <= 0:
        return None, "must be > 0"
    return value, None


def raw(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    return str(value).strip().strip("'\"")


def _effective_outputd_positive_int(
    key: str,
    *,
    default: int,
    layers: Sequence[Mapping[str, str]],
) -> tuple[int, str | None, int | None]:
    """Resolve one integer knob across the env layers, highest-precedence first.

    Third element is the INDEX into ``layers`` that supplied the value, or
    ``None`` when no layer stated it and the packaged default applies. The
    caller needs that to tell an operator which file to edit — the value alone
    cannot, and a refusal that names only the key is what left #2489 pointing
    at the wrong file.
    """
    for index in reversed(range(len(layers))):
        raw_value = raw(layers[index], key)
        if raw_value is None:
            continue
        value, error = positive_int(raw_value)
        if error is not None or value is None:
            return default, f"{key}={raw_value!r} is invalid ({error})", index
        return value, None, index
    return default, None, None


@dataclass(frozen=True)
class _PositiveIntPolicy:
    """Policy-specific provenance and warning vocabulary for one integer knob."""

    value: int | None
    source_kind: SourceKind
    source: str
    name: str
    owner_id: str
    absent_detail: str
    override_scope: str
    packaged_default: int
    packaged_source: str


def _resolve_layered_policy_int(
    *,
    key: str,
    policy: _PositiveIntPolicy,
    base_env: Mapping[str, str],
    override_env: Mapping[str, str],
    generated_env: Mapping[str, str],
    base_label: str,
    override_label: str,
    generated_label: str,
) -> RuntimeSetting:
    """Resolve override/operator/policy/default precedence for a positive int."""

    operator_raw = raw(base_env, key)
    override_raw = raw(override_env, key)
    generated_raw = raw(generated_env, key)
    operator_value, operator_error = positive_int(operator_raw)
    override_value, override_error = positive_int(override_raw)
    generated_value, generated_error = positive_int(generated_raw)
    warnings: list[str] = []

    if override_error is not None:
        warnings.append(
            f"{key} in {override_label} is invalid ({override_raw!r}: "
            f"{override_error}); ignored"
        )
    if operator_error is not None:
        warnings.append(
            f"{key} in {base_label} is invalid ({operator_raw!r}: "
            f"{operator_error}); ignored"
        )
    if generated_error is not None:
        warnings.append(
            f"{key} in {generated_label} is invalid ({generated_raw!r}: "
            f"{generated_error}); remove it or rerun audio hardware reconcile"
        )
    if operator_raw is not None and generated_raw is not None:
        warnings.append(
            f"{key} is set in both {base_label} and {generated_label}; "
            "one knob has two homes"
        )
    if override_raw is not None and (
        operator_raw is not None or generated_raw is not None
    ):
        warnings.append(
            f"{key} lab override in {override_label} is active; it intentionally "
            f"wins over env/{policy.override_scope} values"
        )

    if override_value is not None:
        return RuntimeSetting(
            key=key,
            value=override_value,
            source_kind="lab_override",
            source=override_label,
            unit="frames",
            override_value=override_raw,
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )

    if operator_value is not None:
        return RuntimeSetting(
            key=key,
            value=operator_value,
            source_kind="operator_env",
            source=base_label,
            unit="frames",
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )

    if policy.value is not None:
        if generated_value is None:
            warnings.append(
                f"{key} {policy.name} for {policy.owner_id} is {policy.value}, but "
                f"{generated_label} does not emit it; run "
                "jasper-audio-hardware-reconcile"
            )
        elif generated_value != policy.value:
            warnings.append(
                f"{key} in {generated_label} is {generated_value}, but the "
                f"{policy.owner_id} {policy.name} is {policy.value}; rerun "
                "audio hardware reconcile"
            )
        return RuntimeSetting(
            key=key,
            value=policy.value,
            source_kind=policy.source_kind,
            source=policy.source,
            unit="frames",
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )

    if generated_value is not None and generated_value != policy.packaged_default:
        warnings.append(
            f"{key} in {generated_label} is {generated_value}, but the active "
            f"{policy.absent_detail}; stale generated value will override the "
            f"packaged default {policy.packaged_default}"
        )
    return RuntimeSetting(
        key=key,
        value=policy.packaged_default,
        source_kind="packaged_default",
        source=policy.packaged_source,
        unit="frames",
        operator_value=operator_raw,
        generated_value=generated_raw,
        warnings=tuple(warnings),
    )


def resolve_profile_floor_int(
    *,
    key: str,
    default: int,
    floor_value: int | None,
    base_env: Mapping[str, str],
    override_env: Mapping[str, str],
    generated_env: Mapping[str, str],
    base_label: str,
    override_label: str,
    generated_label: str,
    profile_id: str,
) -> RuntimeSetting:
    return _resolve_layered_policy_int(
        key=key,
        policy=_PositiveIntPolicy(
            value=floor_value,
            source_kind="device_profile",
            source=f"DacProfile:{profile_id}",
            name="profile floor",
            owner_id=profile_id,
            absent_detail="profile has no floor",
            override_scope="profile",
            packaged_default=default,
            packaged_source="packaged systemd/Camilla default",
        ),
        base_env=base_env,
        override_env=override_env,
        generated_env=generated_env,
        base_label=base_label,
        override_label=override_label,
        generated_label=generated_label,
    )


def resolve_fanin_int(
    *,
    key: str,
    default: int,
    base_env: Mapping[str, str],
    override_env: Mapping[str, str],
    fanin_env: Mapping[str, str],
    base_label: str,
    override_label: str,
    fanin_label: str,
    operator_env_allowed: bool = False,
    min_value: int = 1,
    max_value: int | None = None,
) -> RuntimeSetting:
    operator_raw = raw(base_env, key)
    override_raw = raw(override_env, key)
    generated_raw = raw(fanin_env, key)
    operator_value, operator_error = positive_int(operator_raw)
    override_value, override_error = positive_int(override_raw)
    generated_value, generated_error = positive_int(generated_raw)

    def enforce_bounds(
        value: int | None,
        error: str | None,
    ) -> tuple[int | None, str | None]:
        if value is None or error is not None:
            return value, error
        if value < min_value or (max_value is not None and value > max_value):
            upper = f"..{max_value}" if max_value is not None else " or greater"
            return None, f"must be {min_value}{upper}"
        return value, None

    operator_value, operator_error = enforce_bounds(
        operator_value,
        operator_error,
    )
    override_value, override_error = enforce_bounds(
        override_value,
        override_error,
    )
    generated_value, generated_error = enforce_bounds(
        generated_value,
        generated_error,
    )
    warnings: list[str] = []

    if override_error is not None:
        warnings.append(
            f"{key} in {override_label} is invalid ({override_raw!r}: "
            f"{override_error}); ignored"
        )
    if operator_error is not None:
        warnings.append(
            f"{key} in {base_label} is invalid ({operator_raw!r}: "
            f"{operator_error}); ignored"
        )
    if generated_error is not None:
        warnings.append(
            f"{key} in {fanin_label} is invalid ({generated_raw!r}: "
            f"{generated_error}); using the next safe source"
        )
    if operator_raw is not None and not operator_env_allowed:
        warnings.append(
            f"{key} is present in {base_label}; fan-in tuning belongs in "
            f"{fanin_label} or the audio runtime lab override artifact"
        )
    if operator_raw is not None and generated_raw is not None:
        warnings.append(
            f"{key} is set in both {base_label} and {fanin_label}; "
            f"{fanin_label} is the reconciler-owned home"
        )
    if override_raw is not None and (
        operator_raw is not None or generated_raw is not None
    ):
        warnings.append(
            f"{key} lab override in {override_label} is active; it intentionally "
            "wins over env/default values"
        )
    if override_value is not None:
        return RuntimeSetting(
            key=key,
            value=override_value,
            source_kind="lab_override",
            source=override_label,
            unit="frames",
            override_value=override_raw,
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )
    if generated_value is not None:
        return RuntimeSetting(
            key=key,
            value=generated_value,
            source_kind="generated_env",
            source=fanin_label,
            unit="frames",
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )
    if operator_value is not None:
        return RuntimeSetting(
            key=key,
            value=operator_value,
            source_kind="operator_env",
            source=base_label,
            unit="frames",
            operator_value=operator_raw,
            generated_value=generated_raw,
            warnings=tuple(warnings),
        )
    return RuntimeSetting(
        key=key,
        value=default,
        source_kind="packaged_default",
        source="packaged fan-in default",
        unit="frames",
        operator_value=operator_raw,
        generated_value=generated_raw,
        warnings=tuple(warnings),
    )
