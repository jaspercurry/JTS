# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The commissioning SPL stop has ONE owner: the ``SafetyEnvelope`` default.

Owner ruling 2026-08-23: the stop is 85 dB SPL — the dataclass's own default
and the top of the 45-85 band ``SafetyEnvelope.validate`` already enforces.
It is not a measurement claim about any driver pair.

Why a tripwire and not just a constant: the live stop was never the default.
Two staging construction sites restated ``max_commissioning_level_db_spl=80.0``
and the bundled epique preset declared 80, so reading the dataclass gave the
wrong answer about what the bench would actually refuse. That hid the real
value and cost a bench night. This file pins both halves — no production site
restates the stop, and every arm that resolves a preset yields the ruled 85.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from jasper.active_speaker import commission_wiring, staging, tone_plan
from jasper.active_speaker.crossover_preview import build_crossover_preview
from jasper.active_speaker.profile import ActiveSpeakerConfigError, SafetyEnvelope
from jasper.active_speaker.seat_level_reference import (
    DEFAULT_TOLERANCE_DB,
    SeatLevelTarget,
)
from jasper.active_speaker.tone_plan import load_active_speaker_preset
from tests.active_speaker_fixtures import (
    mono_output_topology,
    standard_design_draft,
)
from tests.test_active_speaker_local_subwoofer import _passive_1way_sub_topology_fc

_REPO = Path(__file__).resolve().parent.parent
_JASPER = _REPO / "jasper"
_PRESETS = _JASPER / "active_speaker" / "presets"

# The ruled stop. Sourced from the dataclass rather than restated as a literal,
# because a literal here would be the very defect this file exists to catch.
RULED_STOP_DB_SPL = SafetyEnvelope().max_commissioning_level_db_spl

_STOP_KWARG = "max_commissioning_level_db_spl"


def _restating_sites(tree: ast.AST) -> list[int]:
    """Line numbers of ``SafetyEnvelope(...)`` calls that could set the stop.

    The explicit keyword is only the obvious route. Two more reach the same
    field without naming it, so all three are flagged:

    * ``max_commissioning_level_db_spl=`` — the plain restatement;
    * ANY positional argument — field ORDER decides what a positional sets, so
      a positional stop names nothing a grep or a reader could see, and no
      site needs positional construction here;
    * a ``**splat`` — its keys are not literals, so no AST scan can read them.
    """
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if name != "SafetyEnvelope":
            continue
        names_the_stop = any(kw.arg == _STOP_KWARG for kw in node.keywords)
        splats = any(kw.arg is None for kw in node.keywords)
        if names_the_stop or splats or node.args:
            lines.append(node.lineno)
    return lines


def test_the_ruled_stop_is_the_dataclass_default() -> None:
    """The ruling in one assertion, so a silent default change is loud."""
    assert RULED_STOP_DB_SPL == 85.0
    # The ruled value is the validated ceiling, not an arbitrary number.
    SafetyEnvelope(max_commissioning_level_db_spl=85.0).validate()
    with pytest.raises(ActiveSpeakerConfigError):
        SafetyEnvelope(max_commissioning_level_db_spl=85.1).validate()


