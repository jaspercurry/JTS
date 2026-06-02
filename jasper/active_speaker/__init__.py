"""Active-speaker crossover commissioning substrate.

This package is intentionally pure Python and import-cheap. It models the
speaker-baseline layer only; it does not generate or load CamillaDSP configs.
"""

from .profile import (
    ACTIVE_BASELINE_KIND,
    ACTIVE_PRESET_KIND,
    SCHEMA_VERSION,
    ActiveChannelMap,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    BaselineVerification,
    CrossoverRegion,
    DriverSpec,
    OutputChannel,
    SafetyEnvelope,
    SpeakerBaselineProfile,
    required_driver_roles,
)
from .camilla_yaml import (
    ACTIVE_STARTUP_CONFIG_NAME,
    STARTUP_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB,
    emit_active_speaker_startup_config,
)

__all__ = [
    "ACTIVE_STARTUP_CONFIG_NAME",
    "ACTIVE_BASELINE_KIND",
    "ACTIVE_PRESET_KIND",
    "SCHEMA_VERSION",
    "STARTUP_HEADROOM_DB",
    "STARTUP_LIMITER_CLIP_LIMIT_DB",
    "ActiveChannelMap",
    "ActiveSpeakerConfigError",
    "ActiveSpeakerPreset",
    "BaselineVerification",
    "CrossoverRegion",
    "DriverSpec",
    "OutputChannel",
    "SafetyEnvelope",
    "SpeakerBaselineProfile",
    "emit_active_speaker_startup_config",
    "required_driver_roles",
]
