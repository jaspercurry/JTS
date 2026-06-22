# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared AEC3 corpus-sweep definitions.

The wake-corpus recorder can ask jasper-aec-bridge to run a bounded
set of extra WebRTC AEC3 engines in parallel with the production
baseline. Keep the sweep small: each variant has adaptive state, CPU
cost, a UDP stream, a WAV per utterance, and a listening burden.

Sweep tuning is intentionally file-backed at runtime. The code owns
the three stable UDP slots and the validation rules; operators can
change labels and AEC3 knob overrides in ``/var/lib/jasper`` and then
restart only ``jasper-aec-bridge`` instead of doing a full deploy.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.log_event import log_event


AEC3_SWEEP_ENV_FLAG = "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED"
AEC3_SWEEP_CONFIG_ENV = "JASPER_AEC3_SWEEP_CONFIG"
AEC3_SWEEP_SOURCE_ENV = "JASPER_AEC_CORPUS_AEC3_SWEEP_SOURCE"
AEC3_SWEEP_SOURCE_XVF = "xvf"
AEC3_SWEEP_SOURCE_USB = "usb"
DEFAULT_AEC3_SWEEP_SOURCE = AEC3_SWEEP_SOURCE_XVF
DEFAULT_AEC3_SWEEP_CONFIG_PATH = Path("/var/lib/jasper/aec3_sweep_variants.json")
MAX_AEC3_SWEEP_VARIANTS = 3
_LEG_RE = re.compile(r"^aec3_variant_[1-3]$")
_VALID_AEC3_SWEEP_SOURCES = (AEC3_SWEEP_SOURCE_XVF, AEC3_SWEEP_SOURCE_USB)
AEC3_EDGE_COMBO_OVERRIDES: dict[str, str] = {
    "JASPER_AEC_CONSERVATIVE_HF": "0",
    "JASPER_AEC_MAX_DEC_LF": "0.02",
    "JASPER_AEC_NEAREND_MAX_DEC_LF": "0.02",
    "JASPER_AEC_DND_SNR_THRESHOLD": "15",
    "JASPER_AEC_DND_ENR_THRESHOLD": "0.50",
    "JASPER_AEC_DND_HOLD_DURATION": "100",
    "JASPER_AEC_DND_TRIGGER_THRESHOLD": "6",
}
USB_AEC3_SWEEP_BASELINE_OVERRIDES: dict[str, str] = {
    **AEC3_EDGE_COMBO_OVERRIDES,
    "JASPER_AEC_STREAM_DELAY_MS": "40",
}
USB_AEC3_SWEEP_BASELINE_LABEL = "USB AEC3 edge combo 40 ms"
USB_AEC3_CORPUS_OVERRIDES: dict[str, str] = {
    **AEC3_EDGE_COMBO_OVERRIDES,
    "JASPER_AEC_STREAM_DELAY_MS": "80",
}
USB_AEC3_CORPUS_LABEL = "USB AEC3 edge combo 80 ms"


@dataclass(frozen=True)
class Aec3SweepVariant:
    leg: str
    label: str
    port_env: str
    default_port: int
    env_overrides: dict[str, str]


@dataclass(frozen=True)
class Aec3SweepConfig:
    variants: tuple[Aec3SweepVariant, ...]
    source: str
    path: str
    config_hash: str
    error: str | None = None


@dataclass(frozen=True)
class _KnobSpec:
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()


class Aec3SweepConfigError(ValueError):
    """Raised when a runtime sweep config is malformed or unsafe."""


