"""Active-speaker crossover commissioning substrate.

This package is intentionally pure Python and import-cheap. It models the
speaker-baseline layer and can emit no-apply startup templates; it does not
load CamillaDSP configs or touch hardware.
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
from .path_safety import (
    HARDWARE_PROBE_EVIDENCE_SOURCE,
    OPERATOR_EVIDENCE_SOURCE,
    PATH_SAFETY_EVIDENCE_KIND,
    REQUIRED_PATHS,
    PathSafetyRequirement,
    evaluate_path_safety_evidence,
    requirements_payload,
)

__all__ = [
    "ACTIVE_STARTUP_CONFIG_NAME",
    "ACTIVE_BASELINE_KIND",
    "ACTIVE_PRESET_KIND",
    "HARDWARE_PROBE_EVIDENCE_SOURCE",
    "OPERATOR_EVIDENCE_SOURCE",
    "PATH_SAFETY_EVIDENCE_KIND",
    "REQUIRED_PATHS",
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
    "PathSafetyRequirement",
    "SafetyEnvelope",
    "SpeakerBaselineProfile",
    "emit_active_speaker_startup_config",
    "evaluate_path_safety_evidence",
    "required_driver_roles",
    "requirements_payload",
]
