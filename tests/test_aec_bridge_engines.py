# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin for `jasper.aec.bridge_engines.select_engine`.

The engine a configuration selects, and the exact keyword arguments that
configuration hands the `jasper_aec3` binding, are what reaches WebRTC AEC3
on the wake path.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from jasper import enhanced_aec
from jasper.aec import bridge_engines

V1_DEFAULTS = {
    "stream_delay_ms": 40,
    "enable_agc2": False,
    "ns_enabled": True,
    "ns_level": "low",
    "agc1_enabled": False,
    "agc1_target_dbfs": 9,
    "agc1_max_gain_db": 18,
}

V2_DEFAULTS = {
    "stream_delay_ms": 40,
    "enable_agc2": False,
    "ns_enabled": True,
    "ns_level": "low",
    "agc1_enabled": True,
    "agc1_target_dbfs": 9,
    "agc1_max_gain_db": 18,
    "filter_refined_length_blocks": 30,
    "ep_strength_bounded_erl": False,
    "ep_strength_default_gain": 0.3,
    "erle_max_l": 1.5,
    "erle_max_h": 1.0,
    "erle_onset_detection": False,
    "use_stationarity_properties": True,
    "conservative_hf_suppression": True,
    "normal_mask_hf_enr_transparent": 0.3,
    "normal_mask_hf_enr_suppress": 0.4,
    "normal_mask_hf_emr_transparent": 0.3,
    "normal_max_dec_factor_lf": 0.05,
    "nearend_average_blocks": 4,
    "nearend_mask_hf_enr_transparent": 0.1,
    "nearend_mask_hf_enr_suppress": 0.3,
    "nearend_mask_hf_emr_transparent": 0.3,
    "nearend_max_dec_factor_lf": 0.25,
    "nearend_max_inc_factor": 2.0,
    "dominant_nearend_snr_threshold": 30.0,
    "dominant_nearend_hold_duration": 50,
    "dominant_nearend_enr_threshold": 0.25,
    "dominant_nearend_trigger_threshold": 12,
}

V1_OVERRIDES = {
    "JASPER_AEC_NS_ENABLED": "0",
    "JASPER_AEC_NS_LEVEL": " HIGH ",
    "JASPER_AEC_AGC1_ENABLED": "yes",
    "JASPER_AEC_AGC1_TARGET_DBFS": "6",
    "JASPER_AEC_AGC1_MAX_GAIN_DB": "24",
    "JASPER_AEC_AGC2": "on",
    "JASPER_AEC_STREAM_DELAY_MS": "80",
}

V2_OVERRIDES = {
    "JASPER_AEC_NS_ENABLED": "0",
    "JASPER_AEC_AGC1_ENABLED": "0",
    "JASPER_AEC_STREAM_DELAY_MS": "0",
    "JASPER_AEC_FILTER_LENGTH": "13",
    "JASPER_AEC_BOUNDED_ERL": "1",
    "JASPER_AEC_DEFAULT_GAIN": "0.7",
    "JASPER_AEC_ERLE_MAX_L": "4.0",
    "JASPER_AEC_ERLE_MAX_H": "2.0",
    "JASPER_AEC_ERLE_ONSET": "true",
    "JASPER_AEC_USE_STATIONARITY": "0",
    "JASPER_AEC_CONSERVATIVE_HF": "0",
    "JASPER_AEC_MASK_HF_ENR_T": "0.11",
    "JASPER_AEC_MASK_HF_ENR_S": "0.22",
    "JASPER_AEC_MASK_HF_EMR_T": "0.33",
    "JASPER_AEC_MAX_DEC_LF": "0.44",
    "JASPER_AEC_NEAREND_AVERAGE_BLOCKS": "7",
    "JASPER_AEC_NEAREND_MASK_HF_ENR_T": "0.55",
    "JASPER_AEC_NEAREND_MASK_HF_ENR_S": "0.66",
    "JASPER_AEC_NEAREND_MASK_HF_EMR_T": "0.77",
    "JASPER_AEC_NEAREND_MAX_DEC_LF": "0.88",
    "JASPER_AEC_NEAREND_MAX_INC": "3.5",
    "JASPER_AEC_DND_SNR_THRESHOLD": "12.5",
    "JASPER_AEC_DND_HOLD_DURATION": "9",
    "JASPER_AEC_DND_ENR_THRESHOLD": "0.9",
    "JASPER_AEC_DND_TRIGGER_THRESHOLD": "3",
}