_BOOL_SPEC = _KnobSpec("bool")
_ALLOWED_AEC3_SWEEP_ENV_VARS: dict[str, _KnobSpec] = {
    # Top-level processing toggles exposed by _Aec3V2Engine.
    "JASPER_AEC_NS_ENABLED": _BOOL_SPEC,
    "JASPER_AEC_NS_LEVEL": _KnobSpec("enum", choices=("low", "moderate", "high")),
    "JASPER_AEC_AGC1_ENABLED": _BOOL_SPEC,
    "JASPER_AEC_AGC1_TARGET_DBFS": _KnobSpec("int", 0, 31),
    "JASPER_AEC_AGC1_MAX_GAIN_DB": _KnobSpec("int", 0, 60),
    "JASPER_AEC_AGC2": _BOOL_SPEC,
    "JASPER_AEC_STREAM_DELAY_MS": _KnobSpec("int", 0, 500),
    # BEST_A / EchoCanceller3Config knobs.
    "JASPER_AEC_FILTER_LENGTH": _KnobSpec("int", 1, 128),
    "JASPER_AEC_BOUNDED_ERL": _BOOL_SPEC,
    "JASPER_AEC_DEFAULT_GAIN": _KnobSpec("float", 0.0, 5.0),
    "JASPER_AEC_ERLE_MAX_L": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_ERLE_MAX_H": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_ERLE_ONSET": _BOOL_SPEC,
    "JASPER_AEC_USE_STATIONARITY": _BOOL_SPEC,
    "JASPER_AEC_CONSERVATIVE_HF": _BOOL_SPEC,
    "JASPER_AEC_MASK_HF_ENR_T": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_MASK_HF_ENR_S": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_MASK_HF_EMR_T": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_MAX_DEC_LF": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_NEAREND_AVERAGE_BLOCKS": _KnobSpec("int", 1, 64),
    "JASPER_AEC_NEAREND_MASK_HF_ENR_T": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_NEAREND_MASK_HF_ENR_S": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_NEAREND_MASK_HF_EMR_T": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_NEAREND_MAX_DEC_LF": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_NEAREND_MAX_INC": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_DND_SNR_THRESHOLD": _KnobSpec("float", 0.0, 80.0),
    "JASPER_AEC_DND_HOLD_DURATION": _KnobSpec("int", 0, 1000),
    "JASPER_AEC_DND_ENR_THRESHOLD": _KnobSpec("float", 0.0, 10.0),
    "JASPER_AEC_DND_TRIGGER_THRESHOLD": _KnobSpec("int", 0, 128),
}


DEFAULT_AEC3_SWEEP_VARIANTS: tuple[Aec3SweepVariant, ...] = (
    Aec3SweepVariant(
        leg="aec3_variant_1",
        label="AEC3 edge combo 80 ms",
        port_env="JASPER_AEC_UDP_PORT_AEC3_VARIANT_1",
        default_port=9884,
        env_overrides={
            **AEC3_EDGE_COMBO_OVERRIDES,
            "JASPER_AEC_STREAM_DELAY_MS": "80",
        },
    ),
    Aec3SweepVariant(
        leg="aec3_variant_2",
        label="AEC3 edge combo 120 ms",
        port_env="JASPER_AEC_UDP_PORT_AEC3_VARIANT_2",
        default_port=9885,
        env_overrides={
            **AEC3_EDGE_COMBO_OVERRIDES,
            "JASPER_AEC_STREAM_DELAY_MS": "120",
        },
    ),
    Aec3SweepVariant(
        leg="aec3_variant_3",
        label="AEC3 edge combo 160 ms",
        port_env="JASPER_AEC_UDP_PORT_AEC3_VARIANT_3",
        default_port=9886,
        env_overrides={
            **AEC3_EDGE_COMBO_OVERRIDES,
            "JASPER_AEC_STREAM_DELAY_MS": "160",
        },
    ),
)


AEC3_SWEEP_VARIANTS = DEFAULT_AEC3_SWEEP_VARIANTS


def _config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    return Path(os.environ.get(AEC3_SWEEP_CONFIG_ENV, DEFAULT_AEC3_SWEEP_CONFIG_PATH))


def normalize_aec3_sweep_source(
    value: str | None,
    *,
    default: str = DEFAULT_AEC3_SWEEP_SOURCE,
) -> str:
    """Normalize the mic leg that feeds the AEC3 sweep engines."""
    normalized_default = default.strip().lower()
    if normalized_default not in _VALID_AEC3_SWEEP_SOURCES:
        raise Aec3SweepConfigError(
            f"default AEC3 sweep source must be one of: "
            f"{', '.join(_VALID_AEC3_SWEEP_SOURCES)}",
        )
    if value is None:
        return normalized_default
    if not isinstance(value, str):
        raise Aec3SweepConfigError("AEC3 sweep source must be a string")
    if not value.strip():
        return normalized_default
    source = value.strip().lower()
    if source not in _VALID_AEC3_SWEEP_SOURCES:
        raise Aec3SweepConfigError(
            f"AEC3 sweep source must be one of: "
            f"{', '.join(_VALID_AEC3_SWEEP_SOURCES)}",
        )
    return source


def current_aec3_sweep_source(
    *,
    default: str = DEFAULT_AEC3_SWEEP_SOURCE,
) -> str:
    """Return the effective runtime AEC3 sweep input source."""
    return normalize_aec3_sweep_source(
        os.environ.get(AEC3_SWEEP_SOURCE_ENV),
        default=default,
    )


def _canonical_variant_payload(
    variants: tuple[Aec3SweepVariant, ...],
) -> list[dict[str, object]]:
    return [
        {
            "leg": variant.leg,
            "label": variant.label,
            "env_overrides": dict(variant.env_overrides),
        }
        for variant in variants
    ]


