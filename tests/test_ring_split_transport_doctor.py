"""The ACTIVE-ring split-transport doctor check (#2285 P2, design §10.3).

The state under test: the loaded CamillaDSP graph names the ACTIVE ring while
the persisted coupling is ``loopback``. Nothing consumes the ring, so the
speaker is silent while every daemon is healthy.

These tests exist because the deletion of the aloop ACTIVE endpoint made that
state QUIETER: it used to fail CamillaDSP's load (the PCM was deleted by
#2534) and park loudly; now the graph loads fine. The check is the
compensation, and each conjunct is pinned separately — a two-term conjunction
passes a single-conjunct test while still being wrong.
"""

from __future__ import annotations

import pytest

from jasper.cli.doctor import audio_runtime as ar
from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE, RING_PLAYBACK_DEVICE

SHM_RING = "shm_ring"
LOOPBACK = "loopback"


def _arrange(monkeypatch, *, coupling: str, playback_device: str | None) -> None:
    """Put the box in one (coupling, loaded-graph-playback) combination."""
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda: coupling,
    )
    monkeypatch.setattr(
        ar, "_active_camilla_config_path", lambda: ("/state.yml", "/loaded.yml")
    )
    monkeypatch.setattr(ar, "_loaded_playback_device", lambda _p: playback_device)


def test_the_split_state_fails_loudly(monkeypatch) -> None:
    """graph@ring AND coupling=loopback is the silent split — it must FAIL."""
    _arrange(monkeypatch, coupling=LOOPBACK, playback_device=RING_ACTIVE_PLAYBACK_DEVICE)

    result = ar.check_active_ring_split_transport()

    assert result.status == "fail", result
    # The remedy must be the EXPLICIT ARM command (the §4.2 mode-split
    # contract), not a rollback: there is no longer a rollback direction, and
    # for a roleful box `loopback` is the park rather than a destination.
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail
    assert "loopback" not in result.detail.split("Complete the arm:")[1]


def test_the_known_mid_arm_transient_is_disclosed_to_the_operator(monkeypatch) -> None:
    """The arm ladder moves the graph first, so this can FAIL mid-ladder.

    That is the ladder working. The detail has to say so, or an operator
    running doctor during an arm reads a correct FAIL as a fault.
    """
    _arrange(monkeypatch, coupling=LOOPBACK, playback_device=RING_ACTIVE_PLAYBACK_DEVICE)

    detail = ar.check_active_ring_split_transport().detail.lower()

    assert "transient" in detail
    assert "authoritative" in detail


# --- Per-conjunct pins. A two-term conjunction passes a single-conjunct test
# --- while still being wrong, so each term gets its own negative case.


def test_conjunct_one_the_coupling_term_alone_does_not_fire(monkeypatch) -> None:
    """coupling=loopback with a NON-ring graph is an ordinary loopback box."""
    _arrange(monkeypatch, coupling=LOOPBACK, playback_device="outputd_content_playback")

    assert ar.check_active_ring_split_transport().status == "ok"


def test_conjunct_two_the_graph_term_alone_does_not_fire(monkeypatch) -> None:
    """graph@ring with coupling=shm_ring is a correctly ARMED box."""
    _arrange(
        monkeypatch, coupling=SHM_RING, playback_device=RING_ACTIVE_PLAYBACK_DEVICE
    )

    assert ar.check_active_ring_split_transport().status == "ok"


def test_the_stereo_ring_is_not_the_active_ring(monkeypatch) -> None:
    """Only the ACTIVE ring is this check's subject.

    A flat box's graph on the stereo ring under loopback is a different
    condition owned by ``check_fanin_coupling``; claiming it here would report
    one fault twice and send a flat box the roleful arm remedy.
    """
    _arrange(monkeypatch, coupling=LOOPBACK, playback_device=RING_PLAYBACK_DEVICE)

    assert ar.check_active_ring_split_transport().status == "ok"


@pytest.mark.parametrize("missing", [None, ""])
def test_an_unreadable_graph_does_not_manufacture_a_fault(monkeypatch, missing) -> None:
    """No loaded playback device is no evidence — never a FAIL.

    Fail-closed applies to arming, not to diagnosis: inventing a split from an
    absent reading would make a fresh or non-JTS box report a silent speaker it
    does not have.
    """
    _arrange(monkeypatch, coupling=LOOPBACK, playback_device=missing)

    assert ar.check_active_ring_split_transport().status == "ok"
