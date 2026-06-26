# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""User-facing microphone settings view model.

The storage/runtime vocabulary is intentionally precise:
``xvf_chip_aec``, ``xvf_software_aec3``, wake-leg booleans, and the DAC
gate all mean specific things to the reconciler. They are not, however,
good primary UI language.

This module is the translation boundary. It consumes the read-only
``/aec`` status payload and produces task-oriented state for the
``/wake/`` page: microphone, echo cancellation, wake word, and advanced
fusion. It is side-effect-free so future mic families can add profile
capabilities without pushing policy into browser JavaScript.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .audio_profile_state import (
    PROFILE_AUTO,
    PROFILE_DIRECT_MIC,
    PROFILE_XVF_CHIP_AEC,
    PROFILE_XVF_CHIP_AEC_TESTING,
    PROFILE_XVF_SOFTWARE_AEC3,
)


@dataclass(frozen=True)
class ProfileChoiceSpec:
    """Stable copy/metadata for one selectable microphone input profile."""

    profile: str
    choice_id: str
    section: str
    label: str
    description: str
    badge: str
    confirm_title: str = ""
    confirm_body: str = ""
    confirm_danger: bool = False
    danger: bool = False

    @property
    def confirm(self) -> dict[str, Any] | None:
        if not self.confirm_title:
            return None
        return {
            "title": self.confirm_title,
            "body": self.confirm_body,
            "danger": self.confirm_danger,
        }


_PROFILE_CHOICE_SPECS = (
    ProfileChoiceSpec(
        profile=PROFILE_AUTO,
        choice_id="best_available",
        section="echo",
        label="Best available",
        description=(
            "Automatically use the strongest supported path for this microphone "
            "and output DAC."
        ),
        badge="Recommended",
    ),
    ProfileChoiceSpec(
        profile=PROFILE_XVF_CHIP_AEC,
        choice_id="hardware_aec",
        section="echo",
        label="Hardware echo cancellation",
        description="Use the microphone array chip when this mic/DAC path is validated.",
        badge="Chip AEC",
        confirm_title="Use hardware echo cancellation?",
        confirm_body=(
            "This uses the active mic profile's validated hardware AEC beam plan "
            "and disables the software raw/DTLN wake legs."
        ),
    ),
    ProfileChoiceSpec(
        profile=PROFILE_XVF_SOFTWARE_AEC3,
        choice_id="software_aec3",
        section="echo",
        label="Software echo cancellation",
        description=(
            "Use WebRTC AEC3 on the host when hardware AEC is unavailable or disabled."
        ),
        badge="AEC3",
    ),
    ProfileChoiceSpec(
        profile=PROFILE_DIRECT_MIC,
        choice_id="direct_mic",
        section="echo",
        label="No echo cancellation",
        description=(
            "Use the microphone directly. Wake may be unreliable while audio is playing."
        ),
        badge="Direct",
        confirm_title="Use the microphone with no echo cancellation?",
        confirm_body=(
            "This disables the AEC bridge. Wake while music is playing may be "
            "unreliable until you choose an AEC profile again."
        ),
        confirm_danger=True,
        danger=True,
    ),
    ProfileChoiceSpec(
        profile=PROFILE_XVF_CHIP_AEC_TESTING,
        choice_id="hardware_aec_testing",
        section="advanced",
        label="Hardware AEC validation mode",
        description="Run chip-AEC on a DAC that is not approved for automatic use.",
        badge="Testing",
        confirm_title="Use hardware AEC validation mode?",
        confirm_body=(
            "This routes the live mic path through hardware AEC on an unapproved "
            "DAC so you can validate it. Use software AEC3 again if wake "
            "reliability drops."
        ),
        confirm_danger=True,
    ),
)


def profile_choice_specs(*, section: str | None = None) -> tuple[ProfileChoiceSpec, ...]:
    """Return backend-owned profile choice metadata for render/validation."""

    if section is None:
        return _PROFILE_CHOICE_SPECS
    return tuple(spec for spec in _PROFILE_CHOICE_SPECS if spec.section == section)


