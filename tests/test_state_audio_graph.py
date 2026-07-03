# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper import audio_runtime_plan
from jasper.audio_hardware.dac import APPLE_USB_C_DONGLE_ID
from jasper.control import state_aggregate


def test_audio_graph_state_aggregates_route_artifact_bridge_fanin_and_outputd(
    monkeypatch,
):
    plan = audio_runtime_plan.build_audio_runtime_plan(
        base_env={
            audio_runtime_plan.AUDIO_ROUTE_PROFILE_KEY: (
                audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
            )
        },
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )
    artifact = {
        "status": "warn",
        "p95_ms": 38.0,
        "p99_ms": None,
        "sample_count": 200,
        "duration_seconds": 300,
        "config_match": True,
    }
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )
    monkeypatch.setattr(
        state_aggregate,
        "_route_latency_artifact_state",
        lambda observed_plan: artifact,
    )

    graph = state_aggregate._audio_graph_state(
        usbsink_raw={
            "implementation": "rust",
            "period_frames": 256,
            "ring": {"fill_periods": 1, "capacity_periods": 3},
            "counters": {"playback_xruns": 0, "underflow_periods": 0},
        },
        fanin_status={
            "inputs": [
                {"label": "spotify", "xrun_count": 2},
                {
                    "label": "usbsink",
                    "xrun_count": 0,
                    "resampler": {
                        "locked": True,
                        "fill_frames": 1120,
                        "target_fill_frames": 2048,
                        "ratio_ppm": 12.5,
                    },
                },
            ]
        },
        outputd_status={
            "dac": {
                "snd_pcm_delay_ms": 10.333,
                "snd_pcm_delay_frames": 496,
            },
            "aec_clock": {
                "status": "locked",
                "latency": {"dac_presentation_ms": 10.333},
            },
        },
    )

    assert graph is not None
    assert graph["route"]["id"] == audio_runtime_plan.ROUTE_USB_LOW_LATENCY_48K
    assert graph["route"]["claim_status"] == "warn"
    assert graph["route"]["route_config_hash"] == plan.route_config_hash
    assert graph["artifact"] == artifact
    # host_clock is absent from this fixture (pre-Stage-1 usbsink_raw shape);
    # the aggregator must surface that as None, not KeyError or a guessed
    # default. The present-pass-through case is covered by
    # tests/test_usbsink_host_clock_contract.py.
    assert graph["rust_bridge"] == {
        "implementation": "rust",
        "period_frames": 256,
        "ring": {"fill_periods": 1, "capacity_periods": 3},
        "counters": {"playback_xruns": 0, "underflow_periods": 0},
        "host_clock": None,
    }
    assert graph["fanin"]["resampler"]["locked"] is True
    assert graph["fanin"]["resampler"]["target_fill_frames"] == 2048
    assert graph["outputd"]["dac_delay_ms"] == 10.333
    assert graph["outputd"]["final_reference_health"]["status"] == "locked"
    assert graph["outputd"]["route_latency_components"] == {
        "dac_presentation_ms": 10.333
    }
