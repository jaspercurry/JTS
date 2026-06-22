# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve the effective speech-input policy for voice providers.

The hardware/reconciler layer owns devices and audio profiles. Provider
adapters own wire-format translation. This module is the small contract
between them: it turns the currently selected input stream into facts a
provider can safely consume.

It is deliberately side-effect-free. Callers pass the already-loaded
Config-like object; no hardware probes, env-file reads, or systemd calls
happen here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


OPENAI_NOISE_REDUCTION_AUTO = "auto"
OPENAI_NOISE_REDUCTION_DISABLED = frozenset((
    "",
    "off",
    "none",
    "disabled",
    "false",
    "0",
))
OPENAI_NOISE_REDUCTION_WIRE_VALUES = frozenset(("near_field", "far_field"))
OPENAI_NOISE_REDUCTION_VALUES = (
    OPENAI_NOISE_REDUCTION_DISABLED
    | OPENAI_NOISE_REDUCTION_WIRE_VALUES
    | {OPENAI_NOISE_REDUCTION_AUTO}
)


@dataclass(frozen=True)
class SpeechInputContract:
    """Facts about the stream the provider receives."""

    profile: str
    source: str
    raw: bool
    echo_cancelled: bool
    denoised: bool
    beamformed: bool
    gain_controlled: bool
    provenance: str

    @property
    def already_processed(self) -> bool:
        return self.echo_cancelled or self.denoised or self.beamformed


@dataclass(frozen=True)
class EffectiveSpeechInputPolicy:
    provider: str
    input_contract: SpeechInputContract
    endpointing: str
    openai_noise_reduction: str | None
    openai_noise_reduction_source: str
    warnings: tuple[str, ...] = ()

    @property
    def openai_noise_reduction_label(self) -> str:
        return self.openai_noise_reduction or "off"


def normalize_openai_noise_reduction(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    return value or OPENAI_NOISE_REDUCTION_AUTO


def validate_openai_noise_reduction(value: str) -> None:
    if value not in OPENAI_NOISE_REDUCTION_VALUES:
        allowed = sorted(
            v for v in OPENAI_NOISE_REDUCTION_VALUES if v
        )
        raise RuntimeError(
            "JASPER_OPENAI_NOISE_REDUCTION must be one of: "
            + ", ".join(allowed)
        )


def contract_from_config(cfg: Any) -> SpeechInputContract:
    """Classify the active mic stream from runtime config facts.

    This intentionally consumes the reconciler's applied env, not raw
    hardware identity. If a custom operator points JASPER_MIC_DEVICE at
    their own source, we describe the selected stream conservatively.
    """

    mic_device = str(getattr(cfg, "mic_device", "") or "")
    chip_enabled = bool(getattr(cfg, "aec_chip_aec_enabled", False))
    chip_150 = str(getattr(cfg, "mic_device_chip_aec_150", "") or "")
    chip_210 = str(getattr(cfg, "mic_device_chip_aec_210", "") or "")

    if chip_enabled and chip_150 and chip_210 and mic_device.startswith("udp:"):
        return SpeechInputContract(
            profile="xvf_chip_aec",
            source=mic_device,
            raw=False,
            echo_cancelled=True,
            denoised=True,
            beamformed=True,
            gain_controlled=True,
            provenance="aec_reconciler",
        )

    if mic_device == "udp:9876":
        return SpeechInputContract(
            profile="xvf_software_aec3",
            source=mic_device,
            raw=False,
            echo_cancelled=True,
            denoised=True,
            beamformed=False,
            gain_controlled=True,
            provenance="aec_reconciler",
        )

    if mic_device.startswith("udp:"):
        return SpeechInputContract(
            profile="custom_udp",
            source=mic_device,
            raw=False,
            echo_cancelled=False,
            denoised=False,
            beamformed=False,
            gain_controlled=False,
            provenance="operator",
        )

    return SpeechInputContract(
        profile="direct_mic",
        source=mic_device or "not_configured",
        raw=True,
        echo_cancelled=False,
        denoised=False,
        beamformed=False,
        gain_controlled=False,
        provenance="operator" if mic_device and mic_device != "Array" else "default",
    )


def _resolve_openai_noise_reduction(
    requested: str,
    contract: SpeechInputContract,
) -> tuple[str | None, str, tuple[str, ...]]:
    requested = normalize_openai_noise_reduction(requested)
    validate_openai_noise_reduction(requested)
    warnings: list[str] = []

    if requested in OPENAI_NOISE_REDUCTION_DISABLED:
        return None, "explicit_off", ()

    if requested in OPENAI_NOISE_REDUCTION_WIRE_VALUES:
        if requested == "far_field" and contract.already_processed:
            warnings.append(
                "OpenAI far_field noise reduction is enabled on an already "
                f"processed input profile ({contract.profile}).",
            )
        return requested, "explicit", tuple(warnings)

    if contract.already_processed:
        return None, "auto_processed_input", ()

    if contract.profile == "custom_udp":
        warnings.append(
            "Custom UDP input profile has no declared preprocessing contract; "
            "OpenAI noise reduction auto mode leaves provider denoising off.",
        )
        return None, "auto_unknown_udp", tuple(warnings)

    return "far_field", "auto_raw_far_field", ()


def build_effective_speech_input_policy(cfg: Any) -> EffectiveSpeechInputPolicy:
    contract = contract_from_config(cfg)
    endpointing = (
        "server_vad_when_music"
        if bool(getattr(cfg, "server_vad_enabled", False))
        else "manual_silero"
    )
    provider = str(getattr(cfg, "voice_provider", "") or "")
    requested_nr = str(getattr(cfg, "openai_noise_reduction", "") or "")
    openai_nr, openai_nr_source, warnings = _resolve_openai_noise_reduction(
        requested_nr,
        contract,
    )
    return EffectiveSpeechInputPolicy(
        provider=provider,
        input_contract=contract,
        endpointing=endpointing,
        openai_noise_reduction=openai_nr,
        openai_noise_reduction_source=openai_nr_source,
        warnings=warnings,
    )
