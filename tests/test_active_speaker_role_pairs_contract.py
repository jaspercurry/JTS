# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from jasper.active_speaker.crossover_preview import CROSSOVER_PREVIEW_KIND
from jasper.active_speaker.preset_binding import compile_preset_from_crossover_preview
from jasper.speaker_layout import ADJACENT_PAIRS_BY_MAIN_MODE
from tests.active_speaker_fixtures import mono_output_topology


def test_active_crossover_role_pairs_cover_supported_topologies() -> None:
    assert ADJACENT_PAIRS_BY_MAIN_MODE == {
        "full_range_passive": (),
        "active_2_way": (("woofer", "tweeter"),),
        "active_3_way": (("woofer", "mid"), ("mid", "tweeter")),
    }


def test_one_way_staging_refuses_before_requiring_crossover_pairs() -> None:
    topology = mono_output_topology(mode="full_range_passive")
    preview = {
        "kind": CROSSOVER_PREVIEW_KIND, "status": "ready_for_protected_staging",
        "source": {"topology_id": topology.topology_id},
        "groups": [{"group_id": "mono", "kind": "mono", "mode": "full_range_passive", "crossovers": []}],
    }
    preset, issues, _ = compile_preset_from_crossover_preview(topology, preview)
    assert preset is None
    assert [issue["code"] for issue in issues] == ["crossover_preview_single_active_mode_required"]
