# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure, fail-closed producer for measured bass-limiter evidence.

This module deliberately has no production caller.  It validates a replayable
bench-evidence bundle against a separately supplied trusted current context and
selects only candidate values that the bundle actually measured.  It performs
no I/O, graph mutation, playback, clock read, or defaulting.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import math
import re
from typing import Any

from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    EvidenceIdentityError,
    json_fingerprint,
)


LIMITER_EVIDENCE_SCHEMA_VERSION = 1
LIMITER_EVIDENCE_PROTOCOL_REVISION = "2026-07-19b"

_EVIDENCE_KIND = "jts_bass_extension_limiter_evidence"
_THRESHOLD_SET_KIND = "jts_bass_extension_limiter_threshold_set"
_REFUSAL_KIND = "jts_bass_extension_limiter_evidence_refusal"
_DETECTOR_REFERENCE = (
    "instantaneous_float_sample_peak_dbfs_re_unity_at_limiter_input"
)
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_TARGET_FILTERS = ("bass_ext_lt", "bass_ext_subsonic")
# The public contract accepts arbitrary objects. Keep each catch-all boundary
# confined to materializing one caller-owned root; validation bugs in _produce
# intentionally remain visible instead of being mislabeled as input defects.
_TOTAL_INPUT_ERRORS = (Exception,)
_UNSUPPORTED_JSON_VALUE = object()
_INVALID_JSON_KEY = object()
_RETAINED_FACTS = frozenset(
    {"sweep", "sustain", "commanded_level", "stimulus_peak", "boost", "digital_clamp"}
)

_ROOT_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "protocol_revision",
        "evidence_fingerprint",
        "measured_context",
        "campaign_manifest",
        "retained_facts",
        "targets",
    }
)
_CONTEXT_FIELDS = frozenset(
    {
        "target_family_fingerprint",
        "target_order",
        "driver_safety_fingerprint",
        "margin_policy_fingerprint",
        "transparency_policy_fingerprint",
        "natural_graph_fingerprint",
        "baseline_limiter_clip_limit_dbfs",
        "limiter_domain_min_dbfs",
        "limiter_domain_max_dbfs",
        "limiter_domain_fingerprint",
        "camilladsp_build_id",
        "owner_channels",
        "sample_rate_hz",
        "limiter_name",
        "limiter_type",
        "soft_clip",
        "tap_implementation_id",
        "detector_reference",
    }
)
_TARGET_ORDER_FIELDS = frozenset({"target_id", "target_fingerprint"})
_RETAINED_FACT_FIELDS = frozenset({"status", "artifact"})
_TARGET_FIELDS = frozenset({"target_id", "target_fingerprint", "result"})
_STOP_RESULT_FIELDS = frozenset({"disposition", "stop_receipt", "partial_artifacts"})
_EVALUATED_RESULT_FIELDS = frozenset(
    {
        "disposition",
        "discovery_activation_receipt",
        "candidate_sources",
        "discovery_restoration_receipt",
        "candidates_least_to_most_permissive",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "source_fingerprint",
        "stimulus",
        "admission",
        "active_graph_readback",
        "pre_limiter_pcm",
        "peak_analysis",
        "pre_limiter_peak_dbfs",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "limiter_threshold_dbfs",
        "source_fingerprint",
        "candidate_activation_receipt",
        "configured_clip_limit_dbfs",
        "active_target_fingerprint",
        "active_graph_fingerprint",
        "ordered_owner_chain",
        "digital_transfer_probe",
        "sweep_transparency",
        "sustain_stress",
        "candidate_restoration_receipt",
        "restored_graph_fingerprint",
        "disposition",
    }
)
_TRANSFER_FIELDS = frozenset(
    {
        "stimulus",
        "pre_limiter_pcm",
        "post_limiter_pcm",
        "reference_post_limiter_pcm",
        "transfer_analysis",
        "verdict",
    }
)
_MEASUREMENT_FIELDS = frozenset(
    {
        "stimulus",
        "admission",
        "pre_limiter_pcm",
        "post_limiter_pcm",
        "acoustic_capture",
        "signal_analysis",
        "protection_analysis",
        "stimulus_band_hz",
        "stimulus_effective_peak_dbfs",
        "commanded_main_volume_db",
        "target_boost_db",
        "digital_clamp_passed",
        "pre_limiter_peak_dbfs",
        "post_limiter_peak_dbfs",
        "hold_duration_s",
        "required_cooldown_s",
        "repeat_count",
        "quality_verdict",
        "protection_verdict",
    }
)
_SWEEP_FIELDS = _MEASUREMENT_FIELDS | {
    "reference_activation_receipt",
    "reference_stimulus",
    "reference_admission",
    "reference_acoustic_capture",
    "transparency_analysis",
    "reference_target_fingerprint",
    "reference_active_graph_fingerprint",
    "reference_configured_clip_limit_dbfs",
    "transparency_verdict",
}


