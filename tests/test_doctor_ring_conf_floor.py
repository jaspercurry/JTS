# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The ring-conf floor-render doctor check (PR-6).

``check_ring_conf_floor_render`` compares two facts, each read from its owner:
the active DAC profile's DECLARED ``LatencyFloor`` (the DAC registry) and the
``period_frames`` the ring conf.d pins (the file). The ring slot IS one outputd
DAC period, so a box whose DAC declares a floor should have that floor rendered
into its conf.d by ``jasper-audio-hardware-reconcile``; this check is the
standing surface that catches a box where that render did not land.

A DAC with no declared floor is ``ok`` by RULE, not by luck — the shipped
conf.d default stands and there is nothing to render.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.audio_hardware.dac import (
    INNOMAKER_HIFI_AMP_PRO_ID,
    LatencyFloor,
    latency_floor_for,
)
from jasper.cli.doctor import audio_runtime as audio
from jasper.output_topology import OutputTopologyError

SHIPPED_RING_CONF = (
    Path(__file__).resolve().parents[1]
    / "deploy" / "alsa" / "conf.d" / "60-jts-ring.conf"
)

# A registered profile that declares NO LatencyFloor, so the no-floor branches
# below exercise the real registry rather than a synthetic id. Asserted rather
# than assumed: declaring a floor for this profile must fail THIS line, not
# silently turn the two no-floor tests into vacuous passes against a branch
# they no longer reach. (That is exactly what a declared DAC8x floor did to
# them in R7a, when they were written against `hifiberry_dac8x`.)
NO_FLOOR_DAC_ID = INNOMAKER_HIFI_AMP_PRO_ID
assert latency_floor_for(NO_FLOOR_DAC_ID) is None, (
    f"{NO_FLOOR_DAC_ID} now declares a latency floor; pick another floorless "
    "profile for the no-floor doctor branches"
)


def _stage(monkeypatch, tmp_path, *, dac_id, conf_text=None):
    conf = tmp_path / "60-jts-ring.conf"
    if conf_text is None:
        conf.write_bytes(SHIPPED_RING_CONF.read_bytes())
    else:
        conf.write_text(conf_text, encoding="utf-8")
    monkeypatch.setattr(audio, "_JTS_RING_CONF_D", str(conf))
    monkeypatch.setattr(audio, "_active_audio_dac_id", lambda: dac_id)
    return conf


def _conf_text(period_frames):
    return (
        f"pcm.jts_ring_capture {{\n    period_frames {period_frames}\n"
        "    n_slots 2\n}\n"
        f"pcm.jts_ring_playback {{\n    period_frames {period_frames}\n"
        "    n_slots 2\n}\n"
    )


def _synthetic_floor(period_frames):
    return LatencyFloor(
        camilla_chunksize=256,
        camilla_target_level=1536,
        outputd_period_frames=period_frames,
        outputd_dac_buffer_frames=4 * period_frames,
    )


def test_ok_when_the_dac_declares_no_floor(monkeypatch, tmp_path):
    # State 1: nothing to render — the shipped default stands by rule.
    _stage(monkeypatch, tmp_path, dac_id=NO_FLOOR_DAC_ID)

    result = audio.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "no latency floor" in result.detail
    assert "shipped default" in result.detail


