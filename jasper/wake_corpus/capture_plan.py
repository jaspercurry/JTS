# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical capture-plan contract for the wake-corpus recorder."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
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

# Corpus-profile vocabulary and plan-schema identity. bridge_session imports
# these back (its own corpus-profile handling predates this module).
PROFILE_STANDARD = "standard"
PROFILE_CHIP_AEC_COMPARISON = "chip_aec_comparison_v1"
CORPUS_PROFILES = (PROFILE_STANDARD, PROFILE_CHIP_AEC_COMPARISON)
CAPTURE_PLAN_SCHEMA_VERSION = 1
CAPTURE_PLAN_STATE_PREVIEW = "preview"
CAPTURE_PLAN_STATE_SESSION = "session"
_CAPTURE_PLAN_PROBE_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    LookupError,
    subprocess.SubprocessError,
)

logger = logging.getLogger("jasper-wake-corpus-web")


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


def _capture_plan_runtime_context() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Best-effort runtime overlay for capture-plan labels.

    This is metadata-only. It mirrors the audio-context probe path and must not
    block recording when local env/hardware probes are unavailable.
    """
    from . import bridge_session

    try:
        intent = bridge_session._read_aec_intent()
        system_env = bridge_session.read_env_file(str(bridge_session.SYSTEM_ENV_PATH))
        runtime = bridge_session.runtime_env_from_mapping(
            system_env, process_env=os.environ,
        )
        mic_probe, _ = bridge_session._mic_probe_and_identity()
        chip_gate = bridge_session._chip_aec_gate_for_status(system_env, intent)
        bridge_active = bridge_session.aec_bridge_active()
        profile_status = bridge_session.build_audio_profile_status(
            intent,
            runtime,
            mic_probe,
            bridge_active=bridge_active,
            chip_available=bridge_session._mic_chip_aec_available(mic_probe),
            chip_gate=chip_gate,
        )
    except Exception as e:  # noqa: BLE001 - advisory metadata only
        bridge_session.log_event(
            logger,
            "wake_corpus.capture_plan_runtime_snapshot_failed",
            error=e,
            level=logging.DEBUG,
        )
        return None, None
    runtime_dict = asdict(runtime)
    runtime_dict["bridge_active"] = bridge_active
    return profile_status["audio_profile"], runtime_dict


def _capture_plan_runtime_snapshot() -> dict[str, Any]:
    """Snapshot hardware/runtime identity used by the capture-plan hash."""
    from . import bridge_session

    system_env = bridge_session.read_env_file(str(bridge_session.SYSTEM_ENV_PATH))
    bridge_env = bridge_session._read_bridge_env()
    merged_env = {**system_env, **bridge_env}
    intent = bridge_session._read_aec_intent()
    runtime = bridge_session.runtime_env_from_mapping(system_env, process_env=os.environ)
    mic_probe, mic_identity = bridge_session._mic_probe_and_identity()
    bridge_outputs = bridge_session.bridge_output_status()
    chip_gate = bridge_session._chip_aec_gate_for_status(system_env, intent)
    bridge_active = bridge_session.aec_bridge_active()
    profile_status = bridge_session.build_audio_profile_status(
        intent,
        runtime,
        mic_probe,
        bridge_active=bridge_active,
        chip_available=bridge_session._mic_chip_aec_available(mic_probe),
        chip_gate=chip_gate,
    )
    runtime_dict = asdict(runtime)
    runtime_dict["bridge_active"] = bridge_active
    validation = bridge_session._validation_artifact_summary(
        requested_profile=profile_status["audio_profile"].get("requested"),
        mic_probe=mic_probe,
        system_env=merged_env,
    )
    dac_reference = bridge_session._dac_reference_context(
        merged_env,
        bridge_outputs,
        process_env=os.environ,
        validation=validation,
    )
    selected_usb_mic = merged_env.get(
        "JASPER_AEC_USB_MIC_DEVICE",
        bridge_session.DEFAULT_USB_MIC_DEVICE,
    )
    mic_fingerprint_source = {
        "family": mic_identity.get("family"),
        "variant_id": mic_identity.get("variant_id"),
        "geometry": mic_identity.get("geometry"),
        "chip_beam_plan": mic_identity.get("chip_beam_plan"),
        "chip_aec_supported": mic_identity.get("chip_aec_supported"),
        "usb_vid_pid": mic_identity.get("usb_vid_pid"),
        "alsa_card": mic_identity.get("alsa_card"),
        "capture_channels": (
            mic_identity.get("observed", {})
            if isinstance(mic_identity.get("observed"), dict) else {}
        ).get("capture_channels"),
        "selected_xvf_mic_device": merged_env.get(bridge_session.AEC_MIC_DEVICE_ENV, ""),
        "selected_usb_mic_device": selected_usb_mic,
        "chip_primary_leg": merged_env.get(bridge_session.CHIP_AEC_PRIMARY_LEG_ENV, ""),
    }
    dac_reference_fingerprint_source = {
        "audio_dac_id": bridge_session.published_dac_id(system_env),
        "dac": dac_reference.get("dac"),
        "reference": dac_reference.get("reference"),
        "chip_gate": bridge_session.chip_gate_identity(chip_gate),
    }
    return {
        # The builder may overlay its desired recorder-owned bridge env and
        # recompute the identity from these sources before hashing the plan.
        # This keeps "plan to apply" and "plan observed after apply" identical
        # without mutating the live env merely to discover its future hash.
        "identity_recomputable": True,
        "system_env": system_env,
        "bridge_env": bridge_env,
        "merged_env": merged_env,
        "active_audio_profile": profile_status["audio_profile"],
        "runtime_audio_env": runtime_dict,
        "mic_identity": mic_identity,
        "dac_reference": dac_reference,
        "bridge_outputs": bridge_outputs,
        "fingerprint_sources": {
            "mic": mic_fingerprint_source,
            "dac_reference": dac_reference_fingerprint_source,
        },
        "fingerprints": {
            "mic": fingerprint_mapping(mic_fingerprint_source),
            "dac_reference": fingerprint_mapping(
                dac_reference_fingerprint_source,
            ),
        },
    }


def _capture_plan_snapshot_for_desired_env(
    runtime_snapshot: Mapping[str, Any],
    *,
    required_env: Mapping[str, str],
    required_outputs: list[str],
) -> dict[str, Any]:
    """Return the plan identity as it will look after its env is applied.

    Recorder-owned reference and optional-mic env is part of the plan itself.
    Hashing the *currently active* env creates a circular identity: applying the
    plan changes the fingerprint and therefore changes the plan id.  Production
    snapshots carry the raw fingerprint sources, so resolve those sources
    against the desired env once, before the id is assigned.  Synthetic/legacy
    snapshots without that marker retain their supplied fingerprints.
    """
    from . import bridge_session

    snapshot = json.loads(json.dumps(dict(runtime_snapshot), default=str))
    if snapshot.get("identity_recomputable") is not True:
        return snapshot
    sources = snapshot.get("fingerprint_sources")
    if not isinstance(sources, Mapping):
        return snapshot
    mic_source_raw = sources.get("mic")
    dac_source_raw = sources.get("dac_reference")
    if not isinstance(mic_source_raw, Mapping) or not isinstance(
        dac_source_raw, Mapping
    ):
        return snapshot

    merged_raw = snapshot.get("merged_env")
    desired_env = dict(merged_raw) if isinstance(merged_raw, Mapping) else {}
    desired_env.update({str(k): str(v) for k, v in required_env.items()})

    desired_outputs_raw = snapshot.get("bridge_outputs")
    desired_outputs = (
        dict(desired_outputs_raw)
        if isinstance(desired_outputs_raw, Mapping)
        else {}
    )
    for output in required_outputs:
        desired_outputs[str(output)] = True

    mic_source = dict(mic_source_raw)
    mic_source.update({
        "selected_xvf_mic_device": desired_env.get(bridge_session.AEC_MIC_DEVICE_ENV, ""),
        "selected_usb_mic_device": desired_env.get(
            "JASPER_AEC_USB_MIC_DEVICE", bridge_session.DEFAULT_USB_MIC_DEVICE
        ),
        "chip_primary_leg": desired_env.get(
            bridge_session.CHIP_AEC_PRIMARY_LEG_ENV, ""
        ),
    })

    prior_context = snapshot.get("dac_reference")
    prior_validation_raw = (
        prior_context.get("validation") if isinstance(prior_context, Mapping) else None
    )
    prior_validation: Mapping[str, Any] = (
        prior_validation_raw
        if isinstance(prior_validation_raw, Mapping)
        else {"status": "unknown"}
    )
    desired_dac_context = bridge_session._dac_reference_context(
        desired_env,
        desired_outputs,
        process_env={},
        validation=dict(prior_validation),
    )
    dac_source = dict(dac_source_raw)
    dac_source.update({
        "dac": desired_dac_context.get("dac"),
        "reference": desired_dac_context.get("reference"),
    })

    snapshot["merged_env"] = desired_env
    snapshot["bridge_outputs"] = desired_outputs
    snapshot["dac_reference"] = desired_dac_context
    snapshot["fingerprint_sources"] = {
        **dict(sources),
        "mic": mic_source,
        "dac_reference": dac_source,
    }
    snapshot["fingerprints"] = {
        "mic": fingerprint_mapping(mic_source),
        "dac_reference": fingerprint_mapping(dac_source),
    }
    return snapshot


def _bridge_env_overrides_for_request(
    *,
    system_env: Mapping[str, str],
    merged_env: Mapping[str, str],
    corpus_profile: str,
    include_dtln: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_xvf_raw0_dtln: bool,
    include_aec3_sweep: bool,
    aec3_sweep_source: str,
) -> dict[str, str]:
    from . import bridge_session

    values: dict[str, str] = {}
    dtln_enabled = system_env.get(bridge_session.DTLN_ENABLED_ENV)
    if include_dtln and not bridge_session._env_truthy(dtln_enabled):
        values[bridge_session.DTLN_ENABLED_ENV] = "1"
    elif (
        (include_aec3_sweep or corpus_profile == PROFILE_CHIP_AEC_COMPARISON)
        and not include_dtln
    ):
        values[bridge_session.DTLN_ENABLED_ENV] = "0"

    sweep_needs_usb = (
        include_aec3_sweep and aec3_sweep_source == bridge_session.AEC3_SWEEP_SOURCE_USB
    )
    needs_ref = corpus_profile == PROFILE_CHIP_AEC_COMPARISON
    needs_usb = include_usb_mic or include_usb_dtln or sweep_needs_usb
    if needs_ref or needs_usb:
        values["JASPER_AEC_CORPUS_REF_ENABLED"] = "1"
    if needs_usb:
        values["JASPER_AEC_CORPUS_USB_ENABLED"] = "1"
        if "JASPER_AEC_USB_MIC_DEVICE" not in system_env:
            values["JASPER_AEC_USB_MIC_DEVICE"] = merged_env.get(
                "JASPER_AEC_USB_MIC_DEVICE",
                bridge_session.DEFAULT_USB_MIC_DEVICE,
            )
    if include_usb_dtln:
        values[bridge_session.CORPUS_USB_DTLN_ENABLED_ENV] = "1"
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        values[bridge_session.CORPUS_CHIP_AEC_ENABLED_ENV] = "1"
        values["JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED"] = "1"
        values[bridge_session.REF_SOURCE_ENV] = "outputd_udp"
        values[bridge_session.OUTPUTD_REF_UDP_HOST_ENV] = "127.0.0.1"
        values[bridge_session.OUTPUTD_REF_UDP_PORT_ENV] = (
            bridge_session.OUTPUTD_REF_UDP_PORT
        )
        values["JASPER_OUTPUTD_CHIP_REF_PCM"] = (
            bridge_session.chip_ref_pcm_for_env(system_env)
        )
        values["JASPER_OUTPUTD_REFERENCE_UDP_TARGET"] = (
            bridge_session.OUTPUTD_REF_UDP_TARGET
        )
        values["JASPER_OUTPUTD_CHIP_REF_SAMPLE_RATE"] = (
            bridge_session.DEFAULT_CHIP_REF_SAMPLE_RATE
        )
        values["JASPER_OUTPUTD_CHIP_REF_PERIOD_FRAMES"] = (
            bridge_session.DEFAULT_CHIP_REF_PERIOD_FRAMES
        )
        values["JASPER_OUTPUTD_CHIP_REF_BUFFER_FRAMES"] = (
            bridge_session.DEFAULT_CHIP_REF_BUFFER_FRAMES
        )
    if include_xvf_raw0_dtln:
        values["JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED"] = "1"
    if include_aec3_sweep:
        values[bridge_session.AEC3_SWEEP_ENV_FLAG] = "1"
        values[bridge_session.AEC3_SWEEP_SOURCE_ENV] = aec3_sweep_source
    return values


def _capture_plan_leg_detail(
    leg: str,
    ports: dict[str, int],
    *,
    aec3_sweep_source: str,
    active_audio_profile: Mapping[str, Any] | None = None,
    runtime_audio_env: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from . import bridge_session

    if (
        leg in bridge_session.AEC3_SWEEP_LEGS
        or leg in bridge_session.LEGACY_AEC3_SWEEP_LEGS
    ):
        source_device = (
            "usb_mic"
            if aec3_sweep_source == bridge_session.AEC3_SWEEP_SOURCE_USB
            else "xvf3800"
        )
        source_label = (
            "USB microphone" if source_device == "usb_mic" else "ReSpeaker XVF3800"
        )
        source_channel = (
            "mono_capture" if source_device == "usb_mic" else "chip_asr_beam"
        )
        return {
            **bridge_session._leg_detail(leg, ports, aec3_sweep_source=aec3_sweep_source),
            "device_id": source_device,
            "device_label": source_label,
            "native_stream": f"{source_device}_aec3_sweep_source",
            "source_channel": source_channel,
            "processing": "webrtc_aec3_sweep",
            "processing_label": "WebRTC AEC3 sweep variant",
            "requires": ["reference"] + (
                ["usb_mic"] if source_device == "usb_mic" else []
            ),
            "resource_weight": 2,
        }

    info = bridge_session._LEG_PLAN_INFO.get(leg, {})
    detail = bridge_session._leg_detail(leg, ports, aec3_sweep_source=aec3_sweep_source)
    if leg == "on":
        overlay = bridge_session._primary_on_leg_overlay(
            active_audio_profile=active_audio_profile,
            runtime_audio_env=runtime_audio_env,
        )
        if overlay is not None:
            return {
                **detail,
                "device_id": info.get("device_id", "unknown"),
                "device_label": info.get("device_label", "Unknown source"),
                **overlay,
            }
    return {
        **detail,
        "device_id": info.get("device_id", "unknown"),
        "device_label": info.get("device_label", "Unknown source"),
        "native_stream": info.get("native_stream", "unknown"),
        "source_channel": info.get("source_channel", "unknown"),
        "processing": info.get("processing", detail["kind"]),
        "processing_label": info.get("processing_label", detail["kind"]),
        "requires": list(info.get("requires", ())),
        "resource_weight": int(info.get("cost", 1)),
    }


def _resource_level(total_weight: int) -> str:
    if total_weight <= 6:
        return "low"
    if total_weight <= 10:
        return "medium"
    if total_weight <= 15:
        return "high"
    return "unsafe"


def _capture_plan_recipe(
    *,
    corpus_profile: str,
    include_aec3_sweep: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_xvf_raw0_dtln: bool,
) -> str:
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        if include_usb_mic or include_usb_dtln or include_xvf_raw0_dtln:
            return "chip_aec_comparison_extended"
        return "chip_aec_comparison"
    if include_aec3_sweep:
        return "software_aec3_sweep"
    if include_usb_mic or include_usb_dtln:
        return "two_mic_comparison"
    return "single_mic_comparison"


def _capture_plan_from_legs(
    *,
    corpus_profile: str,
    enabled_legs: tuple[str, ...],
    ports: dict[str, int],
    include_raw_mic_0: bool,
    include_dtln: bool,
    include_usb_mic: bool,
    include_usb_dtln: bool,
    include_xvf_raw0_dtln: bool,
    include_aec3_sweep: bool,
    aec3_sweep_source: str,
    missing_bridge_outputs: list[str] | None = None,
    required_bridge_outputs: list[str] | None = None,
    required_bridge_env: Mapping[str, str] | None = None,
    runtime_snapshot: Mapping[str, Any] | None = None,
    plan_state: str = CAPTURE_PLAN_STATE_PREVIEW,
    active_audio_profile: Mapping[str, Any] | None = None,
    runtime_audio_env: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from . import bridge_session

    leg_details = [
        _capture_plan_leg_detail(
            leg,
            ports,
            aec3_sweep_source=aec3_sweep_source,
            active_audio_profile=active_audio_profile,
            runtime_audio_env=runtime_audio_env,
        )
        for leg in enabled_legs
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for detail in leg_details:
        device_id = str(detail["device_id"])
        device = grouped.setdefault(
            device_id,
            {
                "device_id": device_id,
                "label": detail["device_label"],
                "kind": (
                    "reference" if device_id == "speaker_reference" else "microphone"
                ),
                "legs": [],
            },
        )
        device["legs"].append(detail["token"])

    mic_ids = [
        device_id for device_id, device in grouped.items()
        if device.get("kind") == "microphone"
    ]
    total_weight = sum(int(detail["resource_weight"]) for detail in leg_details)
    resource_level = _resource_level(total_weight)
    dtln_legs = [
        detail["token"] for detail in leg_details
        if detail.get("processing") == "dtln"
    ]
    software_aec_legs = [
        detail["token"] for detail in leg_details
        if str(detail.get("processing", "")).startswith("webrtc_aec3")
    ]
    warnings: list[str] = []
    if len(mic_ids) > 1:
        warnings.append(
            "Recording multiple microphones is useful for comparison, but "
            "it increases bridge fan-out and file count.",
        )
    if len(dtln_legs) > 1:
        warnings.append(
            "Multiple DTLN legs are CPU/RAM heavy on small Pis; review "
            "capture_health before using these clips for training.",
        )
    if include_aec3_sweep:
        warnings.append(
            "AEC3 sweep records several software-AEC variants from one "
            "source; leave DTLN off unless you are intentionally stress-testing.",
        )
    if resource_level == "high":
        warnings.append(
            "This is a high-load capture plan. Watch for warnings or "
            "compromised capture_health before trusting the session.",
        )
    elif resource_level == "unsafe":
        warnings.append(
            "This capture plan is likely too heavy for a 1 GB Pi. Prefer "
            "a smaller comparison set or record in separate sessions.",
        )
    if missing_bridge_outputs:
        labels = [
            bridge_session.BRIDGE_OUTPUT_LABELS.get(key, key)
            for key in missing_bridge_outputs
        ]
        warnings.append(
            "The bridge is not currently emitting required output(s): "
            + ", ".join(labels),
        )

    fingerprints = {}
    fingerprint_sources = {}
    if isinstance(runtime_snapshot, Mapping):
        raw_fingerprints = runtime_snapshot.get("fingerprints")
        if isinstance(raw_fingerprints, Mapping):
            fingerprints = dict(raw_fingerprints)
        raw_sources = runtime_snapshot.get("fingerprint_sources")
        if isinstance(raw_sources, Mapping):
            fingerprint_sources = dict(raw_sources)

    plan = {
        "schema_version": CAPTURE_PLAN_SCHEMA_VERSION,
        "state": plan_state,
        "recipe": _capture_plan_recipe(
            corpus_profile=corpus_profile,
            include_aec3_sweep=include_aec3_sweep,
            include_usb_mic=include_usb_mic,
            include_usb_dtln=include_usb_dtln,
            include_xvf_raw0_dtln=include_xvf_raw0_dtln,
        ),
        "corpus_profile": corpus_profile,
        "selected_legs": list(enabled_legs),
        "expected_emitted_legs": list(enabled_legs),
        "selected_physical_mics": mic_ids,
        "devices": list(grouped.values()),
        "legs": leg_details,
        "software_transforms": {
            "webrtc_aec3": software_aec_legs,
            "dtln": dtln_legs,
        },
        "resource": {
            "weight": total_weight,
            "level": resource_level,
            "warning_count": len(warnings),
        },
        "bridge": {
            "required_outputs": list(required_bridge_outputs or []),
            "required_env": dict(required_bridge_env or {}),
            "missing_outputs": list(missing_bridge_outputs or []),
        },
        "required_bridge_outputs": list(required_bridge_outputs or []),
        "required_bridge_env": dict(required_bridge_env or {}),
        "fingerprints": fingerprints,
        "fingerprint_sources": fingerprint_sources,
        "flags": {
            "include_raw_mic_0": include_raw_mic_0,
            "include_dtln": include_dtln,
            "include_usb_mic": include_usb_mic,
            "include_usb_dtln": include_usb_dtln,
            "include_xvf_raw0_dtln": include_xvf_raw0_dtln,
            "include_aec3_sweep": include_aec3_sweep,
            "aec3_sweep_source": aec3_sweep_source,
        },
        "warnings": warnings,
    }
    return WakeCorpusCapturePlan.from_mapping(plan, assign_plan_id=True).to_json()


def build_capture_plan(
    ports: dict[str, int],
    *,
    corpus_profile: str = PROFILE_STANDARD,
    include_dtln: bool = True,
    include_raw_mic_0: bool = False,
    include_usb_mic: bool = False,
    include_usb_dtln: bool = False,
    include_xvf_raw0_dtln: bool = False,
    include_aec3_sweep: bool = False,
    aec3_sweep_source: str | None = None,
    include_bridge_readiness: bool = True,
    include_runtime_profile: bool = False,
    active_audio_profile: Mapping[str, Any] | None = None,
    runtime_audio_env: Mapping[str, Any] | None = None,
    runtime_snapshot: Mapping[str, Any] | None = None,
    plan_state: str = CAPTURE_PLAN_STATE_PREVIEW,
) -> dict[str, Any]:
    """Return the layered mic/channel/transform plan for a session request.

    This is the authoritative interpretation layer for the web UI and
    metadata: physical microphones expose native streams, JTS optionally
    derives software-AEC/DTLN legs, and the plan records resource cost
    plus bridge-output readiness without starting any capture.
    """
    from . import bridge_session

    if corpus_profile not in CORPUS_PROFILES:
        raise ValueError(f"unknown corpus_profile: {corpus_profile!r}")
    if corpus_profile == PROFILE_CHIP_AEC_COMPARISON:
        include_raw_mic_0 = True
        include_dtln = False
        include_aec3_sweep = False
        sweep_source = bridge_session.AEC3_SWEEP_SOURCE_XVF
    else:
        sweep_source = (
            bridge_session._session_aec3_sweep_source(aec3_sweep_source)
            if include_aec3_sweep else bridge_session.AEC3_SWEEP_SOURCE_XVF
        )
    effective_include_usb_mic = include_usb_mic or (
        include_aec3_sweep and sweep_source == bridge_session.AEC3_SWEEP_SOURCE_USB
    )
    if runtime_snapshot is None:
        try:
            runtime_snapshot = _capture_plan_runtime_snapshot()
        except _CAPTURE_PLAN_PROBE_ERRORS as e:
            bridge_session.log_event(
                logger,
                "wake_corpus.capture_plan_identity_snapshot_failed",
                error=e,
                level=logging.DEBUG,
            )
            runtime_snapshot = {}
    system_env = (
        runtime_snapshot.get("system_env", {})
        if isinstance(runtime_snapshot, Mapping) else {}
    )
    merged_env = (
        runtime_snapshot.get("merged_env", {})
        if isinstance(runtime_snapshot, Mapping) else {}
    )
    if not isinstance(system_env, Mapping):
        system_env = {}
    if not isinstance(merged_env, Mapping):
        merged_env = {}
    enabled_legs = bridge_session._session_legs(
        ports,
        corpus_profile=corpus_profile,
        include_dtln=include_dtln,
        include_raw_mic_0=include_raw_mic_0,
        include_usb_mic=effective_include_usb_mic,
        include_usb_dtln=include_usb_dtln,
        include_xvf_raw0_dtln=include_xvf_raw0_dtln,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
    )
    required_outputs = bridge_session._required_bridge_outputs_for_request(
        corpus_profile=corpus_profile,
        include_dtln=include_dtln,
        include_usb_mic=effective_include_usb_mic,
        include_usb_dtln=include_usb_dtln,
        include_xvf_raw0_dtln=include_xvf_raw0_dtln,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
    )
    bridge_outputs = (
        runtime_snapshot.get("bridge_outputs", {})
        if isinstance(runtime_snapshot, Mapping) else {}
    )
    if not isinstance(bridge_outputs, Mapping):
        bridge_outputs = {}
    missing = (
        bridge_session._missing_bridge_outputs_from_required(
            required_outputs,
            bridge_outputs or bridge_session.bridge_output_status(),
            aec3_sweep_source=sweep_source,
        )
        if include_bridge_readiness else []
    )
    if include_runtime_profile and (
        active_audio_profile is None or runtime_audio_env is None
    ):
        active_audio_profile = (
            runtime_snapshot.get("active_audio_profile")
            if isinstance(runtime_snapshot, Mapping) else None
        )
        runtime_audio_env = (
            runtime_snapshot.get("runtime_audio_env")
            if isinstance(runtime_snapshot, Mapping) else None
        )
        if active_audio_profile is None or runtime_audio_env is None:
            active_audio_profile, runtime_audio_env = _capture_plan_runtime_context()
    required_env = _bridge_env_overrides_for_request(
        system_env=system_env,
        merged_env=merged_env,
        corpus_profile=corpus_profile,
        include_dtln=bridge_session.DTLN_LEG in enabled_legs,
        include_usb_mic=effective_include_usb_mic,
        include_usb_dtln=bridge_session.USB_DTLN_LEG in enabled_legs,
        include_xvf_raw0_dtln=bridge_session.XVF_RAW0_DTLN_LEG in enabled_legs,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
    )
    runtime_snapshot = _capture_plan_snapshot_for_desired_env(
        runtime_snapshot,
        required_env=required_env,
        required_outputs=required_outputs,
    )
    return _capture_plan_from_legs(
        corpus_profile=corpus_profile,
        enabled_legs=enabled_legs,
        ports=ports,
        include_raw_mic_0=bridge_session.RAW0_LEG in enabled_legs,
        include_dtln=bridge_session.DTLN_LEG in enabled_legs,
        include_usb_mic=effective_include_usb_mic,
        include_usb_dtln=bridge_session.USB_DTLN_LEG in enabled_legs,
        include_xvf_raw0_dtln=bridge_session.XVF_RAW0_DTLN_LEG in enabled_legs,
        include_aec3_sweep=include_aec3_sweep,
        aec3_sweep_source=sweep_source,
        missing_bridge_outputs=missing,
        required_bridge_outputs=required_outputs,
        required_bridge_env=required_env,
        runtime_snapshot=runtime_snapshot,
        plan_state=plan_state,
        active_audio_profile=active_audio_profile,
        runtime_audio_env=runtime_audio_env,
    )


def validate_active_capture_plan(
    plan: WakeCorpusCapturePlan | Mapping[str, Any],
    bridge_stats: Mapping[str, Any] | None = None,
    runtime_snapshot: Mapping[str, Any] | None = None,
) -> PlanConformance:
    """Validate that the running bridge conforms to a stored capture plan."""
    from . import bridge_session

    capture_plan = (
        plan if isinstance(plan, WakeCorpusCapturePlan)
        else WakeCorpusCapturePlan.from_mapping(plan)
    )
    if not capture_plan.plan_id:
        return PlanConformance(
            ok=False,
            status="legacy_plan",
            errors=[
                "session metadata predates the capture-plan contract; "
                "start a fresh wake-corpus session before appending clips",
            ],
        )
    if bridge_stats is None:
        bridge_stats = bridge_session.read_bridge_stats_snapshot()
    if not isinstance(bridge_stats, Mapping):
        return PlanConformance(
            ok=False,
            status="bridge_stats_unavailable",
            expected_plan_id=capture_plan.plan_id,
            errors=["aec bridge stats are unavailable"],
        )
    active = bridge_stats.get("active_capture_plan")
    if not isinstance(active, Mapping):
        active = {}
    active_plan_id = str(
        active.get("wake_corpus_plan_id")
        or bridge_stats.get("wake_corpus_plan_id")
        or "",
    )
    raw_emitted = active.get("emitted_legs") or bridge_stats.get("emitted_legs") or []
    emitted_legs = (
        [str(leg) for leg in raw_emitted]
        if isinstance(raw_emitted, list) else []
    )
    expected_legs = list(capture_plan.expected_emitted_legs)
    missing_legs = [leg for leg in expected_legs if leg not in emitted_legs]
    errors: list[str] = []
    warnings: list[str] = []
    if active_plan_id != capture_plan.plan_id:
        errors.append(
            "aec bridge is running a different wake-corpus plan "
            f"(active={active_plan_id or 'none'}, expected={capture_plan.plan_id})",
        )
    if missing_legs:
        errors.append(
            "aec bridge is not emitting promised leg(s): "
            + ", ".join(missing_legs),
        )

    if runtime_snapshot is None:
        try:
            runtime_snapshot = _capture_plan_runtime_snapshot()
        except _CAPTURE_PLAN_PROBE_ERRORS as e:
            runtime_snapshot = {}
            errors.append(f"could not fingerprint current mic/DAC runtime: {e}")
    fingerprints = (
        runtime_snapshot.get("fingerprints", {})
        if isinstance(runtime_snapshot, Mapping) else {}
    )
    if not isinstance(fingerprints, Mapping):
        fingerprints = {}
    mismatches: list[str] = []
    current_mic = str(fingerprints.get("mic") or "")
    if capture_plan.mic_fingerprint and current_mic:
        if current_mic != capture_plan.mic_fingerprint:
            mismatches.append("mic")
    current_dac = str(fingerprints.get("dac_reference") or "")
    if capture_plan.dac_reference_fingerprint and current_dac:
        if current_dac != capture_plan.dac_reference_fingerprint:
            mismatches.append("dac_reference")
    if mismatches:
        errors.append(
            "mic/DAC runtime changed after the session plan was built: "
            + ", ".join(mismatches),
        )
    if not capture_plan.mic_fingerprint or not capture_plan.dac_reference_fingerprint:
        warnings.append("stored plan has incomplete runtime fingerprints")

    ok = not errors
    return PlanConformance(
        ok=ok,
        status="ok" if ok else "blocked",
        active_plan_id=active_plan_id,
        expected_plan_id=capture_plan.plan_id,
        emitted_legs=emitted_legs,
        missing_emitted_legs=missing_legs,
        fingerprint_mismatches=mismatches,
        errors=errors,
        warnings=warnings,
    )
