# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from jasper.tts_routing import FANIN_TTS_SOCKET
from jasper.voice_daemon import _tts_ready_detail


def test_tts_ready_detail_reports_outputd_socket() -> None:
    cfg = SimpleNamespace(
        tts_transport="outputd",
        tts_outputd_socket=FANIN_TTS_SOCKET,
        tts_device="jasper_out",
    )

    detail = _tts_ready_detail(cfg)

    assert detail == (
        "tts_transport=outputd "
        "tts_owner=fanin "
        f"tts_socket={FANIN_TTS_SOCKET}"
    )
    assert "jasper_out" not in detail


def test_tts_ready_detail_marks_non_outputd_transport_unsupported() -> None:
    cfg = SimpleNamespace(
        tts_transport="sounddevice",
        tts_outputd_socket=FANIN_TTS_SOCKET,
        tts_device="jasper_out",
    )

    detail = _tts_ready_detail(cfg)

    assert detail == "tts_transport=sounddevice unsupported=true"