def test_ok_when_the_conf_matches_the_declared_floor(monkeypatch, tmp_path):
    # State 2: the golden Apple case — the declared floor IS the shipped 128.
    _stage(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    result = audio.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "128" in result.detail
    assert "apple_usb_c_dongle" in result.detail


def test_ok_when_the_floor_exceeds_the_fixed_ring_slot(monkeypatch, tmp_path):
    # State 4, the product boundary: Ring A's slot is fan-in's COMPILE-TIME
    # RING_SLOT_FRAMES, so a DAC whose floor is not exactly that never gets a
    # rendered conf.d — shm_ring is simply unavailable on it until #2147.
    # That is a documented boundary, NOT drift, so it reports ok.
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    monkeypatch.setattr(
        audio,
        "latency_floor_for",
        lambda _id: _synthetic_floor(2 * RING_SLOT_FRAMES),
    )
    _stage(monkeypatch, tmp_path, dac_id="hifiberry_dac8x")

    result = audio.check_ring_conf_floor_render()

    assert result.status == "ok"
    # Honest: names both numbers, says what is unavailable and what still works.
    assert str(2 * RING_SLOT_FRAMES) in result.detail
    assert str(RING_SLOT_FRAMES) in result.detail
    assert "shm_ring" in result.detail
    assert "loopback" in result.detail.lower()
    assert "#2147" in result.detail


def test_warns_when_the_conf_diverges_from_the_declared_floor(
    monkeypatch, tmp_path
):
    # State 5, real drift: the Apple dongle's floor IS renderable (it equals
    # RING_SLOT_FRAMES) but this box's conf.d was never rendered to it. Warn,
    # not fail — the conf.d is inert unless shm_ring is armed, and the coupling
    # reconciler independently fail-closes to loopback on this mismatch.
    _stage(
        monkeypatch,
        tmp_path,
        dac_id="apple_usb_c_dongle",
        conf_text=_conf_text(1024),
    )

    result = audio.check_ring_conf_floor_render()

    assert result.status == "warn"
    # Names BOTH numbers and the remedy, not a bare "mismatch".
    assert "128" in result.detail
    assert "1024" in result.detail
    assert "jasper-audio-hardware-reconcile" in result.detail


@pytest.mark.parametrize(
    "conf_text",
    [
        # Torn: the two PCMs disagree, so there is no single geometry.
        "pcm.jts_ring_capture {\n    period_frames 128\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 1024\n}\n",
        # No period_frames line at all.
        "pcm.jts_ring_capture { type jts_ring }\n",
    ],
)
def test_warns_when_the_conf_period_is_indeterminate(
    monkeypatch, tmp_path, conf_text
):
    _stage(
        monkeypatch, tmp_path, dac_id="apple_usb_c_dongle", conf_text=conf_text
    )

    result = audio.check_ring_conf_floor_render()

    assert result.status == "warn"
    assert "no single period_frames" in result.detail
    assert "redeploy" in result.detail


def test_warns_when_the_conf_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(audio, "_JTS_RING_CONF_D", str(tmp_path / "missing.conf"))
    monkeypatch.setattr(audio, "_active_audio_dac_id", lambda: "apple_usb_c_dongle")

    result = audio.check_ring_conf_floor_render()

    assert result.status == "warn"
    assert "absent or torn" in result.detail


def test_check_is_registered_in_the_audio_doctor_group():
    from jasper.cli.doctor import audio as audio_group

    assert (
        audio_group.check_ring_conf_floor_render
        is audio.check_ring_conf_floor_render
    )
    assert "check_ring_conf_floor_render" in audio_group.__all__


# --- #2294: a floor-blocked box must SAY it cannot ring -----------------------


def test_no_floor_detail_names_the_resolved_ring_ineligibility(monkeypatch, tmp_path):
    """`ok` is right, silence was not (issue #2294).

    A DAC with no declared floor leaves outputd on its default period, which can
    never equal the fixed ring slot, so the arm preflight refuses shm_ring. The
    conf.d is correct either way — hence `ok` — but the household's actual
    question is "why is this box on loopback?", and before this the answer
    appeared on no surface. The InnoMaker HiFi AMP Pro is the live case.
    """
    from jasper.audio_runtime_plan import DEFAULT_OUTPUTD_PERIOD_FRAMES
    from jasper.fanin_coupling import RING_SLOT_FRAMES

    _stage(monkeypatch, tmp_path, dac_id=NO_FLOOR_DAC_ID)

    result = audio.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert str(DEFAULT_OUTPUTD_PERIOD_FRAMES) in result.detail
    assert str(RING_SLOT_FRAMES) in result.detail
    assert "shm_ring is unavailable" in result.detail
    assert "#2147" in result.detail


def test_a_matching_floor_does_not_claim_ineligibility(monkeypatch, tmp_path):
    # The mirror of the above: a box that CAN ring must not be told it cannot.
    _stage(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    result = audio.check_ring_conf_floor_render()

    assert result.status == "ok"
    assert "unavailable" not in result.detail


# --- `_requires_roleful_graph` fail-soft DIRECTION ----------------------------


@pytest.mark.parametrize(
    "exc",
    [
        # The real-world case first. It subclasses ValueError, so the bare
        # ValueError below is the same except-arm — named separately because a
        # reader should not have to know the hierarchy to see it is covered.
        OutputTopologyError("topology has an unsupported shape"),
        OSError("topology unreadable"),
        ValueError("topology malformed"),
    ],
)
def test_an_unreadable_topology_fails_soft_to_not_roleful(monkeypatch, tmp_path, exc):
    """The documented direction, pinned — an unreadable topology asserts NOTHING.

    ``_requires_roleful_graph`` only ever SOFTENS a message or adds an
    eligibility sentence; it gates nothing, and every caller that acts on
    rolefulness reads the fail-CLOSED loaders instead. So its ``except`` arm must
    return False: a box whose topology cannot be read must keep the generic
    wording rather than be told, on no evidence, that it is an active-crossover
    box whose ring is explicit-arm-only. Failing soft to True would print a
    remediation ladder at every box with a torn topology file.

    Asserted at BOTH surfaces — the helper's own answer and the detail string it
    feeds — so flipping the arm cannot pass by only breaking the private half.
    """
    import jasper.output_topology as output_topology

    def _raise(*_a, **_kw):
        raise exc

    monkeypatch.setattr(output_topology, "load_output_topology_strict", _raise)
    _stage(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    assert audio._requires_roleful_graph() is False
    assert "ROLEFUL" not in audio.check_ring_conf_floor_render().detail


def test_a_roleful_topology_is_reported_roleful(monkeypatch, tmp_path):
    """The other direction, so the fail-soft pin cannot be satisfied by a stub.

    Without this, ``return False`` unconditionally would pass the test above and
    the helper would be pinned to a constant. This runs the real classifier over
    a real roleful topology.
    """
    import jasper.output_topology as output_topology
    from tests.test_active_speaker_runtime_contract import _active_topology

    topology = _active_topology("mono", "active_2_way")
    monkeypatch.setattr(
        output_topology, "load_output_topology_strict", lambda *a, **kw: topology
    )
    _stage(monkeypatch, tmp_path, dac_id="apple_usb_c_dongle")

    assert audio._requires_roleful_graph() is True
    assert "ROLEFUL" in audio.check_ring_conf_floor_render().detail
