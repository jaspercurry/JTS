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


def _write_pair(tmp_path, name: str, playback_device: str | None):
    """Write a real statefile + the CamillaDSP config it points at.

    Returns the statefile path. ``playback_device=None`` writes a config with no
    ``playback`` device key at all — the program-bake / parked shape an active
    leader really keeps in its primary statefile.
    """
    config = tmp_path / f"{name}-config.yml"
    playback = (
        f"  playback:\n    type: Alsa\n    device: {playback_device}\n"
        if playback_device
        else "  playback:\n    type: File\n    filename: /dev/null\n"
    )
    config.write_text(
        "devices:\n"
        "  samplerate: 48000\n"
        "  chunksize: 1024\n"
        "  capture:\n    type: Alsa\n    device: jts_ring_capture\n"
        + playback,
        encoding="utf-8",
    )
    statefile = tmp_path / f"{name}-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    return statefile


def _arrange(
    monkeypatch,
    tmp_path,
    *,
    coupling: str,
    playback_device: str | None,
    crossover_playback_device: str | None = None,
    primary_config_missing: bool = False,
) -> None:
    """Put the box in one (coupling, loaded-graph-playback) combination.

    REAL STATEFILES ON DISK, not stubs, and that is the point of this harness.
    The check resolves its evidence through BOTH the primary and the camilla#2
    crossover statefile (first recognized endpoint wins). Stubbing the resolved
    device would let the two halves of that union drift apart without any test
    noticing — which is exactly how the pre-#2285 gap between this check and
    `check_outputd_service`'s transport note survived unseen.
    """
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda: coupling,
    )
    primary = _write_pair(tmp_path, "primary", playback_device)
    if primary_config_missing:
        # The statefile parses but names a config that is gone — evidence
        # carries an error string and the primary contributes nothing.
        primary.write_text(
            f"config_path: {tmp_path / 'deleted-config.yml'}\n", encoding="utf-8"
        )
    crossover = _write_pair(tmp_path, "crossover", crossover_playback_device)
    monkeypatch.setattr(
        ar, "_active_camilla_config_path", lambda: (str(primary), None)
    )
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_CAMILLA2_STATEFILE_PATH", str(crossover)
    )


def test_the_split_state_fails_loudly(monkeypatch, tmp_path) -> None:
    """graph@ring AND coupling=loopback is the silent split — it must FAIL."""
    _arrange(monkeypatch, tmp_path, coupling=LOOPBACK, playback_device=RING_ACTIVE_PLAYBACK_DEVICE)

    result = ar.check_active_ring_split_transport()

    assert result.status == "fail", result
    # The remedy must be the EXPLICIT ARM command (the §4.2 mode-split
    # contract), not a rollback: there is no longer a rollback direction, and
    # for a roleful box `loopback` is the park rather than a destination.
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail
    assert "loopback" not in result.detail.split("Complete the arm:")[1]


def test_the_known_mid_arm_transient_is_disclosed_to_the_operator(
    monkeypatch, tmp_path
) -> None:
    """The arm ladder moves the graph first, so this can FAIL mid-ladder.

    That is the ladder working. The detail has to say so, or an operator
    running doctor during an arm reads a correct FAIL as a fault.
    """
    _arrange(monkeypatch, tmp_path, coupling=LOOPBACK, playback_device=RING_ACTIVE_PLAYBACK_DEVICE)

    detail = ar.check_active_ring_split_transport().detail.lower()

    assert "transient" in detail
    assert "authoritative" in detail


# --- Per-conjunct pins. A two-term conjunction passes a single-conjunct test
# --- while still being wrong, so each term gets its own negative case.


def test_conjunct_one_the_coupling_term_alone_does_not_fire(monkeypatch, tmp_path) -> None:
    """coupling=loopback with a NON-ring graph is an ordinary loopback box."""
    _arrange(monkeypatch, tmp_path, coupling=LOOPBACK, playback_device="outputd_content_playback")

    assert ar.check_active_ring_split_transport().status == "ok"


