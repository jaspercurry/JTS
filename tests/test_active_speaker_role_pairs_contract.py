# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One active-crossover role-pair contract feeds draft and preview paths."""

from pathlib import Path
import re

from jasper.active_speaker._common import ACTIVE_CROSSOVER_ROLE_PAIRS


ROOT = Path(__file__).resolve().parents[1]


def test_active_crossover_role_pairs_cover_supported_topologies() -> None:
    assert ACTIVE_CROSSOVER_ROLE_PAIRS == {
        "active_2_way": (("woofer", "tweeter"),),
        "active_3_way": (("woofer", "mid"), ("mid", "tweeter")),
    }


def test_draft_and_preview_import_the_shared_role_pair_contract() -> None:
    for relative in (
        "jasper/active_speaker/design_draft.py",
        "jasper/active_speaker/crossover_preview.py",
    ):
        source = (ROOT / relative).read_text()
        assert source.count("ACTIVE_CROSSOVER_ROLE_PAIRS") == 2
        assert not re.search(r"^_CROSSOVER_ROLE_PAIRS\s*=", source, re.MULTILINE)
        assert not re.search(r"^_ACTIVE_ROLE_PAIRS\s*=", source, re.MULTILINE)