def _config_hash(variants: tuple[Aec3SweepVariant, ...]) -> str:
    payload = json.dumps(
        _canonical_variant_payload(variants),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _normalize_bool(key: str, raw: Any) -> str:
    if isinstance(raw, bool):
        return "1" if raw else "0"
    if isinstance(raw, int) and raw in (0, 1):
        return str(raw)
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("1", "true", "yes", "on"):
            return "1"
        if value in ("0", "false", "no", "off"):
            return "0"
    raise Aec3SweepConfigError(f"{key} must be boolean-like")


def _normalize_knob_value(key: str, raw: Any) -> str:
    spec = _ALLOWED_AEC3_SWEEP_ENV_VARS.get(key)
    if spec is None:
        raise Aec3SweepConfigError(f"unknown AEC3 sweep knob: {key}")

    if spec.kind == "bool":
        return _normalize_bool(key, raw)
    if spec.kind == "enum":
        if not isinstance(raw, str):
            raise Aec3SweepConfigError(f"{key} must be a string")
        value = raw.strip().lower()
        if value not in spec.choices:
            choices = ", ".join(spec.choices)
            raise Aec3SweepConfigError(f"{key} must be one of: {choices}")
        return value
    if spec.kind == "int":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise Aec3SweepConfigError(f"{key} must be an integer") from None
        if (
            spec.minimum is not None and value < spec.minimum
        ) or (
            spec.maximum is not None and value > spec.maximum
        ):
            raise Aec3SweepConfigError(
                f"{key} must be between {spec.minimum:g} and {spec.maximum:g}",
            )
        return str(value)
    if spec.kind == "float":
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise Aec3SweepConfigError(f"{key} must be a number") from None
        if not math.isfinite(value):
            raise Aec3SweepConfigError(f"{key} must be finite")
        if (
            spec.minimum is not None and value < spec.minimum
        ) or (
            spec.maximum is not None and value > spec.maximum
        ):
            raise Aec3SweepConfigError(
                f"{key} must be between {spec.minimum:g} and {spec.maximum:g}",
            )
        return f"{value:g}"
    raise Aec3SweepConfigError(f"{key} has unsupported validator kind")


def _validate_variant_payload(
    item: Any,
    *,
    index: int,
    expected: Aec3SweepVariant,
) -> Aec3SweepVariant:
    if not isinstance(item, dict):
        raise Aec3SweepConfigError(f"variant {index + 1} must be an object")
    allowed_keys = {"leg", "label", "env_overrides"}
    unknown_keys = sorted(set(item) - allowed_keys)
    if unknown_keys:
        raise Aec3SweepConfigError(
            f"variant {index + 1} has unknown key(s): {', '.join(unknown_keys)}",
        )

    raw_leg = item.get("leg")
    if not isinstance(raw_leg, str):
        raise Aec3SweepConfigError(f"variant {index + 1} leg must be a string")
    leg = raw_leg.strip()
    if leg != expected.leg or not _LEG_RE.match(leg):
        raise Aec3SweepConfigError(
            f"variant {index + 1} leg must be {expected.leg!r}",
        )

    raw_label = item.get("label")
    if not isinstance(raw_label, str):
        raise Aec3SweepConfigError(f"variant {index + 1} label must be a string")
    label = raw_label.strip()
    if not label:
        raise Aec3SweepConfigError(f"variant {index + 1} label is required")
    if len(label) > 80:
        raise Aec3SweepConfigError(f"variant {index + 1} label is too long")

    raw_overrides = item.get("env_overrides", {})
    if not isinstance(raw_overrides, dict):
        raise Aec3SweepConfigError(
            f"variant {index + 1} env_overrides must be an object",
        )
    env_overrides = {
        str(key): _normalize_knob_value(str(key), value)
        for key, value in sorted(raw_overrides.items())
    }
    return Aec3SweepVariant(
        leg=expected.leg,
        label=label,
        port_env=expected.port_env,
        default_port=expected.default_port,
        env_overrides=env_overrides,
    )


def validate_aec3_sweep_config_payload(payload: Any) -> tuple[Aec3SweepVariant, ...]:
    """Validate a JSON-decoded runtime sweep config."""
    if not isinstance(payload, dict):
        raise Aec3SweepConfigError("AEC3 sweep config must be a JSON object")
    allowed_keys = {"version", "variants"}
    unknown_keys = sorted(set(payload) - allowed_keys)
    if unknown_keys:
        raise Aec3SweepConfigError(
            f"AEC3 sweep config has unknown key(s): {', '.join(unknown_keys)}",
        )
    version = payload.get("version", 1)
    if version != 1:
        raise Aec3SweepConfigError("AEC3 sweep config version must be 1")
    raw_variants = payload.get("variants")
    if not isinstance(raw_variants, list):
        raise Aec3SweepConfigError("AEC3 sweep config variants must be a list")
    if len(raw_variants) != MAX_AEC3_SWEEP_VARIANTS:
        raise Aec3SweepConfigError(
            f"AEC3 sweep config must define exactly {MAX_AEC3_SWEEP_VARIANTS} "
            "variants",
        )
    return tuple(
        _validate_variant_payload(item, index=index, expected=expected)
        for index, (item, expected) in enumerate(
            zip(raw_variants, DEFAULT_AEC3_SWEEP_VARIANTS, strict=True),
        )
    )


def aec3_sweep_config_payload(
    variants: tuple[Aec3SweepVariant, ...] = DEFAULT_AEC3_SWEEP_VARIANTS,
) -> dict[str, object]:
    """Return the editable JSON shape for these variants."""
    return {"version": 1, "variants": _canonical_variant_payload(variants)}


def load_aec3_sweep_config(
    path: str | Path | None = None,
    *,
    strict: bool = False,
    logger: Any | None = None,
) -> Aec3SweepConfig:
    """Load the effective runtime sweep config.

    Missing config files fall back to the code defaults. Invalid config
    files also fall back unless ``strict`` is true; the bridge uses the
    non-strict path so a bad test file cannot strand production audio.
    The apply/validate CLI uses strict mode before writing.
    """
    config_path = _config_path(path)
    if not config_path.exists():
        variants = DEFAULT_AEC3_SWEEP_VARIANTS
        return Aec3SweepConfig(
            variants=variants,
            source="default",
            path=str(config_path),
            config_hash=_config_hash(variants),
        )
    try:
        payload = json.loads(config_path.read_text())
        variants = validate_aec3_sweep_config_payload(payload)
        return Aec3SweepConfig(
            variants=variants,
            source="file",
            path=str(config_path),
            config_hash=_config_hash(variants),
        )
    except (Aec3SweepConfigError, OSError, json.JSONDecodeError) as e:
        if strict:
            if isinstance(e, Aec3SweepConfigError):
                raise
            raise Aec3SweepConfigError(str(e)) from e
        if logger is not None:
            log_event(
                logger,
                "aec3_sweep_config_invalid",
                path=config_path,
                error=str(e),
                fallback="default",
                level=logging.WARNING,
            )
        variants = DEFAULT_AEC3_SWEEP_VARIANTS
        return Aec3SweepConfig(
            variants=variants,
            source="default",
            path=str(config_path),
            config_hash=_config_hash(variants),
            error=str(e),
        )


def write_aec3_sweep_config(
    payload: Any,
    path: str | Path | None = None,
    *,
    mode: int = 0o644,
) -> Aec3SweepConfig:
    """Validate and atomically write a runtime sweep config file."""
    config_path = _config_path(path)
    variants = validate_aec3_sweep_config_payload(payload)
    data = json.dumps(aec3_sweep_config_payload(variants), indent=2) + "\n"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(data)
    os.chmod(tmp, mode)
    tmp.replace(config_path)
    return Aec3SweepConfig(
        variants=variants,
        source="file",
        path=str(config_path),
        config_hash=_config_hash(variants),
    )


def variant_metadata(
    variants: tuple[Aec3SweepVariant, ...] | None = None,
    *,
    input_source: str | None = None,
) -> list[dict[str, object]]:
    """JSON-friendly description stored in corpus session metadata."""
    effective = variants if variants is not None else load_aec3_sweep_config().variants
    data = _canonical_variant_payload(effective)
    if input_source is None:
        return data
    source = normalize_aec3_sweep_source(input_source)
    prefix = "USB" if source == AEC3_SWEEP_SOURCE_USB else "XVF"
    for item in data:
        label = str(item["label"])
        if not label.lower().startswith(("usb ", "xvf ")):
            item["label"] = f"{prefix} {label}"
        item["input_source"] = source
    return data


def config_metadata(
    config: Aec3SweepConfig | None = None,
    *,
    input_source: str | None = None,
) -> dict[str, object]:
    """JSON-friendly description of the effective sweep config source."""
    effective = config if config is not None else load_aec3_sweep_config()
    data: dict[str, object] = {
        "source": effective.source,
        "path": effective.path,
        "hash": effective.config_hash,
    }
    if input_source is not None:
        data["input_source"] = normalize_aec3_sweep_source(input_source)
    if effective.error:
        data["error"] = effective.error
    return data
