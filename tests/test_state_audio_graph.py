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
    # host_clock is absent from the usbsink_raw shape — the standby-only bridge no
    # longer emits a host_clock block (the solo host clock was removed with the
    # aloop path), so the aggregator surfaces rust_bridge.host_clock as None. The
    # LIVE combo host clock is fanin.host_clock, covered by
    # tests/test_fanin_host_clock_contract.py.
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


# --- /state.audio_graph.coupling (P2) ----------------------------------------


def test_coupling_state_loopback_default_is_coherent(monkeypatch):
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "loopback",
    )
    # No outputd.env -> content_bridge defaults to direct.
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH",
        "/nonexistent/outputd.env",
    )
    block = state_aggregate._coupling_state(
        fanin_status={"output": {"transport": "loopback"}}
    )
    assert block["persisted"] == "loopback"
    assert block["content_bridge"] == "direct"
    assert block["coherent"] is True
    assert block["live_transport"] == "loopback"


def test_coupling_state_ring_armed_reports_coherent_pair(monkeypatch, tmp_path):
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd_env)
    )
    block = state_aggregate._coupling_state(
        fanin_status={"output": {"transport": "shm_ring", "ring": {}}}
    )
    assert block["persisted"] == "shm_ring"
    assert block["content_bridge"] == "shm_ring"
    assert block["coherent"] is True
    assert block["live_transport"] == "shm_ring"


def test_coupling_state_partial_flip_reports_incoherent(monkeypatch, tmp_path):
    # shm_ring fan-in but direct outputd = partial flip -> coherent False.
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("")  # bridge defaults to direct
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd_env)
    )
    block = state_aggregate._coupling_state(fanin_status=None)
    assert block["persisted"] == "shm_ring"
    assert block["content_bridge"] == "direct"
    assert block["coherent"] is False


def test_coupling_state_fail_soft_on_read_error(monkeypatch):
    # A read failure degrades to the loopback default, never raises. OSError is a
    # realistic failure (an unreadable env file); the fail-soft catch is a
    # concrete exception set, not a blind except.
    def _boom(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling", _boom
    )
    block = state_aggregate._coupling_state(fanin_status=None)
    assert block == {
        "persisted": "loopback",
        "content_bridge": "direct",
        "coherent": True,
        "live_transport": None,
        "choice": "auto",
        "combo": {"state": "disarmed", "fallback": None},
    }


def test_coupling_state_choice_reports_operator_marker(monkeypatch, tmp_path):
    # P4 default-flip: /state surfaces whether the coupling is an operator pick or
    # an auto-resolved default, read from the JASPER_FANIN_COUPLING_CHOICE marker.
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        "JASPER_FANIN_COUPLING_CHOICE=operator\n"
        "JASPER_FANIN_CAMILLA_COUPLING=loopback\n"
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "loopback",
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH",
        str(tmp_path / "nope.env"),
    )
    block = state_aggregate._coupling_state(fanin_status=None)
    assert block["choice"] == "operator"


def test_coupling_state_choice_defaults_to_auto(monkeypatch, tmp_path):
    # No marker in fanin.env -> the default is auto-owned.
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH",
        str(tmp_path / "nope.env"),
    )
    block = state_aggregate._coupling_state(fanin_status=None)
    assert block["choice"] == "auto"


# ---- combo runtime-fallback state (defect 2026-07-10) ----------------------


def _patch_coupling_reads(monkeypatch, tmp_path, *, armed=False):
    """Neutralize the ring/coupling reads so a _coupling_state() call exercises
    only the combo sub-block. Writes a fanin.env with (un)armed combo."""
    from jasper.fanin import coupling_auto as ca

    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda *a, **k: "loopback",
    )
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        f"{ca.USB_DIRECT_ENV_VAR}={ca.USB_COMBO_ENABLED_VALUE}\n" if armed else ""
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH",
        str(tmp_path / "outputd.env"),
    )


def test_combo_state_armed(monkeypatch, tmp_path):
    from jasper.fanin import combo_health as ch

    _patch_coupling_reads(monkeypatch, tmp_path, armed=True)
    monkeypatch.setattr(ch, "read_fallback_marker", lambda *a, **k: None)
    block = state_aggregate._coupling_state(fanin_status=None)
    assert block["combo"] == {"state": "armed", "fallback": None}


def test_combo_state_disarmed(monkeypatch, tmp_path):
    from jasper.fanin import combo_health as ch

    _patch_coupling_reads(monkeypatch, tmp_path, armed=False)
    monkeypatch.setattr(ch, "read_fallback_marker", lambda *a, **k: None)
    block = state_aggregate._coupling_state(fanin_status=None)
    assert block["combo"] == {"state": "disarmed", "fallback": None}


def test_combo_state_fallback_reports_reason(monkeypatch, tmp_path):
    from jasper.fanin import combo_health as ch

    _patch_coupling_reads(monkeypatch, tmp_path, armed=True)  # armed env, but...
    marker = ch.FallbackMarker(reason="capture broke x2", at_epoch=42.0)
    monkeypatch.setattr(ch, "read_fallback_marker", lambda *a, **k: marker)
    block = state_aggregate._coupling_state(fanin_status=None)
    # The marker wins over the armed env — the box is on the fallback path.
    assert block["combo"]["state"] == "fallback"
    assert block["combo"]["fallback"] == {"reason": "capture broke x2", "at_epoch": 42.0}