# name, binding pref, v2 verified, overrides, label, env, expected binding,
# expected kwargs.
CASES = [
    ("v1_forced", "v1", False, None, None, {}, "Aec3", V1_DEFAULTS),
    ("auto_unverified", "auto", False, None, None, {}, "Aec3", V1_DEFAULTS),
    ("v2_unverified", "v2", False, None, None, {}, "Aec3", V1_DEFAULTS),
    ("auto_verified", "auto", True, None, None, {}, "Aec3V2", V2_DEFAULTS),
    (
        "v2_verified_labelled",
        "v2", True, None, "corpus_usb", {}, "Aec3V2", V2_DEFAULTS,
    ),
    (
        "v1_overrides",
        "v1", False, V1_OVERRIDES, "sweep_v1", {}, "Aec3",
        V1_DEFAULTS | {
            "ns_enabled": False,
            "ns_level": "high",
            "agc1_enabled": True,
            "agc1_target_dbfs": 6,
            "agc1_max_gain_db": 24,
            "enable_agc2": True,
            "stream_delay_ms": 80,
        },
    ),
    (
        "v2_overrides",
        "v2", True, V2_OVERRIDES, None, {}, "Aec3V2",
        V2_DEFAULTS | {
            "ns_enabled": False,
            "agc1_enabled": False,
            "stream_delay_ms": 0,
            "filter_refined_length_blocks": 13,
            "ep_strength_bounded_erl": True,
            "ep_strength_default_gain": 0.7,
            "erle_max_l": 4.0,
            "erle_max_h": 2.0,
            "erle_onset_detection": True,
            "use_stationarity_properties": False,
            "conservative_hf_suppression": False,
            "normal_mask_hf_enr_transparent": 0.11,
            "normal_mask_hf_enr_suppress": 0.22,
            "normal_mask_hf_emr_transparent": 0.33,
            "normal_max_dec_factor_lf": 0.44,
            "nearend_average_blocks": 7,
            "nearend_mask_hf_enr_transparent": 0.55,
            "nearend_mask_hf_enr_suppress": 0.66,
            "nearend_mask_hf_emr_transparent": 0.77,
            "nearend_max_dec_factor_lf": 0.88,
            "nearend_max_inc_factor": 3.5,
            "dominant_nearend_snr_threshold": 12.5,
            "dominant_nearend_hold_duration": 9,
            "dominant_nearend_enr_threshold": 0.9,
            "dominant_nearend_trigger_threshold": 3,
        },
    ),
    (
        "env_without_overrides",
        "v1", False, None, None,
        {
            "JASPER_AEC_NS_LEVEL": "moderate",
            "JASPER_AEC_AGC1_TARGET_DBFS": "3",
            "JASPER_AEC_STREAM_DELAY_MS": "120",
        },
        "Aec3",
        V1_DEFAULTS | {
            "ns_level": "moderate",
            "agc1_target_dbfs": 3,
            "stream_delay_ms": 120,
        },
    ),
]


@pytest.mark.parametrize(
    "pref,verified,overrides,label,env,expected_binding,expected_kwargs",
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_select_engine_configuration_snapshot(
    monkeypatch,
    pref,
    verified,
    overrides,
    label,
    env,
    expected_binding,
    expected_kwargs,
):
    built: list[tuple[str, dict]] = []

    class _FakeAec3:
        def __init__(self, **kwargs):
            built.append(("Aec3", kwargs))

    class _FakeAec3V2:
        def __init__(self, **kwargs):
            built.append(("Aec3V2", kwargs))

    for key in [k for k in os.environ if k.startswith("JASPER_AEC")]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("JASPER_AEC_BINDING", pref)
    monkeypatch.setattr(enhanced_aec, "runtime_v2_verified", lambda: verified)
    monkeypatch.setitem(
        sys.modules,
        "jasper_aec3",
        SimpleNamespace(HAS_V2=True, Aec3=_FakeAec3, Aec3V2=_FakeAec3V2),
    )

    engine = bridge_engines.select_engine(overrides=overrides, label=label)

    expected_engine = (
        bridge_engines.Aec3V2Engine
        if expected_binding == "Aec3V2"
        else bridge_engines.Aec3V1Engine
    )
    assert isinstance(engine, expected_engine)
    assert built == [(expected_binding, expected_kwargs)]
