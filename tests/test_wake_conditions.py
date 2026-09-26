# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the wake-condition taxonomy single source of truth."""
from __future__ import annotations

from jasper.wake_conditions import (
    CONDITIONS,
    CORPUS_DIR_BY_CONDITION,
    CORPUS_DIR_CONDITIONS,
    DISTANCES,
)


def test_conditions_taxonomy():
    # Pins the taxonomy the corpus tool and telemetry all
    # bind to. Changing this is a deliberate act with data implications
    # (see the module's stability contract), so it should fail loudly here.
    assert CONDITIONS == ("quiet", "ambient", "music")
    assert DISTANCES == ("near", "mid", "far")


def test_corpus_directory_condition_encoding():
    # The semantic label and on-disk token intentionally differ for quiet:
    # existing enrollment/extractor corpora use the frozen ``nomusic`` name.
    assert CORPUS_DIR_BY_CONDITION == {
        "quiet": "nomusic",
        "ambient": "ambient",
        "music": "music",
    }
    assert CORPUS_DIR_CONDITIONS == ("nomusic", "ambient", "music")
