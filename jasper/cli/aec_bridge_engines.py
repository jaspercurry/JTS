# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""AEC engines — the WebRTC AEC3 bindings and the selector in front of them.

Imports run one way only: nothing here reads `jasper.cli.aec_bridge`. Engine
configuration arrives as an `overrides` mapping; every knob absent from it
falls back to the `JASPER_AEC_*` environment variable named at its call site,
so one process can run the production engine off the environment and corpus
lanes off explicit overrides at the same time.
"""
from __future__ import annotations

import os
from typing import Protocol

from jasper.aec_sweep import (
    AGC1_ENABLED_ENV,
    AGC1_MAX_GAIN_DB_ENV,
    AGC1_TARGET_DBFS_ENV,
    NS_ENABLED_ENV,
    NS_LEVEL_ENV,
)
from jasper.cli.aec_bridge_telemetry import logger

# 320 samples @ 16 kHz = 20 ms, a multiple of WebRTC AEC3's 10 ms frame
# requirement (160 samples); the binding splits 320 → 2×160 internally per
# the AEC3 API contract.
FRAME_SAMPLES = 320
SAMPLE_RATE = 16000

# DTLN engine-selection env keys: not sweep knobs, so aec_sweep doesn't carry
# them, but read here and by wake_corpus/cli callers that report the same
# flags.
DTLN_ENABLED_ENV = "JASPER_AEC_DTLN_ENABLED"
CORPUS_USB_DTLN_ENABLED_ENV = "JASPER_AEC_CORPUS_USB_DTLN_ENABLED"


class Aec3Engine(Protocol):
    """The engine surface every caller relies on."""

    def process(self, mic: bytes, ref: bytes) -> bytes: ...

    def close(self) -> None: ...


class EngineSelector(Protocol):
    """`select_engine`, injected so caller modules stay one-way."""

    def __call__(
        self,
        overrides: dict[str, str] | None = None,
        label: str | None = None,
    ) -> Aec3Engine: ...


def _cfg_value(
    name: str,
    default: str,
    overrides: dict[str, str] | None = None,
) -> str:
    if overrides is not None and name in overrides:
        return overrides[name]
    return os.environ.get(name, default)


def _cfg_bool(
    name: str,
    default: str,
    overrides: dict[str, str] | None = None,
) -> bool:
    return _cfg_value(name, default, overrides).strip().lower() in (
        "1", "true", "yes", "on",
    )


class Aec3V1Engine:
    """WebRTC AEC3 via the jasper_aec3 v1.3-3 (legacy) pybind11 binding.

    Splits each FRAME_SAMPLES (20 ms) buffer into 2× 10 ms windows, calls
    ProcessReverseStream + ProcessStream per window, and returns the joined
    AEC'd capture. Top-level AudioProcessing::Config knobs only — no deep
    EchoCanceller3Config access. The mandatory fallback engine when the v2
    binding is unavailable or unverified.
    """

    def __init__(
        self,
        overrides: dict[str, str] | None = None,
        label: str = "aec3_v1",
    ) -> None:
        from jasper_aec3 import Aec3

        ns_enabled = _cfg_bool(NS_ENABLED_ENV, "1", overrides)
        ns_level = _cfg_value(NS_LEVEL_ENV, "low", overrides).strip().lower()
        agc1_enabled = _cfg_bool(AGC1_ENABLED_ENV, "0", overrides)
        agc1_target_dbfs = int(_cfg_value(
            AGC1_TARGET_DBFS_ENV, "9", overrides,
        ))
        agc1_max_gain_db = int(_cfg_value(
            AGC1_MAX_GAIN_DB_ENV, "18", overrides,
        ))
        enable_agc2 = _cfg_bool("JASPER_AEC_AGC2", "0", overrides)
        stream_delay_ms = int(_cfg_value(
            "JASPER_AEC_STREAM_DELAY_MS", "40", overrides,
        ))
        self._aec = Aec3(
            stream_delay_ms=stream_delay_ms,
            enable_agc2=enable_agc2,
            ns_enabled=ns_enabled,
            ns_level=ns_level,
            agc1_enabled=agc1_enabled,
            agc1_target_dbfs=agc1_target_dbfs,
            agc1_max_gain_db=agc1_max_gain_db,
        )
        logger.info(
            "engine=%s ns=%s/%s agc1=%s(target=%d,max=%ddB) "
            "agc2=%s stream_delay_ms=%d frame=%d rate=%d",
            label,
            "on" if ns_enabled else "off", ns_level,
            "on" if agc1_enabled else "off",
            agc1_target_dbfs, agc1_max_gain_db,
            "on" if enable_agc2 else "off", stream_delay_ms,
            FRAME_SAMPLES, SAMPLE_RATE,
        )

    def process(self, mic: bytes, ref: bytes) -> bytes:
        return self._aec.process(mic, ref)

    def close(self) -> None:
        # The pybind11 wrapper's std::unique_ptr<AudioProcessing> is freed
        # when the Python Aec3 instance is GC'd — no explicit teardown.
        pass


class Aec3V2Engine:
    """WebRTC AEC3 via the jasper_aec3 v2.1 vendored-static binding.

    Exposes the deep EchoCanceller3Config knobs the v1 binding cannot reach.
    Every default below is the BEST_A canonical config, which is also what
    the Aec3V2 constructor's own defaults carry (see jasper_aec3 for
    per-knob rationale); each is individually overridable by the
    `JASPER_AEC_*` env var named at its call site.
    """

    def __init__(
        self,
        overrides: dict[str, str] | None = None,
        label: str = "aec3_v2(BEST_A)",
    ) -> None:
        from jasper_aec3 import Aec3V2

        # Top-level, shared with v1
        ns_enabled = _cfg_bool(NS_ENABLED_ENV, "1", overrides)
        ns_level = _cfg_value(NS_LEVEL_ENV, "low", overrides).strip().lower()
        agc1_enabled = _cfg_bool(AGC1_ENABLED_ENV, "1", overrides)
        agc1_target_dbfs = int(_cfg_value(
            AGC1_TARGET_DBFS_ENV, "9", overrides,
        ))
        agc1_max_gain_db = int(_cfg_value(
            AGC1_MAX_GAIN_DB_ENV, "18", overrides,
        ))
        enable_agc2 = _cfg_bool("JASPER_AEC_AGC2", "0", overrides)

        # Deep EchoCanceller3Config — defaults from BEST_A
        filter_length = int(_cfg_value("JASPER_AEC_FILTER_LENGTH", "30", overrides))
        bounded_erl = _cfg_bool("JASPER_AEC_BOUNDED_ERL", "0", overrides)
        default_gain = float(_cfg_value("JASPER_AEC_DEFAULT_GAIN", "0.3", overrides))
        erle_max_l = float(_cfg_value("JASPER_AEC_ERLE_MAX_L", "1.5", overrides))
        erle_max_h = float(_cfg_value("JASPER_AEC_ERLE_MAX_H", "1.0", overrides))
        erle_onset = _cfg_bool("JASPER_AEC_ERLE_ONSET", "0", overrides)
        use_stationarity = _cfg_bool("JASPER_AEC_USE_STATIONARITY", "1", overrides)
        conservative_hf = _cfg_bool("JASPER_AEC_CONSERVATIVE_HF", "1", overrides)
        mask_hf_enr_t = float(_cfg_value(
            "JASPER_AEC_MASK_HF_ENR_T", "0.3", overrides,
        ))
        mask_hf_enr_s = float(_cfg_value(
            "JASPER_AEC_MASK_HF_ENR_S", "0.4", overrides,
        ))
        mask_hf_emr_t = float(_cfg_value(
            "JASPER_AEC_MASK_HF_EMR_T", "0.3", overrides,
        ))
        max_dec_lf = float(_cfg_value("JASPER_AEC_MAX_DEC_LF", "0.05", overrides))
        nearend_avg_blocks = int(_cfg_value(
            "JASPER_AEC_NEAREND_AVERAGE_BLOCKS", "4", overrides,
        ))
        nearend_mask_hf_enr_t = float(_cfg_value(
            "JASPER_AEC_NEAREND_MASK_HF_ENR_T", "0.1", overrides,
        ))
        nearend_mask_hf_enr_s = float(_cfg_value(
            "JASPER_AEC_NEAREND_MASK_HF_ENR_S", "0.3", overrides,
        ))
        nearend_mask_hf_emr_t = float(_cfg_value(
            "JASPER_AEC_NEAREND_MASK_HF_EMR_T", "0.3", overrides,
        ))
        nearend_max_dec_lf = float(_cfg_value(
            "JASPER_AEC_NEAREND_MAX_DEC_LF", "0.25", overrides,
        ))
        nearend_max_inc = float(_cfg_value(
            "JASPER_AEC_NEAREND_MAX_INC", "2.0", overrides,
        ))
        dnd_snr_threshold = float(_cfg_value(
            "JASPER_AEC_DND_SNR_THRESHOLD", "30", overrides,
        ))
        dnd_hold_duration = int(_cfg_value(
            "JASPER_AEC_DND_HOLD_DURATION", "50", overrides,
        ))
        dnd_enr_threshold = float(_cfg_value(
            "JASPER_AEC_DND_ENR_THRESHOLD", "0.25", overrides,
        ))
        dnd_trigger_threshold = int(_cfg_value(
            "JASPER_AEC_DND_TRIGGER_THRESHOLD", "12", overrides,
        ))
        stream_delay_ms = int(_cfg_value(
            "JASPER_AEC_STREAM_DELAY_MS", "40", overrides,
        ))

        self._aec = Aec3V2(
            stream_delay_ms=stream_delay_ms,
            enable_agc2=enable_agc2,
            ns_enabled=ns_enabled,
            ns_level=ns_level,
            agc1_enabled=agc1_enabled,
            agc1_target_dbfs=agc1_target_dbfs,
            agc1_max_gain_db=agc1_max_gain_db,
            filter_refined_length_blocks=filter_length,
            ep_strength_bounded_erl=bounded_erl,
            ep_strength_default_gain=default_gain,
            erle_max_l=erle_max_l,
            erle_max_h=erle_max_h,
            erle_onset_detection=erle_onset,
            use_stationarity_properties=use_stationarity,
            conservative_hf_suppression=conservative_hf,
            normal_mask_hf_enr_transparent=mask_hf_enr_t,
            normal_mask_hf_enr_suppress=mask_hf_enr_s,
            normal_mask_hf_emr_transparent=mask_hf_emr_t,
            normal_max_dec_factor_lf=max_dec_lf,
            nearend_average_blocks=nearend_avg_blocks,
            nearend_mask_hf_enr_transparent=nearend_mask_hf_enr_t,
            nearend_mask_hf_enr_suppress=nearend_mask_hf_enr_s,
            nearend_mask_hf_emr_transparent=nearend_mask_hf_emr_t,
            nearend_max_dec_factor_lf=nearend_max_dec_lf,
            nearend_max_inc_factor=nearend_max_inc,
            dominant_nearend_snr_threshold=dnd_snr_threshold,
            dominant_nearend_hold_duration=dnd_hold_duration,
            dominant_nearend_enr_threshold=dnd_enr_threshold,
            dominant_nearend_trigger_threshold=dnd_trigger_threshold,
        )
        logger.info(
            "engine=%s ns=%s/%s agc1=%s(target=%d,max=%ddB) "
            "agc2=%s stream_delay_ms=%d filter_len=%d bounded_erl=%s "
            "default_gain=%.2f "
            "erle=%.2f/%.2f onset=%s stationarity=%s conservative_hf=%s "
            "mask_hf=%.2f/%.2f/%.2f max_dec_lf=%.3f "
            "nearend_avg=%d nearend_mask_hf=%.2f/%.2f/%.2f "
            "nearend_max_dec_lf=%.3f nearend_max_inc=%.2f "
            "dnd=snr%.1f/enr%.2f/hold%d/trigger%d",
            label,
            "on" if ns_enabled else "off", ns_level,
            "on" if agc1_enabled else "off",
            agc1_target_dbfs, agc1_max_gain_db,
            "on" if enable_agc2 else "off", stream_delay_ms,
            filter_length, bounded_erl, default_gain,
            erle_max_l, erle_max_h,
            "on" if erle_onset else "off",
            "on" if use_stationarity else "off",
            "on" if conservative_hf else "off",
            mask_hf_enr_t, mask_hf_enr_s, mask_hf_emr_t,
            max_dec_lf,
            nearend_avg_blocks,
            nearend_mask_hf_enr_t, nearend_mask_hf_enr_s,
            nearend_mask_hf_emr_t,
            nearend_max_dec_lf, nearend_max_inc,
            dnd_snr_threshold, dnd_enr_threshold,
            dnd_hold_duration, dnd_trigger_threshold,
        )

    def process(self, mic: bytes, ref: bytes) -> bytes:
        return self._aec.process(mic, ref)

    def close(self) -> None:
        pass


def select_engine(
    overrides: dict[str, str] | None = None,
    label: str | None = None,
) -> Aec3Engine:
    """Pick the AEC engine and return it ready to `.process()`.

    JASPER_AEC_BINDING=v2 prefers v2; =v1 forces v1; default (=auto) tries v2
    first. Both v2 modes require the activation marker, source fingerprint,
    and extension digest to agree — an orphan or stale native module is never
    imported. Any unavailable or unverified v2 path falls back to the
    mandatory v1 binding.
    """
    pref = os.environ.get("JASPER_AEC_BINDING", "auto").strip().lower()
    if pref == "v1":
        return Aec3V1Engine(overrides=overrides, label=label or "aec3_v1")
    v2_verified = False
    if pref in {"auto", "v2"}:
        try:
            from jasper.enhanced_aec import runtime_v2_verified

            v2_verified = runtime_v2_verified()
        except (ImportError, OSError):
            v2_verified = False
    if v2_verified:
        try:
            import jasper_aec3
            if jasper_aec3.HAS_V2:
                return Aec3V2Engine(
                    overrides=overrides,
                    label=label or "aec3_v2(BEST_A)",
                )
        except ImportError:
            pass
    if pref == "v2":
        logger.warning(
            "JASPER_AEC_BINDING=v2 requested but no verified enhanced "
            "AEC activation is available — falling back to v1 binding"
        )
    else:
        logger.info(
            "verified jasper_aec3._aec3_v2 not available — falling back "
            "to v1 binding"
        )
    return Aec3V1Engine(overrides=overrides, label=label or "aec3_v1")
