# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One capture-preset resolution contract feeds web analysis surfaces."""

from types import SimpleNamespace

import pytest

from jasper.active_speaker import commission_wiring
from jasper.active_speaker import staging
from jasper.active_speaker import tone_plan


def test_capture_preset_prefers_resolved_preset(monkeypatch) -> None:
    preset = object()
    monkeypatch.setattr(
        commission_wiring,
        "resolve_commission_inputs",
        lambda: (preset, None),
    )

    assert commission_wiring.resolve_capture_preset(object()) is preset


def test_capture_preset_compiles_ready_preview(monkeypatch) -> None:
    topology = object()
    preview = {"status": "ready_for_protected_staging"}
    compiled = object()
    monkeypatch.setattr(
        commission_wiring,
        "resolve_commission_inputs",
        lambda: (None, preview),
    )
    monkeypatch.setattr(
        staging,
        "compile_preset_from_crossover_preview",
        lambda got_topology, got_preview: (
            compiled if (got_topology, got_preview) == (topology, preview) else None,
            [],
            [],
        ),
    )

    assert commission_wiring.resolve_capture_preset(topology) is compiled


def test_capture_preset_reports_first_two_compile_issues(monkeypatch) -> None:
    monkeypatch.setattr(
        commission_wiring,
        "resolve_commission_inputs",
        lambda: (None, {"status": "blocked"}),
    )
    monkeypatch.setattr(
        staging,
        "compile_preset_from_crossover_preview",
        lambda *_args: (
            None,
            [
                {"message": "first problem"},
                {"code": "second_problem"},
                {"message": "not surfaced"},
            ],
            [],
        ),
    )

    with pytest.raises(
        ValueError,
        match=(
            r"^active speaker preset is not ready for capture analysis: "
            r"first problem; second_problem$"
        ),
    ):
        commission_wiring.resolve_capture_preset(object())


def test_capture_preset_uses_configured_fallback(monkeypatch) -> None:
    calls: list[str | None] = []
    fallback = object()

    def fake_load(path: str | None) -> object:
        calls.append(path)
        return fallback

    monkeypatch.setattr(
        commission_wiring,
        "resolve_commission_inputs",
        lambda: (None, None),
    )
    monkeypatch.setattr(
        tone_plan,
        "load_active_speaker_preset",
        fake_load,
    )
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_PRESET", "/tmp/custom.json")

    assert commission_wiring.resolve_capture_preset(object()) is fallback
    assert calls == ["/tmp/custom.json"]


def _preset_declaring(ceiling: object) -> object:
    return SimpleNamespace(
        safety=SimpleNamespace(max_commissioning_level_db_spl=ceiling)
    )


def test_commissioning_ceiling_is_the_presets_own_finite_declaration(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        commission_wiring,
        "resolve_capture_preset",
        lambda _topology: _preset_declaring(82.5),
    )

    assert commission_wiring.commissioning_spl_ceiling_db(object()) == 82.5


@pytest.mark.parametrize("declared", [float("nan"), float("inf"), None, "loud"])
def test_commissioning_ceiling_refuses_a_declaration_that_is_not_finite(
    monkeypatch, declared
) -> None:
    """The stop is a hard one: no ceiling at all rather than a bogus number."""
    monkeypatch.setattr(
        commission_wiring,
        "resolve_capture_preset",
        lambda _topology: _preset_declaring(declared),
    )

    with pytest.raises(ValueError):
        commission_wiring.commissioning_spl_ceiling_db(object())