def test_conjunct_two_the_graph_term_alone_does_not_fire(monkeypatch, tmp_path) -> None:
    """graph@ring with coupling=shm_ring is a correctly ARMED box."""
    _arrange(
        monkeypatch, tmp_path, coupling=SHM_RING,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )

    assert ar.check_active_ring_split_transport().status == "ok"


def test_the_stereo_ring_is_not_the_active_ring(monkeypatch, tmp_path) -> None:
    """Only the ACTIVE ring is this check's subject.

    A flat box's graph on the stereo ring under loopback is a different
    condition owned by ``check_fanin_coupling``; claiming it here would report
    one fault twice and send a flat box the roleful arm remedy.
    """
    _arrange(monkeypatch, tmp_path, coupling=LOOPBACK, playback_device=RING_PLAYBACK_DEVICE)

    assert ar.check_active_ring_split_transport().status == "ok"


@pytest.mark.parametrize("missing", [None, ""])
def test_an_unreadable_graph_does_not_manufacture_a_fault(
    monkeypatch, tmp_path, missing
) -> None:
    """No loaded playback device is no evidence — never a FAIL.

    Fail-closed applies to arming, not to diagnosis: inventing a split from an
    absent reading would make a fresh or non-JTS box report a silent speaker it
    does not have.
    """
    _arrange(monkeypatch, tmp_path, coupling=LOOPBACK, playback_device=missing)

    assert ar.check_active_ring_split_transport().status == "ok"


# --- The union of BOTH statefiles (#2285 panel finding C1). These two shapes
# --- were measured going quiet from every doctor surface: the folded-away
# --- `check_outputd_service` transport note saw them (it read both statefiles),
# --- and this check did not (it read only the primary). The de-duplication was
# --- only correct once the survivor read what the deleted branch read.


def test_a_program_bake_in_the_primary_does_not_hide_the_ring_in_camilla2(
    monkeypatch, tmp_path
) -> None:
    """An active leader keeps a bake in the primary and its endpoint in camilla#2.

    The primary's graph names no registered output endpoint at all (a File sink
    — the program-bake / parked shape), so reading only the primary answers
    "(none)" and the box reads healthy while camilla#2 writes the ACTIVE ring
    under a loopback coupling: silent, with every daemon green.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        coupling=LOOPBACK,
        playback_device=None,
        crossover_playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )

    result = ar.check_active_ring_split_transport()

    assert result.status == "fail", result
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail


def test_a_primary_statefile_pointing_at_a_deleted_config_does_not_hide_the_split(
    monkeypatch, tmp_path
) -> None:
    """The primary parses but its config is gone — evidence errors, not absence.

    The reader records "devices unavailable" for the primary and falls through
    to camilla#2, which names the ACTIVE ring. Before the union this returned
    the hardcoded `sound_current.yml` rescue's answer and went quiet.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        coupling=LOOPBACK,
        playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        crossover_playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
        primary_config_missing=True,
    )

    assert ar.check_active_ring_split_transport().status == "fail"


def test_camilla2_evidence_still_respects_the_coupling_term(
    monkeypatch, tmp_path
) -> None:
    """The union widens the GRAPH term only — the conjunction still holds.

    Without this, widening the reader could have turned the check into a
    one-term alarm that reds every correctly-armed box.
    """
    _arrange(
        monkeypatch,
        tmp_path,
        coupling=SHM_RING,
        playback_device=None,
        crossover_playback_device=RING_ACTIVE_PLAYBACK_DEVICE,
    )

    assert ar.check_active_ring_split_transport().status == "ok"


# ---------------------------------------------------------------------------
# The ENDPOINT rung — check_active_ring_path_projection.
#
# The sibling above owns the graph rung and returns ok the moment the coupling
# is `shm_ring`. These pin the other side of that partition: under the ring
# coupling, the ring PATH lagging its endpoint MARKER.
# ---------------------------------------------------------------------------


