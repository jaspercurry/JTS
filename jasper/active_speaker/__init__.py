# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-speaker crossover commissioning substrate.

Most of this package does not emit audio or grant playback authority — the
exception is `web_commissioning`, whose safety-gated driver/summed test and
capture-sweep flows launch `aplay` to produce real hardware
audio.

Package-level names resolve lazily (PEP 562): importing a submodule such as
`volume_latch` must not drag the measurement stack (yaml, numpy/scipy via
`jasper.audio_measurement`) into a resident daemon.
"""

from importlib import import_module
from typing import Any

_LAZY_ATTRS: dict[str, str] = {
    "ACTIVE_PROGRAM_BAKE_SOURCE": "camilla_yaml",
    "ActiveSpeakerConfigError": "profile",
    "ActiveSpeakerDesignDraftError": "design_draft",
    "ActiveSpeakerPreset": "profile",
    "BaselineVerification": "profile",
    "COMMISSIONING_CONFIG_KIND": "staging",
    "COMMISSION_RAMP_MAX_LEVEL_DBFS": "commission_ramp",
    "CROSSOVER_PREVIEW_KIND": "crossover_preview",
    "CROSSOVER_PREVIEW_PATH_ENV": "crossover_preview",
    "DESIGN_DRAFT_KIND": "design_draft",
    "DESIGN_DRAFT_PATH_ENV": "design_draft",
    "DRIVER_DOMAIN_PROGRAM_CHANNELS": "camilla_yaml",
    "DRIVER_RESEARCH_KIND": "design_draft",
    "DRIVER_TEST_SIGNAL_PLAN_KIND": "test_signal_plan",
    "HARDWARE_PROBE_EVIDENCE_SOURCE": "path_safety",
    "LocalSubwoofer": "profile",
    "OPERATOR_EVIDENCE_SOURCE": "path_safety",
    "PATH_SAFETY_EVIDENCE_KIND": "path_safety",
    "STAGED_STARTUP_CONFIG_KIND": "staging",
    "SpeakerBaselineProfile": "profile",
    "TONE_PLAN_KIND": "tone_plan",
    "abort_ramp": "commission_ramp",
    "audible_outputs_for_role": "camilla_yaml",
    "build_crossover_preview": "crossover_preview",
    "build_design_draft": "design_draft",
    "build_stage5_ramp_gate": "commission_ramp",
    "build_startup_load_path_safety_evidence": "path_safety",
    "channel_select_mixer_name": "camilla_yaml",
    "compile_preset_from_crossover_preview": "staging",
    "crossover_preview_fingerprint": "crossover_preview",
    "driver_commission_audible_evidence": "graph_evidence",
    "driver_test_signal_plan": "test_signal_plan",
    "driver_test_signal_plan_from_edges": "test_signal_plan",
    "emit_active_speaker_baseline_config": "camilla_yaml",
    "emit_active_speaker_commissioning_config": "camilla_yaml",
    "emit_active_speaker_driver_domain_config": "camilla_yaml",
    "emit_active_speaker_program_bake_config": "camilla_yaml",
    "emit_active_speaker_program_config": "camilla_yaml",
    "emit_active_speaker_startup_config": "camilla_yaml",
    "evaluate_path_safety_evidence": "path_safety",
    "load_active_speaker_preset": "tone_plan",
    "load_commission_load_state": "commission_load",
    "load_crossover_preview": "crossover_preview",
    "load_design_draft": "design_draft",
    "load_driver_commissioning_config": "commission_load",
    "load_ramp_state": "commission_ramp",
    "load_staged_startup_config": "staging",
    "load_summed_commissioning_config": "commission_load",
    "lowest_driver_role": "profile",
    "next_ramp_gain_db": "commission_ramp",
    "parse_camilla_statefile_config_path": "environment",
    "prepare_driver_commissioning_config": "staging",
    "ramp_audible_step": "commission_ramp",
    "record_ramp_operator_ack": "commission_ramp",
    "requirements_payload": "path_safety",
    "reset_ramp_state": "commission_ramp",
    "rollback_driver_commissioning_config": "commission_load",
    "running_commission_evidence": "graph_evidence",
    "save_crossover_preview": "crossover_preview",
    "save_design_draft": "design_draft",
    "stage_protected_startup_config": "staging",
    "write_path_safety_evidence": "path_safety",
}

__all__ = sorted(_LAZY_ATTRS)


def __getattr__(name: str) -> Any:
    # Caching into globals() keeps the old eager re-export's binding
    # semantics: the name is resolved once, so patching the defining
    # submodule afterwards does not retarget it. jasper.multiroom's
    # __getattr__ deliberately does the opposite (#1270, #1678) — it
    # resolves per access because its callables are monkeypatched.
    module = _LAZY_ATTRS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY_ATTRS})
