# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""JASPER_CROSSOVER_FLOW selector (v2 conductor, Wave 5a).

Pins the W5a contract from
docs/crossover-measurement-productization-design.md §6: the selector defaults
to ``legacy`` and, with any non-``v2`` value, ``build_crossover_envelope``
behaves BYTE-IDENTICALLY to today (schema 6, same payload); only the exact
literal ``v2`` dispatches to the schema-7 conductor envelope. Fail-safe: a
typo can never silently activate the unvalidated v2 path.
"""
from __future__ import annotations

import pytest

from jasper.active_speaker import crossover_envelope_v2
from jasper.active_speaker.crossover_envelope import build_crossover_envelope
from jasper.active_speaker.crossover_flow import (
    CROSSOVER_FLOW_ENV,
    CROSSOVER_FLOW_LEGACY,
    CROSSOVER_FLOW_V2,
    active_crossover_flow,
    resolve_crossover_flow,
)


# Representative statuses spanning distinct legacy envelope branches: passive,
# volume recovery, the setup gate, and two active walks (default + a manual
# applied profile). The deeper structural guarantee is that the ONLY v2 touch
# inside build_crossover_envelope is an 11-line early-return guard clause —
# with the selector resolving legacy, execution falls through to the
# unmodified legacy body, so these samples pin the dispatch, and the full
# legacy suite (tests/test_web_correction_crossover_flow.py) pins the body.
def _statuses() -> list[dict]:
    return [
        {"active": False},
        {
            "active": True,
            "level_match": {
                "unresolved_volume_safety": {"status": "unresolved"},
            },
        },
        {
            "active": True,
            "setup": {"active": True, "status": "blocked"},
            "targets": {"drivers": []},
            "measurements": {},
            "level_match": {},
        },
        {
            "active": True,
            "setup": {"active": True, "status": "ready"},
            "targets": {"drivers": []},
            "measurements": {},
            "level_match": {},
        },
        {
            "active": True,
            "setup": {
                "active": True,
                "status": "ready",
                "applied_crossover": {"valid": True, "owner": "manual"},
                "manual_preservation": {"ready": True},
            },
            "targets": {"drivers": []},
            "measurements": {},
            "level_match": {},
        },
    ]


# --- resolver ----------------------------------------------------------------


def test_selector_defaults_to_legacy():
    assert active_crossover_flow({}) == CROSSOVER_FLOW_LEGACY


@pytest.mark.parametrize(
    "value", ["", "legacy", "LEGACY", "bogus", "1", "true", "v3", "conductor"]
)
def test_selector_rejects_everything_but_v2(value):
    assert active_crossover_flow({CROSSOVER_FLOW_ENV: value}) == CROSSOVER_FLOW_LEGACY


@pytest.mark.parametrize("value", ["v2", "V2", "  v2  "])
def test_selector_accepts_v2_literal(value):
    assert active_crossover_flow({CROSSOVER_FLOW_ENV: value}) == CROSSOVER_FLOW_V2


def test_selector_reads_process_env(monkeypatch):
    monkeypatch.delenv(CROSSOVER_FLOW_ENV, raising=False)
    assert active_crossover_flow() == CROSSOVER_FLOW_LEGACY
    monkeypatch.setenv(CROSSOVER_FLOW_ENV, "v2")
    assert active_crossover_flow() == CROSSOVER_FLOW_V2


def test_status_override_wins_and_invalid_override_is_ignored(monkeypatch):
    monkeypatch.setenv(CROSSOVER_FLOW_ENV, "v2")
    assert resolve_crossover_flow({"crossover_flow": "legacy"}) == CROSSOVER_FLOW_LEGACY
    monkeypatch.delenv(CROSSOVER_FLOW_ENV, raising=False)
    assert resolve_crossover_flow({"crossover_flow": "v2"}) == CROSSOVER_FLOW_V2
    # Invalid override falls back to the env-resolved flow (fail-safe).
    assert resolve_crossover_flow({"crossover_flow": "bogus"}) == CROSSOVER_FLOW_LEGACY
    assert resolve_crossover_flow({"crossover_flow": 42}) == CROSSOVER_FLOW_LEGACY


# --- envelope dispatch byte-identity ------------------------------------------


def test_legacy_envelope_is_byte_identical_across_non_v2_values(monkeypatch):
    """Every non-v2 selector value produces the exact same schema-6 payload."""
    monkeypatch.delenv(CROSSOVER_FLOW_ENV, raising=False)
    baseline = [build_crossover_envelope(status) for status in _statuses()]
    assert all(env["schema_version"] == 6 for env in baseline)
    for value in ("", "legacy", "bogus", "true"):
        monkeypatch.setenv(CROSSOVER_FLOW_ENV, value)
        assert [build_crossover_envelope(s) for s in _statuses()] == baseline


def test_legacy_never_calls_the_v2_renderer(monkeypatch):
    monkeypatch.delenv(CROSSOVER_FLOW_ENV, raising=False)

    def _boom(status):  # pragma: no cover - would fail the test if reached
        raise AssertionError("v2 renderer called on the legacy path")

    monkeypatch.setattr(crossover_envelope_v2, "build_crossover_envelope_v2", _boom)
    for status in _statuses():
        assert build_crossover_envelope(status)["schema_version"] == 6


def test_v2_selector_dispatches_to_schema_7(monkeypatch):
    monkeypatch.setenv(CROSSOVER_FLOW_ENV, "v2")
    for status in _statuses():
        assert build_crossover_envelope(status)["schema_version"] == 7


def test_env_example_seeds_legacy_default():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    lines = [
        line.strip() for line in text.splitlines()
        if line.strip().startswith("JASPER_CROSSOVER_FLOW=")
    ]
    assert lines == ["JASPER_CROSSOVER_FLOW=legacy"]
