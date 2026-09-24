# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import pytest

from jasper.active_speaker import baseline_profile
from jasper.cli.doctor.active_speaker import check_bass_extension_profile
from tests.test_bass_extension_dynamic import _descriptor


@pytest.mark.parametrize("descriptor", [{}, _descriptor().payload()])
def test_doctor_reads_applied_bass(monkeypatch, descriptor):
    monkeypatch.setattr(baseline_profile, "applied_bass_extension", lambda: descriptor)
    result = check_bass_extension_profile()
    assert result.status == "ok"
