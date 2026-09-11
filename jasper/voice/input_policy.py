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

from .catalog import provider_by_id

from jasper.mics.xvf3800 import ALSA_CARD_NAMES
from jasper.wake_ports import DEFAULT_AEC_ON_PORT, DEFAULT_AEC_UDP_HOST, parse_udp_device


# The endpointer JTS ships. Provider server VAD was measured and ruled out
# permanently — see ADR-0152 and ADR-0244.
ENDPOINTING = "manual_silero"

# The host's noise-reduction intent. Provider adapters own the wire values
# these map to.
NOISE_REDUCTION_NEAR = "near"
NOISE_REDUCTION_FAR = "far"
NOISE_REDUCTION_AUTO = "auto"
NOISE_REDUCTION_OFF_VALUES = frozenset(
    ("", "off", "none", "disabled", "false", "0")
)
# Spellings JASPER_OPENAI_NOISE_REDUCTION accepts, by the intent each asks for.
NOISE_REDUCTION_INTENTS = {
    "near_field": NOISE_REDUCTION_NEAR,
    "far_field": NOISE_REDUCTION_FAR,
}
NOISE_REDUCTION_VALUES = (
    NOISE_REDUCTION_OFF_VALUES
    | set(NOISE_REDUCTION_INTENTS)
    | {NOISE_REDUCTION_AUTO}
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
    noise_reduction: str | None
    noise_reduction_source: str
    warnings: tuple[str, ...] = ()

    @property
    def noise_reduction_label(self) -> str:
        return self.noise_reduction or "off"


def normalize_openai_noise_reduction(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    return value or NOISE_REDUCTION_AUTO


def validate_openai_noise_reduction(value: str) -> None:
    if value not in NOISE_REDUCTION_VALUES:
        allowed = sorted(v for v in NOISE_REDUCTION_VALUES if v)
        raise RuntimeError(
            "JASPER_OPENAI_NOISE_REDUCTION must be one of: " + ", ".join(allowed)
        )


def contract_from_config(cfg: Any) -> SpeechInputContract:
    """Classify the active mic stream from runtime config facts.

    This intentionally consumes the reconciler's applied env, not raw
    hardware identity. If a custom operator points JASPER_MIC_DEVICE at
    their own source, we describe the selected stream conservatively.
    """

    mic_device = str(getattr(cfg, "mic_device", "") or "")
    udp = parse_udp_device(mic_device.strip())
    chip_enabled = bool(getattr(cfg, "aec_chip_aec_enabled", False))
    aec_port = getattr(cfg, "aec_udp_port", DEFAULT_AEC_ON_PORT)
    aec_host = getattr(cfg, "aec_udp_host", DEFAULT_AEC_UDP_HOST).strip().lower()
    if udp is not None and udp[1] == aec_port and (
        udp[0].lower() in ("", "0.0.0.0", aec_host)
    ):
        return SpeechInputContract(
            profile="xvf_chip_aec" if chip_enabled else "xvf_software_aec3",
            source=mic_device,
            raw=False,
            echo_cancelled=True,
            denoised=True,
            beamformed=chip_enabled,
            gain_controlled=True,
            provenance="aec_reconciler",
        )

    if udp is not None:
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
        provenance=(
            "operator"
            if mic_device and mic_device not in ALSA_CARD_NAMES
            else "default"
        ),
    )


def _resolve_openai_noise_reduction(
    requested: str,
    contract: SpeechInputContract,
) -> tuple[str | None, str, tuple[str, ...]]:
    requested = normalize_openai_noise_reduction(requested)
    validate_openai_noise_reduction(requested)

    if requested in NOISE_REDUCTION_OFF_VALUES:
        return None, "explicit_off", ()

    intent = NOISE_REDUCTION_INTENTS.get(requested)
    if intent is not None:
        warnings: tuple[str, ...] = ()
        if intent == NOISE_REDUCTION_FAR and contract.already_processed:
            warnings = (
                "Far-field noise reduction was requested on an already "
                f"processed input profile ({contract.profile}).",
            )
        return intent, "explicit", warnings

    if contract.already_processed:
        return None, "auto_processed_input", ()

    if contract.profile == "custom_udp":
        return None, "auto_unknown_udp", (
            "Custom UDP input profile has no declared preprocessing "
            "contract; auto mode leaves provider denoising off.",
        )

    return NOISE_REDUCTION_FAR, "auto_raw_far", ()


def build_effective_speech_input_policy(cfg: Any) -> EffectiveSpeechInputPolicy:
    contract = contract_from_config(cfg)
    provider = str(getattr(cfg, "voice_provider", "") or "")
    requested_nr = str(getattr(cfg, "openai_noise_reduction", "") or "")
    openai_nr, openai_nr_source, warnings = _resolve_openai_noise_reduction(
        requested_nr,
        contract,
    )
    entry = provider_by_id(provider)
    return EffectiveSpeechInputPolicy(
        provider=provider,
        input_contract=contract,
        endpointing="continuous_audio" if entry and entry.continuous_input else ENDPOINTING,
        noise_reduction=openai_nr,
        noise_reduction_source=openai_nr_source,
        warnings=warnings,
    )