def _arrange_projection(monkeypatch, tmp_path, *, coupling: str, env_lines: str):
    """Put outputd.env and the persisted coupling on disk for the projection check.

    A real file, not a stubbed mapping: the check reads persisted evidence
    precisely BECAUSE outputd is not running in its target state, so what is
    under test includes the read itself.
    """
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda: coupling,
    )
    env = tmp_path / "outputd.env"
    env.write_text(env_lines, encoding="utf-8")
    monkeypatch.setattr(
        "jasper.audio_runtime_plan.DEFAULT_OUTPUTD_ENV_PATH", str(env)
    )


@pytest.mark.parametrize(
    "marker,carried,derived",
    [
        # The first-arm lag (jts.local, 2026-08-21): marker armed, path Ring B.
        ("1", "/dev/shm/jts-ring/content.ring", "/dev/shm/jts-ring/active-content.ring"),
        # The disarm lag: marker cleared, path still the active ring.
        ("", "/dev/shm/jts-ring/active-content.ring", "/dev/shm/jts-ring/content.ring"),
    ],
)
def test_a_ring_path_lagging_its_marker_fails_with_the_runnable_remedy(
    monkeypatch, tmp_path, marker, carried, derived
) -> None:
    """The waypoint must produce a doctor line, and it must be actionable.

    This is the state ``check_outputd_service`` structurally cannot report:
    outputd refuses the crossed pair at startup, so that check returns its
    systemd failure before ever reaching the transport comparison. Nothing owned
    the finding, and the operator got "the unit is not running" with no cause.
    """
    _arrange_projection(
        monkeypatch,
        tmp_path,
        coupling=SHM_RING,
        env_lines=(
            "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n"
            f"JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT={marker}\n"
            f"JASPER_OUTPUTD_SHM_RING_PATH={carried}\n"
        ),
    )

    result = ar.check_active_ring_path_projection()

    assert result.status == "fail", result
    assert carried in result.detail
    assert derived in result.detail
    # Runnable, and the same pass the ladder's own next step runs.
    assert "jasper-fanin-coupling-reconcile shm_ring" in result.detail


def test_a_converged_ring_pair_is_ok(monkeypatch, tmp_path) -> None:
    """The negative control: an armed box on the active ring must not red."""
    _arrange_projection(
        monkeypatch,
        tmp_path,
        coupling=SHM_RING,
        env_lines=(
            "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n"
            "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1\n"
            "JASPER_OUTPUTD_SHM_RING_PATH=/dev/shm/jts-ring/active-content.ring\n"
        ),
    )

    assert ar.check_active_ring_path_projection().status == "ok"


def test_the_two_ladder_checks_partition_the_coupling_space(
    monkeypatch, tmp_path
) -> None:
    """Neither rung is unowned, and neither is double-reported.

    The split check returns ok as soon as the coupling is ``shm_ring``; the
    projection check returns ok as soon as it is not. Pinning the handoff means a
    later edit cannot leave a coupling value that both checks ignore — the gap
    that let the endpoint rung go unowned in the first place.
    """
    _arrange_projection(
        monkeypatch,
        tmp_path,
        coupling=LOOPBACK,
        env_lines=(
            "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1\n"
            "JASPER_OUTPUTD_SHM_RING_PATH=/dev/shm/jts-ring/content.ring\n"
        ),
    )
    # Under loopback outputd runs the `direct` bridge and never reads the ring
    # path, so the projection check stands down — and the split check is the one
    # that owns anything wrong on this side.
    assert ar.check_active_ring_path_projection().status == "ok"

    _arrange(monkeypatch, tmp_path, coupling=SHM_RING, playback_device=RING_PLAYBACK_DEVICE)
    assert ar.check_active_ring_split_transport().status == "ok"
