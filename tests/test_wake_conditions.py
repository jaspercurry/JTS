# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the wake-condition taxonomy single source of truth."""
from __future__ import annotations

from jasper.wake_conditions import (
    CONDITIONS,
    CORPUS_DIR_BY_CONDITION,
    CORPUS_DIR_CONDITIONS,
    DEFAULT_CONDITION,
    DISTANCES,
    normalize_condition,
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


def test_default_condition_is_a_real_condition():
    # The fallback must itself be a valid condition (and the base one, which
    # is the safe fallback.
    assert DEFAULT_CONDITION in CONDITIONS


def test_normalize_condition_passes_known_values():
    for c in CONDITIONS:
        assert normalize_condition(c) == c


def test_normalize_condition_tolerates_unknown_and_none():
    # The forward-compat / historical-drift guarantee: an unknown label (a
    # future taxonomy, a renamed/retired condition, None) must never raise —
    # it resolves to the base condition so consumers can't crash on it.
    assert normalize_condition("tv") == DEFAULT_CONDITION
    assert normalize_condition("") == DEFAULT_CONDITION
    assert normalize_condition(None) == DEFAULT_CONDITION
