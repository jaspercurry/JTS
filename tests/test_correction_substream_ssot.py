# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: one owner for the correction/commissioning fan-in lane name
(issue #1789).

``jasper.audio_measurement.correction_lane.DEFAULT_ALSA_DEVICE`` is the single
source of truth for the ALSA pcm alias ``"correction_substream"``. Before this
test existed the literal was independently re-declared under eight-plus
differently-named constants (and a few bare literals) across
``jasper.correction``, ``jasper.active_speaker``, ``jasper.web``, and
``jasper.cli`` — nothing enforced that the copies agreed, so a rename would
have silently diverged.

Two checks:

  1. **The drift guard.** No file under ``jasper/`` other than the owning
     module declares a string constant whose value is EXACTLY
     ``"correction_substream"``. This is the test that fails when someone
     adds a ninth copy. Matched by VALUE from the parsed AST — the same
     technique ``tests/test_correction_boundary_ssot.py`` uses for the
     room-correction band edge — so prose that merely *mentions* the lane
     (docstrings, comments, diagnostic messages) is not a false positive,
     and neither are the differently-valued derived tags like
     ``"correction_substream_continuous_tone"``: those are different string
     values, not this one.
  2. **Co-ownership.** The sites that used to hold their own named copy of
     the literal still resolve to the SSOT's value, not a reintroduced local
     override.
"""
from __future__ import annotations

import ast
from pathlib import Path

from jasper.active_speaker import program_playback, web_commissioning
from jasper.audio_measurement.correction_lane import DEFAULT_ALSA_DEVICE
from jasper.correction import playback
from jasper.web import balance_flow, sound_setup, sync_flow

REPO_ROOT = Path(__file__).resolve().parents[1]
JASPER_ROOT = REPO_ROOT / "jasper"

# The one file allowed to declare the literal.
OWNING_MODULE = JASPER_ROOT / "audio_measurement" / "correction_lane.py"


def _redeclares_literal(path: Path) -> bool:
    """True if any string constant in `path` is EXACTLY the lane literal.

    AST-based and matched by value rather than scanned as text, for the same
    reason ``test_correction_boundary_ssot.py`` parses rather than
    text-searches: prose that merely names the lane is not a re-declaration
    (its literal is a longer string, not an exact match), and neither is a
    longer derived value like ``"correction_substream_continuous_tone"``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.Constant) and node.value == DEFAULT_ALSA_DEVICE
        for node in ast.walk(tree)
    )


def test_no_new_literal_declarations_outside_the_owning_module() -> None:
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted(JASPER_ROOT.rglob("*.py"))
        if path != OWNING_MODULE and _redeclares_literal(path)
    ]
    assert not offenders, (
        "the correction/commissioning fan-in lane literal "
        f"{DEFAULT_ALSA_DEVICE!r} was re-declared outside its owning module "
        f"({OWNING_MODULE.relative_to(REPO_ROOT)}) — import "
        "DEFAULT_ALSA_DEVICE from jasper.audio_measurement.correction_lane "
        "instead of spelling the string again:\n" + "\n".join(offenders)
    )


def test_owning_module_value_is_the_expected_lane_name() -> None:
    # Pins the value itself, so a silent edit to the SSOT is visible here
    # rather than only surfacing later as a runtime ALSA "no such pcm"
    # failure.
    assert DEFAULT_ALSA_DEVICE == "correction_substream"


def test_known_consuming_sites_resolve_to_the_ssot() -> None:
    """The sites that used to hold their own named copy still agree.

    Each of these imports ``DEFAULT_ALSA_DEVICE`` under its own historical
    local name (``as``-import or same-file alias) rather than re-declaring
    the string — see each module for the exact wiring.
    """
    assert playback.DEFAULT_ALSA_DEVICE == DEFAULT_ALSA_DEVICE
    assert program_playback.CORRECTION_SUBSTREAM == DEFAULT_ALSA_DEVICE
    assert web_commissioning.COMMISSION_TONE_ALSA_DEVICE == DEFAULT_ALSA_DEVICE
    assert sound_setup.VOLUME_FLOOR_TONE_ALSA_DEVICE == DEFAULT_ALSA_DEVICE
    assert sound_setup.COMMISSION_TONE_ALSA_DEVICE == DEFAULT_ALSA_DEVICE
    assert sync_flow.PLAYBACK_DEVICE == DEFAULT_ALSA_DEVICE
    assert balance_flow.PLAYBACK_DEVICE == DEFAULT_ALSA_DEVICE


def test_guard_detects_a_reintroduced_literal(tmp_path) -> None:
    """The guard must fail on what it exists to catch, and not on prose."""
    offender = tmp_path / "offender.py"
    offender.write_text('ALSA_DEVICE = "correction_substream"\n')
    assert _redeclares_literal(offender)

    clean = tmp_path / "clean.py"
    clean.write_text(
        '"""Docstring mentioning correction_substream in prose."""\n'
        "# comment about correction_substream\n"
        'DERIVED_TAG = "correction_substream_continuous_tone"\n'
        "from jasper.audio_measurement.correction_lane import DEFAULT_ALSA_DEVICE\n"
        "ALSA_DEVICE = DEFAULT_ALSA_DEVICE\n"
    )
    assert not _redeclares_literal(clean)
