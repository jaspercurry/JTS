# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.active_speaker import baseline_profile
from jasper.cli.doctor.active_speaker import REASON_BASS_EXTENSION_NOT_COMMISSIONED, check_bass_extension_profile
from tests.test_bass_extension_dynamic import _descriptor


@pytest.mark.parametrize("section,status,reason", [
    ({}, "ok", REASON_BASS_EXTENSION_NOT_COMMISSIONED),
    (_descriptor().payload(), "ok", ""),
    ({"low_boost_db": 4.0, "reference_level_db": -10.0, "detector_lowpass_hz": 120.0, "compressor_threshold_dbfs": -12.0},
     "fail", "bass_descriptor_malformed"),
])
def test_doctor_reads_applied_bass(monkeypatch, section, status, reason):
    monkeypatch.setattr(baseline_profile, "load_applied_baseline_profile_state",
                        lambda: {"recomposition_snapshot": {"bass_extension": section}})
    result = check_bass_extension_profile()
    assert (result.status, result.reason) == (status, reason)
