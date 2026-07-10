# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical capture-plan contract for the wake-corpus recorder."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

PLAN_ID_ENV = "JASPER_WAKE_CORPUS_PLAN_ID"
EXPECTED_LEGS_ENV = "JASPER_WAKE_CORPUS_EXPECTED_LEGS"
MIC_FINGERPRINT_ENV = "JASPER_WAKE_CORPUS_MIC_FINGERPRINT"
DAC_FINGERPRINT_ENV = "JASPER_WAKE_CORPUS_DAC_FINGERPRINT"

PLAN_ENV_VARS = (
    PLAN_ID_ENV,
    EXPECTED_LEGS_ENV,
    MIC_FINGERPRINT_ENV,
    DAC_FINGERPRINT_ENV,
)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stable_digest(value: Any, *, length: int = 16) -> str:
    digest = hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()
    return digest[:length]


def _fingerprint_value(value: Any) -> str:
    return stable_digest(value, length=16)


def _contract_hash_source(data: Mapping[str, Any]) -> dict[str, Any]:
    raw_bridge = data.get("bridge")
    bridge: Mapping[str, Any] = raw_bridge if isinstance(raw_bridge, Mapping) else {}
    return {
        "schema_version": data.get("schema_version"),
        "corpus_profile": data.get("corpus_profile"),
        "recipe": data.get("recipe"),
        "selected_legs": data.get("selected_legs"),
        "legs": data.get("legs"),
        "required_bridge_outputs": data.get("required_bridge_outputs"),
        "required_bridge_env": data.get("required_bridge_env")
        or bridge.get("required_env"),
        "expected_emitted_legs": data.get("expected_emitted_legs"),
        "flags": data.get("flags"),
        "fingerprints": data.get("fingerprints"),
    }


@dataclass(frozen=True)
class WakeCorpusCapturePlan:
    """Resolved wake-corpus plan stored in metadata and applied to the bridge."""

    data: dict[str, Any]

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        assign_plan_id: bool = False,
    ) -> "WakeCorpusCapturePlan":
        payload = json.loads(json.dumps(dict(data), default=str))
        plan_id = str(payload.get("plan_id") or "").strip()
        if not plan_id and assign_plan_id:
            plan_id = stable_digest(_contract_hash_source(payload), length=16)
            payload["plan_id"] = plan_id
        return cls(payload)

    @property
    def plan_id(self) -> str:
        return str(self.data.get("plan_id") or "")

    @property
    def selected_legs(self) -> tuple[str, ...]:
        raw = self.data.get("selected_legs")
        return tuple(str(leg) for leg in raw) if isinstance(raw, list) else ()

    @property
    def expected_emitted_legs(self) -> tuple[str, ...]:
        raw = self.data.get("expected_emitted_legs")
        if isinstance(raw, list):
            return tuple(str(leg) for leg in raw)
        return self.selected_legs

    @property
    def required_bridge_outputs(self) -> tuple[str, ...]:
        raw = self.data.get("required_bridge_outputs")
        if isinstance(raw, list):
            return tuple(str(item) for item in raw)
        bridge = self.data.get("bridge")
        if isinstance(bridge, Mapping) and isinstance(
            bridge.get("required_outputs"), list,
        ):
            return tuple(str(item) for item in bridge["required_outputs"])
        return ()

    @property
    def mic_fingerprint(self) -> str:
        fp = self.data.get("fingerprints")
        if isinstance(fp, Mapping):
            return str(fp.get("mic") or "")
        return ""

    @property
    def dac_reference_fingerprint(self) -> str:
        fp = self.data.get("fingerprints")
        if isinstance(fp, Mapping):
            return str(fp.get("dac_reference") or "")
        return ""

    def env_overrides(self) -> dict[str, str]:
        """Return bridge env values that activate this exact plan."""

        raw_bridge = self.data.get("bridge")
        bridge = raw_bridge if isinstance(raw_bridge, Mapping) else {}
        raw = self.data.get("required_bridge_env") or bridge.get("required_env") or {}
        values = {str(k): str(v) for k, v in dict(raw).items()}
        values[PLAN_ID_ENV] = self.plan_id
        values[EXPECTED_LEGS_ENV] = ",".join(self.expected_emitted_legs)
        if self.mic_fingerprint:
            values[MIC_FINGERPRINT_ENV] = self.mic_fingerprint
        if self.dac_reference_fingerprint:
            values[DAC_FINGERPRINT_ENV] = self.dac_reference_fingerprint
        return values

    def to_json(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.data, default=str))


@dataclass(frozen=True)
class PlanConformance:
    ok: bool
    status: str
    active_plan_id: str = ""
    expected_plan_id: str = ""
    emitted_legs: list[str] = field(default_factory=list)
    missing_emitted_legs: list[str] = field(default_factory=list)
    fingerprint_mismatches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def fingerprint_mapping(value: Mapping[str, Any]) -> str:
    return _fingerprint_value(value)


def build_capture_plan(
    request: Mapping[str, Any],
    ports: Mapping[str, int],
    runtime_snapshot: Mapping[str, Any] | None = None,
) -> WakeCorpusCapturePlan:
    """Build a canonical plan from a request mapping.

    This is the compact public entry point described by the recorder contract.
    The existing bridge-session builder owns the domain vocabulary, so this
    function delegates there lazily to avoid an import cycle.
    """

    from . import bridge_session

    plan = bridge_session.build_capture_plan(
        dict(ports),
        corpus_profile=str(
            request.get("corpus_profile") or bridge_session.PROFILE_STANDARD,
        ),
        include_dtln=bool(request.get("include_dtln", True)),
        include_raw_mic_0=bool(request.get("include_raw_mic_0", False)),
        include_usb_mic=bool(request.get("include_usb_mic", False)),
        include_usb_dtln=bool(request.get("include_usb_dtln", False)),
        include_xvf_raw0_dtln=bool(request.get("include_xvf_raw0_dtln", False)),
        include_aec3_sweep=bool(request.get("include_aec3_sweep", False)),
        aec3_sweep_source=request.get("aec3_sweep_source"),
        include_bridge_readiness=True,
        include_runtime_profile=True,
        runtime_snapshot=runtime_snapshot,
    )
    return WakeCorpusCapturePlan.from_mapping(plan)