class LimiterRefusalReason(StrEnum):
    """Stable total-refusal categories in precedence order."""

    MISSING = "missing"
    STALE = "stale"
    INCONSISTENT = "inconsistent"
    OUT_OF_ENVELOPE = "out_of_envelope"


@dataclass(frozen=True, slots=True)
class TargetLimiterThreshold:
    target_id: str
    target_fingerprint: str
    limiter_threshold_dbfs: float
    source_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "target_fingerprint": self.target_fingerprint,
            "limiter_threshold_dbfs": self.limiter_threshold_dbfs,
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class LimiterThresholdSet:
    evidence_fingerprint: str
    required_context_fingerprint: str
    targets: tuple[TargetLimiterThreshold, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": LIMITER_EVIDENCE_SCHEMA_VERSION,
            "kind": _THRESHOLD_SET_KIND,
            "evidence_fingerprint": self.evidence_fingerprint,
            "required_context_fingerprint": self.required_context_fingerprint,
            "targets": [target.to_dict() for target in self.targets],
        }


@dataclass(frozen=True, slots=True)
class LimiterEvidenceRefusal:
    reason: LimiterRefusalReason
    evidence_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": LIMITER_EVIDENCE_SCHEMA_VERSION,
            "kind": _REFUSAL_KIND,
            "reason": self.reason.value,
            "evidence_paths": list(self.evidence_paths),
        }


@dataclass(slots=True)
class _Defects:
    missing: set[str]
    stale: set[str]
    inconsistent: set[str]
    out_of_envelope: set[str]

    @classmethod
    def empty(cls) -> _Defects:
        return cls(set(), set(), set(), set())

    def refusal(self) -> LimiterEvidenceRefusal | None:
        for reason, paths in (
            (LimiterRefusalReason.MISSING, self.missing),
            (LimiterRefusalReason.STALE, self.stale),
            (LimiterRefusalReason.INCONSISTENT, self.inconsistent),
            (LimiterRefusalReason.OUT_OF_ENVELOPE, self.out_of_envelope),
        ):
            if paths:
                return LimiterEvidenceRefusal(reason, tuple(sorted(paths)))
        return None