def valid_profile_ids() -> frozenset[str]:
    """Profiles the wake page may submit to jasper-control."""

    return frozenset(spec.profile for spec in _PROFILE_CHOICE_SPECS)


def build_microphone_settings_view(status: Mapping[str, Any]) -> dict[str, Any]:
    """Build the backend-owned view contract for the microphone UI."""

    mic = _mapping(status.get("microphone"))
    profile = _mapping(status.get("audio_profile"))
    gate = _mapping(status.get("chip_aec_gate"))
    legs = _mapping(status.get("legs"))
    software = _mapping(status.get("software_aec3"))
    wake_word = _mapping(status.get("wake_word"))

    mic_view = _mic_view(mic, gate)
    echo_view = _echo_view(
        mic_view=mic_view,
        profile=profile,
        gate=gate,
        software=software,
        bridge_active=bool(status.get("bridge_active")),
    )
    return {
        "schema_version": 1,
        "mic": mic_view,
        "echo": echo_view,
        "fusion": _fusion_view(
            profile=profile,
            mic=mic,
            legs=legs,
            software=software,
            echo_mode=str(echo_view.get("mode") or ""),
        ),
        "wake": _wake_view(wake_word, status.get("threshold")),
        "advanced": _advanced_view(profile=profile, gate=gate, mic_view=mic_view),
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mic_view(mic: Mapping[str, Any], gate: Mapping[str, Any]) -> dict[str, Any]:
    detected = bool(mic.get("detected"))
    variant_id = str(mic.get("variant_id") or "")
    geometry = str(mic.get("geometry") or "")
    firmware = _mapping(mic.get("firmware"))
    firmware_label = str(firmware.get("label") or "unknown")
    chip_capable = bool(gate.get("mic_available"))
    warnings = [
        str(item)
        for item in mic.get("warnings", [])
        if str(item).strip()
    ] if isinstance(mic.get("warnings"), list) else []

    if not detected:
        kind = "none"
        title = "No microphone detected"
        subtitle = "Connect a supported microphone before changing echo or wake input settings."
    elif variant_id.startswith("xvf3800"):
        kind = "xvf3800"
        title = str(mic.get("name") or "XVF3800 microphone")
        details = []
        if geometry:
            details.append(f"{geometry} geometry")
        if firmware_label:
            details.append(firmware_label)
        subtitle = " · ".join(details) if details else "XVF3800 detected"
    else:
        kind = "simple"
        title = str(mic.get("name") or "Microphone detected")
        subtitle = "Hardware echo cancellation is not available for this microphone."

    capabilities: list[str] = []
    if chip_capable:
        capabilities.append("hardware_aec")
    if detected:
        capabilities.append("software_aec")
    return {
        "detected": detected,
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "variant_id": variant_id,
        "geometry": geometry,
        "firmware": dict(firmware),
        "chip_aec_capable": chip_capable,
        "chip_beam_plan": str(mic.get("chip_beam_plan") or ""),
        "capabilities": capabilities,
        "warnings": warnings,
    }


def _echo_view(
    *,
    mic_view: Mapping[str, Any],
    profile: Mapping[str, Any],
    gate: Mapping[str, Any],
    software: Mapping[str, Any],
    bridge_active: bool,
) -> dict[str, Any]:
    detected = bool(mic_view.get("detected"))
    selection = str(profile.get("selection") or "")
    requested = str(profile.get("requested") or "")
    active = str(profile.get("active") or "")
    state = str(profile.get("state") or "unknown")
    reason = str(profile.get("reason") or "")
    chip_selected = requested in {PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING}
    chip_active = active in {PROFILE_XVF_CHIP_AEC, PROFILE_XVF_CHIP_AEC_TESTING}
    software_active = active == PROFILE_XVF_SOFTWARE_AEC3
    direct_active = active == PROFILE_DIRECT_MIC or state == "disabled"
    chip_production_available = bool(gate.get("production_available"))
    chip_testing_available = bool(gate.get("testing_available"))

    if not detected:
        mode = "no_mic"
        title = "No microphone detected"
        detail = "Echo cancellation will be available after a supported microphone is connected."
    elif chip_active:
        mode = "hardware_chip_aec"
        title = "Using microphone hardware echo cancellation"
        detail = "The XVF3800 chip is producing the voice stream; software AEC3 is bypassed."
    elif software_active:
        mode = "software_aec3"
        title = "Using software echo cancellation"
        detail = reason or "WebRTC AEC3 is processing the microphone stream."
    elif chip_selected:
        mode = "hardware_chip_aec_pending"
        title = "Hardware echo cancellation is selected"
        detail = reason or "Waiting for the reconciler to apply the hardware AEC path."
    elif direct_active:
        mode = "direct_mic"
        title = "Using direct microphone input"
        detail = "No echo cancellation bridge is active."
    else:
        mode = "pending"
        title = "Microphone input is changing"
        detail = reason or "Waiting for runtime state to settle."

    choices = [
        _profile_choice(
            _profile_spec(PROFILE_AUTO),
            selected=selection == PROFILE_AUTO,
            enabled=detected,
            visible=True,
            status="recommended",
        ),
        _profile_choice(
            _profile_spec(PROFILE_XVF_CHIP_AEC),
            selected=selection == PROFILE_XVF_CHIP_AEC,
            enabled=detected and chip_production_available,
            visible=bool(mic_view.get("kind") == "xvf3800" or chip_selected),
            status=(
                "active" if chip_active
                else "available" if chip_production_available
                else _gate_short_status(gate)
            ),
        ),
        _profile_choice(
            _profile_spec(PROFILE_XVF_SOFTWARE_AEC3),
            selected=selection == PROFILE_XVF_SOFTWARE_AEC3,
            enabled=detected,
            visible=detected,
            status=(
                "active" if software_active
                else "bypassed" if bool(software.get("bypassed"))
                else "available"
            ),
        ),
        _profile_choice(
            _profile_spec(PROFILE_DIRECT_MIC),
            selected=selection == PROFILE_DIRECT_MIC,
            enabled=detected,
            visible=detected,
            status="active" if direct_active else "available",
        ),
    ]

    return {
        "mode": mode,
        "title": title,
        "detail": detail,
        "state": state,
        "bridge_active": bridge_active,
        "hardware": {
            "available": bool(chip_production_available),
            "testing_available": bool(chip_testing_available),
            "active": bool(chip_active),
            "selected": bool(chip_selected),
            "gate_status": str(gate.get("status") or ""),
            "gate_detail": str(gate.get("detail") or ""),
        },
        "software_aec3": {
            "available": detected,
            "configured": bool(software.get("configured")),
            "active": bool(software.get("active")),
            "bypassed": bool(software.get("bypassed")),
            "reason": str(software.get("reason") or ""),
        },
        "choices": choices,
    }


def _profile_choice(
    spec: ProfileChoiceSpec,
    *,
    selected: bool,
    enabled: bool,
    visible: bool,
    status: str,
) -> dict[str, Any]:
    choice = {
        "id": spec.choice_id,
        "profile": spec.profile,
        "label": spec.label,
        "description": spec.description,
        "badge": spec.badge,
        "selected": selected,
        "enabled": enabled,
        "visible": visible or selected,
        "status": status,
        "danger": spec.danger,
    }
    confirm = spec.confirm
    if confirm is not None:
        choice["confirm"] = confirm
    return choice


def _profile_spec(profile: str) -> ProfileChoiceSpec:
    for spec in _PROFILE_CHOICE_SPECS:
        if spec.profile == profile:
            return spec
    raise KeyError(profile)


def _gate_short_status(gate: Mapping[str, Any]) -> str:
    status = str(gate.get("status") or "")
    if status == "needs_calibration":
        return "needs calibration"
    return status or "unavailable"


def _fusion_view(
    *,
    profile: Mapping[str, Any],
    mic: Mapping[str, Any],
    legs: Mapping[str, Any],
    software: Mapping[str, Any],
    echo_mode: str,
) -> dict[str, Any]:
    selection = str(profile.get("selection") or "")
    wake_legs = [
        str(item)
        for item in mic.get("wake_legs", [])
        if str(item).strip()
    ] if isinstance(mic.get("wake_legs"), list) else []
    custom = selection == "custom"
    bridge_unavailable = echo_mode in {"direct_mic", "no_mic"}
    if echo_mode == "hardware_chip_aec":
        summary = "Default hardware beam fusion"
    elif echo_mode == "software_aec3":
        summary = "Software AEC3 with optional wake streams"
    elif echo_mode == "direct_mic":
        summary = "Direct microphone only"
    elif custom:
        summary = "Custom wake stream configuration"
    else:
        summary = "Wake stream configuration pending"

    chip_on = bool(_mapping(legs.get("chip_aec")).get("configured"))
    aec3_bypassed = bool(software.get("bypassed"))
    toggles = [
        _fusion_toggle(
            "raw",
            "Direct/raw wake stream",
            "Parallel raw mic wake scoring for software-AEC experiments.",
            _mapping(legs.get("raw")),
            enabled=not chip_on and not bridge_unavailable,
            disabled_reason=(
                "Advanced wake streams require the AEC bridge."
                if bridge_unavailable
                else "Paused while hardware AEC beams are active." if chip_on else ""
            ),
        ),
        _fusion_toggle(
            "dtln",
            "DTLN neural stream",
            "Optional neural cleanup stream. Higher CPU and memory cost.",
            _mapping(legs.get("dtln")),
            enabled=not chip_on and not bridge_unavailable,
            disabled_reason=(
                "Advanced wake streams require the AEC bridge."
                if bridge_unavailable
                else "Paused while hardware AEC beams are active." if chip_on else ""
            ),
        ),
        _fusion_toggle(
            "chip_aec",
            "Hardware beam scoring",
            "XVF3800 chip-AEC beam streams used for wake scoring.",
            _mapping(legs.get("chip_aec")),
            enabled=(
                not bridge_unavailable
                and bool(_mapping(legs.get("chip_aec")).get("production_available"))
            ),
            disabled_reason=(
                "Advanced wake streams require the AEC bridge."
                if bridge_unavailable
                else
                "Use the validation profile first."
                if not bool(_mapping(legs.get("chip_aec")).get("production_available"))
                else ""
            ),
        ),
    ]
    if aec3_bypassed:
        toggles[0]["disabled_reason"] = "Software streams are bypassed by hardware AEC."
        toggles[1]["disabled_reason"] = "Software streams are bypassed by hardware AEC."
    return {
        "summary": summary,
        "custom": custom,
        "wake_legs": wake_legs,
        "toggles": toggles,
    }

def _fusion_toggle(
    toggle_id: str,
    label: str,
    description: str,
    leg: Mapping[str, Any],
    *,
    enabled: bool,
    disabled_reason: str,
) -> dict[str, Any]:
    checked = bool(leg.get("configured"))
    return {
        "id": toggle_id,
        "label": label,
        "description": description,
        "checked": checked,
        "enabled": enabled,
        "status": "on" if checked else "off",
        "disabled_reason": disabled_reason,
    }


def _wake_view(wake_word: Mapping[str, Any], threshold: Any) -> dict[str, Any]:
    try:
        threshold_value = float(threshold)
    except (TypeError, ValueError):
        threshold_value = None
    return {
        "label": str(wake_word.get("label") or ""),
        "pronunciation": str(wake_word.get("pronunciation") or ""),
        "model": str(wake_word.get("model") or ""),
        "threshold": threshold_value,
    }


def _advanced_view(
    *,
    profile: Mapping[str, Any],
    gate: Mapping[str, Any],
    mic_view: Mapping[str, Any],
) -> dict[str, Any]:
    selection = str(profile.get("selection") or "")
    selected = selection == PROFILE_XVF_CHIP_AEC_TESTING
    testing_visible = bool(
        mic_view.get("kind") == "xvf3800"
        and gate.get("testing_available")
    )
    spec = _profile_spec(PROFILE_XVF_CHIP_AEC_TESTING)
    status = "active" if selected else "available"
    if selected and not testing_visible:
        status = _gate_short_status(gate)
    return {
        "validation_profile": _profile_choice(
            spec,
            selected=selected,
            enabled=testing_visible,
            visible=testing_visible,
            status=status,
        )
    }
