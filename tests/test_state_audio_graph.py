# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper import audio_runtime_plan
from jasper.audio_hardware.dac import APPLE_USB_C_DONGLE_ID
from jasper.control import state_aggregate


def test_audio_graph_state_aggregates_route_fanin_and_outputd(
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
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    graph = state_aggregate._audio_graph_state(
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
    assert graph["route"]["route_config_hash"] == plan.route_config_hash
    assert "rust_bridge" not in graph
    assert graph["fanin"]["resampler"]["locked"] is True
    assert graph["fanin"]["resampler"]["target_fill_frames"] == 2048
    assert graph["outputd"]["dac_delay_ms"] == 10.333
    assert graph["outputd"]["final_reference_health"]["status"] == "locked"
    assert graph["outputd"]["route_latency_components"] == {
        "dac_presentation_ms": 10.333
    }


@pytest.mark.parametrize(
    ("service_states", "expected"),
    [
        # A cleanly stopped CamillaDSP: no other field in this block can see
        # it, because fan-in free-run-drops on an absent ring reader and
        # outputd zero-fills an absent writer.
        (
            {
                "jasper-camilla.service": {
                    "load_state": "loaded",
                    "active_state": "inactive",
                    "sub_state": "dead",
                    "result": "success",
                },
            },
            {
                "unit": "jasper-camilla.service",
                "load_state": "loaded",
                "active_state": "inactive",
                "sub_state": "dead",
                "result": "success",
            },
        ),
        # Nothing sampled yet -> "not observed", never a guessed "running".
        (None, None),
        ({}, None),
    ],
)
def test_audio_graph_publishes_camilla_unit_state(
    monkeypatch, service_states, expected
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
    monkeypatch.setattr(
        audio_runtime_plan,
        "build_audio_runtime_plan_from_system",
        lambda: plan,
    )

    graph = state_aggregate._audio_graph_state(
        fanin_status=None,
        outputd_status=None,
        service_states=service_states,
    )

    assert graph is not None
    assert graph["camilla"] == expected


def test_camilla_row_survives_an_unreadable_route_plan(monkeypatch):
    """An absent DAC throws the plan read AND stops CamillaDSP — same cause."""
    def _boom():
        raise OSError("no output hardware")

    monkeypatch.setattr(
        audio_runtime_plan, "build_audio_runtime_plan_from_system", _boom,
    )

    graph = state_aggregate._audio_graph_state(
        fanin_status=None,
        outputd_status=None,
        service_states={
            "jasper-camilla.service": {
                "load_state": "loaded",
                "active_state": "inactive",
                "sub_state": "dead",
                "result": "success",
            },
        },
    )

    assert graph is not None
    assert graph["route"]["status"] == "unavailable"
    assert graph["camilla"]["active_state"] == "inactive"


# --- /state.audio_graph.coupling (P2) ----------------------------------------


def _pin_fanin_env(monkeypatch, tmp_path, text: str) -> None:
    """Point the coupling block at a real fanin.env holding ``text``."""
    path = tmp_path / "fanin.env"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr("jasper.fanin.ring_health.FANIN_ENV_PATH", str(path))


def test_coupling_state_undeclared_box_is_not_coherent(monkeypatch, tmp_path):
    """ADR-0100: only the ring PAIR is coherent, and one half here has not moved.

    The two halves answer absence differently, and that is the point of this
    case: an unreadable outputd.env still leaves outputd on the ring (its own
    default), while the persisted coupling is a written token that says
    ``loopback``. So the bridge reports the ring — what outputd runs — and the
    pair is still incoherent, because the fan-in half names the retired
    transport, which this surface publishes AS WRITTEN.
    """
    _pin_fanin_env(monkeypatch, tmp_path, "JASPER_FANIN_CAMILLA_COUPLING=loopback\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH",
        "/nonexistent/outputd.env",
    )
    block = state_aggregate._coupling_state(
        fanin_status={"output": {"transport": "loopback"}}
    )
    assert block["persisted"] == "loopback"
    assert block["content_bridge"] == "shm_ring"
    assert block["intent_coherent"] is False
    assert block["live_transport"] == "loopback"


@pytest.mark.parametrize(
    "outputd_env_text",
    [
        # The reconciler WRITES the token, so a converged box carries it...
        "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n",
        # ...and a box that never ran it is on the ring anyway, because that is
        # outputd's own answer to an undeclared key. Both are the same running
        # transport, so both must report the same coherent pair.
        "",
    ],
)
def test_coupling_state_ring_armed_reports_coherent_pair(
    monkeypatch, tmp_path, outputd_env_text
):
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(outputd_env_text)
    _pin_fanin_env(monkeypatch, tmp_path, "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd_env)
    )
    block = state_aggregate._coupling_state(
        fanin_status={"output": {"transport": "shm_ring", "ring": {}}}
    )
    assert block["persisted"] == "shm_ring"
    assert block["content_bridge"] == "shm_ring"
    assert block["intent_coherent"] is True
    assert block["live_transport"] == "shm_ring"


@pytest.mark.parametrize(
    "fanin_text,persisted,coherent",
    [
        ("", None, True),
        ("JASPER_FANIN_CAMILLA_COUPLING=\n", None, True),
        ("JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n", "shm_ring", True),
        ("JASPER_FANIN_CAMILLA_COUPLING=loopback\n", "loopback", False),
    ],
    ids=["absent_key", "empty", "declared", "retired_token"],
)
def test_coupling_state_intent_coherence_follows_the_shared_predicate(
    monkeypatch, tmp_path, fanin_text, persisted, coherent
):
    """Both halves ask their own daemon's accept set, so UNDECLARED is the ring.

    ``persisted`` still publishes the token AS WRITTEN — that is what names a
    migrating box — but the VERDICT is `persisted_coupling_feeds_ring`'s, the
    same rule `jasper-fanin` applies. Keying it on the token would call every
    box the reconciler has not written an incoherent pair while both daemons ran
    the ring (#3655).
    """
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("")
    _pin_fanin_env(monkeypatch, tmp_path, fanin_text)
    monkeypatch.setattr(
        "jasper.fanin.ring_health.OUTPUTD_ENV_PATH", str(outputd_env)
    )

    block = state_aggregate._coupling_state(fanin_status=None)

    assert block["persisted"] == persisted
    assert block["content_bridge"] == "shm_ring"
    assert block["intent_coherent"] is coherent


# The two producers spell the SAME field differently, and `_observed_ring_wire`
# is the one place that reconciles them. These fixtures mirror the live emitters
# rather than an idealized shape:
#
#   fan-in   rust/jasper-fanin/src/state.rs — the `ring` block nested under
#            `output`, keys `path` / `slots` / `wire_format` / `channels` / ...
#   outputd  rust/jasper-outputd/src/state.rs — a TOP-LEVEL `shm_ring` block,
#            keys `enabled` / `path` / `attached` / `slots` / `format` /
#            `channels` / ...
#
# Getting a path or a key name wrong here is invisible in production: the reader
# answers None, /state publishes "not observed", and a reader cannot tell that
# from "the daemon is not on a ring". That is why the positive path is pinned.
_FANIN_RING_STATUS = {
    "output": {
        "transport": "shm_ring",
        "ring": {
            "path": "/dev/shm/jts-ring/program.ring",
            "slots": 2,
            "wire_format": "S32_LE",
            "channels": 2,
            "occupancy": 1,
            "published": 4096,
        },
    }
}
_OUTPUTD_RING_STATUS = {
    "shm_ring": {
        "enabled": True,
        "path": "/dev/shm/jts-ring/content.ring",
        "attached": True,
        "slots": 2,
        "format": "S32_LE",
        "channels": 4,
        "occupancy": 1,
    }
}


def test_observed_ring_wire_reads_both_producers_real_status_shapes(monkeypatch, tmp_path):
    """THE POSITIVE PATH, against the shapes the Rust daemons actually emit.

    Ring A and Ring B are reported independently and may legitimately differ on
    channels (Ring A is always the stereo program fan-in mixes; Ring B follows
    the box's output topology), so the two are not interchangeable and a test
    that only proved "some dict comes back" would not catch the two being
    crossed.
    """
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n")
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd_env)
    )
    block = state_aggregate._coupling_state(
        fanin_status=_FANIN_RING_STATUS, outputd_status=_OUTPUTD_RING_STATUS
    )
    assert block["observed"]["ring_a"] == {
        "sample_format": "S32_LE",
        "channels": 2,
        "slots": 2,
    }
    assert block["observed"]["ring_b"] == {
        "sample_format": "S32_LE",
        "channels": 4,
        "slots": 2,
    }


def test_observed_ring_wire_distinguishes_not_observed_from_observed():
    """``None`` means NOT OBSERVED and must never be read as "observed correct".

    Three ways to get there, all real: an unreachable daemon, a loopback daemon
    that publishes no ring block at all, and a block present but silent on both
    wire axes. The last one is the interesting one — a partially-populated block
    must not yield a dict of Nones that a reader would treat as a reading.
    """
    observed = state_aggregate._observed_ring_wire
    assert observed(None, ("output", "ring"), format_key="wire_format") is None
    # Loopback fan-in: an `output` block with no `ring` child.
    assert (
        observed(
            {"output": {"transport": "loopback"}},
            ("output", "ring"),
            format_key="wire_format",
        )
        is None
    )
    # Present but silent on BOTH axes -> not observed.
    assert (
        observed({"shm_ring": {"attached": False}}, ("shm_ring",), format_key="format")
        is None
    )
    # ONE axis present IS an observation — reporting it beats discarding it.
    assert observed(
        {"shm_ring": {"channels": 2}}, ("shm_ring",), format_key="format"
    ) == {"sample_format": None, "channels": 2, "slots": None}


def test_observed_ring_wire_uses_each_producers_own_format_key():
    """The spelling reconciliation is the reason this helper exists.

    Reading fan-in with outputd's key (or the reverse) silently answers "not
    observed" on a healthy armed box, so the two spellings are pinned against
    each other rather than each in isolation.
    """
    observed = state_aggregate._observed_ring_wire
    # fan-in's key applied to fan-in's block: found.
    assert (
        observed(_FANIN_RING_STATUS, ("output", "ring"), format_key="wire_format")[
            "sample_format"
        ]
        == "S32_LE"
    )
    # outputd's key applied to fan-in's block: the format axis goes silent, which
    # is exactly the invisible failure the reconciliation prevents.
    assert (
        observed(_FANIN_RING_STATUS, ("output", "ring"), format_key="format")[
            "sample_format"
        ]
        is None
    )


def test_coupling_state_bonded_member_is_coherent_on_the_return_ring(
    monkeypatch, tmp_path
):
    """A DUMB bonded member is a THIRD content source, not an incoherent box.

    Its marker lives in `grouping-outputd.env`, the second env layer — so this
    also pins that this surface reads the merge rather than `outputd.env` alone,
    which is what left it reporting a bonded box on an env it is not running.
    Its fan-in hop is Ring A like every other box's, and its post-DSP hop is the
    bonded return ring BY DESIGN, so the pair is coherent.
    """
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("", encoding="utf-8")
    grouping_env = tmp_path / "grouping-outputd.env"
    grouping_env.write_text(
        "JASPER_OUTPUTD_DAC_CONTENT_LANE=1\n", encoding="utf-8"
    )
    _pin_fanin_env(monkeypatch, tmp_path, "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd_env)
    )
    monkeypatch.setattr(
        "jasper.multiroom.reconcile.OUTPUTD_GROUPING_ENV_FILE", str(grouping_env)
    )

    block = state_aggregate._coupling_state(fanin_status=None)
    assert block["persisted"] == "shm_ring"
    assert block["content_bridge"] == "dac_content_ring"
    assert block["intent_coherent"] is True


def test_coupling_state_reports_the_marker_beside_a_bridge_as_incoherent(
    monkeypatch, tmp_path
):
    """The pair outputd refuses is neither content source, and not coherent.

    This is the live shape a bonded member lands in when its grouping layer
    fails to clear the bridge `jasper-fanin-coupling-auto` writes (ADR-0220).
    """
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(
        "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n", encoding="utf-8"
    )
    grouping_env = tmp_path / "grouping-outputd.env"
    grouping_env.write_text(
        "JASPER_OUTPUTD_DAC_CONTENT_LANE=1\n", encoding="utf-8"
    )
    _pin_fanin_env(monkeypatch, tmp_path, "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd_env)
    )
    monkeypatch.setattr(
        "jasper.multiroom.reconcile.OUTPUTD_GROUPING_ENV_FILE", str(grouping_env)
    )

    block = state_aggregate._coupling_state(fanin_status=None)
    assert block["content_bridge"] == "contradicted"
    assert block["intent_coherent"] is False


def test_coupling_state_partial_flip_reports_incoherent(monkeypatch, tmp_path):
    # shm_ring fan-in but direct outputd = partial flip -> coherent False. The
    # token has to be STATED: an absent key is the ring, so a partial flip is
    # now something an operator (or a stale env file) declared.
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("JASPER_OUTPUTD_CONTENT_BRIDGE=direct\n")
    _pin_fanin_env(monkeypatch, tmp_path, "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n")
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH", str(outputd_env)
    )
    block = state_aggregate._coupling_state(fanin_status=None)
    assert block["persisted"] == "shm_ring"
    assert block["content_bridge"] == "direct"
    assert block["intent_coherent"] is False