def _materialize_json_input(value: object) -> object:
    """Copy JSON-shaped input without carrying caller-defined behavior inward."""

    if value is None or type(value) in {bool, int, float, str}:
        return value
    if type(value) is list:
        return [_materialize_json_input(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[object, object] = {}
        for key, item in value.items():
            normalized_key = key if type(key) is str else _INVALID_JSON_KEY
            if normalized_key in result:
                raise ValueError("duplicate object key while materializing JSON input")
            result[normalized_key] = _materialize_json_input(item)
        return result
    return _UNSUPPORTED_JSON_VALUE


def _root_input_refusal(path: str) -> LimiterEvidenceRefusal:
    return LimiterEvidenceRefusal(
        LimiterRefusalReason.INCONSISTENT,
        (path,),
    )


def _strict_object(
    value: object,
    *,
    path: str,
    fields: frozenset[str],
    defects: _Defects,
) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        defects.inconsistent.add(path)
        return None
    keys = set(value)
    for name in fields - keys:
        defects.missing.add(f"{path}.{name}")
    for name in keys - fields:
        defects.inconsistent.add(f"{path}.{name}")
    return value


def _required_list(
    value: object,
    *,
    path: str,
    defects: _Defects,
    allow_empty: bool = False,
) -> list[Any] | None:
    if type(value) is not list:
        defects.inconsistent.add(path)
        return None
    if not value and not allow_empty:
        defects.missing.add(path)
    return value


def _trimmed_text(value: object, *, path: str, defects: _Defects) -> str | None:
    if type(value) is not str or not value or value != value.strip():
        defects.inconsistent.add(path)
        return None
    return value


def _fingerprint(value: object, *, path: str, defects: _Defects) -> str | None:
    if type(value) is not str or _FINGERPRINT_RE.fullmatch(value) is None:
        defects.inconsistent.add(path)
        return None
    return value


def _finite_float(value: object, *, path: str, defects: _Defects) -> float | None:
    if type(value) is not float or not math.isfinite(value):
        defects.inconsistent.add(path)
        return None
    return value


def _positive_float(value: object, *, path: str, defects: _Defects) -> float | None:
    result = _finite_float(value, path=path, defects=defects)
    if result is not None and result <= 0.0:
        defects.inconsistent.add(path)
        return None
    return result


def _nonnegative_float(value: object, *, path: str, defects: _Defects) -> float | None:
    result = _finite_float(value, path=path, defects=defects)
    if result is not None and result < 0.0:
        defects.inconsistent.add(path)
        return None
    return result


def _positive_int(value: object, *, path: str, defects: _Defects) -> int | None:
    if type(value) is not int or value <= 0:
        defects.inconsistent.add(path)
        return None
    return value


def _exact_bool(value: object, *, path: str, defects: _Defects) -> bool | None:
    if type(value) is not bool:
        defects.inconsistent.add(path)
        return None
    return value


def _choice(
    value: object,
    *,
    path: str,
    choices: frozenset[str],
    defects: _Defects,
) -> str | None:
    result = _trimmed_text(value, path=path, defects=defects)
    if result is not None and result not in choices:
        defects.inconsistent.add(path)
        return None
    return result


def _artifact(value: object, *, path: str, defects: _Defects) -> ArtifactIdentity | None:
    try:
        return ArtifactIdentity.from_mapping(value)
    except (EvidenceIdentityError, KeyError, TypeError, ValueError):
        defects.inconsistent.add(path)
        return None


def _context(
    value: object,
    *,
    path: str,
    defects: _Defects,
) -> tuple[Mapping[str, Any] | None, bool]:
    missing_before = len(defects.missing)
    inconsistent_before = len(defects.inconsistent)
    raw = _strict_object(value, path=path, fields=_CONTEXT_FIELDS, defects=defects)
    if raw is None:
        return None, False

    for name in (
        "target_family_fingerprint",
        "driver_safety_fingerprint",
        "margin_policy_fingerprint",
        "transparency_policy_fingerprint",
        "natural_graph_fingerprint",
        "limiter_domain_fingerprint",
    ):
        if name in raw:
            _fingerprint(raw[name], path=f"{path}.{name}", defects=defects)

    target_order = None
    if "target_order" in raw:
        target_order = _required_list(
            raw["target_order"], path=f"{path}.target_order", defects=defects
        )
    target_ids: list[str] = []
    target_fingerprints: list[str] = []
    for index, item in enumerate(target_order or []):
        item_path = f"{path}.target_order[{index}]"
        target = _strict_object(
            item, path=item_path, fields=_TARGET_ORDER_FIELDS, defects=defects
        )
        if target is None:
            continue
        target_id = (
            _trimmed_text(target["target_id"], path=f"{item_path}.target_id", defects=defects)
            if "target_id" in target
            else None
        )
        target_fp = (
            _fingerprint(
                target["target_fingerprint"],
                path=f"{item_path}.target_fingerprint",
                defects=defects,
            )
            if "target_fingerprint" in target
            else None
        )
        if target_id is not None:
            if target_id in target_ids:
                defects.inconsistent.add(f"{item_path}.target_id")
            target_ids.append(target_id)
        if target_fp is not None:
            if target_fp in target_fingerprints:
                defects.inconsistent.add(f"{item_path}.target_fingerprint")
            target_fingerprints.append(target_fp)

    domain_values: dict[str, float] = {}
    for name in (
        "baseline_limiter_clip_limit_dbfs",
        "limiter_domain_min_dbfs",
        "limiter_domain_max_dbfs",
    ):
        if name in raw:
            parsed = _finite_float(raw[name], path=f"{path}.{name}", defects=defects)
            if parsed is not None:
                domain_values[name] = parsed
    if len(domain_values) == 3:
        low = domain_values["limiter_domain_min_dbfs"]
        high = domain_values["limiter_domain_max_dbfs"]
        baseline = domain_values["baseline_limiter_clip_limit_dbfs"]
        if low >= high:
            defects.inconsistent.update(
                {f"{path}.limiter_domain_min_dbfs", f"{path}.limiter_domain_max_dbfs"}
            )
        if not low <= baseline <= high:
            defects.inconsistent.add(f"{path}.baseline_limiter_clip_limit_dbfs")

    for name in ("camilladsp_build_id", "limiter_name", "tap_implementation_id"):
        if name in raw:
            _trimmed_text(raw[name], path=f"{path}.{name}", defects=defects)
    if "limiter_type" in raw and raw["limiter_type"] != "Limiter":
        defects.inconsistent.add(f"{path}.limiter_type")
    if "soft_clip" in raw and raw["soft_clip"] is not True:
        defects.inconsistent.add(f"{path}.soft_clip")
    if "detector_reference" in raw and raw["detector_reference"] != _DETECTOR_REFERENCE:
        defects.inconsistent.add(f"{path}.detector_reference")
    if "sample_rate_hz" in raw:
        _positive_int(raw["sample_rate_hz"], path=f"{path}.sample_rate_hz", defects=defects)

    if "owner_channels" in raw:
        channels = _required_list(
            raw["owner_channels"], path=f"{path}.owner_channels", defects=defects
        )
        seen_channels: set[int] = set()
        for index, channel in enumerate(channels or []):
            channel_path = f"{path}.owner_channels[{index}]"
            if type(channel) is not int or channel < 0:
                defects.inconsistent.add(channel_path)
            elif channel in seen_channels:
                defects.inconsistent.add(channel_path)
            else:
                seen_channels.add(channel)

    valid = (
        len(defects.missing) == missing_before
        and len(defects.inconsistent) == inconsistent_before
    )
    return raw, valid


def _context_staleness(
    measured: Mapping[str, Any],
    required: Mapping[str, Any],
    defects: _Defects,
) -> None:
    for name in sorted(_CONTEXT_FIELDS):
        if measured[name] != required[name]:
            defects.stale.add(f"$evidence.measured_context.{name}")
            defects.stale.add(f"$required_context.{name}")


def _payload_matches(left: ArtifactIdentity | None, right: ArtifactIdentity | None) -> bool:
    return (
        left is not None
        and right is not None
        and left.sha256 == right.sha256
        and left.byte_size == right.byte_size
    )


def _parse_transfer(
    value: object,
    *,
    path: str,
    defects: _Defects,
) -> tuple[str | None, dict[str, ArtifactIdentity | None]]:
    raw = _strict_object(value, path=path, fields=_TRANSFER_FIELDS, defects=defects)
    artifacts: dict[str, ArtifactIdentity | None] = {}
    if raw is None:
        return None, artifacts
    for name in _TRANSFER_FIELDS - {"verdict"}:
        if name in raw:
            artifacts[name] = _artifact(raw[name], path=f"{path}.{name}", defects=defects)
    verdict = (
        _choice(
            raw["verdict"],
            path=f"{path}.verdict",
            choices=frozenset({"pass", "fail"}),
            defects=defects,
        )
        if "verdict" in raw
        else None
    )
    if verdict == "pass" and not _payload_matches(
        artifacts.get("post_limiter_pcm"), artifacts.get("reference_post_limiter_pcm")
    ):
        defects.inconsistent.add(f"{path}.verdict")
    return verdict, artifacts


def _parse_measurement(
    value: object,
    *,
    path: str,
    sweep: bool,
    defects: _Defects,
) -> dict[str, Any]:
    fields = frozenset(_SWEEP_FIELDS if sweep else _MEASUREMENT_FIELDS)
    raw = _strict_object(value, path=path, fields=fields, defects=defects)
    parsed: dict[str, Any] = {}
    if raw is None:
        return parsed

    artifact_names = {
        "stimulus",
        "admission",
        "pre_limiter_pcm",
        "post_limiter_pcm",
        "acoustic_capture",
        "signal_analysis",
        "protection_analysis",
    }
    if sweep:
        artifact_names.update(
            {
                "reference_activation_receipt",
                "reference_stimulus",
                "reference_admission",
                "reference_acoustic_capture",
                "transparency_analysis",
            }
        )
    for name in artifact_names:
        if name in raw:
            parsed[name] = _artifact(raw[name], path=f"{path}.{name}", defects=defects)

    if "stimulus_band_hz" in raw:
        band = _required_list(
            raw["stimulus_band_hz"], path=f"{path}.stimulus_band_hz", defects=defects
        )
        if band is not None and len(band) != 2:
            defects.inconsistent.add(f"{path}.stimulus_band_hz")
        elif band is not None:
            low = _positive_float(
                band[0], path=f"{path}.stimulus_band_hz[0]", defects=defects
            )
            high = _positive_float(
                band[1], path=f"{path}.stimulus_band_hz[1]", defects=defects
            )
            if low is not None and high is not None and low >= high:
                defects.inconsistent.add(f"{path}.stimulus_band_hz")

    for name in (
        "stimulus_effective_peak_dbfs",
        "commanded_main_volume_db",
        "target_boost_db",
        "pre_limiter_peak_dbfs",
        "post_limiter_peak_dbfs",
    ):
        if name in raw:
            parsed[name] = _finite_float(raw[name], path=f"{path}.{name}", defects=defects)
    if "hold_duration_s" in raw:
        _positive_float(raw["hold_duration_s"], path=f"{path}.hold_duration_s", defects=defects)
    if "required_cooldown_s" in raw:
        _nonnegative_float(
            raw["required_cooldown_s"],
            path=f"{path}.required_cooldown_s",
            defects=defects,
        )
    if "repeat_count" in raw:
        _positive_int(raw["repeat_count"], path=f"{path}.repeat_count", defects=defects)
    if "digital_clamp_passed" in raw:
        parsed["digital_clamp_passed"] = _exact_bool(
            raw["digital_clamp_passed"],
            path=f"{path}.digital_clamp_passed",
            defects=defects,
        )
    for name in ("quality_verdict", "protection_verdict"):
        if name in raw:
            parsed[name] = _choice(
                raw[name],
                path=f"{path}.{name}",
                choices=frozenset({"pass", "fail"}),
                defects=defects,
            )

    if sweep:
        for name in ("reference_target_fingerprint", "reference_active_graph_fingerprint"):
            if name in raw:
                parsed[name] = _fingerprint(
                    raw[name], path=f"{path}.{name}", defects=defects
                )
        if "reference_configured_clip_limit_dbfs" in raw:
            parsed["reference_configured_clip_limit_dbfs"] = _finite_float(
                raw["reference_configured_clip_limit_dbfs"],
                path=f"{path}.reference_configured_clip_limit_dbfs",
                defects=defects,
            )
        if "transparency_verdict" in raw:
            parsed["transparency_verdict"] = _choice(
                raw["transparency_verdict"],
                path=f"{path}.transparency_verdict",
                choices=frozenset({"pass", "fail"}),
                defects=defects,
            )
    return parsed


def _parse_source(
    value: object,
    *,
    path: str,
    defects: _Defects,
) -> dict[str, Any]:
    raw = _strict_object(value, path=path, fields=_SOURCE_FIELDS, defects=defects)
    parsed: dict[str, Any] = {}
    if raw is None:
        return parsed
    for name in ("stimulus", "admission", "active_graph_readback", "pre_limiter_pcm", "peak_analysis"):
        if name in raw:
            parsed[name] = _artifact(raw[name], path=f"{path}.{name}", defects=defects)
    if "source_fingerprint" in raw:
        parsed["source_fingerprint"] = _fingerprint(
            raw["source_fingerprint"], path=f"{path}.source_fingerprint", defects=defects
        )
    if "pre_limiter_peak_dbfs" in raw:
        parsed["pre_limiter_peak_dbfs"] = _finite_float(
            raw["pre_limiter_peak_dbfs"],
            path=f"{path}.pre_limiter_peak_dbfs",
            defects=defects,
        )
    if "source_fingerprint" in raw:
        try:
            content = {name: raw[name] for name in _SOURCE_FIELDS - {"source_fingerprint"}}
            actual = json_fingerprint(content, field_name="limiter candidate source")
        except (EvidenceIdentityError, KeyError, TypeError, ValueError):
            defects.inconsistent.add(path)
        else:
            if raw["source_fingerprint"] != actual:
                defects.inconsistent.add(f"{path}.source_fingerprint")
    return parsed


def _inside_domain(value: float | None, context: Mapping[str, Any]) -> bool:
    return (
        value is not None
        and context["limiter_domain_min_dbfs"] <= value <= context["limiter_domain_max_dbfs"]
    )


def _parse_candidate(
    value: object,
    *,
    path: str,
    target_id: str | None,
    target_fingerprint: str | None,
    sources: Mapping[str, dict[str, Any]],
    context: Mapping[str, Any],
    defects: _Defects,
) -> tuple[TargetLimiterThreshold | None, str | None, float | None]:
    raw = _strict_object(value, path=path, fields=_CANDIDATE_FIELDS, defects=defects)
    if raw is None:
        return None, None, None

    threshold = (
        _finite_float(
            raw["limiter_threshold_dbfs"],
            path=f"{path}.limiter_threshold_dbfs",
            defects=defects,
        )
        if "limiter_threshold_dbfs" in raw
        else None
    )
    configured = (
        _finite_float(
            raw["configured_clip_limit_dbfs"],
            path=f"{path}.configured_clip_limit_dbfs",
            defects=defects,
        )
        if "configured_clip_limit_dbfs" in raw
        else None
    )
    source_fp = (
        _fingerprint(raw["source_fingerprint"], path=f"{path}.source_fingerprint", defects=defects)
        if "source_fingerprint" in raw
        else None
    )
    active_target_fp = (
        _fingerprint(
            raw["active_target_fingerprint"],
            path=f"{path}.active_target_fingerprint",
            defects=defects,
        )
        if "active_target_fingerprint" in raw
        else None
    )
    active_graph_fp = (
        _fingerprint(
            raw["active_graph_fingerprint"],
            path=f"{path}.active_graph_fingerprint",
            defects=defects,
        )
        if "active_graph_fingerprint" in raw
        else None
    )

    for name in ("candidate_activation_receipt", "candidate_restoration_receipt"):
        if name in raw:
            _artifact(raw[name], path=f"{path}.{name}", defects=defects)
    restored_graph_fp = (
        _fingerprint(
            raw["restored_graph_fingerprint"],
            path=f"{path}.restored_graph_fingerprint",
            defects=defects,
        )
        if "restored_graph_fingerprint" in raw
        else None
    )

    if threshold is not None and configured is not None and threshold != configured:
        defects.inconsistent.add(f"{path}.configured_clip_limit_dbfs")
    if active_target_fp is not None and target_fingerprint is not None:
        if active_target_fp != target_fingerprint:
            defects.inconsistent.add(f"{path}.active_target_fingerprint")
    if restored_graph_fp is not None and restored_graph_fp != context["natural_graph_fingerprint"]:
        defects.inconsistent.add(f"{path}.restored_graph_fingerprint")

    source = sources.get(source_fp or "")
    if source_fp is not None and source is None:
        defects.inconsistent.add(f"{path}.source_fingerprint")
    elif source is not None and threshold is not None:
        if source.get("pre_limiter_peak_dbfs") != threshold:
            defects.inconsistent.add(f"{path}.limiter_threshold_dbfs")

    for name, numeric in (
        ("limiter_threshold_dbfs", threshold),
        ("configured_clip_limit_dbfs", configured),
    ):
        if numeric is not None and (
            not _inside_domain(numeric, context)
            or numeric > context["baseline_limiter_clip_limit_dbfs"]
        ):
            defects.out_of_envelope.add(f"{path}.{name}")

    if "ordered_owner_chain" in raw:
        chain = _required_list(
            raw["ordered_owner_chain"], path=f"{path}.ordered_owner_chain", defects=defects
        )
        names: list[str] = []
        for index, name in enumerate(chain or []):
            parsed_name = _trimmed_text(
                name, path=f"{path}.ordered_owner_chain[{index}]", defects=defects
            )
            if parsed_name is not None:
                if parsed_name in names:
                    defects.inconsistent.add(f"{path}.ordered_owner_chain[{index}]")
                names.append(parsed_name)
        expected = (*_TARGET_FILTERS, context["limiter_name"])
        try:
            positions = tuple(names.index(name) for name in expected)
        except ValueError:
            defects.inconsistent.add(f"{path}.ordered_owner_chain")
        else:
            if positions != tuple(sorted(positions)):
                defects.inconsistent.add(f"{path}.ordered_owner_chain")

    transfer_verdict, _ = (
        _parse_transfer(raw["digital_transfer_probe"], path=f"{path}.digital_transfer_probe", defects=defects)
        if "digital_transfer_probe" in raw
        else (None, {})
    )
    sweep = (
        _parse_measurement(
            raw["sweep_transparency"],
            path=f"{path}.sweep_transparency",
            sweep=True,
            defects=defects,
        )
        if "sweep_transparency" in raw
        else {}
    )
    sustain = (
        _parse_measurement(
            raw["sustain_stress"],
            path=f"{path}.sustain_stress",
            sweep=False,
            defects=defects,
        )
        if "sustain_stress" in raw
        else {}
    )

    reference_clip = sweep.get("reference_configured_clip_limit_dbfs")
    if reference_clip is not None:
        if reference_clip != context["baseline_limiter_clip_limit_dbfs"]:
            defects.inconsistent.add(
                f"{path}.sweep_transparency.reference_configured_clip_limit_dbfs"
            )
        if not _inside_domain(reference_clip, context):
            defects.out_of_envelope.add(
                f"{path}.sweep_transparency.reference_configured_clip_limit_dbfs"
            )
    if sweep.get("reference_target_fingerprint") is not None:
        if sweep["reference_target_fingerprint"] != target_fingerprint:
            defects.inconsistent.add(f"{path}.sweep_transparency.reference_target_fingerprint")
    reference_graph_fp = sweep.get("reference_active_graph_fingerprint")
    if (
        active_graph_fp is not None
        and reference_graph_fp is not None
        and threshold is not None
        and reference_clip is not None
        and (active_graph_fp == reference_graph_fp) != (threshold == reference_clip)
    ):
        defects.inconsistent.update(
            {
                f"{path}.active_graph_fingerprint",
                f"{path}.sweep_transparency.reference_active_graph_fingerprint",
            }
        )
    if not _payload_matches(sweep.get("stimulus"), sweep.get("reference_stimulus")):
        defects.inconsistent.add(f"{path}.sweep_transparency.reference_stimulus")
    if not _payload_matches(sweep.get("admission"), sweep.get("reference_admission")):
        defects.inconsistent.add(f"{path}.sweep_transparency.reference_admission")

    disposition = (
        _choice(
            raw["disposition"],
            path=f"{path}.disposition",
            choices=frozenset({"accepted", "limiter_transparency_failed"}),
            defects=defects,
        )
        if "disposition" in raw
        else None
    )
    base_pass = (
        transfer_verdict == "pass"
        and sweep.get("quality_verdict") == "pass"
        and sweep.get("protection_verdict") == "pass"
        and sweep.get("digital_clamp_passed") is True
        and sustain.get("quality_verdict") == "pass"
        and sustain.get("protection_verdict") == "pass"
        and sustain.get("digital_clamp_passed") is True
    )
    expected_disposition = None
    if base_pass and sweep.get("transparency_verdict") == "pass":
        expected_disposition = "accepted"
    elif base_pass and sweep.get("transparency_verdict") == "fail":
        expected_disposition = "limiter_transparency_failed"
    if disposition is not None and disposition != expected_disposition:
        defects.inconsistent.add(f"{path}.disposition")

    selected = None
    if (
        disposition == "accepted"
        and expected_disposition == "accepted"
        and target_id is not None
        and target_fingerprint is not None
        and threshold is not None
        and source_fp is not None
    ):
        selected = TargetLimiterThreshold(
            target_id,
            target_fingerprint,
            threshold,
            source_fp,
        )
    return selected, disposition, threshold


def _parse_target(
    value: object,
    *,
    path: str,
    expected_target: Mapping[str, Any] | None,
    context: Mapping[str, Any],
    defects: _Defects,
) -> TargetLimiterThreshold | None:
    raw = _strict_object(value, path=path, fields=_TARGET_FIELDS, defects=defects)
    if raw is None:
        return None
    target_id = (
        _trimmed_text(raw["target_id"], path=f"{path}.target_id", defects=defects)
        if "target_id" in raw
        else None
    )
    target_fp = (
        _fingerprint(
            raw["target_fingerprint"], path=f"{path}.target_fingerprint", defects=defects
        )
        if "target_fingerprint" in raw
        else None
    )
    if expected_target is not None:
        if target_id is not None and target_id != expected_target["target_id"]:
            defects.inconsistent.add(f"{path}.target_id")
        if target_fp is not None and target_fp != expected_target["target_fingerprint"]:
            defects.inconsistent.add(f"{path}.target_fingerprint")

    result = raw.get("result")
    if not isinstance(result, Mapping):
        if "result" in raw:
            defects.inconsistent.add(f"{path}.result")
        return None
    disposition_value = result.get("disposition")
    if disposition_value == "refused" or disposition_value == "aborted":
        stop = _strict_object(
            result, path=f"{path}.result", fields=_STOP_RESULT_FIELDS, defects=defects
        )
        if stop is not None:
            if "stop_receipt" in stop:
                _artifact(
                    stop["stop_receipt"], path=f"{path}.result.stop_receipt", defects=defects
                )
            if "partial_artifacts" in stop:
                partial = _required_list(
                    stop["partial_artifacts"],
                    path=f"{path}.result.partial_artifacts",
                    defects=defects,
                    allow_empty=True,
                )
                for index, artifact in enumerate(partial or []):
                    _artifact(
                        artifact,
                        path=f"{path}.result.partial_artifacts[{index}]",
                        defects=defects,
                    )
            defects.out_of_envelope.add(f"{path}.result.disposition")
        return None
    if disposition_value != "evaluated":
        if "disposition" not in result:
            defects.missing.add(f"{path}.result.disposition")
        else:
            defects.inconsistent.add(f"{path}.result.disposition")
        return None

    evaluated = _strict_object(
        result, path=f"{path}.result", fields=_EVALUATED_RESULT_FIELDS, defects=defects
    )
    if evaluated is None:
        return None
    for name in ("discovery_activation_receipt", "discovery_restoration_receipt"):
        if name in evaluated:
            _artifact(evaluated[name], path=f"{path}.result.{name}", defects=defects)

    source_items = (
        _required_list(
            evaluated["candidate_sources"],
            path=f"{path}.result.candidate_sources",
            defects=defects,
        )
        if "candidate_sources" in evaluated
        else None
    )
    sources: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(source_items or []):
        source_path = f"{path}.result.candidate_sources[{index}]"
        source = _parse_source(item, path=source_path, defects=defects)
        source_fp = source.get("source_fingerprint")
        if source_fp is not None:
            if source_fp in sources:
                defects.inconsistent.add(f"{source_path}.source_fingerprint")
            sources[source_fp] = source
        peak = source.get("pre_limiter_peak_dbfs")
        if peak is not None and (
            not _inside_domain(peak, context)
            or peak > context["baseline_limiter_clip_limit_dbfs"]
        ):
            defects.out_of_envelope.add(f"{source_path}.pre_limiter_peak_dbfs")

    candidate_items = (
        _required_list(
            evaluated["candidates_least_to_most_permissive"],
            path=f"{path}.result.candidates_least_to_most_permissive",
            defects=defects,
        )
        if "candidates_least_to_most_permissive" in evaluated
        else None
    )
    selected: TargetLimiterThreshold | None = None
    previous_threshold: float | None = None
    accepted_seen = False
    for index, item in enumerate(candidate_items or []):
        candidate_path = f"{path}.result.candidates_least_to_most_permissive[{index}]"
        candidate, disposition, threshold = _parse_candidate(
            item,
            path=candidate_path,
            target_id=target_id,
            target_fingerprint=target_fp,
            sources=sources,
            context=context,
            defects=defects,
        )
        if threshold is not None:
            if previous_threshold is not None and threshold <= previous_threshold:
                defects.inconsistent.add(f"{candidate_path}.limiter_threshold_dbfs")
            previous_threshold = threshold
        if accepted_seen:
            defects.out_of_envelope.add(candidate_path)
        if candidate is not None:
            accepted_seen = True
            selected = candidate
    if not accepted_seen:
        defects.out_of_envelope.add(
            f"{path}.result.candidates_least_to_most_permissive"
        )
    return selected


def _produce(
    evidence: object,
    required_context: object,
) -> LimiterThresholdSet | LimiterEvidenceRefusal:
    defects = _Defects.empty()
    required, required_valid = _context(
        required_context, path="$required_context", defects=defects
    )
    root = _strict_object(
        evidence, path="$evidence", fields=_ROOT_FIELDS, defects=defects
    )
    measured: Mapping[str, Any] | None = None
    measured_valid = False
    declared_evidence_fp: str | None = None
    selected: list[TargetLimiterThreshold] = []

    if root is not None:
        if "kind" in root and root["kind"] != _EVIDENCE_KIND:
            defects.inconsistent.add("$evidence.kind")
        if "schema_version" in root and (
            type(root["schema_version"]) is not int
            or root["schema_version"] != LIMITER_EVIDENCE_SCHEMA_VERSION
        ):
            defects.inconsistent.add("$evidence.schema_version")
        if "protocol_revision" in root and (
            root["protocol_revision"] != LIMITER_EVIDENCE_PROTOCOL_REVISION
        ):
            defects.inconsistent.add("$evidence.protocol_revision")
        if "evidence_fingerprint" in root:
            declared_evidence_fp = _fingerprint(
                root["evidence_fingerprint"],
                path="$evidence.evidence_fingerprint",
                defects=defects,
            )
        if "measured_context" in root:
            measured, measured_valid = _context(
                root["measured_context"],
                path="$evidence.measured_context",
                defects=defects,
            )
        if "campaign_manifest" in root:
            _artifact(
                root["campaign_manifest"], path="$evidence.campaign_manifest", defects=defects
            )
        if "retained_facts" in root:
            retained = _strict_object(
                root["retained_facts"],
                path="$evidence.retained_facts",
                fields=_RETAINED_FACTS,
                defects=defects,
            )
            if retained is not None:
                for name in sorted(_RETAINED_FACTS):
                    if name not in retained:
                        continue
                    fact_path = f"$evidence.retained_facts.{name}"
                    fact = _strict_object(
                        retained[name],
                        path=fact_path,
                        fields=_RETAINED_FACT_FIELDS,
                        defects=defects,
                    )
                    if fact is None:
                        continue
                    if "status" in fact and fact["status"] != "replaced":
                        defects.inconsistent.add(f"{fact_path}.status")
                    if "artifact" in fact:
                        _artifact(fact["artifact"], path=f"{fact_path}.artifact", defects=defects)

    if measured_valid and required_valid and measured is not None and required is not None:
        _context_staleness(measured, required, defects)

    active_context = measured if measured_valid else required if required_valid else None
    if root is not None and active_context is not None and "targets" in root:
        targets = _required_list(root["targets"], path="$evidence.targets", defects=defects)
        expected_order = active_context["target_order"]
        if targets is not None and len(targets) != len(expected_order):
            defects.inconsistent.add("$evidence.targets")
        seen_ids: set[str] = set()
        seen_fingerprints: set[str] = set()
        for index, item in enumerate(targets or []):
            target_path = f"$evidence.targets[{index}]"
            expected = expected_order[index] if index < len(expected_order) else None
            threshold = _parse_target(
                item,
                path=target_path,
                expected_target=expected,
                context=active_context,
                defects=defects,
            )
            if isinstance(item, Mapping):
                target_id = item.get("target_id")
                target_fp = item.get("target_fingerprint")
                if type(target_id) is str:
                    if target_id in seen_ids:
                        defects.inconsistent.add(f"{target_path}.target_id")
                    seen_ids.add(target_id)
                if type(target_fp) is str:
                    if target_fp in seen_fingerprints:
                        defects.inconsistent.add(f"{target_path}.target_fingerprint")
                    seen_fingerprints.add(target_fp)
            if threshold is not None:
                selected.append(threshold)

    if root is not None and "evidence_fingerprint" in root:
        try:
            fingerprint_payload = {
                name: item for name, item in root.items() if name != "evidence_fingerprint"
            }
            actual_evidence_fp = json_fingerprint(
                fingerprint_payload, field_name="limiter evidence bundle"
            )
        except (EvidenceIdentityError, KeyError, TypeError, ValueError):
            defects.inconsistent.add("$evidence")
        else:
            if root["evidence_fingerprint"] != actual_evidence_fp:
                defects.inconsistent.add("$evidence.evidence_fingerprint")

    if len(selected) >= 2:
        for index in range(len(selected) - 1):
            if selected[index].limiter_threshold_dbfs > selected[index + 1].limiter_threshold_dbfs:
                defects.out_of_envelope.update(
                    {
                        f"$evidence.targets[{index}].result.candidates_least_to_most_permissive",
                        f"$evidence.targets[{index + 1}].result.candidates_least_to_most_permissive",
                    }
                )

    refusal = defects.refusal()
    if refusal is not None:
        return refusal
    assert declared_evidence_fp is not None
    assert required is not None
    return LimiterThresholdSet(
        evidence_fingerprint=declared_evidence_fp,
        required_context_fingerprint=json_fingerprint(
            required, field_name="required limiter context"
        ),
        targets=tuple(selected),
    )


def produce_limiter_thresholds(
    evidence: object,
    *,
    required_context: object,
) -> LimiterThresholdSet | LimiterEvidenceRefusal:
    """Return measured thresholds or one typed, deterministic refusal.

    Evidence content is a total input domain: malformed or unacceptable values
    never escape as exceptions and never receive defaults.
    """

    try:
        materialized_context = _materialize_json_input(required_context)
    except _TOTAL_INPUT_ERRORS:
        return _root_input_refusal("$required_context")
    try:
        materialized_evidence = _materialize_json_input(evidence)
    except _TOTAL_INPUT_ERRORS:
        return _root_input_refusal("$evidence")
    return _produce(materialized_evidence, materialized_context)