def test_no_production_site_restates_the_commissioning_stop() -> None:
    """No module under ``jasper/`` passes the stop to ``SafetyEnvelope(...)``.

    Scope floor — what this actually scans, stated so nobody reads it as
    broader than it is: every ``jasper/**/*.py`` file, AST-based (so prose
    naming the kwarg does not count), matching the call by the literal name
    ``SafetyEnvelope``.

    Three things it deliberately does NOT cover, each for a reason:

    * ``tests/`` — a test constructing an envelope with an explicit stop is
      exercising ``validate``, which is legitimate;
    * preset JSON data — a stored envelope declares its own stop, and
      ``SafetyEnvelope.from_mapping`` is its reader (the bundled files are
      covered by ``test_every_bundled_preset_declares_the_ruled_stop``);
    * in-class construction spelled ``cls(...)`` rather than by name. That is
      exactly ``from_mapping``, the deserializer, which MUST pass the field
      because it is reading stored data — and which since the 2026-08-23
      ruling defaults it from ``cls.max_commissioning_level_db_spl`` rather
      than a literal of its own. A name-matched detector cannot see ``cls``;
      an allowlist would only re-admit the one site it would flag.
    """
    offenders: dict[str, list[int]] = {}
    for path in sorted(_JASPER.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a parse failure is another test's job
            continue
        lines = _restating_sites(tree)
        if lines:
            offenders[str(path.relative_to(_REPO))] = lines

    assert offenders == {}, (
        f"{_STOP_KWARG} is restated at {offenders}. The stop rides the "
        "SafetyEnvelope dataclass default (owner ruling 2026-08-23); delete "
        "the kwarg rather than setting it at a construction site."
    )


def test_every_bundled_preset_declares_the_ruled_stop() -> None:
    """Every shipped preset states the ruled stop — no per-preset asymmetry.

    Floor: whatever is under ``jasper/active_speaker/presets/``.
    """
    declared = {
        path.name: json.loads(path.read_text(encoding="utf-8"))["safety"][_STOP_KWARG]
        for path in sorted(_PRESETS.glob("*.json"))
    }

    assert declared == {
        "epique_e150he44_eminence_f110m8_safe_v1.json": 85,
    }


def test_preview_compiled_preset_rides_the_ruled_stop() -> None:
    """Arm 2 of the resolve chain: a compiled crossover preview."""
    topology = mono_output_topology()
    preview = build_crossover_preview(
        standard_design_draft(topology), created_at="2026-08-23T12:00:00Z"
    )

    preset, issues, _gates = staging.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert [i for i in issues if i.get("severity") == "blocker"] == []
    assert preset is not None
    assert preset.safety.max_commissioning_level_db_spl == RULED_STOP_DB_SPL
    # The two envelope fields that DIFFER from their dataclass defaults stay
    # restated, and are pinned here only so a later cleanup cannot delete them
    # as if they were restatements too. This asserts what they ARE, not what
    # they do: neither has a production reader today (pre-existing, and not
    # this change's to fix).
    assert preset.safety.initial_sweep_level_db_spl == 55.0
    assert preset.safety.escalation_step_db == 1.0


def test_passive_sub_preset_rides_the_ruled_stop() -> None:
    """The sibling compile site (passive mains + local sub) rides it too."""
    preset, issues, _gates = staging.build_passive_mains_preset(
        _passive_1way_sub_topology_fc(120.0)
    )

    assert [i for i in issues if i.get("severity") == "blocker"] == []
    assert preset is not None
    assert preset.safety.max_commissioning_level_db_spl == RULED_STOP_DB_SPL


def test_capture_preset_bundled_fallback_arm_yields_the_ruled_stop(
    monkeypatch,
) -> None:
    """Arm 3: no ready preview, so the bundled preset answers.

    This is the arm jts3 resolves through today (its preview is stale), so it
    is the one that decides what the bench actually refuses.
    """
    monkeypatch.delenv("JASPER_ACTIVE_SPEAKER_PRESET", raising=False)
    monkeypatch.setattr(
        commission_wiring, "resolve_commission_inputs", lambda: (None, None)
    )

    preset = commission_wiring.resolve_capture_preset(mono_output_topology())

    assert preset.preset_id == "epique-e150he44-eminence-f110m8-safe-v1"
    assert preset.safety.max_commissioning_level_db_spl == RULED_STOP_DB_SPL
    # The bundled default IS the file the fallback arm loads.
    assert (
        tone_plan.DEFAULT_PRESET_RESOURCE
        == "presets/epique_e150he44_eminence_f110m8_safe_v1.json"
    )


def test_capture_preset_explicit_arm_passes_its_presets_stop_through(
    monkeypatch,
) -> None:
    """Arm 1 is a pass-through: it yields whatever preset it is handed.

    Stated honestly rather than claimed as a guarantee — the explicit arm
    cannot itself promise 85; it promises not to rewrite the stop. Handed the
    bundled preset, it yields the ruled stop.
    """
    explicit = load_active_speaker_preset()
    monkeypatch.setattr(
        commission_wiring, "resolve_commission_inputs", lambda: (explicit, None)
    )

    preset = commission_wiring.resolve_capture_preset(mono_output_topology())

    assert preset is explicit
    assert preset.safety.max_commissioning_level_db_spl == RULED_STOP_DB_SPL


def test_ruled_75_db_seat_frame_validates_under_the_ruled_stop() -> None:
    """The frame the ruling exists to let converge: 75 dB SPL, band 72.5-77.5.

    Under the old 80.0 stop the band itself was already legal; what tripped
    was the measured post-step transient, which the ramp compares against the
    same ceiling. This pins the band half.
    """
    target = SeatLevelTarget(target_db_spl=75.0, tolerance_db=DEFAULT_TOLERANCE_DB)

    assert (target.low_db_spl, target.high_db_spl) == (72.5, 77.5)
    target.validate(ceiling_db_spl=RULED_STOP_DB_SPL)
