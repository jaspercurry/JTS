# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`_current_stream`: the presentation shape for what is playing right now --
the USB latency breakdown, when the processing/media/output rows appear at
all, and AirPlay's sync-evidence-only latency (no numeric claim).

Behavior lives in :mod:`jasper.control.audio_stream_card`; these exercise it
only through :func:`compose_audio_health`, the public contract, matching how
the rest of the split pins its leaves.
"""

from __future__ import annotations

import json

from jasper.control.audio_health import compose_audio_health

from .audio_health_fixtures import _airplay, _compose, _outputd, _route


def test_usb_current_stream_is_presentation_ready_without_bitrate_inference() -> None:
    health = _compose(selected="usbsink", ladder="l0_locked")
    stream = health["current_stream"]

    assert stream["source_id"] == "usbsink"
    assert "bitrate" not in json.dumps(stream).lower()
    assert stream["media"]["summary"]
    assert stream["output"]["summary"]
    # The rendered "... latency stable" wording is presentation copy for
    # `runtime.raw_mode`/the summed queue estimate, not a separate fact --
    # pin those instead of the sentence.
    assert stream["latency"]["mode"] == "l0_locked"
    assert isinstance(stream["latency"]["estimate"]["lower_ms"], float)
    assert stream["latency"]["estimate"]["lower_ms"] > 0
    assert stream["latency"]["detail"] == ""
    assert [row["label"] for row in stream["latency"]["details"]] == [
        "Mixing queue",
        "DSP queue",
        "DAC presentation queue",
    ]
    assert stream["session"]["summary"] == "No interruptions observed"
    assert [row["label"] for row in stream["reliability"]["details"]] == [
        "Output queue pressure",
    ]


def test_usb_latency_omits_stale_or_unaged_dac_delay() -> None:
    for outputd in (_outputd(delay_age_ms=4000), _outputd()):
        if outputd["dac"]["snd_pcm_delay_sample_age_ms"] == 10:
            del outputd["dac"]["snd_pcm_delay_sample_age_ms"]
        health = compose_audio_health(
            airplay=_airplay(selected="usbsink", ladder="l0_locked"),
            outputd=outputd,
            route=_route(),
            issues=[],
            sampled_at=1000.0,
        )
        latency_details = health["current_stream"]["latency"]["details"]
        output_details = health["current_stream"]["output"]["details"]

        assert all(row["label"] != "DAC presentation queue" for row in latency_details)
        assert all(row["label"] != "DAC queue" for row in output_details)


def test_usb_latency_omits_negative_queue_telemetry() -> None:
    airplay = _airplay(selected="usbsink", ladder="l0_locked")
    fanin = airplay["current"]["fanin"]
    fanin["inputs"]["usbsink"]["resampler"] = {"fill_frames": -480}
    fanin["output"]["ring"]["occupancy"] = -2
    airplay["current"]["camilla"]["buffer_level"] = -32
    outputd = _outputd()
    outputd["dac"]["snd_pcm_delay_ms"] = -5.0

    health = compose_audio_health(
        airplay=airplay,
        outputd=outputd,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    latency = health["current_stream"]["latency"]

    assert latency["estimate"] is None
    assert latency["details"] == []
    assert health["current_stream"]["output"]["details"] == []


def test_current_stream_omits_processing_without_live_processing_telemetry() -> None:
    airplay = _airplay(selected="spotify")
    airplay["current"]["camilla"] = None
    health = compose_audio_health(
        airplay=airplay,
        outputd=_outputd(),
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )

    assert "processing" not in health["current_stream"]


def test_airplay_uses_sync_evidence_without_numeric_latency_claim() -> None:
    health = _compose(selected="airplay")
    latency = health["current_stream"]["latency"]
    airplay_card = next(c for c in health["sources"] if c["id"] == "airplay")

    assert airplay_card["timing"]["status"] == "ok"
    assert airplay_card["timing"]["kind"] == "sync"
    assert latency["summary"] == airplay_card["timing"]["headline"]
    assert latency["details"] == []
    assert "estimate" not in latency
    assert "ms" not in latency["summary"]


def test_unsupported_source_omits_latency_and_missing_output_is_not_active() -> None:
    health = compose_audio_health(
        airplay=_airplay(selected="spotify"),
        outputd=None,
        route=_route(),
        issues=[],
        sampled_at=1000.0,
    )
    stream = health["current_stream"]

    assert "latency" not in stream
    assert "output" not in stream
