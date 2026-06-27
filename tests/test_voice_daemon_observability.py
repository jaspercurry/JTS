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


def test_unpriced_research_model_warns(caplog) -> None:
    # Pins the documented C2-5 behavior: an unpriced research model (e.g.
    # JASPER_RESEARCH_OPENAI_MODEL overridden to a model with no rate) records
    # $0 cost and the daily spend cap can't bound it, so the daemon emits a
    # WARNING. `gpt-realtime-3` is the canonical unknown/unpriced id (shared
    # with test_usage.test_unknown_model_is_unpriced_not_invented).
    import logging

    from jasper.usage import load_pricing_overrides
    from jasper.voice.daemon_main import _warn_if_research_model_unpriced

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        fired = _warn_if_research_model_unpriced(
            "gpt-realtime-3",
            pricing_overrides=load_pricing_overrides(),
        )

    assert fired is True
    assert any(
        "event=pricing.unpriced" in r.getMessage()
        and "surface=research" in r.getMessage()
        and "model=gpt-realtime-3" in r.getMessage()
        for r in caplog.records
    )


def test_priced_research_model_does_not_warn(caplog) -> None:
    # Contrast case: the shipped research default IS priced, so the warn path
    # must stay silent. Keeps the warn from becoming journal noise on the
    # common path and proves the guard is rate-driven, not always-on.
    import logging

    from jasper.research.providers import openai_research
    from jasper.usage import load_pricing_overrides
    from jasper.voice.daemon_main import _warn_if_research_model_unpriced

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        fired = _warn_if_research_model_unpriced(
            openai_research.DEFAULT_MODEL,
            pricing_overrides=load_pricing_overrides(),
        )

    assert fired is False
    assert not any(
        "event=pricing.unpriced" in r.getMessage() for r in caplog.records
    )
