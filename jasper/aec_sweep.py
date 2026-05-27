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
        leg="aec3_hf_relaxed",
        label="AEC3 HF relaxed",
        port_env="JASPER_AEC_UDP_PORT_AEC3_HF_RELAXED",
        default_port=9884,
        env_overrides={"JASPER_AEC_CONSERVATIVE_HF": "0"},
    ),
    Aec3SweepVariant(
        leg="aec3_nearend_fast",
        label="AEC3 near-end fast",
        port_env="JASPER_AEC_UDP_PORT_AEC3_NEAREND_FAST",
        default_port=9885,
        env_overrides={
            "JASPER_AEC_CONSERVATIVE_HF": "0",
            "JASPER_AEC_DND_SNR_THRESHOLD": "15",
            "JASPER_AEC_DND_ENR_THRESHOLD": "0.50",
            "JASPER_AEC_DND_HOLD_DURATION": "100",
            "JASPER_AEC_DND_TRIGGER_THRESHOLD": "6",
        },
    ),
    Aec3SweepVariant(
        leg="aec3_slow_attack",
        label="AEC3 slow attack",
        port_env="JASPER_AEC_UDP_PORT_AEC3_SLOW_ATTACK",
        default_port=9886,
        env_overrides={
            "JASPER_AEC_CONSERVATIVE_HF": "0",
            "JASPER_AEC_MAX_DEC_LF": "0.02",
            "JASPER_AEC_NEAREND_MAX_DEC_LF": "0.02",
        },
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
