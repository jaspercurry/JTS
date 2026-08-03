# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_aec3_bindings_share_one_ten_ms_process_loop():
    shared = (ROOT / "jasper_aec3/src/process_10ms.h").read_text(encoding="utf-8")
    v1 = (ROOT / "jasper_aec3/src/aec3_binding.cpp").read_text(encoding="utf-8")
    v2 = (ROOT / "jasper_aec3/src/aec3_binding_v2.cpp").read_text(encoding="utf-8")
    setup = (ROOT / "jasper_aec3/setup.py").read_text(encoding="utf-8")

    assert "ProcessReverseStream" in shared
    assert "ProcessStream" in shared
    assert "set_stream_delay_ms" in shared
    assert "apm_->ProcessReverseStream" not in v1
    assert "apm_->ProcessReverseStream" not in v2
    assert "jasper_aec3::process_10ms" in v1
    assert "jasper_aec3::process_10ms" in v2
    assert setup.count('depends=["src/process_10ms.h"]') == 2