def test_coupling_state_fail_soft_reports_the_unknown_rather_than_a_default(
    monkeypatch,
):
    """A read failure is fail-SOFT, but it is not a diagnosis.

    It used to degrade to ``"persisted": "loopback"`` with
    ``"intent_coherent": True`` — after ADR-0100 that named the RETIRED
    transport and called it healthy, so an unreadable env file surfaced as a
    working speaker. ``None`` says what actually happened. A MISSING file is
    not this case (undeclared is the ring); an UNREADABLE one is.
    """
    from pathlib import Path

    def _boom(*a, **k):
        raise PermissionError("boom")

    monkeypatch.setattr(Path, "read_text", _boom)
    block = state_aggregate._coupling_state(fanin_status=None)
    assert block == {
        "persisted": None,
        "content_bridge": None,
        "intent_coherent": None,
        "live_transport": None,
        "observed": {"ring_a": None, "ring_b": None},
        "combo": {"state": "disarmed"},
    }


def test_coupling_state_publishes_no_operator_pin(monkeypatch, tmp_path):
    """The block stopped carrying `choice`, and the reason is not tidiness.

    That key reported whether the coupling was an operator pick or the auto
    default, read from JASPER_FANIN_COUPLING_CHOICE. ADR-0100 left one transport
    and the endpoint convergence stopped honouring the marker, so a box whose
    fanin.env still carries `operator` was being shown a pin that nothing in the
    system obeys — a surface telling the household something untrue about who
    controls their speaker. The marker file may survive on a deployed box, which
    is exactly why this is driven with one present.
    """
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        "JASPER_FANIN_COUPLING_CHOICE=operator\n"
        "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n"
    )
    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: "shm_ring",
    )
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH",
        str(tmp_path / "nope.env"),
    )
    block = state_aggregate._coupling_state(fanin_status=None)
    assert "choice" not in block
    assert block["persisted"] == "shm_ring"


