# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from jasper.active_speaker import (
    CROSSOVER_PREVIEW_PATH_ENV,
    DESIGN_DRAFT_PATH_ENV,
    DRIVER_RESEARCH_KIND,
    save_crossover_preview,
    save_design_draft,
)
from jasper.active_speaker import crossover_preview, design_draft
from jasper.active_speaker.commission_wiring import resolve_commission_inputs
from tests.active_speaker_fixtures import mono_output_topology


def _minimal_research() -> dict[str, object]:
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": [
            {"role": "woofer", "model": "Test woofer"},
            {
                "role": "tweeter",
                "model": "Test tweeter",
                "do_not_test_below_hz": 1200,
            },
        ],
        "crossover_candidates": [
            {
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": 2500,
                "confidence": "medium",
            }
        ],
    }


def test_explicit_preset_preserves_identity_and_skips_persisted_loaders(
    monkeypatch,
) -> None:
    draft_loader = Mock(side_effect=AssertionError("design draft loader called"))
    preview_loader = Mock(side_effect=AssertionError("crossover preview loader called"))
    monkeypatch.setattr(design_draft, "load_design_draft", draft_loader)
    monkeypatch.setattr(crossover_preview, "load_crossover_preview", preview_loader)
    preset = object()

    resolved_preset, resolved_preview = resolve_commission_inputs(preset)

    assert resolved_preset is preset
    assert resolved_preview is None
    draft_loader.assert_not_called()
    preview_loader.assert_not_called()


def test_fresh_saved_crossover_preview_is_the_commissioning_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    draft_path = tmp_path / "design-draft.json"
    preview_path = tmp_path / "crossover-preview.json"
    monkeypatch.setenv(DESIGN_DRAFT_PATH_ENV, str(draft_path))
    monkeypatch.setenv(CROSSOVER_PREVIEW_PATH_ENV, str(preview_path))
    draft = save_design_draft(
        mono_output_topology(),
        driver_research=_minimal_research(),
        path=draft_path,
        created_at="2026-07-12T12:00:00Z",
    )
    saved_preview = save_crossover_preview(
        draft,
        path=preview_path,
        created_at="2026-07-12T12:01:00Z",
    )

    resolved_preset, resolved_preview = resolve_commission_inputs()

    assert saved_preview["status"] == "ready_for_protected_staging"
    assert resolved_preset is None
    assert resolved_preview is not saved_preview
    assert resolved_preview == saved_preview


def test_blocked_saved_crossover_preview_is_not_used_for_commissioning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    draft_path = tmp_path / "design-draft.json"
    preview_path = tmp_path / "crossover-preview.json"
    monkeypatch.setenv(DESIGN_DRAFT_PATH_ENV, str(draft_path))
    monkeypatch.setenv(CROSSOVER_PREVIEW_PATH_ENV, str(preview_path))
    draft = save_design_draft(
        mono_output_topology(),
        path=draft_path,
        created_at="2026-07-12T12:00:00Z",
    )
    saved_preview = save_crossover_preview(
        draft,
        path=preview_path,
        created_at="2026-07-12T12:01:00Z",
    )

    resolved_preset, resolved_preview = resolve_commission_inputs()

    assert saved_preview["status"] == "blocked"
    assert (resolved_preset, resolved_preview) == (None, None)
