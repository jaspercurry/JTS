# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring (shm_ring) wire agreement + statefile seeding (P2).

Ring-B eligibility itself is pinned in ``tests/test_output_contract.py``. Here:
  1. the topology side's stereo width agrees with Ring A's declaration.
  2. safe_graph_for_current_topology() re-seeds the RING flat
     config on a ring-armed box, not the loopback flat config — audit finding 5's
     built-in-revert dies here.
"""

from __future__ import annotations

from pathlib import Path

from jasper.active_speaker.output_contract import RING_STEREO_PROGRAM_CHANNELS
from jasper.active_speaker.runtime_contract import safe_graph_for_current_topology
from jasper.sound.camilla_yaml import (
    emit_flat_outputd_cutover_config,
)

# Reuse the topology builders from the main runtime-contract suite.
from tests.test_active_speaker_runtime_contract import (
    _active_topology,
    _full_range_stereo,
    _subwoofer_topology,
)


def test_ring_stereo_program_channels_agrees_with_the_ring_a_declaration():
    """One number, reached from the topology side and from the wire side.

    ``RING_STEREO_PROGRAM_CHANNELS`` (output_contract) and ``RING_A_CHANNELS``
    (jasper.fanin_coupling) are the same fact — the program upstream of
    CamillaDSP is stereo — declared where each side needs it. Pin them equal so
    a change to one is a failing test, not a shear on the wire.
    """
    from jasper.fanin_coupling import RING_A_CHANNELS

    assert RING_STEREO_PROGRAM_CHANNELS == RING_A_CHANNELS


# --- statefile seeding has ONE flat graph -----------------------------------


def test_the_one_flat_graph_is_seeded(tmp_path: Path):
    """ADR-0100: the flat startup graph IS the ring graph.

    It used to pick between a loopback flat config and a ring sibling by the
    persisted coupling; with one transport there is nothing to pick, and no
    declaration left that could seed a box onto a graph its transport cannot
    serve.
    """
    flat = tmp_path / "outputd-cutover.yml"
    emit_flat_outputd_cutover_config(out_path=flat)

    decision = safe_graph_for_current_topology(
        _full_range_stereo(),
        flat_config_path=flat,
    )
    assert decision.status == "select_flat", decision.reason
    assert decision.selected_config_path == str(flat)


def _render_names(tmp_path: Path, topology) -> set[str]:
    from jasper.sound.camilla_yaml import render_flat_cutover_configs

    out = tmp_path / "camilladsp"
    out.mkdir(parents=True)
    render_flat_cutover_configs(config_dir=out, topology=topology)
    return {p.name for p in out.iterdir()}


def test_the_flat_cutover_renders_on_every_topology(tmp_path: Path):
    """One file, rendered whatever the topology — its surplus channels are hard
    muted by the emitter rather than the file being withheld."""
    for factory in (
        _full_range_stereo,
        lambda: _active_topology("stereo", "active_2_way"),
        _subwoofer_topology,
    ):
        names = _render_names(tmp_path / str(id(factory)), factory())
        assert names == {"outputd-cutover.yml"}