# ---- combo resolved state --------------------------------------------------


def _patch_coupling_reads(monkeypatch, tmp_path, *, armed=False):
    """Neutralize the ring/coupling reads so a _coupling_state() call exercises
    only the combo sub-block. Writes a fanin.env with (un)armed combo."""
    from jasper.fanin import coupling_auto as ca

    monkeypatch.setattr(
        "jasper.fanin.ring_health.read_persisted_coupling",
        lambda *a, **k: "loopback",
    )
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text(
        f"{ca.USB_DIRECT_ENV_VAR}={ca.USB_COMBO_ENABLED_VALUE}\n" if armed else ""
    )
    monkeypatch.setattr(
        "jasper.fanin.ring_health.FANIN_ENV_PATH", str(fanin_env)
    )
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.OUTPUTD_ENV_PATH",
        str(tmp_path / "outputd.env"),
    )


def test_combo_state_armed(monkeypatch, tmp_path):
    _patch_coupling_reads(monkeypatch, tmp_path, armed=True)
    block = state_aggregate._coupling_state(fanin_status=None)
    assert block["combo"] == {"state": "armed"}


def test_combo_state_disarmed(monkeypatch, tmp_path):
    _patch_coupling_reads(monkeypatch, tmp_path, armed=False)
    block = state_aggregate._coupling_state(fanin_status=None)
    assert block["combo"] == {"state": "disarmed"}
