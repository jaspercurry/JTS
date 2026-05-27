"""Shared AEC3 corpus-sweep definitions.

The wake-corpus recorder can ask jasper-aec-bridge to run a bounded
set of extra WebRTC AEC3 engines in parallel with the production
baseline. Keep the sweep small: each variant has adaptive state, CPU
cost, a UDP stream, a WAV per utterance, and a listening burden.
"""
from __future__ import annotations

from dataclasses import dataclass


AEC3_SWEEP_ENV_FLAG = "JASPER_AEC_CORPUS_AEC3_SWEEP_ENABLED"
MAX_AEC3_SWEEP_VARIANTS = 3


@dataclass(frozen=True)
class Aec3SweepVariant:
    leg: str
    label: str
    port_env: str
    default_port: int
    env_overrides: dict[str, str]


AEC3_SWEEP_VARIANTS: tuple[Aec3SweepVariant, ...] = (
    Aec3SweepVariant(
        leg="aec3_ns_off",
        label="AEC3 NS off",
        port_env="JASPER_AEC_UDP_PORT_AEC3_NS_OFF",
        default_port=9884,
        env_overrides={"JASPER_AEC_NS_ENABLED": "0"},
    ),
    Aec3SweepVariant(
        leg="aec3_default_gain_08",
        label="AEC3 default gain 0.8",
        port_env="JASPER_AEC_UDP_PORT_AEC3_DEFAULT_GAIN_08",
        default_port=9885,
        env_overrides={"JASPER_AEC_DEFAULT_GAIN": "0.8"},
    ),
    Aec3SweepVariant(
        leg="aec3_hf_relaxed",
        label="AEC3 HF relaxed",
        port_env="JASPER_AEC_UDP_PORT_AEC3_HF_RELAXED",
        default_port=9886,
        env_overrides={"JASPER_AEC_CONSERVATIVE_HF": "0"},
    ),
)


def variant_metadata() -> list[dict[str, object]]:
    """JSON-friendly description stored in corpus session metadata."""
    return [
        {
            "leg": variant.leg,
            "label": variant.label,
            "env_overrides": dict(variant.env_overrides),
        }
        for variant in AEC3_SWEEP_VARIANTS
    ]
