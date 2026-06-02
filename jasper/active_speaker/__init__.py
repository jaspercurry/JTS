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
from .environment import (
    ENVIRONMENT_REPORT_KIND,
    classify_camilla_config_text,
    parse_aplay_playback_devices,
    parse_camilla_statefile_config_path,
    probe_active_speaker_environment,
)
from .safe_playback import (
    SAFE_PLAYBACK_SESSION_KIND,
    arm_safe_playback_session,
    load_safe_playback_state,
    stop_safe_playback_session,
)
from .tone_plan import (
    TONE_PLAN_KIND,
    build_safe_tone_plan,
    load_active_speaker_preset,
    tone_targets_payload,
)

__all__ = [
    "ACTIVE_STARTUP_CONFIG_NAME",
    "ACTIVE_BASELINE_KIND",
    "ACTIVE_PRESET_KIND",
    "ENVIRONMENT_REPORT_KIND",
    "HARDWARE_PROBE_EVIDENCE_SOURCE",
    "OPERATOR_EVIDENCE_SOURCE",
    "PATH_SAFETY_EVIDENCE_KIND",
    "REQUIRED_PATHS",
    "SAFE_PLAYBACK_SESSION_KIND",
    "SCHEMA_VERSION",
    "STARTUP_HEADROOM_DB",
    "STARTUP_LIMITER_CLIP_LIMIT_DB",
    "TONE_PLAN_KIND",
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
    "classify_camilla_config_text",
    "emit_active_speaker_startup_config",
    "evaluate_path_safety_evidence",
    "build_safe_tone_plan",
    "load_active_speaker_preset",
    "parse_aplay_playback_devices",
    "parse_camilla_statefile_config_path",
    "probe_active_speaker_environment",
    "required_driver_roles",
    "requirements_payload",
    "arm_safe_playback_session",
    "load_safe_playback_state",
    "stop_safe_playback_session",
    "tone_targets_payload",
]
